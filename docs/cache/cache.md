# 缓存：核心层（`tickneko.core.cache`）+ 节点层（cache 节点）

变量查看器、编辑接口与 ds-list 双键族联动见 [变量文档](../variables/variables.md)。

> 本模块文档是**核心层**（一套可换后端的键值存取设施）。缓存还有**第二层**：
> 工作流里的 `cache` 节点（`tickneko/workflow/nodes/cache.py`）—— 它负责把「跨执行记住
> 状态」这件事翻译成核心层的 `get` / `set` 调用，并自己定下**键前缀与作用域**。两层关系
> 与节点层的设计见文末「缓存节点」一节；节点层只碰核心层门面，不感知后端差异。

## 设计要点

- **一套 API，两种后端**：`backend = "redis"` 走 Redis（redis-py 异步客户端），
  `"memory"`（默认）走进程内存；两者实现同一个协议（`CacheBackend`），键是字符串、
  值分字符串 / 列表 / 哈希三种结构、TTL 按秒，语义一致 —— 换后端不用改上层代码；
- **业务代码只碰进程级单例** `cache`（`from tickneko.core.cache import cache`）；要一份
  独立的缓存就自己 `Cache()`（比如测试里）；
- **不隐式启动**：没 `start()` 就调数据接口会抛 `CacheError` —— 连不连得上应该在启动
  阶段就见分晓，而不是等到哪一次 `get` 才炸；
- **连不上不静默**：配了 Redis 但连不上，默认**当场抛错**并在报错信息里提示检查
  `config.toml` 的 `[cache.redis]`；只有显式打开 `fallback_to_memory` 才退回本地缓存并
  记一条 warning（要上报就问 `Cache.degraded`）；
- **命名空间**：Redis 上所有键都带 `<namespace>:` 前缀（多个应用共用一个实例时隔离），
  上层看到 / 传进来的键名始终是不带前缀的那一份；
- **配置由上层传**：`[cache]` 区域（见 `config.py` / `config.toml.example`）的映射由
  `CacheOptions.from_mapping` 转成选项，本层不 import `config` —— 和日志核心、调度器
  一样，配置由 `app.py` 传下来。

## 职责划分

| 模块 | 职责 |
| --- | --- |
| `core.Cache` | 缓存门面：抹平后端差异，定下「`ttl=None` 用哪个 TTL」 |
| `interfaces.CacheBackend` | 后端协议，两个实现都符合它 |
| `memory.MemoryCache` | 进程内存实现，也是 Redis 连不上时的降级兜底 |
| `redis.RedisCache` | Redis 实现（可选依赖 `pip install "tickneko[redis]"`） |
| `models` | `CacheOptions` / `RedisOptions` / `CacheError` |
| `manager.cache` | 进程级单例 |
| `logging` | 本层的日志接入点 |

## 快速开始

```python
from tickneko.core.cache import CacheOptions, cache

await cache.start()                     # 默认就是本地内存版：不启用 Redis 也能用
await cache.set("k", "v", ttl=60)
await cache.get("k")

await cache.list_push_right("queue", "a", "b")      # 列表
await cache.hash_set("user:1", {"name": "阿一"})    # 哈希
await cache.hash_get("user:1", "name")              # 单字段读
await cache.hash_exists("user:1", "name")           # 字段在不在（HEXISTS）
await cache.hash_length("user:1")                   # 字段数（HLEN，服务端 O(1)）
await cache.hash_keys("user:1")                     # 字段名列表（HKEYS，只搬名字不搬值）
await cache.type("user:1")                          # 结构类型："string" / "list" / "hash" / None
await cache.set_json("profile", {"tags": ["a"]})    # 嵌套结构走 JSON

cache.configure(CacheOptions(backend="redis", namespace="tickneko"))   # 要用 Redis
await cache.start()
```

`start()` / `stop()` 都是显式的、重复调用是空操作。

## 后端协议约定

两个后端必须一致，差异在协议层被抹平：

- **键**是普通字符串、**不带前缀**（前缀是 Redis 后端自己的事）；
- **值**有三种结构：字符串（`get` / `set`）、列表（`list_*`）、哈希（`hash_*`）。
  元素与字段值都是 `str` —— Redis 后端开 `decode_responses` 直接拿回字符串，上层不必
  解码字节；收到别的类型一律抛 `CacheError`，不会被悄悄塞进去。要存嵌套结构（哈希的
  哈希、对象、数组）就用 `Cache.set_json` 装成一段 JSON 文本；
- **结构不能混用**：一个键按哪种结构写入，就只能按那种结构访问，换一种访问抛
  `CacheError`（对应 Redis 的 `WRONGTYPE`）—— 唯一的例外是 `get_many`，它遇到非字符串
  键当作没取到（照 `MGET` 来）；
