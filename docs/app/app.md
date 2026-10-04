# 应用入口与配置：`app.py` / `config.py`

仓库根目录这两个文件不属于 `tickneko` 包 —— 它们是**进程的起点**，一个管「按什么顺序把东西
建起来」，一个管「配置怎么读、错了怎么报」。

| 文件 | 管什么 | 刻意不做什么 |
|---|---|---|
| `app.py` | 解析命令行 → 读配置 → **按配置建日志核心与数据库引擎** → 交给 `tickneko.bootstrap` 装配业务 → 等停机 | 顶层不 import 任何业务模块（原因见 §1.1） |
| `config.py` | 读 TOML、用 pydantic 校验、产出 `Settings`；派生值（Kook 凭证密钥）也在这儿 | 不做装配、不碰数据库 |

两个文件的模块 docstring 只留一句话定位，细节都在这里。

---

## 1. `app.py` —— 启动顺序

一句话：**本文件只做核心初始化**，业务一律交给 `tickneko.bootstrap`。

```
main()  →  _main()
  1. 解析命令行、读 data/config.toml（Settings.load）  配置错 → [配置错误] + 退出码 2
  2. setup_logging(settings)                          建日志核心 + 建库引擎 + 探一次库
  3. from tickneko.bootstrap import run, ...           ← 到这里才 import 业务模块
     await run(engine=..., api=..., onebot=..., kook=...)
  4. await serve_forever()                            主协程停在这；端口被占当场报错退出
  5. 停机：await shutdown() → 关日志库连接
```

### 1.1 为什么先建日志核心（这一刀切在哪）

日志核心必须在**任何业务模块被 import 之前**按配置建好。`tickneko` 里有模块级
`default_core().child(...)`（导入即执行）——谁先被 import，谁就顺手把进程默认核心按**默认
参数**建出来，配置里的颜色 / 级别就此定死、再也传不进去（`LogManager.configure` 在「核心已
存在」时只会合并 processors）。

所以：`app.py` 顶层**不 import 任何业务模块**，`tickneko.bootstrap` 也是建好核心之后才在函数
里导入。初始化只用日志门面的两个动作：

```python
configure(...)         # 建（或复用）进程默认核心
await core.start()     # 起来之后，业务模块取到的实例才带得上这些出口
```

### 1.2 连不上数据库，当场报错

引擎建好后立刻 `SELECT 1` 探一次（`_probe_database`）。SQLAlchemy 的引擎是**惰性**的
（`create_async_engine` 只建连接池），不主动探一下，连库失败会推迟到日志出口建表时 —— 那里
异常被日志核心吞掉，用户视角只剩一行乱码 traceback，等于「什么提示都没有」。

探测失败抛带 `host:port/db` 的异常，由入口统一打成 `[初始化错误]` 干净退出（退出码 2）。
MariaDB 连接还带 `connect_timeout=5`：默认驱动会一直重试到系统级超时（实测十几秒），到期
五秒内报错。

### 1.3 停机：Ctrl+C 之后发生什么

主协程被取消 → 业务侧收尾（停服务、等调度器、冲刷日志余量、关库连接）→ 再关日志库连接
（`[logging.database]` 单独连了另一个库时那个引擎也在收尾清单里），安静退出不吐 traceback。

**收尾期间再按一次 Ctrl+C 才是强杀**（第一次是「好好收」）。

### 1.4 服务都在哪儿跑

接口层（`tickneko.api`，约定用 `tickneko.api`）随主程序由 uvicorn 起成 HTTP 服务，和 OneBot
同进程、同事件循环；监听地址在 `[api]` 的 `host` / `port`。OneBot 反向 WS 同样随主程序起，
监听 `[onebot]` 的 `host` / `port`（端口默认 `16700`）；它的日志单独落 `logs/onebot.log`
（共用同一个日志核心，只是换了个文件出口）。

两处的监听地址默认都是 `0.0.0.0`：它们都在「等服务上门」（浏览器连控制台、OneBot 实现连反向
WS），只听回环的话容器 / 局域网里就够不着 —— 只在本机用再改回 `127.0.0.1`。

装配与停机顺序的细节在 `tickneko/bootstrap.py`，逐层职责见 [docs/README.md](../README.md)。

### 1.5 怎么运行、依赖什么

```bash
python app.py                     # 读 data/config.toml，不存在则按默认值启动
python app.py -c path/to.toml     # 指定配置文件
```

依赖：`tickneko[api]` + `tickneko[onebot]`（`fastapi` / `uvicorn` / `sqlmodel` / `aiosqlite` /
`websockets`）。

---

## 2. `config.py` —— 配置怎么读、怎么报错

配置文件是 TOML（`#` 写注释），模板 `config.toml.example`，**复制成 `data/config.toml` 才生效**；
那份已进 `.gitignore`（整个 `data/` 都不入库）。

