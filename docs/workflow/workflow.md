# tickneko.workflow 模块索引（MODULE MAP）

> 本文件是 `tickneko/workflow/` 下**逐文件 → 作用**的速查索引 + 各模块的「为什么」（第 7 节）；
> 各 `.py` 的模块 docstring 只留一句话定位，并指回这里。
>
> **要写自己的节点，直接跳第 5 节**（完整指南：契约、注册即校验、可选依赖、测试写法）。
>
> **画布上怎么把流跑起来**（暂存 / 保存版本 / 发布 / 运行开关，以及「运行的是哪一份图」）看第 8 节。
>
> 同步规则：新增 / 重命名 / 删除文件时，记得更新这里。

---

## 0. 分层总览

```
tickneko/workflow/
├── models.py        图（节点 / 边）、校验报告、落库记录、规范 JSON / 摘要
├── validator.py     入库前校验：结构 → 拓扑 → 语义（④ Dry Run 只有阶段名，未接）
├── store.py         落库：定义 / 版本两张表（SQLModel + AsyncSession），按归属隔离
├── nodes/           ★ 节点执行器：一类节点一个文件 + 注册表（**写自己的节点看这里**）
│   ├── base.py          契约：NodeExecutor / NodeSpec / ConfigField / PortSpec / NodeExecutionContext
│   ├── registry.py      注册表：register_node / declare_node_type / get_spec / load_node_modules
│   ├── variable_viewer.py   变量查看器契约：上下文 / 匹配规则 / view / add
│   ├── variable_viewers.py  显式基础查看器；接入规则见 ../variables/variables.md
│   ├── triggers.py      内置：三个触发器（trigger-message 消息 / trigger-time 定时 / trigger-event 事件）
│   ├── end.py           内置：end（图终点）
│   ├── log.py           内置：log（按级别写业务日志；内容从 message 入口来）
│   ├── test.py          内置：test（调试：回显入口的值到日志，画布联调用）
│   ├── constant.py      内置：constant（一个节点一个常量值，从 value 出口送下去）
│   ├── http.py          内置：http（发一次 HTTP 请求；需要可选依赖 httpx）
│   ├── ai_service.py    内置：ai-service（将选中分支的消息投递到 AI 服务，凭据取环境变量；见 ai-service.md）
│   ├── delay.py         内置：delay（异步等待：秒数可接线覆盖手填，不阻塞事件循环）
│   ├── json.py          内置：json（解析 JSON 文本 + 点路径取值；取不到 = 业务失败，停止向下传播）
│   ├── regex.py         内置：regex（正则提取 / 替换；抽不到 = 业务失败，停止向下传播）
│   ├── now.py           内置：now（当前时间：格式化文本 + Unix 时间戳）
│   ├── condition.py     内置：condition（条件分支：true / false 双出口；引擎按选中出口剪枝）
│   ├── send.py          内置：send（把 message 发到 target 指向的会话：去向走 target 值端口 + 内容端口，走 ctx.gateway.reply；没有 target 就不发，回执不成功 = 业务失败）
│   ├── unpack.py        内置：unpack-onebot / unpack-kook（会话解包：把会话定位拆成字符串字段，号统一字符串化）
│   ├── pack.py          内置：pack-onebot / pack-kook（会话封装：把字符串字段拼回会话定位，走 gateway.make_target）
│   ├── operator.py      内置：operator（算术：+ - * / %；结果文本化，算不出来 = 业务失败）
│   ├── cache.py         内置：cache（变量存取：get / set；读不到回默认值；作用域账号 / 图，键前缀区分）
│   └── placeholder.py   内置：placeholder（占位：只透传不做事，参与画布理线）
├── graph.py         图的小工具：出边索引 / 可达集合 / 入口节点 / 边端口（校验器与运行器共用）
├── executor.py      运行器：只跑 start 可达的主流程，按拓扑顺序执行 + **按边投递数据** + **按选中出口剪枝**
└── runtime.py       运行时：启动只给**开着运行开关**的已发布流登记触发（定时 / 消息 / 事件，不执行图）；
                     到点后加载该版本跑整条流程；拨开关即时启停（WorkflowTriggers）
```

**依赖方向（单向、无环）**：

```
executor / runtime ──► nodes ──► models
validator ──────────► nodes.registry / nodes.base（读注册规格：角色、字段、校验器）
store ──────────────► models
```

- `nodes/` 只依赖 `models`：**节点不需要知道**运行器、校验器、存储的存在；
- 运行器按**类型名**从注册表取执行函数，不认识任何具体节点；
- 本包不 import FastAPI（HTTP 入口在 `tickneko/api/api/workflow/`，装配在 `tickneko/api/app.py`）。

---

## 1. models.py —— 图与数据协议

| 名字 | 作用 |
|---|---|
| `WorkflowNode` | 一个节点：`id`（图内唯一）/ `type`（合法值 = 注册表里已登记的类型）/ `config` / 画布坐标 `x`·`y` |
| `WorkflowEdge` | 一条有向边：`source` 的**输出端口** → `target` 的**输入端口**，值就沿它流 |
| `WorkflowGraph` | 一张图：`nodes` + `edges`，入库前校验与版本快照装的都是它 |
| `ValidationIssue` / `ValidationReport` | 校验结果：`node_id` + `code` + 人话 + 建议；失败会带上卡在哪个 `stage` |
| `STAGE_*` | 四个阶段名（`structure` / `topology` / `semantic` / `dry_run`） |
| `WorkflowDefinitionRecord` / `WorkflowVersionRecord` | 落库记录（定义 / 版本快照） |
| `canonical_graph_json` / `graph_checksum` | 规范 JSON 与它的 sha256（内容没变就不产生新版本） |

> **数据沿连线走，没有全局变量**：节点从自己的**输入端口**拿到上游送来的值（引擎按边投递，
> 键 = 目标端口名），把产出放在**输出端口**上（执行函数返回值的键 = 端口 id）。
> `trigger` 类型端口只表达先后；其余类型（`message` / `target` / `list` / `dict` / `set` /
> `generic`，见 `nodes/port_types.py` 的 `PORT_TYPES`）都送值；边两端端口类型必须相同（见第 5.2 / 5.6 节）。
>
> **泛型端口 `generic`（透传）是唯一例外**：它是「输入什么类型、输出就是什么类型」，所以能
> 接**任意数据流端口**（接了会话定位就当会话定位用），但**不接触发**（`generic` ↔ `trigger`
> 不放行）—— 接线语义见 `port_types.port_types_compatible`。声明成 `generic` 的一对入口 / 出口
> 用 `PortSpec(..., tie="value")` 互相指认为**透传对**（`tie` 指向同一节点另一侧的端口 id）：
> 两端生效类型永远一致（输入没接线但输出接了，输入显示输出定下的类型，反之亦然），画布上
> 就靠**两端同色**表达这层对应（见 §5.6 ⑥）。
>
> 边没写端口时按 `trigger` 读（`graph.DEFAULT_EDGE_PORT`）：这类边只表达顺序、不送值。

## 2. validator.py —— 入库前三个阶段（④ Dry Run 还没接）

