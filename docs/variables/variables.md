# 变量查看器与键族联动

变量查看 / 编辑由写变量的节点拥有。`NodeSpec.variable_viewer` 声明一个 `VariableViewer`
子类，查看器负责匹配规则、只读探测、编码、参数校验、存储读写和展示数据。HTTP 入口只做
身份定位、鉴权、分派与响应封装；前端只认识 `json / list / dict / str` 四种展示类型。

## 1. 模块与调用链

| 文件 | 职责 |
|---|---|
| `tickneko/workflow/nodes/base.py` | `NodeSpec.variable_viewer`，默认 `None`，兼容原有节点 |
| `tickneko/workflow/nodes/variable_viewer.py` | `VariableContext`、`VariableRule`、`VariableView`、`VariableViewer` 契约 |
| `tickneko/workflow/nodes/variable_viewers.py` | 显式注册的 str / json / list / dict 基础查看器 |
| `tickneko/workflow/nodes/registry.py` | 三种节点注册入口均收集查看器；同类去重，顺序确定 |
| `tickneko/workflow/nodes/cache.py` | `CacheViewer`，匹配 cache 的 `set` 动作 |
| `tickneko/workflow/nodes/ds_dict.py` | `DsDictViewer`，字典文本编码与完整覆盖 |
| `tickneko/workflow/nodes/ds_container.py` | `DsListViewer` 与运行时共用的本体 / 频次联动 |
| `tickneko/api/services/variables/service.py` | 逻辑变量发现、图来源反查、探测与查看器选择 |
| `tickneko/api/api/variables/router.py`、`responses.py` | HTTP 入口、错误转换与通用响应壳 |
| `frontend/src/features/variables/` | 类型渲染、编辑草稿、新增 / 保存与重新读取 |

调用链：`VariablesPage → VariableValueViewer → variablesApi → router → VariableService →
节点注册的查看器 → 节点拥有的存储实现`。工作流层不 import FastAPI，核心缓存层不依赖工作流。

## 2. 注册、反查与探测

`register_node`、`register_executor`、`declare_node_type` 都接受
`variable_viewer=SomeViewer`。查看器定义 `rules`、`priority`、`match(context)`、
只读的 `probe(context)`、`view()` 和 `add(self, /, **params)`。

上下文只由服务端创建：包含缓存连接、作用域、真实归属、图 id、逻辑名称和可选来源节点。
物理键由可信身份计算，请求体里的 `owner_id / key / context` 等字段不能改变它。

图级变量优先检查已发布的图快照；没有发布版本时检查当前提交版本，再检查未提交图的暂存区。
默认规则匹配节点配置中的 `scope / key`，查看器可以改为自己的字段或多个规则。读取命中来源后，
检查其 `NodeSpec.variable_viewer` 和查看器规则。多个节点注册同一个查看器只算一个候选。
当前来源节点未开放查看器时返回 `type=null, editable=false` 和原因，不绕过声明走基础写入。

账号级变量直接探测。图已删除、节点类型已失效、无匹配来源、快照不存在或无法解析也进入探测。
探测只读，不创建键、不修复计数；数据库 / 缓存连接失败仍是存储故障，不伪装成来源不存在。

选择顺序为：有效来源声明 → 有键族或明确规则证据的查看器 → 显式注册的基础查看器。
同一阶段按 `priority` 取唯一最高值；同优先级的不同查看器冲突时，尽可能用低优先级查看器
只读展示，并给出冲突原因，修改返回 409。模块导入顺序不决定写入者。

基础 str / list / dict 的优先级为 0，合法 JSON 文本为 10，内置 ds 查看器为 100。
已命中 cache 写节点的值仍按文本展示；来源丢失时，合法 JSON 文本可按 JSON 展示。
物理 list 缺少来源和键族证据时可能是辅助键丢失的 ds-list，因此基础 list 探测只读。
不能凭 `:meta` 不存在就认定它是普通 list。

扩展节点接入示例：

```python
from tickneko.workflow.nodes import (
    JsonViewer, VariableContext, VariableRule, register_node,
)

class SettingsViewer(JsonViewer):
    priority = 100
    rules = (VariableRule(key_field="slot", name_pattern=r"配置-.+"),)

    @classmethod
    async def probe(cls, context: VariableContext) -> bool:
        return cls.match(context) and await super().probe(context)

@register_node("settings-write", variable_viewer=SettingsViewer)
async def write_settings(node, ctx):
    # 与查看器使用同一 JSON 编码与作用域规则写入自己的变量。
    ...
```

需要新修改参数时覆写 `add(self, /, **params)`，自行校验全部字段，非法请求抛 `ValueError`。
`self` 使用仅位置参数，保证客户端提交同名未知字段也进入参数校验，不意外变成 Python `TypeError`。

## 3. 逻辑变量与 HTTP 契约

缓存门面返回不含 Redis namespace 的逻辑键：

```text
workflow:graph:{workflow_id}:{name}
workflow:acct:{owner_id}:{name}
```

发现变量时先解析作用域与名称，再跳过 **名称包含 `:`** 的项，过滤辅助键、孤立辅助键，
并去重。总数与分页都按过滤后的逻辑变量计算。Redis namespace 可以包含冒号，不影响本体展示。

接口前缀默认 `/api`：

| 方法 / 路径 | 作用 |
|---|---|
| `GET /api/variables` | 原有 `owner_id / scope / query / limit / offset` 查询与分页 |
| `GET /api/variables/value` | 查看一个逻辑变量 |
| `POST /api/variables/add` | 新增或保存，整份 JSON 对象交给查看器 |