- **空结构不占键**：列表被弹空、哈希字段被删空之后，这个键就不存在了（与 Redis 一致）；
- **TTL** 是秒（`float`）：`set(..., ttl=None)` 表示永不过期，`ttl()` 用 `None` 表示键不
  存在、`math.inf` 表示永不过期；`list_*` / `hash_*` 上的 `ttl` **只在新建键时生效**，
  往已有键上追加不动它的过期时间（照 Redis 来：`RPUSH` / `HSET` 不刷新 TTL）；
- **批量**方法要么全做要么不做（Redis 后端走 pipeline / 单条多键命令），不返回半截结果；
- **生命周期**：`start` / `stop`，重复调用是空操作；出错一律抛 `CacheError`
  （驱动层异常不外泄）。

## 两个后端各自的注意点

**内存后端**：数据只活在当前进程里 —— 换进程就没了，也不跨机器共享，换来的是零依赖、
零网络；每条记录带类型标签，拿另一种结构访问同一个键抛 `CacheError`；过期键由后台任务
按 `sweep_interval`（默认 30 秒）扫掉。

**Redis 后端**：一条命令一个协程，不阻塞事件循环，连接池由驱动自己管；所有键拼上
`<namespace>:` 前缀，`keys()` 用 `SCAN` 游标遍历（`KEYS` 在大库上会阻塞整个 Redis），
返回前把前缀去掉；驱动的 `RedisError` 一律翻成 `CacheError`，上层不必 import redis 就能
接住缓存层的错。没装 redis 包时 `start()` 报 `CacheError` 并给出安装提示。

## 选项与配置

两份不可变 dataclass：`CacheOptions`（`backend` / `namespace` / `default_ttl` /
`fallback_to_memory` / `sweep_interval` / `redis`）与 `RedisOptions`（地址、账号、
超时、重试、连接池上限）。配置系统产出的是普通映射，由 `from_mapping` 转进来 ——
缺的项用默认值，多出来的键忽略（校验归配置系统管，报错也要报在配置那一层）。

## 日志接入

本层接的是 `tickneko.core.logger` 的进程门面，名为 `cache`（相对核心 `tickneko` ->
`tickneko.cache`）。**业务模块一律不直接 `default_core()`**：要日志实例就调
`cache_logger()`；装配层也可把实例经 `Cache(..., logger=)` 传入。核心由装配层
（`tickneko.bootstrap` 或 `tickneko.wiring.wire_loggers`）经 `set_core()` 存进本模块槽位；
`import` 本模块**零副作用**，没装配就调 `cache_logger()` 会当场抛错（fail fast），
不会默默按默认参数建一份把配置定死的核心。

---

## 键前缀分配（`<namespace>:<系统>:<用途>:…`）

键的前缀是**分段拼出来的**：Redis 上最前面一段是命名空间（隔离应用），之后由用它的系统
继续往下分。现在这套缓存上住着两个系统，各段占位如下：

| 段 | 取值 | 谁写的 | 用作 |
| --- | --- | --- | --- |
| `tickneko` | `CacheOptions.namespace`（默认 `tickneko`） | 配置 `[cache]` | **命名空间段**：多个应用共用一个 Redis 时隔离（一改，整套键跟着换） |
| `auth` | 硬编码（`api/services/session/tokens.py`） | 会话系统 | **系统段**：这套键是会话系统的 |
| `token` | 硬编码（`tokens.py` 的 `_TOKEN_KEY`） | 会话系统 | **用途段**：令牌索引 —— 令牌摘要 → 用户 id |
| `{token_hash}` | 令牌摘要（sha256 十六进制） | 会话系统 | 哪一条会话 |
| `workflow` | 硬编码（`nodes/cache.py`） | 工作流系统 | **系统段**：这套键是工作流系统的 |
| `graph` / `acct` | 节点 `scope` | cache 节点 | **用途段**：变量作用域（图级 / 账号级） |
| `{图 id}` / `{账号 id}` | `ctx.workflow_id` / `ctx.owner_id` | cache 节点 | 这份变量属于哪张图 / 哪个账号 |
| `{变量名}` | 节点 `key`（接线或手填） | cache 节点 | 最终业务键 |

拼出来的完整键：

```
tickneko:auth:token:{token_hash}           会话系统：令牌 → 用户（值形如 "id|ttl"）
tickneko:workflow:graph:{图 id}:{变量名}    工作流：图级变量
tickneko:workflow:acct:{账号 id}:{变量名}   工作流：账号级变量
```

- 最前面那段 `tickneko` 是**配置**不是写死的：`namespace` 一改，整套键跟着换，两套部署
  就互相读不到对方的键了；
- 第二段起是**各系统自己的地盘**：`auth`（会话）/ `workflow`（工作流）两个系统段由各系统
  硬编码。新增系统时照这个约定再开一段，**别去抢别人已经占了的第二段**；
- 各系统往核心层传的键**不含**最前面那段命名空间（核心层 `_full` 自动拼上、`_strip` 摘掉，
  见上文「命名空间」）—— 上表 `auth:...` / `workflow:...` 之后的部分，就是各系统实际传给
  `cache.get` / `cache.set` 的键；
