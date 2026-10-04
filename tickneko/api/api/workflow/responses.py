"""工作流接口的响应体：校验报告 / 工作流 / 版本 / 节点目录（套进统一响应壳 ``data`` 里）。"""
from __future__ import annotations

from typing import Any

from pydantic import Field

from ...common.models import _Frozen
from tickneko.workflow import (
    CATEGORY_LABELS,
    MISSING_DEFAULT,
    PORT_TYPES,
    ConfigField,
    NodeSpec,
    PortSpec,
    ValidationIssue,
    ValidationReport,
    WorkflowDefinitionRecord,
    WorkflowVersionRecord,
    get_spec,
    registered_types,
)


class ValidationIssueData(_Frozen):
    """一条校验错误（字段名与前端约定：nodeId 驼峰）。"""

    nodeId: str = ""
    code: str
    message: str
    suggestion: str = ""

    @classmethod
    def from_issue(cls, issue: ValidationIssue) -> ValidationIssueData:
        return cls(
            nodeId=issue.node_id,
            code=issue.code,
            message=issue.message,
            suggestion=issue.suggestion,
        )


class ValidationReportData(_Frozen):
    """校验结论：``valid=false`` 时带阶段与错误明细（HTTP 仍为 200——这是业务结果不是请求错误）。"""

    valid: bool
    stage: str | None = None
    errors: list[ValidationIssueData] = Field(default_factory=list)

    @classmethod
    def from_report(cls, report: ValidationReport) -> ValidationReportData:
        return cls(
            valid=report.valid,
            stage=report.stage,
            errors=[ValidationIssueData.from_issue(issue) for issue in report.errors],
        )


class NodePortData(_Frozen):
    """一个端口（画布上的圆点）：``id`` 就是 edge 的 ``source_port`` / ``target_port``。

    ``type`` 决定它传不传值（``message`` 传、``trigger`` 不传），``required`` 只对输入端口
    有意义：画布把没接线的必填入口标出来（后端也会在语义阶段报 ``INPUT_NOT_CONNECTED``）。
    ``tie`` 是**透传对**：指向同一节点另一侧的端口 id —— 两端生效类型永远一致（输入接什么、
    输出就是什么），画布据此让两端显示同一种类型、同色表示对应。
    """

    id: str
    type: str
    label: str
    required: bool = False
    tie: str = ""

    @classmethod
    def from_port(cls, port: PortSpec) -> NodePortData:
        return cls(
            id=port.id,
            type=port.type,
            label=port.label or port.id,
            required=port.required,
            tie=port.tie,
        )


class NodeFieldData(_Frozen):
    """``config`` 里的一个字段：画布照它渲染输入框 / 下拉，并按 ``default`` 补初始值。

    ``default`` 只在 ``has_default=true`` 时有意义 —— ``None`` 也是合法默认值，不能拿它当
    「没声明默认值」的标记（后端那边用 ``MISSING_DEFAULT`` 哨兵区分）。
    """

    name: str
    label: str
    required: bool
    has_default: bool
    default: Any = None
    options: list[str] | None = None
    #: 枚举项的**显示名**（值 -> 画布上显示的文字）：值仍是 ``options`` 里那个（它可能要跟
    #: 外部对上号，别改），中文只用来看着好懂 —— 没配的项前端直接显示值本身
    option_labels: dict[str, str] | None = None

    @classmethod
    def from_field(cls, field: ConfigField) -> NodeFieldData:
        has_default = field.default is not MISSING_DEFAULT
        return cls(
            name=field.name,
            label=field.label or field.name,
            required=field.required,
            has_default=has_default,
            default=field.default if has_default else None,
            options=list(field.options) if field.options else None,
            option_labels=dict(field.option_labels) or None,
        )


class NodeTypeData(_Frozen):
    """一种节点类型：画布的面板项 / 节点标题 / 端口 / 配置表单都从这里来。"""

    type: str
    label: str
    #: 画布配色（CSS 颜色值）；空串 = 注册时没配，前端用兜底色
    color: str = ""
    role: str
    order: int
    has_executor: bool
    #: 语义分类（画布面板按它分组）：trigger / constant / action / control / data / onebot / kook / end
    category: str = "data"
    min_outgoing: int
    max_outgoing: int | None = None
    inputs: list[NodePortData] = Field(default_factory=list)
    outputs: list[NodePortData] = Field(default_factory=list)
    fields: list[NodeFieldData] = Field(default_factory=list)

    @classmethod
    def from_spec(cls, spec: NodeSpec) -> NodeTypeData:
        return cls(
            type=spec.node_type,
            label=spec.label or spec.node_type,
            color=spec.color,
            role=spec.role,
            order=spec.order,
            has_executor=spec.executor is not None,
            category=spec.category,
            min_outgoing=spec.min_outgoing,
            max_outgoing=spec.max_outgoing,
            inputs=[NodePortData.from_port(port) for port in spec.inputs],
            outputs=[NodePortData.from_port(port) for port in spec.outputs],
            fields=[NodeFieldData.from_field(field) for field in spec.fields],
        )


class NodePortTypeData(_Frozen):
    """一种端口类型的展示信息：画布的端口配色 / 面板图例 / 数据流语义都从它来。

    ``data=true`` 表示沿边送值（message / target / list / dict / set）；``false`` 只表达
    先后（trigger）。前端**不再自己抄一份端口类型表** —— 加类型只改后端
    :data:`tickneko.workflow.PORT_TYPES`，画布自动跟着变。
    """

    type: str
    label: str
    color: str
    data: bool


