# tickneko.api 模块索引（MODULE MAP）

> 本文件是 `tickneko/api/` 下**逐文件 → 作用**的速查索引。详细的「为什么」写在每个 `.py` 的模块
> docstring 里（`:mod:` 交叉引用），本文件只做「一眼定位」用。
>
> 同步规则：新增 / 重命名 / 删除文件时，记得更新这里。

---

## 0. 分层总览

```
tickneko/api/
├── app.py            create_app()：装配（把下面这些装成一个 FastAPI 应用）
├── options.py        接口层选项（对应配置 [api]）
├── logging.py        日志接入点
├── common/           ★ 跨业务通用件（不认识任何具体业务）
│   ├── models.py         响应壳 ApiResponse/ErrorResponse…
│   ├── errors/           错误码 / 异常 / 处理器
│   ├── middlewares/      编号 + 访问日志
│   ├── dependencies.py   取请求编号
│   ├── headers.py        自定义响应头常量（X-Session-Expires-In 等）
│   └── encoding.py       base64 编解码
├── api/              ★ 入口层：只管「对外怎么说」，认识 FastAPI
│   ├── auth/
│   │   ├── router.py         POST /auth/register、POST /auth/login、GET /auth/me
│   │   │                     GET/DELETE /auth/sessions（登录设备与吊销）
│   │   ├── dependencies.py   get_auth_service / bearer_scheme
│   │   ├── requests.py       LoginRequest / RegisterRequest（请求 schema）
│   │   └── responses.py      LoginData（响应 schema）
│   ├── profile/      个人设置（改昵称 / 头像；头像存储走协议，落哪可换）
│   │   ├── router.py         GET/PATCH /profile、PUT/GET/DELETE /profile/avatar
│   │   ├── dependencies.py   get_profile_service
│   │   ├── requests.py       UpdateProfileRequest（请求 schema）
│   │   └── responses.py      ProfileData（资料 + 头像信息）
│   ├── onebot/       OneBot 管理（服务本身在 tickneko.onebot，按协议取用，不 import）
│   │   ├── router.py         在线列表 / 踢人 / 令牌签发与吊销
│   │   ├── protocols.py      OneBotLike / TokenRegistry（结构化协议）
│   │   ├── dependencies.py   get_onebot / CurrentUserDep
│   │   ├── requests.py       IssueTokenRequest
│   │   └── responses.py      ClientData / TokenData / IssuedTokenData…
│   ├── bots/        机器人管理（跨平台 /api/bots/*：增 / 启停 / 删；凭证行走 bot_credentials）
│   │   ├── router.py         列表 / 添加（选 platform）/ 启用停用 / 删除
│   │   ├── protocols.py      BotsService / OnlineBot（结构化协议）
│   │   ├── dependencies.py   get_bots / CurrentUserDep
│   │   ├── requests.py       AddBotRequest / SetBotEnabledRequest
│   │   └── responses.py      BotData / IssuedBotData（明文只在签发那一次出现）
│   ├── owners/       归属清单（GET /owners：按归属筛选时的下拉选项）
│   │   ├── router.py         管理员拿全部用户，普通用户只有自己
│   │   └── responses.py      OwnerData（owner_id / account / nickname）
│   ├── workflow/     工作流管理（实现在 tickneko.workflow，按协议取用，不 import 实现）
│   │   ├── router.py         定义增删查改 / 暂存 / 版本 / 发布 / 入库前校验
│   │   ├── protocols.py      WorkflowStoreLike（结构化协议）
│   │   ├── dependencies.py   get_workflow_store / get_in_scope / owner_filter_of
│   │   ├── requests.py       请求 schema
│   │   └── responses.py      WorkflowData / WorkflowVersionData / 校验报告…
│   └── log/          运行日志检索（GET /logs）
│       ├── router.py         条件原样透传给日志系统的 search
│       ├── dependencies.py   get_app_logger
│       └── responses.py      LogData
└── services/        ★ 业务层：只算「业务怎么办」，不认识 FastAPI
    ├── user/        用户：models/protocols/security/store/validation
    ├── session/     会话：登录开出来的那一次会话（设备信息 + 令牌）、滑动续期、吊销
    │                models/client/protocols/store_sql/tokens/service
    ├── auth/        鉴权：models/service（令牌机制在 session 里）
    └── profile/     个人设置：models/images/protocols/store_file/service（头像存储走协议）
```

