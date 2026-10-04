"""工作流运行时：把已发布版本的图加载出来，让开始节点的触发配置生效。

**发布 ≠ 运行**：发布接口只挪发布指针，要不要真的跑由定义上的**运行开关**（``enabled``）
决定，默认关着。启动时 :func:`load_published_workflows` 只挑开关开着的已发布流，把
``trigger=time`` 的开始节点按 cron 登记到调度器，整张图**不执行**；运行期间拨开关由
:class:`WorkflowTriggers` 即时启停。登记 / 停用只调开始节点自己（不跑图，见
:func:`register_published_workflow` / :func:`stop_published_workflow`）；调度器到点走
:func:`make_trigger` 重载版本图**跑整条流程**，这一趟**不碰调度器**
（``ctx.register_triggers=False``）。详见 ``docs/workflow/workflow.md`` 第 7.4 节。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from tickneko.core.logger import BaseLogger, BoundLogger
from tickneko.core.scheduler import TaskManager

from .executor import NodeExecutionContext, SimpleWorkflowRunner
from .graph import start_ids
from .logging import workflow_logger
from .models import WorkflowGraph
from .nodes import get_executor, get_spec, workflow_task_id

if TYPE_CHECKING:
    from .store import SqlWorkflowStore


def _log() -> BaseLogger | BoundLogger:
    """取本模块的日志实例：**用到才取**，不要在模块级取。

    模块级 ``_logger = workflow_logger()`` 是**导入即执行**的：谁先 import 这个模块，谁就
    顺手把进程默认日志核心按默认参数建出来（那时配置还没读），``[logging]`` 里的颜色 / 级别
    就此定死再也传不进去。核心改由装配层建好后经 :func:`tickneko.wiring.wire_loggers` 存进
    workflow 的日志槽位（:func:`workflow_logger` 未装配即抛错），这里只负责取用。
    """
    return workflow_logger("workflow.runtime")


def _log_run_failure(
    logger: BaseLogger | BoundLogger,
    message: str,
    exc: BaseException,
    **fields: object,
) -> None:
    """记一条执行失败：**可预期的环境问题**只记一行，别的异常才铺堆栈。

    固定带上三样排错信息：``node_id`` / ``node_type``（哪个节点）与 ``error_type`` /
    ``error``（什么错）。异常链是「引擎包装 → 节点抛的错 → 底层库的错」，所以这里拆开取：
    ``error_type`` 用**最内层**（到底是 httpx 超时还是 DNS 失败），``error`` 用节点那层
    （外层包装的消息只是把原因又包一遍，底层库的消息又常常是空串）。

    连不上 / 超时这类「外面不通」是预期内的（节点抛
    :class:`~tickneko.workflow.nodes.base.EnvironmentFailure`）：记堆栈只会把日志刷满，
    httpx / httpcore 那几十行帧没有信息增量。
    """
    cause = exc.__cause__ if exc.__cause__ is not None else exc  # 节点自己抛的那一层
    root = cause
    while root.__cause__ is not None:  # 追到最里头：ReadTimeout / ConnectError / DNS 失败…
        root = root.__cause__
    extra: dict[str, object] = {
        **fields,
        "node_id": getattr(exc, "node_id", ""),
        "node_type": getattr(exc, "node_type", ""),
        "error_type": type(root).__name__,
        "error": str(cause) or repr(cause),
    }
    if getattr(exc, "expected", False):
        logger.error(message, **extra)
    else:
        logger.exception(message, **extra)


def make_trigger(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    gateway: Any | None = None,
) -> Callable[[], Awaitable[None]]:
    """构造到点触发回调：重新加载版本图并执行整条流程。

    每次触发都重新加载该版本的图跑一遍。开始节点在这一趟**不再动调度器**（任务在它触发之前
    就已经排好了下一次），所以不会因为重复触发而在调度器里堆积任务。

    ``gateway``（平台总线，可选）在这里就得带上：调度器到点执行的是**这一趟构造的闭包**，
    错过这儿后面没机会再补（见 :func:`register_published_workflow`）。
    """

    async def trigger() -> None:
        await run_published_workflow(workflow_id, version, store, scheduler, gateway=gateway)

    return trigger


async def run_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    gateway: Any | None = None,
    trigger_data: Mapping[str, Any] | None = None,
    user_id: str = "",
) -> None:
    """加载指定版本的图并**执行整条流程**；到点 / 消息回调都走它。

    启动载入**不走这里** —— 那一步只登记触发、不执行图，见
    :func:`register_published_workflow`。``gateway``（平台总线）从这里注进 ``ctx.gateway``，
    ``send`` 节点靠它按平台路由发动作。

    ``trigger_data`` / ``user_id`` 是**消息触发**这一趟的入口：``trigger_data`` 进
    ``ctx.trigger_data``（start 的 message 端口从它取 ``message``），``user_id`` 是发消息
    的人（``ctx.user_id``）。定时触发没有这俩，留空即可 —— 与 ``ctx.user_id`` 的
    ``NO_USER_ID``（空串）口径一致。

    归属先读出来：这一趟的每条日志都挂在**这条流的主人**名下（与 ``ctx.owner_id`` 同一个
    出处），日志页里按人筛得到、也追得到责 —— 记成公共的话，谁的流在跑都看不出来。
    """
    # 归属（定义表的 owner_id）：日志按它认主人（与 ctx.owner_id 同一个出处）
    definition = await store.get(workflow_id)
    owner_id: str = definition.owner_id if definition is not None else ""
    log: BoundLogger = _log().bind(workflow_id=workflow_id, owner_id=owner_id)

    record = await store.get_version(workflow_id, version)
    if record is None:
        log.warning("工作流版本不存在，跳过执行", version=version)
        return

    graph = record.graph()
    # 到点回调：这个版本下次再到点，还是从这儿跑一遍（与本次同一个入口）
    trigger = make_trigger(workflow_id, version, store, scheduler, gateway=gateway)
    # 执行那一趟（register_triggers 缺省 False）：开始节点不碰调度器，它自己排下一次
    ctx = NodeExecutionContext(
        scheduler=scheduler,
        run=trigger,
        workflow_id=workflow_id,
        owner_id=owner_id,
        user_id=user_id,
        gateway=gateway,
    )
    if trigger_data is not None:
        ctx.trigger_data = dict(trigger_data)
    runner = SimpleWorkflowRunner()
    try:
        await runner.run(graph, ctx)
        log.info("工作流执行完成", version=version, node_count=len(graph.nodes))
    except Exception as exc:  # noqa: BLE001 — 执行引擎异常不能让发布接口挂掉
        _log_run_failure(log, "工作流执行失败", exc, version=version)


def _message_start_ids(graph: WorkflowGraph) -> set[str]:
    """图里 ``trigger=message`` 的开始节点 id 集合（图是 ``WorkflowGraph``）。

    消息触发与时间触发分走两条登记路：时间触发登记到调度器（cron），消息触发登记到
    :class:`MessageRouter`（按 owner 路由）。这里只负责认「哪些开始节点是消息触发」——
    依据是**节点类型** ``trigger-message``（见 :mod:`tickneko.workflow.nodes.triggers`）。
    """
    # 触发器拆成三个节点之后，触发方式由**节点类型**直接决定（不再看 config）
    return {node.id for node in graph.nodes if node.type == "trigger-message"}


def _event_start_ids(graph: WorkflowGraph) -> dict[str, str]:
    """图里**事件触发**的开始节点：``节点 id -> 订阅的事件类型``（``"*"`` = 任何事件）。

    与 :func:`_message_start_ids` 同一条依据（节点类型），订阅的事件类型一起带出来给
    :class:`EventRouter` 登记。
    """
    # 没填（校验会拦）就按「任何事件」处理，登记那一趟别因此崩掉
    return {
        node.id: str(node.config.get("event_type", "") or "*")
        for node in graph.nodes
        if node.type == "trigger-event"
    }


async def register_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    gateway: Any | None = None,
    message_router: MessageRouter | None = None,
    event_router: EventRouter | None = None,
) -> int:
    """**只跑开始节点、不跑下游**：把这一版的触发登记好，返回跑过的开始节点数量。

    时间触发的开始节点，执行器做的就是「按 cron 把整条流程登记到调度器」（见
    :func:`tickneko.workflow.nodes.triggers.exec_trigger_time`）；消息触发的登记到
    :class:`MessageRouter`（按 owner 路由，消息进来时跑）。两种都只处理**开始节点自己**，
    后面的节点一个都不跑 —— 启动载入不是执行。以前这里是「跑一遍整张图，靠开始节点顺带
    登记」，代价是每次开机都真的把整条流程执行一次（下游的 http / log 全都跟着跑了），
    而登记本身只是点个名。

    这是**登记那一趟**（``ctx.register_triggers=True``）：加 / 摘任务只在这儿发生；真正整图
    执行（cron 到点走 :func:`run_published_workflow`）那一趟不碰调度器，它自己会排下一次。

    ``gateway``（平台总线，可选）要在这里就带上：交给调度器的到点回调是**这一趟构造
    的**（``make_trigger`` 闭包），到点执行那一趟没机会再补。``message_router``（可选）
    同样要在这里就带上：消息触发的登记 / 摘除也发生在这一趟，错过就没人给它登记了。

    与执行那条路一个口径：归属先读出来，日志都挂在流的**主人**名下（``owner_id``），
    节点上下文也带同一份（见 :meth:`NodeExecutionContext.owner_id`）。
    """
    # 实例策略是**工作流级设置**（定义表里的列），与图无关：登记时读一次，由开始节点带给调度器
    definition = await store.get(workflow_id)
    owner_id: str = definition.owner_id if definition is not None else ""
    log: BoundLogger = _log().bind(workflow_id=workflow_id, owner_id=owner_id)

    record = await store.get_version(workflow_id, version)
    if record is None:
        log.warning("工作流版本不存在，跳过登记", version=version)
        return 0

    graph = record.graph()
    starts = set(start_ids(graph.nodes))
    message_starts = _message_start_ids(graph)
    event_starts = _event_start_ids(graph)
    # 登记时给的到点回调是「跑整条流程」那个（与到点触发同一条路）；
    # register_triggers=True：这才是「登记那一趟」，开始节点据此去调度器加 / 改任务
    ctx = NodeExecutionContext(
        scheduler=scheduler,
        run=make_trigger(workflow_id, version, store, scheduler, gateway=gateway),
        workflow_id=workflow_id,
        register_triggers=True,
        multi_instance=definition.multi_instance if definition is not None else False,
        owner_id=owner_id,
        gateway=gateway,
    )
    primed = 0
    for node in graph.nodes:
        if node.id not in starts:
            continue
        if node.id in message_starts:
            # 消息触发：登记到消息路由（按 owner 路由），不跑执行器（执行器只是写「等待消息」）
            if message_router is None:
                log.warning(
                    "消息触发的开始节点未注入消息路由，跳过登记",
                    version=version,
                    node_id=node.id,
                )
                continue
            message_router.register(workflow_id, version, owner_id)
            log.info("已登记消息触发", version=version, node_id=node.id)
            primed += 1
            continue
        if node.id in event_starts:
            # 事件触发：登记到事件路由（按 owner + 订阅的事件类型匹配），不跑执行器
            if event_router is None:
                log.warning(
                    "事件触发的开始节点未注入事件路由，跳过登记",
                    version=version,
                    node_id=node.id,
                )
                continue
            event_router.register(workflow_id, version, owner_id, event_starts[node.id])
            log.info(
                "已登记事件触发",
                version=version,
                node_id=node.id,
                event_type=event_starts[node.id],
            )
            primed += 1
            continue
        executor = get_executor(node.type)
        if executor is None:
            log.warning(
                "开始节点没有执行器，跳过载入",
                version=version,
                node_id=node.id,
                node_type=node.type,
            )
            continue
        await executor(node, ctx)
        primed += 1
    return primed


async def stop_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    message_router: MessageRouter | None = None,
    event_router: EventRouter | None = None,
) -> int:
    """把这一版里**开始节点登记过的触发**摘掉（定时任务 + 消息 / 事件路由），返回摘掉的数量。

    与登记对称：定时任务名由 :func:`tickneko.workflow.nodes.triggers.workflow_task_id` 定
    （``wf-<工作流 id>-<节点 id>``），照图里的开始节点算一遍 id 去摘；消息触发的从
    :class:`MessageRouter` 摘除（按 workflow_id）—— **不用把图跑一遍**（那是执行，不是停机）。

    与登记对称，归属一样从定义表读：停用也是「谁的流被停了」，记成公共就没法按人查。
    """
    definition = await store.get(workflow_id)
    owner_id: str = definition.owner_id if definition is not None else ""
    log: BoundLogger = _log().bind(workflow_id=workflow_id, owner_id=owner_id)

    record = await store.get_version(workflow_id, version)
    if record is None:
        log.warning("工作流版本不存在，跳过停用", version=version)
        return 0

    graph = record.graph()
    message_starts = _message_start_ids(graph)
    event_starts = _event_start_ids(graph)
    removed = 0
    # 消息触发按 workflow 摘一次（多个消息 start 节点共享同一路由条目，别重复计）
    if message_starts and message_router is not None:
        message_router.unregister(workflow_id, owner_id)
        removed += 1
    # 事件触发同理：一条工作流一个路由条目（订阅的事件类型跟着版本走，直接整条摘掉）
    if event_starts and event_router is not None:
        if event_router.unregister(workflow_id, owner_id):
            removed += 1
    for node_id in start_ids(graph.nodes):
        if node_id in message_starts or node_id in event_starts:
            continue
        if scheduler.remove(workflow_task_id(workflow_id, node_id)):
            removed += 1
    if removed:
        log.info("已停止触发", version=version, count=removed)
    return removed


class EventRouter:
    """事件触发的登记处：按**归属 + 订阅的事件类型**找到匹配的工作流，事件进来时逐个跑。

    与 :class:`MessageRouter` 平行：那条是「来消息就跑」，这条是「来了**订阅的那种**事件
    才跑」—— 平台事件（加好友请求、进群、撤回、戳一戳…）不是消息，不该每条都把工作流拉起来。

    匹配口径：节点上 ``event_type`` 填 ``"*"`` 表示任何事件都触发，其余与事件的原生类型名
    （``trigger_data["event_type"]``）**全等**才触发 —— 不做前缀匹配：订阅
    ``group_increase`` 的图不该被 ``group_decrease`` 拉起来。

    同样的两趟口径：登记 / 摘除只在「登记那一趟」（拨开关 / 启动载入 / 发布新版）发生，
    ``dispatch`` 是执行那一趟，**不碰登记表**。

    **本模块不 import bridge**：它只认普通数据（``trigger_data`` 里的 ``event_type`` /
    ``user_id``…），「平台事件拆成这些普通数据」由装配层（bootstrap）做。
    """

    def __init__(self) -> None:
        #: owner_id -> { workflow_id -> (version, 订阅的事件类型) }
        self._routes: dict[str, dict[str, tuple[int, str]]] = {}
        #: 事件来了「跑整条流程」要用的回调；装配时注入（见 :meth:`attach`）
        self._run: Callable[..., Awaitable[object]] | None = None

    def attach(self, run: Callable[..., Awaitable[object]]) -> None:
        """注入「跑整条流程」的回调：``dispatch`` 拿它执行匹配的工作流。

        回调签名是 ``(workflow_id, version, *, trigger_data=..., user_id=...) -> Awaitable``。
        """
        self._run = run

    def register(self, workflow_id: str, version: int, owner_id: str, event_type: str) -> None:
        """登记一条事件触发（同一工作流重复登记按新版本覆盖）。"""
        self._routes.setdefault(owner_id, {})[workflow_id] = (version, event_type)

    def unregister(self, workflow_id: str, owner_id: str) -> bool:
        """摘掉一条事件触发；真摘掉了返回 ``True``。"""
        routes = self._routes.get(owner_id)
        if not routes or workflow_id not in routes:
            return False
        routes.pop(workflow_id)
        if not routes:
            self._routes.pop(owner_id, None)  # 这个归属下没别的了，连空壳一起清掉
        return True

    def routes_of(self, owner_id: str) -> dict[str, tuple[int, str]]:
        """某个归属下登记过的事件触发快照（``{workflow_id: (version, event_type)}``）。"""
        return dict(self._routes.get(owner_id, {}))

    async def dispatch(self, owner_id: str, *, trigger_data: Mapping[str, Any]) -> int:
        """事件进来：跑这个归属下**订阅了这种事件**的工作流，返回跑过的条数。

        * 每个工作流都拿同一份 ``trigger_data``（事件数据）与 ``user_id``（事件相关的人，
          从 ``trigger_data`` 里取）；
        * 单个工作流抛异常不影响其它（记 error 继续），异常不冒给调用方 —— 一个事件不该
          因为某条工作流坏了就没人处理。
        """
        routes = self._routes.get(owner_id)
        if not routes or self._run is None:
            return 0
        event_type = str(trigger_data.get("event_type", ""))
        ran = 0
        for workflow_id, (version, wanted) in list(routes.items()):
            if wanted != "*" and wanted != event_type:
                continue
            try:
                await self._run(
                    workflow_id,
                    version,
                    trigger_data=trigger_data,
                    user_id=str(trigger_data.get("user_id", "") or ""),
                )
                ran += 1
            except Exception as exc:  # noqa: BLE001 — 单个坏工作流不能淹其它
                _log_run_failure(
                    _log(),
                    "事件触发的工作流执行失败",
                    exc,
                    workflow_id=workflow_id,
                    version=version,
                    event_type=event_type,
                )
        return ran


class MessageRouter:
    """消息触发的登记处：按归属（``owner_id``）找到匹配的工作流，消息进来时逐个跑。

    与时间触发的对偶：``trigger=time`` 把整条流程登记到调度器（cron 到点跑），
    ``trigger=message`` 把整条流程登记到这里（收到消息事件时跑）。登记在「登记那一趟」
    （拨运行开关 / 启动载入 / 发布新版）做，``dispatch`` 是「执行那一趟」——**不碰登记表**，
    与 ``NodeExecutionContext.register_triggers`` 同一套口径。

    路由键 = ``owner_id``：消息发给哪个机器人（owner，握手时令牌定下的 id），就触发那个
    owner 下所有登记过的 ``trigger=message`` 工作流。发消息的人是 ``user_id``，由调用方
    从 ``trigger_data`` 里带进来。

    **本模块不 import bridge**：它只认普通数据（``trigger_data`` 字典 + ``user_id``），
    「PlatformEvent 拆成这些普通数据」由装配层（bootstrap）做 —— 与 P2「bridge 不 import
    workflow」对得上，依赖方向不破。
    """

    def __init__(self) -> None:
        #: owner_id -> { workflow_id -> version }：同一工作流重复登记覆盖（版本号随发布挪）
        self._routes: dict[str, dict[str, int]] = {}
        #: dispatch 跑整条流程要用的回调；装配时注入（见 :meth:`attach`）
        self._run: Callable[..., Awaitable[None]] | None = None

    def attach(self, run: Callable[..., Awaitable[None]]) -> None:
        """注入「跑整条流程」的回调：``dispatch`` 拿它执行匹配的工作流。

        回调签名是 ``(workflow_id, version) -> Awaitable[None]``，装配层把
        :func:`run_published_workflow` 连同 store / scheduler 一起闭包进来。
        """
        self._run = run

    def register(self, workflow_id: str, version: int, owner_id: str) -> None:
        """登记一条消息触发：同一工作流重复登记按新版本覆盖（发布挪指针后重新登记）。"""
        self._routes.setdefault(owner_id, {})[workflow_id] = version

    def unregister(self, workflow_id: str, owner_id: str) -> None:
        """摘除一条消息触发；不存在无害。"""
        routes = self._routes.get(owner_id)
        if routes is not None:
            routes.pop(workflow_id, None)

    def routes(self, owner_id: str) -> dict[str, int]:
        """某个归属下登记过的消息触发快照（``{workflow_id: version}``）。"""
        return dict(self._routes.get(owner_id, {}))

    async def dispatch(self, owner_id: str, *, trigger_data: Mapping[str, Any]) -> int:
        """消息进来：跑这个归属下**所有**登记过的 ``trigger=message`` 工作流，返回跑过的条数。

        * 每个工作流都拿同一份 ``trigger_data``（消息内容）与 ``user_id``（发消息的人，从
          ``trigger_data`` 里取，没有就是空串）；
        * 单个工作流失败只记 error、不淹其它（口径同 :func:`load_published_workflows`）；
        * 没注入执行回调（``attach`` 没调）时只记 warning、返回 0 —— 路由是纯登记表，
          执行能力由装配层给。

        :param trigger_data: 消息事件拆成的普通数据（含 ``message`` / ``user_id`` 等）；
            缺省 ``user_id`` 键时按空串（``NO_USER_ID`` 口径）。
        """
        if self._run is None:
            _log().warning("消息路由未注入执行回调，丢弃消息", owner_id=owner_id)
            return 0
        routes = self._routes.get(owner_id)
        if not routes:
            return 0
        user_id = str(trigger_data.get("user_id", "") or "")
        ran = 0
        for workflow_id, version in routes.items():
            try:
                await self._run(workflow_id, version, trigger_data=trigger_data, user_id=user_id)
                ran += 1
            except Exception as exc:  # noqa: BLE001 — 单个坏工作流不能淹其它
                _log_run_failure(
                    _log(),
                    "消息触发执行工作流失败",
                    exc,
                    workflow_id=workflow_id,
                    owner_id=owner_id,
                    version=version,
                )
        return ran


class WorkflowTriggers:
    """启停某个已发布版本的时间触发（接口层的「运行开关」靠它**即时生效**）。

    接口层只认结构化的 ``start`` / ``stop`` 两个方法（见
    :mod:`tickneko.api.api.workflow.protocols`），**不 import 本模块**；装配时由主程序把这一份
    传进 ``create_app(workflow_triggers=...)``。没传的场合（直接 ``create_app`` 的测试 / 示例）
    开关照样落库，只是生效点在下次启动载入。

    ``gateway``（平台总线，可选）装配时给：拨开关即时生效走的是这儿的登记，登记构造的
    到点闭包要带上它（见 :func:`make_trigger`），``send`` 节点到点执行那一趟也要能拿得到它。
    ``message_router``（可选）同理：消息触发的登记 / 摘除也走这儿；``event_router``
    （可选）是事件触发那一档。
    """

    def __init__(
        self,
        store: SqlWorkflowStore,
        scheduler: TaskManager,
        *,
        gateway: Any | None = None,
        message_router: MessageRouter | None = None,
        event_router: EventRouter | None = None,
    ) -> None:
        self._store: SqlWorkflowStore = store
        self._scheduler: TaskManager = scheduler
        self._gateway: Any | None = gateway
        self._message_router: MessageRouter | None = message_router
        self._event_router: EventRouter | None = event_router

    async def start(self, workflow_id: str, version: int) -> int:
        """登记这一版的触发（重复调用幂等），返回跑过的开始节点数量。"""
        return await register_published_workflow(
            workflow_id,
            version,
            self._store,
            self._scheduler,
            gateway=self._gateway,
            message_router=self._message_router,
            event_router=self._event_router,
        )

    async def stop(self, workflow_id: str, version: int) -> int:
        """摘掉这一版登记过的触发（重复调用无害），返回摘掉的数量。"""
        return await stop_published_workflow(
            workflow_id,
            version,
            self._store,
            self._scheduler,
            message_router=self._message_router,
            event_router=self._event_router,
        )


async def load_published_workflows(
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    gateway: Any | None = None,
    message_router: MessageRouter | None = None,
    event_router: EventRouter | None = None,
    page_size: int = 500,
) -> int:
    """启动时把**开着运行开关**的已发布工作流登记就绪，返回载入的开始节点数量。

    遍历 ``status=published`` 且 ``published_version>0`` **且 ``enabled``** 的定义，逐个跑其
    已发布版本的**开始节点**（时间触发的据此把整条流程登记到 cron，消息触发的登记到消息
    路由；**下游一个都不执行**，见 :func:`register_published_workflow`）。发布 ≠ 运行：刚发布
    的（开关默认关）不在这里被跑。单个失败不影响其他工作流，异常只记 error。

    **翻页翻到底，不给自己设总量上限**：以前是写死 ``limit=500`` 一次拉完 —— 超过 500 条的
    那些工作流开机根本不会登记（静默漏跑，最难查的那种）。现在按 ``page_size`` 一页页拉
    （``store.list`` 的 ``limit``/``offset``），拉空为止。

    分页按 ``updated_at`` 倒序进行，而这一趟**只读库**（登记不写定义表，见
    :func:`register_published_workflow`），所以遍历期间顺序稳定；即便如此，接口层此刻已经在
    跑、可能并发保存暂存区（会改 ``updated_at``、把行挪到另一页），因此对同一 id 只登记一次
    —— 重复登记无害，漏掉才致命。

    ``gateway``（平台总线，可选）由装配层传进来，跟着登记一起进到点闭包（见
    :func:`make_trigger`）；``send`` 节点靠它发动作。

    日志：开头一条「开始载入」，结尾一条「载入完成」带各档条数（扫过多少、登记了哪些、开关
    关着跳过了多少、登记到几个开始节点）—— **一条都没登记也照记**，好把「载入跑过了，只是
    没得跑」和「载入压根没跑」分开。哪条工作流被登记，看开始节点那条（``已登记到调度器`` /
    ``工作流开始（消息触发…）``，都带 ``workflow_id``）。

    归属：开头 / 结尾这两条是**跨所有工作流**的全局事件，归公共；单条工作流的事（载入失败的
    error、开始节点那几条）挂在它自己的 ``owner_id`` 名下。
    """
    log: BaseLogger | BoundLogger = _log()
    primed = 0  # 登记到的开始节点数
    registered = 0  # 真正登记上的工作流条数
    disabled = 0  # 已发布但开关关着：跳过
    scanned = 0  # 扫过的定义数（含没发布 / 开关关着的）
    seen: set[str] = set()
    offset = 0
    log.info("开始载入已发布工作流")
    while True:
        definitions = await store.list(owner_id=None, limit=page_size, offset=offset)
        if not definitions:
            break
        for definition in definitions:
            if definition.id in seen:
                continue
            seen.add(definition.id)
            scanned += 1
            if definition.status != "published" or definition.published_version <= 0:
                continue
            if not definition.enabled:
                disabled += 1
                continue  # 已发布但开关关着：不登记、不跑（新发布默认就是这个状态）
            try:
                primed += await register_published_workflow(
                    definition.id,
                    definition.published_version,
                    store,
                    scheduler,
                    gateway=gateway,
                    message_router=message_router,
                    event_router=event_router,
                )
                registered += 1
            except Exception as exc:  # noqa: BLE001 — 单个坏工作流不能挡住启动
                _log_run_failure(
                    log,
                    "启动载入已发布工作流失败",
                    exc,
                    workflow_id=definition.id,
                    owner_id=definition.owner_id,  # 谁的流没载进来，当场认得出
                    version=definition.published_version,
                )
        offset += len(definitions)
        if len(definitions) < page_size:
            break
    log.info(
        "已发布工作流启动载入完成",
        scanned=scanned,  # 扫过的定义
        registered=registered,  # 登记上的工作流
        triggers=primed,  # 登记到的开始节点
        disabled=disabled,  # 开关关着跳过的
    )
    return primed