| 阶段 | 查什么 | 典型错误码 |
|---|---|---|
| ① 结构 | 能不能解析成图、节点 id 唯一、边端点存在、主流程上的类型都已注册（孤儿类型不查） | `UNKNOWN_NODE_TYPE` |
| ② 拓扑 | **只看触发节点可达的主流程**：触发节点（`role="start"` 的消息 / 定时 / 事件触发）有且仅有 1 个、至少一个可达 end、无环（Kahn）、注册的出入边约束（分流类节点 ≥2 出边、end 无出边） | `START_NOT_UNIQUE` / `END_MISSING` / `CYCLE_DETECTED` 等 |
| ③ 语义 | 注册字段必填、节点自注册校验器（trigger/cron、log level、http method）、**连线**（端口存在 / 两端同类 / 必填入口接上没）、表达式语法；**全部只查主流程节点** | `MISSING_CONFIG` / `INPUT_NOT_CONNECTED` / `UNKNOWN_PORT` / `PORT_TYPE_MISMATCH` / `DUPLICATE_INPUT_EDGE` / `INVALID_TRIGGER` / `INVALID_CRON` / `INVALID_LOG_LEVEL` / `INVALID_HTTP_METHOD` |
| ④ Dry Run | **还没接**：只留了阶段名常量 `STAGE_DRY_RUN`，等执行引擎就位再加 | —— |

> **短路**：某一阶段出错就不再往后跑 —— 结构都不对，拓扑 / 语义无从谈起。
> 类型专属规则不在校验器里写分支：每个节点在注册时挂自己的校验器（第 5.6 节）。
>
> **孤儿节点永远合法**：从 start 不可达的节点（散点、独立小图、内部带环的组件）不产生
> 任何错误、不参与语义检查，运行器也不执行它们——「用不到，但确实可以保存」。

## 3. store.py —— 落库

| 名字 | 作用 |
|---|---|
| `WorkflowDefinitionTable` | `workflow_definitions`：一个工作流一行（元数据 + 版本指针 + **运行开关** `enabled` + **实例策略** `multi_instance`），`UNIQUE(owner_id, name)` |
| `WorkflowVersionTable` | `workflow_versions`：每次保存一张**不可变**图快照，`UNIQUE(workflow_id, version)` |
| `SqlWorkflowStore` | 读写实现：查询走 `AsyncSession`（不手写 SQL；`ensure_schema` 里那一句 `ALTER TABLE` 是给老库补列的迁移，属例外）；按 `owner_id` 隔离 |
| `WorkflowError` / `WorkflowNameConflict` | 存储层错误（消息直接给人看） |

> **发布 ≠ 运行**：发布只挪发布指针（`status=published` + `published_version`），**不执行图**；
> 要不要真的跑由 `enabled`（运行开关，默认 `False`）说了算 —— 它是**库里的字段**，重启 / 多进程
> 认的是同一份。老库升级时这一列按 `0` 补（见 `_DEFINITION_ADDED_COLUMNS`），不会因为多了个
> 开关就突然开始跑。开关怎么拨（接口 / 隔离 / 即时启停）见 `tickneko/api/api/workflow/`。
>
> **实例策略**（`multi_instance`，就是设置弹窗里的「单实例 / 多实例」，默认 `False`）只管定时
> 触发**这一拍怎么跑**：上一次还没跑完、到点又到点时，跳过本次（单实例）还是开新实例叠加
> （多实例）。登记那一趟由 `runtime.register_published_workflow` 读出来交给调度器的
> `add(..., multi_instance=...)`；老库补列同样按 `0`（单实例）填，升级行为不变。

表结构（类型 / 约束写在 Python 里，DDL 按方言生成，sqlite 与 mariadb 共用一份）：

```
workflow_definitions          一个工作流一行（元数据 + 版本指针 + 暂存区）
    id                 VARCHAR(64)  PRIMARY KEY
    owner_id           VARCHAR(64)  INDEX            归属用户（多用户隔离的过滤列）
    name               VARCHAR(128)                  同归属下唯一
    status             VARCHAR(16)  DEFAULT 'draft'  draft / published
    current_version    INTEGER      DEFAULT 0        最近提交的版本号
    published_version  INTEGER      DEFAULT 0        已发布版本号（0 = 没发布过）
    enabled            BOOLEAN      DEFAULT 0        运行开关（发布 ≠ 运行，默认不跑）
    multi_instance     BOOLEAN      DEFAULT 0        实例策略（单 / 多实例，见上）
    draft_graph_json   TEXT         DEFAULT ''       暂存区图（编辑中，未提交）
    draft_updated_at   FLOAT        DEFAULT 0        暂存区最近保存时间
    current_ref        VARCHAR(16)  DEFAULT 'draft'  当前指针 draft / version
    created_at / updated_at        FLOAT             Unix 秒
    UNIQUE(owner_id, name)

workflow_versions             每次保存一张不可变图快照
    id            VARCHAR(64)  PRIMARY KEY
    workflow_id   VARCHAR(64)  INDEX
    owner_id      VARCHAR(64)  INDEX  冗余归属，列表 / 鉴权少一次 join
    version       INTEGER             同一工作流内自增
    graph_json    TEXT                规范 JSON 快照
    checksum      VARCHAR(64)         graph_json 的 sha256（内容没变不新增版本）
    note          VARCHAR(255)
    created_at    FLOAT
    UNIQUE(workflow_id, version)
```

引擎由外部注入（同用户 / 会话 / 令牌存储的惯例），本模块不建引擎、不读配置。

暂存 / 保存版本 / 发布 / 运行开关这四个动作分别动上面哪几个字段、运行时又按哪一份图跑，见第 8 节。

## 4. nodes/ —— 节点执行器（一类一文件）