**依赖方向（单向、无环）**：

```
api/*  ──►  services/*  ──►  (services/auth ──► services/user)
  │            │
  └──────────► common/*  ◄── (所有层都能用，通用件不认识业务)
```

- 入口层 `api/` 可以依赖业务层 `services/` 与通用件 `common/`；
- 业务层 `services/` **绝不** import 入口层 `api/`；
- 想加一个业务模块：逻辑丢 `services/`、HTTP 入口丢 `api/`、通用件 `common/` 不动。

---

## 1. 顶层装配与配置

| 文件 | 作用 |
|---|---|
| `tickneko/api/__init__.py` | 接口层总览 + 公开导出（`create_app`、`ApiOptions`、各业务服务、错误体系、日志接入点）。分层约定写在模块 docstring 里。 |
| `tickneko/api/app.py` | 唯一装配入口 `create_app()`：建 `FastAPI` → 装访问日志中间件 → 注册异常处理器 → 挂路由并把各业务服务挂到 `app.state` 供注入。不传 `user_store` / `hasher` / `session_store` / `workflow_store` / `avatar_store` 也能跑（走默认实现）；`onebot` 不传时那组接口回 503。跨包的两块（OneBot / 工作流）只认协议，工作流的默认实现**按需 import**；头像存储默认落 `avatar_dir` 那个本地目录。 |
| `tickneko/api/options.py` | 接口层选项 `ApiOptions`（对应配置 `[api]`）。**不读配置文件**，靠 `from_mapping` 普通映射解耦；含 `DEFAULT_PREFIX`（`/api`）、`DEFAULT_TOKEN_TTL`（7200s，访问令牌滑动有效期）、`DEFAULT_REMEMBER_TTL`（30 天，「记住设备」的长期有效期）、`DEFAULT_AVATAR_DIR`（头像目录）与 `DEFAULT_AVATAR_MAX_BYTES`（2 MiB 上限）。 |
| `tickneko/api/logging.py` | 日志接入点：`api`（业务日志）/`api.access`（访问日志）两个 logger 名 + `keep_access_off_audit()`（访问日志不进审计库）。**不挂文件出口** —— 文件是整进程一份、按天分片的，来源靠 `logger_name` 区分。 |

---

## 2. common/ —— 横向通用件（不认识任何业务）

> 这一段是「每个业务都要用」的横切能力，因此不塞进任何业务模块。加业务不需要动这里。

| 文件 | 作用 |
|---|---|
| `common/__init__.py` | 汇总导出通用件（响应壳 / 错误 / 中间件 / 取请求编号）。 |
| `common/models.py` | 响应壳：`ApiResponse` / `ErrorResponse` / `ErrorPayload` / `ErrorDetail`，以及 `DEFAULT_TRACE_ID`。不知道 `data` 里装的是什么。 |
| `common/errors/__init__.py` | 汇总导出错误体系。 |
| `common/errors/codes.py` | 错误码与 HTTP 状态：`ErrorCode` / `HttpStatus`。 |
| `common/errors/exceptions.py` | 异常类型：`ApiError` 及其子类（`ValidationError`、`InvalidCredentialsError`、`PasswordMismatchError`、`AccountDisabledError`、`AccountAlreadyExistsError`、`AvatarTooLargeError`、`AvatarTypeUnsupportedError`、`UnauthorizedError`、`TokenInvalidError`、`TokenExpiredError`、`InternalError`）。状态码挂在异常上。 |
| `common/errors/handlers.py` | `register_exception_handlers` / `to_error_details`：把异常翻成统一的 `ErrorResponse` 形状。 |
| `common/middlewares/__init__.py` | 汇总导出中间件。 |
| `common/middlewares/request_log.py` | `RequestLogMiddleware`：每次请求生成编号、写访问日志一行（方法 / 路径 / 状态码 / 耗时 / trace_id）。 |
| `common/dependencies.py` | 路由通用依赖 `trace_id_of`：取本次请求的编号。 |
| `common/encoding.py` | base64 编解码：哈希串与令牌共用。 |