class NodeCategoryData(_Frozen):
    """一种语义分类：画布面板的分组。**从节点注册自动收集**（见 ``from_registry``）：

    * ``name`` 是节点标的分类机器名；* ``label`` 是显示名（查 ``CATEGORY_LABELS``，查不到用原名）。
    * 顺序 = 面板顺序（按节点 ``order`` 排序后去重）—— 面板按它逐组展开。
    """

    name: str
    label: str


class NodeCatalogData(_Frozen):
    """节点目录：**后端注册了什么，画布就显示什么**（面板顺序按 ``order``）。"""

    nodes: list[NodeTypeData] = Field(default_factory=list)
    categories: list[NodeCategoryData] = Field(default_factory=list)
    port_types: list[NodePortTypeData] = Field(default_factory=list)

    @classmethod
    def from_registry(cls) -> NodeCatalogData:
        specs = [get_spec(node_type) for node_type in registered_types()]
        nodes = [NodeTypeData.from_spec(spec) for spec in specs if spec is not None]
        nodes.sort(key=lambda item: (item.order, item.type))
        # 分类自动收集：从排好序的节点里按出现顺序去重，画布面板的分组 = 这份清单
        categories: list[NodeCategoryData] = []
        seen: set[str] = set()
        for item in nodes:
            if item.category in seen:
                continue
            seen.add(item.category)
            categories.append(
                NodeCategoryData(
                    name=item.category,
                    label=CATEGORY_LABELS.get(item.category, item.category),
                )
            )
        port_types = [
            NodePortTypeData(
                type=item.type,
                label=item.label,
                color=item.color,
                data=item.data,
            )
            for item in PORT_TYPES.values()
        ]
        return cls(nodes=nodes, categories=categories, port_types=port_types)


class WorkflowData(_Frozen):
    """工作流定义（列表 / 详情 / 改名后都返回它）。"""

    id: str
    owner_id: str
    #: 归属那个 id 在用户表里的昵称（查不到就是空串）—— 管理员看的是全库，靠它认人
    owner_name: str = Field(default="", description="归属的昵称（空串 = 查不到 / 没设过）")
    name: str
    status: str
    current_version: int
    published_version: int
    #: 运行开关：已发布但它是 ``false`` 时不会跑（发布 ≠ 运行）
    enabled: bool = False
    #: 实例策略（工作流设置）：``false`` = 单实例（上次没跑完跳过本次），``true`` = 多实例（允许叠加）
    multi_instance: bool = False
    #: 暂存区最近保存时间（0 = 没暂存过）
    draft_updated_at: float = 0.0
    #: 编辑器当前指向：draft（暂存区）/ version（已提交版本）
    current_ref: str = "draft"
    created_at: float
    updated_at: float

    @classmethod
    def from_record(
        cls, record: WorkflowDefinitionRecord, *, owner_name: str = ""
    ) -> WorkflowData:
        """记录 -> 响应；``owner_name`` 由调用方查好传进来（本层不碰用户表）。"""
        return cls(
            id=record.id,
            owner_id=record.owner_id,
            owner_name=owner_name,
            name=record.name,
            status=record.status,
            current_version=record.current_version,
            published_version=record.published_version,
            enabled=record.enabled,
            multi_instance=record.multi_instance,
            draft_updated_at=record.draft_updated_at,
            current_ref=record.current_ref,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class WorkflowDraftData(_Frozen):
    """暂存区内容：没暂存过时 ``graph`` 为 None、``updated_at`` 为 0。"""

    graph: dict[str, Any] | None = None
    updated_at: float = 0.0

    @classmethod
    def from_record(cls, record: WorkflowDefinitionRecord) -> WorkflowDraftData:
        draft = record.draft_graph()
        return cls(
            graph=draft.model_dump(mode="json") if draft is not None else None,
            updated_at=record.draft_updated_at,
        )


class WorkflowVersionData(_Frozen):
    """一个版本快照：图以解析后的对象返回（不再让前端 parse 字符串）。"""

    id: str
    workflow_id: str
    owner_id: str
    version: int
    graph: dict[str, Any]
    checksum: str
    note: str
    created_at: float

    @classmethod
    def from_record(cls, record: WorkflowVersionRecord) -> WorkflowVersionData:
        return cls(
            id=record.id,
            workflow_id=record.workflow_id,
            owner_id=record.owner_id,
            version=record.version,
            graph=record.graph().model_dump(mode="json"),
            checksum=record.checksum,
            note=record.note,
            created_at=record.created_at,
        )


class PublishedWorkflowData(_Frozen):
    """已发布的那一份：工作流（含运行开关与发布指针）+ 已发布版本快照（图在里面）。

    前端「运行 / 停止」面板进来一次看全：开关开没开、发的是哪一版、那一版长什么样。
    """

    workflow: WorkflowData
    version: WorkflowVersionData


class SaveVersionResultData(_Frozen):
    """保存版本的结果：工作流最新状态 + 命中的版本 + 这次有没有真的产生新版本。"""

    workflow: WorkflowData
    version: WorkflowVersionData
    created: bool = Field(description="图内容与最新版本一致时为 false（去重，不新增版本）")
