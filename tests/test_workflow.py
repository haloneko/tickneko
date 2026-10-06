"""工作流框架的测试：校验流水线、双表存储（版本 / 隔离 / 去重）、HTTP 接口。

分三块：

* 校验器：结构 → 拓扑 → 语义三阶段短路与各类错误码（纯函数，不要库）；
* 存储：内存 sqlite 上验多用户隔离、版本自增、checksum 去重、发布与级联删除；
* 接口：``create_app`` + httpx ASGI 直连，验登录隔离、校验失败不写库、保存 / 发布链路。

需要 ``fastapi`` / ``httpx``（``pip install "tickneko[dev]"``），没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import pytest

pytest.importorskip("fastapi", reason="接口层要装 fastapi：pip install \"tickneko[api]\"")
pytest.importorskip("httpx", reason="接口层测试用 httpx 发请求：pip install \"tickneko[dev]\"")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from tickneko.api import ApiOptions, Pbkdf2PasswordHasher, create_app  # noqa: E402
from tickneko.core.logger import (  # noqa: E402
    BaseLogProcessor,
    LogCore,
    LogRecord,
    LogSearchResult,
    configure,
    manager,
)
from tickneko.wiring import wire_loggers  # noqa: E402
from tickneko.workflow import (  # noqa: E402
    ConfigField,
    NodeExecutionContext,
    NodeSpec,
    PortSpec,
    SimpleWorkflowRunner,
    SqlWorkflowStore,
    ValidationIssue,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNameConflict,
    WorkflowNode,
    apply_config_defaults,
    canonical_graph_json,
    declare_node_type,
    graph_checksum,
    input_value,
    load_node_modules,
    register_node,
    registered_types,
    validate_graph,
)
from tickneko.workflow.executor import get_executor  # noqa: E402
from tickneko.workflow.nodes import (  # noqa: E402
    exec_cache,
    exec_condition,
    exec_http,
    exec_json,
    exec_now,
    exec_operator,
    exec_placeholder,
    exec_regex,
)
from tickneko.workflow.validator import STAGE_SEMANTIC, STAGE_STRUCTURE, STAGE_TOPOLOGY  # noqa: E402

#: 演示账号只剩 admin（id 即 u-admin）；robot 由 :func:`login` 顺手注册（id 还是 u-robot）
ADMIN = {"account": "admin", "password": "tickneko-admin"}
ROBOT = {"account": "robot", "password": "tickneko-robot", "nickname": "巡检机器人"}
_TEST_HASHER = Pbkdf2PasswordHasher(iterations=1_000)


# --------------------------------------------------------------------------- 图夹具
def node(node_id: str, node_type: str, **config: object) -> dict[str, object]:
    """造一个节点（config 直接平铺传）。"""
    return {"id": node_id, "type": node_type, "config": dict(config)}


def edge(
    source: str,
    target: str,
    source_port: str = "trigger",
    target_port: str = "trigger",
) -> dict[str, str]:
    """造一条边；端口缺省是「触发 -> 触发」（只表达先后的边，见 graph.DEFAULT_EDGE_PORT）。"""
    return {
        "source": source,
        "target": target,
        "source_port": source_port,
        "target_port": target_port,
    }


def linear_graph() -> dict[str, object]:
    """一张各阶段都该过的最小线性图：start -> end。"""
    return {"nodes": [node("s", "trigger-message"), node("e", "end")], "edges": [edge("s", "e")]}


# --------------------------------------------------------------------------- 日志采集
class LogCollector(BaseLogProcessor):
    """把**整条记录**收进口袋：断言日志字段（``owner_id`` / ``extra``）时只看消息不够。"""

    name: str = "collector"

    def __init__(self, records: list[LogRecord]) -> None:
        super().__init__(buffer_size=1, flush_interval=0)  # 逐条直写，不用等攒批
        self.records: list[LogRecord] = records

    async def write(self, records: list[LogRecord]) -> None:
        self.records.extend(records)

    async def search(
        self,
        *,
        query: str | None = None,
        level: object = None,
        start: object = None,
        end: object = None,
        logger_name: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> LogSearchResult:
        return LogSearchResult()


async def wait_for_records(records: list[LogRecord], *, count: int = 1) -> None:
    """等分发器把日志送到出口（分发是异步的，写完立刻断言会扑空）。"""
    for _ in range(100):
        if len(records) >= count:
            return
        await asyncio.sleep(0.01)


@asynccontextmanager
async def runtime_logs() -> AsyncIterator[list[LogRecord]]:
    """把**进程默认核心**换成带采集出口的一份，并把各业务模块的日志槽位指到它
    （产出「收到的记录」列表，退出时还原）。

    运行时模块的日志走 ``tickneko.workflow.runtime`` 的 ``_log()``，而它没有可从调用点注入的
    口子 —— 改造后走的是**装配槽位**（:func:`tickneko.wiring.wire_loggers` 存进去的核心）。
    这里换上新核心后重新 wire 一次，runtime 那几条日志就落进采集出口；退出时还原默认核心
    并清空槽位（下个用例由 conftest 重新装配）。
    """
    records: list[LogRecord] = []
    manager.reset()
    core: LogCore = configure(
        "tickneko", console=False, processors=[LogCollector(records)], dispatch_timeout=0.01
    )
    await core.start()
    wire_loggers(core)  # 槽位指到带采集出口的这份核心，runtime._log() 由此落进口袋
    try:
        yield records
    finally:
        await core.stop()
        manager.reset()  # 默认核心是进程级的：用完还回去，别影响别的用例
        wire_loggers(None)  # 槽位一并清空，避免指向已停止的核心


# --------------------------------------------------------------------------- ① 结构校验
def test_valid_linear_graph_passes() -> None:
    report = validate_graph(linear_graph())
    assert report.valid and report.errors == [] and report.stage is None


def test_structure_rejects_unknown_type_and_missing_nodes() -> None:
    bad_type = {"nodes": [node("s", "外星人"), node("e", "end")], "edges": [edge("s", "e")]}
    report = validate_graph(bad_type)
    assert not report.valid and report.stage == STAGE_STRUCTURE
    assert {issue.code for issue in report.errors} == {"UNKNOWN_NODE_TYPE"}

    assert validate_graph({"nodes": []}).stage == STAGE_STRUCTURE  # 空节点列表


def test_structure_rejects_duplicate_id_and_dangling_edge() -> None:
    graph = {
        "nodes": [node("s", "trigger-message"), node("s", "end")],
        "edges": [edge("s", "ghost")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_STRUCTURE
    codes = {issue.code for issue in report.errors}
    assert "DUPLICATE_NODE_ID" in codes
    assert "EDGE_ENDPOINT_MISSING" in codes


def test_structure_short_circuits_topology() -> None:
    """结构没过时不跑拓扑：两个 start 也不应该报 START_NOT_UNIQUE（短路）。"""
    graph = {
        "nodes": [node("s1", "trigger-message"), node("s2", "trigger-message"), node("e", "end")],
        "edges": [edge("s1", "ghost")],
    }
    report = validate_graph(graph)
    assert report.stage == STAGE_STRUCTURE
    assert all(issue.code != "START_NOT_UNIQUE" for issue in report.errors)


# --------------------------------------------------------------------------- ② 拓扑校验
def test_topology_start_and_end_counts() -> None:
    no_start = {"nodes": [node("e", "end")], "edges": []}
    report = validate_graph(no_start)
    assert not report.valid and report.stage == STAGE_TOPOLOGY
    assert {issue.code for issue in report.errors} == {"START_NOT_UNIQUE"}

    no_end = {"nodes": [node("s", "trigger-message")], "edges": []}
    codes = {issue.code for issue in validate_graph(no_end).errors}
    assert "END_MISSING" in codes


def test_topology_detects_cycle() -> None:
    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("a", "test"),
            node("b", "test"),
            node("e", "end"),
        ],
        "edges": [edge("s", "a"), edge("a", "b"), edge("b", "a"), edge("a", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_TOPOLOGY
    assert any(issue.code == "CYCLE_DETECTED" for issue in report.errors)


def test_topology_allows_orphans_but_still_rejects_main_path_self_loop() -> None:
    """孤儿节点（不可达）及其自环 / 环 / 缺配置一律放行；主路径上的自环仍要拦。"""
    with_orphan = {
        "nodes": [node("s", "trigger-message"), node("e", "end"), node("lonely", "test")],
        "edges": [edge("s", "e"), edge("lonely", "lonely")],
    }
    assert validate_graph(with_orphan).valid  # 孤儿自环不影响主流程

    # 孤儿组件内部成环 + 是个没配 url 的 http：照样允许保存
    orphan_mess = {
        "nodes": [
            node("s", "trigger-message"),
            node("e", "end"),
            node("o1", "http"),  # 缺必填 url/method，但不可达
            node("o2", "test"),
        ],
        "edges": [
            edge("s", "e"),
            edge("o1", "o2"),
            edge("o2", "o1"),  # 孤儿环
        ],
    }
    assert validate_graph(orphan_mess).valid

    # 未注册类型的孤儿也放行（插件没装 / 先画了再说）
    orphan_unknown = {
        "nodes": [node("s", "trigger-message"), node("e", "end"), node("x", "火星节点")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(orphan_unknown).valid

    # 主路径自环仍报错
    main_self_loop = {
        "nodes": [node("s", "trigger-message"), node("e", "end")],
        "edges": [edge("s", "e"), edge("s", "s")],
    }
    codes = {issue.code for issue in validate_graph(main_self_loop).errors}
    assert "SELF_LOOP" in codes


def test_topology_end_must_be_reachable_from_start() -> None:
    """end 存在但没接进主流程（另一个孤儿）不算数，报 END_MISSING。"""
    graph = {
        "nodes": [node("s", "trigger-message"), node("e", "end"), node("x", "test")],
        "edges": [edge("s", "x")],  # end 孤立
    }
    codes = {issue.code for issue in validate_graph(graph).errors}
    assert "END_MISSING" in codes


def test_topology_min_outgoing_comes_from_registration() -> None:
    """出边下限由注册时声明（``min_outgoing``）：少了就报 GATEWAY_NEEDS_BRANCHES。"""
    declare_node_type("test-split", min_outgoing=2)
    graph = {
        "nodes": [node("s", "trigger-message"), node("g", "test-split"), node("e", "end")],
        "edges": [edge("s", "g"), edge("g", "e")],
    }
    report = validate_graph(graph)
    assert report.stage == STAGE_TOPOLOGY
    assert any(issue.code == "GATEWAY_NEEDS_BRANCHES" for issue in report.errors)

    # 两条分支后转通过（继续跑到语义：这张图语义干净）
    graph["nodes"].append(node("e2", "end"))
    graph["edges"].append(edge("g", "e2"))
    assert validate_graph(graph).valid


def test_topology_end_with_outgoing_rejected() -> None:
    graph = {
        "nodes": [node("s", "trigger-message"), node("e", "end"), node("x", "log", message="hi")],
        "edges": [edge("s", "e"), edge("e", "x")],
    }
    codes = {issue.code for issue in validate_graph(graph).errors}
    assert "END_HAS_OUTGOING" in codes


# --------------------------------------------------------------------------- ③ 语义校验
def test_semantic_missing_required_config() -> None:
    """必填分两种：普通字段报 MISSING_CONFIG，数据入口没接线没填值报 INPUT_NOT_CONNECTED。

    ``http.method`` 有默认值（保存时补 GET），所以它不算缺；``constant.value`` 是必填且没有
    默认值的普通字段，缺了报 MISSING_CONFIG。
    """
    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("call", "http"),  # url 入口没接线也没填
            node("note", "log"),  # message 入口没接线也没填
            node("c", "constant"),  # value 必填、没有默认值
            node("e", "end"),
        ],
        "edges": [
            edge("s", "call"),
            edge("call", "note"),
            edge("note", "c"),
            edge("c", "e"),
        ],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    missing = [issue for issue in report.errors if issue.code == "MISSING_CONFIG"]
    assert {issue.node_id for issue in missing} == {"c"}
    unconnected = [issue for issue in report.errors if issue.code == "INPUT_NOT_CONNECTED"]
    assert {issue.node_id for issue in unconnected} == {"call", "note"}  # url / message


def test_semantic_checks_edge_ports() -> None:
    """连线就是数据契约：端口名写错 / 两端类型不配，都在语义阶段拦住。"""
    typo = {
        "nodes": [node("s", "trigger-message"), node("l", "log"), node("e", "end")],
        "edges": [
            edge("s", "l"),
            edge("l", "e"),
            edge("s", "l", "mesage", "message"),  # start 上没有 mesage 这个出口
        ],
    }
    report = validate_graph(typo)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    unknown = next(issue for issue in report.errors if issue.code == "UNKNOWN_PORT")
    assert unknown.node_id == "s" and "mesage" in unknown.message
    assert "message" in unknown.suggestion  # 拼错时给「是否想用」

    mismatch = {
        "nodes": [node("s", "trigger-message"), node("l", "log"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e"), edge("s", "l", "trigger", "message")],
    }
    codes = {issue.code for issue in validate_graph(mismatch).errors}
    assert "PORT_TYPE_MISMATCH" in codes


def test_semantic_generic_port_connects_any_data_port_not_trigger() -> None:
    """泛型端口（placeholder 透传口）：接任意数据流端口都放行、接触发端口报错。"""
    # 泛型 -> 会话定位（send 的 target 入口）：透传口接数据流端口，放行
    ok = {
        "nodes": [
            node("s", "trigger-message"),
            node("p", "placeholder"),
            node("d", "send", message="hi"),  # target 接线、message 手填，都不缺
            node("e", "end"),
        ],
        "edges": [
            edge("s", "p"),
            edge("p", "d", "value", "target"),  # generic -> target
            edge("d", "e"),
        ],
    }
    report = validate_graph(ok)
    assert report.valid, report.errors

    # 泛型 -> 触发：泛型只走数据流，接触发端口报 PORT_TYPE_MISMATCH
    bad = {
        "nodes": [
            node("s", "trigger-message"),
            node("p", "placeholder"),
            node("l", "log"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "p"),
            edge("p", "l", "value", "trigger"),  # generic -> trigger
            edge("l", "e"),
        ],
    }
    codes = {issue.code for issue in validate_graph(bad).errors}
    assert "PORT_TYPE_MISMATCH" in codes

    # 泛型 -> 泛型（占位 -> 占位）：同为数据流，放行
    through = {
        "nodes": [
            node("s", "trigger-message"),
            node("p1", "placeholder"),
            node("p2", "placeholder"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "p1"),
            edge("p1", "p2", "value", "value"),  # generic -> generic
            edge("p2", "e"),
        ],
    }
    assert validate_graph(through).valid


def test_semantic_one_data_input_takes_one_edge() -> None:
    """一个数据入口只允许接一条线（要合并就先汇到一个节点再往下送）。"""
    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("c", "constant", value="a"),
            node("l", "log"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "c"),
            edge("c", "l", "value", "message"),
            edge("s", "l", "message", "message"),  # 第二条线接到同一个入口
            edge("l", "e"),
        ],
    }
    codes = {issue.code for issue in validate_graph(graph).errors}
    assert "DUPLICATE_INPUT_EDGE" in codes


def test_port_tie_must_be_generic_and_point_to_a_real_port() -> None:
    """透传对 ``tie`` 写错是静默失效（画布上只表现为两端颜色对不上）—— 声明时当场报错。

    * 非泛型端口带 ``tie``：没意义（只有泛型「输入什么输出什么」才谈得上配对）；
    * ``tie`` 指向另一侧不存在的端口 id：查不到就当没配对，比报错更难查。
    """
    with pytest.raises(ValueError, match="generic"):
        PortSpec("text", "message", "文本", tie="text")

    with pytest.raises(ValueError, match="没有这个 id"):
        NodeSpec(
            node_type="bad-tie",
            inputs=[PortSpec("value", "generic", "透传值", tie="value")],
            outputs=[PortSpec("other", "generic", "别的出口")],  # 没有叫 value 的出口
        )

    # 正常配对：输入输出互相指认（placeholder 那样）不报错
    NodeSpec(
        node_type="ok-tie",
        inputs=[PortSpec("value", "generic", "透传值", tie="value")],
        outputs=[PortSpec("value", "generic", "透传结果", tie="value")],
    )


def test_semantic_trigger_ports_allow_convergence() -> None:
    """「只接一条线」只管数据入口：控制流端口允许多条入边汇聚（菱形 / 多分支汇流）。

    引擎按入度排序，两条 trigger 边汇到同一个 end 就是「都跑完才轮到它」；校验器不该拦。
    数据入口（message）照旧只允许一条 —— 两份值进同一个入口没法选。
    """
    diamond = {
        "nodes": [
            node("s", "trigger-message"),
            node("a", "test", message="A"),
            node("b", "test", message="B"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "a"),
            edge("s", "b"),
            edge("a", "e"),  # 两条 trigger 边汇到 e.trigger：允许
            edge("b", "e"),
        ],
    }
    assert validate_graph(diamond).valid

    dup_data = {
        "nodes": [
            node("s", "trigger-message"),
            node("t", "test", message="x"),
            node("l", "log"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "t"),
            edge("t", "l", "message", "message"),
            edge("s", "l", "message", "message"),  # 第二条数据线接到同一个入口
            edge("l", "e"),
        ],
    }
    codes = {issue.code for issue in validate_graph(dup_data).errors}
    assert "DUPLICATE_INPUT_EDGE" in codes


def test_semantic_wired_data_input_passes() -> None:
    """数据入口接上上游的输出端口（类型也对得上）就通过 —— 不需要在 config 里填值。"""
    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("c", "constant", value="https://api.example.com"),
            node("call", "http", method="POST"),
            node("note", "log"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "c"),
            edge("c", "call", "value", "url"),  # 常量 -> http 的 url 入口
            edge("call", "note", "http_status", "message"),  # 状态码 -> log 的 message 入口
            edge("call", "e"),
        ],
    }
    assert validate_graph(graph).valid


# --------------------------------------------------------------------------- ③-D 类型专属语义校验
def test_semantic_start_time_trigger_requires_cron_and_validates_it() -> None:
    """start 选时间触发：缺 cron 报 MISSING_CONFIG，cron 非法报 INVALID_CRON；消息触发免配置。"""
    g_missing = {
        "nodes": [node("s", "trigger-time"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    report = validate_graph(g_missing)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "MISSING_CONFIG" for e in report.errors)

    g_bad = {
        "nodes": [node("s", "trigger-time", cron="not a cron"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    report = validate_graph(g_bad)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "INVALID_CRON" for e in report.errors)

    g_good = {
        "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(g_good).valid

    # 老的 start 节点（触发器拆分前的写法）：类型未注册要拦下，并提示换成哪个触发器
    g_legacy = {
        "nodes": [node("s", "start", trigger="message"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    report = validate_graph(g_legacy)
    assert not report.valid
    legacy = next(e for e in report.errors if e.code == "UNKNOWN_NODE_TYPE")
    assert "trigger-message" in legacy.suggestion  # 报错里直接指路换哪个

    # 消息触发（含完全不配 trigger 的旧 start）无需任何配置
    g_message = {
        "nodes": [node("s", "trigger-message"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(g_message).valid
    assert validate_graph(linear_graph()).valid


def test_semantic_log_requires_message_and_validates_level() -> None:
    """log 的 message 入口没接线也没手填 → INPUT_NOT_CONNECTED；level 非法 → INVALID_LOG_LEVEL。"""
    g_missing = {
        "nodes": [node("s", "trigger-message"), node("l", "log"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e")],
    }
    report = validate_graph(g_missing)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "INPUT_NOT_CONNECTED" for e in report.errors)

    g_bad_level = {
        "nodes": [node("s", "trigger-message"), node("l", "log", message="hi", level="TRACE"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e")],
    }
    report = validate_graph(g_bad_level)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "INVALID_LOG_LEVEL" for e in report.errors)

    g_good = {
        "nodes": [node("s", "trigger-message"), node("l", "log", message="hi", level="WARNING"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e")],
    }
    assert validate_graph(g_good).valid


def test_semantic_test_node_passes_without_config() -> None:
    """test 节点没有必填项：message 入口可选（没接线时用手填值，连键都没有才用节点 id）。"""
    g = {
        "nodes": [node("s", "trigger-message"), node("t", "test"), node("e", "end")],
        "edges": [edge("s", "t"), edge("t", "e")],
    }
    assert validate_graph(g).valid


# ------------------------------------------------------------------- ③-E 注册驱动的校验
def test_validation_rules_are_driven_by_registration_not_validator_code() -> None:
    """新增类型只在注册处声明规则：必填 / 默认值 / 自定义校验器全部生效，不碰 validator。"""

    def validate_ping(n: WorkflowNode) -> list[ValidationIssue]:
        if str(n.config.get("mode", "")) not in {"sync", "async"}:
            return [
                ValidationIssue(
                    node_id=n.id, code="BAD_PING_MODE", message="mode 只能是 sync/async"
                )
            ]
        return []

    @register_node(
        "reg-ping",
        fields=[
            ConfigField("url", "地址", required=True),
            ConfigField("mode", "模式", default="sync"),
        ],
        validator=validate_ping,
    )
    async def exec_ping(n: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        return {}

    # 未接进主流程时什么都不查；接进主流程后规则全生效
    base_nodes = [node("s", "trigger-message"), node("p", "reg-ping"), node("e", "end")]

    missing = {"nodes": base_nodes, "edges": [edge("s", "p"), edge("p", "e")]}
    report = validate_graph(missing)
    assert report.stage == STAGE_SEMANTIC
    assert any(
        i.node_id == "p" and i.code == "MISSING_CONFIG" for i in report.errors
    )

    bad_mode = {
        "nodes": [
            node("s", "trigger-message"),
            node("p", "reg-ping", url="https://x", mode="weird"),
            node("e", "end"),
        ],
        "edges": [edge("s", "p"), edge("p", "e")],
    }
    codes = {i.code for i in validate_graph(bad_mode).errors}
    assert "BAD_PING_MODE" in codes

    good = {
        "nodes": [
            node("s", "trigger-message"),
            node("p", "reg-ping", url="https://x"),  # mode 缺，默认 sync，校验器放行
            node("e", "end"),
        ],
        "edges": [edge("s", "p"), edge("p", "e")],
    }
    assert validate_graph(good).valid

    # 同类型作为孤儿：缺 url + mode 非法，照样通过
    orphan = {
        "nodes": [node("s", "trigger-message"), node("e", "end"), node("p2", "reg-ping")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(orphan).valid
    _ = exec_ping  # 装饰器返回原函数，保留引用仅为表明这一点


def test_apply_config_defaults_fills_registered_defaults() -> None:
    """保存版本前的默认值补全：trigger / level / method / message 缺失就填，给了值不覆盖。"""
    raw = {
        "nodes": [
            node("s", "trigger-message"),  # trigger 缺
            node("l", "log", message="hi"),  # level 缺
            node("h", "http", url="https://x", timeout=3),  # method 缺、timeout 给了
            node("t", "test"),  # message 缺
            node("e", "end"),
        ],
        "edges": [edge("s", "l"), edge("l", "h"), edge("h", "t"), edge("t", "e")],
    }
    graph = apply_config_defaults(raw)
    configs = {n.id: n.config for n in graph.nodes}
    # 消息触发器没有配置字段（默认值也就无从补起）
    assert configs["s"] == {}
    assert configs["l"]["level"] == "INFO"
    assert configs["h"]["method"] == "GET"  # 画布一直替它填 GET，现在后端也这么声明
    assert configs["h"]["timeout"] == 3  # 显式值不被覆盖
    assert configs["t"]["message"] == "hello"  # test 节点的回显内容（没接线时用它）
    # 原 dict 不被修改
    assert "trigger" not in raw["nodes"][0]["config"]


# --------------------------------------------------------------------------- ④ 节点执行器
@pytest.mark.asyncio
async def test_executor_start_end_log_test_run() -> None:
    """start -> test -> log -> end 全链路：test 把入口值回显出来，log 收到的是**线上来的**值。

    这里没有全局变量：test 的 ``message`` 是它自己手填的（没接线），它的 ``message`` 出口又
    接到了 log 的 ``message`` 入口 —— 值真的沿边走了一遍。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("t", "test", message="hello tickneko"),
                node("l", "log", level="INFO"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "t"),
                edge("t", "l", "message", "message"),  # test 的回显 -> log 的日志内容
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    # 日志收集器按顺序记了节点，log 那行是 test 送过来的值
    assert any("[test] t: hello tickneko" in line for line in ctx.log)
    assert any("[INFO] l: hello tickneko" in line for line in ctx.log)
    # 最后一个节点（end）没接线的入口：触发边不送值
    assert ctx.inputs == {}


