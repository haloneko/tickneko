"""工作流 JSON 入库前的校验流水线：结构 → 拓扑 → 语义（→ 将来还有 Dry Run）。

一句话：**结构对不对 → 走不走得通 → 跑不跑得动 →（试不试一遍）→ 入库**。三个阶段由
:func:`validate_graph` 同步跑完，阶段间**短路**（前一阶段没过后面不跑）；阶段内把错误
**收齐**再返回（前端一次性画出所有红点）。第四阶段 Dry Run 目前只留了阶段名常量
``STAGE_DRY_RUN``，协议与实现都还没接。

校验规则**全部从节点注册表推导**，新增节点类型不需要改本文件；语义阶段最主要的活是
**检查连线**（见 :func:`_port_wiring`）；**孤儿节点允许存在** —— 只保证「start 沿出边能
展开到 end」，但孤儿连出来的线不算数。三阶段各自查什么、错误码清单见
``docs/workflow/workflow.md`` 第 2 节。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from difflib import get_close_matches
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from .graph import edge_source_port, edge_target_port, out_targets, reachable_from, start_ids
from .models import (
    STAGE_SEMANTIC,
    STAGE_STRUCTURE,
    STAGE_TOPOLOGY,
    ValidationIssue,
    ValidationReport,
    WorkflowGraph,
)
from .nodes.base import MISSING_DEFAULT, PortSpec
from .nodes.port_types import port_types_compatible
from .nodes.registry import get_spec, registered_types


# --------------------------------------------------------------------------- 表达式引擎位
@runtime_checkable
class ExpressionSyntaxChecker(Protocol):
    """表达式语法检查器（**只 parse 不执行**）的协议位。

    默认实现 :class:`AcceptAllExpressions` 全放行——执行引擎 / 表达式库还没接，
    先把阶段与错误结构定下来；接入时传一个「parse 失败就返回错误描述」的实现即可。
    """

    def check(self, node_id: str, expression: str) -> ValidationIssue | None:
        """语法树构建成功返回 None；失败返回一条语义错误（code=EXPRESSION_SYNTAX）。"""
        ...


class AcceptAllExpressions:
    """默认表达式检查器：不执行任何语法判断（执行引擎就位前的占位）。"""

    def check(self, node_id: str, expression: str) -> ValidationIssue | None:
        return None


# --------------------------------------------------------------------------- 入口
def validate_graph(
    raw: dict[str, Any] | WorkflowGraph,
    *,
    expression_checker: ExpressionSyntaxChecker | None = None,
) -> ValidationReport:
    """跑前三阶段（结构 / 拓扑 / 语义），返回完整报告；不抛「图内容」类异常。

    :param raw: 前端提交的图 dict（或已是 :class:`WorkflowGraph`——那时结构阶段照跑一遍
        不变量检查，只是不会有解析错误）；
    :param expression_checker: 表达式语法检查器，默认全放行（占位，见类文档）。
    """
    graph, structure_errors = _structure_stage(raw)
    # 结构阶段要么给图、要么给错误（见它的返回值约定），所以「没图」就等于有结构错误
    if graph is None:
        return ValidationReport.reject(STAGE_STRUCTURE, structure_errors)

    # 默认值字段先补到解析出的图上再进拓扑 / 语义：节点的自定义校验器看到的是补全后的
    # config（缺省值不需要每个校验器自己猜）。这里只动本轮解析出的模型，不回写入参 dict；
    # 保存版本时 router 会再幂等补一遍并落库。
    graph = apply_config_defaults(graph)

    topology_errors, reachable = _topology_stage(graph)
    if topology_errors:
        return ValidationReport.reject(STAGE_TOPOLOGY, topology_errors)

    checker: ExpressionSyntaxChecker = (
        expression_checker if expression_checker is not None else AcceptAllExpressions()
    )
    semantic_errors = _semantic_stage(graph, reachable, checker)
    if semantic_errors:
        return ValidationReport.reject(STAGE_SEMANTIC, semantic_errors)

    return ValidationReport.ok()


def apply_config_defaults(raw: dict[str, Any] | WorkflowGraph) -> WorkflowGraph:
    """按各类型注册的默认值字段补全 config，返回补全后的图（原 dict 不被修改）。

    在**保存版本**时、校验通过之后调用：键缺失或值为 None 的默认值字段用注册时声明的
    默认值代替（如 start.trigger=message、log.level=INFO、http.timeout=10），
    让落库快照配置完整、运行期不必再猜缺省。
    """
    graph = raw if isinstance(raw, WorkflowGraph) else WorkflowGraph.model_validate(raw)
    for node in graph.nodes:
        spec = get_spec(node.type)
        if spec is None:
            continue
        for field in spec.fields:
            if field.default is not MISSING_DEFAULT and node.config.get(field.name) is None:
                node.config[field.name] = field.default
    return graph


# --------------------------------------------------------------------------- ① 结构
def _structure_stage(raw: dict[str, Any] | WorkflowGraph) -> tuple[WorkflowGraph | None, list[ValidationIssue]]:
    """结构阶段：必填字段 / 节点 ID 唯一 / 边的端点存在 / 可达节点类型已注册。

    返回 ``(解析出的图, 错误)``：有错时图是 None。pydantic 的字段级错误在这里翻译成
    业务错误码，不把库的错误原文漏给前端。

    类型是否合法查注册表；**孤儿节点的类型不查**——没接进主流程的节点允许保存。
    """
    if isinstance(raw, WorkflowGraph):
        graph, errors = raw, []
    else:
        parsed, errors = _parse_graph(raw)
        # 解析不出来（有错）就直接回错误；过了这一步 graph 一定在
        if errors or parsed is None:
            return None, errors
        graph = parsed

    seen: set[str] = set()
    for node in graph.nodes:
        if not node.id.strip():
            errors.append(
                ValidationIssue(
                    code="EMPTY_NODE_ID",
                    message="存在空的节点 ID",
                    suggestion="给每个节点一个非空 ID",
                )
            )
        elif node.id in seen:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="DUPLICATE_NODE_ID",
                    message=f"节点 ID {node.id} 重复",
                    suggestion="节点 ID 必须在一张图内全局唯一",
                )
            )
        else:
            seen.add(node.id)

    for index, edge in enumerate(graph.edges):
        if edge.source not in seen:
            errors.append(
                ValidationIssue(
                    node_id=edge.source,
                    code="EDGE_ENDPOINT_MISSING",
                    message=f"第 {index + 1} 条边的起点 {edge.source!r} 不存在",
                    suggestion="边的 source 必须指向一个存在的节点 ID",
                )
            )
        if edge.target not in seen:
            errors.append(
                ValidationIssue(
                    node_id=edge.target,
                    code="EDGE_ENDPOINT_MISSING",
                    message=f"第 {index + 1} 条边的终点 {edge.target!r} 不存在",
                    suggestion="边的 target 必须指向一个存在的节点 ID",
                )
            )
    if errors:
        return None, errors

    # 边端点都有效后算可达域：唯一 start 时只查主流程上的类型；start 数量异常（0 或多个）
    # 时没法界定主流程，退回查全部节点——拓扑阶段随后会报 START_NOT_UNIQUE
    starts = start_ids(graph.nodes)
    if len(starts) == 1:
        scope = reachable_from(out_targets(graph), starts[0])
    else:
        scope = set(seen)
    for node in graph.nodes:
        if node.id in scope and get_spec(node.type) is None:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="UNKNOWN_NODE_TYPE",
                    message=f"节点 {node.id} 的类型 {node.type!r} 未注册",
                    suggestion=_renamed_node_hint(node.type)
                    or f"已注册类型：{', '.join(registered_types())}",
                )
            )
    return (graph if not errors else None), errors


#: 拆过 / 改过名的旧类型 -> 换成什么（老图报错时直接指路，别让人对着「未注册」猜）
_RENAMED_NODE_TYPES: dict[str, str] = {
    "start": (
        "start 已拆成三个触发器：trigger-message（消息触发）/ trigger-time（定时触发）"
        "/ trigger-event（事件触发）—— 按原来 config.trigger 的取值换成对应那个"
    ),
}


def _renamed_node_hint(node_type: str) -> str:
    """旧类型换名提示；不是换过的类型返回空串（调用方退到「已注册类型」兜底）。"""
    return _RENAMED_NODE_TYPES.get(node_type, "")


def _parse_graph(raw: Any) -> tuple[WorkflowGraph | None, list[ValidationIssue]]:
    """把任意 JSON 入参解析成图；解析失败翻译成结构错误（而非库异常）。"""
    if not isinstance(raw, dict):
        return None, [
            ValidationIssue(
                code="GRAPH_NOT_OBJECT",
                message="工作流定义必须是一个对象，包含 nodes 与 edges",
                suggestion="检查提交的 JSON 顶层结构",
            )
        ]
    try:
        return WorkflowGraph.model_validate(raw), []
    except ValidationError as exc:
        issues: list[ValidationIssue] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ()))
            issues.append(
                ValidationIssue(
                    node_id=_node_id_of_loc(loc, raw),
                    code="INVALID_GRAPH_SCHEMA",
                    message=f"字段 {loc or '<根>'} 不合法：{err.get('msg', '类型错误')}",
                    suggestion="检查节点 id / type 与 edges 的 source / target 是否齐全",
                )
            )
        return None, issues


def _node_id_of_loc(loc: str, raw: dict[str, Any]) -> str:
    """从 pydantic 错误定位（如 ``nodes.2.id``）里尽量抠出节点 ID，抠不到就空串。"""
    parts = loc.split(".")
    if len(parts) >= 2 and parts[0] == "nodes":
        try:
            node = raw["nodes"][int(parts[1])]
            if isinstance(node, dict):
                return str(node.get("id", ""))
        except (KeyError, IndexError, TypeError, ValueError):
            return ""
    return ""


# --------------------------------------------------------------------------- ② 拓扑
def _topology_stage(
    graph: WorkflowGraph,
) -> tuple[list[ValidationIssue], set[str]]:
    """拓扑阶段（只看 start 可达的主流程）：start 唯一 / end≥1 可达 / 无环 / 注册的出入边约束。

    返回 ``(错误, start 可达节点集合)``：语义阶段只检查可达集合里的节点。
    孤儿节点（不可达）不产生任何错误——它们不会被执行，允许先画在画布上保存。
    """
    errors: list[ValidationIssue] = []
    out_edges = out_targets(graph)

    starts = start_ids(graph.nodes)
    if len(starts) != 1:
        errors.append(
            ValidationIssue(
                code="START_NOT_UNIQUE",
                message=f"触发节点必须有且仅有 1 个，当前有 {len(starts)} 个",
                suggestion="保留一个触发节点作为唯一入口（消息触发 / 定时触发 / 事件触发）",
            )
        )
        # 入口不唯一时主流程无从界定，后续检查不跑（可达域给空集，语义阶段也不会误报）
        return errors, set()

    start_id = starts[0]
    reachable = reachable_from(out_edges, start_id)

    # 主流程上至少一个 end 角色节点
    if not any(
        (spec := get_spec(node.type)) is not None and spec.role == "end"
        for node in graph.nodes
        if node.id in reachable
    ):
        errors.append(
            ValidationIssue(
                code="END_MISSING",
                message="从触发节点可达的路径上没有 end 节点（至少 1 个）",
                suggestion="给主流程接一个 end 出口（没接进来的 end 不算）",
            )
        )

    # 主流程内的入度（只计可达边）与自环
    in_degree: dict[str, int] = {node_id: 0 for node_id in reachable}
    for node_id in reachable:
        for target in out_edges[node_id]:
            if target == node_id:
                errors.append(
                    ValidationIssue(
                        node_id=node_id,
                        code="SELF_LOOP",
                        message=f"节点 {node_id} 存在指向自己的边",
                        suggestion="去掉自环（审批节点也不能自己连自己）",
                    )
                )
            if target in in_degree:
                in_degree[target] += 1

    # 各类型注册的出入边条数约束（分流类节点至少 2 条出边、end 不许有出边……）
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is None:  # 结构阶段已拦，防御性跳过
            continue
        outgoing = out_edges[node.id]
        if len(outgoing) < spec.min_outgoing:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    # 码名是历史遗留（当初只有分流类节点会声明 min_outgoing）；现在任何类型都能
                    # 登记这个下限，语义就是「出边不够」。改名属接口可见变更，先留原名。
                    code="GATEWAY_NEEDS_BRANCHES",
                    message=(
                        f"节点 {node.id}（{node.type}）至少要有 {spec.min_outgoing} 条出边，"
                        f"当前 {len(outgoing)} 条"
                    ),
                    suggestion="给每个分支各接一条出边",
                )
            )
        if spec.max_outgoing is not None and len(outgoing) > spec.max_outgoing:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="END_HAS_OUTGOING",
                    message=f"节点 {node.id}（{node.type}）最多允许 {spec.max_outgoing} 条出边",
                    suggestion="该类型是终点语义，删掉它后面的边",
                )
            )

    # Kahn 拓扑排序（仅可达子图）：能取完 = 主流程是 DAG；取不完 = 剩下的全在环上。
    # 孤儿组件里就算有环也不在可达子图内——永不执行，不拦。
    remaining = deque(node_id for node_id in reachable if in_degree[node_id] == 0)
    visited = 0
    degrees = dict(in_degree)
    while remaining:
        current = remaining.popleft()
        visited += 1
        for target in out_edges[current]:
            if target not in degrees:
                continue
            degrees[target] -= 1
            if degrees[target] == 0:
                remaining.append(target)
    if visited != len(reachable):
        cyclic = sorted(node_id for node_id, degree in degrees.items() if degree > 0)
        errors.append(
            ValidationIssue(
                node_id=cyclic[0] if cyclic else start_id,
                code="CYCLE_DETECTED",
                message=f"主流程存在环路，涉及节点：{', '.join(cyclic)}",
                suggestion="工作流必须是 DAG：断开回边或改用循环节点表达重复执行",
            )
        )
    return errors, reachable


# --------------------------------------------------------------------------- ③ 语义
def _semantic_stage(
    graph: WorkflowGraph,
    reachable: set[str],
    checker: ExpressionSyntaxChecker,
) -> list[ValidationIssue]:
    """语义阶段（只看主流程可达节点）：注册字段必填 → 节点自注册校验 → 表达式语法 → 连线。"""
    errors: list[ValidationIssue] = []
    errors.extend(_config_completeness(graph, reachable))
    errors.extend(_node_validators(graph, reachable))
    errors.extend(_expression_syntax(graph, reachable, checker))
    errors.extend(_port_wiring(graph, reachable))
    return errors


def _config_completeness(graph: WorkflowGraph, reachable: set[str]) -> list[ValidationIssue]:
    """③-C 各类型注册的必填 config 字段：缺失 / None / 空白字符串都算缺。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is None:
            continue
        for field in spec.fields:
            if not field.required:
                continue
            value = node.config.get(field.name)
            if _is_blank(value):
                label = f"（{field.label}）" if field.label else ""
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="MISSING_CONFIG",
                        message=f"{node.type} 节点 {node.id} 缺少必填配置 {field.name}{label}",
                        suggestion=f"在 config.{field.name} 中补充{field.label or '该字段'}",
                    )
                )
    return issues


