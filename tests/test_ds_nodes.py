"""数据结构节点（ds-dict / ds-container）的测试：直接调 exec + 注入假缓存门面。

单元测试不碰进程级缓存单例（它没 ``start()``，直接调会抛 ``CacheError``）—— 给 ctx 注入
``_FakeCache``（鸭子形状对齐 ``tickneko.core.cache.Cache`` 的 ``get_json`` / ``set_json``）即可。
"""
from __future__ import annotations

import pytest

from tickneko.workflow import (
    NodeExecutionContext,
    WorkflowGraph,
    WorkflowNode,
    validate_graph,
)
from tickneko.workflow.nodes import NodeFailure, exec_ds_dict, exec_ds_container


# --------------------------------------------------------------------------- 假缓存门面
class _FakeCache:
    """假缓存门面：``get_json`` / ``set_json`` 落到普通 dict（ds 系列节点只用到这俩）。"""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def set_json(self, key: str, value: object) -> None:
        self.data[key] = value

    async def get_json(self, key: str) -> object | None:
        return self.data.get(key)


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
async def test_ds_dict_new_writes_dict_to_cache_and_outputs_object() -> None:
    """new：接 dict 端口的初值原样存进缓存（作用域前缀），dict_out 回传字典对象。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "new", "key": "配置"})
    ctx.inputs = {"items": {"主题色": "蓝", "字号": 14}}  # dict 端口原样投递

    result = await exec_ds_dict(node_, ctx)

    assert fake.data["workflow:graph:w1:配置"] == {"主题色": "蓝", "字号": 14}
    assert result["dict_out"] == {"主题色": "蓝", "字号": 14}
    assert any("[ds-dict] d1: new workflow:graph:w1:配置" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_ds_dict_new_without_items_starts_empty() -> None:
    """new 不带初值：建一份空字典；手填 JSON 兜底也能当初值。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "new", "key": "空"})
    result = await exec_ds_dict(node_, ctx)
    assert fake.data["workflow:graph:w1:空"] == {}
    assert result["dict_out"] == {}

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "new", "key": "手填", "items": '{"a": 1}'})
    _ = await exec_ds_dict(node_, ctx)
    assert fake.data["workflow:graph:w1:手填"] == {"a": 1}


@pytest.mark.asyncio
async def test_ds_dict_set_and_get_roundtrip() -> None:
    """set 读改写、get 取值；整数照旧文本化（与 cache 节点同一口径）。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "set", "key": "配置"})
    ctx.inputs = {"field": "模式", "value": "夜间"}
    result = await exec_ds_dict(node_, ctx)
    assert result["dict_out"] == {"模式": "夜间"}

    ctx.inputs = {"field": "音量", "value": 5}
    _ = await exec_ds_dict(node_, ctx)
    assert fake.data["workflow:graph:w1:配置"] == {"模式": "夜间", "音量": "5"}

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "get", "key": "配置"})
    ctx.inputs = {"field": "模式"}
    result = await exec_ds_dict(node_, ctx)
    assert result["value_out"] == "夜间"


@pytest.mark.asyncio
async def test_ds_dict_get_missing_returns_default() -> None:
    """get 键不存在不算事故：送默认值（没填就是空串），流程继续。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "get", "key": "配置"})

    ctx.inputs = {"field": "不存在的", "default": "兜底"}
    result = await exec_ds_dict(node_, ctx)
    assert result["value_out"] == "兜底"
    assert any("用默认值" in line for line in ctx.log)

    ctx.inputs = {"field": "不存在的"}
    result = await exec_ds_dict(node_, ctx)
    assert result["value_out"] == ""


