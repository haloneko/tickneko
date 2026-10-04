"""工作流编排的领域模型：图（节点 / 边）、落库记录、校验结果。

这一层**不认识 FastAPI、也不认识数据库**：图就是前端画布设的那份 JSON
（``{"nodes": [...], "edges": [...]}``），记录在各层之间流转用的是冻结模型。

节点类型（``type`` 枚举）与入库前校验的阶段约定见 :mod:`tickneko.workflow.validator`；
**数据值沿边流动**：边的 ``source_port`` / ``target_port`` 指向两端节点声明的端口
（见 :class:`tickneko.workflow.nodes.base.PortSpec`），节点不声明任何「变量名清单」。
设计要点见 ``docs/workflow/workflow.md`` 第 7.1 节。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- 图
#: 节点类型不在这里做字面量枚举：合法类型 = 节点注册表里已登记的类型
#: （见 :mod:`tickneko.workflow.nodes.registry`）。新增类型在自己的模块里注册即可，
#: 必填字段 / 默认值 / 专属校验都在注册时声明，不用改模型与校验器。

#: 工作流状态：草稿（可继续改）/ 已发布（published_version 指的那份可被执行器取用）
WorkflowStatus = Literal["draft", "published"]

#: 编辑器打开时「当前看哪份图」的指针：暂存区 / 已提交版本
#: 默认 draft；暂存保存后仍指向 draft，提交版本后切到 version
CurrentRef = Literal["draft", "version"]


class WorkflowNode(BaseModel):
    """画布上的一个节点。

    :param id: 节点 ID，**一张图内全局唯一**（边的 source/target 指的就是它）；
    :param type: 节点类型，取值为节点注册表中已登记的类型（见
        :mod:`tickneko.workflow.nodes.registry`）；
    :param config: 节点配置（各类型要什么由注册规格里的字段声明把）；
    :param x/y: 画布坐标（UI 字段）：**随图持久化**，但不参与 :func:`graph_checksum`
        ——只挪动节点位置不产生新版本。

    不标 frozen：config 是 dict，pydantic 给 frozen 模型生成的 ``__hash__`` 会把全部字段
    值拿去 hash，dict 不可哈希会让「把节点放进集合」类操作直接炸；图模型按可解析数据对待即可。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    #: 画布横坐标（前端布局用，缺省 None = 没存过位置）
    x: float | None = None
    #: 画布纵坐标
    y: float | None = None


class WorkflowEdge(BaseModel):
    """一条有向边：``source`` 的某个**输出端口**接到 ``target`` 的某个**输入端口**。

    * 两端端口必须同类（``trigger`` 是控制流、不送值；``message`` 送值），语义阶段会查；
    * 端口留空按 ``trigger`` 读（只表达先后的边，见 ``graph.DEFAULT_EDGE_PORT``）；
    * 入参同时认前端的驼峰写法 ``sourcePort`` / ``targetPort``。
    """

    model_config = ConfigDict(extra="ignore")

    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    source_port: str = Field(
        default="",
        validation_alias=AliasChoices("source_port", "sourcePort"),
    )
    target_port: str = Field(
        default="",
        validation_alias=AliasChoices("target_port", "targetPort"),
    )


class WorkflowGraph(BaseModel):
    """工作流图：入库前校验与版本快照装的都是它。"""

    nodes: list[WorkflowNode] = Field(min_length=1)
    edges: list[WorkflowEdge] = Field(default_factory=list)


class DraftNode(BaseModel):
    """暂存区节点：**编辑到一半的图也能存**——不做任何内容约束，额外字段（前端 UI
    数据等）一并保留；提交版本时才过 :class:`WorkflowGraph` 的完整校验。"""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    type: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    x: float | None = None
    y: float | None = None


class DraftEdge(BaseModel):
    """暂存区边：同样宽松，端口字段驼峰 / 下划线写法都认。"""

    model_config = ConfigDict(extra="allow")

    source: str = ""
    target: str = ""
    source_port: str = Field(
        default="",
        validation_alias=AliasChoices("source_port", "sourcePort"),
    )
    target_port: str = Field(
        default="",
        validation_alias=AliasChoices("target_port", "targetPort"),
    )


class DraftGraph(BaseModel):
    """暂存区图：允许 0 节点（新建还没拖节点也能存），顶层额外字段保留。"""

    model_config = ConfigDict(extra="allow")

    nodes: list[DraftNode] = Field(default_factory=list)
    edges: list[DraftEdge] = Field(default_factory=list)


# --------------------------------------------------------------------------- 校验结果
#: 校验阶段名（顺序即入库前的流水线顺序，dry_run 可选）
STAGE_STRUCTURE: str = "structure"
STAGE_TOPOLOGY: str = "topology"
STAGE_SEMANTIC: str = "semantic"
STAGE_DRY_RUN: str = "dry_run"


class ValidationIssue(BaseModel):
    """一条校验错误：定位（nodeId）+ 机器码 + 给人看的话 + 修改建议。

    与前端约定的错误结构一致：节点级问题带节点 ID，整图级问题（如没有 start）留空串。
    """

    model_config = ConfigDict(frozen=True)

    node_id: str = ""
    code: str
    message: str
    suggestion: str = ""


