"""把接口层装成一个 FastAPI 应用。

:func:`create_app` 是唯一的装配入口，按顺序做四件事：

1. 建 ``FastAPI``（带 :func:`lifespan`：启动 / 停各记一条日志）；
2. 装 :class:`~tickneko.api.common.middlewares.RequestLogMiddleware`（编号 + 访问日志）；
3. 装异常处理器（:func:`~tickneko.api.common.errors.register_exception_handlers`）—— 出去的错误
   都是 :class:`~tickneko.api.common.models.ErrorResponse` 那个形状；
4. 挂各业务模块的路由（鉴权 ``<prefix>/auth``、OneBot 管理 ``<prefix>/onebot``、机器人管理
   ``<prefix>/bots``、运行日志 ``<prefix>/logs``），并把各模块的服务挂到 ``app.state`` 上给路由注入。

依赖全是可选的：不传 ``user_store`` / ``session_store`` 就用落库版
:class:`~tickneko.api.services.user.store_sql.SqlUserStore`（查 ``users`` 表）与
:class:`~tickneko.api.services.session.store_sql.SqlSessionStore`（查 ``auth_sessions`` 表），
不传 ``hasher`` 就走默认实现；没传 ``db`` 时给它们挂一块**内存 sqlite**（启动时建表 +
空表种演示账号），所以 **不接数据库也能直接跑起来**。

``onebot`` 同样是可选的：主程序把 :class:`tickneko.platforms.onebot.OneBotServer` 传进来，
``<prefix>/onebot/*`` 那组管理接口才有用；没传就回 503（「没接入」和「出错了」分开报）。
这里按 :class:`~tickneko.api.api.onebot.protocols.OneBotLike` 协议接收，所以 **接口层不 import
``tickneko.platforms.onebot``** —— 只装 ``tickneko[api]`` 也能跑起来（具体说明见那个模块）。

``bots`` 同样是可选的：主程序把 :class:`tickneko.platforms.bridge.manager.BotManager` 传进来，
``<prefix>/bots/*`` 那组管理接口（跨平台增 / 启停 / 删）才有用；没传就回 503。这里按
:class:`~tickneko.api.api.bots.protocols.BotsService` 协议接收，所以接口层不 import 任何平台包。

用法::

    from tickneko.api import ApiOptions, create_app

    app = create_app(ApiOptions.from_mapping(settings.api.model_dump()))

    # uvicorn tickneko_api:app --port 18080
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tickneko import __version__
from tickneko.core.logger import BaseLogger

from .common.errors import register_exception_handlers
from .common.middlewares import RequestLogMiddleware
from .logging import API_LOGGER_NAME, api_logger
from .api import (
    auth_router,
    bots_router,
    log_router,
    onebot_router,
    owners_router,
    profile_router,
    workflow_router,
)
from .api.bots.protocols import BotsService
from .api.onebot.protocols import OneBotLike
from .api.workflow.protocols import WorkflowStoreLike, WorkflowTriggerLike
from .services.auth import AuthService
from .services.profile import AvatarStore, FileAvatarStore, ProfileService
from .services.session import SessionService, SqlSessionStore
from .services.session.protocols import SessionStore
from .services.user.protocols import PasswordHasher, UserStore
from .services.user.security import Pbkdf2PasswordHasher
from .services.user.store_sql import SqlUserStore
from .options import ApiOptions


def create_app(
    options: ApiOptions | None = None,
    *,
    user_store: UserStore | None = None,
    hasher: PasswordHasher | None = None,
    db: AsyncEngine | None = None,
    session_store: SessionStore | None = None,
    onebot: OneBotLike | None = None,
    bots: BotsService | None = None,
    workflow_store: WorkflowStoreLike | None = None,
    workflow_triggers: WorkflowTriggerLike | None = None,
    avatar_store: AvatarStore | None = None,
    title: str = "TickNeko",
    version: str = __version__,
    logger: BaseLogger | None = None,
) -> FastAPI:
    """装配一个 FastAPI 应用（接口层的对外门面）。

    :param options: 接口层选项（路由前缀、访问令牌滑动有效期、长期令牌有效期、访问日志）；
    :param user_store: 用户存储，默认落库版（不接库时挂内存 sqlite，空表种演示账号）；传了就直接用；
    :param hasher: 密码哈希器，默认 PBKDF2；
    :param session_store: 会话存储，默认落库版（不接库时与用户存储共挂一块内存 sqlite）
        ——登录令牌是**有状态**的，会话信息得有地方放；
    :param db: 异步引擎（``AsyncEngine``）；传了就用它跑落库版
        :class:`~tickneko.api.services.user.store_sql.SqlUserStore`（查 ``users`` 表）与
        :class:`~tickneko.api.services.session.store_sql.SqlSessionStore`（查 ``auth_sessions`` 表），
        没传就兜底挂一块**内存 sqlite**——**不接数据库也能直接跑起来**；
    :param workflow_store: 工作流存储（默认实现 ``tickneko.workflow.SqlWorkflowStore``，这里只认
        :class:`~tickneko.api.api.workflow.protocols.WorkflowStoreLike` 那份能力协议）；传了
        ``<prefix>/workflows/*`` 那组接口才可用，没传时这些接口回 503；
    :param onebot: OneBot 服务端（``tickneko.platforms.onebot.OneBotServer``，只认 ``OneBotLike``
        协议）；传了 ``<prefix>/onebot/*`` 那组管理接口（在线列表 / 踢人 / 令牌增删）才可用，
        没传时这些接口回 503；
    :param bots: 机器人管理服务（``tickneko.platforms.bridge.manager.BotManager``，只认
        ``BotsService`` 协议）；传了 ``<prefix>/bots/*`` 那组管理接口（跨平台增 / 启停 / 删）
        才可用，没传时这些接口回 503；
    :param workflow_triggers: 工作流的运行时触发器（只认
        :class:`~tickneko.api.api.workflow.protocols.WorkflowTriggerLike`）；传了以后拨运行开关
        **即时启停**，没传时开关只落库、效果等下次启动载入（主程序会传，见 ``tickneko.bootstrap``）；
    :param avatar_store: 头像存储（只认
        :class:`~tickneko.api.services.profile.protocols.AvatarStore` 协议）；默认是落本地目录的
        :class:`~tickneko.api.services.profile.FileAvatarStore`，目录取选项里的 ``avatar_dir``
        （对应 ``[api] avatar_dir``）——要换成对象存储 / 存库，传一份自己的实现就行；
    :param title / version: OpenAPI 文档上的标题与版本；
    :param logger: 业务日志实例（也是 ``<prefix>/logs`` 检索用的那个），默认 ``api`` 那个。
    """
    chosen: ApiOptions = options if options is not None else ApiOptions()
    log: BaseLogger = logger if logger is not None else api_logger(API_LOGGER_NAME)
    # 访问日志只进文件与控制台：落库那份是审计时间线（/logs 查的就是它），一次请求一条
    # 的访问流水进了库只会把「谁干了什么」淹掉 —— 这份静音已内建在 api_logger 里，见
    # tickneko.api.logging（日志接入点：模块名仍是小写包名），这里不需要再做任何事。
    chosen_hasher: PasswordHasher = hasher if hasher is not None else Pbkdf2PasswordHasher()

    # 用户 / 会话都只保留落库实现（见各自 store_sql），所以都得有一块库：优先显式传的 ``db``，
    # 没传就兜底挂一块**内存 sqlite**（用户与会话共用同一块，开箱即跑）。这块内存引擎是自己
    # 建的，记下来交给 lifespan 停机时关；建表 / 种演示账号也都在 lifespan 里做。
    fallback_engine: AsyncEngine | None = None
    backing: AsyncEngine
    if db is not None:
        backing = db
    else:
        fallback_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        backing = fallback_engine

    # 选用户存储 / 会话存储：显式传的优先 -> 否则落库版挂到上面那块库上
    store: UserStore = (
        user_store if user_store is not None else SqlUserStore(backing, hasher=chosen_hasher)
    )
    sessions_store: SessionStore = (
        session_store if session_store is not None else SqlSessionStore(backing)
    )
    session_service = SessionService(
        sessions_store,
        access_ttl=chosen.token_ttl,
        remember_ttl=chosen.remember_ttl,
        logger=log,
    )
    # 工作流存储（定义 + 版本双表）：显式传的优先，否则落库版挂同一块库，开箱即跑。
    # 默认实现**按需 import**：本模块只认 WorkflowStoreLike 协议、不认识 tickneko.workflow 的实现，
    # 注入自己存储的调用方（或只想跑登录的场合）不必把工作流那包拉进来。
    workflows_store: WorkflowStoreLike
    if workflow_store is not None:
        workflows_store = workflow_store
    else:
        from tickneko.workflow import SqlWorkflowStore  # 兜底实现：只在没传存储时用

        workflows_store = SqlWorkflowStore(backing)

    # 头像存储：默认落 ``[api] avatar_dir`` 那个目录（第一次写入时才建目录，见 FileAvatarStore）。
    # 换成对象存储 / 落库，就传一份满足 AvatarStore 协议的实现进来，上面一行都不用改。
    avatars_store: AvatarStore = (
        avatar_store if avatar_store is not None else FileAvatarStore(chosen.avatar_dir)
    )
    profile_service = ProfileService(
        store,
        avatars_store,
        max_avatar_bytes=chosen.avatar_max_bytes,
        logger=log,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """启动 / 停机各记一条；落库存储在这里建表 + 空表种演示账号。"""
        log.info(
            "接口层启动",
            title=app.title,
            prefix=chosen.prefix,
            session_ttl=chosen.token_ttl,
            remember_ttl=chosen.remember_ttl,
            access_log=chosen.access_log,
        )
        # 落到库的存储：启动时先确保表在、空表种演示账号（幂等）；store 是闭包里的局部变量。
        # 主程序（根目录 app.py）在**起服务之前**会先建一次（失败即启动失败，看得见）；这里
        # 这次是幂等的兜底 —— 直接 ``create_app`` 起来的场合（测试 / 示例）靠它。
        if isinstance(store, SqlUserStore):
            await store.ensure_schema()
            await store.seed_demo()
        if isinstance(sessions_store, SqlSessionStore):
            await sessions_store.ensure_schema()
        # 工作流双表（定义 + 版本）同样幂等兜底
        await workflows_store.ensure_schema()
        try:
            yield
        finally:
            if fallback_engine is not None:
                await fallback_engine.dispose()  # 兜底的内存引擎：自己建的就自己关
            log.info("接口层停止")

    app = FastAPI(
        title=title,
        version=version,
        description="TickNeko 接口层：登录等对外接口（数据协议见各接口的 Schema）。",
        lifespan=lifespan,
    )
    # 中间件后加的先执行：访问日志在外层，能兜住里面抛出来的异常
    app.add_middleware(RequestLogMiddleware, options=chosen, logger=None)
    register_exception_handlers(app, logger=log)
    app.include_router(auth_router, prefix=chosen.prefix)
    app.include_router(profile_router, prefix=chosen.prefix)
    app.include_router(onebot_router, prefix=chosen.prefix)
    app.include_router(bots_router, prefix=chosen.prefix)
    app.include_router(owners_router, prefix=chosen.prefix)
    app.include_router(log_router, prefix=chosen.prefix)
    app.include_router(workflow_router, prefix=chosen.prefix)

    # 依赖注入：服务在这一层建好挂上去（换存储 / 换算法只改这一处）
    app.state.auth_service = AuthService(
        store,
        hasher=chosen_hasher,
        sessions=session_service,
        logger=log,
    )
    # 选项也挂上去：信任代理、长期 Cookie 的有效期这些路由要用
    app.state.api_options = chosen
    # 日志实例也挂上去：<prefix>/logs 拿它检索（与核心共享出口注册表，查得到所有出口）
    app.state.logger = log
    # 用户存储也挂上去：OneBot 令牌列表要拿归属的 id 来查昵称
    app.state.user_store = store
    # OneBot 服务端（可空）：没传时 <prefix>/onebot/* 回 503，见 onebot/dependencies.py
    app.state.onebot_server = onebot
    # 机器人管理服务（可空，只认 BotsService 协议）：没传时 <prefix>/bots/* 回 503
    app.state.bots_service = bots
    # 工作流存储（定义 + 版本双表）：<prefix>/workflows/* 用
    app.state.workflow_store = workflows_store
    # 运行时触发器（可空）：拨运行开关时即时启停，没传就只落库
    app.state.workflow_triggers = workflow_triggers
    # 个人设置（改昵称 + 头像）：<prefix>/profile/* 用
    app.state.profile_service = profile_service
    return app
