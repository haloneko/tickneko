"""写一个节点要用到的东西：执行函数的形状 + 它的运行时上下文 + 端口契约 + 注册规格。

**数据沿连线走，没有全局变量**：一个节点从自己的**输入端口**拿到上游送来的值，把产出放在
**输出端口**上，边就是这两者之间的管道（edge 的 ``source_port`` / ``target_port``）。

**这一份是对外契约**：别人写自己的节点时只从这里（以及 :mod:`.registry`）import，
不需要碰框架里别的文件::

    from tickneko.workflow.nodes import (
        NodeExecutionContext, ConfigField, PortSpec, register_node, input_value,
    )

    @register_node(
        "dingtalk",
        # 输入端口：上游把消息接到 text 入口；required 表示「必须接线或手填」
        inputs=[PortSpec("text", "message", "消息内容", required=True)],
        outputs=[PortSpec("sent", "message", "是否发出")],
        # 同名字段 = 没接线时的手填兜底（连了线就用线上的值）
        fields=[ConfigField("text", "消息内容")],
    )
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        text = input_value(node, ctx, "text")
        ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
        return {"sent": True}      # 键 = 输出端口名，下游连哪根线就拿到哪个值

函数签名就是 :data:`NodeExecutor`：收「节点 + 上下文」，返回**本节点的产出**（键必须是
已声明的输出端口名；引擎按边把它投递给下游对应入口）。

**校验什么由注册方自己说了算**：必填字段 / 默认值通过 :class:`ConfigField` 声明，端口与
「入口必填」通过 :class:`PortSpec` 声明，表格化覆盖不了的规则（枚举、条件必填）写一个
:data:`NodeConfigValidator` 挂上来，不用改校验器框架代码。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from tickneko.core.cache import cache as process_cache
from tickneko.core.logger import BaseLogger, BoundLogger, ChildLogger
from tickneko.core.scheduler import TaskManager

from ..logging import workflow_logger
from ..models import ValidationIssue, WorkflowNode
from .port_types import PortType

#: 节点执行函数：(节点, 上下文) -> 本节点产出（键 = 已声明的输出端口名）
NodeExecutor = Callable[[WorkflowNode, "NodeExecutionContext"], Awaitable[dict[str, Any]]]


class NodeFailure(RuntimeError):
    """**业务失败**：节点自己判定「这一趟没做成」（算不出来、解析不出、对方回了错）。

    两类失败分开处理（见 ``docs/workflow/workflow.md`` 第 5.5 节）：

    * **业务失败** —— 抛本类：引擎**只停止它向下传播** —— 本节点不产出值、出边全部置死，
      下游整段跳过（``ctx.log`` 留 ``[skip]``），**别的分支与流程其余部分照常跑**，
      不留堆栈；
    * **环境问题**（连不上、没接线、依赖没装、没接总线）—— 抛 :class:`ConnectionError`
      / :class:`RuntimeError` 一类：整条流程中断并留下堆栈，看得见是没配好。
    """


class EnvironmentFailure(ConnectionError):
    """**可预期的环境问题**：连不上、超时、对端拒绝、DNS 失败这类「外面不通」的错。

    与普通异常一样会中断整条流程，区别只在日志：引擎知道这是可预期的，就只记**一行**
    （哪个节点 + 什么原因），不铺几十行底层堆栈 —— 超时这种错，httpx / httpcore 那一串
    帧没有任何信息增量。真正需要堆栈的是「代码 bug / 没配好」，那些照旧抛
    :class:`ValueError` / :class:`RuntimeError`。

    节点实现里：``raise EnvironmentFailure("...") from exc``，原异常链照留。
    """

#: 节点在图中的拓扑角色：start=唯一入口 / end=终点 / normal=普通节点
NodeRole = Literal["start", "end", "normal"]


#: 节点的语义分类：画布面板按它分组。**分类集合不在这里维护** —— 目录接口从节点注册里
#: 自动收集（见 ``NodeCatalogData.from_registry``）：哪个节点标了什么类，分类清单就是什么，
#: 加平台 / 加扩展分类只改「标分类的那个节点」，不用再动白名单或面板分组。
NodeCategory = str

#: 分类的显示名映射（机器名 -> 中文名）。这是**唯一**要维护的地方：目录接口把它随
#: ``categories`` 下发，画布面板按下发结果分组 —— 查不到名字的分类原样显示机器名。
CATEGORY_LABELS: dict[str, str] = {
    "trigger": "触发",
    "constant": "常量",
    "action": "动作",
    "control": "控制",
    "data": "数据",
    "ds_dict": "键值对",
    "ds_list": "列表",
    "onebot": "OneBot 平台",
    "kook": "Kook 平台",
    "end": "结束",
}

#: 节点配置校验器：收节点，返回校验问题列表（空列表 = 通过）
NodeConfigValidator = Callable[[WorkflowNode], list[ValidationIssue]]

#: 「字段没有声明默认值」的哨兵（None 也是合法默认值，不能拿 None 当缺省标记）
MISSING_DEFAULT: Any = object()

#: 上下文里「没有所属工作流」时的代号：离线跑 / 测试直接构造 ctx 的场合
NO_WORKFLOW_ID: str = "local"

#: 上下文里「这次执行不针对某个用户」时的代号：定时触发 / 离线跑 / 测试直接构造 ctx 的场合。
#: 与 :data:`NO_WORKFLOW_ID` 不同，这里用空串——空串同时也是缓存键、日志里「没有这个人」的
#: 自然写法，别给它一个看着像真 id 的值。
NO_USER_ID: str = ""


@dataclass(frozen=True)
class ConfigField:
    """节点 ``config`` 里的一个字段声明：必填规则与默认值在注册时定死。

    * ``required=True``：缺失 / None / 空白字符串 → 校验直接报 ``MISSING_CONFIG``；
    * 给了 ``default``：缺失（键不存在或值为 None）时由
      :func:`tickneko.workflow.validator.apply_config_defaults` 在保存版本时填默认值；
    * 两个都不给：纯可选字段，校验器不碰；
    * 给了 ``options``：这是**枚举**字段（画布渲染成下拉，顺序即显示顺序）。校验规则仍写在
      节点自己的 validator 里，这里只描述「有哪些可选值」；
    * 给了 ``option_labels``：枚举项的**显示名**（值 -> 画布上显示的文字）。值本身不改 ——
      它可能是写进图里要跟外部对上号的东西（事件类型要跟平台上报的 ``event_type`` 全等匹配、
      日志级别要原样交给日志库），中文只用来「看着好懂」，不参与匹配；没配显示名的项直接
      显示值本身；
    * 给了 ``editor``：这个字段用**专用编辑器**（``"cron"`` -> 可视化 cron 选择器）。画布按
      这个标识挑控件，**不认识节点类型** —— 加节点类型不用动前端；空串 = 通用渲染
      （有 ``options`` 就下拉、没有就输入框）。
    """

    name: str
    label: str = ""
    required: bool = False
    default: Any = MISSING_DEFAULT
    options: tuple[str, ...] | None = None
    option_labels: Mapping[str, str] = field(default_factory=dict[str, str])
    editor: str = ""


@dataclass(frozen=True)
class PortSpec:
    """节点一端的一个端口 —— **既是画布上的圆点，也是执行期的数据契约**。

    ``id`` 就是写进 edge 的 ``source_port`` / ``target_port`` 的那个值：

    * ``type="message"``（数据端口）：边**送值**。输出端口的值 = 执行函数返回值里同名的键；
      输入端口的值进 :attr:`NodeExecutionContext.inputs`，节点用 :func:`input_value` 取
      （同名的 :class:`ConfigField` 是「没接线时手填」的兜底）；
    * ``type="trigger"``（控制流端口）：边只表达「谁先谁后」，不送值。

    输入端口还多一个 ``required``：标了就必须**接上线或手填同名字段**，否则语义阶段报
    ``INPUT_NOT_CONNECTED``（输出端口忽略它）。

    :param id: 端口名（edge 两端引用的就是它；数据输出端口同时是产出值的键名）；
    :param type: 端口类型，连线两端必须同类；
    :param label: 显示名（缺省用 id）；
    :param required: 仅输入端口有效：必须接线（或同名字段手填了值）；
    :param tie: **透传对**（可选）：指向**同一节点另一侧**的端口 id，表示「输入输出是
        同一种类型」—— 输入端口的生效类型决定输出，反之亦然（placeholder 的透传口用它
        声明配对；泛型端口接什么类型，对端就跟着显示什么类型）。只有泛型端口有意义：
        非泛型端口带 ``tie`` 当场报错（见 :meth:`__post_init__`），对侧有没有这个端口由
        :class:`NodeSpec` 在注册时查。
    """

    id: str
    type: PortType = "trigger"
    label: str = ""
    required: bool = False
    tie: str = ""

    def __post_init__(self) -> None:
        """``tie`` 只对泛型端口有意义 —— 别的类型带上它说明写错了，声明时当场炸掉。

        指错端口 id 是**静默失效**（画布上只是两端颜色对不上，看不出原因），能在注册这一
        步拦住就别留到画布上猜。
        """
        if self.tie and self.type != "generic":
            raise ValueError(
                f"端口 {self.id!r} 声明了透传对 tie={self.tie!r}，"
                f"但它的类型不是 generic（{self.type!r}）—— 只有泛型端口能配对"
            )


#: 各节点通用的触发端口（出入口都叫「触发」）
TRIGGER_PORT = PortSpec("trigger", "trigger", "触发")


@dataclass(frozen=True)
class NodeSpec:
    """一种节点类型的完整注册规格：执行器 + 配置字段规则 + 拓扑约束。

    :param node_type: 类型名（节点 JSON 的 ``type``）；
    :param executor: 执行函数；声明了但执行器还没实现时为 None（跑到它才报暂无执行器）；
    :param fields: :class:`ConfigField` 清单，必填 / 默认值都从这里推导；
    :param validator: 自定义配置校验器（枚举、条件必填这类表格盖不住的规则）；
    :param role: 拓扑角色，start 全图唯一、end 至少一个可达；
    :param min_outgoing: 出边条数下限（如 condition 的「至少接一个出口」）；
    :param max_outgoing: 出边条数上限（end 为 0），None 不限；
    :param expression_field: 该字段内容要交图级表达式语法检查器过一遍；
    :param branching: 分流节点（如 condition）：执行后只让**选中端口的出边**保持活着，
        其余出口的边整段剪枝（对岸节点不执行，级联到它的下游）；普通节点永远 False；
    :param label: 显示名（画布面板项 / 节点标题），缺省用 ``node_type``；
    :param color: 画布配色（CSS 颜色值，如 ``"#3b82f6"``）；空串 = 没配，前端用兜底色。
        加节点类型**不需要改前端** —— 颜色跟其他展示信息一起从目录接口下发；
    :param order: 画布面板顺序（小的在前，内置节点从 10 起）；
    :param category: 语义分类（画布面板分组用，见 :data:`NodeCategory`）；
    :param inputs: 输入端口（画布左侧圆点；数据入口的值进 ``ctx.inputs``）；
    :param outputs: 输出端口（画布右侧圆点；执行函数返回值的键必须是这里的 id）。

    端口上声明的 ``tie``（透传对）在**注册这一步**就查对侧有没有那个端口（见
    :meth:`__post_init__`）：三个注册入口（``register_node`` / ``register_executor`` /
    ``declare_node_type``）都经过这里，不用各写一遍。
    """

    node_type: str
    executor: NodeExecutor | None = None
    fields: tuple[ConfigField, ...] = ()
    validator: NodeConfigValidator | None = None
    role: NodeRole = "normal"
    min_outgoing: int = 0
    max_outgoing: int | None = None
    expression_field: str | None = None
    branching: bool = False
    label: str = ""
    color: str = ""
    order: int = 100
    category: NodeCategory = "data"
    inputs: tuple[PortSpec, ...] = ()
    outputs: tuple[PortSpec, ...] = ()

    def __post_init__(self) -> None:
        """透传对 ``tie`` 必须**指向另一侧的某个真实端口** —— 写错 id 是静默失效
        （画布上表现为两端颜色对不上），注册时当场报错更好排查。"""
        for port, others, side in (
            *((p, self.outputs, "输出") for p in self.inputs),
            *((p, self.inputs, "输入") for p in self.outputs),
        ):
            if not port.tie:
                continue
            if not any(other.id == port.tie for other in others):
                raise ValueError(
                    f"{self.node_type} 的输入 / 输出端口 {port.id!r} 声明了透传对 "
                    f"tie={port.tie!r}，但{side}端口里没有这个 id"
                )

def input_value(
    node: WorkflowNode, ctx: NodeExecutionContext, name: str, default: Any = ""
) -> Any:
    """取某个数据入口的值：**线上送来的优先，没接线才用 config 里同名字段的手填值**。

    这是「字段名 = 端口名」那条约定的唯一实现处，节点不用自己判断有没有接线::

        text = input_value(node, ctx, "message", default="")

    :param node: 当前节点（手填兜底从它的 ``config`` 取）；
    :param ctx: 运行时上下文（``inputs`` 里是引擎按边投递进来的值）；
    :param name: 入口名（数据端口的 id，通常与同名的 :class:`ConfigField` 一致）；
    :param default: 既没接线、config 里也没有这个键时返回什么。
    """
    if name in ctx.inputs:
        return ctx.inputs[name]
    return node.config.get(name, default)


def cache_key(node: WorkflowNode, ctx: NodeExecutionContext, scope: str, key: str) -> str:
    """拼缓存键：**用前缀区分作用域**（账号级带归属 id、图级带图 id）。

    这是 cache 节点与 ds-*（数据结构）节点共用的键规则：``workflow`` 作用域落在
    ``workflow:graph:{workflow_id}:{key}``、``account`` 落在 ``workflow:acct:{owner_id}:{key}``。
    离线跑（没挂到具体图上）时图级前缀是 ``local``（:data:`NO_WORKFLOW_ID`）；账号级没有
    归属就当场抛 —— 不知道是谁的缓存不能瞎写。

    :raises ValueError: 账号级作用域但上下文不知道归属（``owner_id`` 为空）。
    """
    if scope == "account":
        if not ctx.owner_id:
            raise ValueError(
                f"节点 {node.id} 的作用域是账号级，但不知道归属（owner_id 为空）"
                "：离线跑请改用 workflow 作用域"
            )
        return f"workflow:acct:{ctx.owner_id}:{key}"
    return f"workflow:graph:{ctx.workflow_id}:{key}"


class NodeExecutionContext:
    """节点运行时上下文：本节点的入口值 + 日志 + 调度器 + 可用的服务（平台总线 / 缓存）。

    ``inputs`` 是**属性**不是入参：引擎每跑一个节点前，按指向它的边把上游产出投递进来
    （键 = 目标端口名）。要预置入口值（测试 / 手动跑）直接写 ``ctx.inputs["x"] = ...``。

    :param logger: 业务日志实例（log 节点写这里）。**缺省那份预先绑好了默认字段**
        （``workflow_id`` / ``owner_id`` / ``user_id``）：节点只管写自己那句话，每条日志
        自己就认得出是哪条工作流、谁的、给谁跑的（见 :meth:`tickneko.core.logger.BaseLogger.
        bind`）。构造时注入的实例同样绑一份；只有不带 ``bind`` 的鸭子形状实例才原样用；
    :param scheduler: 调度器（定时触发的节点把流程图登记到这里）；
    :param run: 触发整条流程的回调，cron 到点时调用；
    :param workflow_id: 这条图属于哪个工作流（时间触发登记任务时要它来保证任务名唯一，
        见 :func:`tickneko.workflow.nodes.triggers.workflow_task_id`）；离线跑 / 测试直接构造
        ctx 时是 :data:`NO_WORKFLOW_ID`；
    :param register_triggers: 本次是不是「登记触发」那一趟（拨运行开关 / 启动载入 / 发布新版
        走的都是这一趟）：定时触发的节点只有这时才去调度器加任务；整图执行（cron 到点
        跑整条流程）是 ``False`` —— 任务在调度器里排着，它自己会排下一次；
    :param multi_instance: 这条工作流的**实例策略**（工作流设置里的「单实例 / 多实例」，来自
        定义表，与图无关）：``False``（缺省，单实例）上一次还没跑完就跳过本次；``True``（多实例）
        到点就开新实例、允许叠加。只有登记那一趟用得上（交给调度器的 ``add``）；
    :param owner_id: 这条工作流**属于谁**（定义表的 ``owner_id``）：历史 onebot 节点按它挑
        「谁的」连接（连接在握手时由令牌定下归属，两边是同一套 id 空间），日志按它认主人；离线跑是空串；
    :param user_id: 这一趟**面向哪个用户**（消息触发时就是发消息那个人）：用来把「同一个
        工作流在不同人身上的那一份」区分开（按人记状态、按人回复、按人打日志）。它与
        ``owner_id`` 是两回事——``owner_id`` 是**工作流的主人**（账号），``user_id`` 是
        **被服务的对象**；定时触发没有「这个人」，是 :data:`NO_USER_ID`（空串）；
    :param gateway: 平台总线（鸭子形状：``async send(platform, owner_id, action, **params)``
        —— 即 ``tickneko.platforms.bridge.gateway.Gateway``）。装配层注入，没接时是 ``None``；
        ``send`` 节点靠它按平台路由发动作。
    :param cache: 缓存门面（鸭子形状：``async get(key) -> str | None`` /
        ``async set(key, value, ttl=None)`` —— 即 ``tickneko.core.cache.Cache``）。
        **缺省就是进程级那一个**（``tickneko.core.cache.cache``，主程序启动时已 ``start()``），
        测试 / 特殊场合可以注入自己的门面；``cache`` 节点靠它存取变量。
    """

    def __init__(
        self,
        *,
        logger: BaseLogger | None = None,
        scheduler: TaskManager | None = None,
        run: Callable[[], Awaitable[None]] | None = None,
        workflow_id: str = NO_WORKFLOW_ID,
        register_triggers: bool = False,
        multi_instance: bool = False,
        owner_id: str = "",
        user_id: str = NO_USER_ID,
        gateway: Any | None = None,
        cache: Any | None = None,
    ) -> None:
        self.inputs: dict[str, Any] = {}
        self.trigger_data: dict[str, Any] = {}  # 消息触发的入口数据（start 的 message 端口）
        self.log: list[str] = []  # 节点产出的文字日志（供测试 / 前端回显）
        self.workflow_id: str = workflow_id
        #: 这条工作流属于谁（历史 OneBot 节点按它对连接的「谁的」）；离线跑 / 没归属时是空串
        self.owner_id: str = owner_id
        #: 这一趟面向哪个用户（消息触发时是发消息的人）；定时触发 / 离线跑是 NO_USER_ID
        self.user_id: str = user_id
        #: 本次是不是「登记触发」那一趟（见类文档）；整图执行时为 ``False``
        self.register_triggers: bool = register_triggers
        #: 实例策略：多实例时到点就开新实例（见类文档）
        self.multi_instance: bool = multi_instance
        #: 平台总线（鸭子形状见类文档）；装配层没注入时是 ``None``，``send`` 节点靠它发动作
        self.gateway: Any | None = gateway
        #: 缓存门面（鸭子形状见类文档）；缺省落进程级单例（正式跑由主程序启动，见 bootstrap）
        self.cache: Any = cache if cache is not None else process_cache
        base: BaseLogger | ChildLogger | BoundLogger = (
            logger if logger is not None else workflow_logger()
        )
        # 日志**提前带好默认参数**：这一趟的身份（哪条工作流 / 谁的 / 给谁跑的）在构造上下文
        # 时就定了，之后每个节点写日志都自动带上，不用谁在调用点手抄一遍。三者都能 bind
        # （核心 / 层级节点 / 绑定视图），只有鸭子形状不带时才原样用。
        self._logger: BaseLogger | ChildLogger | BoundLogger = (
            base.bind(workflow_id=workflow_id, owner_id=owner_id, user_id=user_id)
            if isinstance(base, (BaseLogger, ChildLogger, BoundLogger))
            else base
        )
        self._scheduler: TaskManager | None = scheduler
        self._run: Callable[[], Awaitable[None]] | None = run

    @property
    def logger(self) -> BaseLogger | BoundLogger:
        """节点写业务日志用的实例：**默认字段已绑好**（工作流 / 归属 / 用户）。"""
        return self._logger

    @property
    def scheduler(self) -> TaskManager | None:
        return self._scheduler

    async def run_workflow(self) -> None:
        """cron 到点时触发整条流程的回调。"""
        if self._run is not None:
            await self._run()