| 文件 | 作用 | 端口（输入 → 输出） | config（必填项加粗） |
|---|---|---|---|
| `base.py` | **契约**：`NodeExecutor` / `NodeSpec` / `ConfigField` / `PortSpec` / `NodeExecutionContext` / `input_value`。`NodeSpec` 除校验规则外还带**展示信息**（`label` / `order` / `inputs` / `outputs`）——画布照它渲染，见 §5.6 ⑥ | —— | —— |
| `registry.py` | **注册表**：`register_node` / `declare_node_type` / `get_spec` / `get_executor` / `registered_types` / `load_node_modules` | —— | —— |
| `triggers.py` | **三个触发器**（都 `role="start"`，图起点）：`trigger-message` 等消息接入（登记到 `MessageRouter`）、`trigger-time` 按 cron 登记到调度器、`trigger-event` 订阅平台事件（加好友 / 进群 / 撤回 / 戳一戳…，登记到 `EventRouter`，按事件类型匹配）—— **加 / 摘触发只在「登记那一趟」**（拨运行开关 / 启动载入 / 发布新版）。拆成三个类型而不是一个节点带下拉：形态本来就不一样（定时要 cron、事件要事件类型、消息什么都不配），拆开后卡片形状固定、面板不出现「跟当前触发方式无关的字段」 | `trigger-message`：— → `trigger` / `message` / `target`；`trigger-time`：— → `trigger`；`trigger-event`：— → `trigger` / `event_type` / `user_id` / `chat` / `chat_id` / `text` / `target` | `trigger-time`：`cron`（必填，自注册校验器）、`name`；`trigger-event`：`event_type`（可选项见 `EVENT_TYPE_OPTIONS`；**缺省 `*` = 任何事件** —— 没动过这个下拉也算配好了，别把它变成「必填没填」；下拉里按 `EVENT_TYPE_LABELS` 显示中文，值仍是平台原生事件名）；`trigger-message`：无 |
| `end.py` | 图终点（`role="end"`，`max_outgoing=0`）：写一条完成日志 | `trigger` → — | —— |
| `log.py` | 按级别写业务日志；内容从 `message` 入口来 | `trigger` / `message` → `trigger` | `message`（没接线时手填）、`level`（缺省 INFO，注册默认值；枚举由自注册校验器把） |
| `test.py` | **调试**：把入口的值**回显**到日志（还附一份**全部入口值**的快照），再原样从出口送下去 —— 夹在中间看「线上流过了什么」；不改写、不判断，纯粹给画布联调用 | `trigger` / `message` → `trigger` / `message` | `message`（没接线时的手填值，缺省 `hello`） |
| `constant.py` | **常量**：一个节点一个值，从 `value` 出口送下去 | `trigger` → `trigger` / `value` | **`value`**（必填，没有默认值） |
| `http.py` | 发一次 HTTP 请求；**4xx / 5xx = 业务失败**（对方回了错）：抛 `NodeFailure`，停止向下传播；连不上 / 超时是**可预期的环境问题**：抛 `EnvironmentFailure`，中断整条流程但日志只记一行（不铺 httpx 堆栈） | `trigger` / `url` / `body` → `trigger` / `http_status` / `http_body` | `url`（**入口**必填：接线或手填）、**`method`**（枚举由自注册校验器把）、`body`（没接线时手填）、`timeout`（缺省 10，注册默认值）、`headers`（只能手写，没有对应端口） |
| `delay.py` | **等待**：异步等一会儿再往下走（`await asyncio.sleep`，**不阻塞事件循环**）；`0` = 不等（临时把等待关掉） | `trigger` / `seconds` → `trigger` | `seconds`（**入口**：接线覆盖手填，缺省 5；`0` 允许，上限 1 小时 —— 手填值由自注册校验器把，线上的值运行期判断） |
| `placeholder.py` | **占位**：没有任何功能，只参与画布理线 —— 入口的值**原样透传**到出口（不读不写不记日志），流程语义和「线直接连」等价；入口出口是 `generic` **泛型**（接会话定位就出会话定位），两者用 `tie` 声明成透传对 | `trigger` / `value`（`generic`） → `trigger` / `value`（`generic`） | ——（无配置字段；`value` 没接线透传空串） |
| `json.py` | **JSON**：解析 JSON 文本 + 点路径取值（HTTP 的搭档）；空文本 / 解析失败 / 路径取不到 = **业务失败**：抛 `NodeFailure`，停止向下传播 | `trigger` / `json` / `path` → `trigger` / `json_value` | `json`（**入口**必填：接线或手填）、`path`（缺省空 = 取整个文档；点分段，数字段是数组下标） |
| `regex.py` | **正则**：提取第一个匹配（有组取组）/ 替换所有匹配（脱敏改写）；空文本 / 空正则 / 没匹配 / 正则语法错 = **业务失败**：抛 `NodeFailure`，停止向下传播 | `trigger` / `text` / `pattern` / `replace` → `trigger` / `regex_value` | `text`、`pattern`（**入口**必填：接线或手填）、`action`（缺省 extract；枚举由自注册校验器把）、`replace`（替换文本，支持 \1 反向引用）、`flags`（i/m/s 组合，缺省无） |
| `now.py` | **当前时间**：产出「现在」（服务器本地时区）——格式化文本 + Unix 时间戳（整数秒）；没有失败分支 | `trigger` / `format` → `trigger` / `now_text` / `now_ts` | `format`（strftime 指令，缺省 `%Y-%m-%d %H:%M:%S`，可接线覆盖） |
| `condition.py` | **条件**：比一次 `left operator right`，二选一走 `true` / `false` 出口；**分流节点**（注册 `branching=True`）——引擎只让**选中出口**的控制流边活着，没走的分支整段跳过（级联到它的下游，`ctx.log` 留 `[skip]` 痕迹），与另一条分支汇合处（还有活 `trigger` 入边）照常执行；比较符非法 / 左值空 / 要数字却转不了 → 只记 warning 走 `false`，不打断流程 | `trigger` / `left` / `right` → `true` / `false` | `left`（**入口**必填：接线或手填）、`operator`（缺省 `==`，枚举由自注册校验器把）、`right`（手填兜底，也能接线） |
| `send.py` | **发送**：把 `message` 发到 `target` 指向的会话（target 化：去向从图上 target 值端口拿，内容从 message 端口拿）；走 `ctx.gateway.reply(target, message)`，平台差异（OneBot 整数号 / Kook 字符串号）由适配器翻译；**`target` 是数据边、不驱动执行**（触发走 `trigger` 边），**没有 target 就不发**（`send_ok=False` 照常送下游），回执（`send_ok` / `send_data`）成功时送下游，**失败回执 = 业务失败**：抛 `NodeFailure`，停止向下传播；没接总线（环境问题）→ 当场抛 | `trigger` / `target` / `message` → `trigger` / `send_ok` / `send_data` | `message`（**入口**必填：接线或手填；`target` 只能接线，从 `start.target` 或 `pack` 节点来） |
| `unpack.py` | **会话解包**（`unpack-onebot` / `unpack-kook`）：把会话定位**拆成字符串字段**（平台 / 会话类型 / 会话号 / 发送者 / 消息号 / 归属），号统一字符串化（OneBot 整数 -> 字符串；缺的字段给空串）；收到**别平台**的 target 当场 `ValueError`（装配错位看得见）；没有会话定位送**全空串**、不打断流程 | `trigger` / `target` → `trigger` / `platform` / `chat` / `chat_id` / `user_id` / `message_id` / `owner_id` | 无（`target` 只能接线，不能手填 —— 会话定位是结构值） |
| `pack.py` | **会话封装**（`pack-onebot` / `pack-kook`）：把字段**拼回**会话定位（unpack 的逆操作）—— 平台写死在节点上，走 `ctx.gateway.make_target`，适配器按平台口径转号；会话号不全不额外拦（reply 时说明缺什么）；没接总线 = 环境问题当场抛 | `trigger` / `chat` / `chat_id` → `trigger` / `target` | `chat`（group / private）、`chat_id`（**按会话类型解释的号**：群聊填群号 / 频道号，私聊填对方账号；都可接线，接上覆盖手填）。**只要这两样** —— 对方号由适配器用 `chat_id` 兜底，消息号是「撤回 / 引用回复」一类动作才要的 |
| `operator.py` | **运算**：对两个操作数做一次算术（`+` / `-` / `*` / `/` / `%`）；`/` 是真除法、`%` 按 Python 语义，结果文本化（整数值不带小数点）；算不出来（空值 / 非数字 / 除数为 0 / 运算符不合法）= **业务失败**：抛 `NodeFailure`，停止向下传播 | `trigger` / `left` / `right` → `trigger` / `operator_result` | `left` / `right`（**入口**必填：接线或手填）、`operator`（缺省 `+`，枚举由自注册校验器把） |
| `cache.py` | **缓存**：把一个变量存进缓存 / 取回来 —— **跨执行（跨工作流）传递状态**的通道；`get` 没取到不算事故（回 `default` 默认值，没填就是空串），`set` 空值 = 清成空串；`key` 入口没接线也没填 / 账号作用域却没有归属 → 当场抛；缓存键按作用域拼前缀（`workflow:graph:{图 id}:{键}` / `workflow:acct:{账号 id}:{键}`） | `trigger` / `key` / `value` / `default` → `trigger` / `cache_value` | `action`（缺省 `get`）、`scope`（缺省 `workflow`；枚举都由自注册校验器把）、`key`（**入口**必填：接线或手填）、`value`（手填兜底，也能接线）、`default`（`get` 读不到时的默认值，手填兜底 / 也能接线） |

