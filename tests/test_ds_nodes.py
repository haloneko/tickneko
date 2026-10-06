"""数据结构节点（键值对系列 ds-dict-* / ds-container）的测试：直接调 exec + 注入假缓存门面。

单元测试不碰进程级缓存单例（它没 ``start()``，直接调会抛 ``CacheError``）—— 给 ctx 注入
``_FakeCache``（鸭子形状对齐 ``tickneko.core.cache.Cache`` 的 ``get_json`` / ``set_json``）即可。
"""
from __future__ import annotations

import pytest

from tickneko.core.cache.models import CacheError
from tickneko.workflow import (
    NodeExecutionContext,
    WorkflowGraph,
    WorkflowNode,
    validate_graph,
)
from tickneko.workflow.nodes import (
    NodeFailure,
    exec_ds_container,
    exec_ds_dict_contains,
    exec_ds_dict_get,
    exec_ds_dict_keys,
    exec_ds_dict_length,
    exec_ds_dict_remove,
    exec_ds_dict_set,
)


# --------------------------------------------------------------------------- 假缓存门面
class _FakeCache:
    """假缓存门面：对齐 ds 系列节点用到的 ``get_json`` / ``set_json`` / ``hash_*``。"""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def set_json(self, key: str, value: object) -> None:
        self.data[key] = value

    async def get_json(self, key: str) -> object | None:
        return self.data.get(key)

    async def exists(self, key: str) -> bool:
        return key in self.data

    # ---- 哈希（ds 系列节点改为服务端原子操作，桩用普通 dict 模拟）----
    def _hash_entry(self, key: str) -> dict[str, str] | None:
        entry = self.data.get(key)
        if entry is None:
            return None
        if not isinstance(entry, dict):  # 模拟真实后端的 WRONGTYPE
            raise CacheError(f"键 {key} 存的是 {type(entry).__name__}，不能按 hash 访问")
        return entry

    async def hash_set(self, key: str, items: dict[str, str]) -> int:
        entry = self._hash_entry(key)
        if entry is None:
            self.data[key] = dict(items)
            return len(items)
        added = sum(1 for field in items if field not in entry)
        entry.update(items)
        return added

    async def hash_get(self, key: str, field: str) -> str | None:
        entry = self._hash_entry(key)
        if entry is None:
            return None
        value = entry.get(field)
        return value if isinstance(value, str) else None

    async def hash_get_all(self, key: str) -> dict[str, str]:
        entry = self._hash_entry(key)
        return dict(entry) if entry is not None else {}

    async def hash_delete(self, key: str, *fields: str) -> int:
        entry = self._hash_entry(key)
        if entry is None:
            return 0
        removed = sum(1 for field in fields if entry.pop(field, None) is not None)
        if not entry:
            self.data.pop(key, None)
        return removed

    async def hash_exists(self, key: str, field: str) -> bool:
        entry = self._hash_entry(key)
        return entry is not None and field in entry

    async def hash_length(self, key: str) -> int:
        entry = self._hash_entry(key)
        return 0 if entry is None else len(entry)

    async def hash_keys(self, key: str) -> list[str]:
        entry = self._hash_entry(key)
        return [] if entry is None else list(entry.keys())


# --------------------------------------------------------------------------- 图夹具
def node(node_id: str, node_type: str, **config: object) -> dict[str, object]:
    return {"id": node_id, "type": node_type, "config": dict(config)}


def edge(source: str, target: str, source_port: str = "trigger", target_port: str = "trigger") -> dict[str, str]:
    return {
        "source": source,
        "target": target,
        "source_port": source_port,
        "target_port": target_port,
    }