@pytest.mark.asyncio
async def test_executor_placeholder_passes_through_without_side_effects() -> None:
    """占位节点：入口的值**原样透传**到出口，不写日志、不留运行痕迹（仿佛不存在）。"""
    node = WorkflowNode(id="p1", type="placeholder", config={})
    ctx = NodeExecutionContext()
    ctx.inputs = {"value": "来自上游"}
    assert await exec_placeholder(node, ctx) == {"value": "来自上游"}
    assert ctx.log == []  # 不留痕迹


@pytest.mark.asyncio
async def test_executor_placeholder_relays_value_along_the_edge() -> None:
    """start -> 占位 -> log：占位把值沿边原样透传，下游 log 收到的是线上来的值。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("p", "placeholder", value="透传我"),
                node("l", "log", level="INFO"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "p"),
                edge("p", "l", "value", "message"),  # 占位的透传 -> log 的日志内容
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[INFO] l: 透传我" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_skips_orphan_nodes_entirely() -> None:
    """孤儿节点不执行：没执行器的孤儿不拖垮主流程，有执行器的孤儿副作用也不发生。"""

    ran: list[str] = []

    @register_node("orphan-marker")
    async def exec_marker(n: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(n.id)
        return {"orphan_var": True}

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("e", "end"),
                node("m", "orphan-marker"),  # 可达性外的有执行器孤儿：不该跑
                node("ghost", "not-a-registered-type"),  # 没登记过（连规格都没）的孤儿
            ],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)  # 不因孤儿缺执行器而抛错
    assert ran == []  # 孤儿副作用没发生
    # 主流程照常跑完（start -> end），孤儿一点痕迹都没留下
    assert any("[trigger-message]" in line for line in ctx.log)
    assert any("[end]" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_orphan_edge_into_main_path_does_not_block() -> None:
    """孤儿有条边指向主流程节点时，入度只算主流程内部，主节点不会被永不执行的孤儿卡死。"""

    ran: list[str] = []

    @register_node("orphan-feeder")
    async def exec_feeder(n: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(n.id)
        return {}

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("e", "end"),
                node("o", "orphan-feeder"),  # 不可达，但有一条 o -> e 的入边
            ],
            # 主流程 s -> e；孤儿 -> e 是从可达域外伸进来的边
            "edges": [edge("s", "e"), edge("o", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)  # 旧实现这里会卡在 e 的入度上
    assert ran == []  # 孤儿依旧不执行


@pytest.mark.asyncio
async def test_executor_ignores_data_edges_from_nodes_that_never_ran() -> None:
    """孤儿连出来的线**不算数**：别拿空串把手填的兜底值顶掉（与校验器同一口径）。

    constant 没接触发线（孤儿）→ 永不执行，却挂着一根 ``value -> log.message``。那根线不该
    被当成「上游送来了空串」：校验器认为它不算数（所以手填值满足必填入口），运行器也得这么算，
    log 才会用手填的 ``config.message``。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("l", "log", message="手填的内容"),
                node("c", "constant", value="孤儿常量"),
                node("e", "end"),
            ],
            "edges": [edge("s", "l"), edge("l", "e"), edge("c", "l", "value", "message")],
        }
    )
    assert validate_graph(graph).valid  # 校验放行：孤儿那根线不算数，手填值就够了

    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[INFO] l: 手填的内容" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_still_sends_empty_for_wired_port_without_value() -> None:
    """上游**跑过了**、只是那个出口没产出（时间触发的 start 没有 message）→ 照旧送空串。

    这跟「上游根本没跑」是两回事：接的线算数、线上确实没值，空串会盖掉手填值（有意为之，
    见 ``_inputs_of`` 的文档）。别把这条语义一起改掉了。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-time", cron="*/5 * * * *"),
                node("l", "log", message="手填的内容"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "l"),
                edge("s", "l", "message", "message"),  # 时间触发没有 message 产出
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any(line == "[INFO] l: " for line in ctx.log)  # 线上来了个空串
    assert not any("手填的内容" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_start_time_trigger_registers_only_when_priming() -> None:
    """只有「登记那一趟」才动调度器（拨运行开关 / 启动载入 / 发布新版走的都是这一趟）。

    task_id = ``wf-<工作流 id>-<节点 id>``（工作流 + 节点两级，避免不同图的同名节点撞车）；
    重复登记是幂等的：同名旧任务先摘掉再加。
    """
    from tickneko.core.scheduler import TaskManager

    scheduler = TaskManager()

    async def run_workflow() -> None: ...

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-time", cron="*/5 * * * *", name="每5分钟"),
                node("e", "end"),
            ],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext(
        scheduler=scheduler, run=run_workflow, workflow_id="demo", register_triggers=True
    )
    await SimpleWorkflowRunner().run(graph, ctx)
    task = scheduler.get("wf-demo-s")
    assert task.name == "每5分钟"

    # 再登记一遍：不报错，仍是同一个 task_id
    await SimpleWorkflowRunner().run(graph, ctx)
    assert scheduler.get("wf-demo-s") is not None


@pytest.mark.asyncio
async def test_executor_start_time_trigger_leaves_scheduler_alone_while_running() -> None:
    """整图执行（cron 到点那一趟）**不碰调度器**：它自己会排下一次。

    回归：以前每次执行都先「摘掉再登记」，等于每跑一次就换一个新的任务对象 ——
    ``run_count`` / ``last_run`` 这些运行统计被清零，连「上一次还没跑完就跳过本次」的
    单实例保护（看 ``task.active``）也一并失效了。
    """
    from tickneko.core.scheduler import TaskManager

    scheduler = TaskManager()
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    # 先把任务登记上（这一趟才是登记）
    priming_ctx = NodeExecutionContext(
        scheduler=scheduler, workflow_id="demo", register_triggers=True
    )
    await SimpleWorkflowRunner().run(graph, priming_ctx)
    task = scheduler.get("wf-demo-s")
    task.run_count = 7  # 假装已经跑过好几轮

    # 再跑一遍 = 到点执行那一趟：同一个任务对象，统计原样
    ctx = NodeExecutionContext(scheduler=scheduler, workflow_id="demo")
    await SimpleWorkflowRunner().run(graph, ctx)
    assert scheduler.get("wf-demo-s") is task
    assert task.run_count == 7
    assert any("执行中" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_start_message_trigger_does_not_register() -> None:
    """消息触发的 start 不登记调度器，只写一条开始日志。"""
    from tickneko.core.scheduler import TaskManager

    scheduler = TaskManager()
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-message"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext(scheduler=scheduler)
    await SimpleWorkflowRunner().run(graph, ctx)
    assert scheduler.list() == []
    assert ctx.inputs == {}  # 触发边不送值，end 什么都没收到
    assert any("消息触发" in line for line in ctx.log)


async def test_context_user_id_identifies_who_the_run_is_for() -> None:
    """``ctx.user_id`` 标识这一趟**面向哪个用户**：没给是空串（``NO_USER_ID``）。

    它与 ``owner_id`` 是两回事：``owner_id`` 是工作流的主人（账号），``user_id`` 是被服务
    的那个人 —— 定时触发 / 离线跑没有「这个人」，所以缺省空串而不是某个占位 id。
    """
    assert NodeExecutionContext().user_id == ""
    assert NodeExecutionContext(user_id="10001").user_id == "10001"

    ctx = NodeExecutionContext(owner_id="u-admin", user_id="10001")
    assert (ctx.owner_id, ctx.user_id) == ("u-admin", "10001")


@pytest.mark.asyncio
async def test_context_logger_binds_who_the_run_is_for() -> None:
    """``ctx.logger`` **提前绑好了默认字段**：节点只写自己那句话，日志自己认得出是谁的。

    身份（哪条工作流 / 谁的 / 给谁跑的）在构造上下文时就定下来，不用每个节点在调用点
    手抄一遍 —— ``start`` 之类的节点因此不必回写上下文。
    """
    collected: list[LogRecord] = []
    core = LogCore(console=False, dispatch_timeout=0.01)
    await core.start()
    core.mount(LogCollector(collected))
    try:
        ctx = NodeExecutionContext(
            logger=core, workflow_id="w1", owner_id="u-admin", user_id="10001"
        )
        ctx.logger.info("节点只写自己这句")
        await wait_for_records(collected)
        # owner_id 是日志的一等字段（不塞 extra），其余两个进 extra
        assert [(r.extra, r.owner_id) for r in collected] == [
            ({"workflow_id": "w1", "user_id": "10001"}, "u-admin")
        ]
    finally:
        await core.stop()


@pytest.mark.asyncio
async def test_executor_start_time_trigger_without_scheduler_skips_gracefully() -> None:
    """登记那一趟没注入调度器时（离线 / 测试）：只记一条 warning，不抛异常。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext(register_triggers=True)
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("未注入调度器" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_start_time_trigger_running_pass_needs_no_scheduler() -> None:
    """执行那一趟本来就不碰调度器：没注入也照跑，不该报「未注入调度器」。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("执行中" in line for line in ctx.log)
    assert not any("未注入调度器" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_unsupported_node_type_raises() -> None:
    """只声明了规格、没实现执行器的类型跑图时抛 NotImplementedError。"""
    declare_node_type("test-noexec")
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-message"), node("c", "test-noexec"), node("e", "end")],
            "edges": [edge("s", "c"), edge("c", "e")],
        }
    )
    with pytest.raises(NotImplementedError):
        await SimpleWorkflowRunner().run(graph, NodeExecutionContext())


@pytest.mark.asyncio
async def test_executor_stops_downstream_on_node_failure() -> None:
    """业务失败（NodeFailure）**停止向下传播**：下游整段跳过，别的分支照跑，流程不中断。"""
    from tickneko.workflow.nodes import NodeFailure

    ran: list[str] = []

    @register_node("boom")
    async def exec_boom(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(node.id)
        raise NodeFailure("算不出来：左值不是数字")

    @register_node("failure-tail")
    async def exec_tail(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(node.id)
        return {}

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("b", "boom"),
                node("t", "failure-tail"),  # 失败节点的下游：不该跑
                node("side", "log", message="别的分支"),  # 平行分支：照跑
                node("e", "end"),
            ],
            "edges": [
                edge("s", "b"),
                edge("b", "t"),
                edge("t", "e"),
                edge("s", "side"),
                edge("side", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)  # 不抛：业务失败不是事故

    assert ran == ["b"]  # 失败节点跑了，它的下游一个都没跑
    assert any("[failed] b:" in line for line in ctx.log)
    assert any("[skip] t:" in line and "失败" in line for line in ctx.log)  # 写明是被谁带停的
    assert any("[INFO] side: 别的分支" in line for line in ctx.log)  # 别的分支照常


@pytest.mark.asyncio
async def test_branch_pruning_ignores_cross_branch_data_edges() -> None:
    """分流未选中的分支**不该因为一条跨分支的数据边**而执行。

    真实场景：``start.target -> send.target`` 是数据边（跨在条件之前），而 send 的触发走
    ``trigger`` 边。以前「只要还有一条活入边就执行」，于是没走中的那条分支上的 send 也跑了
    （message 拿不到，报「内容为空」）。现在执行与否只看**控制流**入边。
    """
    ran: list[str] = []

    @register_node("prune-mark-a")
    async def exec_mark_a(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(node.id)
        return {}

    @register_node("prune-mark-b")
    async def exec_mark_b(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(node.id)
        return {}

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("c", "condition", left="1", operator="==", right="2"),  # 1 == 2 -> false
                node("on_true", "prune-mark-a"),
                node("on_false", "prune-mark-b"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "c"),
                edge("c", "on_true"),  # true 出口：没走中
                edge("c", "on_false", "false", "trigger"),  # false 出口：走中
                # 跨分支的数据边（模仿 start.target -> send.target）：以前它会把 on_true 撑活
                edge("s", "on_true", "message", "value"),
                edge("s", "on_false", "message", "value"),
                edge("on_true", "e"),
                edge("on_false", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)

    assert ran == ["on_false"]  # 只有走中的那条分支执行
    assert any("[skip] on_true" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_node_with_only_data_edges_still_runs() -> None:
    """只有数据入边（没有控制流入边）的节点照常执行：数据边不驱动、也不阻止执行。"""
    ran: list[str] = []

    @register_node("data-only")
    async def exec_data_only(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(node.id)
        return {}

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("d", "data-only"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "d", "message", "value"),  # 纯数据边：不是控制流
                edge("d", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)

    assert ran == ["d"]


@pytest.mark.asyncio
async def test_executor_wraps_environment_error_with_node_context() -> None:
    """节点抛环境异常（不是 NodeFailure）：包上节点信息再抛，原异常链保留（堆栈里看得到）。"""
    from tickneko.workflow.executor import NodeExecutionError

    @register_node("boom-env")
    async def exec_boom(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise ValueError("配置写错了")

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-message"), node("b", "boom-env"), node("e", "end")],
            "edges": [edge("s", "b"), edge("b", "e")],
        }
    )
    with pytest.raises(NodeExecutionError) as caught:
        await SimpleWorkflowRunner().run(graph, NodeExecutionContext())

    assert caught.value.node_id == "b"
    assert caught.value.node_type == "boom-env"
    assert "ValueError" in str(caught.value) and "配置写错了" in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)  # 堆栈里连着原始异常


@pytest.mark.asyncio
async def test_executor_wrapped_error_survives_empty_message() -> None:
    """原异常消息为空（httpx 那种）：包装后的消息仍然非空（用 repr 兜底）。"""

    @register_node("boom-empty-msg")
    async def exec_boom(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise ValueError("")

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-message"), node("b", "boom-empty-msg"), node("e", "end")],
            "edges": [edge("s", "b"), edge("b", "e")],
        }
    )
    from tickneko.workflow.executor import NodeExecutionError

    with pytest.raises(NodeExecutionError) as caught:
        await SimpleWorkflowRunner().run(graph, NodeExecutionContext())

    assert str(caught.value)  # 非空
    assert "ValueError" in str(caught.value)


@pytest.mark.asyncio
async def test_executor_failed_node_produces_nothing_downstream() -> None:
    """失败节点**不产出**：下游如有其它活入边照常执行，但从失败那条线拿不到值（回落手填）。"""
    from tickneko.workflow.nodes import NodeFailure

    @register_node("boom2")
    async def exec_boom(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise NodeFailure("这一步没做成")

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("b", "boom2"),
                node("l", "log", message="手填兜底"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "b"),
                edge("s", "l"),
                edge("b", "l", "value", "message"),  # 失败节点 -> log 的内容入口
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)

    # log 有另一条活入边（start 的触发边）所以照常执行，但内容入口没被失败节点顶掉
    assert any("[INFO] l: 手填兜底" in line for line in ctx.log)
    assert any("[failed] b:" in line for line in ctx.log)


# ------------------------------------------------------------- ④-B http 节点（打桩，不走网络）
class FakeResponse:
    """假的 httpx 响应：http 节点只用到 ``status_code`` / ``text`` 两样。"""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeAsyncClient:
    """替身 ``httpx.AsyncClient``：记下收到的请求，回一个编好的响应（或抛编好的异常）。

    ``httpx`` 是可选依赖、真发请求又要走网络，所以按本项目一贯的做法**打桩**：节点内部
    取的就是 ``httpx.AsyncClient`` 这个名字，替换掉它即可（见 :func:`fake_http`）。
    """

    #: 编好的行为（fixture 每次重置）
    status: int = 200
    text: str = ""
    error: Exception | None = None
    #: 收到的请求 / 建客户端时的参数
    calls: ClassVar[list[dict[str, object]]] = []
    client_kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, **kwargs: object) -> None:
        FakeAsyncClient.client_kwargs = dict(kwargs)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: str | None = None,
    ) -> FakeResponse:
        FakeAsyncClient.calls.append(
            {"method": method, "url": url, "headers": headers, "content": content}
        )
        if FakeAsyncClient.error is not None:
            raise FakeAsyncClient.error
        return FakeResponse(FakeAsyncClient.status, FakeAsyncClient.text)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[FakeAsyncClient]:
    """把 ``httpx.AsyncClient`` 换成替身（节点内部就取它这个名字，打这里够用）。"""
    import httpx

    FakeAsyncClient.calls = []
    FakeAsyncClient.client_kwargs = {}
    FakeAsyncClient.status = 200
    FakeAsyncClient.text = '{"ok": true}'
    FakeAsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


def http_node(**config: object) -> WorkflowNode:
    """造一个 http 节点；config 缺省补一份能跑通的（url / method 是必填项）。"""
    merged: dict[str, object] = {"url": "https://api.example.com/items", "method": "GET"}
    merged.update(config)
    return WorkflowNode.model_construct(id="h1", type="http", config=merged)


@pytest.mark.asyncio
async def test_http_node_takes_url_and_body_from_wires(
    fake_http: type[FakeAsyncClient],
) -> None:
    """url / body 可以走连线（线上的值覆盖手填值）；状态码与正文从两个出口产出。"""
    fake_http.status = 201
    fake_http.text = '{"id": 7}'
    node_obj = http_node(
        url="http://ignored.example.com",  # 被线上来的值覆盖
        method="post",
        headers={"X-Robot": "r-001"},  # headers 只能手写（没有对应端口）
        timeout=3,
    )
    ctx = NodeExecutionContext()
    ctx.inputs = {"url": "https://api.example.com/items/7", "body": '{"id": 7}'}

    outputs = await exec_http(node_obj, ctx)

    assert outputs == {"http_status": 201, "http_body": '{"id": 7}'}
    assert fake_http.calls == [
        {
            "method": "POST",  # 方法大小写不敏感
            "url": "https://api.example.com/items/7",
            "headers": {"X-Robot": "r-001"},
            "content": '{"id": 7}',
        }
    ]
    assert fake_http.client_kwargs["timeout"] == 3.0
    assert any("POST" in line and "201" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_http_node_uses_config_when_not_wired(fake_http: type[FakeAsyncClient]) -> None:
    """没接线就用 config 里手填的 url / body：入口值「线上优先，没有才用手填」。"""
    await exec_http(http_node(url="https://x/y", body="raw"), NodeExecutionContext())

    assert fake_http.calls[0]["url"] == "https://x/y"
    assert fake_http.calls[0]["content"] == "raw"


@pytest.mark.asyncio
async def test_http_status_reaches_downstream_message_input(
    fake_http: type[FakeAsyncClient],
) -> None:
    """http 的 ``http_status`` 出口接到 log 的 ``message`` 入口：值真的沿边走完整条链路。"""
    fake_http.status = 201  # 成功响应才往下传（4xx / 5xx 现在算业务失败，下游会被带停）
    fake_http.text = "201"
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("h", "http", url="https://api.example.com", method="GET"),
                node("l", "log", level="WARNING"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "h"),
                edge("h", "l", "http_status", "message"),  # 状态码 -> 日志内容
                edge("h", "e"),  # 触发边继续往下走
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[WARNING] l: 201" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_http_error_status_is_a_node_failure(fake_http: type[FakeAsyncClient]) -> None:
    """4xx / 5xx 是「对方的回答」= **业务失败**：抛 NodeFailure（停止向下传播），不再当正常结果。"""
    from tickneko.workflow.nodes import NodeFailure

    fake_http.status = 500
    fake_http.text = "boom"
    ctx_ = NodeExecutionContext()

    with pytest.raises(NodeFailure, match="HTTP 500"):
        await exec_http(http_node(), ctx_)

    assert any("-> 500" in line for line in ctx_.log)  # 状态码与字节数照旧进日志


@pytest.mark.asyncio
async def test_http_error_status_skips_downstream_in_graph(
    fake_http: type[FakeAsyncClient],
) -> None:
    """图里跑：http 回了 5xx 时下游整段跳过（不再把错误状态码送下去）。"""
    fake_http.status = 503
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("h", "http", url="https://api.example.com", method="GET"),
                node("l", "log", message="手填兜底"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "h"),
                edge("s", "l"),
                edge("h", "l", "http_status", "message"),
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)  # 不抛：业务失败只停自己这条线

    assert any("[failed] h:" in line for line in ctx.log)
    assert any("[INFO] l: 手填兜底" in line for line in ctx.log)  # 另一条活入边还在，照常执行


@pytest.mark.asyncio
async def test_http_node_raises_on_connection_failure(fake_http: type[FakeAsyncClient]) -> None:
    """连不上 / 超时是环境问题：直接抛，但消息里要带方法 / 地址 / 超时与异常类型。"""
    import httpx

    from tickneko.workflow.nodes import EnvironmentFailure

    fake_http.error = httpx.ConnectError("连不上")

    with pytest.raises(EnvironmentFailure) as caught:
        await exec_http(http_node(), NodeExecutionContext())

    message = str(caught.value)
    assert "GET" in message and "https://api.example.com/items" in message
    assert "ConnectError" in message and "timeout=" in message
    assert isinstance(caught.value.__cause__, httpx.ConnectError)  # 原异常链保留


async def test_http_node_connection_failure_names_the_exception_when_str_is_empty(
    fake_http: type[FakeAsyncClient],
) -> None:
    """``str(exc)`` 为空（httpx 常见）时消息也不能空着 —— 用类型名兜底。"""
    import httpx

    from tickneko.workflow.nodes import EnvironmentFailure

    fake_http.error = httpx.ReadTimeout("")

    with pytest.raises(EnvironmentFailure, match="ReadTimeout"):
        await exec_http(http_node(), NodeExecutionContext())


@pytest.mark.asyncio
async def test_http_node_rejects_unknown_method_and_empty_url() -> None:
    """配置写错当场抛（校验阶段也会拦，见 INVALID_HTTP_METHOD）。"""
    with pytest.raises(ValueError, match="method"):
        await exec_http(http_node(method="FETCH"), NodeExecutionContext())
    with pytest.raises(ValueError, match="url"):
        await exec_http(http_node(url=""), NodeExecutionContext())


@pytest.mark.asyncio
async def test_http_node_without_httpx_says_how_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没装 httpx（可选依赖）：报错里给安装提示，而不是莫名的 AttributeError。"""
    monkeypatch.setitem(sys.modules, "httpx", None)  # 之后再 import httpx 会抛 ImportError

    with pytest.raises(RuntimeError, match="httpx"):
        await exec_http(http_node(), NodeExecutionContext())


def test_http_method_is_checked_at_validation() -> None:
    """method 拼错在校验阶段就报；合法方法放行。"""

    def graph_with(method: str) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("h", "http", url="https://api.example.com", method=method),
                node("e", "end"),
            ],
            "edges": [edge("s", "h"), edge("h", "e")],
        }

    bad = validate_graph(graph_with("FETCH"))
    assert [issue.code for issue in bad.errors] == ["INVALID_HTTP_METHOD"]

    assert validate_graph(graph_with("GET")).valid


