"""变量查看接口的测试：作用域 / 归属过滤，以及「管理员看全部、普通用户只看自己」的权限口径。

请求不走真实网络：用 ``httpx.AsyncClient`` 挂 ``ASGITransport`` 直接打进 ASGI 应用。
变量写在缓存里（直接往缓存塞 ``workflow:graph:…`` / ``workflow:acct:…`` 这些键），
接口再把键反解成结构化字段 —— 这里验证的正是这一步与归属隔离。

需要 ``fastapi`` / ``httpx``；没装就整文件跳过。
"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("fastapi", reason='接口层要装 fastapi：pip install "tickneko[api]"')
pytest.importorskip("httpx", reason='接口层测试用 httpx 发请求：pip install "tickneko[dev]"')

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from tickneko.api import ApiOptions, create_app  # noqa: E402
from tickneko.api.services.user.security import Pbkdf2PasswordHasher  # noqa: E402
from tickneko.api.services.user.store_sql import SqlUserStore  # noqa: E402
from tickneko.core.cache import Cache, CacheOptions  # noqa: E402
from tickneko.workflow import SqlWorkflowStore  # noqa: E402
from tickneko.workflow.nodes import registry  # noqa: E402
from tickneko.workflow.nodes import (  # noqa: E402
    StringViewer, VariableContext, declare_node_type,
)

#: 演示账号（见 tickneko.api.services.user.demo）—— 唯一自带的账号，且是管理员
ADMIN = {"account": "admin", "password": "tickneko-admin"}
#: 普通账号：由用例顺手注册（注册要昵称）
PLAIN = {"account": "robot", "password": "tickneko-robot", "nickname": "巡检机器人"}

LOGIN_PATH = "/api/auth/login"
REGISTER_PATH = "/api/auth/register"
VARIABLES_PATH = "/api/variables"

#: 测试用哈希迭代次数（生产默认 20 万次，用例不必每次重付这笔钱）
_TEST_HASHER = Pbkdf2PasswordHasher(iterations=1_000)


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """一个直连 ASGI 应用的异步客户端（顺带把 app 的 lifespan 跑起来建表）。"""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


@dataclass
class Env:
    """一个装配好的测试环境：客户端 + 缓存门面 + 工作流存储（用例自己往里塞数据）。"""

    client: httpx.AsyncClient
    cache: Cache
    workflows: SqlWorkflowStore
    app: FastAPI


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    """接口层 + 一块内存 sqlite（用户 / 工作流）+ 一份跑起来的本地缓存。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        users = SqlUserStore(engine, hasher=_TEST_HASHER)
        await users.ensure_schema()
        await users.seed_demo()
        workflows = SqlWorkflowStore(engine)
        await workflows.ensure_schema()
        store = Cache(CacheOptions(backend="memory"))
        await store.start()
        try:
            app = create_app(
                ApiOptions(prefix="/api", token_ttl=1800.0),
                user_store=users,
                cache=store,
                workflow_store=workflows,
            )
            async with client_for(app) as client:
                yield Env(client=client, cache=store, workflows=workflows, app=app)
        finally:
            await store.stop()
    finally:
        await engine.dispose()


def auth(token: str) -> dict[str, str]:
    """带令牌的请求头。"""
    return {"Authorization": f"Bearer {token}"}


async def login(client: httpx.AsyncClient, creds: dict[str, str]) -> tuple[str, str]:
    """登录，返回 ``(令牌, 归属 id)``。"""
    response = await client.post(LOGIN_PATH, json=creds)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return data["token"], data["user"]["id"]


async def register(client: httpx.AsyncClient, creds: dict[str, str]) -> tuple[str, str]:
    """注册一个普通账号并登录，返回 ``(令牌, 归属 id)``。"""
    response = await client.post(REGISTER_PATH, json=creds)
    assert response.status_code == 201, response.text
    return await login(client, creds)


def page_of(response: httpx.Response) -> dict[str, object]:
    """取出响应壳里的 ``data``（顺带确认是成功响应）。"""
    assert response.status_code == 200, response.text
    return response.json()["data"]