> **「入口」= 字段名与端口 id 同名的那个数据端口**：`log.message` / `http.url` / `http.body` 都能
> 被连线覆盖 —— **线上的值优先，没接线才用 config 里手填的**（`input_value` 就是这个口径）。
> 标了 `required=True` 的入口必须「接线或手填」，否则语义阶段报 `INPUT_NOT_CONNECTED`。
>
> **字面量尽量走常量节点**：地址、模板、固定文案这类值写在 `constant` 节点上，谁要用就连一根线
> 过来 —— 别把同一串值复制进每个节点的 config（改一次要翻整张图）。要几个常量就摆几个节点；常量
> 节点必须在 start 可达的主流程里（孤儿不执行，它的线也就没人送值）。

**注册表是进程级、内存里的一张表**（`registry._SPECS`，类型名 → `NodeSpec`），不落库、没有配置文件：

```python
_SPECS: dict[str, NodeSpec] = {}

def register_node(node_type, *, fields=(), validator=None, role="normal",
                  min_outgoing=0, max_outgoing=None, expression_field=None): ...  # 装饰器
def declare_node_type(node_type, *, fields=(), ...): ...   # 只登记规则、执行器留空
def get_spec(node_type) -> NodeSpec | None: ...            # 校验器读的就是它
def get_executor(node_type) -> NodeExecutor | None: ...    # 没注册（或只声明）给 None
def registered_types() -> tuple[str, ...]: ...             # 排查「注册上没有」
def load_node_modules(*module_names) -> list[str]: ...     # 装外部模块
```

包一被 import，各节点模块就自己登记一次（`nodes/__init__.py` 逐个 import 内置节点）。
所以「注册一个节点」= **让那个模块被 import 到**。

## 5. 写自己的节点（开发者指南）★

### 5.1 三步，不用改框架文件

```python
# ① 新建一个模块（照 tickneko/workflow/nodes/log.py 的样子；一个节点一个文件）
#    my_pkg/nodes/dingtalk.py
from tickneko.workflow.nodes import (
    ConfigField, NodeExecutionContext, PortSpec, input_value, register_node,
)

@register_node(
    "dingtalk",
    # ② 当场注册：执行函数 + 端口 + 校验规则一起声明，校验器 / 模型 / 前端框架代码都不用动
    inputs=[
        # 数据入口：上游把值接到 text；required 表示「必须接线或手填同名字段」
        PortSpec("text", "message", "消息内容", required=True),
    ],
    outputs=[PortSpec("sent", "message", "是否发出")],
    fields=[
        ConfigField("text", "消息内容"),                  # 没接线时的手填兜底
        ConfigField("format", "格式", default="text"),     # 缺失 → 保存时自动补 text
    ],
)
async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
    text = str(input_value(node, ctx, "text"))         # 线上来的优先，没接线才用手填值
    ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
    ctx.log.append(f"[dingtalk] {node.id}: {text}")
    return {"sent": True}                              # 键 = 输出端口名
```

```python
# ③ 启动时装进来（app.py 或你自己的入口），一行
from tickneko.workflow import load_node_modules
load_node_modules("my_pkg.nodes.dingtalk")
```

`load_node_modules` 只是替你 `import`：**重复加载幂等**（命中 `sys.modules`），模块 import
失败**当场抛** —— 别把「节点没注册上」藏到跑图时才报「暂无执行器」。想核验：

```python
from tickneko.workflow import get_executor, registered_types
assert get_executor("dingtalk") is not None
print(registered_types())        # 已注册的类型名（排序）
```

> 也可以不用装饰器，运行时手工登记：`register_executor("dingtalk", exec_dingtalk)`。
> **重复注册是覆盖**，测试里换实现就靠这个。

### 5.2 契约：收什么、回什么

```python
NodeExecutor = Callable[[WorkflowNode, NodeExecutionContext], Awaitable[dict[str, Any]]]
```

- 入参：节点本身（`id` / `type` / `config`）+ 运行时上下文；
- 返回：**本节点产出的值**（`dict`，**键 = 已声明的输出端口名**）。引擎按边把它投递给下游的
  对应入口；多出来的键不会被投递（定时触发器的 `scheduled` / `task_id` 就是这种「只给日志看」的
  信息）。没有产出就返回 `{}`（像 `log` / `end` 那样）。
- 执行是**串行**的（节点之间有数据依赖）；**要不要执行只看 `trigger` 入边**：数据边
  （`message` / `target`）只送值，不会把节点撑活；
- **分支剪枝**已由引擎支持（`condition` 这类分流节点）：节点注册 `branching=True` 后，引擎只让
  「选中出口」（返回值里给了真值的输出端口）的**控制流**边活着，没走的分支整段跳过 ——
  `ctx.log` 留 `[skip]` 痕迹，被跳过节点的下游也跟着死，直到与别的活分支汇合（还有活
  `trigger` 入边就照常执行）；并行执行留给将来。

### 5.3 上下文 `NodeExecutionContext` 能给什么

| 成员 | 是什么 | 用来干嘛 |
|---|---|---|
| `ctx.inputs` | `dict[str, Any]`，**引擎按入边投递进来的值**（键 = 目标端口名） | 用 `input_value(node, ctx, "名字")` 取；测试里直接 `ctx.inputs["x"] = ...` 预置。**上游没执行过的边不算数**（孤儿连出来的线不送值，`input_value` 回落到同名字段的手填值）；上游跑了但那个出口没产出才送空串 |
| `ctx.trigger_data` | `dict[str, Any]`，触发时外面送进来的数据 | 消息触发器的 `message` 出口从它取（`ctx.trigger_data["message"]`）；事件触发器从它取 `event_type` / `user_id` / `chat` / `chat_id` / `text` |
| `ctx.logger` | `BaseLogger` / `BoundLogger`（`tickneko.core.logger`） | 写业务日志（节点自己的运行痕迹）。**默认字段已提前绑好**：每条日志自动带 `workflow_id` / `owner_id` / `user_id`，节点只写自己那句话就认得出是哪条工作流、谁的、给谁跑的 |
| `ctx.log` | `list[str]` | 节点产出的文字行（给前端回显 / 测试断言，不落日志文件） |
| `ctx.scheduler` | `TaskManager \| None` | 要把流程挂到 cron 就用它（定时触发器的做法）；没注入时是 `None` |
| `ctx.run_workflow()` | `async` 回调 | 触发整条流程（cron 到点时调它） |
| `ctx.owner_id` | `str`：这条工作流属于谁（定义表里的归属） | `pack` 节点手动构造会话定位时带它（`gateway.make_target(platform, owner_id=...)`，握手时令牌定下，同一套 id 空间）；离线跑是空串 |
| `ctx.user_id` | `str`：这一趟**面向哪个用户**（消息触发时是发消息那个人） | 把「同一个工作流在不同人身上的那一份」区分开（按人记状态 / 按人回复 / 按人打日志）。**和 `owner_id` 是两回事**：`owner_id` 是工作流的主人（账号），`user_id` 是被服务的对象。缺省空串（`NO_USER_ID`）—— 定时触发没有「这个人」；消息触发由消息路由（`dispatch`）带进来 |
| `ctx.gateway` | 平台总线（装配层注入；没接时是 `None`） | `pack` 节点靠它构造会话定位（`make_target`），`send` 节点靠它发消息（`reply(target, message)`），`unpack` 节点解包 target 值。鸭子形状：`async make_target(platform, **fields) -> ChatTarget`、`async reply(target, content) -> ActionResult`、`async send(platform, owner_id, action, **params) -> ActionResult` —— 即 `tickneko.bridge.gateway.Gateway` |
| `ctx.cache` | 缓存门面（鸭子形状：`async get(key)` / `async set(key, value, ttl=None)` —— 即 `tickneko.core.cache.Cache`） | `cache` 节点靠它存取变量。**缺省落进程级单例**（`tickneko.core.cache.cache`，主程序启动时已 `start()`）；测试 / 特殊场合可注入自己的门面 |