# ------------------------------------------------------------- ④-C 常量
@pytest.mark.asyncio
async def test_constant_node_produces_one_value_on_its_port() -> None:
    """一个常量节点就一个值：从 ``value`` 出口送给下游连上来的入口。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("c", "constant", value="https://api.example.com"),
                node("l", "log"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "c"),
                edge("c", "l", "value", "message"),  # 常量 -> log 的日志内容
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("https://api.example.com" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_multiple_constants_are_multiple_nodes() -> None:
    """要几个常量就摆几个节点：各自的线互不串（以前是一个节点塞一组「名字 -> 值」）。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("c1", "constant", value="第一"),
                node("c2", "constant", value="第二"),
                node("l1", "log", level="INFO"),
                node("l2", "log", level="WARNING"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "c1"),
                edge("c1", "l1", "value", "message"),
                edge("c1", "c2"),  # 触发边：先 c1 再 c2
                edge("c2", "l2", "value", "message"),
                edge("l1", "e"),
                edge("l2", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[INFO] l1: 第一" in line for line in ctx.log)
    assert any("[WARNING] l2: 第二" in line for line in ctx.log)


def test_constant_value_is_required() -> None:
    """常量节点的 ``value`` 是必填字段：没写报 MISSING_CONFIG。"""
    graph = {
        "nodes": [node("s", "trigger-message"), node("c", "constant"), node("e", "end")],
        "edges": [edge("s", "c"), edge("c", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(i.node_id == "c" and i.code == "MISSING_CONFIG" for i in report.errors)


def test_constant_must_be_wired_to_be_read() -> None:
    """值只能沿边走：常量接到下游才读得到；常量成了孤儿，下游那个入口就是空的。"""
    nodes = [
        node("s", "trigger-message"),
        node("c", "constant", value="https://api.example.com"),
        node("l", "log"),
        node("e", "end"),
    ]
    wired = {
        "nodes": nodes,
        "edges": [edge("s", "c"), edge("c", "l", "value", "message"), edge("l", "e")],
    }
    assert validate_graph(wired).valid

    # 常量没接进主流程（孤儿）：它不执行，log 的 message 入口也就没有线 —— 报没接上
    orphan = {"nodes": nodes, "edges": [edge("s", "l"), edge("l", "e")]}
    report = validate_graph(orphan)
    assert not report.valid
    assert any(
        issue.node_id == "l" and issue.code == "INPUT_NOT_CONNECTED" for issue in report.errors
    )


# ------------------------------------------------------------- ④-D 等待节点
@pytest.mark.asyncio
async def test_executor_delay_waits_then_passes_control() -> None:
    """等待节点：等够秒数再往下走（触发进 / 触发出），自己不产出值。

    真等 0.05 秒量一次耗时：既验它确实等了，也验下游是在它之后才跑的。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("d", "delay", seconds=0.05),
                node("l", "log", message="等到了"),
                node("e", "end"),
            ],
            "edges": [edge("s", "d"), edge("d", "l"), edge("l", "e")],
        }
    )
    ctx = NodeExecutionContext()
    started = time.monotonic()
    await SimpleWorkflowRunner().run(graph, ctx)
    elapsed = time.monotonic() - started

    waited = "[delay] d: 等待 0.05 秒"
    # 真的等了，不是立即返回。阈值不用 0.05：Windows 定时器粒度 15.625ms，
    # asyncio.sleep(0.05) 实测会在 ~47ms（3 tick）与 ~63ms（4 tick）之间摇摆，
    # 按请求值卡线会偶发失败；立即返回的话 elapsed 在毫秒级，跟 0.04 差一个量级。
    assert elapsed >= 0.04
    assert any(waited in line for line in ctx.log)
    assert ctx.log.index(waited) < ctx.log.index("[INFO] l: 等到了")  # 先等完再走下游


@pytest.mark.asyncio
async def test_executor_delay_zero_passes_through_without_waiting() -> None:
    """``seconds=0`` = 不等（临时把等待关掉）：照常往下走，只是不写「等待 N 秒」那行。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "trigger-message"), node("d", "delay", seconds=0), node("e", "end")],
            "edges": [edge("s", "d"), edge("d", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[delay] d: 不等待" in line for line in ctx.log)
    assert not any(line.startswith("[delay] d: 等待") for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_delay_takes_seconds_from_the_wire() -> None:
    """等待秒数可以**从连线来**：线上优先，没接线才用手填的 ``config.seconds``。

    这条路是给「等多久由上游算」用的（常量 / HTTP 结果 / 别的节点算出来的值都行）。
    这里手填 2 秒、线上送 0.05 秒：跑完必须远小于 2 秒，证明用的是线上的值。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("c", "constant", value="0.05"),
                node("d", "delay", seconds=2),  # 手填的会被线上值覆盖
                node("e", "end"),
            ],
            # c.value -> d.seconds 是数据线；d -> e 只表达先后
            "edges": [edge("s", "c"), edge("c", "d", "value", "seconds"), edge("d", "e")],
        }
    )
    assert validate_graph(graph).valid  # 入口不标必填：接线或手填都行，校验放行

    ctx = NodeExecutionContext()
    started = time.monotonic()
    await SimpleWorkflowRunner().run(graph, ctx)
    elapsed = time.monotonic() - started

    assert any("[delay] d: 等待 0.05 秒" in line for line in ctx.log)
    assert elapsed < 1.0  # 没被手填的 2 秒拖住


@pytest.mark.asyncio
async def test_executor_delay_rejects_bad_wired_seconds() -> None:
    """线上送来的秒数不合法（不是数字 / 超上限）运行期当场抛 —— 不静默截断成别的值。"""

    def graph_with(value: str) -> WorkflowGraph:
        return WorkflowGraph.model_validate(
            {
                "nodes": [
                    node("s", "trigger-message"),
                    node("c", "constant", value=value),
                    node("d", "delay", seconds=1),
                    node("e", "end"),
                ],
                "edges": [edge("s", "c"), edge("c", "d", "value", "seconds"), edge("d", "e")],
            }
        )

    from tickneko.workflow.executor import NodeExecutionError

    with pytest.raises(NodeExecutionError, match="不是数字") as caught:
        await SimpleWorkflowRunner().run(graph_with("一会儿"), NodeExecutionContext())
    assert caught.value.node_id == "d"  # 包装后能一眼看出是哪个节点炸的
    assert caught.value.node_type == "delay"
    assert isinstance(caught.value.__cause__, ValueError)

    with pytest.raises(NodeExecutionError, match="超过上限"):
        await SimpleWorkflowRunner().run(graph_with("99999"), NodeExecutionContext())


def test_delay_seconds_is_validated() -> None:
    """等待时长：非数字 / 负数 / 超过上限都在语义阶段拦住；``0`` 与正数是合法值。"""
    def graph_with(seconds: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("d", "delay", seconds=seconds),
                node("e", "end"),
            ],
            "edges": [edge("s", "d"), edge("d", "e")],
        }

    for bad in ("一会儿", -1, 3600.5, 999999):
        report = validate_graph(graph_with(bad))
        assert not report.valid and report.stage == STAGE_SEMANTIC, bad
        assert [issue.code for issue in report.errors] == ["INVALID_DELAY_SECONDS"], bad

    for good in (0, 0.5, 30, "15"):
        assert validate_graph(graph_with(good)).valid, good


@pytest.mark.asyncio
async def test_json_extracts_nested_scalar_and_whole_document() -> None:
    """点路径提取：嵌套 / 数组下标（负数从后往前）/ 留空取整个文档；值统一字符串化。"""
    doc = (
        '{"data": {"user": {"name": "小明"}, "items": [{"t": "a"}, {"t": "b"}],'
        ' "n": 3, "ok": true, "none": null, "s": "文字"}}'
    )

    async def extract(path: str) -> str:
        node_ = WorkflowNode(id="j1", type="json", config={"path": path})
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"json": doc}  # 线上来的文本（接线场景）
        result = await exec_json(node_, ctx_)
        return result["json_value"]

    assert await extract("data.user.name") == "小明"
    assert await extract("data.s") == "文字"
    assert await extract("data.items.1.t") == "b"
    assert await extract("data.items.-1.t") == "b"  # 负索引：从后往前
    assert await extract("data.n") == "3"  # 数字 -> JSON 字面量
    assert await extract("data.ok") == "true"
    assert await extract("data.none") == "null"  # 值真的是 null：算「取到了」
    # 留空 = 整个文档：紧凑序列化，与输入里的空白无关
    assert await extract("") == (
        '{"data":{"user":{"name":"小明"},"items":[{"t":"a"},{"t":"b"}],'
        '"n":3,"ok":true,"none":null,"s":"文字"}}'
    )


@pytest.mark.asyncio
async def test_json_failure_stops_propagation() -> None:
    """三块「数据不合预期」都是**业务失败**（抛 NodeFailure，停止向下传播）：空文本 /
    非法 JSON / 路径不存在 —— 不再送空串。"""
    from tickneko.workflow.nodes import NodeFailure

    async def failing(text: str, path: str = "") -> str:
        node_ = WorkflowNode(id="j1", type="json", config={"path": path})
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"json": text}
        with pytest.raises(NodeFailure) as caught:
            await exec_json(node_, ctx_)
        assert any("[json]" in line for line in ctx_.log)  # 痕迹照留
        return str(caught.value)

    assert "没拿到" in await failing("")  # 上游送了空串
    assert "解析失败" in await failing("<html>502 Bad Gateway</html>")  # 对方回了个错误页
    assert "在文档里不存在" in await failing('{"a": {"b": 1}}', "a.c")  # 字段名拼错 / 对方改了结构


def test_json_fields_are_validated() -> None:
    """手填值的防呆在语义阶段：路径写法 / JSON 文本语法；必填入口「接线或手填」照常生效。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("j", "json", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "j"), edge("j", "e")],
        }

    assert validate_graph(graph_with(json='{"a": 1}', path="a.b")).valid  # 手填合法文本即可放行

    report = validate_graph(graph_with(json='{"a": 1}', path="a..b"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_JSON_PATH"]

    report = validate_graph(graph_with(json="{oops}", path="a"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_JSON_TEXT"]

    # 必填入口：接线或手填两个都没有 -> INPUT_NOT_CONNECTED
    report = validate_graph(graph_with())
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INPUT_NOT_CONNECTED"]


@pytest.mark.asyncio
async def test_regex_extracts_and_replaces() -> None:
    """提取：无组取整体 / 有组取第 1 组 / 可选组没匹配回落整体 / i 旗标；替换：\1 反向引用。"""

    async def run(
        text: str, pattern: str, action: str = "extract", replace: str = "", flags: str = ""
    ) -> str:
        node_ = WorkflowNode(
            id="r1",
            type="regex",
            config={"action": action, "replace": replace, "flags": flags},
        )
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"text": text, "pattern": pattern}
        result = await exec_regex(node_, ctx_)
        return result["regex_value"]

    assert await run("验证码 123456，5 分钟内有效", r"(\d{4,6})") == "123456"
    assert await run("id=u-42 已注册", r"id=([\w-]+)") == "u-42"  # 连字符要靠 [\w-] 才吃得住
    assert await run("123", r"(x)?(\d+)") == "123"  # 第 1 组没参与匹配 -> 回落整体匹配
    assert await run("XABCx", "abc", flags="i") == "ABC"  # 忽略大小写；没组取整体
    assert await run("13801234", r"(\d{4})\d{4}", action="replace", replace=r"\1****") == "1380****"
    assert await run("a  b\tc", r"\s+", action="replace", replace="") == "abc"  # 空替换 = 删掉


@pytest.mark.asyncio
async def test_regex_failure_stops_propagation() -> None:
    """抽不到 = **业务失败**（抛 NodeFailure，停止向下传播）：没匹配 / 空文本 / 线上来的
    非法正则 —— 不再送空串。"""
    from tickneko.workflow.nodes import NodeFailure

    async def failing(text: str, pattern: str) -> str:
        node_ = WorkflowNode(id="r1", type="regex", config={})
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"text": text, "pattern": pattern}
        with pytest.raises(NodeFailure) as caught:
            await exec_regex(node_, ctx_)
        assert any("[regex]" in line for line in ctx_.log)  # 痕迹照留
        return str(caught.value)

    assert "没有匹配" in await failing("没有数字的句子", r"\d+")
    assert "没拿到文本" in await failing("", r"\d+")
    assert "编译失败" in await failing("abc", "(abc")  # 线上来的正则不合法（手填的会被校验拦住）


def test_regex_fields_are_validated() -> None:
    """手填值的防呆在语义阶段：正则语法 / 动作枚举 / 旗标字母；必填入口「接线或手填」照常生效。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("r", "regex", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "r"), edge("r", "e")],
        }

    assert validate_graph(graph_with(text="hi", pattern="h")).valid

    report = validate_graph(graph_with(text="hi", pattern="(abc"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_REGEX"]

    report = validate_graph(graph_with(text="hi", pattern="h", action="删掉"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_REGEX_ACTION"]

    report = validate_graph(graph_with(text="hi", pattern="h", flags="ix"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_REGEX_FLAGS"]

    # 必填入口：text / pattern 接一个差一个都不行
    report = validate_graph(graph_with(text="hi"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INPUT_NOT_CONNECTED"]


@pytest.mark.asyncio
async def test_now_outputs_formatted_text_and_timestamp() -> None:
    """产出当前时刻：格式按手填 / 连线（线上优先），时间戳是整数秒的「现在」。"""
    node_ = WorkflowNode(id="n1", type="now", config={"format": "%Y%m%d"})
    result = await exec_now(node_, NodeExecutionContext())
    assert result["now_text"] == time.strftime("%Y%m%d")  # 手填格式
    assert isinstance(result["now_ts"], int)
    assert abs(result["now_ts"] - int(time.time())) <= 5  # 就是「现在」

    ctx_ = NodeExecutionContext()
    ctx_.inputs = {"format": "%Y"}  # 线上来的格式覆盖手填
    result = await exec_now(node_, ctx_)
    assert result["now_text"] == time.strftime("%Y")


@pytest.mark.asyncio
async def test_condition_picks_branch_and_engine_prunes_skipped_side() -> None:
    """分流执行：只跑选中分支，没走的整段跳过（[skip] 留痕）；汇合点有活入边照常收尾。"""

    def branch_graph(left: str, operator: str, right: str) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("c", "condition", left=left, operator=operator, right=right),
                node("yes", "log", message="true 分支"),
                node("no", "log", message="false 分支"),
                node("no2", "log", message="false 级联"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "c"),
                edge("c", "yes", source_port="true"),
                edge("c", "no", source_port="false"),
                edge("yes", "e"),
                edge("no", "no2"),
                edge("no2", "e"),
            ],
        }

    ctx_ = NodeExecutionContext()
    await SimpleWorkflowRunner().run(
        WorkflowGraph.model_validate(branch_graph("5", ">", "3")), ctx_
    )
    assert any("[condition] c: 5 > 3 -> true" in line for line in ctx_.log)
    assert any("[INFO] yes: true 分支" in line for line in ctx_.log)
    assert not any("[INFO] no" in line for line in ctx_.log)  # false 侧两个节点都没执行
    assert any("[skip] no: 分支未选中，未执行" in line for line in ctx_.log)
    assert any("[skip] no2: 分支未选中，未执行" in line for line in ctx_.log)  # 级联跳过
    assert any("[end] e 流程结束" in line for line in ctx_.log)  # 汇合点：还有活入边

    ctx_ = NodeExecutionContext()
    await SimpleWorkflowRunner().run(
        WorkflowGraph.model_validate(branch_graph("2", ">", "3")), ctx_
    )
    assert any("[condition] c: 2 > 3 -> false" in line for line in ctx_.log)
    assert any("[INFO] no: false 分支" in line for line in ctx_.log)  # 这次走 false
    assert not any("[INFO] yes" in line for line in ctx_.log)  # true 侧被剪掉
    assert any("[skip] yes: 分支未选中，未执行" in line for line in ctx_.log)
    assert any("[end] e 流程结束" in line for line in ctx_.log)


@pytest.mark.asyncio
async def test_skipping_a_branching_node_cascades_to_its_downstream() -> None:
    """被跳过的**分流节点**同样要级联：它下游的节点也得跟着跳过。

    真实事故：``条件①「不满足」-> 条件② -> HTTP -> 发送``。条件①走 ``true`` 时条件②被跳过，
    但条件②的出边是从 ``true`` 端口出来的 —— 按源端口名筛「控制流出边」的话，条件②的下游
    减不到活入边，HTTP / 发送照样跑（结果两条消息都发出去）。
    """

    def outer_graph(right: str) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("c1", "condition", left="1", operator="==", right=right),
                node("c2", "condition", left="1", operator="==", right="1"),
                node("hit", "log", message="内层下面"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "c1"),
                edge("c1", "e", source_port="true"),  # 外层满足：直接收尾
                edge("c1", "c2", source_port="false"),  # 外层不满足：进内层条件
                edge("c2", "hit", source_port="true"),
                edge("hit", "e"),
            ],
        }

    ctx_ = NodeExecutionContext()
    await SimpleWorkflowRunner().run(WorkflowGraph.model_validate(outer_graph("1")), ctx_)
    assert any("[condition] c1: 1 == 1 -> true" in line for line in ctx_.log)
    assert not any("[condition] c2" in line for line in ctx_.log)  # 内层条件被跳过
    assert not any("[INFO] hit" in line for line in ctx_.log)  # 它的下游一并跳过（级联）
    assert any("[skip] c2" in line for line in ctx_.log)
    assert any("[skip] hit" in line for line in ctx_.log)  # 理由也顺着传下去了
    assert any("[end] e 流程结束" in line for line in ctx_.log)

    ctx_ = NodeExecutionContext()
    await SimpleWorkflowRunner().run(WorkflowGraph.model_validate(outer_graph("2")), ctx_)
    assert any("[condition] c2: 1 == 1 -> true" in line for line in ctx_.log)  # 这次走内层
    assert any("[INFO] hit: 内层下面" in line for line in ctx_.log)


@pytest.mark.asyncio
async def test_condition_compares_by_operator() -> None:
    """比较口径：==/!= 比文本，>/>=/</<= 要比数字，contains/not_contains 比子串。"""

    async def pick(left: str, operator: str, right: str) -> str:
        node_ = WorkflowNode(id="c1", type="condition", config={"operator": operator})
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"left": left, "right": right}
        result = await exec_condition(node_, ctx_)
        return "true" if result.get("true") else "false"

    assert await pick("5", ">", "3") == "true"
    assert await pick("3", ">=", "3") == "true"
    assert await pick("2.5", "<=", "3") == "true"  # 小数也认
    assert await pick("3", "<", "3") == "false"
    assert await pick("abc", "==", "abc") == "true"
    assert await pick("abc", "!=", "abc") == "false"
    assert await pick("开播了，快来看", "contains", "开播") == "true"
    assert await pick("开播了", "not_contains", "下播") == "true"


@pytest.mark.asyncio
async def test_condition_soft_fails_go_false() -> None:
    """拿不到值不算事故（warning + 走 false）：左值空 / 要数字却是文本 / contains 右值空。"""

    async def run(left: str, operator: str, right: str) -> tuple[bool, list[str]]:
        node_ = WorkflowNode(id="c1", type="condition", config={"operator": operator})
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"left": left, "right": right}
        result = await exec_condition(node_, ctx_)
        return bool(result.get("false")), ctx_.log

    gone_false, log = await run("", "==", "x")  # 上游没送值或送了空串
    assert gone_false and any("左值为空" in line for line in log)

    gone_false, log = await run("一会儿", ">", "3")  # 数值比较符遇到了文本
    assert gone_false and any("非数字" in line for line in log)

    gone_false, log = await run("有现货", "contains", "")  # 子串比较没给右值
    assert gone_false and any("右值为空" in line for line in log)

    gone_false, log = await run("x", "≈", "y")  # 没走过校验的图：非法比较符兜底
    assert gone_false and any("比较符" in line for line in log)


def test_condition_fields_are_validated() -> None:
    """手填值防呆：比较符枚举 / 必填入口「接线或手填」；一个出口都不接由拓扑阶段拦住。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("c", "condition", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "c"), edge("c", "e", source_port="true")],
        }

    assert validate_graph(graph_with(left="x", right="y")).valid

    report = validate_graph(graph_with(left="x", operator="≈", right="y"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_CONDITION_OPERATOR"]

    # 必填入口 left：接线或手填两个都没有 -> INPUT_NOT_CONNECTED
    report = validate_graph(graph_with(right="y"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INPUT_NOT_CONNECTED"]

    # 一个出口都不接：条件节点至少接一个出口（分流节点的出边下限，拓扑阶段）
    report = validate_graph(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("c", "condition", left="x"),
                node("e", "end"),
            ],
            "edges": [edge("s", "c"), edge("s", "e")],
        }
    )
    assert not report.valid and report.stage == STAGE_TOPOLOGY
    assert [issue.code for issue in report.errors] == ["GATEWAY_NEEDS_BRANCHES"]


# ------------------------------------------------------------- ④-E 假平台总线（send / target 测试共用）
class _FakeActionResponse:
    """假的动作回应（形状对齐 ``tickneko.platforms.onebot.models.ActionResponse``：``ok`` = status/retcode 都成功）。"""

    def __init__(self, status: str = "ok", retcode: int = 0, data: object = None) -> None:
        self.status = status
        self.retcode = retcode
        self.data = data

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.retcode == 0


class _FakeGateway:
    """假的平台总线：``send(platform, owner_id, action, **params)`` / ``reply(target, content)`` 返回**规范化回执**。

    与生产一致：``send`` / ``onebot`` 节点读 ``ok`` / ``data``，``onebot`` 别名还下探 ``raw``
    取 retcode —— 所以这里把 ``_FakeActionResponse`` 包成 ``ActionResult``（``raw`` = 那条假回执）。
    """

    def __init__(self, response: _FakeActionResponse | None = None) -> None:
        self.calls: list[tuple[str, str, str, dict[str, object]]] = []
        self.reply_calls: list[tuple[object, str]] = []
        self.target_calls: list[tuple[str, dict[str, object]]] = []
        self._response = response if response is not None else _FakeActionResponse()

    async def send(
        self, platform: str, owner_id: str, action: str, /, **params: object
    ) -> Any:
        from tickneko.platforms.bridge.models import ActionResult

        self.calls.append((platform, owner_id, action, dict(params)))
        resp = self._response
        return ActionResult(
            ok=resp.ok,
            message="" if resp.ok else f"retcode={resp.retcode}",
            data=resp.data,
            raw=resp,
        )

    async def reply(self, target: object, content: str) -> Any:
        from tickneko.platforms.bridge.models import ActionResult

        self.reply_calls.append((target, content))
        resp = self._response
        return ActionResult(
            ok=resp.ok,
            message="" if resp.ok else f"retcode={resp.retcode}",
            data=resp.data,
            raw=resp,
        )

    def make_target(self, platform: str, **fields: object) -> Any:
        from types import SimpleNamespace

        self.target_calls.append((platform, dict(fields)))
        # 与真实适配器同一口径：私聊没给「对方号」时用 chat_id 兜底（生产里 pack 只给 chat_id，
        # 两个适配器的 make_target 都是 ``user_id or chat_id``）—— 假网关不兜底的话，
        # 测出来的 target 跟生产不一致（回复私聊会缺对方号）
        resolved = dict(fields)
        if resolved.get("chat") == "private" and not resolved.get("user_id"):
            resolved["user_id"] = resolved.get("chat_id", "")
        return SimpleNamespace(platform=platform, **resolved)


# ------------------------------------------------------------- ④-F send 节点（P3 泛化）
@pytest.mark.asyncio
async def test_send_replies_via_gateway_to_target() -> None:
    """send 节点把 message 发到 target 指向的会话：走 ``ctx.gateway.reply(target, message)``，回执从 send_ok / send_data 送下去。"""
    from tickneko.workflow.nodes import exec_send

    gateway = _FakeGateway(response=_FakeActionResponse(data={"message_id": 7}))
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway)
    node_ = WorkflowNode(id="snd1", type="send")
    target = gateway.make_target("onebot", chat="group", chat_id="123")
    ctx_.inputs = {"target": target, "message": "hi"}
    result = await exec_send(node_, ctx_)

    assert gateway.reply_calls == [(target, "hi")]
    assert result == {"send_ok": True, "send_data": '{"message_id":7}'}


@pytest.mark.asyncio
async def test_send_without_target_skips_and_requires_gateway() -> None:
    """没有会话定位（target 是 None / 空）就不发：send_ok=False 照常送下游，也不碰网关；有 target 但没接总线是环境问题，当场抛。"""
    from tickneko.workflow.nodes import exec_send

    gateway = _FakeGateway()
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway)
    node_ = WorkflowNode(id="snd1", type="send")

    # 没有 target：不发
    ctx_.inputs = {"message": "hi"}
    result = await exec_send(node_, ctx_)
    assert result == {"send_ok": False, "send_data": ""}
    assert gateway.reply_calls == []

    # 有 target 但没接总线：环境问题当场抛
    no_gateway = NodeExecutionContext(owner_id="u-admin")
    no_gateway.inputs = {
        "target": gateway.make_target("onebot", chat="group", chat_id="7"),
        "message": "hi",
    }
    with pytest.raises(ConnectionError, match="需要平台总线"):
        await exec_send(node_, no_gateway)


class _RecordingLogger:
    """假的日志实例：记下消息与结构化字段（``ctx.log`` 只有文字，看不到字段）。"""

    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, object]]] = []
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def info(self, message: str, **fields: object) -> None:
        self.infos.append((message, fields))

    def warning(self, message: str, **fields: object) -> None:
        self.warnings.append((message, fields))


