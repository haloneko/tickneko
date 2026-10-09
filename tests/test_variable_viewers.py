"""节点查看器契约与真实缓存语义；Redis 沿用项目的本机可用时集成验证。"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from tickneko.core.cache import Cache, CacheOptions, RedisOptions
from tickneko.workflow.models import WorkflowNode
from tickneko.workflow.nodes import (
    JsonViewer,
    ListViewer,
    NodeExecutionContext,
    NodeSpec,
    VariableContext,
    VariableRule,
    declare_node_type,
    exec_ds_list_append,
    exec_ds_list_contains,
    exec_ds_list_length,
    exec_ds_list_pop,
    exec_ds_list_pop_left,
    get_spec,
    register_executor,
    register_node,
    registered_variable_viewers,
    registry,
)
from tickneko.workflow.nodes.ds_container import DsListViewer
from tickneko.workflow.nodes.ds_dict import DsDictViewer


@pytest.fixture
async def memory() -> AsyncIterator[Cache]:
    cache = Cache(CacheOptions(backend="memory"))
    await cache.start()
    try:
        yield cache
    finally:
        await cache.stop()


@pytest.fixture(params=["memory", "redis"])
async def family_cache(request: pytest.FixtureRequest) -> AsyncIterator[Cache]:
    if request.param == "redis":
        pytest.importorskip("redis")
        try:
            with socket.create_connection(("127.0.0.1", 6379), timeout=0.25):
                pass
        except OSError:
            pytest.skip("本机没有可用的 Redis，跳过键族集成用例")
    cache = Cache(
        CacheOptions(
            backend=request.param,
            namespace=f"tickneko:viewer-test:{uuid4().hex}",
            redis=RedisOptions(socket_timeout=1.0, connect_retries=0),
        )
    )
    await cache.start()
    try:
        yield cache
    finally:
        await cache.clear()
        await cache.stop()


def context(cache: Cache) -> VariableContext:
    return VariableContext(cache, "graph", "u-admin", "w1", "队列")


def test_all_registration_paths_collect_viewers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))
    assert NodeSpec("plain").variable_viewer is None
    declare_node_type("viewer-declared", variable_viewer=DsListViewer)

    @register_node("viewer-decorated", variable_viewer=DsListViewer)
    async def execute(
        node: WorkflowNode, ctx: NodeExecutionContext
    ) -> dict[str, object]:
        return {}

    register_executor("viewer-manual", execute, variable_viewer=DsListViewer)
    for name in ("viewer-declared", "viewer-decorated", "viewer-manual", "ds-list-pop"):
        assert get_spec(name).variable_viewer is DsListViewer
    assert registered_variable_viewers().count(DsListViewer) == 1


def test_viewer_rules_own_scope_name_and_actions(memory: Cache) -> None:
    node = WorkflowNode(
        id="write",
        type="custom",
        config={"slot": "队列", "scope": "workflow", "action": "write"},
    )
    bound = VariableContext(memory, "graph", "u-admin", "w1", "队列", node)
    assert VariableRule(key_field="slot", actions=("write",)).matches(bound)
    assert not VariableRule(key_field="slot", actions=("read",)).matches(bound)
    assert not VariableRule(key_field="slot", scopes=("account",)).matches(bound)
    assert VariableRule(name_pattern="队.*").matches(context(memory))


async def test_ds_list_replace_clear_and_recreate(family_cache: Cache) -> None:
    bound = context(family_cache)
    viewer = DsListViewer(bound)
    await viewer.add(items=["a", "a", "b"])
    view = await viewer.view()
    assert (view.type, view.data, view.length) == ("list", ["a", "a", "b"], 3)
    assert await family_cache.hash_get_all(f"{bound.full_key}:meta") == {
        "a": "2",
        "b": "1",
    }
    await viewer.add(items=["b", "b"])
    assert await family_cache.list_range(bound.full_key) == ["b", "b"]
    assert await family_cache.hash_get_all(f"{bound.full_key}:meta") == {"b": "2"}
    await viewer.add(items=[])
    assert not await family_cache.exists(bound.full_key)
    assert not await family_cache.exists(f"{bound.full_key}:meta")
    await viewer.add(item="")
    assert await family_cache.list_range(bound.full_key) == [""]
    assert await family_cache.hash_get_all(f"{bound.full_key}:meta") == {"": "1"}


async def test_runtime_pops_delete_last_hash_field(family_cache: Cache) -> None:
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=family_cache)
    node = WorkflowNode(id="queue", type="ds-list-append", config={"key": "队列"})
    for item in ("", "a", "a"):
        ctx.inputs = {"item": item}
        await exec_ds_list_append(node, ctx)
    key = context(family_cache).full_key
    assert (await exec_ds_list_pop_left(node, ctx))["value_out"] == ""
    assert not await family_cache.hash_exists(f"{key}:meta", "")
    assert (await exec_ds_list_pop(node, ctx))["value_out"] == "a"
    ctx.inputs = {"item": "a"}
    assert (await exec_ds_list_contains(node, ctx))["flag"] == "true"
    assert (await exec_ds_list_pop(node, ctx))["value_out"] == "a"
    assert not await family_cache.exists(
        f"{key}:meta"
    )  # 真 Redis 的 HDEL 删空行为也验证。
    await family_cache.hash_set(f"{key}:meta", {"stale": "10"})
    ctx.inputs = {"default": "空了"}
    assert (await exec_ds_list_pop(node, ctx))["value_out"] == "空了"
    assert not await family_cache.exists(f"{key}:meta")


async def test_body_is_authority_probe_and_view_are_read_only(memory: Cache) -> None:
    bound = context(memory)
    await memory.list_push_right(bound.full_key, "a", "a", "b")
    viewer = DsListViewer(bound)
    assert not await DsListViewer.probe(bound)
    assert (await viewer.view()).length == 3
    assert not await memory.exists(f"{bound.full_key}:meta")
    await memory.hash_set(f"{bound.full_key}:meta", {"wrong": "999"})
    assert await DsListViewer.probe(bound)
    assert (await viewer.view()).data == ["a", "a", "b"]
    ctx = NodeExecutionContext(owner_id="u-admin", workflow_id="w1", cache=memory)
    node = WorkflowNode(id="queue", type="ds-list-length", config={"key": "队列"})
    assert (await exec_ds_list_length(node, ctx))["count"] == "3"
    assert await memory.hash_get_all(f"{bound.full_key}:meta") == {"wrong": "999"}


@pytest.mark.parametrize("legacy", [False, True])
async def test_add_rebuilds_missing_or_old_auxiliary_counts(
    memory: Cache, legacy: bool
) -> None:
    bound = context(memory)
    await memory.list_push_right(bound.full_key, "a", "a", "b")
    if legacy:
        await memory.hash_set(f"{bound.full_key}:meta", {"count": "999"})
        await memory.hash_set(f"{bound.full_key}:idx", {"wrong": "100"})
        before = await memory.hash_get_all(f"{bound.full_key}:meta")
        assert await DsListViewer.probe(bound)
        await DsListViewer(bound).view()
        assert await memory.hash_get_all(f"{bound.full_key}:meta") == before
        assert await memory.exists(f"{bound.full_key}:idx")
    await DsListViewer(bound).add(item="a")
    assert await memory.hash_get_all(f"{bound.full_key}:meta") == {"a": "3", "b": "1"}
    assert not await memory.exists(f"{bound.full_key}:idx")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"item": "x", "items": []},
        {"items": {}},
        {"items": ["x", {}]},
        {"item": []},
        {"item": "x", "unknown": True},
        {"items": None},
    ],
)
async def test_list_validation_finishes_before_writes(
    memory: Cache, params: dict[str, object]
) -> None:
    bound = context(memory)
    viewer = DsListViewer(bound)
    await viewer.add(items=["original"])
    with pytest.raises(ValueError):
        await viewer.add(**params)
    assert await memory.list_range(bound.full_key) == ["original"]
    assert await memory.hash_get_all(f"{bound.full_key}:meta") == {"original": "1"}


async def test_dict_full_replace_and_scalar_text_encoding(memory: Cache) -> None:
    bound = VariableContext(memory, "account", "u-admin", "", "字典")
    viewer = DsDictViewer(bound)
    await viewer.add(fields={"old": "x", "kept": "y"})
    await viewer.add(fields={"kept": "new", "zero": 0, "false": False, "empty": None})
    assert (await viewer.view()).data == {
        "kept": "new",
        "zero": "0",
        "false": "False",
        "empty": "",
    }
    with pytest.raises(ValueError):
        await viewer.add(fields={"valid": "x", "invalid": []})
    assert not await memory.hash_exists(bound.full_key, "valid")
    await viewer.add(fields={})
    assert not await memory.exists(bound.full_key)
    await viewer.add(field="new", value="")
    assert (await viewer.view()).data == {"new": ""}


@pytest.mark.parametrize("value", [0, False, None, [], {}])
async def test_json_keeps_value_types(memory: Cache, value: object) -> None:
    bound = VariableContext(memory, "account", "u-admin", "", "JSON")
    viewer = JsonViewer(bound)
    await viewer.add(value=value)
    result = await viewer.view()
    assert result.type == "json" and result.data == value
    assert type(result.data) is type(value)
    assert await JsonViewer.probe(bound)


async def test_registered_plain_list_can_edit_without_creating_family(
    memory: Cache,
) -> None:
    bound = context(memory)
    viewer = ListViewer(bound)  # 已有节点声明，而非来源丢失时的只读 probe。
    await viewer.add(items=["a", "a", ""])
    assert (await viewer.view()).data == ["a", "a", ""]
    assert not await memory.exists(f"{bound.full_key}:meta")
    with pytest.raises(ValueError):
        await ListViewer(bound, editable=False).add(item="x")