`input_value(node, ctx, name, default="")`：取某个数据入口的值 —— **线上的值优先，没接线才用
config 里同名字段的手填值**，两者都没有才用 `default`。这是「字段名 = 端口名」那条约定的唯一
实现处，节点不用自己判断有没有接线。

节点拿到的**只有指向自己的那些边送来的值**：别的节点产出什么它看不见（没有全局变量上下文）。

### 5.4 端口怎么设计

- **端口名就是对外契约**：它写进 edge 的 `source_port` / `target_port`，也是执行函数返回值的
  键名 —— 改端口等于改「这张图还能不能跑」（以前端口只是画布上的装饰）；
- 输出端口用**带节点前缀**的名字（`http_status` / `http_body`、`dingtalk_sent`），别用 `result`、
  `data` 这种通用词：端口虽然是每个节点自己一份（不会互相覆盖），但下游连线时要一眼看出线上是什么；
- 端口**类型**按内容选（`PORT_TYPES` 里那几种：`message` / `target` / `list` / `dict` / `set` 送值、
  `trigger` 只表达先后），画布的端口配色、面板图例都跟着它走；
- 数据入口用**普通名词**（`url` / `body` / `message`），并给同名字段留个手填兜底：画布会把
  「已接线 / 未接线」标出来，校验器只在「既没接线也没填」时才报 `INPUT_NOT_CONNECTED`；
- **一个数据入口只允许一条入边**（`DUPLICATE_INPUT_EDGE`）：要合并多个上游，就先各自接到一个
  中间节点，再从那一个节点往下送。

### 5.5 失败怎么处理：分两类

| 情况 | 怎么办 | 例子 |
|---|---|---|
| **业务失败**（这一趟没做成：算不出来、取不到、对方回了错） | 抛 `NodeFailure`：**停止向下传播** —— 本节点不产出值、出边全部置死，下游整段跳过（`ctx.log` 留 `[failed]` / `[skip]`），**别的分支与流程其余部分照常跑**，不留堆栈 | `operator` 算不出来；`json` / `regex` 取不到、抽不到；`http` 的 4xx / 5xx；`send` 的失败回执 |
| **可预期的环境问题**（连不上、超时、对端拒绝、DNS 失败） | 抛 `EnvironmentFailure`（`ConnectionError` 子类）：整条流程照样中断，但日志**只记一行**（哪个节点 + 什么原因），**不铺底层堆栈** —— httpx / httpcore 那几十行帧没有信息增量 | `http` 节点连不上 / 超时 |
| **其它环境问题**（配置写错、依赖没装、没接线、没接总线） | 直接 `raise`（普通异常）：整条流程失败并留下堆栈，别伪装成「成功但没内容」 | `http` 的 `url` 入口没接线也没填；`send` 没接总线 / 没这个平台 / 归属下没在线连接；`cache` 的 `key` 入口没接线也没填、账号作用域却没有归属 |

**为什么业务失败不再「送空串继续」**：空值会一路传到下游 —— 上一节的真实事故就是
「operator 算不出来 → 空串 → send 把空消息发了出去 → 平台回个参数错误」。失败的值当结果
往下传，排查时只能看到最后一环的症状，看不到是谁先没做成。

写自己的节点时按这个口径分：拿不到 / 算不出 / 对方不认 → `raise NodeFailure(...)`；
连不上 / 没配好 → 抛原来的异常。

### 5.6 校验那一关：注册什么，就校验什么

校验器（`validator.py`）**没有任何具体节点类型的知识**：它只从注册表读每个类型的
`NodeSpec`，按规格办事。新增类型不用动 `validator.py` / `models.py` 一行，也**不用动画布**
（画布从节点目录接口读，见 ⑥）。

**① 端口与 config 字段，都在注册处声明**：

| 声明 | 写法 | 语义 |
|---|---|---|
| 数据入口 | `inputs=[PortSpec("url", "message", "请求地址", required=True)]` | 上游把值接进来；`required` = 「必须接线或手填同名字段」，否则 `INPUT_NOT_CONNECTED` |
| 控制流端口 | `PortSpec("trigger", "trigger", "触发")`（内置节点用常量 `TRIGGER_PORT`） | 只表达先后，不送值；多条入边允许（汇聚） |
| 数据出口 | `outputs=[PortSpec("http_status", "message", "状态码")]` | 执行函数返回值的键；下游把线接过来才拿得到 |
| 出口 / 入口与字段同名 | `ConfigField("url", "请求地址")` 配 `PortSpec("url", ...)` | 该字段可被连线覆盖：**线上优先**，没接线才用手填 |
| 泛型端口（透传） | `PortSpec("value", "generic", "透传值")` | 输入接什么类型、输出就是什么类型：可接任意数据流端口、不接触发（见 `port_types.py`） |
| 透传对（输入输出同一种类型） | 两端都声明：`PortSpec("value", "generic", "透传值", tie="value")` / `PortSpec("value", "generic", "透传结果", tie="value")` | `tie` 指向**同一节点另一侧**的端口 id：两端生效类型永远一致（输入没接线、输出接了，输入就显示输出定下的类型，反之亦然），画布上两端**同色**表示对应 |
| 不可缺失字段 | `ConfigField("url", required=True)` | 主流程上的节点直接报 `MISSING_CONFIG`（`None` / 空串也算缺失） |
| 默认值字段 | `ConfigField("level", default="INFO")` | 校验前先补默认值（自定义校验器看到的是补全后的 config）；保存版本时写进快照 |
| 枚举字段 | `ConfigField("level", default="INFO", options=LOG_LEVEL_ORDER)` | 同上；`options` 只描述「有哪些可选值、按什么顺序显示」（画布渲染成下拉），校验仍归自定义校验器 |
| 枚举显示名（下拉里显示中文） | `ConfigField("event_type", options=EVENT_TYPE_OPTIONS, option_labels=EVENT_TYPE_LABELS)` | `option_labels` 是「值 → 界面文字」：**值不改** —— 它可能要跟外部对上号（事件类型要跟平台上报的 `event_type` 全等匹配），中文只用来看着好懂；没配显示名的项直接显示值本身 |
| 专用编辑器 | `ConfigField("cron", "cron 表达式", editor="cron")` | 这个字段用专用控件（`"cron"` → 可视化 cron 选择器）：画布**按标识挑控件、不认识节点类型** —— 加节点类型不用动前端；空串 = 通用渲染（有 `options` 就下拉、没有就输入框） |