---

## 3. api/ —— 入口层（认识 FastAPI）

> 只管「对外怎么说」：路由、依赖注入、请求 / 响应 schema。业务怎么算在 `services/`。

### 3.1 入口层总览

| 文件 | 作用 |
|---|---|
| `api/__init__.py` | 入口层总览，导出 `auth_router`、`profile_router`、`onebot_router`、`bots_router`、`owners_router`、`workflow_router`、`log_router`。 |
| `api/auth/__init__.py` | 鉴权入口汇总（注册 / 登录 / 改密码 / 当前用户 / 登录设备）。 |
| `api/profile/__init__.py` | 个人设置入口汇总（`ProfileData` / `profile_router`）。 |
| `api/onebot/__init__.py` | OneBot 管理入口汇总（在线客户端列表 / 踢人 / 令牌签发与吊销；P5 起是兼容面）。 |
| `api/bots/__init__.py` | 机器人管理入口汇总（跨平台增 / 启停 / 删，列表 / 添加 / 启用停用 / 删除）。 |
| `api/owners/__init__.py` | 归属清单入口汇总（`OwnerData` / `owners_router`）：管理员的「按归属筛选」选项。 |
| `api/workflow/__init__.py` | 工作流管理入口汇总（`WorkflowStoreLike` / `workflow_router`）。 |
| `api/log/__init__.py` | 运行日志入口汇总（`LogData` / `log_router`）。 |

### 3.2 api/auth/ —— 鉴权入口