# --------------------------------------------------------------------------- ① ds-dict：动作
@pytest.mark.asyncio
async def test_ds_dict_set_creates_missing_variable_implicitly() -> None:
    """set 遇到变量不存在自动建字典（隐式创建，没有 new）；obj_out 送出变量名。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    node_ = WorkflowNode(id="d1", type="ds-dict-set", config={"key": "配置"})
    ctx.inputs = {"field": "主题色", "value": "蓝"}

    result = await exec_ds_dict_set(node_, ctx)

    assert fake.data["workflow:graph:w1:配置"] == {"主题色": "蓝"}
    assert result["obj_out"] == "配置"
    assert any("隐式建字典" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_ds_dict_set_and_get_roundtrip() -> None:
    """set 读改写、get 取值；整数照旧文本化（与 cache 节点同一口径）。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    node_ = WorkflowNode(id="d1", type="ds-dict-set", config={"key": "配置"})
    ctx.inputs = {"field": "模式", "value": "夜间"}
    result = await exec_ds_dict_set(node_, ctx)
    assert result["obj_out"] == "配置"

    ctx.inputs = {"field": "音量", "value": 5}
    _ = await exec_ds_dict_set(node_, ctx)
    assert fake.data["workflow:graph:w1:配置"] == {"模式": "夜间", "音量": "5"}

    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"key": "配置"})
    ctx.inputs = {"field": "模式"}
    result = await exec_ds_dict_get(node_, ctx)
    assert result["value_out"] == "夜间"


@pytest.mark.asyncio
async def test_ds_dict_get_missing_returns_default() -> None:
    """get 键不存在不算事故：送默认值（没填就是空串），流程继续。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"key": "配置"})

    ctx.inputs = {"field": "不存在的", "default": "兜底"}
    result = await exec_ds_dict_get(node_, ctx)
    assert result["value_out"] == "兜底"
    assert any("用默认值" in line for line in ctx.log)

    ctx.inputs = {"field": "不存在的"}
    result = await exec_ds_dict_get(node_, ctx)
    assert result["value_out"] == ""


@pytest.mark.asyncio
async def test_ds_dict_contains_remove_keys_length() -> None:
    """存在检测 / 删除 / 键列表 / 条数；remove 不存在的键不报错。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    for field, value in (("模式", "夜间"), ("音量", "5")):
        node_ = WorkflowNode(id="d1", type="ds-dict-set", config={"key": "配置"})
        ctx.inputs = {"field": field, "value": value}
        _ = await exec_ds_dict_set(node_, ctx)

    node_ = WorkflowNode(id="d1", type="ds-dict-contains", config={"key": "配置"})
    ctx.inputs = {"field": "模式"}
    assert (await exec_ds_dict_contains(node_, ctx))["flag"] == "true"
    ctx.inputs = {"field": "闹钟"}
    assert (await exec_ds_dict_contains(node_, ctx))["flag"] == "false"

    node_ = WorkflowNode(id="d1", type="ds-dict-keys", config={"key": "配置"})
    assert (await exec_ds_dict_keys(node_, ctx))["list_out"] == ["模式", "音量"]  # 插入序

    node_ = WorkflowNode(id="d1", type="ds-dict-length", config={"key": "配置"})
    assert (await exec_ds_dict_length(node_, ctx))["count"] == "2"

    node_ = WorkflowNode(id="d1", type="ds-dict-remove", config={"key": "配置"})
    ctx.inputs = {"field": "模式"}
    result = await exec_ds_dict_remove(node_, ctx)
    assert result["obj_out"] == "配置"
    ctx.inputs = {"field": "本来没有的"}
    _ = await exec_ds_dict_remove(node_, ctx)  # 不报错
    assert fake.data["workflow:graph:w1:配置"] == {"音量": "5"}