连线本身的规则（端口存不存在 `UNKNOWN_PORT`、两端同不同类 `PORT_TYPE_MISMATCH`、数据入口只接
一条线 `DUPLICATE_INPUT_EDGE`）由校验器统一查，**不用自己写**。

> 一个类型**完全没声明端口**时（只 `declare_node_type` 占位的扩展节点），校验不查它的那一端 ——
> 没声明就谈不上「端口名对不对」；声明了才查。

未声明的字段一律不查，原样留在 config 里。

**② 类型专属规则挂自定义校验器**（普通必填 / 默认表达不了的，比如枚举、跨字段条件）：

```python
def validate_dingtalk(node: WorkflowNode) -> list[ValidationIssue]:
    if node.config.get("format") not in ("text", "markdown"):
        return [ValidationIssue(node_id=node.id, code="BAD_FORMAT",
                                message="format 只认 text/markdown")]
    return []

@register_node("dingtalk", fields=[...], validator=validate_dingtalk)
async def exec_dingtalk(node, ctx): ...
```

校验器收到的是**补完默认值**的节点，只负责返回 issue 列表（空列表 = 通过），
错误码自定义（照 `http.py` 的 `INVALID_HTTP_METHOD`、`triggers.py` 的 `INVALID_CRON` 抄）。

**③ 拓扑角色与出入边约束也在注册处声明**：`role="start"|"end"|"normal"`、
`min_outgoing` / `max_outgoing`、`expression_field`（指定哪个字段按表达式做语法检查）、
`branching`（分流节点：执行后没选中的出口整段剪枝，见 §5.2）。比如 `condition` 用
`min_outgoing=1` 表达「至少接一个出口」、`end` 用 `max_outgoing=0` 表达
「不能有出边」，都是通用约束，没有特判代码。

**④ 只声明、不实现：`declare_node_type`**。执行器还没写、但希望类型已经能进画布、
能保存、能被校验时，只登记规格。**内置节点里已经没有这种类型**（都在自己文件里带执行器）；
这一条留给扩展方：先占位，以后再补一个 `register_node` 覆盖掉即可。这种类型真被主流程
跑到时，运行器按老规矩报「暂无执行器」。

**⑤ 孤儿节点**：从 start 不可达的节点**一律放行**——类型未注册、config 缺失、自带环都不报错，
保存可以、运行不跑。所以字段规则只对主流程（start 可达）上的节点生效。

**⑥ 画布不自己定义节点 / 端口类型**：编辑器启动时拉一次节点目录
（`GET <prefix>/workflows/node-types`，见 `api/workflow/router.py`），**面板项 / 中文名 /
端口 / 配置表单 / 节点配色全按注册表渲染**，**端口类型（有哪些、什么色、是不是数据流）也随
目录的 ``port_types`` 下发**（见 `nodes/port_types.py` 的 `PORT_TYPES`）—— 节点颜色在
`register_node` 的 ``color`` 参数里声明（CSS 颜色值，不传画布用灰兜底）—— 加一个节点类型 /
端口类型只改后端，画布与接口都不用动。

前端不定义任何节点展示信息，只留兜底：认不出的节点类型用灰的；端口类型配色认不出也一律
淡灰。

**泛型端口怎么显色**也照这套：端口圆点 / 连线 / 配置面板都按「生效类型」画 —— 由
`editor/catalog.ts` 的 `effectivePortTypes` 顺着边（以及透传对 `tie`）推出来，接什么类型就
显什么颜色；声明了 `tie` 的透传对两端**颜色永远一致** —— 对应关系就靠这层同色表达（端口
圆点、连线、配置面板三处口径一致）。没接线 / 追不到具体类型的泛型端口保持灰。

**字段用哪个控件也全按字段声明挑**（`Inspector.tsx`）：`editor` 有专用编辑器（`"cron"` →
可视化选择器）、`option_labels` 决定下拉里显示什么字、`options` 渲染成下拉、其余是输入框；
config 里缺这个键时显示后端声明的默认值（界面上看到的就是实际生效的）。**画布不认识节点
类型**——加节点类型、给某个字段换个控件，都只改后端。

触发器拆成三个类型（`trigger-message` / `trigger-time` / `trigger-event`）之后，**没有「形状随
config 变」的节点了** —— 卡片形状只由类型决定，前端不再需要任何特判。

> 那份目录就是「两边对得上」的契约：面板上摆的必然是后端登记过的类型。万一旧图里还有认不出
> 的类型，画布画成灰色未知节点，保存时被 `UNKNOWN_NODE_TYPE` 挡下 —— 以前那批只有声明没有
> 执行器的类型（gateway / approval / expression / condition / task）已前后端一起删掉。

### 5.7 带可选依赖的节点（照 `http.py` 抄）

节点要用的第三方库如果不是框架的必装项，**别在模块顶层 import**（那会让整个 `nodes` 包 import 失败）：

```python
def _import_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:
        hint = 'HTTP 节点需要 httpx：pip install httpx（或 pip install "tickneko[workflow]"）'
        raise RuntimeError(hint) from exc
    return httpx
```

再在 `pyproject.toml` 的 `[project.optional-dependencies]` 里加一个按能力命名的 extra
（`workflow = ["httpx>=0.27"]`），并在模块 docstring 里写明「不装只影响这个节点」。

### 5.8 测试怎么写

```python
@pytest.mark.asyncio
async def test_my_node_outputs(...) -> None:
    node = WorkflowNode(                          # type 已是自由字符串，正常构造即可
        id="d1", type="dingtalk", config={}       # text 走连线，不写在 config 里
    )
    ctx = NodeExecutionContext()
    ctx.inputs = {"text": "hi tickneko"}             # 引擎投递进来的入口值（测试直接预置）
    assert await exec_dingtalk(node, ctx) == {"sent": True}
```

- **直接调函数**，不必为了测节点去拼一张图（要验「值真的沿边走」再拼 `WorkflowGraph` +
  `SimpleWorkflowRunner`，见 `tests/test_workflow.py` 里的 `test_http_status_reaches_*`）；
- 对外部 IO **打桩**，别走真实网络：`monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)`
  （`tests/test_workflow.py` 里的 `FakeAsyncClient` 就是这个套路），本项目的惯例是打桩而不是起服务；
- 注册本身也值得测：`assert get_executor("dingtalk") is exec_dingtalk`、
  `load_node_modules("你的模块")` 幂等、模块不存在时抛 `ModuleNotFoundError`。

### 5.9 上线前自查

- [ ] 类型在模块里注册上了（`get_spec("类型") is not None`；有执行函数再查 `get_executor`）
- [ ] 端口声明齐了：控制流用 `TRIGGER_PORT`；数据入口 / 出口按内容选类型（`message` / `target` / `list` / `dict` / `set`，见 `PORT_TYPES`）；必填入口标 `required=True`
- [ ] 返回值键 = 输出端口 id；取入口值用 `input_value`（别直接读 `ctx.inputs`，那会绕过「没接线用手填」的兜底）
- [ ] config 字段规则在注册处声明齐了：必填的 `required=True`，有缺省的给 `default`
- [ ] 类型专属校验（可选）：注册时挂 `validator`，配置写错在保存时就报
- [ ] 需要的拓扑约束：`role` / `min_outgoing` / `max_outgoing` / `expression_field`
- [ ] 画布**不用改**：`label` / `order` / 端口 / `fields` 声明全了，节点就自动出现在面板上
- [ ] 环境问题会抛（连不上 / 超时用 `EnvironmentFailure`，其余抛普通异常）、业务结果会返回（第 5.5 节）
- [ ] 有单测，且外部依赖是打桩的