class TestVariables:
    async def test_requires_login(self, env: Env) -> None:
        """没登录一律 401，不往外说有哪些变量。"""
        response = await env.client.get(VARIABLES_PATH)
        assert response.status_code == 401

    async def test_503_when_cache_is_not_wired(self) -> None:
        """没给 create_app 传缓存：503 并说清楚，而不是静默给一个空列表。"""
        app = create_app(ApiOptions(prefix="/api"), hasher=_TEST_HASHER)
        async with client_for(app) as client:
            token, _ = await login(client, ADMIN)
            response = await client.get(VARIABLES_PATH, headers=auth(token))
        assert response.status_code == 503
        assert "缓存未接入" in response.json()["error"]["message"]

    async def test_admin_sees_everyone_plain_user_only_self(self, env: Env) -> None:
        """管理员看得到所有人的变量；普通用户只看得到自己名下的。"""
        admin_token, admin_id = await login(env.client, ADMIN)
        plain_token, plain_id = await register(env.client, PLAIN)
        graph = await env.workflows.create(admin_id, "图一")

        await env.cache.set(f"workflow:acct:{admin_id}:日签", "开")
        await env.cache.set(f"workflow:acct:{plain_id}:计数", "3")
        await env.cache.set(f"workflow:graph:{graph.id}:计数", "1")

        # 管理员：三条都能看到，字段按作用域拆好
        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(admin_token)))
        assert page["total"] == 3
        rows = {(item["scope"], item["owner_id"], item["key"]): item for item in page["items"]}
        graph_row = rows[("graph", admin_id, "计数")]
        assert graph_row["workflow_id"] == graph.id
        assert graph_row["value"] == "1"
        assert graph_row["ttl"] is None  # 没设 TTL = 永不过期（JSON 里没有 Infinity）
        account_row = rows[("account", admin_id, "日签")]
        assert account_row["workflow_id"] == ""  # 账号级不挂在某张图上
        assert account_row["value"] == "开"
        assert rows[("account", plain_id, "计数")]["value"] == "3"

        # 普通用户：只看得到自己那条（图级的图不是他的，也看不到）
        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(plain_token)))
        assert page["total"] == 1
        assert [item["owner_id"] for item in page["items"]] == [plain_id]

    async def test_hash_and_list_variables_are_readable(self, env: Env) -> None:
        """哈希 / 列表变量照常展示：hash 走 hash_get_all、list 走 list_range，都序列化成 JSON 文本。

        以前只认字符串键，对哈希发 ``GET`` 会撞 WRONGTYPE 把整页打成 500 —— 现在三种结构都能看。
        """
        admin_token, admin_id = await login(env.client, ADMIN)
        await env.cache.set(f"workflow:acct:{admin_id}:开关", "开")
        await env.cache.hash_set(
            f"workflow:acct:{admin_id}:配置", {"主题色": "蓝", "模式": "夜间"}
        )
        await env.cache.list_push_right(f"workflow:acct:{admin_id}:队列", "A", "B")

        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(admin_token)))
        assert page["total"] == 3
        rows = {item["key"]: item for item in page["items"]}
        assert rows["开关"]["value"] == "开"
        assert json.loads(rows["配置"]["value"]) == {"主题色": "蓝", "模式": "夜间"}
        assert json.loads(rows["队列"]["value"]) == ["A", "B"]

    async def test_plain_user_cannot_ask_for_someone_else(self, env: Env) -> None:
        """普通用户显式写别人的归属 -> 403（和日志 / 工作流列表一个口径）。"""
        _, admin_id = await login(env.client, ADMIN)
        plain_token, _ = await register(env.client, PLAIN)
        await env.cache.set(f"workflow:acct:{admin_id}:日签", "开")

        response = await env.client.get(
            VARIABLES_PATH, headers=auth(plain_token), params={"owner_id": admin_id}
        )
        assert response.status_code == 403

    async def test_admin_can_filter_by_owner(self, env: Env) -> None:
        """管理员带 ``?owner_id=`` 就缩到那一个人。"""
        admin_token, admin_id = await login(env.client, ADMIN)
        _, plain_id = await register(env.client, PLAIN)
        await env.cache.set(f"workflow:acct:{admin_id}:日签", "开")
        await env.cache.set(f"workflow:acct:{plain_id}:计数", "3")

        page = page_of(
            await env.client.get(
                VARIABLES_PATH, headers=auth(admin_token), params={"owner_id": plain_id}
            )
        )
        assert page["total"] == 1
        assert page["items"][0]["owner_id"] == plain_id

    async def test_scope_and_keyword_filters(self, env: Env) -> None:
        """作用域与变量名两个筛子各自生效。"""
        admin_token, admin_id = await login(env.client, ADMIN)
        graph = await env.workflows.create(admin_id, "图一")
        await env.cache.set(f"workflow:acct:{admin_id}:日签", "开")
        await env.cache.set(f"workflow:graph:{graph.id}:计数", "1")

        page = page_of(
            await env.client.get(
                VARIABLES_PATH, headers=auth(admin_token), params={"scope": "graph"}
            )
        )
        assert page["total"] == 1
        assert page["items"][0]["scope"] == "graph"

        page = page_of(
            await env.client.get(
                VARIABLES_PATH, headers=auth(admin_token), params={"query": "日签"}
            )
        )
        assert page["total"] == 1
        assert page["items"][0]["key"] == "日签"

    async def test_bad_scope_is_422(self, env: Env) -> None:
        """作用域写错是写错了，别静默当成「一个都没有」。"""
        admin_token, _ = await login(env.client, ADMIN)
        response = await env.client.get(
            VARIABLES_PATH, headers=auth(admin_token), params={"scope": "nope"}
        )
        assert response.status_code == 422

    async def test_pagination(self, env: Env) -> None:
        """一页给 ``limit`` 条，``total`` 是不受分页限制的命中总数。"""
        admin_token, admin_id = await login(env.client, ADMIN)
        for index in range(5):
            await env.cache.set(f"workflow:acct:{admin_id}:k{index}", str(index))

        page = page_of(
            await env.client.get(
                VARIABLES_PATH, headers=auth(admin_token), params={"limit": 2, "offset": 0}
            )
        )
        assert page["total"] == 5
        assert len(page["items"]) == 2

        page = page_of(
            await env.client.get(
                VARIABLES_PATH, headers=auth(admin_token), params={"limit": 2, "offset": 4}
            )
        )
        assert page["total"] == 5
        assert len(page["items"]) == 1

    async def test_unrelated_keys_are_ignored(self, env: Env) -> None:
        """缓存里别的系统的键（会话令牌等）不算变量。"""
        admin_token, _ = await login(env.client, ADMIN)
        await env.cache.set("auth:token:abc", "x")
        await env.cache.set("something:else", "y")

        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(admin_token)))
        assert page["total"] == 0

    async def test_orphan_graph_variable_is_admin_only(self, env: Env) -> None:
        """图被删了：图级变量查不到归属 —— 普通用户看不到，管理员看得到（归属是空串）。"""
        admin_token, admin_id = await login(env.client, ADMIN)
        plain_token, _ = await register(env.client, PLAIN)
        graph = await env.workflows.create(admin_id, "图一")
        await env.cache.set(f"workflow:graph:{graph.id}:计数", "1")
        assert await env.workflows.delete(graph.id) is True

        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(plain_token)))
        assert page["total"] == 0

        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(admin_token)))
        assert page["total"] == 1
        assert page["items"][0]["owner_id"] == ""