后两个接口通过 query 定位：图级传 `scope=graph&workflow_id=...&key=...`；账号级传
`scope=account&owner_id=...&key=...`，省略账号归属时取当前用户。图归属从工作流存储查，
调用方不能指定或伪造。管理员可操作所有归属与已删图的孤儿变量，普通用户只能操作自己的。

沿用 `ApiResponse` 外壳，业务数据示例：

```json
{
  "scope": "graph",
  "owner_id": "u-admin",
  "workflow_id": "some-graph",
  "key": "队列",
  "type": "list",
  "data": ["a", "a", "b"],
  "length": 3,
  "editable": true,
  "reason": "",
  "value": "[\"a\", \"a\", \"b\"]",
  "ttl": null
}
```

`value` 保留给已有只读调用方，编辑器使用 `type / data / editable`。`length` 来自 list
本体；其它类型为 null。`ttl=null` 表示无剩余过期秒数，已有本体的编辑保留其 TTL。

| 修改请求 | 语义 |
|---|---|
| `{"value": ...}` | str / json 覆盖；JSON 保留 false、0、null 等类型 |
| `{"item": ...}` | list 新增一个元素，ds-list 联动维护频次 |
| `{"items": [...]}` | list 完整覆盖，保留顺序和重复项 |
| `{"field": "name", "value": ...}` | dict 新增 / 修改一个字段 |
| `{"fields": {...}}` | dict 完整覆盖，删除未提交的旧字段 |

`items=[] / fields={}` 是合法清空。空字符串、0、false、null 都按字段存在性判断，
具体是否允许由查看器决定。内置 cache / ds 节点继续使用文本编码：null → 空串，标量 →
Python `str`（例如 false → `"False"`），拒绝嵌套结构；普通 ListViewer / DictViewer 要求文本值。
前端不会替其它查看器把所有元素转成字符串，非文本元素用 JSON 编辑。

缺参、互斥参数、未知参数、错误类型及非法值均在查看器校验，路由将 `ValueError` 转为统一
422 响应，参数无效时不写入。请求不是 JSON 对象也返回 422。未开放编辑 / 匹配冲突返回 409；
没有本体且无法找到查看器返回 404，不探测性创建键。存储故障沿用 500 错误封装。

## 4. ds-list 键族与旧数据

一份 ds-list 只有两个物理键：

```text
<key>        list 本体，内容与长度的唯一依据
<key>:meta   hash，编码后的文本元素 → 出现次数
```

`["a", "a", "b"]` 的 meta 为 `a=2,b=1`。覆盖为 `["b", "b"]` 后只剩 `b=2`；
追加 / 头插加对应元素的次数，弹出后减次数，归零就删除字段。最后一个字段删除后 Redis
自动删除空 hash，`exists(<key>:meta)==false` 是正常状态。空字符串也是真实元素，不等于弹空。
`length` 始终读 LLEN，`contains` 正常情况下用 meta 的 HEXISTS。

查看 / probe 不读辅助数据来代替本体，也不修复辅助键。非空本体而 meta 缺失时，已确认的
DsListViewer 新增会先按本体重建全部频次，运行时 push / pop / contains 同样按需恢复。
完整覆盖先完成所有参数校验与元素编码，再覆盖本体并重建 meta；清空或弹空清理辅助数据。

旧版本的 `:meta={count:总数}` 和 `:idx={元素:次数}` 不做启动时批量迁移：
旧 `:idx` 可以作为键族探测线索，读操作继续读本体；首次修改或运行时 contains 从本体重建
新的频次 meta，再删除旧 idx。旧索引的数值不被当成正确计数依据。`count` 不再是保留字段，
新列表可以存一个文本为 `count` 的普通元素。旧 / 新辅助键均不作为独立变量暴露。

空集合不占存储键，清空后会从列表消失。已确认的查看器实例或仍有来源声明的图级变量可以
继续新增，重新创建键族。来源也丢失的账号级 / 孤儿变量，清空后无法凭空恢复类型，重新查询
返回 404；前端保留本次清空的成功快照并刷新列表，节点重新写入后即可再次查看。

本体与辅助键更新 **不保证跨键原子性**，并发时允许短暂不同步。中途缓存错误会向上抛出，
接口不会返回保存成功，也不宣称回滚；前端保留草稿并允许重新读取本体。正常整体覆盖成功后
频次必须对应新本体。

## 5. 前端与验证

编辑器支持文本和 JSON 保存，list 按序编辑 / 删除 / 新增，dict 编辑字段名和值 / 删除 / 新增。
保存提交完整 `items / fields`，新增直接调用 add。已有编辑草稿未保存时先保存再新增，
避免新增后的刷新覆盖未提交修改。请求期间禁用重复提交；失败保留草稿并展示服务端 422 文案。
保存成功后重新调用 view 刷新真实值和长度，随后刷新列表。只读变量只有查看入口。

回归测试在 `tests/test_variable_viewers.py`、`tests/test_variables.py`、`tests/test_ds_nodes.py`：
覆盖注册兼容与去重、来源规则、账号级 / 已删图 / 失效节点探测、规则冲突、辅助键缺失、
逻辑枚举与分页、重复 / 空元素计数、旧 idx 按需重建、完整覆盖、清空、权限、422 及部分存储失败。
Redis 集成用例使用含冒号的独立 namespace，验证真实 HDEL 删空行为；本机无 Redis 时跳过，
CI 的 Ubuntu Redis 服务会执行。

```powershell
venv\Scripts\python.exe -m pytest -q -rs -n 4
cd frontend
npx tsc --noEmit
npm run build
node --test tests/*.test.mjs   # 本命令需要支持 TS strip 的 Node 版本（本机 Node 24）
```