@pytest.mark.asyncio
async def test_send_failure_logs_what_was_sent() -> None:
    """回执失败（业务失败，抛 NodeFailure）时日志带上内容长度与片段：看得出到底发了什么。"""
    from tickneko.workflow.nodes import NodeFailure, exec_send

    content = "当前时间为：2026-10-02 18:57:51"
    gateway = _FakeGateway(response=_FakeActionResponse(status="failed", retcode=1200))
    recorder = _RecordingLogger()
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway, logger=recorder)
    node_ = WorkflowNode(id="snd1", type="send")
    ctx_.inputs = {
        "target": gateway.make_target("onebot", chat="group", chat_id="1"),
        "message": content,
    }

    with pytest.raises(NodeFailure):  # 回执不成功 = 业务失败（停止向下传播）
        await exec_send(node_, ctx_)

    _, fields = recorder.warnings[-1]  # 但日志照旧记了长度与片段
    assert fields["ok"] is False
    assert fields["message_chars"] == len(content)
    assert "当前时间为" in str(fields["message_preview"])


@pytest.mark.asyncio
async def test_send_success_logs_length_without_preview() -> None:
    """发送成功只记长度、不记内容片段（日志不囤正文）。"""
    from tickneko.workflow.nodes import exec_send

    gateway = _FakeGateway()
    recorder = _RecordingLogger()
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway, logger=recorder)
    node_ = WorkflowNode(id="snd1", type="send")
    ctx_.inputs = {
        "target": gateway.make_target("onebot", chat="group", chat_id="1"),
        "message": "开播了",
    }

    await exec_send(node_, ctx_)

    _, fields = recorder.infos[-1]
    assert fields["ok"] is True
    assert fields["message_chars"] == 3
    assert "message_preview" not in fields


@pytest.mark.asyncio
async def test_send_empty_message_skips() -> None:
    """内容为空（上游「算不出来」送的就是空串）：不发，send_ok=False 送下游，也不碰网关。"""
    from tickneko.workflow.nodes import exec_send

    gateway = _FakeGateway()
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway)
    node_ = WorkflowNode(id="snd1", type="send")
    target = gateway.make_target("onebot", chat="group", chat_id="1")

    ctx_.inputs = {"target": target, "message": ""}  # 上游送了空串
    result = await exec_send(node_, ctx_)
    assert result == {"send_ok": False, "send_data": ""}
    assert gateway.reply_calls == []  # 没往平台上发一条空消息
    assert any("内容为空" in line for line in ctx_.log)

    ctx_.inputs = {"target": target, "message": "   "}  # 纯空白同样跳过
    await exec_send(node_, ctx_)
    assert gateway.reply_calls == []


@pytest.mark.asyncio
async def test_send_failed_receipt_is_a_node_failure() -> None:
    """对方收下但回执不成功（ok=False）= **业务失败**：抛 NodeFailure（停止向下传播），
    不再把「没发出去」当结果往下送。"""
    from tickneko.workflow.nodes import NodeFailure, exec_send

    gateway = _FakeGateway(
        response=_FakeActionResponse(status="failed", retcode=1200, data={"msg": "被禁言"})
    )
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway)
    node_ = WorkflowNode(id="snd1", type="send")
    ctx_.inputs = {
        "target": gateway.make_target("onebot", chat="group", chat_id="1"),
        "message": "hi",
    }

    with pytest.raises(NodeFailure, match="发送失败"):
        await exec_send(node_, ctx_)

    assert any("[send] snd1: reply -> failed" in line for line in ctx_.log)  # 痕迹照留


def test_send_requires_a_target_source() -> None:
    """target 是 send 的必填入口：没接线也没手填，语义阶段报 INPUT_NOT_CONNECTED。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("snd", "send", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "snd"), edge("snd", "e")],
        }

    report = validate_graph(graph_with(message="hi"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INPUT_NOT_CONNECTED"]


@pytest.mark.asyncio
async def test_send_passes_target_through_untranslated() -> None:
    """send 不碰 target 内部结构：适配器构造的 target 原样透传给 ``gateway.reply``（id 转不转整数由适配器管，这里验证的是「不拆、不转、不过手」）。"""
    from tickneko.workflow.nodes import exec_send

    gateway = _FakeGateway()
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway)
    node_ = WorkflowNode(id="snd1", type="send")
    # 用 kook 的 target（id 是字符串）验证：send 节点不按平台认字段，原样发
    target = gateway.make_target("kook", chat="group", chat_id="ch-123")
    ctx_.inputs = {"target": target, "message": "hi"}
    await exec_send(node_, ctx_)
    assert gateway.reply_calls == [(target, "hi")]


# ------------------------------------------------------------- ④-F' 会话解包 / 封装（unpack / pack 节点）
def _fake_onebot_target(**overrides: object):
    """OneBot 形状的会话定位（号是整数；形状对齐 ``OneBotTarget``）。"""
    from types import SimpleNamespace

    base: dict[str, object] = {
        "platform": "onebot",
        "owner_id": "u-admin",
        "chat": "group",
        "group_id": 70001,
        "user_id": 123456,
        "message_id": 999,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_unpack_onebot_splits_target_into_strings() -> None:
    """unpack-onebot：把 OneBot 的整数号**字符串化**拆出（群号读 ``group_id``），缺的字段给空串。"""
    from tickneko.workflow.nodes import exec_unpack_onebot

    ctx_ = NodeExecutionContext()
    ctx_.inputs = {"target": _fake_onebot_target()}
    result = await exec_unpack_onebot(WorkflowNode(id="u1", type="unpack-onebot"), ctx_)
    assert result == {
        "platform": "onebot",
        "chat": "group",
        "chat_id": "70001",    # 群号（group_id，整数 -> 字符串）
        "user_id": "123456",   # 发送者
        "message_id": "999",
        "owner_id": "u-admin",
    }

    # 私聊没有群号：chat_id 给空串，对方号在 user_id
    ctx_.inputs = {"target": _fake_onebot_target(chat="private", group_id=None)}
    result = await exec_unpack_onebot(WorkflowNode(id="u1", type="unpack-onebot"), ctx_)
    assert result["chat"] == "private"
    assert result["chat_id"] == ""
    assert result["user_id"] == "123456"


@pytest.mark.asyncio
async def test_unpack_kook_splits_target_into_strings() -> None:
    """unpack-kook：Kook 的字符串 id 原样拆出（频道号读 ``chat_id``）。"""
    from types import SimpleNamespace

    from tickneko.workflow.nodes import exec_unpack_kook

    target = SimpleNamespace(
        platform="kook",
        owner_id="u-admin",
        chat="group",
        chat_id="ch-70001",
        user_id="u-123456",
        message_id="m-999",
    )
    ctx_ = NodeExecutionContext()
    ctx_.inputs = {"target": target}
    result = await exec_unpack_kook(WorkflowNode(id="u2", type="unpack-kook"), ctx_)
    assert result == {
        "platform": "kook",
        "chat": "group",
        "chat_id": "ch-70001",
        "user_id": "u-123456",
        "message_id": "m-999",
        "owner_id": "u-admin",
    }


@pytest.mark.asyncio
async def test_unpack_without_target_yields_empty_strings() -> None:
    """没有会话定位（运行值是 None，定时触发时 ``start.target`` 透下来的就是 None）：
    全空串照常送下游，不打断流程。"""
    from tickneko.workflow.nodes import exec_unpack_onebot

    ctx_ = NodeExecutionContext()
    ctx_.inputs = {"target": None}
    result = await exec_unpack_onebot(WorkflowNode(id="u1", type="unpack-onebot"), ctx_)
    assert result == {
        key: "" for key in ("platform", "chat", "chat_id", "user_id", "message_id", "owner_id")
    }


@pytest.mark.asyncio
async def test_unpack_rejects_other_platform_target() -> None:
    """装配错位看得见：解包某平台的节点收到别平台的 target，当场 ValueError。"""
    from tickneko.workflow.nodes import exec_unpack_onebot

    ctx_ = NodeExecutionContext()
    ctx_.inputs = {"target": _fake_onebot_target(platform="kook")}
    with pytest.raises(ValueError, match="生产与消费必须同平台"):
        await exec_unpack_onebot(WorkflowNode(id="u1", type="unpack-onebot"), ctx_)


def test_unpack_requires_a_target_source() -> None:
    """target 是 unpack 的必填入口：没接线也没手填，语义阶段报 INPUT_NOT_CONNECTED。"""
    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("u", "unpack-onebot"),
            node("e", "end"),
        ],
        "edges": [edge("s", "u"), edge("u", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INPUT_NOT_CONNECTED"]


@pytest.mark.asyncio
async def test_pack_nodes_build_target_via_gateway() -> None:
    """pack 节点把字符串字段交给 ``gateway.make_target`` 拼回会话定位（platform 写死在节点上）。"""
    from tickneko.workflow.nodes import exec_pack_kook, exec_pack_onebot

    gateway = _FakeGateway()
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway)

    node_ = WorkflowNode(id="p1", type="pack-onebot", config={"chat": "group", "chat_id": "70001"})
    result = await exec_pack_onebot(node_, ctx_)
    # 只带「会话类型 + 一个号」：消息号、对方号都不归封装管（私聊的号写在 chat_id 里）
    assert gateway.target_calls == [
        ("onebot", {"owner_id": "u-admin", "chat": "group", "chat_id": "70001"})
    ]
    assert result["target"].platform == "onebot"

    # kook 同款：platform 写死 kook；私聊的对方号填在 chat_id
    gateway.target_calls.clear()
    node_k = WorkflowNode(id="p2", type="pack-kook", config={"chat": "private", "chat_id": "u-9"})
    result = await exec_pack_kook(node_k, ctx_)
    assert gateway.target_calls == [
        ("kook", {"owner_id": "u-admin", "chat": "private", "chat_id": "u-9"})
    ]
    assert result["target"].platform == "kook"


@pytest.mark.asyncio
async def test_pack_wire_values_override_hand_fill() -> None:
    """封装节点的字段可接线：线上的值优先，覆盖手填（与其它数据入口同一口径）。"""
    from tickneko.workflow.nodes import exec_pack_onebot

    gateway = _FakeGateway()
    ctx_ = NodeExecutionContext(owner_id="u-admin", gateway=gateway)
    node_ = WorkflowNode(id="p1", type="pack-onebot", config={"chat": "private", "chat_id": "hand"})
    ctx_.inputs = {"chat": "group", "chat_id": "wired-70001"}
    await exec_pack_onebot(node_, ctx_)
    assert gateway.target_calls == [
        ("onebot", {"owner_id": "u-admin", "chat": "group", "chat_id": "wired-70001"})
    ]


@pytest.mark.asyncio
async def test_pack_requires_gateway() -> None:
    """封装需要平台总线：没接（gateway=None）是环境问题，当场抛（与 target 手动填同口径）。"""
    from tickneko.workflow.nodes import exec_pack_onebot

    ctx_ = NodeExecutionContext(owner_id="u-admin")
    node_ = WorkflowNode(id="p1", type="pack-onebot", config={"chat": "group", "chat_id": "7"})
    with pytest.raises(RuntimeError, match="gateway"):
        await exec_pack_onebot(node_, ctx_)


# ------------------------------------------------------------- ④-G 运算节点
@pytest.mark.asyncio
async def test_operator_does_arithmetic_and_formats_result() -> None:
    """加减乘除取余都能算：两边转数字；结果文本化——整数值不带小数点，除法是真除法。"""

    async def compute(symbol: str, left: str, right: str) -> str:
        node_ = WorkflowNode(id="m1", type="operator", config={"operator": symbol})
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"left": left, "right": right}
        result = await exec_operator(node_, ctx_)
        return str(result["operator_result"])

    assert await compute("+", "2", "3") == "5"
    assert await compute("-", "7", "10") == "-3"
    assert await compute("*", "2.5", "4") == "10"  # 整数值不带小数点
    assert await compute("/", "7", "2") == "3.5"  # 真除法（不是整除）
    assert await compute("/", "6", "2") == "3"
    assert await compute("%", "7", "3") == "1"
    assert await compute("%", "-7", "3") == "2"  # Python 语义：符号跟随除数
    assert await compute("+", "0.1", "0.2") == "0.30000000000000004"  # 不做「善意」四舍五入


@pytest.mark.asyncio
async def test_operator_takes_operands_from_wire_with_hand_fallback() -> None:
    """线上的值优先；没接线才用手填兜底（left / right 就是「可被连线覆盖的入口」）。"""
    node_ = WorkflowNode(id="m1", type="operator", config={"operator": "+", "left": "3", "right": "4"})

    # 没接线：手填的 left / right 生效
    ctx_ = NodeExecutionContext()
    result = await exec_operator(node_, ctx_)
    assert result["operator_result"] == "7"

    # 接线了：线上的值覆盖手填
    ctx_ = NodeExecutionContext()
    ctx_.inputs = {"left": "9", "right": "4"}
    result = await exec_operator(node_, ctx_)
    assert result["operator_result"] == "13"  # 9 + 4（若手填生效会是 3 + 4 = 7）


@pytest.mark.asyncio
async def test_operator_failure_stops_propagation() -> None:
    """算不出来 = **业务失败**（抛 NodeFailure，引擎停止向下传播）：空值 / 非数字 / 除数为 0 /
    运算符不合法 —— 不再送空串，免得下游拿着「没算出来」的结果继续跑。"""
    from tickneko.workflow.nodes import NodeFailure

    async def failing(symbol: str, left: str, right: str) -> str:
        node_ = WorkflowNode(id="m1", type="operator", config={"operator": symbol})
        ctx_ = NodeExecutionContext()
        ctx_.inputs = {"left": left, "right": right}
        with pytest.raises(NodeFailure) as caught:
            await exec_operator(node_, ctx_)
        assert any("算不出来" in line for line in ctx_.log)  # 痕迹照留
        return str(caught.value)

    assert "左值为空" in await failing("+", "", "1")
    assert "不是数字" in await failing("*", "一会儿", "2")
    assert "除数为 0" in await failing("/", "1", "0")
    assert "除数为 0" in await failing("%", "1", "0")  # 取余同管
    assert "不合法" in await failing("≈", "1", "2")  # 没走过校验的图：非法运算符也走这条


@pytest.mark.asyncio
async def test_operator_failure_in_graph_skips_downstream() -> None:
    """图里跑：operator 算不出来时下游整段跳过（不是拿空串继续跑）。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("m", "operator", operator="+", left="当前时间为：", right="1"),
                node("l", "log", message="手填兜底"),  # 内容入口接了 operator（算不出来）
                node("e", "end"),
            ],
            "edges": [
                edge("s", "m"),
                edge("s", "l"),
                edge("m", "l", "operator_result", "message"),
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)  # 不抛：业务失败只停自己这条线

    assert any("[failed] m:" in line for line in ctx.log)
    # log 的另一条活入边（start）还在，所以照常执行；内容入口没被失败节点顶掉
    assert any("[INFO] l: 手填兜底" in line for line in ctx.log)