# --------------------------------------------------------------------------- ② ds-dict：失败语义
@pytest.mark.asyncio
async def test_ds_dict_raises_node_failure_when_variable_is_not_a_dict() -> None:
    """变量里存的不是字典（别的节点写坏了）→ 业务失败，下游跳过、流程继续。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    fake.data["workflow:graph:w1:不是字典"] = [1, 2, 3]

    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"key": "不是字典"})
    ctx.inputs = {"field": "x"}
    with pytest.raises(NodeFailure, match="不是字典"):
        _ = await exec_ds_dict_get(node_, ctx)


@pytest.mark.asyncio
async def test_ds_dict_raises_on_missing_key_field_or_bad_enums() -> None:
    """环境 / 配置问题当场抛：key 空 / field 空 / 非法动作、作用域 / 冒号 / 账号级没有归属。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", cache=fake)

    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"key": "x"})
    ctx.inputs = {"key": ""}
    with pytest.raises(ValueError, match="key 为空"):
        _ = await exec_ds_dict_get(node_, ctx)

    node_ = WorkflowNode(id="d1", type="ds-dict-set", config={"key": "x"})
    ctx.inputs = {"key": "x", "field": ""}
    with pytest.raises(ValueError, match="field"):
        _ = await exec_ds_dict_set(node_, ctx)

    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"scope": "account", "key": "x"})
    ctx_no_owner = NodeExecutionContext(cache=fake)
    ctx_no_owner.inputs = {"key": "x"}
    with pytest.raises(ValueError, match="owner_id"):
        _ = await exec_ds_dict_get(node_, ctx_no_owner)

    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"scope": "全局", "key": "x"})
    with pytest.raises(ValueError, match="作用域不合法"):
        _ = await exec_ds_dict_get(node_, ctx)

    ctx.inputs = {"key": "a:b"}
    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"key": "x"})
    with pytest.raises(ValueError, match="冒号"):
        _ = await exec_ds_dict_get(node_, ctx)