class ValidationReport(BaseModel):
    """一次校验的完整结论；``valid=True`` 时 ``stage`` / ``errors`` 都是空的。

    失败在哪个阶段（``stage``）就说明后面的阶段没再跑——流水线**短路**：
    结构都不对，拓扑 / 语义无从谈起。
    """

    model_config = ConfigDict(frozen=True)

    valid: bool
    stage: str | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def ok(cls) -> ValidationReport:
        """全过。"""
        return cls(valid=True)

    @classmethod
    def reject(cls, stage: str, errors: list[ValidationIssue]) -> ValidationReport:
        """卡在 ``stage``：带上该阶段收集到的错误（至少一条）。"""
        return cls(valid=False, stage=stage, errors=errors)


# --------------------------------------------------------------------------- 落库记录
class WorkflowDefinitionRecord(BaseModel):
    """工作流**定义**：一个工作流一行（元数据 + 当前 / 已发布版本指针 + 暂存区）。"""

    model_config = ConfigDict(frozen=True)

    id: str
    #: 归属用户 id；多用户隔离就靠查询时强制带它（管理员可跨归属，见路由层）
    owner_id: str
    name: str
    status: WorkflowStatus = "draft"
    #: 最近一次提交的版本号；还没提交过图就是 0
    current_version: int = 0
    #: 已发布的版本号；从没发布过就是 0
    published_version: int = 0
    #: **运行开关**：发布 ≠ 运行 —— 发布只挪指针，这里为 ``True`` 才会被调度器跑起来。
    #: 默认 ``False``（发布完是「已发布但不跑」，由接口层开关拨开）。
    enabled: bool = False
    #: **实例策略**（设置弹窗里的「单实例 / 多实例」）：``False``（缺省，单实例）上一次还没
    #: 跑完、到点就跳过本次；``True``（多实例）到点就开新实例、允许叠加。登记定时触发时交给
    #: 调度器（见 :func:`tickneko.workflow.nodes.triggers.exec_trigger_time`）。
    multi_instance: bool = False
    #: 暂存区图原文（规范 JSON 字符串）；从没暂存过是空串
    draft_graph_json: str = ""
    #: 暂存区最近保存时间（Unix 秒；0 = 没暂存过）
    draft_updated_at: float = 0.0
    #: 编辑器当前指向：暂存区 / 已提交版本（暂存后指 draft，提交后指 version）
    current_ref: CurrentRef = "draft"
    created_at: float
    updated_at: float

    def draft_graph(self) -> DraftGraph | None:
        """暂存区原文解析回 :class:`DraftGraph`；没暂存过返回 None。"""
        if not self.draft_graph_json:
            return None
        return DraftGraph.model_validate(json.loads(self.draft_graph_json))


class WorkflowVersionRecord(BaseModel):
    """工作流**版本**：每次保存一张不可变的图快照（同内容不产生新版本，见 checksum）。"""

    model_config = ConfigDict(frozen=True)

    id: str
    workflow_id: str
    #: 冗余归属：按归属拉版本列表 / 鉴权时少一次 join，与定义上的 owner_id 一致
    owner_id: str
    version: int
    #: 图快照原文（规范 JSON 字符串，:func:`canonical_graph_json` 的产出）
    graph_json: str
    #: graph_json 的 sha256：内容没变就不新增版本
    checksum: str
    note: str = ""
    created_at: float

    def graph(self) -> WorkflowGraph:
        """把快照原文解析回 :class:`WorkflowGraph`（入库前已校验过，这里不该再失败）。"""
        return WorkflowGraph.model_validate(json.loads(self.graph_json))


# --------------------------------------------------------------------------- JSON / 摘要
#: 不参与内容摘要的「纯 UI 字段」：节点坐标只影响布局，挪动节点不算图内容变更
_CHECKSUM_IGNORED_NODE_FIELDS: frozenset[str] = frozenset({"x", "y"})


def canonical_graph_json(graph: WorkflowGraph | dict[str, Any]) -> str:
    """把图序列化成**规范** JSON：键排序、无多余空白——快照存储的基准。

    传入 dict 时先过一遍 :class:`WorkflowGraph`（丢未知字段、统一形态），
    保证「同一张图不同写法」（键顺序、空格）归一。节点坐标 x/y **保留**在快照里
    （打开旧版本也要还原布局），但不参与 :func:`graph_checksum`。
    """
    normalized = graph if isinstance(graph, WorkflowGraph) else WorkflowGraph.model_validate(graph)
    # 当前 pydantic 的 model_dump_json 不认 sort_keys，统一 dump 成 dict 再走标准库；
    # ensure_ascii=False：中文配置直接进摘要；sort_keys 让键顺序不影响结果
    return json.dumps(
        normalized.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )


def canonical_draft_json(raw: dict[str, Any]) -> str:
    """把暂存图序列化成规范 JSON。

    与 :func:`canonical_graph_json` 的区别：走宽松的 :class:`DraftGraph`——
    编辑到一半（空节点列表、缺字段、前端额外 UI 数据）也能存，提交版本时才严格校验。
    """
    normalized = DraftGraph.model_validate(raw)
    return json.dumps(
        normalized.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )


def graph_checksum(graph: WorkflowGraph | dict[str, Any]) -> str:
    """图内容的 sha256（64 位十六进制）；版本去重 / 变更比对用它。

    摘要前剔除节点坐标等纯 UI 字段（:data:`_CHECKSUM_IGNORED_NODE_FIELDS`）：
    只挪节点位置、不改连线 / 配置时摘要不变，不产生垃圾版本。
    """
    normalized = graph if isinstance(graph, WorkflowGraph) else WorkflowGraph.model_validate(graph)
    payload = normalized.model_dump(mode="json")
    for node in payload.get("nodes", []):
        for field in _CHECKSUM_IGNORED_NODE_FIELDS:
            node.pop(field, None)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
