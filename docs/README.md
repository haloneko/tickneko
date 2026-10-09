# TickNeko 文档总目录

> 这里是 `docs/` 的总入口：**按模块查文档见「文档索引」，想先看清整个框架见「框架总览」**。
> 每个模块的「为什么」都收在对应文档里，代码里的模块 docstring 只留一句话定位并指回这里。
>
> 项目名 **TickNeko**，Python 包名 `tickneko`（文档里写路径 / import 时用小写包名）。

---

## 1. 文档索引

| 文档 | 覆盖的模块 | 主要内容 |
|---|---|---|
| [app/app.md](app/app.md) | 根目录 `app.py` / `config.py` | 启动顺序与停机收尾、数据库探测、配置区域与三层覆盖、报错口径、Kook 密钥的派生规则 |
| [logger/logger.md](logger/logger.md) | `tickneko.core.logger` | 异步日志：root + child / bind / route、目标与过滤器、检索与刷新、进程门面 |
| [cache/cache.md](cache/cache.md) | `tickneko.core.cache` + cache 节点 | 一套 API 两种后端（Redis / 内存）、后端协议约定、降级与命名空间、配置注入；cache 节点的键前缀 / 作用域 / get-set 口径（文末「缓存节点」） |
| [variables/variables.md](variables/variables.md) | 节点查看器 + 变量接口 + 前端编辑器 | IoC 注册与探测、四种展示类型、全量保存、ds-list 双键族频次联动、旧 idx 按需重建 |
| [scheduler/scheduler.md](scheduler/scheduler.md) | `tickneko.core.scheduler` | cron 语法、单 / 多实例、错过不补、失败隔离、红黑树排程索引 |
| [bridge/bridge.md](bridge/bridge.md) | `tickneko.platforms.bridge` | 规范化事件 + 适配器协议 + Gateway 总线、OneBot / Kook 两个适配器、怎么写第三个平台 |
| [workflow/workflow.md](workflow/workflow.md) | `tickneko.workflow` | 模块索引、节点契约与注册即校验、写自己的节点（第 5 节）、各模块设计要点（第 7 节）、**从画布到运行：保存版本 → 发布 → 运行开关**（第 8 节，含画布截图） |
| [deploy.md](deploy.md) | 打包与部署 | Docker 单镜像 / compose、一键打包脚本（`scripts\build-all.bat` / `.sh`）、不用 Docker 的跑法、容器里的配置与卷、上线前检查 |

包内还有一份接口层索引 `tickneko/api/MODULES.md`（尚未搬进 `docs/`）。

跟代码无关的项目文档在根与 `.github/` 下 —— GitHub 认这两个位置，放那儿不影响仓库页的展示：

| 文档 | 位置 | 内容 |
|---|---|---|
| 贡献指南 | [`.github/CONTRIBUTING.md`](../.github/CONTRIBUTING.md) | 环境准备、目录速览、代码约定、测试、提交规范 |
| 安全策略 | [`.github/SECURITY.md`](../.github/SECURITY.md) | 漏洞报告渠道、部署必关的门、已知设计取舍 |
| 行为准则 | [`.github/CODE_OF_CONDUCT.md`](../.github/CODE_OF_CONDUCT.md) | 社区行为准则 |
| 许可证 | [`LICENSE`](../LICENSE) · [`NOTICE`](../NOTICE) · [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES) | Apache-2.0 与第三方组件许可 |


---

## 2. 框架总览

### 2.1 分层

```
根目录  app.py / config.py        读 TOML、建引擎、起服务（不在 tickneko 包内）
          │  传映射（from_mapping）
          ▼
tickneko    bootstrap.py / wiring.py  组合根：装配各块 + 派发日志核心
          │
          ├── api/        接口层：6 组路由 + 业务服务（唯一认识 FastAPI 的层）
          ├── workflow/   编排层：图校验 → 落库 → 节点执行器（不 import FastAPI）
          ├── platforms/  平台层：onebot（反向 WS）/ kook（正向 WS）/ bridge（总线与协议）
          ├── bots/       凭证层：跨平台机器人凭证（bot_credentials，可逆加密）
          ├── db/         数据层：日志 LogStore 实现（logs 表）
          └── core/       核心层：logger / cache / scheduler（不依赖任何上层）
```

| 层 | 职责一句话 |
|---|---|
| `core/` | 与业务无关的地基：日志、缓存、定时调度三个子包 |
| `platforms/onebot` | OneBot **反向** WS（框架当服务端，等实现连进来），只管连接 + 协议 |
| `platforms/kook` | Kook **正向** WS（框架当客户端，主动连网关），发消息走 REST |
| `platforms/bridge` | 桥接层：把各平台适配器统一到「规范化事件 + 能力协议」，`Gateway` 是总线 |
| `workflow/` | 工作流编排：图校验流水线 + 定义 / 版本落库 + 节点执行器 |
| `api/` | 对外协议：请求校验、鉴权、6 组路由；内部再分 `api/api`（入口层）→ `api/services`（业务层） |
| `db/` | 数据库实现（`logs` 表），**自建不建引擎、不读配置** |
| `bots/` | 通用机器人凭证：一个用户一个机器人 = 一行凭证，跨平台 |
| `bootstrap.py` | 组合根：入口备好「日志核心 + 库引擎」后，由它把各块挂起来跑 |
| `wiring.py` | 把进程日志核心派发到各业务模块的日志槽位 |