## 6. 要加的东西放哪

| 要加的东西 | 放哪 |
|---|---|
| 新节点类型 | `nodes/<类型>.py`（一类一个文件，`@register_node` 一次声明执行器 + 字段 + 校验规则，第 5.6 节）；只有规则没有执行器用 `declare_node_type` |
| 节点类型专属的校验规则 | 该节点模块里写校验函数，注册时挂 `validator=`（不动 `validator.py`） |
| 通用的图层面校验（新的拓扑规则 / 新阶段） | `validator.py`（只放跨类型、与具体节点无关的规则） |
| 图 / 记录上要加字段 | `models.py`（协议）+ `store.py`（表结构） |
| 新的 HTTP 接口 | `tickneko/api/api/workflow/`（入口层，路由 + 请求 / 响应 schema） |
| 新的执行语义（并发 / 重试） | `executor.py`（串行 + 分支剪枝已就位；换引擎就换这个类，调用方只认 `run()`） |
| 图算法（可达集合 / 拓扑遍历 / 找入口） | `graph.py`（校验器与运行器共用一份，**别再各写一份 BFS**） |
| 发布 / 触发链路 | `runtime.py`（启动 `load_published_workflows` 只登记**开着开关**的；`WorkflowTriggers.start/stop` 给接口层即时启停；到点 `make_trigger` → `run_published_workflow` 跑整条流程，**加 / 摘任务只在登记那一趟**，跑图这趟不碰调度器 —— 见 `NodeExecutionContext.register_triggers`；消息触发走 `MessageRouter`，`dispatch(owner_id)` 按归属跑匹配工作流；**事件触发走 `EventRouter`**，除归属外还按 `event_type` 匹配订阅） |
| 给 `ctx` 注入新能力（如平台总线） | `nodes/base.py`（加参数与属性）+ `runtime.py`（`register_published_workflow` / `make_trigger` / `run_published_workflow` 全链路 keyword-only 透传）+ 装配处（`bootstrap.py`）—— **调度器到点执行的是登记那一趟构造的闭包**，能力必须从登记链路就带上（见 `send.py` 模块文档） |
| 给 `ctx` 加「缺省就有、可注入」的服务（如缓存门面） | 只动 `nodes/base.py`：参数缺省值落进程级单例 / 框架实例（如 `tickneko.core.cache.cache`），测试再注入自己的假对象 —— 单例不涉「登记那一趟」的时机问题，**不用走 runtime / bootstrap 透传**（见 `cache.py` 模块文档） |

---

## 7. 各模块的「为什么」

### 7.1 models.py —— 领域模型不认识 FastAPI，也不认识数据库

图就是前端画布设的那份 JSON（`{"nodes": [...], "edges": [...]}`），记录在各层之间流转用的是
冻结模型。节点类型（`type`）不做字面量枚举：合法类型 = 节点注册表里已登记的类型，新增类型在
自己的模块里注册即可。**数据值沿边流动**：边的 `source_port` / `target_port` 指向两端节点声明
的端口，节点不声明任何「变量名清单」。

### 7.2 validator.py —— 三个阶段怎么跑

一句话：**结构对不对 → 走不走得通 → 跑不跑得动 →（试不试一遍）→ 入库**。三个阶段由
`validate_graph` 同步跑完，阶段间**短路**（前一阶段没过，后面不跑）。错误收集口径：一个阶段
内把错误**收齐**再返回（前端一次性把所有红点画出来），阶段内部的检查不互相打断。

校验规则**全部从节点注册表推导**，新增节点类型不需要改本文件。语义阶段最主要的活是**检查连线**
（`_port_wiring`）：端口名对不对、两端类型配不配、必填入口接上没接上。

### 7.3 executor.py —— 怎么把图跑起来

- **节点执行函数不在这里**：都在 `nodes/`（一类节点一个文件）。本模块只管「怎么按顺序跑、
  值怎么沿边流」；
- 只跑 **start 沿出边可达的主流程节点**：孤儿节点允许存在于图里、允许保存，但永不执行；
- 按拓扑顺序逐个跑。跑之前按**入边**把上游产出投递到本节点的入口（`ctx.inputs`，键 = 目标
  端口名），跑完把返回值按**输出端口名**记下来供下游取；
- **执行由控制流决定**：节点要不要跑，只看指向它的 **`trigger` 入边** —— 数据边（`message` /
  `target`）只送值、不驱动执行。所以 `start.target -> send.target` 这种跨在条件之前的数据边
  **不会**把未选中分支上的 `send` 撑活（以前会：那样 message 取不到值，报「内容为空」）；
- **分支剪枝**（`condition` 这类分流节点）：执行后只让「选中出口」的**控制流**边活着，没走的
  分支整段跳过（`[skip]` 记在 `ctx.log`）并级联到它的下游；与另一条分支汇合（还有活 `trigger`
  入边）的节点照常执行；
- **业务失败停止向下传播**（`NodeFailure`）：节点自己判定「没做成」时抛它 —— 本节点**不产出
  值**（下游那条边取不到，回落到手填值）、出边全部置死，下游整段跳过并写明是被谁带停的；
  **别的分支与流程其余部分照常跑**，不留堆栈。环境问题才中断整条：可预期的（连不上 / 超时）
  抛 `EnvironmentFailure`，失败日志只记一行（堆栈没有信息增量）；别的抛普通异常并留堆栈；
- 同步执行（不并发），因为单条图的节点之间有数据依赖；并行执行留给将来；
- 触发是**开始节点**自己的事（三个触发器各管一档）：`trigger-time` 把整张图登记到
  `TaskManager`，由调度器按 cron 触发整条流程；`trigger-message` / `trigger-event` 分别登记到
  `MessageRouter` / `EventRouter` 等着被派发；发布 / 试跑时只写一条开始日志；
- 图的公共算法在 `graph.py`，与校验器共用同一份口径。本模块只再导出
  `SimpleWorkflowRunner` / `NodeExecutionContext` / `get_executor` 三个 —— 老代码
  `from tickneko.workflow.executor import ...` 还能用，新代码直接从 `tickneko.workflow` 取。

### 7.4 runtime.py —— 发布 ≠ 运行

发布接口只挪发布指针；要不要真的跑由定义上的**运行开关**（`enabled`）决定，默认关着。服务
启动时调一次 `load_published_workflows` —— 只挑**开关开着**的已发布工作流，把 `trigger=time`
的开始节点按 cron 登记到调度器，整张图**不执行**；运行期间拨开关由 `WorkflowTriggers` 即时
启停。

登记只调开始节点自己（`register_published_workflow`）：以前靠「跑一遍图、顺带登记」，代价是
每次启动都真的把整条流程执行一遍；停用是对称的（`stop_published_workflow`），同样不跑图。

调度器到点后走 `make_trigger`：重新加载该版本的图并**跑整条流程**。这一趟**不碰调度器**
（`ctx.register_triggers=False`）—— 任务在调度器里排着，而它在派发前就重排好了下一次；加 / 摘
任务只发生在「登记那一趟」。