@pytest.mark.asyncio
async def test_ds_dict_contains_remove_keys_length() -> None:
    """存在检测 / 删除 / 键列表 / 条数；remove 不存在的键不报错。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    for field, value in (("模式", "夜间"), ("音量", "5")):
        node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "set", "key": "配置"})
        ctx.inputs = {"field": field, "value": value}
        _ = await exec_ds_dict(node_, ctx)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "contains", "key": "配置"})
    ctx.inputs = {"field": "模式"}
    assert (await exec_ds_dict(node_, ctx))["flag"] == "true"
    ctx.inputs = {"field": "闹钟"}
    assert (await exec_ds_dict(node_, ctx))["flag"] == "false"

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "keys", "key": "配置"})
    assert (await exec_ds_dict(node_, ctx))["list_out"] == ["模式", "音量"]  # 插入序

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "length", "key": "配置"})
    assert (await exec_ds_dict(node_, ctx))["count"] == "2"

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "remove", "key": "配置"})
    ctx.inputs = {"field": "模式"}
    result = await exec_ds_dict(node_, ctx)
    assert result["dict_out"] == {"音量": "5"}
    ctx.inputs = {"field": "本来没有的"}
    _ = await exec_ds_dict(node_, ctx)  # 不报错
    assert fake.data["workflow:graph:w1:配置"] == {"音量": "5"}


# --------------------------------------------------------------------------- ② ds-dict：失败语义
@pytest.mark.asyncio
async def test_ds_dict_raises_node_failure_when_variable_is_not_a_dict() -> None:
    """变量里存的不是字典（别的节点写坏了）→ 业务失败，下游跳过、流程继续。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    fake.data["workflow:graph:w1:不是字典"] = [1, 2, 3]

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "get", "key": "不是字典"})
    ctx.inputs = {"field": "x"}
    with pytest.raises(NodeFailure, match="不是字典"):
        _ = await exec_ds_dict(node_, ctx)


@pytest.mark.asyncio
async def test_ds_dict_raises_on_missing_key_field_or_bad_enums() -> None:
    """环境 / 配置问题当场抛：key 空 / field 空 / 非法动作、作用域 / 冒号 / 账号级没有归属。"""
    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", cache=fake)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "get"})
    ctx.inputs = {"key": ""}
    with pytest.raises(ValueError, match="key 为空"):
        _ = await exec_ds_dict(node_, ctx)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "set", "key": "x"})
    ctx.inputs = {"key": "x", "field": ""}
    with pytest.raises(ValueError, match="field"):
        _ = await exec_ds_dict(node_, ctx)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "get", "scope": "account"})
    ctx_no_owner = NodeExecutionContext(cache=fake)
    ctx_no_owner.inputs = {"key": "x"}
    with pytest.raises(ValueError, match="owner_id"):
        _ = await exec_ds_dict(node_, ctx_no_owner)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "删掉", "key": "x"})
    with pytest.raises(ValueError, match="动作不合法"):
        _ = await exec_ds_dict(node_, ctx)

    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "get", "scope": "全局", "key": "x"})
    with pytest.raises(ValueError, match="作用域不合法"):
        _ = await exec_ds_dict(node_, ctx)

    ctx.inputs = {"key": "a:b"}
    node_ = WorkflowNode(id="d1", type="ds-dict", config={"action": "get"})
    with pytest.raises(ValueError, match="冒号"):
        _ = await exec_ds_dict(node_, ctx)


# --------------------------------------------------------------------------- ③ ds-dict：校验
def test_ds_dict_fields_are_validated() -> None:
    """动作 / 作用域枚举与变量名冒号在语义阶段拦住；合法图放行。"""

    def graph_with(**config: object) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "trigger-message"),
                node("d", "ds-dict", **config),
                node("e", "end"),
            ],
            "edges": [edge("s", "d"), edge("d", "e")],
        }

    report = validate_graph(graph_with(action="删掉", key="x"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_DICT_ACTION"]

    report = validate_graph(graph_with(action="get", scope="全局", key="x"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_DICT_SCOPE"]

    report = validate_graph(graph_with(action="get", key="a:b"))
    assert not report.valid
    assert [issue.code for issue in report.errors] == ["INVALID_DS_DICT_KEY"]

    report = validate_graph(graph_with(action="new", scope="workflow", key="配置"))
    assert report.valid and report.errors == []


# --------------------------------------------------------------------------- ④ 集成：跑一张含 ds 节点的小图
@pytest.mark.asyncio
async def test_ds_dict_runs_inside_a_real_graph() -> None:
    """真实跑图：trigger -> ds-dict(set) -> end，缓存键按作用域落在图级前缀下。"""
    from tickneko.workflow import SimpleWorkflowRunner

    fake = _FakeCache()
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=fake)
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "trigger-message"),
                node("d", "ds-dict", action="set", key="配置", field="主题色", value="蓝"),
                node("e", "end"),
            ],
            "edges": [edge("s", "d"), edge("d", "e")],
        }
    )

    await SimpleWorkflowRunner().run(graph, ctx)

    assert fake.data["workflow:graph:w1:配置"] == {"主题色": "蓝"}


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