async def writer_graph(env: Env, owner: str, node_type: str = "ds-list-append", key: str = "队列") -> str:
    graph = await env.workflows.create(owner, "变量写入图")
    await env.workflows.save_draft(graph.id, json.dumps({
        "nodes": [{"id": "writer", "type": node_type, "config": {"key": key, "scope": "workflow", "action": "set"}}],
        "edges": [],
    }))
    return graph.id


def graph_params(ident: str, key: str = "队列") -> dict[str, str]:
    return {"scope": "graph", "workflow_id": ident, "key": key}


class TestVariableViewers:
    async def test_discovery_filters_families_before_pagination(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        token, owner = await login(env.client, ADMIN)
        await env.cache.list_push_right(f"workflow:acct:{owner}:a", "a", "a", "b")
        await env.cache.hash_set(f"workflow:acct:{owner}:a:meta", {"a": "2", "b": "1"})
        await env.cache.hash_set(f"workflow:acct:{owner}:a:idx", {"legacy": "1"})
        await env.cache.hash_set(f"workflow:acct:{owner}:orphan:meta", {"stale": "1"})
        await env.cache.set(f"workflow:acct:{owner}:b", "text")
        keys = env.cache.keys

        async def duplicates(pattern: str = "*") -> list[str]:
            rows = await keys(pattern)
            return rows + rows  # SCAN 可以重复返回；逻辑变量仍只计一次。

        monkeypatch.setattr(env.cache, "keys", duplicates)
        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(token), params={"limit": 1}))
        assert page["total"] == 2
        assert page["items"][0]["key"] == "a"
        assert (page["items"][0]["type"], page["items"][0]["data"], page["items"][0]["length"]) == ("list", ["a", "a", "b"], 3)
        page = page_of(await env.client.get(VARIABLES_PATH, headers=auth(token), params={"limit": 1, "offset": 1}))
        assert [row["key"] for row in page["items"]] == ["b"]
        assert page_of(await env.client.get(VARIABLES_PATH, headers=auth(token), params={"query": ":meta"}))["total"] == 0

    async def test_node_source_handles_missing_meta_and_recreates_after_clear(self, env: Env) -> None:
        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner)
        key = f"workflow:graph:{ident}:队列"
        await env.cache.list_push_right(key, "a", "a", "b")
        params = graph_params(ident)
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=params))
        assert data["editable"] and data["length"] == 3
        assert not await env.cache.exists(f"{key}:meta")  # view 不做修复。
        data = page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"item": "b"}))
        assert data["data"] == ["a", "a", "b", "b"]
        assert await env.cache.hash_get_all(f"{key}:meta") == {"a": "2", "b": "2"}
        data = page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"items": ["b", "b"]}))
        assert data["length"] == 2 and await env.cache.hash_get_all(f"{key}:meta") == {"b": "2"}
        page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"items": []}))
        assert not await env.cache.exists(key) and not await env.cache.exists(f"{key}:meta")
        assert page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=params))["data"] == []
        data = page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"item": ""}))
        assert data["data"] == [""] and await env.cache.hash_get_all(f"{key}:meta") == {"": "1"}

    @pytest.mark.parametrize("scope", ["account", "orphan", "missing-node", "invalid-graph"])
    async def test_probe_fallback_preserves_family_linkage(self, env: Env, scope: str) -> None:
        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner, node_type="unloaded-extension")
        if scope == "orphan":
            await env.workflows.delete(ident)
        if scope == "invalid-graph":
            await env.workflows.save_draft(ident, "invalid JSON")
        params = {"scope": "account", "owner_id": owner, "key": "队列"} if scope == "account" else graph_params(ident)
        key = f"workflow:acct:{owner}:队列" if scope == "account" else f"workflow:graph:{ident}:队列"
        await env.cache.list_push_right(key, "a", "a")
        await env.cache.hash_set(f"{key}:meta", {"a": "2"})
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=params))
        assert data["editable"]
        page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"items": ["b", "b"]}))
        assert await env.cache.hash_get_all(f"{key}:meta") == {"b": "2"}

    @pytest.mark.parametrize("orphan", [False, True])
    async def test_unidentified_list_is_read_only(self, env: Env, orphan: bool) -> None:
        token, owner = await login(env.client, ADMIN)
        params = {"scope": "graph", "workflow_id": "deleted", "key": "队列"} if orphan else {"scope": "account", "owner_id": owner, "key": "队列"}
        key = "workflow:graph:deleted:队列" if orphan else f"workflow:acct:{owner}:队列"
        await env.cache.list_push_right(key, "a")
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=params))
        assert data["type"] == "list" and data["data"] == ["a"] and not data["editable"]
        response = await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"items": ["bad"]})
        assert response.status_code == 409
        assert await env.cache.list_range(key) == ["a"]
        assert not await env.cache.exists(f"{key}:meta")

    async def test_live_node_without_viewer_cannot_use_fallback(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))
        declare_node_type("private-variable")
        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner, "private-variable")
        key = f"workflow:graph:{ident}:队列"
        await env.cache.list_push_right(key, "a")
        await env.cache.hash_set(f"{key}:meta", {"a": "1"})
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=graph_params(ident)))
        assert data["type"] is None and not data["editable"] and "未开放" in data["reason"]
        response = await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=graph_params(ident), json={"item": "b"})
        assert response.status_code == 409 and await env.cache.list_range(key) == ["a"]

    @pytest.mark.parametrize("body", [{}, {"item": "x", "items": []}, {"items": ["x", {}]}, {"items": "x"}, {"item": "x", "unknown": 0}, {"key": "else", "item": "x"}, {"item": "x", "self": 0}])
    async def test_viewer_errors_are_422_and_leave_data_intact(self, env: Env, body: dict[str, object]) -> None:
        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner)
        key = f"workflow:graph:{ident}:队列"
        await env.cache.list_push_right(key, "original")
        response = await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=graph_params(ident), json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await env.cache.list_range(key) == ["original"]
        assert not await env.cache.exists(f"{key}:meta")

    async def test_account_dict_and_json_full_save(self, env: Env) -> None:
        token, owner = await login(env.client, ADMIN)
        params = {"scope": "account", "owner_id": owner, "key": "配置"}
        key = f"workflow:acct:{owner}:配置"
        await env.cache.hash_set(key, {"old": "x", "kept": "y"})
        data = page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"fields": {"new": ""}}))
        assert data["type"] == "dict" and data["data"] == {"new": ""}
        page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"fields": {}}))
        assert not await env.cache.exists(key)
        params["key"] = "JSON"
        await env.cache.set_json(f"workflow:acct:{owner}:JSON", {"initial": True})
        for value in (False, 0, None):
            data = page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"value": value}))
            assert data["type"] == "json" and type(data["data"]) is type(value) and data["data"] == value

    async def test_extension_receives_complete_body_and_trusted_context(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))
        seen: list[tuple[dict[str, object], VariableContext]] = []

        class ExtensionViewer(StringViewer):
            async def add(self, **params: object) -> None:
                seen.append((params, self.context))
                await self.context.cache.set(self.context.full_key, "updated")

        declare_node_type("extended-variable", variable_viewer=ExtensionViewer)
        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner, "extended-variable")
        await env.cache.set(f"workflow:graph:{ident}:队列", "initial")
        body = {"value": "x", "option": False, "offset": 0, "owner_id": "injected", "context": {"key": "else"}}
        page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=graph_params(ident), json=body))
        assert seen[0][0] == body
        assert seen[0][1].owner_id == owner and seen[0][1].key == "队列"

    async def test_equal_priority_viewers_disable_writes(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))

        class FirstViewer(StringViewer):
            priority = 100

        class SecondViewer(StringViewer):
            priority = 100

        declare_node_type("conflict-first", variable_viewer=FirstViewer)
        declare_node_type("conflict-second", variable_viewer=SecondViewer)
        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner, "conflict-first")
        await env.workflows.save_draft(ident, json.dumps({"nodes": [
            {"id": "a", "type": "conflict-first", "config": {"key": "队列"}},
            {"id": "b", "type": "conflict-second", "config": {"key": "队列"}},
        ], "edges": []}))
        key = f"workflow:graph:{ident}:队列"
        await env.cache.set(key, "original")
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=graph_params(ident)))
        assert data["type"] == "str" and not data["editable"] and "冲突" in data["reason"]
        response = await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=graph_params(ident), json={"value": "bad"})
        assert response.status_code == 409 and await env.cache.get(key) == "original"

    async def test_modify_enforces_auth_and_real_graph_ownership(self, env: Env) -> None:
        _, admin_id = await login(env.client, ADMIN)
        token, plain_id = await register(env.client, PLAIN)
        ident = await writer_graph(env, admin_id, "cache", "开关")
        params = graph_params(ident, "开关")
        key = f"workflow:graph:{ident}:开关"
        await env.cache.set(key, "original")
        assert (await env.client.post(f"{VARIABLES_PATH}/add", params=params, json={"value": "bad"})).status_code == 401
        params["owner_id"] = plain_id
        assert (await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"value": "bad"})).status_code == 403
        await env.workflows.delete(ident)
        assert (await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=params)).status_code == 403
        assert await env.cache.get(key) == "original"

    async def test_partial_storage_failure_never_returns_success(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        from tickneko.core.cache.models import CacheError

        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner)
        key = f"workflow:graph:{ident}:队列"
        await env.cache.list_push_right(key, "original")
        set_hash = env.cache.hash_set

        async def fail_meta(target: str, items: dict[str, str], ttl: float | None = None) -> int:
            if target == f"{key}:meta":
                raise CacheError("meta 写入失败")
            return await set_hash(target, items, ttl=ttl)

        monkeypatch.setattr(env.cache, "hash_set", fail_meta)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False), base_url="http://test") as client:
            response = await client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=graph_params(ident), json={"items": ["new"]})
        assert response.status_code == 500 and not response.json()["success"]
        assert await env.cache.list_range(key) == ["new"]  # 非原子边界明确暴露，而非宣称回滚。

    async def test_unknown_variable_does_not_create_probe_keys(self, env: Env) -> None:
        token, owner = await login(env.client, ADMIN)
        params = {"scope": "account", "owner_id": owner, "key": "missing"}
        assert (await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=params)).status_code == 404
        assert (await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"item": "x"})).status_code == 404
        assert await env.cache.keys("workflow:*") == []

    @pytest.mark.parametrize("body", [{}, {"field": "x"}, {"value": "x"}, {"fields": None}, {"fields": []}, {"field": "x", "value": 0, "fields": {}}, {"fields": {"valid": "x", "invalid": []}}, {"fields": {}, "unknown": False}])
    async def test_dict_errors_are_422_before_overwrite(self, env: Env, body: dict[str, object]) -> None:
        token, owner = await login(env.client, ADMIN)
        params = {"scope": "account", "owner_id": owner, "key": "配置"}
        key = f"workflow:acct:{owner}:配置"
        await env.cache.hash_set(key, {"original": "x"})
        response = await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json=body)
        assert response.status_code == 422
        assert await env.cache.hash_get_all(key) == {"original": "x"}

    async def test_body_expiring_during_discovery_does_not_fail_page(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        token, owner = await login(env.client, ADMIN)
        key = f"workflow:acct:{owner}:expired"
        await env.cache.set(key, "x")
        keys = env.cache.keys

        async def expire_after_scan(pattern: str = "*") -> list[str]:
            found = await keys(pattern)
            await env.cache.delete(key)
            return found

        monkeypatch.setattr(env.cache, "keys", expire_after_scan)
        assert page_of(await env.client.get(VARIABLES_PATH, headers=auth(token)))["items"] == []

    async def test_published_source_takes_precedence_over_new_draft(self, env: Env) -> None:
        from tickneko.workflow import canonical_graph_json, graph_checksum

        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner, "cache", "开关")
        record = await env.workflows.get(ident)
        graph = {"nodes": [{"id": "writer", "type": "cache", "config": {"key": "开关", "action": "set"}}], "edges": []}
        version, _ = await env.workflows.add_version(record, graph_json=canonical_graph_json(graph), checksum=graph_checksum(graph))
        await env.workflows.publish(ident, version.version)
        await env.workflows.save_draft(ident, json.dumps({"nodes": [{"id": "writer", "type": "ds-list-append", "config": {"key": "开关"}}], "edges": []}))
        key = f"workflow:graph:{ident}:开关"
        await env.cache.set(key, "false")
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=graph_params(ident, "开关")))
        assert data["type"] == "str" and data["data"] == "false" and data["editable"]

    async def test_declared_storage_mismatch_does_not_bypass_viewer(self, env: Env) -> None:
        token, owner = await login(env.client, ADMIN)
        ident = await writer_graph(env, owner, "cache", "开关")
        await env.cache.hash_set(f"workflow:graph:{ident}:开关", {"field": "value"})
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=graph_params(ident, "开关")))
        assert data["type"] is None and not data["editable"]

    async def test_name_rule_can_identify_account_list_without_meta(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        from tickneko.workflow.nodes import VariableRule
        from tickneko.workflow.nodes.ds_container import DsListViewer

        monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))

        class NamedListViewer(DsListViewer):
            priority = 200
            rules = (VariableRule(name_pattern="recognized-list"),)

        declare_node_type("named-list-writer", variable_viewer=NamedListViewer)
        token, owner = await login(env.client, ADMIN)
        key = f"workflow:acct:{owner}:recognized-list"
        await env.cache.list_push_right(key, "a", "a")
        params = {"scope": "account", "owner_id": owner, "key": "recognized-list"}
        data = page_of(await env.client.get(f"{VARIABLES_PATH}/value", headers=auth(token), params=params))
        assert data["editable"] and not await env.cache.exists(f"{key}:meta")
        page_of(await env.client.post(f"{VARIABLES_PATH}/add", headers=auth(token), params=params, json={"item": "b"}))
        assert await env.cache.hash_get_all(f"{key}:meta") == {"a": "2", "b": "1"}
