"""三个**触发器**节点：图的起点，各等各的触发。

为什么分三个，而不是一个节点带「触发方式」下拉：

* 形态本来就不一样 —— 定时要 ``cron``、事件要 ``event_type``、消息什么都不用配，塞在一个
  节点里会让卡片形态随配置变（画布得特判挑端口、属性面板会出现「跟当前触发方式无关的字段」）；
* **机器名把语义写死在图里**：``trigger-time`` 一眼看出这张图是定时的，不用先读 config；
* 老图里 ``config.trigger`` 这种「一个字段切换身份」的写法随之消失（迁移见文末）。

三个节点（都 ``role="start"``）：

===============  ==============  ==========================  ===================================
节点类型          面板名           什么时候跑                   登记到哪
===============  ==============  ==========================  ===================================
trigger-message  消息触发         消息事件进来                  MessageRouter（按归属路由）
trigger-time     定时触发         cron 到点                     调度器（TaskManager）
trigger-event    事件触发         平台事件进来（加好友 / 进群 /    EventRouter（按归属 + 订阅类型）
                                  撤回 / 戳一戳…）
===============  ==============  ==========================  ===================================

三种都**只在「登记那一趟」**（拨运行开关 / 启动载入 / 发布新版）登记触发，整图执行那一趟不碰
登记表 —— 定时任务在调度器里排着（调度器派发前会重排下一次），消息 / 事件由各自的路由派发。

config：

    trigger-time  : ``cron``（必填，5 / 6 段表达式）、``name``（调度任务显示名，可选）
    trigger-event : ``event_type``（必填，见 :data:`EVENT_TYPE_OPTIONS`；``*`` = 任何事件）
    trigger-message: 无

输出端口：

    trigger-message : ``trigger`` / ``message``（那条消息）/ ``target``（会话定位）
    trigger-time    : ``trigger``
    trigger-event   : ``trigger`` / ``event_type`` / ``user_id`` / ``chat`` / ``chat_id`` /
                      ``text``（事件带的文本）/ ``target``（会话定位）

**老图迁移**：以前是 ``start`` 节点 + ``config.trigger`` 下拉。现在 ``start`` 不再注册 ——
含 ``start`` 的图会报 ``UNKNOWN_NODE_TYPE``，校验器会提示该换成哪个触发器（见
``validator.py`` 里的旧类型提示）。
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec
from .registry import register_node

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

#: 事件类型在画布下拉里的**显示名**（值 -> 中文）。**值一个字符都不改** —— 它要跟平台上报的
#: ``event_type`` 全等匹配，中文只用来「看着好懂」，不参与匹配（见 :class:`ConfigField`
#: 的 ``option_labels``）；键集合与 :data:`EVENT_TYPE_OPTIONS` 一致。
EVENT_TYPE_LABELS: dict[str, str] = {
    "*": "任何事件",
    "friend": "加好友请求",
    "group": "加群 / 被邀请入群",
    "group_increase": "有人进群",
    "group_decrease": "有人退群 / 被踢",
    "group_ban": "群禁言",
    "group_recall": "群消息被撤回",
    "friend_recall": "私聊消息被撤回",
    "group_upload": "群文件上传",
    "friend_add": "好友添加成功",
    "notify": "戳一戳 / 群荣誉",
}


# --------------------------------------------------------------------------- 校验
def validate_time_cron(node: WorkflowNode) -> list[ValidationIssue]:
    """``trigger-time`` 的配置：cron 必填且合法。"""
    cron = node.config.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        return [
            ValidationIssue(
                node_id=node.id,
                code="MISSING_CONFIG",
                message=f"定时的开始节点 {node.id} 缺少必填配置 cron（cron 表达式）",
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
                message=f"定时的开始节点 {node.id} 的 cron 表达式不合法：{exc}",
                suggestion="cron 用 5 段（分 时 日 月 周）或 6 段（秒 分 时 日 月 周），如 */5 * * * *",
            )
        ]
    return []


def validate_event_type(node: WorkflowNode) -> list[ValidationIssue]:
    """``trigger-event`` 的配置：event_type 必填且是认得的事件类型。"""
    event_type = node.config.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        return [
            ValidationIssue(
                node_id=node.id,
                code="MISSING_CONFIG",
                message=f"事件触发的开始节点 {node.id} 缺少必填配置 event_type（订阅哪种事件）",
                suggestion="在 config.event_type 里填事件类型，如 friend（加好友请求）；填 * 表示任何事件都触发",
            )
        ]
    if event_type not in EVENT_TYPES:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_EVENT_TYPE",
                message=f"事件触发的开始节点 {node.id} 订阅的事件类型 {event_type!r} 不认得",
                suggestion="可选："
                + " / ".join(
                    f"{value}（{EVENT_TYPE_LABELS.get(value, value)}）"
                    for value in EVENT_TYPE_OPTIONS
                ),
            )
        ]
    return []


# --------------------------------------------------------------------------- 消息触发
@register_node(
    "trigger-message",
    label="消息触发",
    color="#22c55e",
    order=10,
    role="start",
    category="trigger",
    outputs=[
        TRIGGER_PORT,
        PortSpec("message", "message", "消息"),
        PortSpec("target", "target", "会话定位"),
    ],
)
async def exec_trigger_message(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """消息触发的消息是「外面送进来的」：从 ``ctx.trigger_data`` 原样送下两个出口。

    ``target`` 出口带会话定位（装配层放进去的 ChatTarget），给 send 节点**回复触发它的
    会话**用；装配层没放（离线跑 / 没造事件）就是 ``None``，下游自己处理「没有 target」。
    """
    message = ctx.trigger_data.get("message", "")
    target = ctx.trigger_data.get("target")
    ctx.log.append(f"[trigger-message] {node.id} 流程开始（消息触发）")
    ctx.logger.info("工作流开始（消息触发，等待消息进入）", node_id=node.id)
    return {"message": message, "target": target}


# --------------------------------------------------------------------------- 定时触发
@register_node(
    "trigger-time",
    label="定时触发",
    color="#f59e0b",
    order=11,
    role="start",
    category="trigger",
    outputs=[TRIGGER_PORT],
    fields=[
        # 手填 5 段表达式容易写错：声明 editor="cron"，画布换成可视化选择器（前端只认标识、
        # 按字段挑控件，不认识节点类型）
        ConfigField("cron", "cron 表达式", editor="cron"),
        ConfigField("name", "调度任务名"),
    ],
    validator=validate_time_cron,
)
async def exec_trigger_time(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """定时触发：**只在「登记那一趟」**把整条流程按 cron 登记到调度器（见 :func:`_register_cron`）。"""
    return await _register_cron(node, ctx)


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
    """``trigger-time`` 的行为：**只在「登记那一趟」**把整条流程按 cron 登记到调度器。

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
        ctx.log.append(f"[trigger-time] {node.id}: 执行中，调度器自己排下一次（cron={cron}）")
        return {"scheduled": _is_registered(ctx, task_id), "task_id": task_id, "cron": cron}

    if ctx.scheduler is None:
        ctx.logger.warning(
            f"[trigger-time:{node.id}] 时间触发未注入调度器，跳过登记",
            node_id=node.id,
            cron=cron,
        )
        ctx.log.append(f"[trigger-time] {node.id}: 未注入调度器，cron={cron}")
        return {"scheduled": False, "task_id": task_id, "cron": cron}

    ctx.scheduler.remove(task_id)  # 幂等：同名旧任务先摘掉（不在就返回 False，不抛）

    async def _trigger() -> None:
        """到点回调：跑整条流程。"""
        ctx.logger.info(f"[trigger-time:{node.id}] cron 触发，开始执行工作流", node_id=node.id)
        await ctx.run_workflow()

    task = ctx.scheduler.add(
        cron,
        _trigger,
        task_id=task_id,
        name=name,
        description=f"工作流开始节点（定时触发）{node.id}",
        multi_instance=ctx.multi_instance,  # 工作流设置：单实例（缺省）/ 多实例
    )
    ctx.logger.info(
        f"[trigger-time:{node.id}] 已登记到调度器",
        node_id=node.id,
        cron=cron,
        task_id=task_id,
        next_run=str(task.next_run) if task.next_run else None,
    )
    ctx.log.append(f"[trigger-time] {node.id}: 已登记 cron={cron}, task_id={task_id}")
    return {"scheduled": True, "task_id": task_id, "cron": cron}


