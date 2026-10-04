"""开始节点：图的起点；``config.trigger`` 决定触发方式。

config:
    trigger:    ``message``（消息触发，缺省）/ ``time``（cron 定时触发）/ ``event``（事件触发）
    cron:       ``trigger=time`` 时必填，5 / 6 段 cron 表达式
    name:       调度任务显示名（可选，缺省用节点 id）
    event_type: ``trigger=event`` 时必填，订阅哪种事件（``*`` = 任何事件）

输出端口：

    ``message``  消息触发时外部送进来的那条消息（``ctx.trigger_data["message"]``，没有就空串）；
                 时间触发没有消息，所以那份图别把线接到 ``message`` 出口上
    ``event_type`` / ``user_id`` / ``chat`` / ``chat_id`` / ``text``
                 事件触发时那一条事件的数据（什么事件 / 谁 / 哪个会话 / 带的文本）；
                 消息与时间触发下这些出口没有意义，画布按 trigger 只显示该显示的几路

``time`` 不自己"到点执行"：把整条流程图登记到
:class:`~tickneko.core.scheduler.TaskManager`，由调度器按 cron 触发整条流程；``message`` 被动
等消息接入（登记到 :class:`~tickneko.workflow.runtime.MessageRouter`）；``event`` 等**平台事件**
（通知 / 请求，如加好友、进群、撤回、戳一戳）接入 —— 登记到
:class:`~tickneko.workflow.runtime.EventRouter`，按事件类型匹配。发布 / 试跑时只写一条开始日志。

事件触发认的是**平台原生事件类型名**（OneBot 的 ``notice_type`` / ``request_type``，
见 :data:`EVENT_TYPE_OPTIONS`）—— 适配器已经把它翻进
:attr:`~tickneko.platforms.bridge.models.PlatformEvent.event_type`，工作流不必下探原始报文。

**加 / 摘任务只在「登记那一趟」做**（拨运行开关 / 启动载入 / 发布新版，见
:attr:`NodeExecutionContext.register_triggers`）；整图执行（cron 到点）那一趟不碰调度器
—— 它在派发前就已经排好了下一次。

校验规则（trigger 枚举 / time 时 cron 必填且合法）在 :func:`validate_start_node` 里，
随注册一起挂进注册表，校验器框架代码不认识具体类型。
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec
from .registry import register_node

#: start 节点的触发方式，**顺序即画布下拉顺序**：message = 消息触发（缺省）；
#: time = cron 定时触发（需配 cron）；event = 事件触发（需配 event_type）
START_TRIGGER_ORDER: tuple[str, ...] = ("message", "time", "event")

#: 触发方式集合（校验用；与上面的顺序表同一份内容）
START_TRIGGERS: frozenset[str] = frozenset(START_TRIGGER_ORDER)

#: 事件触发订阅哪种事件，**顺序即画布下拉顺序**：``"*"`` = 任何事件都触发；其余是
#: **平台原生事件类型名**（OneBot 的 ``notice_type`` / ``request_type``），按它与
#: :attr:`~tickneko.platforms.bridge.models.PlatformEvent.event_type` 全等匹配。
#: 目前只有 OneBot 上报这类事件（Kook 适配器还只翻消息），所以清单按 OneBot 的口径列。
EVENT_TYPE_OPTIONS: tuple[str, ...] = (
    "*",  # 任何事件
    "friend",  # 加好友请求（request）
    "group",  # 加群 / 邀请入群请求（request）
    "group_increase",  # 有人进群
    "group_decrease",  # 有人退群 / 被踢
    "group_ban",  # 群禁言
    "group_recall",  # 群消息被撤回
    "friend_recall",  # 私聊消息被撤回
    "group_upload",  # 群文件上传
    "friend_add",  # 好友添加成功
    "notify",  # 戳一戳 / 群荣誉一类（sub_type 区分，要细分交给下游节点）
)

#: 事件类型集合（校验用；与上面的顺序表同一份内容）
EVENT_TYPES: frozenset[str] = frozenset(EVENT_TYPE_OPTIONS)


def validate_start_node(node: WorkflowNode) -> list[ValidationIssue]:
    """start 配置校验：trigger 只能是 message/time/event（缺省 message）；
    time 时 cron 必填且合法，event 时 event_type 必填且认得。"""
    trigger = node.config.get("trigger", "message")
    if not isinstance(trigger, str) or trigger not in START_TRIGGERS:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_TRIGGER",
                message=f"start 节点 {node.id} 的触发方式 {trigger!r} 不合法",
                suggestion="config.trigger 只能是 message（消息）/ time（定时）/ event（事件）",
            )
        ]
    if trigger == "time":
        return validate_time_cron(node)
    if trigger == "event":
        return validate_event_type(node)
    return []


def validate_event_type(node: WorkflowNode) -> list[ValidationIssue]:
    """``trigger=event`` 的配置：event_type 必填且是认得的事件类型。"""
    event_type = node.config.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        return [
            ValidationIssue(
                node_id=node.id,
                code="MISSING_CONFIG",
                message=f"start 节点 {node.id} 选择了事件触发，但缺少必填配置 event_type（订阅哪种事件）",
                suggestion="在 config.event_type 里填事件类型，如 friend（加好友请求）；填 * 表示任何事件都触发",
            )
        ]
    if event_type not in EVENT_TYPES:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_EVENT_TYPE",
                message=f"start 节点 {node.id} 订阅的事件类型 {event_type!r} 不认得",
                suggestion=f"可选：{' / '.join(EVENT_TYPE_OPTIONS)}",
            )
        ]
    return []


def validate_time_cron(node: WorkflowNode) -> list[ValidationIssue]:
    """``trigger=time`` 的配置：cron 必填且合法。"""
    cron = node.config.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        return [
            ValidationIssue(
                node_id=node.id,
                code="MISSING_CONFIG",
                message=f"start 节点 {node.id} 选择了时间触发，但缺少必填配置 cron（cron 表达式）",
                suggestion="在 config.cron 中补充 5/6 段 cron 表达式，如 */5 * * * *",
            )
        ]

    from tickneko.core.scheduler import CronExpr, CronError  # 局部导入避免循环依赖

    try:
        CronExpr.parse(cron.strip())
    except CronError as exc:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_CRON",
                message=f"start 节点 {node.id} 的 cron 表达式不合法：{exc}",
                suggestion="cron 用 5 段（分 时 日 月 周）或 6 段（秒 分 时 日 月 周），如 */5 * * * *",
            )
        ]
    return []


@register_node(
    "start",
    label="开始",
    color="#22c55e",
    order=10,
    role="start",
    category="trigger",
    # 端口按**缺省形态**（消息触发 = 触发 + 消息 + target）声明；时间触发只出触发端口，
    # 由画布按 config.trigger 切换 —— start 是唯一一个端口随配置变的类型，前端为它留了特判。
    outputs=[
        TRIGGER_PORT,
        PortSpec("message", "message", "消息"),
        PortSpec("target", "target", "会话定位"),
        # 事件触发（trigger=event）才有意义的几路：什么事件 / 谁 / 哪个会话 / 事件带的文本
        PortSpec("event_type", "message", "事件类型"),
        PortSpec("user_id", "message", "对方"),
        PortSpec("chat", "message", "会话类型"),
        PortSpec("chat_id", "message", "会话号"),
        PortSpec("text", "message", "事件文本"),
    ],
    fields=[
        ConfigField("trigger", "触发方式", default="message", options=START_TRIGGER_ORDER),
        # cron / name 只在 trigger=time 时有意义，event_type 只在 trigger=event 时有意义
        #（其余触发方式下既不校验也没用处），但仍然是这张图**认**的字段，所以照实声明
        # —— 画布拿到什么就渲染什么，不再自己猜。
        ConfigField("cron", "cron 表达式"),
        ConfigField("name", "调度任务名"),
        ConfigField("event_type", "事件类型", options=EVENT_TYPE_OPTIONS),
    ],
    validator=validate_start_node,
)
async def exec_start(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """开始节点：按 ``config.trigger`` 分流。"""
    trigger = str(node.config.get("trigger", "message"))
    if trigger == "time":
        return await _register_cron(node, ctx)
    if trigger == "event":
        return _event_payload(node, ctx)

    # 消息触发的消息是「外面送进来的」：调用方把它放在 ctx.trigger_data 里，这里原样从
    # message 出口送下去（消息源还没接，缺省就是空串）。target 出口同理：带会话定位
    # （ChatTarget）下去给 send 节点用（回复触发它的会话）；装配层没放（定时触发 / 离线跑）就是
    # None —— 下游节点自己处理「没有 target」的分支。
    message = ctx.trigger_data.get("message", "")
    target = ctx.trigger_data.get("target")
    ctx.log.append(f"[start] {node.id} 流程开始（消息触发）")
    ctx.logger.info("工作流开始（消息触发，等待消息进入）", node_id=node.id)
    return {"message": message, "target": target}


def _event_payload(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """``trigger=event``（执行那一趟）：把这一条事件的数据从出口送下去。

    事件不是消息，没有「正文」这回事 —— 送下去的是**事件本身的几样**：什么事件、谁、
    哪个会话、事件带的文本（加好友请求里的招呼语 / 验证信息）。``target`` 也带上
    （适配器构造的会话定位），方便「同意好友后回一句话」这类动作。

    登记不在节点里做（同消息触发）：由 :class:`~tickneko.workflow.runtime.EventRouter`
    在「登记那一趟」订阅，见 :func:`tickneko.workflow.runtime.register_published_workflow`。
    """
    data = ctx.trigger_data
    event_type = str(data.get("event_type", ""))
    ctx.log.append(f"[start] {node.id} 流程开始（事件触发：{event_type or '任意'}）")
    ctx.logger.info("工作流开始（事件触发）", node_id=node.id, event_type=event_type)
    return {
        "event_type": event_type,
        "user_id": str(data.get("user_id", "")),
        "chat": str(data.get("chat", "")),
        "chat_id": str(data.get("chat_id", "")),
        "text": str(data.get("text", "")),
        "target": data.get("target"),
    }


def workflow_task_id(workflow_id: str, node_id: str) -> str:
    """这条时间触发在调度器里的任务名：**工作流 + 节点**两级。

    只用节点 id 不够 —— 节点 id 只在**一张图内**唯一，两条工作流里都叫 ``s`` 的开始节点会
    互相顶掉（后登记的把先登记的移除）。带上工作流 id 之后，不同工作流、不同节点都不会撞。

    ``stop`` 那边照同一份口径算 id 去摘任务（见 :func:`tickneko.workflow.runtime.
    stop_published_workflow`），所以**改这里的形状两边要一起改**。
    """
    return f"wf-{workflow_id}-{node_id}"


def _is_registered(ctx: NodeExecutionContext, task_id: str) -> bool:
    """调度器里有没有这个任务（没注入调度器 / 没这个 id 都算没有）。"""
    if ctx.scheduler is None:
        return False
    try:
        ctx.scheduler.get(task_id)
    except KeyError:
        return False
    return True


async def _register_cron(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """``trigger=time`` 的行为：**只在「登记那一趟」**把整条流程按 cron 登记到调度器。

    两趟分得很清（见 :attr:`NodeExecutionContext.register_triggers`）：

    * **登记那一趟**（拨运行开关 / 启动载入 / 发布新版）：加任务，或把同名旧任务换成新定义。
      调度器没注入时只记日志、不实际登记（测试 / 离线场景）；
    * **执行那一趟**（cron 到点跑整条流程）：**不碰调度器** —— 任务在里面排着，而调度器在派发
      前就会重排下一次（``tickneko.core.scheduler.core.Scheduler._spawn``）。以前每次跑图都先摘
      再登记，等于每执行一次就新建一个任务对象：运行统计被清零，连「上一次还没跑完就跳过本次」
      的单实例保护也一并失效了。

    登记的 task_id 由 :func:`workflow_task_id` 定（``wf-<工作流 id>-<节点 id>``）。登记是幂等
    的：先移除同名旧任务再添加，改 cron / 改名字后重复登记不会残留旧任务。实例策略（单实例 /
    多实例）是**工作流设置**，经 ``ctx.multi_instance`` 传进来后交给调度器的 ``add``。
    """
    cron = str(node.config.get("cron", "")).strip()
    name = str(node.config.get("name", node.id))
    task_id = workflow_task_id(ctx.workflow_id, node.id)

    if not ctx.register_triggers:
        ctx.log.append(f"[start:time] {node.id}: 执行中，调度器自己排下一次（cron={cron}）")
        return {"scheduled": _is_registered(ctx, task_id), "task_id": task_id, "cron": cron}

    if ctx.scheduler is None:
        ctx.logger.warning(
            f"[start:{node.id}] 时间触发未注入调度器，跳过登记",
            node_id=node.id,
            cron=cron,
        )
        ctx.log.append(f"[start:time] {node.id}: 未注入调度器，cron={cron}")
        return {"scheduled": False, "task_id": task_id, "cron": cron}

    ctx.scheduler.remove(task_id)  # 幂等：同名旧任务先摘掉（不在就返回 False，不抛）

    async def _trigger() -> None:
        """到点回调：跑整条流程。"""
        ctx.logger.info(f"[start:{node.id}] cron 触发，开始执行工作流", node_id=node.id)
        await ctx.run_workflow()

    task = ctx.scheduler.add(
        cron,
        _trigger,
        task_id=task_id,
        name=name,
        description=f"工作流开始节点（时间触发）{node.id}",
        multi_instance=ctx.multi_instance,  # 工作流设置：单实例（缺省）/ 多实例
    )
    ctx.logger.info(
        f"[start:{node.id}] 已登记到调度器",
        node_id=node.id,
        cron=cron,
        task_id=task_id,
        next_run=str(task.next_run) if task.next_run else None,
    )
    ctx.log.append(f"[start:time] {node.id}: 已登记 cron={cron}, task_id={task_id}")
    return {"scheduled": True, "task_id": task_id, "cron": cron}