def test_operator_symbol_is_validated() -> None:
    """运算符枚举在语义阶段拦住（拼错保存就报 INVALID_OPERATOR_SYMBOL）；必填入口照常。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("m", "operator", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "m"), edge("m", "e")],
        }

    assert validate_graph(graph_with(left="1", operator="*", right="2")).valid

    report = validate_graph(graph_with(left="1", operator="≈", right="2"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_OPERATOR_SYMBOL"]

    # 必填入口 right：接线或手填两个都没有 -> INPUT_NOT_CONNECTED
    report = validate_graph(graph_with(operator="+", left="1"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INPUT_NOT_CONNECTED"]


# ------------------------------------------------------------- ④-G 缓存节点
class _FakeCache:
    """假的缓存门面（鸭子形状对齐 ``tickneko.core.cache.Cache``：``get`` / ``set`` 两个异步方法）。

    单元测试里不碰进程级单例（它没 ``start()``，直接调会抛 ``CacheError``）—— 给 ctx
    注入这个假对象即可。
    """

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        self.data[key] = value


@pytest.mark.asyncio
async def test_cache_set_then_get_roundtrip_with_scoped_prefixes() -> None:
    """set 写、get 读（值按文本存）；缓存键用前缀区分作用域：账号级带归属、图级带图 id。"""
    fake = _FakeCache()
    ctx_ = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    async def run(action: str, **inputs: object) -> dict[str, Any]:
        node_ = WorkflowNode(id="ca1", type="cache", config={"action": action, "scope": "account"})
        ctx_.inputs = dict(inputs)
        return await exec_cache(node_, ctx_)

    # set：写进归属前缀下，输出把写进去的值回传（下游接着用）
    result = await run("set", key="日签", value="上班")
    assert fake.data == {"workflow:acct:u-admin:日签": "上班"}
    assert result["cache_value"] == "上班"
    assert any("[cache] ca1: set workflow:acct:u-admin:日签 = 上班" in line for line in ctx_.log)

    # get：同一个键读回来
    result = await run("get", key="日签")
    assert result["cache_value"] == "上班"
    assert any("-> 上班" in line for line in ctx_.log)

    # 图级作用域换前缀；线上送整数（now_ts 那种）也会转成文本
    node_ = WorkflowNode(id="ca1", type="cache", config={"action": "set", "scope": "workflow"})
    ctx_.inputs = {"key": "计数", "value": 42}
    await exec_cache(node_, ctx_)
    assert fake.data["workflow:graph:w1:计数"] == "42"


@pytest.mark.asyncio
async def test_cache_get_miss_returns_empty_without_alarm() -> None:
    """没存过不算事故：送空串、流程继续（日志留一行「还没存过」）。"""
    fake = _FakeCache()
    ctx_ = NodeExecutionContext(owner_id="u-admin", cache=fake)
    node_ = WorkflowNode(id="ca1", type="cache", config={"action": "get"})
    ctx_.inputs = {"key": "没存过的"}
    result = await exec_cache(node_, ctx_)

    assert result["cache_value"] == ""
    assert any("还没存过" in line for line in ctx_.log)


@pytest.mark.asyncio
async def test_cache_get_miss_falls_back_to_default_value() -> None:
    """读不到回默认值：手填 / 接线都行（线上优先）；存过了默认值就不生效。"""
    fake = _FakeCache()
    ctx_ = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    node_ = WorkflowNode(id="ca1", type="cache", config={"action": "get", "default": "1"})

    # 没存过 + 手填默认值：输出默认值，日志留一行
    ctx_.inputs = {"key": "计数"}
    result = await exec_cache(node_, ctx_)
    assert result["cache_value"] == "1"
    assert any("还没存过，用默认值" in line for line in ctx_.log)

    # 没存过 + 线上也送默认值：线上优先（手填的 1 被盖掉），整数照样文本化
    ctx_.inputs = {"key": "计数", "default": 7}
    result = await exec_cache(node_, ctx_)
    assert result["cache_value"] == "7"

    # 存过了：默认值不生效，读到什么给什么
    set_node = WorkflowNode(id="ca1", type="cache", config={"action": "set"})
    ctx_.inputs = {"key": "计数", "value": "9"}
    await exec_cache(set_node, ctx_)
    ctx_.inputs = {"key": "计数"}
    result = await exec_cache(node_, ctx_)
    assert result["cache_value"] == "9"


@pytest.mark.asyncio
async def test_cache_raises_on_missing_key_and_owner_or_bad_enums() -> None:
    """环境 / 配置问题当场抛：key 空 / 账号作用域没有归属 / 非法的动作、作用域（兜底）。"""
    fake = _FakeCache()
    ctx_ = NodeExecutionContext(owner_id="u-admin", cache=fake)

    node_ = WorkflowNode(id="ca1", type="cache", config={"action": "get"})
    ctx_.inputs = {"key": ""}  # key 没接线也没手填
    with pytest.raises(ValueError, match="key 为空"):
        await exec_cache(node_, ctx_)

    node_ = WorkflowNode(id="ca1", type="cache", config={"action": "get", "scope": "account"})
    ctx_no_owner = NodeExecutionContext(cache=fake)  # 离线跑，没归属
    ctx_no_owner.inputs = {"key": "x"}
    with pytest.raises(ValueError, match="owner_id"):
        await exec_cache(node_, ctx_no_owner)

    node_ = WorkflowNode(id="ca1", type="cache", config={"action": "remove", "scope": "workflow"})
    ctx_.inputs = {"key": "x"}
    with pytest.raises(ValueError, match="动作不合法"):
        await exec_cache(node_, ctx_)

    node_ = WorkflowNode(id="ca1", type="cache", config={"action": "get", "scope": "全局"})
    with pytest.raises(ValueError, match="作用域不合法"):
        await exec_cache(node_, ctx_)


def test_cache_fields_are_validated() -> None:
    """两块枚举在语义阶段拦住（INVALID_CACHE_ACTION / INVALID_CACHE_SCOPE）；key 必填照常。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("ca", "cache", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "ca"), edge("ca", "e")],
        }

    assert validate_graph(graph_with(action="set", scope="account", key="x", value="1")).valid

    report = validate_graph(graph_with(action="remove", scope="account", key="x"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_CACHE_ACTION"]

    report = validate_graph(graph_with(action="get", scope="全局", key="x"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INVALID_CACHE_SCOPE"]

    # key 是必填入口：没接线也没手填 -> INPUT_NOT_CONNECTED
    report = validate_graph(graph_with(action="get"))
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert [issue.code for issue in report.errors] == ["INPUT_NOT_CONNECTED"]


# --------------------------------------------------------------------------- ⑤ 自写节点
def test_builtin_node_executors_are_registered() -> None:
    """包一被 import，内置节点的执行函数就都登记好了（一类一个文件，各自注册）。"""
    for node_type in (
        "trigger-message", "trigger-time", "trigger-event", "end", "log", "test", "http", "constant", "delay",
        "json", "regex", "now", "condition", "operator", "cache", "placeholder",
    ):
        assert get_executor(node_type) is not None
    assert set(registered_types()) >= {
        "trigger-message", "trigger-time", "trigger-event", "end", "log", "test", "http", "constant", "delay",
        "json", "regex", "now", "condition", "operator", "cache", "placeholder",
    }


def test_builtin_field_metadata_is_declared_in_backend() -> None:
    """枚举选项与默认值都写在注册表里，画布照单渲染（不再自己填 GET / INFO / hello）。

    这几条以前只活在前端的节点表里（后端没声明），是两边最容易各自漂移的地方：
    ``test`` 的 message 字段、``http.method`` 的缺省 GET、``log.level`` / ``start.trigger``
    的可选值。声明清楚了，「后端提供什么、画布显示什么」才立得住。
    """
    from tickneko.workflow import get_spec
    from tickneko.workflow.nodes import HTTP_METHODS

    http = get_spec("http")
    assert http is not None
    method = next(f for f in http.fields if f.name == "method")
    assert method.default == "GET"
    assert method.options is not None
    assert set(method.options) == HTTP_METHODS  # 下拉选项与校验规则同一份

    log = get_spec("log")
    assert log is not None
    level = next(f for f in log.fields if f.name == "level")
    assert level.default == "INFO"
    assert level.options == ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    from tickneko.workflow.nodes.triggers import EVENT_TYPE_OPTIONS

    # 触发器拆成三个：各带自己那份配置（定时看 cron、事件看事件类型、消息什么都不用配）
    message = get_spec("trigger-message")
    assert message is not None
    assert [f.name for f in message.fields] == []

    time_trigger = get_spec("trigger-time")
    assert time_trigger is not None
    assert [f.name for f in time_trigger.fields] == ["cron", "name"]
    # 专用编辑器由字段自己声明（cron -> 可视化选择器）：前端按标识挑控件，不认识节点类型
    assert time_trigger.fields[0].editor == "cron"
    assert time_trigger.fields[1].editor == ""

    event_trigger = get_spec("trigger-event")
    assert event_trigger is not None
    assert [f.name for f in event_trigger.fields] == ["event_type"]
    # 订阅哪种事件也是后端声明的下拉（画布不再自己抄一份事件类型）
    assert event_trigger.fields[0].options == EVENT_TYPE_OPTIONS
    assert "*" in EVENT_TYPE_OPTIONS  # 通配：任何事件都触发
    # 下拉里显示中文、值仍是平台原生事件名（要跟上报的 event_type 全等匹配，一个字符都不能改）
    labels = event_trigger.fields[0].option_labels
    assert set(labels) == set(EVENT_TYPE_OPTIONS)  # 每一项都有显示名，别漏
    assert labels["group_increase"] == "有人进群" and labels["*"] == "任何事件"

    test = get_spec("test")
    assert test is not None
    assert [f.name for f in test.fields] == ["message"]
    assert test.fields[0].default == "hello"


def test_builtin_node_ports_and_labels_are_declared() -> None:
    """内置节点的中文名 / 面板顺序 / 端口都在注册表里（画布照它画，不再自己维护一份）。

    端口是**图的执行契约**：edge 的 ``source_port`` / ``target_port`` 存的就是这些 id，
    ``message`` 端口送值、``trigger`` 端口只表达先后；字段名与端口 id 同名的（``log`` 的
    ``message``、``http`` 的 ``url``）就是「可以被连线覆盖的那个入口」。
    """
    from tickneko.workflow import get_spec

    expected: dict[str, tuple[int, str, list[str], list[str]]] = {
        # 三个触发器各是一种节点类型（形状固定，画布不用再按 config 挑端口）
        "trigger-message": (10, "消息触发", [], ["trigger", "message", "target"]),
        "trigger-time": (11, "定时触发", [], ["trigger"]),
        "trigger-event": (
            12,
            "事件触发",
            [],
            ["trigger", "event_type", "user_id", "chat", "chat_id", "text", "target"],
        ),
        "end": (20, "结束", ["trigger"], []),
        "constant": (30, "常量", ["trigger"], ["trigger", "value"]),
        "log": (40, "写日志", ["trigger", "message"], ["trigger"]),
        "test": (50, "调试", ["trigger", "message"], ["trigger", "message"]),
        "http": (60, "HTTP", ["trigger", "url", "body"], ["trigger", "http_status", "http_body"]),
        "delay": (70, "等待", ["trigger", "seconds"], ["trigger"]),
        "json": (80, "JSON", ["trigger", "json", "path"], ["trigger", "json_value"]),
        "regex": (90, "正则", ["trigger", "text", "pattern", "replace"], ["trigger", "regex_value"]),
        "now": (100, "当前时间", ["trigger", "format"], ["trigger", "now_text", "now_ts"]),
        "condition": (110, "条件", ["trigger", "left", "right"], ["true", "false"]),
        "operator": (130, "运算", ["trigger", "left", "right"], ["trigger", "operator_result"]),
        "cache": (140, "缓存", ["trigger", "key", "value", "default"], ["trigger", "cache_value"]),
    }
    orders: list[int] = []
    for node_type, (order, label, inputs, outputs) in expected.items():
        spec = get_spec(node_type)
        assert spec is not None
        assert spec.order == order, node_type
        assert spec.label == label, node_type
        assert [p.id for p in spec.inputs] == inputs, node_type
        assert [p.id for p in spec.outputs] == outputs, node_type
        orders.append(spec.order)
    assert orders == sorted(orders)  # 面板顺序：内置节点依次排开、互不打架

    # 端口类型也要在：trigger 是控制流、message 是数据流，连线时按它配对
    log = get_spec("log")
    assert log is not None
    assert [(p.id, p.type) for p in log.inputs] == [
        ("trigger", "trigger"),
        ("message", "message"),
    ]
    assert log.inputs[0].label == "触发"  # 显示名同样来自后端
    assert log.inputs[1].required is True  # 必填入口：接线或手填同名字段
    assert log.inputs[0].required is False  # 触发端口不谈必填

    # 占位节点是泛型透传口：输入接什么类型、输出就是什么类型（泛型只走数据流、不接触发）
    placeholder = get_spec("placeholder")
    assert placeholder is not None
    assert [(p.id, p.type) for p in placeholder.inputs] == [("trigger", "trigger"), ("value", "generic")]
    assert [(p.id, p.type) for p in placeholder.outputs] == [("trigger", "trigger"), ("value", "generic")]

    http = get_spec("http")
    assert http is not None
    http_inputs = {p.id: p for p in http.inputs}
    assert http_inputs["url"].required is True  # url 是必填入口
    assert http_inputs["body"].required is False  # body 可选
    assert [(p.id, p.type) for p in http.outputs] == [
        ("trigger", "trigger"),
        ("http_status", "message"),
        ("http_body", "message"),
    ]

    json = get_spec("json")
    assert json is not None
    json_inputs = {p.id: p for p in json.inputs}
    assert json_inputs["json"].required is True  # json 文本：接线或手填的必填入口
    assert json_inputs["path"].required is False  # path 可选（留空 = 取整个文档）
    assert [(p.id, p.type) for p in json.outputs] == [
        ("trigger", "trigger"),
        ("json_value", "message"),
    ]

    regex = get_spec("regex")
    assert regex is not None
    regex_inputs = {p.id: p for p in regex.inputs}
    assert regex_inputs["text"].required is True  # 待处理文本：接线或手填
    assert regex_inputs["pattern"].required is True  # 正则：接线或手填
    assert regex_inputs["replace"].required is False  # 替换文本可选
    assert [(p.id, p.type) for p in regex.outputs] == [
        ("trigger", "trigger"),
        ("regex_value", "message"),
    ]

    now = get_spec("now")
    assert now is not None
    now_inputs = {p.id: p for p in now.inputs}
    assert now_inputs["format"].required is False  # 格式有缺省，可选
    assert [(p.id, p.type) for p in now.outputs] == [
        ("trigger", "trigger"),
        ("now_text", "message"),
        ("now_ts", "message"),
    ]

    condition = get_spec("condition")
    assert condition is not None
    condition_inputs = {p.id: p for p in condition.inputs}
    assert condition_inputs["left"].required is True  # 左值：接线或手填
    assert condition_inputs["right"].required is False  # 右值可选（也能接线）
    assert [(p.id, p.type) for p in condition.outputs] == [
        ("true", "trigger"),
        ("false", "trigger"),
    ]
    assert condition.branching is True  # 分流节点：引擎按选中出口剪枝
    assert condition.min_outgoing == 1  # 至少接一个出口才谈得上分支

    operator = get_spec("operator")
    assert operator is not None
    operator_inputs = {p.id: p for p in operator.inputs}
    assert operator_inputs["left"].required is True  # 左值：接线或手填
    assert operator_inputs["right"].required is True  # 右值同样必填（算术缺一边算不了）
    assert [(p.id, p.type) for p in operator.outputs] == [
        ("trigger", "trigger"),
        ("operator_result", "message"),
    ]

    cache = get_spec("cache")
    assert cache is not None
    cache_inputs = {p.id: p for p in cache.inputs}
    assert cache_inputs["key"].required is True  # 变量名：接线或手填
    assert cache_inputs["value"].required is False  # 写入值：set 才要，运行期用
    assert cache_inputs["default"].required is False  # 默认值：get 读不到时兜底，运行期用
    assert [(p.id, p.type) for p in cache.outputs] == [
        ("trigger", "trigger"),
        ("cache_value", "message"),
    ]


def test_declare_node_type_gives_rules_without_executor() -> None:
    """``declare_node_type``：规则在（必填字段 / 出边下限生效），执行器留空。

    内置节点里已经没有这种「只声明不实现」的类型了（见 nodes/ 的文件表），这条给扩展方用。
    """
    from tickneko.workflow import get_spec

    declare_node_type(
        "test-declared",
        fields=[ConfigField("who", "审批人", required=True)],
        min_outgoing=2,
    )
    spec = get_spec("test-declared")
    assert spec is not None
    assert spec.min_outgoing == 2
    assert spec.executor is None  # 只有声明
    assert get_spec("end").max_outgoing == 0  # 内置节点的约束同样在注册表里

    # 出边给够（两条），好让流水线走到语义阶段去查必填字段
    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("a", "test-declared"),
            node("e", "end"),
            node("e2", "end"),
        ],
        "edges": [edge("s", "a"), edge("a", "e"), edge("a", "e2")],
    }
    codes = {i.code for i in validate_graph(graph).errors}
    assert "MISSING_CONFIG" in codes  # who 必填规则来自注册声明


def test_register_node_decorator_registers_and_returns_the_function() -> None:
    """``@register_node`` 当场注册，并返回原函数（照旧能直接调用 / 拿去单测）。"""

    @register_node("my-echo")
    async def exec_my_echo(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        return {"echo": node.id, "seen": len(ctx.inputs)}

    assert get_executor("my-echo") is exec_my_echo


def test_load_node_modules_is_idempotent_and_loud_on_failure() -> None:
    """装别人的节点模块：重复加载幂等；模块不存在当场抛（别把「没注册上」藏到跑图时才报）。"""
    assert load_node_modules("tickneko.workflow.nodes.log") == ["tickneko.workflow.nodes.log"]
    # 第二次命中 sys.modules 缓存：模块体不会再执行一遍
    assert load_node_modules("tickneko.workflow.nodes.log") == ["tickneko.workflow.nodes.log"]

    with pytest.raises(ModuleNotFoundError):
        load_node_modules("tickneko.workflow.nodes.no_such_module")


@pytest.mark.asyncio
async def test_custom_node_type_runs_end_to_end() -> None:
    """自写的节点类型：声明输入 / 输出端口后，引擎按边把值送进来、再按出口送下去。

    这里直接构造图（不经过校验）：本条验的是「注册表 + 引擎投递」这条链路。
    校验那一关的合法性同样查注册表（见 test_validation_rules_are_driven_by_registration_*）。
    """
    seen: list[str] = []

    @register_node(
        "my-upper",
        inputs=[PortSpec("text", "message", "文本")],
        outputs=[PortSpec("upper", "message", "大写")],
    )
    async def exec_my_upper(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        text = str(input_value(node, ctx, "text"))
        seen.append(text)
        return {"upper": text.upper()}

    graph = WorkflowGraph(
        nodes=[
            WorkflowNode(id="s", type="trigger-message", config={}),
            WorkflowNode(id="c", type="constant", config={"value": "hi tickneko"}),
            WorkflowNode(id="u", type="my-upper", config={}),
        ],
        edges=[
            WorkflowEdge(source="s", target="c", source_port="trigger", target_port="trigger"),
            # 常量节点的 value 出口 -> 自写节点的 text 入口
            WorkflowEdge(source="c", target="u", source_port="value", target_port="text"),
        ],
    )
    ctx = NodeExecutionContext()

    await SimpleWorkflowRunner().run(graph, ctx)

    assert seen == ["hi tickneko"]  # 值确实沿 c.value -> u.text 送到了
    assert ctx.inputs == {"text": "hi tickneko"}  # 最后一个节点的入口就是它收到的那份


# --------------------------------------------------------------------------- checksum
def test_checksum_stable_under_key_order_and_spacing() -> None:
    """同一张图不同写法（键序、空白）算同一个摘要；内容变了摘要才变。"""
    first = canonical_graph_json(linear_graph())
    reordered = {"edges": [
        {"targetPort": "trigger", "target": "e", "sourcePort": "trigger", "source": "s"},
    ], "nodes": [
        {"config": {}, "type": "trigger-message", "id": "s"},
        {"config": {}, "type": "end", "id": "e"},
    ]}
    assert graph_checksum(reordered) == graph_checksum(linear_graph())
    changed = {"nodes": [node("s", "trigger-message"), node("e2", "end")], "edges": [edge("s", "e2")]}
    assert graph_checksum(changed) != graph_checksum(linear_graph())
    assert json_loads(first)["nodes"][0]["id"] == "s"


def test_checksum_ignores_node_positions_but_snapshot_keeps_them() -> None:
    """挪动节点坐标不改变摘要（不产生新版本），但规范快照里坐标仍然保留。"""
    base = {"nodes": [node("s", "trigger-message"), node("e", "end")], "edges": [edge("s", "e")]}
    moved: dict[str, object] = {
        "nodes": [
            {**node("s", "trigger-message"), "x": 120, "y": 240},
            {**node("e", "end"), "x": 480, "y": 96},
        ],
        "edges": [edge("s", "e")],
    }
    assert graph_checksum(moved) == graph_checksum(base)

    snapshot = json_loads(canonical_graph_json(moved))
    assert snapshot["nodes"][0]["x"] == 120
    assert snapshot["nodes"][0]["y"] == 240
    # 再挪一次：摘要依旧相同（坐标字段不进 hash）
    moved_again = {
        "nodes": [
            {**node("s", "trigger-message"), "x": 1, "y": 1},
            node("e", "end"),
        ],
        "edges": [edge("s", "e")],
    }
    assert graph_checksum(moved_again) == graph_checksum(base)


def test_edge_ports_round_trip_and_affect_checksum() -> None:
    """边的端口字段（驼峰 / 下划线两种写法）都能解析，且属于图内容、参与摘要。"""
    camel = {
        "nodes": [node("s", "trigger-message"), node("e", "end")],
        "edges": [{"source": "s", "target": "e", "sourcePort": "trigger",
                   "targetPort": "trigger"}],
    }
    snake = {
        "nodes": [node("s", "trigger-message"), node("e", "end")],
        "edges": [{"source": "s", "target": "e", "source_port": "trigger",
                   "target_port": "trigger"}],
    }
    camel_g = WorkflowGraph.model_validate(camel)
    snake_g = WorkflowGraph.model_validate(snake)
    assert camel_g.edges[0].source_port == "trigger"
    assert snake_g.edges[0].source_port == "trigger"
    assert graph_checksum(camel_g) == graph_checksum(snake_g)

    # 端口是图内容的一部分：同样的连线、没写端口，摘要就不同（缺省端口只影响运行期口径）
    no_ports = {
        "nodes": [node("s", "trigger-message"), node("e", "end")],
        "edges": [{"source": "s", "target": "e"}],
    }
    assert graph_checksum(camel_g) != graph_checksum(no_ports)


def test_draft_graph_is_lenient() -> None:
    """暂存图允许空节点列表 / 缺字段 / 额外 UI 数据（提交版本时才严格校验）。"""
    from tickneko.workflow import DraftGraph

    draft = DraftGraph.model_validate({"nodes": [], "edges": []})
    assert draft.nodes == [] and draft.edges == []
    half = DraftGraph.model_validate(
        {"nodes": [{"id": "n1"}], "edges": [{"source": "n1"}], "viewport": {"zoom": 1.5}}
    )
    assert half.nodes[0].type == ""
    assert half.edges[0].target == ""
    assert half.model_dump()["viewport"] == {"zoom": 1.5}


def json_loads(text: str) -> dict[str, object]:
    """测试用小工具（放文件尾部避免遮蔽标准库导入位置）。"""
    import json

    return json.loads(text)


# --------------------------------------------------------------------------- 存储
_MEMORY_ENGINES: list[AsyncEngine] = []


@pytest.fixture
async def store() -> AsyncGenerator[SqlWorkflowStore]:
    """每个用例一块内存 sqlite（表已建好），用完 dispose。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    created = SqlWorkflowStore(engine)
    await created.ensure_schema()
    yield created


@pytest.fixture(autouse=True)
async def _dispose_memory_engines() -> AsyncGenerator[None]:
    yield
    while _MEMORY_ENGINES:
        await _MEMORY_ENGINES.pop().dispose()


async def test_store_owner_isolation_and_name_conflict(
    store: SqlWorkflowStore,
) -> None:
    alice = await store.create("u-admin", "审批流")
    bob = await store.create("u-robot", "审批流")  # 不同归属允许同名
    assert alice.id != bob.id

    with pytest.raises(WorkflowNameConflict):
        await store.create("u-admin", "审批流")  # 同归属同名拒绝

    assert {item.id for item in await store.list(owner_id="u-admin")} == {alice.id}
    assert {item.id for item in await store.list(owner_id=None)} == {alice.id, bob.id}
    assert (await store.list(owner_id="u-robot"))[0].current_version == 0


async def test_store_version_increment_dedup_and_publish(
    store: SqlWorkflowStore,
) -> None:
    definition = await store.create("u-admin", "发版流")
    graph = linear_graph()

    first, created_first = await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
        note="首版",
    )
    assert created_first and first.version == 1

    # 提交后当前指针切到 version
    pointed = await store.get(definition.id)
    assert pointed is not None and pointed.current_ref == "version"

    # 内容没变：命中最新版本，不新增
    again, created_again = await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert not created_again and again.version == 1
    # 命中已有版本也算一次「提交」：指针仍是 version
    pointed_again = await store.get(definition.id)
    assert pointed_again is not None and pointed_again.current_ref == "version"

    # 改了：新版本 2，定义指针跟着挪
    graph_v2 = {"nodes": [node("s", "trigger-message"), node("m", "test"), node("e", "end")],
                "edges": [edge("s", "m"), edge("m", "e")]}
    second, created_second = await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph_v2),
        checksum=graph_checksum(graph_v2),
    )
    assert created_second and second.version == 2
    latest = await store.get(definition.id)
    assert latest is not None and latest.current_version == 2 and latest.status == "draft"

    # 版本历史倒序、按号取快照
    history = await store.list_versions(definition.id)
    assert [item.version for item in history] == [2, 1]
    snapshot = await store.get_version(definition.id, 1)
    assert snapshot is not None and snapshot.graph().nodes[0].id == "s"

    # 发布最新版；发布不存在的版本返回 None
    published = await store.publish(definition.id, 2)
    assert published is not None and published.status == "published"
    assert published.published_version == 2
    assert await store.publish(definition.id, 99) is None


async def test_store_delete_cascades_versions(store: SqlWorkflowStore) -> None:
    definition = await store.create("u-admin", "待删流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(linear_graph()),
        checksum=graph_checksum(linear_graph()),
    )
    assert await store.delete(definition.id) is True
    assert await store.get(definition.id) is None
    assert await store.list_versions(definition.id) == []
    assert await store.delete(definition.id) is False  # 再删一次


async def test_store_draft_save_overwrites_and_switches_pointer(
    store: SqlWorkflowStore,
) -> None:
    """暂存覆盖式写图、指针切 draft；提交版本后指针切 version；暂存内容原样可读。"""
    from tickneko.workflow import canonical_draft_json

    definition = await store.create("u-admin", "暂存流")
    # 新建默认指针 draft，没暂存过
    assert definition.current_ref == "draft"
    assert definition.draft_graph_json == "" and definition.draft_updated_at == 0.0

    # 半张图也能暂存（不校验）
    half = {"nodes": [{"id": "s", "type": "trigger-message"}], "edges": []}
    saved = await store.save_draft(definition.id, canonical_draft_json(half))
    assert saved is not None and saved.current_ref == "draft"
    draft = saved.draft_graph()
    assert draft is not None and draft.nodes[0].id == "s"
    assert saved.draft_updated_at > 0

    # 提交版本：指针切到 version，暂存内容不受影响
    version, created = await store.add_version(
        saved,
        graph_json=canonical_graph_json(linear_graph()),
        checksum=graph_checksum(linear_graph()),
    )
    assert created and version.version == 1
    committed = await store.get(definition.id)
    assert committed is not None and committed.current_ref == "version"

    # 再暂存：指针切回 draft，暂存被覆盖
    redraft = await store.save_draft(definition.id, canonical_draft_json(half))
    assert redraft is not None and redraft.current_ref == "draft"

    # 不存在的工作流暂存返回 None
    assert await store.save_draft("not-exist", canonical_draft_json(half)) is None


async def test_definition_enabled_defaults_off_and_toggles() -> None:
    """运行开关：新建默认关（发布 ≠ 运行），能拨开能拨回，不存在返回 ``None``。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    try:
        await store.ensure_schema()
        created = await store.create("u-admin", "开关流")
        assert created.enabled is False  # 默认不跑

        turned_on = await store.set_enabled(created.id, True)
        assert turned_on is not None and turned_on.enabled is True
        stored = await store.get(created.id)
        assert stored is not None and stored.enabled is True  # 真写进去了

        turned_off = await store.set_enabled(created.id, False)
        assert turned_off is not None and turned_off.enabled is False
        assert await store.set_enabled("not-exist", True) is None
    finally:
        await engine.dispose()


async def test_definition_settings_default_single_instance_and_update() -> None:
    """工作流**设置**（实例策略）：新建默认**单实例**，能改成多实例再改回来；不存在返回 ``None``。

    设置与运行开关 / 发布指针各管各的：改设置不该顺手动了那两样。
    """
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    try:
        await store.ensure_schema()
        created = await store.create("u-admin", "设置流")
        assert created.multi_instance is False  # 默认单实例

        updated = await store.update_settings(created.id, multi_instance=True)
        assert updated is not None and updated.multi_instance is True
        stored = await store.get(created.id)
        assert stored is not None and stored.multi_instance is True  # 真写进去了
        assert stored.enabled is False  # 只碰设置：开关没被顺手拨开

        back = await store.update_settings(created.id, multi_instance=False)
        assert back is not None and back.multi_instance is False
        assert await store.update_settings("not-exist", multi_instance=True) is None
    finally:
        await engine.dispose()


async def test_old_definition_table_gets_the_added_columns() -> None:
    """老库（建表时还没有 enabled / multi_instance 列）在 ``ensure_schema`` 时补上，都填 0。

    补列不能让升级上来的库突然开始跑、也不能让定时任务突然变成多实例 —— 所以 ALTER 的默认值
    取「关」和「单实例」（见 store 的 ``_DEFINITION_ADDED_COLUMNS``）。
    """
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        # 造一张「老表」：只有最早的几列，没有 enabled / 暂存区那几列
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE workflow_definitions ("
                "id VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(64), name VARCHAR(128),"
                " status VARCHAR(16), current_version INTEGER, published_version INTEGER,"
                " created_at FLOAT, updated_at FLOAT)"
            )
            await conn.exec_driver_sql(
                "INSERT INTO workflow_definitions (id, owner_id, name, status,"
                " current_version, published_version, created_at, updated_at)"
                " VALUES ('wf-old', 'u-admin', '老数据', 'published', 1, 1, 1.0, 1.0)"
            )

        store = SqlWorkflowStore(engine)
        await store.ensure_schema()  # 建表跳过（已存在）+ 补增量列

        old = await store.get("wf-old")
        assert old is not None
        assert old.enabled is False  # 升级上来默认「不跑」
        assert old.multi_instance is False  # 实例策略默认「单实例」
        assert old.published_version == 1  # 别的列没被碰
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- HTTP 接口
def api_app() -> FastAPI:
    """接口层应用（演示账号在 lifespan 里种好；工作流双表在同一块内存 sqlite）。"""
    return create_app(ApiOptions(prefix="/api"), hasher=_TEST_HASHER)


class FakeTriggers:
    """假触发器：只记账（``start`` / ``stop`` 各被叫了几次、拿的哪一版）。

    验的是「接口层有没有按开关去即时启停」，不用真调度器（那个由运行时那组用例覆盖）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def start(self, workflow_id: str, version: int) -> int:
        self.calls.append(("start", workflow_id, version))
        return 1

    async def stop(self, workflow_id: str, version: int) -> int:
        self.calls.append(("stop", workflow_id, version))
        return 1


def api_app_with_triggers(triggers: FakeTriggers) -> FastAPI:
    """接口层应用 + 运行时触发器（主程序就是这么传的，见 ``tickneko.bootstrap``）。"""
    return create_app(
        ApiOptions(prefix="/api"), hasher=_TEST_HASHER, workflow_triggers=triggers
    )


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """直连 ASGI 并手动跑一遍 lifespan（建表 / 种账号在里面）。"""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


async def login(client: httpx.AsyncClient, who: dict[str, str]) -> str:
    """登录拿令牌。

    演示账号现在只剩 ``admin``（见 ``tickneko.api.services.user.demo``）：非 admin 的账号
    由这里顺手注册一个（注册要昵称，就用账号名顶上），用例不必各自准备。
    """
    if who["account"] != ADMIN["account"]:
        await client.post(
            "/api/auth/register", json={"nickname": who["account"], **who}
        )
    response = await client.post("/api/auth/login", json=who)
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_api_node_types_catalog_matches_registry() -> None:
    """``GET /workflows/node-types``：画布要的 label / 端口 / 字段全从这儿来，顺序也排好了。

    这就是「后端提供什么、画布显示什么」的那份数据 —— 没有它，前端只能自己维护一份节点表，
    两边迟早漂移（``test`` 的 echo 字段就是这么漂出去的）。
    """
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)
        response = await client.get("/api/workflows/node-types", headers=auth(token))
        assert response.status_code == 200, response.text
        payload = response.json()["data"]

    nodes = {item["type"]: item for item in payload["nodes"]}
    assert set(nodes) == set(registered_types())  # 注册了什么就有什么

    # 端口类型也随目录下发：画布的端口配色 / 图例 / 数据流语义都从这儿来，前端不再抄一份
    port_types = {item["type"]: item for item in payload["port_types"]}
    assert [item["type"] for item in payload["port_types"]] == [
        "trigger",
        "message",
        "target",
        "list",
        "dict",
        "generic",
    ]
    assert port_types["trigger"]["data"] is False  # 控制流：只表达先后
    assert port_types["trigger"]["label"] == "触发（控制流）"
    for port_type in ("message", "target", "list", "dict", "generic"):
        assert port_types[port_type]["data"] is True  # 数据流：沿边送值
        assert port_types[port_type]["color"]  # 每种类型都带配色

    http = nodes["http"]
    assert http["label"] == "HTTP"
    assert http["color"] == "#0ea5e9"  # 节点配色也随目录下发：加类型只改后端，前端不再抄一份
    assert http["role"] == "normal"
    assert http["has_executor"] is True
    assert [port["id"] for port in http["outputs"]] == ["trigger", "http_status", "http_body"]
    assert http["outputs"][2]["label"] == "响应正文"  # 端口显示名也来自后端
    assert [port["type"] for port in http["outputs"]] == ["trigger", "message", "message"]
    # 输入端口：触发 + url（必填入口）+ body（可选）
    assert [(port["id"], port["required"]) for port in http["inputs"]] == [
        ("trigger", False),
        ("url", True),
        ("body", False),
    ]
    method = next(field for field in http["fields"] if field["name"] == "method")
    assert method["default"] == "GET" and method["has_default"] is True
    assert method["options"] == ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    url = next(field for field in http["fields"] if field["name"] == "url")
    # url 的「必填」落在入口上：字段本身没默认值，接线或手填都行
    assert url["required"] is False and url["has_default"] is False and url["default"] is None
    # 没配显示名的枚举就是 null：画布直接显示值本身（通用协议名，中文反而不好认）
    assert method["option_labels"] is None

    # 枚举的显示名随目录下发：事件类型的值是平台原生名（要跟上报全等匹配，不能改），
    # 画布按 option_labels 显示中文
    event_field = nodes["trigger-event"]["fields"][0]
    assert event_field["name"] == "event_type"
    assert set(event_field["option_labels"]) == set(event_field["options"])
    assert event_field["option_labels"]["friend"] == "加好友请求"
    assert "*" in event_field["options"]  # 值仍是通配符那个写法
    assert event_field["default"] == "*"  # 缺省「任何事件」：没动过这个下拉也算配好了

    # 专用编辑器也由后端声明：画布只认这个标识、按字段挑控件，不认识节点类型
    # （cron 走可视化选择器；其余字段没有专用编辑器，通用渲染）
    cron_field = next(f for f in nodes["trigger-time"]["fields"] if f["name"] == "cron")
    assert cron_field["editor"] == "cron"
    assert cron_field["options"] is None  # 有专用编辑器就不走「下拉」那条路
    assert method["editor"] == "" and event_field["editor"] == ""

    # 透传对：placeholder 的泛型入口 / 出口用 tie 互相指认（指向同一节点另一侧的端口 id）——
    # 画布据此让输入输出显示同一种类型（两端同色表示对应）
    ph = nodes["placeholder"]
    assert [(port["id"], port["type"], port["tie"]) for port in ph["inputs"]] == [
        ("trigger", "trigger", ""),
        ("value", "generic", "value"),
    ]
    assert [(port["id"], port["type"], port["tie"]) for port in ph["outputs"]] == [
        ("trigger", "trigger", ""),
        ("value", "generic", "value"),
    ]

    end = nodes["end"]
    assert end["max_outgoing"] == 0 and end["outputs"] == []
    orders = [item["order"] for item in payload["nodes"]]
    assert orders == sorted(orders)  # 面板顺序：接口给的就已经排好
    # 语义分类：画布面板按它分组 —— 目录的 categories 从节点注册**自动收集**：
    # 集合 = 所有节点的分类，顺序 = 面板顺序（按 order 排序后首次出现的顺序去重），
    # 显示名来自后端 CATEGORY_LABELS（查不到用机器名兜底）。面板不再维护白名单。
    # 三个触发器都归「触发」分类（面板里同一组）
    for trigger_type in ("trigger-message", "trigger-time", "trigger-event"):
        assert nodes[trigger_type]["category"] == "trigger"
    assert nodes["end"]["category"] == "end"
    assert nodes["constant"]["category"] == "constant"
    assert nodes["send"]["category"] == "action"
    assert nodes["condition"]["category"] == "control"
    assert nodes["json"]["category"] == "data"
    # 平台专属节点按平台分类：会话解包 / 封装，onebot 归 onebot、kook 归 kook
    assert nodes["unpack-onebot"]["category"] == "onebot"
    assert nodes["unpack-kook"]["category"] == "kook"
    assert nodes["pack-onebot"]["category"] == "onebot"
    assert nodes["pack-kook"]["category"] == "kook"
    # 自动收集口径：categories 与「节点分类按面板顺序去重」完全一致，且都有显示名
    expected = list(dict.fromkeys(item["category"] for item in payload["nodes"]))
    assert [c["name"] for c in payload["categories"]] == expected
    assert all(c["label"] for c in payload["categories"])


async def test_api_trace_id_is_filled_in_every_workflow_response() -> None:
    """每个工作流响应的 body 里都要有 trace_id（= 响应头 X-Trace-Id），不能是占位符 "-"。

    以前只有写操作（新建 / 改名 / 暂存 / 提交 / 发布）填了它，只读接口落到响应壳的默认值
    ``"-"`` —— 响应头一直是对的，缺的是 body 这个字段，于是排障时用户只能报一个 "-"。
    """
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "带编号的流"}
        )
        workflow_id = created.json()["data"]["id"]

        for path in (
            "/api/workflows",
            f"/api/workflows/{workflow_id}",
            f"/api/workflows/{workflow_id}/draft",
            f"/api/workflows/{workflow_id}/versions",
            "/api/workflows/node-types",
        ):
            response = await client.get(path, headers=auth(token))
            assert response.status_code == 200, (path, response.text)
            body_id = response.json()["trace_id"]
            assert body_id != "-", f"{path} 的 trace_id 是占位符"
            assert body_id == response.headers["X-Trace-Id"], path

        # 校验接口（业务结果走 200）同样要带上
        checked = await client.post(
            "/api/workflows/validate", headers=auth(token), json={"graph": {"nodes": []}}
        )
        assert checked.json()["trace_id"] == checked.headers["X-Trace-Id"]


async def test_api_owners_lists_everyone_for_admin_and_self_for_user() -> None:
    """``GET /owners``：给「按归属筛选」提供选项 —— 管理员拿全量，普通用户只有自己。

    昵称一起回来（下拉直接显示显示名，前端不必再查人）；没登录一律 401。
    """
    async with api_client(api_app()) as client:
        assert (await client.get("/api/owners")).status_code == 401  # 不往外说有哪些人

        admin = await login(client, ADMIN)
        registered = await client.post(
            "/api/auth/register",
            json={"account": "worker", "password": "tickneko-worker", "nickname": "干活的"},
        )
        assert registered.status_code == 201, registered.text
        worker_id = registered.json()["data"]["id"]

        owners = (await client.get("/api/owners", headers=auth(admin))).json()["data"]
        by_id = {item["owner_id"]: item for item in owners}
        assert by_id[worker_id]["nickname"] == "干活的"  # 昵称跟着回来
        assert "u-admin" in by_id  # 管理员自己也在清单里

        worker = await login(client, {"account": "worker", "password": "tickneko-worker"})
        mine = (await client.get("/api/owners", headers=auth(worker))).json()["data"]
        assert [item["owner_id"] for item in mine] == [worker_id]  # 只看得见自己


async def test_api_workflow_list_carries_owner_name_and_filters_by_owner() -> None:
    """工作流列表：每条带**归属昵称**；管理员 ``?owner_id=`` 可缩到某个归属。

    管理员看的是全库，两条流分属不同人时光有名字分不清是谁的 —— 归属昵称与归属筛选补的
    就是这一块。普通用户传了 ``owner_id`` 也不生效：隔离始终在服务端按登录身份把关。
    """
    async with api_client(api_app()) as client:
        admin = await login(client, ADMIN)
        registered = await client.post(
            "/api/auth/register",
            json={"account": "worker", "password": "tickneko-worker", "nickname": "干活的"},
        )
        worker_id = registered.json()["data"]["id"]
        worker = await login(client, {"account": "worker", "password": "tickneko-worker"})

        await client.post("/api/workflows", headers=auth(admin), json={"name": "管理员的流"})
        await client.post("/api/workflows", headers=auth(worker), json={"name": "干活的流"})

        # 管理员默认看全部：两条都在，各自带归属昵称
        everything = (await client.get("/api/workflows", headers=auth(admin))).json()["data"]
        assert {item["name"] for item in everything} == {"管理员的流", "干活的流"}
        names = {item["owner_id"]: item["owner_name"] for item in everything}
        assert names[worker_id] == "干活的"
        assert names["u-admin"]  # 昵称非空（演示账号有昵称）

        # 按归属筛：只剩那个人的流
        only_worker = (
            await client.get(
                "/api/workflows", headers=auth(admin), params={"owner_id": worker_id}
            )
        ).json()["data"]
        assert [item["name"] for item in only_worker] == ["干活的流"]

        # 普通用户带别人的归属也不生效：仍然只看得见自己的
        visible = (
            await client.get(
                "/api/workflows", headers=auth(worker), params={"owner_id": "u-admin"}
            )
        ).json()["data"]
        assert [item["name"] for item in visible] == ["干活的流"]


async def test_api_delete_workflow_stops_its_scheduled_tasks() -> None:
    """删工作流要**先把定时触发摘掉**：只删库的话任务还在调度器里，到点空跑一趟。

    顺序是先摘后删 —— 任务名要照这一版的图算，图没了就算不出来。
    """
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "待删的流"}
        )
        workflow_id = created.json()["data"]["id"]
        saved = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": linear_graph()},
        )
        assert saved.status_code == 201, saved.text
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200, published.text

        deleted = await client.delete(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert deleted.status_code == 204

    # 发布时开关还默认关着（不会 start），删除时应当 stop 一次、带着发布版本号
    assert ("stop", workflow_id, 1) in triggers.calls


async def test_api_create_validate_save_publish_full_chain() -> None:
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)

        # 校验接口：坏图返回 valid=false 且不碰库
        invalid = await client.post(
            "/api/workflows/validate", headers=auth(token), json={"graph": {"nodes": []}}
        )
        assert invalid.status_code == 200 and invalid.json()["data"]["valid"] is False

        # 新建
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "接口链路流"}
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["data"]["id"]

        # 保存坏版本：200 + 报告，版本号没动
        rejected = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": {"nodes": [node("s", "trigger-message")]}},  # 没 end
        )
        assert rejected.status_code == 200
        assert rejected.json()["data"]["valid"] is False
        detail = await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert detail.json()["data"]["current_version"] == 0

        # 保存好版本：201 + created=true；再存同样内容 created=false
        saved = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": linear_graph(), "note": "首版"},
        )
        assert saved.status_code == 201 and saved.json()["data"]["created"] is True
        saved_again = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": linear_graph()},
        )
        # 内容没变：200（没创建新资源）+ created=false
        assert saved_again.status_code == 200
        assert saved_again.json()["data"]["created"] is False

        # 版本历史 / 单版本 / 发布
        versions = await client.get(
            f"/api/workflows/{workflow_id}/versions", headers=auth(token)
        )
        assert versions.status_code == 200 and len(versions.json()["data"]) == 1
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200
        assert published.json()["data"]["status"] == "published"

        # 删除
        removed = await client.delete(
            f"/api/workflows/{workflow_id}", headers=auth(token)
        )
        assert removed.status_code == 204
        assert (await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))).status_code == 404


async def test_api_draft_stage_then_commit_then_publish() -> None:
    """暂存链路：空暂存 → 半张图可暂存（含坐标 / 端口）→ 指针随动作切换 → 提交 → 发布。"""
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "暂存链路流"}
        )
        workflow_id = created.json()["data"]["id"]

        # 初始：暂存区为空，指针默认 draft
        empty_draft = await client.get(
            f"/api/workflows/{workflow_id}/draft", headers=auth(token)
        )
        assert empty_draft.status_code == 200
        assert empty_draft.json()["data"]["graph"] is None
        assert (await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))) \
            .json()["data"]["current_ref"] == "draft"

        # 半张图（只有 start、带坐标）也能暂存，不校验；坐标原样回来
        half = {"nodes": [{"id": "s", "type": "trigger-message", "x": 12.5, "y": 34}], "edges": []}
        put = await client.put(
            f"/api/workflows/{workflow_id}/draft",
            headers=auth(token),
            json={"graph": half},
        )
        assert put.status_code == 200, put.text
        assert put.json()["data"]["current_ref"] == "draft"
        assert put.json()["data"]["draft_updated_at"] > 0

        got_draft = await client.get(
            f"/api/workflows/{workflow_id}/draft", headers=auth(token)
        )
        nodes = got_draft.json()["data"]["graph"]["nodes"]
        assert nodes[0]["id"] == "s" and nodes[0]["x"] == 12.5 and nodes[0]["y"] == 34

        # 图形态不合法（nodes 不是数组）-> 422，不写库
        bad = await client.put(
            f"/api/workflows/{workflow_id}/draft",
            headers=auth(token),
            json={"graph": {"nodes": "oops"}},
        )
        assert bad.status_code == 422

        # 提交合法版本后：指针切到 version
        graph_with_ports = {
            "nodes": [node("s", "trigger-message"), node("e", "end")],
            "edges": [{"source": "s", "target": "e", "sourcePort": "trigger",
                       "targetPort": "trigger"}],
        }
        committed = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": graph_with_ports},
        )
        assert committed.status_code == 201, committed.text
        detail = await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert detail.json()["data"]["current_ref"] == "version"

        # 版本快照里带回了端口字段（下划线形态）
        versions = await client.get(
            f"/api/workflows/{workflow_id}/versions", headers=auth(token)
        )
        snap_edge = versions.json()["data"][0]["graph"]["edges"][0]
        assert snap_edge["source_port"] == "trigger"
        assert snap_edge["target_port"] == "trigger"

        # 再暂存：指针切回 draft；发布只挪指针、200
        redraft = await client.put(
            f"/api/workflows/{workflow_id}/draft",
            headers=auth(token),
            json={"graph": half},
        )
        assert redraft.status_code == 200
        assert redraft.json()["data"]["current_ref"] == "draft"
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200
        assert published.json()["data"]["status"] == "published"
        # 发布不改变当前查看指针（仍指向暂存区）
        assert published.json()["data"]["current_ref"] == "draft"


async def test_load_published_workflows_registers_crons() -> None:
    """启动载入：**开着运行开关**的已发布定时流才把 start 登记到调度器。

    「已发布但开关关着」与「只存了版本没发布」两种都不登记 —— 发布 ≠ 运行。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    time_graph = {
        "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    published_def = await store.create("u-admin", "定时流")
    await store.add_version(
        published_def,
        graph_json=canonical_graph_json(time_graph),
        checksum=graph_checksum(time_graph),
    )
    assert await store.publish(published_def.id, 1) is not None
    assert await store.set_enabled(published_def.id, True) is not None  # 拨开开关才算「要跑」

    # 只有版本、没发布的工作流不该被载入（消息触发也不会登记任务）
    draft_def = await store.create("u-admin", "草稿流")
    await store.add_version(
        draft_def,
        graph_json=canonical_graph_json(linear_graph()),
        checksum=graph_checksum(linear_graph()),
    )

    # 发布过、但开关没拨开的：同样不登记（另起一个节点 id，免得和上面那个撞 task_id）
    off_graph = {
        "nodes": [node("so", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("so", "e")],
    }
    off_def = await store.create("u-admin", "发了但不跑的流")
    await store.add_version(
        off_def,
        graph_json=canonical_graph_json(off_graph),
        checksum=graph_checksum(off_graph),
    )
    assert await store.publish(off_def.id, 1) is not None

    scheduler = TaskManager()
    try:
        loaded = await load_published_workflows(store, scheduler)
        assert loaded == 1
        assert scheduler.get(f"wf-{published_def.id}-s") is not None
        # 开关关着的不登记（`get` 对不存在的任务是抛 KeyError，所以按清单看）
        assert [task.task_id for task in scheduler.list()] == [f"wf-{published_def.id}-s"]
    finally:
        await engine.dispose()


async def test_load_published_workflows_logs_what_it_loaded() -> None:
    """启动载入自己也要留痕：开头一条、结尾一条带各档条数，**一条没登记也照记**。

    「载入跑过了，只是没得跑」和「载入压根没跑」在日志里得能分开，所以完成那条不带
    ``if primed`` 的条件；扫过多少 / 登记上几条 / 开关关着跳过几条一并给出来 —— 排
    「某条流为何没跑」时不用再猜。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    # 三条定义：发布 + 开（登记）、发布但开关关着（跳过）、只存了版本没发布（跳过）
    on_def = await store.create("u-admin", "要跑的流")
    await store.add_version(
        on_def, graph_json=canonical_graph_json(graph), checksum=graph_checksum(graph)
    )
    assert await store.publish(on_def.id, 1) is not None
    assert await store.set_enabled(on_def.id, True) is not None

    off_def = await store.create("u-admin", "发了但不跑的流")
    await store.add_version(
        off_def, graph_json=canonical_graph_json(graph), checksum=graph_checksum(graph)
    )
    assert await store.publish(off_def.id, 1) is not None

    draft_def = await store.create("u-admin", "草稿流")
    await store.add_version(
        draft_def,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )

    scheduler = TaskManager()
    try:
        async with runtime_logs() as collected:
            assert await load_published_workflows(store, scheduler) == 1
            await wait_for_records(collected, count=2)

            messages = [record.message for record in collected]
            assert messages[0] == "开始载入已发布工作流"  # 时间线的起点
            assert messages[-1] == "已发布工作流启动载入完成"  # 结尾这条最后到
            assert collected[-1].extra == {
                "scanned": 3,  # 扫过的定义
                "registered": 1,  # 登记上的工作流
                "triggers": 1,  # 登记到的开始节点
                "disabled": 1,  # 开关关着跳过的
            }
    finally:
        await engine.dispose()


async def test_runtime_logs_are_attributed_to_the_workflow_owner() -> None:
    """跑图这趟的日志挂在**流的主人**名下，不再是一条「公共」的完成日志。

    归属本来就写在定义表里（``onebot`` 节点挑连接用的也是它），日志页却按 ``owner_id`` 筛
    —— 记成公共的话，「谁的流在跑」既筛不出来也追不到人。节点日志与运行时那几条同一口径：
    这一趟里**每条**都该是 ``u-admin``。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import make_trigger

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = linear_graph()  # start（消息触发）-> end
    definition = await store.create("u-admin", "认主人的流")
    await store.add_version(
        definition, graph_json=canonical_graph_json(graph), checksum=graph_checksum(graph)
    )
    assert await store.publish(definition.id, 1) is not None

    scheduler = TaskManager()
    try:
        async with runtime_logs() as collected:
            await make_trigger(definition.id, 1, store, scheduler)()
            await wait_for_records(collected)

            done = [record for record in collected if record.message == "工作流执行完成"]
            assert len(done) == 1
            assert done[0].owner_id == "u-admin"  # 一等字段：日志页按它筛
            assert done[0].extra["workflow_id"] == definition.id
            # 整趟都记在主人名下（节点日志走 ctx.logger，运行时那几条走 bind 的默认字段）
            assert {record.owner_id for record in collected} == {"u-admin"}
    finally:
        await engine.dispose()


async def test_load_published_workflows_pages_past_the_first_page() -> None:
    """启动载入**翻页翻到底**：超过一页的已发布工作流一个都不能漏。

    回归：以前那边写死 ``limit=500`` 一次拉完，第 501 条起的工作流开机不会登记（静默漏跑）。
    这里用 ``page_size=2`` 造 5 条，逼它翻三页（最后一页不满）。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    expected: list[str] = []
    for i in range(5):
        # 每条用各自的节点 id：任务名是 wf-<工作流 id>-<节点 id>，不该互相顶掉
        graph = {
            "nodes": [
                node(f"s{i}", "trigger-time", cron="*/5 * * * *"),
                node("e", "end"),
            ],
            "edges": [edge(f"s{i}", "e")],
        }
        definition = await store.create("u-admin", f"定时流 {i}")
        await store.add_version(
            definition,
            graph_json=canonical_graph_json(graph),
            checksum=graph_checksum(graph),
        )
        assert await store.publish(definition.id, 1) is not None
        assert await store.set_enabled(definition.id, True) is not None
        expected.append(f"wf-{definition.id}-s{i}")

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler, page_size=2) == 5
        assert sorted(task.task_id for task in scheduler.list()) == sorted(expected)
    finally:
        await engine.dispose()


async def test_load_published_workflows_passes_the_instance_strategy_to_the_scheduler() -> None:
    """实例策略是**工作流设置**（定义表里的列）：登记时传给调度器，单 / 多实例各按各的。

    调度器靠 ``Task.multi_instance`` 决定「上一次还没跑完、到点又到点」时是跳过本次还是开新
    实例，所以这条断言的是「设置真的落到了那个任务上」——登记那一趟读定义表，见
    :func:`tickneko.workflow.runtime.register_published_workflow`。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    plain = await store.create("u-admin", "单实例流")
    stacked = await store.create("u-admin", "多实例流")
    for definition in (plain, stacked):
        await store.add_version(
            definition,
            graph_json=canonical_graph_json(graph),
            checksum=graph_checksum(graph),
        )
        assert await store.publish(definition.id, 1) is not None
        assert await store.set_enabled(definition.id, True) is not None
    assert await store.update_settings(stacked.id, multi_instance=True) is not None

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler) == 2
        assert scheduler.get(f"wf-{plain.id}-s").multi_instance is False  # 缺省单实例
        assert scheduler.get(f"wf-{stacked.id}-s").multi_instance is True
    finally:
        await engine.dispose()


async def test_load_published_workflows_registers_without_running_the_graph() -> None:
    """启动载入**只登记、不执行图**：下游节点一个都不许跑。

    回归：以前靠「跑一遍整张图」让开始节点顺带登记 cron，于是每次开机都把整条流程真的
    执行了一次（没到点也跑）。现在登记只调开始节点自己。
    """

    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import load_published_workflows

    ran: list[str] = []

    @register_node("probe")
    async def exec_probe(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(node.id)
        return {}

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [
            node("s", "trigger-time", cron="*/5 * * * *"),
            node("p", "probe"),
            node("e", "end"),
        ],
        "edges": [edge("s", "p"), edge("p", "e")],
    }
    definition = await store.create("u-admin", "带下游的定时流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert await store.publish(definition.id, 1) is not None
    assert await store.set_enabled(definition.id, True) is not None  # 开关拨开（否则连登记都不做）

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler) == 1
        assert scheduler.get(f"wf-{definition.id}-s") is not None  # 定时开始节点登记上了
        assert ran == []  # 而下游（probe）一次都没跑
    finally:
        await engine.dispose()


async def test_workflow_failure_log_has_stack_type_and_node() -> None:
    """执行失败时日志带**堆栈 / 异常类型 / 哪个节点**：只记 ``str(exc)`` 会是空串，排不了错。"""
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import run_published_workflow

    @register_node("boom-env-log")
    async def exec_boom(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise ValueError("")  # 消息为空：正是「排不了错」的那种

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()
    graph = {
        "nodes": [node("s", "trigger-message"), node("b", "boom-env-log"), node("e", "end")],
        "edges": [edge("s", "b"), edge("b", "e")],
    }
    definition = await store.create("u-admin", "会炸的流")
    await store.add_version(
        definition, graph_json=canonical_graph_json(graph), checksum=graph_checksum(graph)
    )

    try:
        async with runtime_logs() as records:
            await run_published_workflow(definition.id, 1, store, TaskManager())
            await wait_for_records(records, count=1)

        failures = [r for r in records if r.message == "工作流执行失败"]
        assert failures, [r.message for r in records]
        record = failures[0]
        assert record.extra["error_type"] == "ValueError"  # 取**最内层**原因，不是外层包装
        assert record.extra["node_id"] == "b"
        assert record.extra["node_type"] == "boom-env-log"
        assert record.extra["error"]  # 消息非空（str(exc) 空时用 repr 兜底）
        assert record.exc_text and "ValueError" in record.exc_text  # 意外异常：堆栈照留
    finally:
        await engine.dispose()


async def test_expected_environment_failure_logs_one_line(
    fake_http: type[FakeAsyncClient],
) -> None:
    """可预期的环境问题（超时 / 连不上）：日志**只记一行**、不铺 httpx 那几十行堆栈。

    ``error_type`` / ``error`` 都取**最内层**原因（``ReadTimeout``），不是外层包装的名字 ——
    排错要看的是「到底哪一步不通、为什么」。
    """
    import httpx

    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import run_published_workflow

    fake_http.error = httpx.ReadTimeout("")

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()
    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("h", "http", url="https://v1.hitokoto.cn/", method="GET", timeout=20),
            node("e", "end"),
        ],
        "edges": [edge("s", "h"), edge("h", "e")],
    }
    definition = await store.create("u-admin", "会超时的流")
    await store.add_version(
        definition, graph_json=canonical_graph_json(graph), checksum=graph_checksum(graph)
    )

    try:
        async with runtime_logs() as records:
            await run_published_workflow(definition.id, 1, store, TaskManager())
            await wait_for_records(records, count=1)

        failures = [r for r in records if r.message == "工作流执行失败"]
        assert failures, [r.message for r in records]
        record = failures[0]
        assert record.exc_text in (None, "")  # 不铺堆栈
        assert record.extra["error_type"] == "ReadTimeout"
        assert record.extra["node_id"] == "h" and record.extra["node_type"] == "http"
        error = str(record.extra["error"])
        assert "v1.hitokoto.cn" in error and "timeout=20.0s" in error  # 一行说清
    finally:
        await engine.dispose()


async def test_make_trigger_injects_gateway_into_the_workflow_context() -> None:
    """到点直接执行这条路：``make_trigger(gateway=...)`` 透传到 ctx（含归属 owner_id）。

    ``gateway``（平台总线）在这一层就得带上 —— 调度器到点执行的是登记时构造的闭包本身，
    send 节点到点那一趟也要能拿得到它发动作。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import make_trigger

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("t", "pack-onebot", chat="private", chat_id="10001"),
            node("snd", "send", message="到点提醒"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "t"),
            edge("t", "snd"),
            edge("t", "snd", source_port="target", target_port="target"),
            edge("snd", "e"),
        ],
    }
    definition = await store.create("u-admin", "发私聊的流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert await store.publish(definition.id, 1) is not None

    gateway = _FakeGateway()  # 归属（owner_id）由 ctx 带来，对上定义表里的 owner_id
    scheduler = TaskManager()
    trigger = make_trigger(definition.id, 1, store, scheduler, gateway=gateway)
    try:
        await trigger()
        assert len(gateway.reply_calls) == 1
        reply_target, content = gateway.reply_calls[0]
        assert content == "到点提醒"
        assert getattr(reply_target, "platform", "") == "onebot"
        assert getattr(reply_target, "user_id", "") == "10001"
    finally:
        await engine.dispose()


async def test_registered_cron_carries_gateway_through_to_the_connection() -> None:
    """登记链路：带上的平台总线跟着到点闭包走到连接上（到点执行的是登记时那个闭包）。

    调度器到点执行的是**登记那一趟构造的闭包**（任务的 ``func``）—— 总线必须从
    ``register_published_workflow`` 就传下去；这里手动调 ``func`` 模拟「到点」。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import register_published_workflow

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [
            node("s", "trigger-time", cron="*/5 * * * *"),
            node("t", "pack-onebot", chat="group", chat_id="9"),
            node("snd", "send", message="到点了"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "t"),
            edge("t", "snd"),
            edge("t", "snd", source_port="target", target_port="target"),
            edge("snd", "e"),
        ],
    }
    definition = await store.create("u-admin", "定时播报流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert await store.publish(definition.id, 1) is not None

    gateway = _FakeGateway()
    scheduler = TaskManager()
    try:
        primed = await register_published_workflow(
            definition.id, 1, store, scheduler, gateway=gateway
        )
        assert primed == 1
        assert gateway.reply_calls == []  # 登记只给 start 点名，不碰连接

        # 模拟「到点」：调度器到点执行的就是登记那一趟构造的闭包（Task.func 是协程）
        func: Any = scheduler.get(f"wf-{definition.id}-s").func
        await func()
        assert len(gateway.reply_calls) == 1
        reply_target, content = gateway.reply_calls[0]
        assert content == "到点了"
        assert getattr(reply_target, "platform", "") == "onebot"
        assert getattr(reply_target, "chat", "") == "group"
        assert getattr(reply_target, "chat_id", "") == "9"
    finally:
        await engine.dispose()


async def test_stop_published_workflow_removes_the_registered_tasks() -> None:
    """停用：把这一版登记的定时任务摘掉（**不跑图**），重复停、停不存在的都无害。"""
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import (
        register_published_workflow,
        stop_published_workflow,
    )

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    definition = await store.create("u-admin", "定时流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert await store.publish(definition.id, 1) is not None

    scheduler = TaskManager()
    try:
        assert await register_published_workflow(definition.id, 1, store, scheduler) == 1
        assert scheduler.get(f"wf-{definition.id}-s") is not None

        assert await stop_published_workflow(definition.id, 1, store, scheduler) == 1
        assert scheduler.list() == []  # 摘干净了
        # 再停一次 / 停一个不存在的版本：都是 0，不抛
        assert await stop_published_workflow(definition.id, 1, store, scheduler) == 0
        assert await stop_published_workflow(definition.id, 9, store, scheduler) == 0
    finally:
        await engine.dispose()


async def test_workflow_task_ids_carry_the_workflow_id() -> None:
    """任务名 = 工作流 + 节点：两条工作流的**同名**开始节点不会互相顶掉。

    以前只按节点 id 算（``wf-<node.id>``），而节点 id 只在**一张图内**唯一 —— 两条图都有
    ``s`` 时，后登记的会把先登记的那条移除，等于悄悄停掉别人的定时。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    first = await store.create("u-admin", "第一条")
    second = await store.create("u-admin", "第二条")  # 故意用同一个节点 id
    for definition in (first, second):
        await store.add_version(
            definition,
            graph_json=canonical_graph_json(graph),
            checksum=graph_checksum(graph),
        )
        assert await store.publish(definition.id, 1) is not None
        assert await store.set_enabled(definition.id, True) is not None

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler) == 2
        task_ids = sorted(task.task_id for task in scheduler.list())
        assert task_ids == sorted([f"wf-{first.id}-s", f"wf-{second.id}-s"])
    finally:
        await engine.dispose()


# ------------------------------------------------------------- 消息触发（P3-1）
async def test_run_published_workflow_injects_trigger_data_and_user_id() -> None:
    """消息触发这一趟：``trigger_data`` 进 start 的 message 端口，``user_id`` 进 ctx.user_id。

    定时触发（缺省）不传这俩 —— message 端口拿空串、user_id 是空串（``NO_USER_ID``），
    行为与现在完全一致。这里用一张 ``start(message) -> log`` 的图，log 节点把 start 送下来的
    message 写进日志，断言它拿到了消息内容。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import run_published_workflow

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [
            node("s", "trigger-message"),
            node("l", "log", level="INFO"),
            node("e", "end"),
        ],
        "edges": [edge("s", "l", "message", "message"), edge("l", "e")],
    }
    definition = await store.create("u-admin", "消息回显流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )

    scheduler = TaskManager()
    try:
        async with runtime_logs() as collected:
            await run_published_workflow(
                definition.id,
                1,
                store,
                scheduler,
                trigger_data={"message": "你好"},
                user_id="10001",
            )
            await wait_for_records(collected, count=3)  # 开始 + log + 完成
            # log 节点把 start 送下来的消息写了出来
            assert any("你好" in record.message for record in collected)
    finally:
        await engine.dispose()


async def test_message_trigger_passes_target_through_to_downstream() -> None:
    """消息触发器把装配层放进 trigger_data 的会话定位（ChatTarget）原样透出 target 出口。

    target 出口是**数据流**：它把它送下去，下游节点（send 节点）拿到同一个
    对象直接可用。没装配（离线跑 / 没造事件）就 None，由下游自己处理分支。
    """
    from tickneko.workflow import WorkflowNode
    from tickneko.workflow.nodes.triggers import exec_trigger_message

    class FakeTarget:  # 鸭子形状：workflow 只认「有 platform 的东西」，不 import bridge
        platform = "onebot"

    start_node = WorkflowNode.model_validate({"id": "s", "type": "trigger-message", "config": {}})
    fake_target = FakeTarget()
    ctx = NodeExecutionContext()
    ctx.trigger_data = {"message": "你好", "target": fake_target}
    outputs = await exec_trigger_message(start_node, ctx)
    assert outputs["message"] == "你好"
    assert outputs["target"] is fake_target  # 同一个对象，原样透出

    # 没装配 target 时是 None，不炸
    empty = NodeExecutionContext()
    empty.trigger_data = {"message": "hi"}
    assert (await exec_trigger_message(start_node, empty))["target"] is None


async def test_message_router_dispatches_by_owner_and_isolates_failures() -> None:
    """消息路由：按 owner_id 找匹配工作流、逐个跑，单个失败不淹其它。

    路由键 = owner_id（机器人归属），发消息的人是 ``user_id``。这里直接验证：
    * ``register`` 登记（重复登记覆盖版本）、``unregister`` 摘除；
    * ``dispatch`` 只跑该 owner 下登记过的工作流，别的 owner 不动；
    * 某个工作流抛异常不影响同 owner 的其它工作流（与 ``load_published_workflows`` 同口径）。
    """
    from tickneko.workflow.runtime import MessageRouter

    ran: list[tuple[str, int, str, str]] = []  # (workflow_id, version, user_id, message)

    async def run(workflow_id: str, version: int, **kw: Any) -> None:
        if workflow_id == "boom":
            raise RuntimeError("坏了")
        ran.append((workflow_id, version, kw.get("user_id", ""), kw["trigger_data"]["message"]))

    router = MessageRouter()
    router.attach(run)

    router.register("a", 1, "u-admin")
    router.register("b", 2, "u-admin")
    router.register("c", 1, "u-robot")  # 别人家的，不该被触发
    router.register("boom", 1, "u-admin")  # 会抛的，不淹 a / b

    # boom 抛了不计数，返回的是「真正跑成」的条数：a / b 两条
    assert await router.dispatch("u-admin", trigger_data={"message": "hi", "user_id": "10001"}) == 2

    # a / b 拿到了 user_id 与 message；boom 抛了没淹 a / b
    assert ("a", 1, "10001", "hi") in ran
    assert ("b", 2, "10001", "hi") in ran
    assert all(entry[0] != "c" for entry in ran)  # u-robot 没被触发
    assert all(entry[0] != "boom" for entry in ran)

    # 摘除后不再跑
    router.unregister("a", "u-admin")
    ran.clear()
    assert await router.dispatch("u-admin", trigger_data={"message": "again"}) == 1
    assert all(entry[0] != "a" for entry in ran)


async def test_message_router_without_executor_drops_and_returns_zero() -> None:
    """没注入执行回调（``attach`` 没调）：dispatch 只记 warning、返回 0，不炸。"""
    from tickneko.workflow.runtime import MessageRouter

    router = MessageRouter()
    router.register("a", 1, "u-admin")
    assert await router.dispatch("u-admin", trigger_data={"message": "hi"}) == 0


async def test_register_published_workflow_registers_message_trigger() -> None:
    """登记链路：``trigger=message`` 的开始节点登记到消息路由，而不是跑执行器。

    与时间触发的对偶：``trigger=time`` 登记到调度器，``trigger=message`` 登记到消息路由
    （按 owner 路由）。登记那一趟只点名、不跑下游，消息路由里出现 ``{workflow_id: version}``。
    """
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import (
        MessageRouter,
        register_published_workflow,
        stop_published_workflow,
    )

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "trigger-message"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    definition = await store.create("u-admin", "消息流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert await store.publish(definition.id, 1) is not None

    scheduler = TaskManager()
    router = MessageRouter()
    try:
        primed = await register_published_workflow(
            definition.id, 1, store, scheduler, message_router=router
        )
        assert primed == 1
        # 消息路由里登记上了；调度器里没有（消息触发不走 cron）
        assert router.routes("u-admin") == {definition.id: 1}
        assert scheduler.list() == []

        # 停用：从消息路由摘除
        assert await stop_published_workflow(
            definition.id, 1, store, scheduler, message_router=router
        ) == 1
        assert router.routes("u-admin") == {}
    finally:
        await engine.dispose()


async def test_load_published_workflows_registers_message_triggers() -> None:
    """启动载入：开着开关的已发布**消息**流登记到消息路由（与定时流分走两条登记路）。"""
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import MessageRouter, load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    msg_graph = {
        "nodes": [node("s", "trigger-message"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    time_graph = {
        "nodes": [node("s", "trigger-time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    msg = await store.create("u-admin", "消息流")
    timed = await store.create("u-admin", "定时流")
    for definition, graph in ((msg, msg_graph), (timed, time_graph)):
        await store.add_version(
            definition,
            graph_json=canonical_graph_json(graph),
            checksum=graph_checksum(graph),
        )
        assert await store.publish(definition.id, 1) is not None
        assert await store.set_enabled(definition.id, True) is not None

    scheduler = TaskManager()
    router = MessageRouter()
    try:
        assert await load_published_workflows(store, scheduler, message_router=router) == 2
        # 消息流进了消息路由，定时流进了调度器
        assert router.routes("u-admin") == {msg.id: 1}
        assert [t.task_id for t in scheduler.list()] == [f"wf-{timed.id}-s"]
    finally:
        await engine.dispose()


async def test_on_platform_event_dispatches_message_to_router() -> None:
    """装配钩子：``kind=message`` 的事件拆成普通数据交给消息路由，非 message 事件不动。

    「PlatformEvent 拆成普通数据」这一步发生在 bootstrap 的 ``on_platform_event``；消息路由
    本身不 import bridge（依赖方向不破）。这里验证：message 事件带着 text / user_id / 平台
    字段进 ``trigger_data``，按 owner_id 路由；notice / meta 之类不触发。
    """
    import tickneko.bootstrap as bootstrap
    from tickneko.platforms.bridge.models import PlatformEvent
    from tickneko.workflow.runtime import MessageRouter

    dispatched: list[tuple[str, dict[str, str]]] = []  # (owner_id, trigger_data)

    async def run(workflow_id: str, version: int, **kw: Any) -> None:
        dispatched.append((kw["trigger_data"].get("owner_id", ""), kw["trigger_data"]))

    router = MessageRouter()
    router.attach(run)
    router.register("w1", 1, "u-admin")

    original = bootstrap._message_router
    bootstrap._message_router = router  # 换掉模块级那个（bootstrap 里 on_platform_event 读它）
    try:
        msg = PlatformEvent(
            platform="onebot",
            owner_id="u-admin",
            kind="message",
            chat="group",
            chat_id="123",
            user_id="10001",
            text="你好",
            message_id="42",
        )
        await bootstrap.on_platform_event(msg)
        assert dispatched == [
            (
                "",
                {
                    "message": "你好",
                    "user_id": "10001",
                    "platform": "onebot",
                    "chat": "group",
                    "chat_id": "123",
                    "message_id": "42",
                    "target": None,  # 这次没造会话定位，None
                },
            )
        ]

        # 非 message 事件不触发
        dispatched.clear()
        await bootstrap.on_platform_event(
            PlatformEvent(platform="onebot", owner_id="u-admin", kind="notice")
        )
        assert dispatched == []
    finally:
        bootstrap._message_router = original


async def test_api_owner_isolation_between_users() -> None:
    async with api_client(api_app()) as client:
        admin_token = await login(client, ADMIN)
        robot_token = await login(client, ROBOT)

        created = await client.post(
            "/api/workflows", headers=auth(admin_token), json={"name": "管理员的流"}
        )
        workflow_id = created.json()["data"]["id"]

        # 普通用户看不到管理员的：详情 404，列表里也没有
        forbidden = await client.get(
            f"/api/workflows/{workflow_id}", headers=auth(robot_token)
        )
        assert forbidden.status_code == 404
        robot_list = await client.get("/api/workflows", headers=auth(robot_token))
        assert robot_list.json()["data"] == []

        # 管理员默认看全部；也能用 owner_id 缩
        admin_list = await client.get("/api/workflows", headers=auth(admin_token))
        assert len(admin_list.json()["data"]) == 1
        scoped = await client.get(
            "/api/workflows?owner_id=u-robot", headers=auth(admin_token)
        )
        assert scoped.json()["data"] == []

        # 普通用户的 owner_id 过滤参数被忽略，强制只看自己
        sneak = await client.get(
            "/api/workflows?owner_id=u-admin", headers=auth(robot_token)
        )
        assert sneak.json()["data"] == []


async def test_api_requires_login() -> None:
    async with api_client(api_app()) as client:
        assert (await client.get("/api/workflows")).status_code == 401
        assert (await client.post("/api/workflows/validate", json={"graph": linear_graph()})
                ).status_code == 401


# --------------------------------------------------------------------------- 运行开关
def _timed_graph(cron: str = "*/5 * * * *") -> dict[str, object]:
    """一张最小的定时图（换 cron 就换 checksum，用来造第二个版本）。"""
    return {
        "nodes": [node("s", "trigger-time", cron=cron), node("e", "end")],
        "edges": [edge("s", "e")],
    }


async def test_api_enabled_switch_and_published_snapshot() -> None:
    """开关接口：新建默认关、发布 ≠ 运行、拨开即时登记、关掉即时摘掉；已发布的那一份读得到。

    这是这次改动的核心口径：**发布只挪指针**（不登记、不执行图），跑不跑由开关说了算。
    """
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "开关流"}
        )
        workflow_id = created.json()["data"]["id"]
        assert created.json()["data"]["enabled"] is False  # 新建就是「不跑」

        # 还没发布：拨开是 409（没东西可跑），而且一次都没碰触发器
        early = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        assert early.status_code == 409
        assert triggers.calls == []

        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200
        assert published.json()["data"]["enabled"] is False
        assert triggers.calls == []  # 发布不跑、也不登记

        # 「已发布的那一份」：开关状态 + 版本 + 图，一次看全
        snapshot = await client.get(
            f"/api/workflows/{workflow_id}/published", headers=auth(token)
        )
        assert snapshot.status_code == 200
        body = snapshot.json()["data"]
        assert body["workflow"]["published_version"] == 1
        assert body["workflow"]["enabled"] is False
        assert [item["id"] for item in body["version"]["graph"]["nodes"]] == ["s", "e"]

        # 拨开 → 即时按已发布版本登记
        turned_on = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        assert turned_on.status_code == 200
        assert turned_on.json()["data"]["enabled"] is True
        assert triggers.calls == [("start", workflow_id, 1)]

        # 关掉 → 即时摘掉
        turned_off = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": False},
        )
        assert turned_off.json()["data"]["enabled"] is False
        assert triggers.calls[-1] == ("stop", workflow_id, 1)


async def test_api_workflow_settings_apply_and_reregister() -> None:
    """设置接口：改「实例策略」落库并回在响应里；**已经在跑的**会即时按新设置重新登记。

    这里用假触发器记账 —— 验的是接口层在设置变化后有没有按已发布版本重新登记；
    「新设置真的传给了调度器」由运行时那条用例（真调度器）覆盖。
    """
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "设置流"}
        )
        workflow_id = created.json()["data"]["id"]
        assert created.json()["data"]["multi_instance"] is False  # 新建就是单实例

        # 还没发布 / 开关关着：只落库，不碰触发器
        saved = await client.put(
            f"/api/workflows/{workflow_id}/settings",
            headers=auth(token),
            json={"multi_instance": True},
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["multi_instance"] is True
        assert triggers.calls == []

        detail = await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert detail.json()["data"]["multi_instance"] is True  # 读回来也是新值

        # 发布 + 拨开开关：按已发布版本登记一次
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        assert triggers.calls == [("start", workflow_id, 1)]

        # 正在跑的时候改设置：即时按新设置重新登记一遍（登记幂等，同名任务被换掉）
        again = await client.put(
            f"/api/workflows/{workflow_id}/settings",
            headers=auth(token),
            json={"multi_instance": False},
        )
        assert again.json()["data"]["multi_instance"] is False
        assert triggers.calls == [("start", workflow_id, 1), ("start", workflow_id, 1)]


async def test_api_enabled_switch_and_snapshot_are_owner_scoped() -> None:
    """个人隔离：别人的开关与已发布快照都按「不存在」处理（同一个 404）。"""
    async with api_client(api_app()) as client:
        admin_token = await login(client, ADMIN)
        robot_token = await login(client, ROBOT)
        created = await client.post(
            "/api/workflows", headers=auth(admin_token), json={"name": "管理员的定时流"}
        )
        workflow_id = created.json()["data"]["id"]
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(admin_token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(admin_token), json={}
        )

        toggled = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(robot_token),
            json={"enabled": True},
        )
        snapshot = await client.get(
            f"/api/workflows/{workflow_id}/published", headers=auth(robot_token)
        )
    assert toggled.status_code == 404
    assert snapshot.status_code == 404


async def test_api_publishing_while_switch_off_does_not_register() -> None:
    """开关关着时发布：一次都不登记（发布 ≠ 运行）。"""
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "不跑的流"}
        )
        workflow_id = created.json()["data"]["id"]
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
    assert triggers.calls == []


async def test_api_publishing_again_while_enabled_re_registers() -> None:
    """开着开关时再发一版：按**新版本**重新登记一遍（别让线上还跑旧版的触发配置）。"""
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "改过定时的流"}
        )
        workflow_id = created.json()["data"]["id"]
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        # 第二版：把 cron 改掉（checksum 不同才会真的多一版）
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph("*/10 * * * *"), "note": "二版"},
        )
        again = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
    assert again.json()["data"]["published_version"] == 2
    # **先停旧版、再起新版**：任务名里带节点 id，新版要是改了开始节点，光靠登记时同名覆盖
    # 盖不住旧任务（那会留下一个永远没人摘的定时）
    assert triggers.calls == [
        ("start", workflow_id, 1),
        ("stop", workflow_id, 1),
        ("start", workflow_id, 2),
    ]


# ------------------------------------------------------- 事件触发（trigger-event）
def test_trigger_validators_check_their_own_config() -> None:
    """三个触发器各查各的：定时看 cron、事件看事件类型；都对就通过。"""
    from tickneko.workflow import get_spec
    from tickneko.workflow.nodes.triggers import validate_event_type, validate_time_cron

    # 定时：cron 必填 + 得合法
    assert [
        i.code
        for i in validate_time_cron(WorkflowNode(id="t", type="trigger-time", config={}))
    ] == ["MISSING_CONFIG"]
    assert [
        i.code
        for i in validate_time_cron(
            WorkflowNode(id="t", type="trigger-time", config={"cron": "不是 cron"})
        )
    ] == ["INVALID_CRON"]
    assert (
        validate_time_cron(
            WorkflowNode(id="t", type="trigger-time", config={"cron": "*/5 * * * *"})
        )
        == []
    )

    # 事件：event_type 必填 + 得是认得的
    assert [
        i.code for i in validate_event_type(WorkflowNode(id="e", type="trigger-event", config={}))
    ] == ["MISSING_CONFIG"]
    assert [
        i.code
        for i in validate_event_type(
            WorkflowNode(id="e", type="trigger-event", config={"event_type": "like"})
        )
    ] == ["INVALID_EVENT_TYPE"]
    assert (
        validate_event_type(
            WorkflowNode(id="e", type="trigger-event", config={"event_type": "friend"})
        )
        == []
    )

    # 消息触发器没有必填配置：注册时没挂校验器，图校验也不会因为它报错
    message = WorkflowNode(id="m", type="trigger-message", config={})
    assert get_spec("trigger-message") is not None and get_spec("trigger-message").validator is None
    assert message.config == {}


def test_event_trigger_defaults_to_any_event() -> None:
    """``event_type`` 有默认值 ``*``（任何事件）：没配过这张图也能过校验。

    画布上的坑在于「config 里还没这个键时下拉停在第一项，看着像已经选了任何事件、其实值是空的」
    （前端另补了「未选择」占位项）；后端这份默认值保证「没动过就不算缺必填」。
    """
    from tickneko.workflow import get_spec

    spec = get_spec("trigger-event")
    assert spec is not None
    field = spec.fields[0]
    assert field.name == "event_type" and field.default == "*"

    # 缺这个键的图照样过：默认值在校验前由 apply_config_defaults 补齐
    graph = {
        "nodes": [node("s", "trigger-event"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(graph).valid

    # 显式填了具体事件也照常过（默认值不覆盖用户填的值）
    picked = {
        "nodes": [node("s", "trigger-event", event_type="friend"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(picked).valid


@pytest.mark.asyncio
async def test_event_trigger_emits_event_data() -> None:
    """事件触发那一趟：事件类型 / 谁 / 哪个会话 / 事件带的文本，从出口送下去。"""
    from tickneko.workflow.nodes.triggers import exec_trigger_event

    ctx_ = NodeExecutionContext()
    ctx_.trigger_data = {
        "event_type": "friend",
        "user_id": "10001",
        "chat": "private",
        "chat_id": "10001",
        "text": "加个好友",
        "target": None,
    }
    out = await exec_trigger_event(
        WorkflowNode(id="s", type="trigger-event", config={"event_type": "friend"}),
        ctx_,
    )
    assert out["event_type"] == "friend"
    assert out["user_id"] == "10001"
    assert out["chat"] == "private"
    assert out["chat_id"] == "10001"
    assert out["text"] == "加个好友"


@pytest.mark.asyncio
async def test_event_router_matches_only_subscribed_types() -> None:
    """事件路由：只跑**订阅了这种事件**的工作流（``*`` = 任何事件都跑），不做前缀匹配。"""
    from tickneko.workflow.runtime import EventRouter

    router = EventRouter()
    calls: list[tuple[str, str]] = []

    async def run(workflow_id: str, version: int, **kw: object) -> None:
        data = kw.get("trigger_data") or {}
        calls.append((workflow_id, str(data.get("event_type", ""))))

    router.attach(run)
    router.register("friend-flow", 1, "u-admin", "friend")
    router.register("any-flow", 2, "u-admin", "*")  # 通配：任何事件都跑
    router.register("other-flow", 1, "u-2", "friend")

    assert await router.dispatch("u-admin", trigger_data={"event_type": "friend"}) == 2
    assert sorted(calls) == [("any-flow", "friend"), ("friend-flow", "friend")]

    calls.clear()
    # 没订阅这种类型：只有通配那条跑（订阅 friend 的不会被 group_increase 拉起来）
    assert await router.dispatch("u-admin", trigger_data={"event_type": "group_increase"}) == 1
    assert calls == [("any-flow", "group_increase")]
    # 别的归属不受影响
    assert await router.dispatch("u-x", trigger_data={"event_type": "friend"}) == 0


@pytest.mark.asyncio
async def test_event_without_type_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有事件类型的事件（如 Kook 的系统消息）不触发任何事件工作流 —— ``*`` 订阅也不该被拉起。"""
    from tickneko import bootstrap
    from tickneko.platforms.bridge.models import PlatformEvent
    from tickneko.workflow.runtime import EventRouter

    router = EventRouter()
    calls: list[str] = []

    async def run(workflow_id: str, version: int, **kw: object) -> None:
        calls.append(workflow_id)

    router.attach(run)
    router.register("any-flow", 1, "u-admin", "*")
    monkeypatch.setattr(bootstrap, "_event_router", router)

    await bootstrap._dispatch_event(
        PlatformEvent(platform="kook", owner_id="u-admin", kind="notice")
    )
    assert calls == []

    # 有事件类型才会分发（同一份路由）
    await bootstrap._dispatch_event(
        PlatformEvent(platform="onebot", owner_id="u-admin", kind="notice", event_type="poke")
    )
    assert calls == ["any-flow"]


@pytest.mark.asyncio
async def test_event_graph_validates_and_carries_event_data_downstream() -> None:
    """事件触发的图能过校验，并且事件数据（这里用 text）沿边送到下游节点。"""
    data = {
        "nodes": [
            node("s", "trigger-event", event_type="friend"),
            node("t", "test"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "t"),
            edge("s", "t", "text", "message"),  # 事件文本 -> 调试节点回显
            edge("t", "e"),
            edge("s", "e"),
        ],
    }
    report = validate_graph(data)
    assert report.valid, report.errors

    ctx_ = NodeExecutionContext()
    ctx_.trigger_data = {"event_type": "friend", "user_id": "10001", "text": "加个好友"}
    await SimpleWorkflowRunner().run(WorkflowGraph.model_validate(data), ctx_)
    assert any("加个好友" in line for line in ctx_.log)  # 调试节点把流过的那句话回显了


@pytest.mark.asyncio
async def test_register_and_stop_event_trigger() -> None:
    """登记那一趟：事件触发的开始节点登记到 EventRouter（不跑执行器）；停用整条摘掉。"""
    from tickneko.core.scheduler import TaskManager
    from tickneko.workflow.runtime import (
        EventRouter,
        register_published_workflow,
        stop_published_workflow,
    )

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()
    graph = {
        "nodes": [node("s", "trigger-event", event_type="friend"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    try:
        definition = await store.create("u-admin", "加好友自动打招呼")
        await store.add_version(
            definition,
            graph_json=canonical_graph_json(graph),
            checksum=graph_checksum(graph),
        )
        assert await store.publish(definition.id, 1) is not None

        router = EventRouter()
        scheduler = TaskManager()
        assert (
            await register_published_workflow(
                definition.id, 1, store, scheduler, event_router=router
            )
            == 1
        )
        assert router.routes_of("u-admin") == {definition.id: (1, "friend")}

        assert (
            await stop_published_workflow(definition.id, 1, store, scheduler, event_router=router)
            == 1
        )
        assert router.routes_of("u-admin") == {}
    finally:
        await engine.dispose()