**为什么住 `data/` 而不是仓库根**：配置是运行时数据，跟 sqlite（`data/tickneko.db`）、Kook 密钥
（`data/secret_key`）、上传的头像同处一地 —— 备份 / 搬迁 / 容器挂载都只搬 `data/` 一个目录，不会
漏掉某一项；容器里 `data/` 是**按目录挂**的，容器才写得进去，「首次启动照模板生成一份配置」才有
可能。

### 2.1 区域 ↔ 模型（一节一块）

| 配置区域 | 对应模型 | 管什么 |
|---|---|---|
| `[app]` | `Settings.app` | 进程名、调试开关 |
| `[database]` | `Settings.database` | 整项目共用的数据库连接 |
| `[logging]` | `Settings.logging` | 级别、控制台、队列 |
| `[logging.file]` | `...logging.file` | 本地文件出口（目录 / 前缀 / 分片 / 保留） |
| `[logging.database]` | `...logging.database` | 数据库出口 |
| `[logging.queue]` | `...logging.queue` | 异步队列与分发器 |
| `[cache]` | `Settings.cache` | 缓存总控：用哪个后端 |
| `[cache.redis]` | `...cache.redis` | Redis 连接（`backend = "redis"` 时才用） |
| `[api]` | `Settings.api` | 接口层：监听地址、路由前缀、令牌有效期、头像存储 |
| `[onebot]` | `Settings.onebot` | 反向 WS 接入：监听地址、路径、令牌 |
| `[kook]` | `Settings.kook` | 正向 WS 接入：连法调优 + 凭证加密密钥 |

**没有 `[scheduler]`**：调度由工作流的定时触发器（`trigger-time`）按 cron 登记，不读这一层配置。

### 2.2 数据库的三层逐项覆盖

`[logging.database]` 的**连接类**项默认整项继承 `[database]`，日志特有的项（`enabled` /
`buffer_size` / `flush_interval`）只在这一节；要给日志单连另一个库，就在这一节里覆盖同名项，
覆盖粒度是**逐项**的：

```
[logging.database] 专用项  >  [database] 公共项  >  代码里的字段默认值
```

合并结果放在 `Settings.logging.database.connection`（业务用的连接则直接是
`Settings.database`）。

### 2.3 容错与报错口径

**能跑就跑**：文件不存在、某一节或某一项没写，都按字段默认值补齐。只有**值写错**才抛
`ConfigError` —— 级别名拼错、该填整数填了字符串、`driver` 不在 `sqlite` / `mariadb` 里、端口
越界。好处是拼错在启动阶段就报出来，而不是静默用默认值跑出「配了没生效」这种更难查的问题。

校验交给 pydantic（类型、取值范围、路径解析都写在字段旁边，不用手写 `isinstance` 那一套）；
报错由 `_describe` 翻成统一的中文提示，**带上完整出处**（如 `logging.database.port`）—— 覆盖
之后光看键名已经不知道值写在哪个节里了：

```
logging.database.port 要1-65535 的端口，收到 70000
```

**废弃键直接报错并指路**（`_Region.legacy_keys`），不静默忽略：改成别的键的写「改用 X」，
已经不需要的写「把这一项删掉即可」+ 现在该去哪配，例如：

```
logging.file.path 已不再使用：改用 dir（那时是单个文件，现在按目录 + 前缀分片）
kook.token 已不再使用：把这一项删掉即可，Bot Token 改在接口层 /api/bots 添加（落库加密）
```

用法：

```python
from config import ConfigError, Settings

try:
    settings = Settings.load()  # 默认读 data/config.toml
except ConfigError as exc:
    ...  # 报给用户，别拿默认值糊过去
```

### 2.4 派生值：Kook 凭证密钥

`[kook].secret_key` 留空时，用 `load_or_create_secret_key()` 读 / 生成
**`data/secret_key`**（0600）：

* 填了就用填的；
* 留空则读那个文件，文件不存在（或为空）就生成一份写进去。

**为什么必须落盘**：这个密钥加密的是**已经落库**的 Bot Token（`/api/bots` 添加机器人时写入
`bot_credentials`），换密钥等于换锁 —— 库里那些密文全都解不开，机器人得重新添加。所以不能
每次启动随机生成。`app.py` 在装配前把这份密钥注入 `[kook]` 配置，`bootstrap` 再拿它解密逐
个连。

---

## 3. 改东西时该动哪（速查）

| 想做的事 | 改哪里 |
|---|---|
| 加一项配置（新区域 / 新字段） | `config.py` 加字段（带 `Field(...)` 描述与取值校验）+ `config.toml.example` 补注释项；测试见 `tests/test_config.py` |
| 停用 / 改名一项配置 | `config.py` 的 `legacy_keys` 写清处理办法（报错指路），别静默忽略 |
| 启动顺序 / 收尾顺序 | `app.py`（核心初始化）与 `tickneko/bootstrap.py`（业务装配、停机） |
| 装配层要读新配置 | `app.py` 取出来传参给 `bootstrap.run(...)` —— 包内一律不读配置文件 |
| 进程默认日志核心 | `app.py` 的 `setup_logging()`（按 `[logging]` 建出口与队列） |