# --------------------------------------------------------------------------- ③ ds-dict：校验
def test_ds_dict_fields_are_validated() -> None:
    """作用域枚举与变量名冒号在语义阶段拦住；合法图放行。"""

    def graph_with(type_: str = "ds-dict-get", **config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("d", type_, **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "d"), edge("d", "e")],
        }

    report = validate_graph(graph_with(scope="全局", key="x"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_DICT_SCOPE"]

    report = validate_graph(graph_with(key="a:b"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_DICT_KEY"]

    report = validate_graph(graph_with(type_="ds-dict-set", scope="workflow", key="配置"))
    assert report.valid and report.errors == []

    # 变量名是「接线或手填」：都不填也合法（画布上靠接线提供变量名）
    report = validate_graph(graph_with(type_="ds-dict-set", scope="workflow"))
    assert report.valid and report.errors == []


# --------------------------------------------------------------------------- ③·对象名：变量名接线传递 + 输出变量名
@pytest.mark.asyncio
async def test_ds_dict_obj_out_carries_variable_name() -> None:
    """每个键值对节点都从 obj_out 送出本次操作的变量名（接线值 / 手填值都原样透传）。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    fake.data["workflow:graph:w1:配置"] = {"模式": "夜间"}

    node_ = WorkflowNode(id="d1", type="ds-dict-get", config={"key": "配置"})
    ctx.inputs = {"field": "模式"}
    result = await exec_ds_dict_get(node_, ctx)
    assert result["obj_out"] == "配置"
    assert result["value_out"] == "夜间"

    # 接线来的变量名原样送出（上游 obj_out -> 本节点 key）
    ctx.inputs = {"key": "用户库", "field": "模式"}
    result = await exec_ds_dict_get(node_, ctx)
    assert result["obj_out"] == "用户库"

    # 没接线时手填兜底：用 config 里的变量名
    ctx.inputs = {"field": "模式"}
    node_ = WorkflowNode(id="d1", type="ds-dict-length", config={"key": "配置"})
    assert (await exec_ds_dict_length(node_, ctx))["obj_out"] == "配置"
    node_ = WorkflowNode(id="d1", type="ds-dict-keys", config={"key": "配置"})
    assert (await exec_ds_dict_keys(node_, ctx))["obj_out"] == "配置"


@pytest.mark.asyncio
async def test_ds_dict_key_and_field_ports_are_wireable() -> None:
    """变量名与键名都能接线：线上送来的优先，config 手填只在没接线时兜底。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    # key 接线优先于 config：config 写"配置"，线上送"用户库"，落到用户库
    node_ = WorkflowNode(id="d1", type="ds-dict-set", config={"key": "配置"})
    ctx.inputs = {"key": "用户库", "field": "主题色", "value": "蓝"}
    result = await exec_ds_dict_set(node_, ctx)
    assert fake.data["workflow:graph:w1:用户库"] == {"主题色": "蓝"}
    assert fake.data.get("workflow:graph:w1:配置") is None
    assert result["obj_out"] == "用户库"

    # field 接线优先于 config
    node_ = WorkflowNode(id="d1", type="ds-dict-set", config={"key": "用户库", "field": "手填"})
    ctx.inputs = {"field": "主题色", "value": "红"}
    _ = await exec_ds_dict_set(node_, ctx)
    assert fake.data["workflow:graph:w1:用户库"] == {"主题色": "红"}  # 写的是线上值，不是"手填"

    # key 不接线、config 也不填 → 运行时当场抛（画布上靠接线提供变量名）
    node_ = WorkflowNode(id="d2", type="ds-dict-get", config={})
    ctx.inputs = {"field": "x"}
    with pytest.raises(ValueError, match="key 为空"):
        _ = await exec_ds_dict_get(node_, ctx)


@pytest.mark.asyncio
async def test_ds_dict_object_name_chaining_writes_same_object() -> None:
    """对象名链式传递：set1 的 obj_out -> set2 的 key，后续节点接着写同一个对象。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    node1 = WorkflowNode(id="d1", type="ds-dict-set", config={"key": "用户库"})
    ctx.inputs = {"field": "主题色", "value": "蓝"}
    result1 = await exec_ds_dict_set(node1, ctx)
    assert result1["obj_out"] == "用户库"

    # 第二个 set 不手填变量名，key 接上一个 set 的 obj_out
    node2 = WorkflowNode(id="d2", type="ds-dict-set", config={})
    ctx.inputs = {"key": result1["obj_out"], "field": "模式", "value": "夜间"}
    result2 = await exec_ds_dict_set(node2, ctx)
    assert result2["obj_out"] == "用户库"
    assert fake.data["workflow:graph:w1:用户库"] == {"主题色": "蓝", "模式": "夜间"}

    # contains 把变量名继续送出，接给 remove
    node3 = WorkflowNode(id="d3", type="ds-dict-contains", config={})
    ctx.inputs = {"key": result2["obj_out"], "field": "模式"}
    result3 = await exec_ds_dict_contains(node3, ctx)
    assert result3["flag"] == "true" and result3["obj_out"] == "用户库"

    node4 = WorkflowNode(id="d4", type="ds-dict-remove", config={})
    ctx.inputs = {"key": result3["obj_out"], "field": "模式"}
    _ = await exec_ds_dict_remove(node4, ctx)
    assert fake.data["workflow:graph:w1:用户库"] == {"主题色": "蓝"}


# --------------------------------------------------------------------------- ④ 集成：跑一张含 ds 节点的小图
@pytest.mark.asyncio
async def test_ds_dict_runs_inside_a_real_graph() -> None:
    """真实跑图：trigger -> ds-dict-set -> end，缓存键按作用域落在图级前缀下。"""
    from tickneko.workflow import SimpleWorkflowRunner

    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("d", "ds-dict-set", key="配置", field="主题色", value="蓝"),
                node("e", "end"),
            ],
            "edges": [edge("s", "d"), edge("d", "e")],
        }
    )

    await SimpleWorkflowRunner().run(graph, ctx)

    assert fake.data["workflow:graph:w1:配置"] == {"主题色": "蓝"}


@pytest.mark.asyncio
async def test_ds_dict_contains_branches_in_a_real_graph() -> None:
    """真实跑图：查存在是分流节点 —— 存在走 true 出口、不存在走 false，没走的分支整段跳过。"""
    from tickneko.workflow import SimpleWorkflowRunner

    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    def build() -> WorkflowGraph:
        return WorkflowGraph.model_validate(
            {
                "nodes": [
                    node("s", "trigger-message"),
                    node("c", "ds-dict-contains", key="配置", field="主题色"),
                    node("ta", "test"),
                    node("tb", "test"),
                    node("ea", "end"),
                    node("eb", "end"),
                ],
                "edges": [
                    edge("s", "c"),
                    edge("c", "ta", source_port="true"),
                    edge("c", "tb", source_port="false"),
                    edge("ta", "ea"),
                    edge("tb", "eb"),
                ],
            }
        )

    # 存在：true 分支跑，false 分支整段跳过（[skip] 留痕）
    fake.data["workflow:graph:w1:配置"] = {"主题色": "蓝"}
    ctx.log.clear()
    await SimpleWorkflowRunner().run(build(), ctx)
    log = "\n".join(ctx.log)
    assert "[test] ta" in log and "[skip] tb" in log

    # 不存在：false 分支跑，true 分支整段跳过
    fake.data["workflow:graph:w1:配置"] = {"音量": "5"}
    ctx.log.clear()
    await SimpleWorkflowRunner().run(build(), ctx)
    log = "\n".join(ctx.log)
    assert "[skip] ta" in log and "[test] tb" in log


@pytest.mark.asyncio
async def test_ds_dict_contains_trigger_port_always_fires() -> None:
    """真实跑图：contains 的 trigger 出口恒触发 —— 存在不存在都不挡这条链，同时分流照旧。"""
    from tickneko.workflow import SimpleWorkflowRunner

    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    def build() -> WorkflowGraph:
        return WorkflowGraph.model_validate(
            {
                "nodes": [
                    node("s", "trigger-message"),
                    node("c", "ds-dict-contains", key="配置", field="主题色"),
                    node("tal", "test"),
                    node("ta", "test"),
                    node("tb", "test"),
                    node("eal", "end"),
                    node("ea", "end"),
                    node("eb", "end"),
                ],
                "edges": [
                    edge("s", "c"),
                    edge("c", "tal", source_port="trigger"),
                    edge("c", "ta", source_port="true"),
                    edge("c", "tb", source_port="false"),
                    edge("tal", "eal"),
                    edge("ta", "ea"),
                    edge("tb", "eb"),
                ],
            }
        )

    # 存在：trigger 链与 true 链都跑，false 整段跳过
    fake.data["workflow:graph:w1:配置"] = {"主题色": "蓝"}
    ctx.log.clear()
    await SimpleWorkflowRunner().run(build(), ctx)
    log = "\n".join(ctx.log)
    assert "[test] tal" in log and "[test] ta" in log and "[skip] tb" in log

    # 不存在：trigger 链与 false 链都跑，true 整段跳过
    fake.data["workflow:graph:w1:配置"] = {"音量": "5"}
    ctx.log.clear()
    await SimpleWorkflowRunner().run(build(), ctx)
    log = "\n".join(ctx.log)
    assert "[test] tal" in log and "[skip] ta" in log and "[test] tb" in log


# --------------------------------------------------------------------------- ⑤ ds-container：list 组
@pytest.mark.asyncio
async def test_ds_container_list_append_get_contains_remove_length() -> None:
    """列表的整套操作：追加 / 按下标取 / 存在检测 / 删除 / 长度；越界取送默认值。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_append", "key": "队列"})
    ctx.inputs = {"item": "A"}
    assert (await exec_ds_container(node_, ctx))["list_out"] == ["A"]
    ctx.inputs = {"item": "B"}
    assert (await exec_ds_container(node_, ctx))["list_out"] == ["A", "B"]
    assert fake.data["workflow:graph:w1:队列"] == ["A", "B"]

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_get", "key": "队列"})
    ctx.inputs = {"index": "0"}
    assert (await exec_ds_container(node_, ctx))["value_out"] == "A"
    ctx.inputs = {"index": "-1"}  # 负数从后往前
    assert (await exec_ds_container(node_, ctx))["value_out"] == "B"
    ctx.inputs = {"index": "9", "default": "没有"}
    assert (await exec_ds_container(node_, ctx))["value_out"] == "没有"

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_contains", "key": "队列"})
    ctx.inputs = {"item": "A"}
    assert (await exec_ds_container(node_, ctx))["flag"] == "true"
    ctx.inputs = {"item": "C"}
    assert (await exec_ds_container(node_, ctx))["flag"] == "false"

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_remove", "key": "队列"})
    ctx.inputs = {"index": "0"}
    assert (await exec_ds_container(node_, ctx))["list_out"] == ["B"]
    ctx.inputs = {"index": "9"}  # 越界：不删，流程继续
    assert (await exec_ds_container(node_, ctx))["list_out"] == ["B"]

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_length", "key": "队列"})
    assert (await exec_ds_container(node_, ctx))["count"] == "1"


# --------------------------------------------------------------------------- ⑨ ds-container：map 组
@pytest.mark.asyncio
async def test_ds_container_map_set_get_contains_remove_keys_length() -> None:
    """字典的整套操作：写 / 读 / 存在检测 / 删 / 键列表 / 条目数；键不存在取送默认值。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_set", "key": "配置"})
    ctx.inputs = {"field": "主题色", "value": "蓝"}
    assert (await exec_ds_container(node_, ctx))["dict_out"] == {"主题色": "蓝"}
    ctx.inputs = {"field": "字号", "value": 14}  # 整数照旧文本化
    assert (await exec_ds_container(node_, ctx))["dict_out"] == {"主题色": "蓝", "字号": "14"}
    assert fake.data["workflow:graph:w1:配置"] == {"主题色": "蓝", "字号": "14"}

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_get", "key": "配置"})
    ctx.inputs = {"field": "主题色"}
    assert (await exec_ds_container(node_, ctx))["value_out"] == "蓝"
    ctx.inputs = {"field": "不存在的", "default": "兜底"}
    assert (await exec_ds_container(node_, ctx))["value_out"] == "兜底"

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_contains", "key": "配置"})
    ctx.inputs = {"field": "字号"}
    assert (await exec_ds_container(node_, ctx))["flag"] == "true"

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_remove", "key": "配置"})
    ctx.inputs = {"field": "字号"}
    assert (await exec_ds_container(node_, ctx))["dict_out"] == {"主题色": "蓝"}

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_keys", "key": "配置"})
    assert (await exec_ds_container(node_, ctx))["list_out"] == ["主题色"]

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_length", "key": "配置"})
    assert (await exec_ds_container(node_, ctx))["count"] == "1"


# --------------------------------------------------------------------------- ⑩ ds-container：失败语义与校验
@pytest.mark.asyncio
async def test_ds_container_raises_node_failure_on_wrong_container_type() -> None:
    """list 动作读到字典 / map 动作读到列表 -> 业务失败（NodeFailure）。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    fake.data["workflow:graph:w1:混用"] = {"a": 1}

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_length", "key": "混用"})
    with pytest.raises(NodeFailure, match="不是列表"):
        _ = await exec_ds_container(node_, ctx)

    fake.data["workflow:graph:w1:混用"] = [1, 2]
    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_length", "key": "混用"})
    with pytest.raises(NodeFailure, match="不是字典"):
        _ = await exec_ds_container(node_, ctx)


@pytest.mark.asyncio
async def test_ds_container_raises_on_bad_index_field_or_enums() -> None:
    """格式错当场抛：index 不是整数 / field 空 / key 空 / 非法动作、作用域。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_get", "key": "x"})
    ctx.inputs = {"index": "abc"}
    with pytest.raises(ValueError, match="不是整数"):
        _ = await exec_ds_container(node_, ctx)

    ctx.inputs = {"index": ""}
    with pytest.raises(ValueError, match="index 为空"):
        _ = await exec_ds_container(node_, ctx)

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_get", "key": "x"})
    ctx.inputs = {"field": ""}
    with pytest.raises(ValueError, match="field"):
        _ = await exec_ds_container(node_, ctx)

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_get"})
    ctx.inputs = {"key": ""}
    with pytest.raises(ValueError, match="key 为空"):
        _ = await exec_ds_container(node_, ctx)

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "list_遍历", "key": "x"})
    with pytest.raises(ValueError, match="动作不合法"):
        _ = await exec_ds_container(node_, ctx)

    node_ = WorkflowNode(id="c1", type="ds-container", config={"action": "map_get", "scope": "全局", "key": "x"})
    with pytest.raises(ValueError, match="作用域不合法"):
        _ = await exec_ds_container(node_, ctx)


def test_ds_container_fields_are_validated() -> None:
    """动作 / 作用域枚举与变量名冒号在语义阶段拦住；合法图放行。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("c", "ds-container", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "c"), edge("c", "e")],
        }

    report = validate_graph(graph_with(action="list_遍历", key="x"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_CONTAINER_ACTION"]

    report = validate_graph(graph_with(action="map_get", scope="全局", key="x"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_CONTAINER_SCOPE"]

    report = validate_graph(graph_with(action="map_get", key="a:b"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_CONTAINER_KEY"]

    report = validate_graph(graph_with(action="list_append", scope="workflow", key="队列"))
    assert report.valid and report.errors == []
