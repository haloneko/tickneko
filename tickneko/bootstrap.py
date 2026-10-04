"""业务装配：入口把「日志核心 + 库引擎」备好之后，由本模块把 tickneko 自己这几块挂起来跑。

与根目录 ``app.py`` 的分工（对看）::

    app.py           读配置 -> 按配置建日志核心 -> 建库引擎 -> 交给本模块 -> 停机收尾
    tickneko/bootstrap  建表与存储 -> 起接口层 HTTP -> 起 bridge 总线（OneBot 适配器经
                     Gateway）-> 起调度器 -> 载入已发布工作流 -> 停机（含冲刷日志余量、
                     关库连接）

为什么要在意这个顺序：日志核心必须在业务真正开始跑之前按配置建好，然后经
:func:`tickneko.wiring.wire_loggers` 存进各业务模块的日志槽位。各 ``logging`` 接入点
（``workflow_logger`` 等）是纯「槽位 + 惰性取」，import 零副作用 —— 没装配就调用会当场
抛错（fail fast），不会默默按默认参数建一份把配置定死的核心。入口因此不在顶层 import
业务模块，本模块也是**建好核心之后**才被导入。

依赖方向：本模块认识 tickneko 的各业务包；反过来不成立 —— 入口只认识本模块，不认识业务。
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress

import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from .api import (
    ApiOptions,
    SqlSessionStore,
    SqlUserStore,
    create_app,
)
from .bots import SqlBotStore
from .core.cache import CacheOptions, cache
from .core.logger import BaseLogger, default_core, manager
from .core.scheduler import scheduler
from .platforms.bridge import Gateway, PlatformEvent
from .platforms.bridge.kook import KookAdapter
from .platforms.bridge.manager import BotManager
from .platforms.bridge.onebot import OneBotAdapter
from .platforms.kook import KookOptions
from .platforms.onebot import OneBotOptions
from .wiring import wire_loggers
from .workflow import SqlWorkflowStore
from .workflow.runtime import (
    EventRouter,
    MessageRouter,
    WorkflowTriggers,
    load_published_workflows,
    run_published_workflow,
)

# --------------------------------------------------------------------------- 状态
#: 接口层 HTTP 服务（随主程序由 uvicorn 起）；停机时取用
_api_server: uvicorn.Server | None = None
_api_task: asyncio.Task[None] | None = None
#: 入口建好、交给本模块共用的数据库引擎；停机时 dispose
_db_engine: AsyncEngine | None = None
#: bridge 总线（平台适配器的注册处 / 事件分发处）；停机时一并停
_gateway: Gateway | None = None
#: OneBot 适配器（包着反向 WS 服务端）；主协程停在它的 serve_forever 上
_onebot_adapter: OneBotAdapter | None = None
#: Kook 适配器（包着正向 WS 客户端）；有 [kook].secret_key（留空时入口自动生成）才建
_kook_adapter: KookAdapter | None = None
#: 消息路由（trigger=message 工作流的登记处 / 消息分发处）；载入时一并登记
_message_router: MessageRouter | None = None
#: 事件路由（trigger=event 工作流的登记处 / 事件分发处）；载入时一并登记
_event_router: EventRouter | None = None


class _NoSignalServer(uvicorn.Server):
    """随主程序跑时信号由主程序统一管：覆盖掉 uvicorn 自带的 SIGINT 安装，免得抢了业务循环的停机。"""

    def install_signal_handlers(self) -> None:
        pass


# --------------------------------------------------------------------------- 落库
async def _prepare_stores(
    db: AsyncEngine, log: BaseLogger
) -> tuple[SqlBotStore, SqlUserStore, SqlSessionStore, SqlWorkflowStore]:
    """建表 + 种演示账号（幂等）：各份落库存储都挂同一个 ``db``。

    放在**启动阶段**而不是等 lifespan：接口服务是 ``create_task`` 起的，启动阶段抛的异常没人
    await、会被静默吞掉，于是「没建成」只在第一个请求时才炸成 1146（表不存在），离真正的原因
    很远。放这里：失败就是启动失败，当场看得见。（lifespan 里那次留着兜底，幂等。）
    """
    tokens = SqlBotStore(db)
    await tokens.ensure_schema()
    users = SqlUserStore(db)  # hasher 默认 PBKDF2，只有 seed_demo 用
    await users.ensure_schema()
    seeded = await users.seed_demo()  # 空表才种演示账号，已有数据不动
    sessions = SqlSessionStore(db)
    await sessions.ensure_schema()
    workflows = SqlWorkflowStore(db)
    await workflows.ensure_schema()
    log.info(
        "数据表就绪",
        tables=[
            "bot_credentials",
            "users",
            "auth_sessions",
            "workflow_definitions",
            "workflow_versions",
        ],
        demo_accounts=seeded,  # 0 = 表里本来就有账号，一条没动
    )
    return tokens, users, sessions, workflows


# --------------------------------------------------------------------------- 业务
async def on_platform_event(event: PlatformEvent) -> None:
    """bridge 事件订阅：业务接这里。

    认的是 :class:`~tickneko.platforms.bridge.models.PlatformEvent`，**不再认识任何平台事件** ——
    平台差异（OneBot 的整数号、Kook 的字符串号）在适配器里翻译掉了。要发消息走
    ``gateway.send(platform, owner_id, action, ...)``。

    ``kind=message`` 的事件拆成普通数据（``trigger_data`` + ``owner_id`` / ``user_id``）交给
    消息路由（``MessageRouter.dispatch``）触发 ``trigger=message`` 的工作流 —— 「PlatformEvent
    拆成普通数据」这一步就发生在这里，消息路由本身不 import bridge，依赖方向不破。
    """
    log = default_core().child("bridge")
    if event.kind == "meta":
        # 连接生命周期 / 心跳一类：与业务无关，静默（只留 debug，不刷 INFO）
        log.debug(
            "收到元事件",
            platform=event.platform,
            owner_id=event.owner_id,
            kind=event.kind,
        )
        return
    if event.kind != "message":
        # 通知 / 请求（加好友、进群、撤回、戳一戳…）：交给**事件路由**，
        # 触发订阅了这种事件的 trigger=event 工作流；没订阅就没人跑（路由内部按类型匹配）。
        await _dispatch_event(event)
        return
    log.info(
        "收到事件",
        platform=event.platform,
        owner_id=event.owner_id,
        kind=event.kind,
        chat=event.chat,
        chat_id=event.chat_id,
        user_id=event.user_id,
    )
    router = _message_router
    if router is None:
        return  # 装配还没走到建路由（或没配消息触发）—— 不该发生，防御性放过
    try:
        await router.dispatch(
            event.owner_id,
            trigger_data={
                "message": event.text,
                "user_id": event.user_id,
                "platform": event.platform,
                "chat": event.chat,
                "chat_id": event.chat_id,
                "message_id": event.message_id,
                #: 会话定位（回程地址）：start 的 target 出口原样透给下游 send 节点
                #: （回复触发它的会话）。workflow 只透传这个对象，不 import bridge 类型 ——
                #: 它认的是「有 platform 属性的东西」，路由键就够用了。没有会话指向的事件是 None。
                "target": event.target,
            },
        )
    except Exception:  # noqa: BLE001 — 消息入口尽力而为，别让一条坏事件拖垮整条链路
        log.exception("消息事件分发失败", platform=event.platform, owner_id=event.owner_id)


async def _dispatch_event(event: PlatformEvent) -> None:
    """通知 / 请求类事件 -> 事件路由（``trigger=event`` 的工作流按订阅的类型匹配）。

    同样只认普通数据：事件类型 / 谁 / 哪个会话 / 带的文本 / 会话定位一并交出去，
    路由与工作流都不 import bridge（依赖方向同消息那条路）。
    """
    log = default_core().child("bridge")
    if not event.event_type:
        # 没有事件类型的事件（如 Kook 的系统消息）：订阅不了、也无从匹配，
        # 静默丢掉 —— 别让「订阅任何事件（*）」的图被这种东西拉起来
        log.debug("收到没有事件类型的事件，已忽略", platform=event.platform, kind=event.kind)
        return
    router = _event_router
    if router is None:
        return  # 装配还没走到建事件路由（或没配事件触发）—— 防御性放过
    try:
        await router.dispatch(
            event.owner_id,
            trigger_data={
                "event_type": event.event_type,
                "user_id": event.user_id,
                "platform": event.platform,
                "self_id": event.self_id,
                "chat": event.chat,
                "chat_id": event.chat_id,
                "text": event.text,
                "message_id": event.message_id,
                "target": event.target,
            },
        )
    except Exception:  # noqa: BLE001 — 事件入口尽力而为，别让一条坏事件拖垮整条链路
        log.exception(
            "平台事件分发失败",
            platform=event.platform,
            owner_id=event.owner_id,
            kind=event.kind,
            event_type=event.event_type,
        )


# --------------------------------------------------------------------------- 装配
async def run(
    *,
    engine: AsyncEngine,
    api: Mapping[str, object],
    api_host: str,
    api_port: int,
    onebot: Mapping[str, object],
    kook: Mapping[str, object] | None = None,
    cache_config: Mapping[str, object],
) -> None:
    """把业务挂起来（不阻塞）：建表 -> 起接口层 -> 起 OneBot / Kook -> 起调度器 -> 载入工作流。

    :param engine: 入口建好的共用引擎（与日志库出口默认是同一个）；
    :param api: ``[api]`` 那块配置，交给 ``ApiOptions.from_mapping``；
    :param api_host / api_port: 接口层监听地址 —— 这两个归入口管（``ApiOptions`` 里没有，它
        只管前缀与令牌有效期）；
    :param onebot: ``[onebot]`` 那块配置，交给 ``OneBotOptions.from_mapping``；
    :param kook: ``[kook]`` 那块配置，交给 ``KookOptions.from_mapping``；``secret_key`` 为空
        就不接入 Kook（跳过建适配器）—— 正常启动时入口会先把留空的密钥补上
        （``data/secret_key``，见 ``config.load_or_create_secret_key``）；
    :param cache_config: ``[cache]`` 那块配置，交给 ``CacheOptions.from_mapping``。
    """
    global _api_server, _api_task, _db_engine, _gateway, _onebot_adapter, _kook_adapter
    global _message_router, _event_router
    _db_engine = engine
    log = default_core().child("bootstrap")
    # 把核心派发给各业务模块（workflow / scheduler / cache / bridge 的日志槽位）：之后它们
    # 再调便捷函数就直接落进这份核心，未装配则当场抛错（fail fast），而不是各自 default_core()
    wire_loggers(default_core())

    # 缓存：默认（memory）就是本地内存；配了 redis 而连不上时默认当场报错（提示改配置），
    # 只有 fallback_to_memory = true 才退回内存
    cache.configure(CacheOptions.from_mapping(cache_config))
    await cache.start()

    tokens, users, sessions, workflows = await _prepare_stores(engine, log)

    # bridge 总线：OneBot 适配器（包着反向 WS 服务端）注册进去，事件翻成规范化形状后
    # 从总线分发（订阅见 on_platform_event）。日志不再各落一份文件：入口那份文件出口
    # 是**整进程共用**的（按天分片），bridge / 接口层的日志照样进它，靠记录里的
    # logger_name 区分来源。
    _gateway = Gateway()
    _onebot_adapter = OneBotAdapter(
        OneBotOptions.from_mapping(onebot),
        publish=_gateway.publish,  # 投递口：适配器翻译完事件调它
        tokens=tokens,  # 令牌 -> 账号；一个端口接多个客户端，靠它认归属
    )
    _gateway.register(_onebot_adapter)

    # Kook 适配器（正向 WS 客户端，**多客户端**）：**一个**适配器管多个机器人
    # （dict[bot_id, client]），Gateway 里 Kook 只占一个 platform 槽位，之后经 /api/bots
    # 增删 Kook 机器人也不会撞「同平台重复注册」的限制。凭证行来自 bot_credentials
    # （platform=kook 且启用），Bot Token 用 [kook].secret_key 从 token_secret 解密出来逐个
    # ``add_bot``；网关地址由客户端连接前自己 discover（配置文件里不填）。
    # 有 secret_key 就算「接入了 Kook」，适配器照建（哪怕此刻一个机器人都没有）—— 这样之后
    # 经接口新增 Kook 机器人能立刻拉起连接。留空时入口已经自动生成/读取一份（data/secret_key），
    # 所以正常总有值；真为空（比如不走入口的测试）就一个 Kook 适配器都不建。
    kook_options = KookOptions.from_mapping(kook) if kook is not None else KookOptions()
    secret_key: str = kook_options.secret_key
    _kook_adapter = None
    if secret_key:
        _kook_adapter = KookAdapter(kook_options, publish=_gateway.publish)
        kook_credentials = await tokens.list_platform("kook", enabled_only=True)
        for cred in kook_credentials:
            try:
                bot_token = await tokens.decrypt_token(cred.bot_id, secret_key)
            except ValueError as exc:
                # 有密文但解不开：几乎都是 secret_key 配错——明说，别让用户只看到「机器人起不来」
                log.error(
                    "Kook 凭证行解不开（secret_key 不对或密文损坏），跳过登记",
                    bot_id=cred.bot_id,
                    error=str(exc) or repr(exc),
                )
                continue
            if not bot_token:
                log.warning("Kook 凭证行没有密文，跳过登记", bot_id=cred.bot_id)
                continue
            _kook_adapter.add_bot(cred.bot_id, bot_token, owner_id=cred.owner_id)
        _gateway.register(_kook_adapter)

    # 机器人管理服务：跨平台统一「增 / 启停 / 删」，凭证落库 + 适配器生命周期一起封在
    # BotManager 里，接口层只认 BotsService 协议（platform 差异不进接口层）。
    bot_manager = BotManager(
        tokens,
        onebot=_onebot_adapter,
        kook=_kook_adapter,
        secret_key=secret_key,
    )

    def _run_flow(workflow_id: str, version: int, **kw: object):
        """「跑整条流程」的回调：消息 / 事件两条路由共用一份（闭包带上 store / scheduler / gateway）。

        两条路由都只认普通数据，跑到整条流程时才需要这些依赖 —— 消息触发与事件触发跑整条
        流程那一趟都要能拿得到它们发动作。
        """
        return run_published_workflow(
            workflow_id, version, workflows, scheduler, gateway=_gateway, **kw
        )

    # 消息路由：trigger=message 工作流的登记处 + 消息分发处。它不 import bridge，只认普通
    # 数据；「跑整条流程」的回调在这里把 run_published_workflow 连同 store / scheduler 闭包
    # 进来（消息触发跑整条流程那一趟也要能拿得到它们发动作）。
    _message_router = MessageRouter()
    _message_router.attach(_run_flow)

    # 事件路由：trigger=event 工作流的登记处 + 事件（通知 / 请求）分发处。与消息路由同一副
    # 形状（不 import bridge、只认普通数据），区别只在「按订阅的事件类型匹配」。
    _event_router = EventRouter()
    _event_router.attach(_run_flow)

    _gateway.subscribe(on_platform_event)

    # 接口层：建应用（注入同一个 db 上的三份存储 + OneBot 适配器）-> 起 uvicorn
    options = ApiOptions.from_mapping(api)
    _api_server = _NoSignalServer(
        uvicorn.Config(
            create_app(
                options,
                user_store=users,
                session_store=sessions,
                # 注入的是适配器（兼容面满足接口层的 OneBotLike 协议：roster / kick /
                # revoke / tokens 都透传给被包的服务端），<prefix>/onebot/* 那组管理接口零改动
                onebot=_onebot_adapter,
                # 机器人管理服务（跨平台增 / 启停 / 删）：<prefix>/bots/* 那组接口用它
                bots=bot_manager,
                # 运行时触发器：拨工作流的运行开关时即时启停（不传是等下次启动才生效）；
                # 带上平台总线：登记构造的到点闭包要能发动作
                workflow_triggers=WorkflowTriggers(
                    workflows,
                    scheduler,
                    gateway=_gateway,
                    message_router=_message_router,
                    event_router=_event_router,
                ),
                workflow_store=workflows,
            ),
            host=api_host,
            port=api_port,
            log_config=None,  # 不接管日志系统（接口层走 tickneko.core.logger）
            access_log=False,  # 访问日志交给接口层自己的中间件
        )
    )
    _api_task = asyncio.create_task(_api_server.serve(), name="api")

    # 启动定时任务调度器：开始节点（trigger=time）靠它到点触发
    await scheduler.start()

    # 把**开着运行开关**的已发布工作流的触发登记就绪：定时触发登记到调度器、消息触发登记到
    # 消息路由，只登记、不执行图（到点 / 来消息才跑）。发布只挪指针、不执行图；跑不跑看开关，
    # 运行期拨开关走接口层那个即时启停。带上平台总线：登记构造的到点闭包要能发动作。
    await load_published_workflows(
        workflows,
        scheduler,
        gateway=_gateway,
        message_router=_message_router,
        event_router=_event_router,
    )


async def serve_forever() -> None:
    """主协程停在这：bridge 总线起监听并一直跑；端口被占等当场抛 ``OSError``（怎么退由入口定）。"""
    if _gateway is None:
        raise RuntimeError("还没装配：先跑 run()")
    await _gateway.start()  # 起所有适配器的监听
    # 等所有暴露 serve_forever 的适配器（OneBot 反向 WS、Kook 正向 WS 都是长连接；
    # 只等一个的话，另一个的服务没人守——所以这里按平台遍历，一个不落）
    waiters = [
        adapter.serve_forever()
        for adapter in (_gateway.adapter(name) for name in _gateway.platforms)
        if adapter is not None and hasattr(adapter, "serve_forever")
    ]
    if waiters:
        await asyncio.gather(*waiters)


# --------------------------------------------------------------------------- 收尾
async def shutdown() -> None:
    """收尾：先停对外的两个服务（接口层 HTTP + bridge 总线）-> 等调度器跑完在飞的任务
    -> 冲刷日志余量 -> 关库连接。

    顺序不能反：任务里还会写日志，得等它们收尾了再冲刷、关库，收尾日志才不会丢 —— 所以
    ``manager.stop()``（冲刷余量，可能写库）必须在关引擎之前。
    """
    if _api_server is not None:
        _api_server.should_exit = True  # 让 uvicorn 优雅退出（在飞的请求处理完再关）
    if _api_task is not None:
        with suppress(asyncio.CancelledError):
            await _api_task
    if _gateway is not None:
        await _gateway.stop()  # 逆序停所有适配器（关监听并断开所有客户端）
    await scheduler.stop()  # 等在飞的任务自然收尾（默认 5 秒，超时只记 warning，不强杀）
    await cache.stop()  # 再停缓存：任务收完了，后面不会再有业务来读写
    await manager.stop()  # 停机自动冲刷余量
    if _db_engine is not None:
        await _db_engine.dispose()