- **键名与账号 id 不允许带冒号**：冒号是分段符，带进去会把键从中间劈开、造成歧义。工作流
  侧由 cache 节点拦（`key` 手填 / 接线 / 账号级归属 id 都当场报错）；账号 id 正常是
  `u-<账号>`，账号注册的字符集白名单（`user/validation.py`，`^[A-Za-z0-9_.-]+$`）本身就不含
  冒号 —— 源头 + 缓存关口双保险。

---

## 缓存节点（`tickneko/workflow/nodes/cache.py`）

核心层是**设施**，节点层是**用法**：工作流里想「跨执行记住点什么」，靠 `cache` 节点把值
写进缓存、下一趟取回来。数据沿边走的模型里**每次执行都是全新一趟**——上游送什么就处理
什么、跑完就散；两次执行之间要传递状态（「上次处理到哪」「这个群开没开提醒」「攒了几条」），
这是缓存节点存在的唯一理由。

### 两层怎么分工

| 层 | 谁 | 管什么 |
| --- | --- | --- |
| 核心层 | `tickneko.core.cache`（本文件上文） | 键值存取设施：换后端、TTL、批量、命名空间 |
| 节点层 | `cache` 节点（`nodes/cache.py`） | **键长什么样**（前缀 + 作用域）、get / set 语义、给用户的口径 |

节点层只依赖核心层**门面**（`ctx.cache`，鸭子形状 `async get(key)` / `async set(key, value)`），
不感知后端差异。节点层往核心层传的键名**不带 Redis 命名空间前缀**——那层前缀是 Redis 后端
自己的事（见上文「命名空间」），核心层 `_full` / `_strip` 自动拼上 / 摘掉。

### 配置与端口

- `action`：`get`（读一个键，缺省）/ `set`（写一个键）；
- `scope`：`workflow`（图级，缺省，只有这张图看得见）/ `account`（账号级，同一账号的工作流共享）；
- 端口：`key`（入口，必填）/ `value`（`set` 的写入值，手填兜底也能接线）/ `default`（`get`
  读不到时的默认值）/ `cache_value`（出口：取到 / 写入的值）。

「入口」= 字段名与端口 id 同名：**线上的值优先，没接线才用 config 手填**（`input_value` 口径）。

### 键前缀：作用域 → 键

节点层用一个前缀区分作用域，同一个缓存里互不打扰：

```
workflow    workflow:graph:{图 id}:{变量名}     例  workflow:graph:3ab9c2…:计数
account     workflow:acct:{账号 id}:{变量名}    例  workflow:acct:u-admin:日签开关
```

- **图级**：挂在 `ctx.workflow_id` 下。离线跑 / 测试直接构造 ctx 时它是
  `NO_WORKFLOW_ID = "local"`，键变成 `workflow:graph:local:…`——不跟任何真图撞；
- **账号级**：挂在 `ctx.owner_id` 下（`owner_id` 是工作流的主人，见 `workflow.md` 的
  `NodeExecutionContext`）。没有归属（离线跑、定时触发）就**当场抛 `ValueError`**：
  不知道是谁的缓存不能瞎写，提示改用 `workflow` 作用域；
- 落在 Redis 上的完整键再拼上命名空间段（各段取值与用途的完整拆解见上文「键前缀分配」）。

### 口径（为什么这么定）

- **值按文本存**：线上送整数会转成字符串——核心层只认字符串，两种后端一致（`set_json`
  是显式包一层 JSON，节点层不走那条路）；
- **`get` 没取到不算事故**：键还没存过就送 `default`（没填就是空串），流程继续——「第一趟
  还没存过也能往下走」是常见用法（配 `default` 当初值，见小抄）；
- **`set` 空值 = 清成空串**：清空是合法操作，不是错误；
- **`key` 必填**：入口没接线、config 也没填，当场抛（同 `http.url`：不知道操作哪个键）；
- **键名不允许冒号**：`key` 手填、接线、账号级归属 id 带冒号都当场拦 —— 冒号是键的
  分段分隔符，带进去会把键从中间劈开（账号 id 正常是 `u-<账号>`，源头字符集已排除）；
- **缓存后端不可用会抛 `CacheError`**（没 `start()` / Redis 掉了且没降级）：环境问题不吞不掩。
  正式跑由主程序启动缓存（`bootstrap` 已做），`ctx.cache` 缺省落进程级单例
  `tickneko.core.cache.cache`；离线测试给 ctx 注入自己的门面即可。

### 小抄

```
每趟 +1:   get 计数 -> operator(+) 1 -> set 计数
初值:      get 计数（default 填 1）—— 第一趟还没存过也能往下走
开关:      get 日签开关 -> condition(== 开) …
跨图传值:  scope 选 account —— 另一个工作流 get 同一个键就拿到了
```