这里说的「该版本」= **已发布版本**（`published_version`）：暂存区与当前版本跟运行无关。四个动作
（暂存 / 保存版本 / 发布 / 运行开关）的完整流程与常见疑问见第 8 节。

### 7.5 graph.py —— 口径只留一份

出边索引 / 可达集合 / 入口节点 / 边端口，校验器与运行器**共用同一份**：校验器拿它决定该查谁、
该跳谁（孤儿不查），运行器拿它决定该跑谁（孤儿不跑）。以前这两处各写了一份 BFS，改一处忘一处
就会出现「校验说没问题、跑起来却不执行」的偏差。

### 7.6 logging.py —— 日志接入

接的是 `tickneko.core.logger` 那套进程门面，用名字 `workflow`（相对核心 `tickneko` ->
`tickneko.workflow`）：

```python
from tickneko.workflow import workflow_logger

workflow_logger().info("工作流已登记", workflow_id=...)
```

**业务模块一律不直接 `default_core()`** —— 要日志实例就调 `workflow_logger()`；`runtime.py`
里的模块级 `_log()` 与节点上下文的 `logger` 都走这一口。核心由装配层（`tickneko.bootstrap` 或
`tickneko.wiring.wire_loggers`）经 `set_core()` 存进本模块槽位；`import` 本模块**零副作用**，
没装配就调用会当场抛错（fail fast），不会默默按默认参数建一份把配置定死的核心。

日志实例**用到才取，不要在模块级取**：模块级 `_logger = workflow_logger()` 是导入即执行的 ——
谁先 import 这个模块，谁就顺手把进程默认日志核心按默认参数建出来（那时配置还没读），
`[logging]` 里的颜色 / 级别就此定死。

---

## 8. 从画布到运行：保存版本 → 发布 → 运行开关

![工作流画布：开始 →（触发）条件；条件「满足」→ 当前时间 → 发送；条件「不满足」→ 条件 → HTTP → JSON → 发送](../workflow.jpg)

一张图从画布走到线上要过四道门，**每道门管的是不同的一份图** —— 这也是最容易混的地方：

| 动作 | 接口 | 动的是哪份数据 | 管的是谁 | 会不会真的跑 |
|---|---|---|---|---|
| 暂存（编辑中随手存） | `PUT <prefix>/workflows/{id}/draft` | `draft_graph_json`、指针 `current_ref = draft` | 编辑器下次打开看什么 | **不跑**，也不校验（半张图也能存） |
| 保存版本（提交） | `POST <prefix>/workflows/{id}/versions` | 新增一条 `workflow_versions` 快照、`current_version = N`、指针 `current_ref = version` | 版本历史（可回看、可发布任意一版） | **不跑**（先过校验，不过不写库） |
| 发布 | `POST <prefix>/workflows/{id}/publish` | `status = published`、`published_version = N` | **运行时跑哪一份** | **不跑**（发布 ≠ 运行） |
| 运行开关 | `PUT <prefix>/workflows/{id}/enabled` | `enabled` | 已发布的那一份**要不要**被跑 | 打开后，到点 / 来消息才真的跑 |

三份图各管各的，别串了：

| 名字 | 字段 | 作用 | 跟运行的关系 |
|---|---|---|---|
| 暂存区 | `draft_graph_json`（+ 指针 `current_ref = draft`） | 编辑中的那张图，覆盖式保存 | 无关 |
| 当前版本 | `current_version`（+ 指针 `current_ref = version`） | **编辑器打开时默认看哪一份**（提交后指针落到最新版） | 无关 |
| 已发布版本 | `published_version` | 运行时的**唯一**依据 | 就是它 |

### 8.1 四个动作，逐个说清

1. **暂存**（`PUT /draft`）：覆盖式存图，**不校验、不产生版本**（半张图也能存）；存完指针切到 `draft`，编辑器再打开默认看暂存这份。「我改的东西怎么又变回去了」通常是只暂存、没提交版本。
2. **保存版本**（`POST /versions`）：先跑三阶段校验（第 2 节），过了才写快照 —— 版本号在工作流内自增，**内容与最新版一样就不新增**（`checksum` 去重，连点保存不堆垃圾版本）。校验没过属于**业务结果**：HTTP 200 + `valid = false` + 错误清单（前端画红点），**不写任何数据**。提交后 `current_ref` 切到 `version`，编辑器默认打开刚提交的那一版。
3. **发布**（`POST /publish`）：把某个版本标成「已发布」，也是运行时的取图依据。**不传 `version` 就发布当前最新版**（`current_version`）；一个版本都没存过 → **409**。发布**不会**顺手打开运行开关，默认还是不跑。开关本来就开着时，发布会**即时按新版本重新登记**（先摘旧版任务、再登记新版）—— 免得线上还跑着上一版的触发配置。
4. **运行开关**（`PUT /enabled`）：只有真 / 假两态，决定「已发布的那一份要不要跑」。**拨开之前必须先发布过**：`published_version = 0` 时拨开 → **409**（先发布再打开开关）；关掉随手就行。关掉是**停止，不是撤回发布** —— `status` 与 `published_version` 一动不动，再拨开就接着跑。开关是**库里的字段**，重启 / 多进程认的是同一份；装配了 `WorkflowTriggers` 时拨动即时生效，没装配的场合（测试 / 示例）开关照样落库，效果等下次启动载入。

### 8.2 运行时到底跑哪一份

**已发布的那一份**（`published_version` 对应的图快照），三种触发都一样：

| 触发方式 | 登记发生在哪一趟 | 到点 / 来消息 / 来事件时跑什么 |
|---|---|---|
| 定时触发（`trigger-time`） | 拨开开关 / 启动载入（`load_published_workflows`）/ 开着开关时发布新版 | 重新加载 `published_version` 的图，按 cron 跑**整条流程** |
| 消息触发（`trigger-message`） | 同上（登记进 `MessageRouter`，键 = 归属 `owner_id`） | 同一份 `published_version`，消息进来时跑该归属下所有登记过的流 |
| 事件触发（`trigger-event`） | 同上（登记进 `EventRouter`，键 = 归属 + 订阅的事件类型） | 同一份 `published_version`，来了**订阅的那种**事件才跑 |

由此得到两条最常用的结论：

* **改图不影响线上**：改暂存区、甚至提交了新版本，只要没发布，线上照旧跑已发布的那个版本；
* **想让新版本生效**：开关开着时发布会自动重新登记（不用手动拨）；开关关着就等拨开。只改了 cron 之类触发配置、又不想动开关的，**重启一次**即可（启动载入按库里最新状态重新登记）。

启动载入的口径（`load_published_workflows`）：只挑 `status = published` **且** `published_version > 0` **且** `enabled` 的流，把 `trigger = time` 的开始节点按 cron 登记到调度器、把 `trigger = message` 的登记进消息路由 —— **整张图不执行**（发布 ≠ 运行的另一半含义）。

### 8.3 四个常见疑问

* **改完图，线上还是老行为？** 只提交了版本、没发布；或者发布了但开关关着。
* **发布了为什么不跑？** 发布 ≠ 运行，开关默认关着（`enabled = 0`）。
* **关掉开关算不算把发布撤回？** 不算。关掉只是停止登记，`status` / `published_version` 原样保留，随时再拨开。
* **想跑某一版旧图？** 发布时带上 `version`（可以发布任意历史版本），线上就按它跑。