def _node_validators(graph: WorkflowGraph, reachable: set[str]) -> list[ValidationIssue]:
    """③-D 各节点在注册时挂上的自定义校验器（枚举、条件必填等）。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is not None and spec.validator is not None:
            issues.extend(spec.validator(node))
    return issues


def _is_blank(value: Any) -> bool:
    """必填判定：``None`` 与空白字符串算「没填」，其余（含数字 0 / False）都算填了。"""
    return value is None or (isinstance(value, str) and not value.strip())


def _find_port(ports: Sequence[PortSpec], port_id: str) -> PortSpec | None:
    """在端口清单里按 id 找端口；找不到返回 None。"""
    for port in ports:
        if port.id == port_id:
            return port
    return None


def _port_hint(port_id: str, ports: Sequence[PortSpec]) -> str:
    """端口名拼错时给一句「是否想用 X」；没有相近的返回空串。"""
    matches = get_close_matches(port_id, [port.id for port in ports], n=1, cutoff=0.6)
    if not matches:
        return ""
    return f"端口 {port_id!r} 不存在，是否想用 {matches[0]!r}？"


def _port_wiring(graph: WorkflowGraph, reachable: set[str]) -> list[ValidationIssue]:
    """③-A 连线：端口存不存在 / 两端同不同类 / 数据入口必接且只接一条。

    值沿边走，所以这里是「值能不能送到该到的地方」的唯一关口。只看主流程上的边（两端都
    可达）：孤儿节点永不执行，它连出去的线不算数 —— 被孤儿喂着的必填入口照样算「没接上」。
    另外，**某类型完全没声明端口**时不查它的那一端（只 ``declare_node_type`` 占位的扩展
    节点没有「端口名对不对」可言），声明了才查。

    「一个入口只接一条线」这条只放行**控制流端口**（``trigger``）——它的多条入边是「汇聚」：
    多个上游都跑完才轮到本节点（执行器的入度排序天然如此），菱形 / 多分支汇流都靠它；
    其余端口（现在的 ``message``、将来新增的类型）都是「一个入口一份值」，多了没法选。
    """
    issues: list[ValidationIssue] = []
    node_type = {node.id: node.type for node in graph.nodes}
    main_edges = [
        edge for edge in graph.edges if edge.source in reachable and edge.target in reachable
    ]
    #: (目标节点, 目标端口) -> 接在上面的边条数：除了控制流端口（trigger），一个入口只允许一条
    incoming: dict[tuple[str, str], int] = {}

    for edge in main_edges:
        source_spec = get_spec(node_type[edge.source])
        target_spec = get_spec(node_type[edge.target])
        if source_spec is None or target_spec is None:  # 结构阶段已拦，这里只防御
            continue
        # 两端各自独立判断：**该类型声明了端口才查这一端**。完全没声明端口的类型（只
        # ``declare_node_type`` 占个位的扩展节点）按「端口未定义」处理，不报端口错。
        source_port = _find_port(source_spec.outputs, edge_source_port(edge))
        target_port = _find_port(target_spec.inputs, edge_target_port(edge))
        if source_spec.outputs and source_port is None:
            issues.append(
                ValidationIssue(
                    node_id=edge.source,
                    code="UNKNOWN_PORT",
                    message=(
                        f"{node_type[edge.source]} 节点 {edge.source} 没有输出端口 "
                        f"{edge_source_port(edge)!r}"
                    ),
                    suggestion=_port_hint(edge_source_port(edge), source_spec.outputs)
                    or "从面板右侧列出的输出端口里挑一个",
                )
            )
        if target_spec.inputs and target_port is None:
            issues.append(
                ValidationIssue(
                    node_id=edge.target,
                    code="UNKNOWN_PORT",
                    message=(
                        f"{node_type[edge.target]} 节点 {edge.target} 没有输入端口 "
                        f"{edge_target_port(edge)!r}"
                    ),
                    suggestion=_port_hint(edge_target_port(edge), target_spec.inputs)
                    or "从面板左侧列出的输入端口里挑一个",
                )
            )
        if (
            source_port is not None
            and target_port is not None
            and not port_types_compatible(source_port.type, target_port.type)
        ):
            issues.append(
                ValidationIssue(
                    node_id=edge.target,
                    code="PORT_TYPE_MISMATCH",
                    message=(
                        f"{edge.source}.{source_port.id}（{source_port.type}）接不到 "
                        f"{edge.target}.{target_port.id}（{target_port.type}）"
                    ),
                    suggestion=(
                        "数据流端口接数据流端口，触发端口（trigger）接触发端口；"
                        "泛型端口（generic）能接任意数据流端口、不接触发"
                    ),
                )
            )
        key = (edge.target, edge_target_port(edge))
        incoming[key] = incoming.get(key, 0) + 1

    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is None:  # 结构阶段已拦，这里只防御
            continue
        for port in spec.inputs:
            count = incoming.get((node.id, port.id), 0)
            # 只放行 trigger：它的多条入边是「汇聚」（上游都跑完才轮到本节点）；其余端口
            # （message 以及将来新增的类型）按「一个入口一份值」处理，写反了只会更严不会更松
            if port.type != "trigger" and count > 1:
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="DUPLICATE_INPUT_EDGE",
                        message=f"{node.type} 节点 {node.id} 的入口 {port.id} 接了 {count} 条线",
                        suggestion="一个入口只连一个上游：多余的线删掉，或先汇到一个节点再往下送",
                    )
                )
            # 必填入口：接了线（主流程上的线）或同名字段手填了内容，二者有其一即可
            if not port.required or count:
                continue
            if _is_blank(node.config.get(port.id)):
                label = f"（{port.label}）" if port.label else ""
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="INPUT_NOT_CONNECTED",
                        message=(
                            f"{node.type} 节点 {node.id} 的必填入口 {port.id}{label} "
                            "既没接线也没填内容"
                        ),
                        suggestion=(
                            f"从一个上游的输出端口连一根线到 {port.id}，"
                            f"或在 config.{port.id} 里手填内容"
                        ),
                    )
                )
    return issues


def _expression_syntax(
    graph: WorkflowGraph,
    reachable: set[str],
    checker: ExpressionSyntaxChecker,
) -> list[ValidationIssue]:
    """③-B 表达式语法：节点注册了 ``expression_field`` 就把该字段交给可替换的检查器。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is None or not spec.expression_field:
            continue
        expression = node.config.get(spec.expression_field)
        if isinstance(expression, str) and expression.strip():
            issue = checker.check(node.id, expression)
            if issue is not None:
                issues.append(issue)
    return issues