# --------------------------------------------------------------------------- 事件触发
@register_node(
    "trigger-event",
    label="事件触发",
    color="#a78bfa",
    order=12,
    role="start",
    category="trigger",
    outputs=[
        TRIGGER_PORT,
        PortSpec("event_type", "message", "事件类型"),
        PortSpec("user_id", "message", "对方"),
        PortSpec("chat", "message", "会话类型"),
        PortSpec("chat_id", "message", "会话号"),
        PortSpec("text", "message", "事件文本"),
        PortSpec("target", "target", "会话定位"),
    ],
    fields=[
        # 值是平台原生事件名（要跟上报的 event_type 全等匹配），下拉里显示中文（option_labels）。
        # 默认 `*`（任何事件）：新建的节点 config 里就带上它，别让「没动过这个下拉」变成
        # 「必填项没填」——默认值在校验前由 apply_config_defaults 补齐（缺键 / None 都补）
        ConfigField(
            "event_type",
            "事件类型",
            default="*",
            options=EVENT_TYPE_OPTIONS,
            option_labels=EVENT_TYPE_LABELS,
        ),
    ],
    validator=validate_event_type,
)
async def exec_trigger_event(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """``trigger-event``（执行那一趟）：把这一条事件的数据从出口送下去。

    事件不是消息，没有「正文」这回事 —— 送下去的是**事件本身的几样**：什么事件、谁、
    哪个会话、事件带的文本（加好友请求里的招呼语 / 验证信息）。``target`` 也带上
    （适配器构造的会话定位），方便「同意好友后回一句话」这类动作。

    登记不在节点里做（同消息触发）：由 :class:`~tickneko.workflow.runtime.EventRouter`
    在「登记那一趟」订阅，见 :func:`tickneko.workflow.runtime.register_published_workflow`。
    """
    data = ctx.trigger_data
    event_type = str(data.get("event_type", ""))
    ctx.log.append(f"[trigger-event] {node.id} 流程开始（事件触发：{event_type or '任意'}）")
    ctx.logger.info("工作流开始（事件触发）", node_id=node.id, event_type=event_type)
    return {
        "event_type": event_type,
        "user_id": str(data.get("user_id", "")),
        "chat": str(data.get("chat", "")),
        "chat_id": str(data.get("chat_id", "")),
        "text": str(data.get("text", "")),
        "target": data.get("target"),
    }
