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
                yield Env(client=client, cache=store, workflows=workflows)
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