### 2.2 依赖方向（单向、无环）

```
api ──► core / workflow          （api 不 import platforms / db，靠协议接收）
workflow ──► core（logger、scheduler）
bridge ──► core（logger）+ bots  （只有适配器实现 import 平台包）
core ──► 自身
db ──► core.logger 的模型        bots ──► 无 tickneko 依赖
```

- **core 只依赖自身**（`redis` 是函数内惰性 import，不装也能用）；
- **bridge 的协议 / 总线层不 import 平台包** —— 只有 `bridge/onebot.py` / `bridge/kook.py` 两个适配器 import；
- **platforms 运行时不 import api**（`TokenRegistry` 只在 `TYPE_CHECKING` 下引用，否则会被迫带上 fastapi）；
- **api 不 import platforms / db**：按 `OneBotLike` / `BotsService` / `WorkflowStoreLike` 协议收实现，缺了回 503；
- **workflow 不 import FastAPI**；**bots 不 import 任何 tickneko 包**。

### 2.3 装配链路（`bootstrap.run()`）

启动顺序：

1. `wire_loggers(default_core())` —— 先把日志核心派发给各模块；
2. `cache.configure(CacheOptions.from_mapping(...))` + `await cache.start()`；
3. 建库表与演示账号（`SqlBotStore` / `SqlUserStore` / `SqlSessionStore` / `SqlWorkflowStore`）；
4. 建 `Gateway()`，建 `OneBotAdapter` 并 `register`；
5. 有 `[kook].secret_key`（留空时入口自动生成 / 读取 `data/secret_key`）才建 `KookAdapter`
   （凭证行的 Bot Token 解密后逐个 `add_bot`；网关地址由客户端连接前自己 discover）；
6. 建 `BotManager`（跨平台增 / 启停 / 删）；
7. 建 `MessageRouter` 并挂上 `gateway.subscribe(on_platform_event)`；
8. `create_app(...)` 建 FastAPI 并起服务；
9. `await scheduler.start()`；
10. `await load_published_workflows(...)` 登记**开着运行开关**的已发布工作流。

停机顺序不可反：停 HTTP → 停 gateway → 停调度器 → 停缓存 → 停 manager（冲刷日志余量）→ 释放引擎。

**日志派发**：`wiring.wire_loggers(core)` 把核心存进 7 个模块的 `set_core` 槽位 ——
`core.cache.logging`、`core.scheduler.logging`、`platforms.bridge.logging`、`workflow.logging`、
`api.logging`、`platforms.onebot.logging`、`platforms.kook.logging`。
这些模块 `import` 零副作用，未装配就调便捷函数（如 `workflow_logger()`）**当场抛错**（fail fast）。

### 2.4 配置

- **读 TOML 的只有根目录 `config.py` 与 `app.py`**（不在 `tickneko` 包内）；`tickneko` 包内**没有任何模块读配置文件** —— 一律由上层用 `from_mapping(映射)` 注入（`ApiOptions` / `OneBotOptions` / `KookOptions` / `CacheOptions`）；
- **配置位置**：`data/config.toml`（模板 `config.toml.example`）—— 跟 sqlite / Kook 密钥 / 头像同住 `data/`，备份 / 搬迁 / 容器挂载只搬一个目录；容器里首次启动会照模板生成；
- 配置区域（`config.toml.example`）：`[app]`、`[database]`、`[logging]`（含 `[logging.file]` / `[logging.database]` / `[logging.queue]`）、`[cache]`（含 `[cache.redis]`）、`[api]`、`[onebot]`、`[kook]`；没有 `[scheduler]` —— 调度由工作流的定时触发器（`trigger-time`）按 cron 登记；
- 容错口径：文件不存在 / 缺项按默认值补齐，只有**值写错**才抛 `ConfigError`；废弃键直接报错而不是静默忽略。
- `app.py` 的启动顺序（为什么先建日志核心、数据库探测、停机收尾）与配置的细节见 [app/app.md](app/app.md)。

### 2.5 可选依赖（extras）

基础依赖只有 `pydantic>=2.7`，其余按能力装：

| extra | 装什么 | 给谁用 |
|---|---|---|
| `api` | fastapi、uvicorn、sqlmodel、aiosqlite | 接口层 |
| `onebot` | websockets>=13 | OneBot 反向 WS 服务端 |
| `kook` | websockets>=13、cryptography>=42 | Kook 客户端（Bot Token 落库可逆加密） |
| `redis` | redis>=5 | 缓存层 Redis 后端 |
| `mariadb` | aiomysql | MariaDB 异步驱动 |
| `workflow` | httpx>=0.27 | 工作流 http 节点 |
| `dev` | pytest、pytest-asyncio、ruff、httpx | 开发 / 测试 |

不装某个 extra 只影响对应那一块（比如没装 `redis` 就只能用本地内存缓存），框架其余部分照常可用。