| 文件 | 作用 |
|---|---|
| `api/auth/router.py` | **HTTP 入口**：`POST <prefix>/auth/register`（注册新账号，201 + 资料、**不发令牌**）、`POST <prefix>/auth/login`（账号+密码换令牌）、`GET <prefix>/auth/me`（令牌换当前用户资料）、`PUT <prefix>/auth/password`（改密码：验当前密码 → 换新哈希 → **其他登录下线**，发起修改的那条留着）、`GET/DELETE `<prefix>/auth/sessions[/{token_hash}]`（登录设备列表 / 吊销一条）。自身不含业务判断，只翻译请求 / 装配响应。 |
| `api/auth/dependencies.py` | 路由注入件：`get_auth_service`（从 `app.state` 取服务）、`bearer_scheme`/`BearerDep`/`AuthServiceDep`。 |
| `api/auth/requests.py` | 请求体 `RegisterRequest`（注册：账号 + 密码 + 昵称）、`LoginRequest`（登录）、`ChangePasswordRequest`（改密码：当前密码 + 新密码），字段复用 `services.user.validation` 的 `Account` / `Password` / `Nickname`。 |
| `api/auth/responses.py` | 响应体 `LoginData`（令牌 + 有效期 + 用户资料）、`ChangedPasswordData`（改完下线了几条）、`SessionData` / `RevokeSessionData`（设备列表与吊销），用户资料复用 `UserProfile`。 |

> **对外那个 id 叫 `token_hash`，不叫 `session_id`**：它就是**令牌摘要**（`sha256(令牌明文)`），
> 同时也是 `auth_sessions` 表的**主键列名**。原本两处各起一个名字，看起来像两样东西，其实
> 是同一个值；统一成 `token_hash` 之后，看到名字就知道拿的是什么、从哪来。
> 明文令牌只存在于 Cookie / `LoginData.token` 里，**不进库、不进 URL**（访问日志会记 path）。

> **登录会复用旧令牌**：请求体可选 `previous_token`（浏览器**不用传**——旧令牌在 HttpOnly
> Cookie 里，JS 读不到，后端自己从 Cookie 取）。认得出、且属于同一个账号时，就延长它的有效期、
> 重写缓存，**不新建会话**（`reused=true`，`token` 与上次相同，设备列表不会因此多出一条；本次
> 勾没勾「记住设备」会同步进设备记录）。认不出来（不在 / 不是本人）就照常发一个新的——复用是
> 尽力而为的优化，**失败不影响登录**。实现见 `services/session/service.py` 的 `reuse()`。

### 3.3 api/profile/ —— 个人设置入口

> 改昵称 + 头像。**写操作只作用于自己**（`user.user.id`）：这组接口就是「个人设置」，不存在
> 「改别人资料」，所以不用判管理员。按 id 取**别人**的头像放行给所有登录用户 —— 头像本来就是
> 给人看的（设备 / 用户列表要画），不算泄露。
>
> 头像的**字节流不套 JSON 壳**：`PUT` 的请求体就是图片本身，`GET` 回的就是图片本身（带
> `ETag` / `Last-Modified`，浏览器拿 `If-None-Match` 回来就换 304）。类型按**文件头**认
> （`Content-Type` 只当提示）：只收 PNG / JPEG / WebP / GIF，**SVG 不收**（那是 XML，能带脚本）。
> 存哪由 `AvatarStore` 协议决定，默认实现落 `[api] avatar_dir` 那个本地目录。

| 文件 | 作用 |
|---|---|
| `api/profile/router.py` | **HTTP 入口**：`GET/PATCH <prefix>/profile`（资料 / 改昵称）、`PUT/DELETE <prefix>/profile/avatar`（传 / 删头像）、`GET <prefix>/profile/avatar[/{user_id}]`（取自己 / 别人的头像字节）。只管翻译，规则在 `services/profile`。 |
| `api/profile/dependencies.py` | 路由注入件：`get_profile_service`（从 `app.state` 取服务）、`ProfileServiceDep`。 |
| `api/profile/requests.py` | 请求体 `UpdateProfileRequest`（昵称，复用 `services.user.validation` 的 `Nickname`）。 |
| `api/profile/responses.py` | 响应体 `ProfileData`：资料 + 头像信息（`has_avatar` / `avatar_mime` / `avatar_size` / `avatar_updated_at`，以及带 `?v=` 的 `avatar_url`，换图后前端好刷新）。 |

### 3.4 api/onebot/ —— OneBot 管理入口

> 把 OneBot 的「在线客户端列表」与「令牌管理」做成 HTTP 接口。服务本身在 `tickneko.onebot`，
> 由主程序装配时传进 `create_app(onebot=...)`；**这里不 import `tickneko.onebot`**（那样等于
> 装 api 就必装 websockets），只按 `protocols.py` 里的结构化协议取用。
>
> **P5 起的兼容面**：新流程统一走 `/api/bots/*`（见 3.5），这组 `/api/onebot/*` 保留给旧
> 客户端 / 旧流程（旧令牌 `nbo_` 仍能连）。

> **登录了还要看身份**：这几个接口是**全局**操作（踢任意账号的客户端、给任意账号签令牌），
> 只做认证等于把所有人的机器人交给每一个登录用户。带 `admin` 角色的人**不限**（能管所有账号），
> 其余人**只限自己那个账号**（`user.account`）。判管理员用 `is_admin()`，能不能碰某账号由
> `may_touch()` / `ensure_can_touch()` 把关。普通用户照样能用这些接口管自己名下的机器人，只是碰不到别人的。
>
> 两种越界的出口**故意不同**：调用方自己写出来的账号名（`?account=`、签发请求体里的
> `account`）回 **403**，说清楚"你不能碰这个账号"；按 **id** 找东西（`{client_id}` /
> `{token_id}`）回 **404**，越界与不存在走同一个出口 —— 给 403 等于确认"这条 id 存在"，
> 拿 id 就能数出别人有几条令牌（同 `/auth/sessions` 吊销别人会话的处理）。

| 文件 | 作用 |
|---|---|
| `api/onebot/router.py` | **HTTP 入口**：`GET <prefix>/onebot/clients`（在线列表，可 `?account=` 过滤）、`DELETE <prefix>/onebot/clients/{id}`（踢下线，`?revoke=true` 连令牌一起吊销）、`GET/POST <prefix>/onebot/tokens`（列表 / 签发）、`PATCH <prefix>/onebot/tokens/{id}`（启用 / 停用，停用会断开客户端）、`DELETE <prefix>/onebot/tokens/{id}`（吊销并断开）。全部要求登录，并由 `ensure_can_touch()` / `may_touch()` 按身份收范围。 |
| `api/onebot/protocols.py` | 结构化协议：`OneBotLike` / `TokenRegistry` / `ClientLike` / `TokenLike`（数据成员写成**只读属性**，对面是冻结数据类）。靠它做到两边互不 import。 |
| `api/onebot/dependencies.py` | 路由注入件：`get_onebot`（从 `app.state` 取服务，没接入回 503）、`CurrentUserDep`（要求登录，401）；以及授权：`ADMIN_ROLE` / `is_admin` / `may_touch` / `ensure_can_touch` / `owner_filter_of`（列表按归属筛：普通用户永远只看自己）。 |
| `api/onebot/requests.py` | 请求体 `IssueTokenRequest`（给哪个账号签、备注）。 |
| `api/onebot/responses.py` | 响应体：`ClientData` / `TokenData` / `IssuedTokenData`（明文令牌只在这一次出现）/ `KickData` / `RevokeData`。 |

### 3.5 api/bots/ —— 机器人管理入口（跨平台泛化版）

> 把「机器人」管理做成 HTTP 接口：一个用户添加一个机器人 = 一行「凭证行」（``tickneko.bots`` 的
> ``bot_credentials``：``platform`` + ``owner_id`` + ``bot_id`` 复合键，一个用户可多个机器人）。
> OneBot 行是随机令牌（反向 WS 握手按 token 认归属）、Kook 行是用户自填的 Bot Token（可逆
> 加密落库、连接时解密）。实现走 ``tickneko.bridge.manager`` 的 ``BotManager``，接口层只认
> ``protocols.py`` 里的 ``BotsService`` 协议——平台差异（Kook 的启停要 start / stop 正向 WS
> 客户端）封在实现里，这里不 import 任何平台包。

> **接口语义不硬套**：统一成「增 / 启停 / 删」，不套 OneBot 的「踢 / revoke」——Kook 单 bot
> 没有「多归属客户端」概念，Kook 的停用 / 删除就是停 / 注销正向 WS 客户端。旧
> ``/api/onebot/*`` 保留为**兼容面**（P5-4 起旧客户端用旧令牌仍能连），新流程都走这里。

| 文件 | 作用 |
|---|---|
| `api/bots/router.py` | **HTTP 入口**：`GET <prefix>/bots`（列表 + 归属昵称 + 在线状态；管理员可 `?owner_id=` 缩到某个归属）、`POST <prefix>/bots`（添加，`platform` 选 onebot / kook）、`PATCH <prefix>/bots/{id}`（启用 / 停用）、`DELETE <prefix>/bots/{id}`（删除）。全部要求登录并按身份收范围。 |
| `api/bots/protocols.py` | 结构化协议 `BotsService`（增 / 启停 / 删 + 在线聚合）+ `OnlineBot`（在线那一格）。记录形状复用 onebot 的 `TokenLike` / `IssuedLike`（一条凭证不含明文是跨平台的）。 |
| `api/bots/dependencies.py` | 路由注入件：`get_bots`（从 `app.state` 取服务，没接入回 503）、`BotsDep`。 |
| `api/bots/requests.py` | 请求体 `AddBotRequest`（`platform` 缺省 onebot；kook 必填 `token`）、`SetBotEnabledRequest`。 |
| `api/bots/responses.py` | 响应体 `BotData`（一条记录，**不含明文**）/ `IssuedBotData`（明文只在签发那一次出现）。 |

### 3.6 api/owners/ —— 归属清单

> 管理员能看到所有人的工作流 / 机器人，列表要按归属筛（`?owner_id=`）—— 筛之前得先知道
> 「有哪些归属」，就是这一组。**普通用户问「有哪些归属」，答案只有他自己**：口径与列表接口
> 一致（`owner_filter_of`），多一个接口不会因此多看得见别人的东西。

| 文件 | 作用 |
|---|---|
| `api/owners/router.py` | **HTTP 入口**：`GET <prefix>/owners`（管理员 = 全部用户，按账号排序；普通用户 = 只有自己）。要求登录（未登录 401）。 |
| `api/owners/responses.py` | 响应体 `OwnerData`（`owner_id` / `account` / `nickname`）：前端下拉与列表里显示归属都用它。 |

### 3.7 api/workflow/ —— 工作流管理入口

> 工作流是**另一块业务**：图形 / 校验 / 落库的实现都在 `tickneko.workflow`，接口层只认一份能力
> 协议 `WorkflowStoreLike`（见 `protocols.py`），装配时由主程序挂到 `app.state.workflow_store`。
> 与 OneBot 那组同一个路子，区别是**数据对象仍用 `tickneko.workflow` 的模型**
> （`WorkflowDefinitionRecord` / `WorkflowVersionRecord`）：那是两边的**契约**（字段语义、
> JSON 形状），不是实现，再复制一层 DTO 只会多两处要对齐。

| 文件 | 作用 |
|---|---|
| `api/workflow/router.py` | **HTTP 入口**：`GET <prefix>/workflows/node-types`（节点类型目录：面板 / 端口 / 配置表单都照它渲染，只读内存注册表、不碰库）、`POST <prefix>/workflows/validate`（只校验不入库，画布里随时试）、定义增删查改（列表每条带归属昵称 `owner_name`，管理员可 `?owner_id=` 缩到某个归属）、`PUT/GET /{id}/draft`（暂存区）、`POST/GET /{id}/versions`（提交一个版本 / 版本历史）、`POST /{id}/publish`（发布）、`GET /{id}/published`（已发布的那一份：开关 + 版本 + 图）、`PUT /{id}/enabled`（拨运行开关：**发布 ≠ 运行**，默认不跑）、`PUT /{id}/settings`（改设置：单实例 / 多实例；开着开关的即时按新设置重新登记）。校验不过是**业务结果**：HTTP 200 + `{valid:false, stage, errors}`（前端按节点画红点），请求格式错才走全局 422；`valid=false` 时**不写任何数据**。 |
| `api/workflow/protocols.py` | `WorkflowStoreLike`：接口层用到的那部分存储能力（定义增删查改 + 暂存 / 版本 / 发布 / 拨开关 / 改设置 + 启动兜底建表）。默认实现 `tickneko.workflow.SqlWorkflowStore` 结构化满足它。另有 `WorkflowTriggerLike`（运行时的 `start` / `stop`，**可空**：注入了拨开关才即时启停）。 |
| `api/workflow/dependencies.py` | 路由注入件：`get_workflow_store`（从 `app.state` 取）、`get_in_scope`（按 id 取 + 归属把关，**越界与不存在同为 404**，不拿 id 试探别人的东西）、`is_admin` / `owner_filter_of`（普通用户只看自己，管理员可跨归属；与 OneBot 那份同口径，改一处要同步另一处）。 |
| `api/workflow/requests.py` | 请求体：新建 / 改名 / 暂存 / 提交版本 / 发布 / 拨开关 / 改设置。 |
| `api/workflow/responses.py` | 响应体：`ValidationIssueData` / `WorkflowData`（含归属昵称 `owner_name`）/ `WorkflowDraftData` / `WorkflowVersionData` / `SaveVersionResultData`，以及节点目录的 `NodeCatalogData` / `NodeTypeData` / `NodeFieldData` / `NodePortData`（从注册表的 `NodeSpec` 映射，`MISSING_DEFAULT` 在这里翻成 `has_default=false`）。 |

### 3.8 api/log/ —— 运行日志检索

> 把「查历史日志」做成一个 HTTP 接口。**查询本身不在这里实现**：条件原样递给日志系统的
> `BaseLogger.search()`，它再扇出到各出口 —— 落库那份就是一条 SQL（`WHERE` / `ORDER BY` /
> `LIMIT` 全在数据库那边做完，不是捞回来再筛）。出口怎么接、日志怎么落，见 `tickneko.db` 与
> `tickneko.core.logger`。

> **登录了还要看范围**：日志里有别人的东西，所以带 `admin` 角色的人**不限**，其余人**只看得见
> 自己名下**的（按记录里的 `owner_id` 过滤，即 `user.user.id`）；显式写别人的归属 → **403**
> （和 OneBot 那组接口一个口径：自己写出来的参数越界就说清楚）。

> **参数先校验再透传**：级别名、时间格式、来源类别、出口名写错当场回 **422**。这一步省不得 ——
> 日志系统内部把出口的失败**吞掉并记账**（一个出口崩了不影响别的），写错的参数会在出口里被吞掉，
> 客户端只看到「一条都没有」，比报错难查得多；出口名尤其要挡：日志系统对**不认识的出口名是直接
> 忽略的**，`processors=local`（实际那份叫 `file`）于是与「真没有日志」表现完全一样。控制台出口
> 那条「本出口不支持检索」的提示记录也会被滤掉（那不是日志，是给 REPL 里的人看的）。

> **默认只查落库那份**（`database` 出口，见 `DEFAULT_PROCESSOR`）：查历史日志以库（SQL）为准
> —— 控制台不留存，文件那份是给人在本机翻的；库出口没开（`[logging.database] enabled = false`）
> 时回 **503 并说清楚**，不然只会静默返回一个空列表，比报错难查。

> **来源说类别，不说名字**：界面上的「落库 / 本机文件 / 两者」用 `?source=`（`database` / `file` /
> `all`），后端按出口**类型**认 —— 文件出口叫什么由装配层定（核心 `file`、接口层 `api.file`、
> OneBot `onebot.file`），`file` 一类全查上；要精确到某一路才用 `?processors=` 给名字，两者
> 二选一（同写 → 422）。**这一步只有管理员能做** —— 来源是全局选择，普通用户只能查默认那份
> （想指定别的来源 → 403；写成默认那份不报错，那跟不写是一回事）。

| 文件 | 作用 |
|---|---|
| `api/log/router.py` | **HTTP 入口**：`GET <prefix>/logs`（`level` / `logger_name` / `owner_id` / `query` / `start` / `end` / `limit` / `offset` / `source` / `processors`），条件原样交给 `logger.search()`，结果按范围收窄；`MAX_LIMIT` 限一次最多给多少条，`DEFAULT_PROCESSOR`（`database`）是不指定出口时的默认，`SOURCE_KINDS`（`database` / `file`）是界面那三个来源选项按类型认出口的依据。 |
| `api/log/dependencies.py` | 路由注入件：`get_app_logger`（从 `app.state.logger` 取日志实例，它与日志核心共享出口注册表，所以查得到所有出口）。 |
| `api/log/responses.py` | 响应体 `LogData`（结构化字段原样给出：级别 / 模块 / 所有者 / 附加字段 / 异常栈）。 |

---

## 4. services/ —— 业务层（不认识 FastAPI）

> 只算「业务怎么办」：查人、验密码、签令牌。失败抛 `common.errors.ApiError`。

### 4.1 业务层总览

| 文件 | 作用 |
|---|---|
| `services/__init__.py` | 业务层汇总导出。 |
| `services/auth/__init__.py` | 鉴权业务汇总（凭据换令牌；令牌机制在 `session/`）。 |
| `services/user/__init__.py` | 用户模块汇总。 |
| `services/session/__init__.py` | 会话模块汇总（登录会话、设备信息、令牌映射、吊销）。 |
| `services/profile/__init__.py` | 个人设置模块汇总（昵称 + 头像、头像存储协议与默认实现）。 |
| `services/variables/__init__.py`、`service.py` | 逻辑变量发现、节点来源反查与查看器探测 / 分派；协议与接口见 [变量文档](../../docs/variables/variables.md)。 |

### 4.2 services/user/ —— 用户域（不管 HTTP）

| 文件 | 作用 |
|---|---|
| `services/user/models.py` | `UserRecord`（内部形状，**带 `password_hash`**）/ `UserProfile`（对外资料，**无密码字段**）/ `profile_of`（两者转换，显式决定露不露字段），以及角色常量 `USER_ROLE`（注册出来的账号默认带它；管理员判定另有 `ADMIN_ROLE`）。 |
| `services/user/validation.py` | 账号 / 密码 / 昵称规则：`Account` / `Password` / `Nickname`（pydantic `AfterValidator`，`SecretStr` 包密码）。登录、注册共用一份，「改资料」将来也用它。 |
| `services/user/protocols.py` | 能力协议（只声明不实现）：`UserStore`（按账号 / 按 id 查人、批量 `get_by_ids`、管理端出归属清单的 `list_all`，外加注册那一笔 `add` 与改密码的 `set_password`，异步）、`PasswordHasher`（hash / verify）。 |
| `services/user/security.py` | 默认实现 `Pbkdf2PasswordHasher`：PBKDF2-SHA256，串自带算法 / 迭代 / 盐 / 摘要，定长比对。 |
| `services/user/store_sql.py` | 落库实现 `SqlUserStore`：`UserTable`（SQLModel）声明表结构与约束，DDL 由 SQLAlchemy 按方言生成（sqlite / mariadb 同一份定义），查询走 `AsyncSession`，**不手写 SQL**；`ensure_schema` 建表、`list_all` 列全部用户（归属清单用）、`set_password` 换密码（只改一列）、`add` 新增一个账号（注册用，**默认带 `USER_ROLE`**；撞 `account` 唯一约束时翻成 `AccountAlreadyExistsError`，不把数据库异常漏出去）、`seed_demo` 空表种演示账号（只有一个 `admin`；要别的账号走注册接口）。 |
| `services/user/demo.py` | 演示账号 `DEMO_USERS` 的单一来源（`seed_demo` 用），改账号只改一处。 |

### 4.3 services/profile/ —— 个人设置域

> 改昵称 + 头像。昵称落在用户表（`services/user`），头像**字节**落在头像存储 —— 两样拼在
> 一起，是因为个人设置页一次要「资料 + 头像」。

| 文件 | 作用 |
|---|---|
| `services/profile/models.py` | `AvatarInfo`（元信息：类型 / 大小 / 更新时间，`updated_at` 兼作 `ETag`）/ `ProfileView`（资料 + 头像）。 |
| `services/profile/images.py` | 图片嗅探 `sniff_image_type`：按**文件头**认类型（不信 `Content-Type`），只收 png / jpeg / webp / gif —— SVG 这类 XML 不收（能带脚本）。 |
| `services/profile/protocols.py` | **能力协议** `AvatarStore`（`save` / `info` / `load` / `remove`）：换底层（对象存储 / 塞库 / 加 CDN）只换实现，上层一行不用改。 |
| `services/profile/store_file.py` | 默认实现 `FileAvatarStore`：一个目录一个用户一个文件（`<user_id><扩展名>`；临时文件 + `os.replace` 原子替换、同用户换类型清旧文件、文件 IO 丢线程池、user id 先按文件名规则挡住 `../`）。 |
| `services/profile/service.py` | **业务编排** `ProfileService`：改昵称与头像的存 / 取 / 删；规则（空 → 422、超限 → 413、不是认得的图 → 415）只在这里判一遍。 |

### 4.4 services/auth/ —— 鉴权业务

| 文件 | 作用 |
|---|---|
| `services/auth/models.py` | 业务层 I/O（不认识 HTTP）：`Credentials`（凭据输入）、`LoginResult`（签发结果）、`CurrentUser`（当前登录者 + 资料）。 |
| `services/auth/service.py` | **业务编排** `AuthService`：`register`（开新账号：查重 → 哈希 → 落库，账号被占抛 409）与 `login`（查人 → 比密码 → 查停用 → 开会话发令牌）；以及 `change_password`（验当前密码 → 写新哈希 → 吊销其他登录）与 `current_user`（令牌 → 查人）。依赖走 `services.user` 与本模块协议，换库 / 换算法 / 换令牌形式都不用改这里。 |

---

## 5. 速查：新东西放哪

| 要加的东西 | 放哪 |
|---|---|
| 新路由 / 新 HTTP 接口 | `api/<业务>/router.py` |
| 请求 / 响应字段定义 | `api/<业务>/requests.py`、`responses.py` |
| 路由里取服务 / 解析 token 的依赖 | `api/<业务>/dependencies.py` |
| 查库、验密、算令牌等纯逻辑 | `services/<业务>/service.py` 及同层 `*.py` |
| 业务自己的数据模型 / 协议 | `services/<业务>/models.py`、`protocols.py` |
| 头像这类「要换底层」的文件存储 | `services/profile/protocols.py` 定协议 + `store_file.py` 给默认实现；目录来自配置 `[api] avatar_dir` |
| 跨业务共享（响应壳 / 错误 / 日志 / 编码） | `common/` |
