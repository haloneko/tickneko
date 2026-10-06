"""缓存层单元测试：统一接口、本地内存后端、Redis 后端（降级 / 真连）、配置映射。"""
from __future__ import annotations

import asyncio
import json
import math
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from textwrap import dedent

import pytest

from config import Settings
from tickneko.core.cache import (
    Cache,
    CacheBackend,
    CacheError,
    CacheOptions,
    MemoryCache,
    RedisCache,
    RedisOptions,
)


def write(tmp_path: Path, text: str) -> Path:
    """把一段 TOML 落盘，返回路径。"""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def redis_reachable(host: str, port: int, timeout: float = 0.25) -> bool:
    """Redis 在不在听：一次 TCP 握手探一下（毫秒级）。

    不能靠「让门面去连、连不上再跳过」来判断有没有服务：连不上时驱动的重试 + 建连等待
    不可控（没 Redis 服务的环境可能白等很久），不该让测试套件买单。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def stub_redis_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """把驱动的 ping 打桩成「当场拒绝连接」，绕开默认的 10 次重试退避。

    给门面留下的只有「连不上」这一个结果，正好是要测的那条分支；真实 TCP 连不上长什么样，
    是驱动自己的事（真连成功那条路径由 :func:`redis_reachable` 放行的用例覆盖）。
    """
    pytest.importorskip("redis.asyncio")  # 驱动是可选依赖：没装就跳过这个用例
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def refuse(_self: object) -> None:
        raise RedisConnectionError("连接被拒绝（本用例打桩）")

    # 属性路径交给 pytest 自己去解析：这里不必再 import 一遍驱动
    monkeypatch.setattr("redis.asyncio.Redis.ping", refuse)


@pytest.fixture
async def memory() -> AsyncIterator[Cache]:
    """默认门面（memory 后端），用例结束就停。"""
    facade = Cache()
    await facade.start()
    yield facade
    await facade.stop()


class TestMemoryBackend:
    """本地内存后端：不启用 Redis 时，上层实际用的就是它。"""

    async def test_set_get_roundtrip(self, memory: Cache) -> None:
        await memory.set("k", "v")
        assert await memory.get("k") == "v"
        assert await memory.exists("k") is True

    async def test_missing_key_reads_as_none(self, memory: Cache) -> None:
        assert await memory.get("nope") is None
        assert await memory.exists("nope") is False
        assert await memory.ttl("nope") is None

    async def test_set_overwrites_value_and_ttl(self, memory: Cache) -> None:
        await memory.set("k", "one", ttl=60)
        await memory.set("k", "two")
        assert await memory.get("k") == "two"
        assert await memory.ttl("k") == math.inf  # 覆盖时 TTL 也跟着换

    async def test_ttl_expires(self, memory: Cache) -> None:
        await memory.set("k", "v", ttl=0.05)
        assert await memory.get("k") == "v"
        await asyncio.sleep(0.1)
        assert await memory.get("k") is None  # 过期即「不存在」，与 Redis 一致

    async def test_ttl_reporting(self, memory: Cache) -> None:
        await memory.set("forever", "v")
        assert await memory.ttl("forever") == math.inf
        await memory.set("soon", "v", ttl=5)
        remaining = await memory.ttl("soon")
        assert remaining is not None and 0 < remaining <= 5

    async def test_expire_sets_deadline(self, memory: Cache) -> None:
        await memory.set("k", "v")
        assert await memory.expire("k", 0.05) is True
        await asyncio.sleep(0.1)
        assert await memory.exists("k") is False
        assert await memory.expire("nope", 5) is False  # 键不存在，设不上

    async def test_delete(self, memory: Cache) -> None:
        await memory.set("k", "v")
        assert await memory.delete("k") is True
        assert await memory.delete("k") is False

    async def test_incr_counts_from_zero(self, memory: Cache) -> None:
        assert await memory.incr("n") == 1  # 键不存在时从 0 起算
        assert await memory.incr("n") == 2
        assert await memory.incr("n", 5) == 7
        assert await memory.get("n") == "7"

    async def test_incr_rejects_non_integer(self, memory: Cache) -> None:
        await memory.set("s", "abc")
        with pytest.raises(CacheError):
            await memory.incr("s")

    async def test_incr_keeps_ttl(self, memory: Cache) -> None:
        await memory.set("n", "1", ttl=5)
        await memory.incr("n")
        remaining = await memory.ttl("n")
        assert remaining is not None and 0 < remaining <= 5  # 自增不该把 TTL 冲掉

    async def test_batch_operations(self, memory: Cache) -> None:
        await memory.set_many({"a": "1", "b": "2"})
        assert await memory.get_many(["a", "b", "c"]) == {"a": "1", "b": "2"}
        assert await memory.delete_many(["a", "b", "c"]) == 2
        assert await memory.delete_many([]) == 0

    async def test_keys_pattern(self, memory: Cache) -> None:
        await memory.set_many({"user:1": "a", "user:2": "b", "post:1": "c"})
        assert sorted(await memory.keys()) == ["post:1", "user:1", "user:2"]
        assert sorted(await memory.keys("user:*")) == ["user:1", "user:2"]

    async def test_keys_skips_expired(self, memory: Cache) -> None:
        await memory.set("gone", "v", ttl=0.05)
        await asyncio.sleep(0.1)
        assert await memory.keys() == []

    async def test_clear(self, memory: Cache) -> None:
        await memory.set_many({"a": "1", "b": "2"})
        assert await memory.clear() == 2
        assert await memory.keys() == []
        assert await memory.clear() == 0

    async def test_default_ttl_applies_when_set_omits_it(self) -> None:
        """配置里的 default_ttl 管「没写 ttl」的那些写入，显式 ttl=0 仍然永不过期。"""
        facade = Cache(CacheOptions(default_ttl=5))
        await facade.start()
        try:
            await facade.set("k", "v")
            remaining = await facade.ttl("k")
            assert remaining is not None and 0 < remaining <= 5
            await facade.set("k", "v", ttl=0)
            assert await facade.ttl("k") == math.inf
        finally:
            await facade.stop()

    async def test_sweeper_drops_expired_keys(self) -> None:
        """没人再访问的过期键由后台任务清掉（只靠惰性过期会一直占着内存）。"""
        backend = MemoryCache(sweep_interval=0.05)
        await backend.start()
        try:
            await backend.set("k", "v", ttl=0.02)
            await asyncio.sleep(0.2)
            assert backend._data == {}  # 看一眼内部存储：这里面空着就是没泄漏
        finally:
            await backend.stop()

    async def test_list_push_range_and_length(self, memory: Cache) -> None:
        assert await memory.list_push("q", "a", "b") == 2
        assert await memory.list_push("q", "c") == 3
        assert await memory.list_range("q") == ["a", "b", "c"]  # 0 -1 就是整条列表
        assert await memory.list_range("q", -2, -1) == ["b", "c"]  # 负数从右数，两端都含
        assert await memory.list_range("q", 1, 1) == ["b"]
        assert await memory.list_range("q", 5, 9) == []  # 越界裁剪成空
        assert await memory.list_length("q") == 3
        assert await memory.list_range("nope") == []  # 键不存在当空列表
        assert await memory.list_length("nope") == 0

    async def test_list_push_left_and_pop_order(self, memory: Cache) -> None:
        await memory.list_push_left("q", "b", "c")  # 左侧推：b 在 c 前面
        await memory.list_push("q", "a")
        assert await memory.list_range("q") == ["b", "c", "a"]
        assert await memory.list_pop("q", 2) == ["a", "c"]  # 按弹出顺序：最右侧的先出来
        assert await memory.list_pop("q") == ["b"]
        assert await memory.exists("q") is False  # 弹空了键就没了（与 Redis 一致）
        assert await memory.list_pop("q") == []
        assert await memory.list_pop("q", 0) == []

    async def test_list_ttl_only_on_create(self, memory: Cache) -> None:
        await memory.list_push("q", "a", ttl=5)
        remaining = await memory.ttl("q")
        assert remaining is not None and 0 < remaining <= 5
        await memory.list_push("q", "b")  # 往已有的键上追加：不动 TTL
        refreshed = await memory.ttl("q")
        assert refreshed is not None and refreshed <= remaining  # 只减不增

    async def test_list_expires(self, memory: Cache) -> None:
        await memory.list_push("q", "a", ttl=0.05)
        await asyncio.sleep(0.1)
        assert await memory.exists("q") is False
        assert await memory.list_range("q") == []

    async def test_hash_set_get_delete(self, memory: Cache) -> None:
        assert await memory.hash_set("user:1", {"name": "阿一", "age": "20"}) == 2  # 新增字段数
        assert await memory.hash_set("user:1", {"name": "阿一改", "city": "上海"}) == 1
        assert await memory.hash_get("user:1", "name") == "阿一改"
        assert await memory.hash_get("user:1", "nope") is None
        assert await memory.hash_get("nope", "f") is None
        expected = {"name": "阿一改", "age": "20", "city": "上海"}
        assert await memory.hash_get_all("user:1") == expected
        assert await memory.hash_get_all("nope") == {}
        assert await memory.hash_exists("user:1", "name") is True
        assert await memory.hash_exists("user:1", "nope") is False
        assert await memory.hash_exists("nope", "f") is False  # 键不存在算 False
        assert await memory.hash_length("user:1") == 3
        assert await memory.hash_length("nope") == 0  # 键不存在算 0
        assert await memory.hash_keys("user:1") == ["name", "age", "city"]
        assert await memory.hash_keys("nope") == []
        assert await memory.hash_delete("user:1", "age", "nope") == 1
        assert await memory.hash_delete("user:1", "name", "city") == 2
        assert await memory.exists("user:1") is False  # 字段删空了键就没了

    async def test_type_reports_structure_kind(self, memory: Cache) -> None:
        """type()：字符串 / 列表 / 哈希三种结构各报各的，不存在的键报 None。"""
        assert await memory.type("nope") is None
        await memory.set("s", "v")
        await memory.list_push("q", "a")
        await memory.hash_set("h", {"f": "v"})
        assert await memory.type("s") == "string"
        assert await memory.type("q") == "list"
        assert await memory.type("h") == "hash"

    async def test_hash_get_all_returns_copy(self, memory: Cache) -> None:
        """拿回来的是一份拷贝：外面改了不该影响缓存里的。"""
        await memory.hash_set("h", {"f": "v"})
        snapshot = await memory.hash_get_all("h")
        snapshot["f"] = "改过了"
        assert await memory.hash_get("h", "f") == "v"

    async def test_hash_ttl_only_on_create(self, memory: Cache) -> None:
        await memory.hash_set("h", {"f": "v"}, ttl=5)
        remaining = await memory.ttl("h")
        assert remaining is not None and 0 < remaining <= 5
        await memory.hash_set("h", {"g": "v"})  # 追加字段：不动 TTL
        refreshed = await memory.ttl("h")
        assert refreshed is not None and refreshed <= remaining

    async def test_hash_expires(self, memory: Cache) -> None:
        await memory.hash_set("h", {"f": "v"}, ttl=0.05)
        await asyncio.sleep(0.1)
        assert await memory.exists("h") is False
        assert await memory.hash_get_all("h") == {}

    async def test_wrong_type_access_raises(self, memory: Cache) -> None:
        """一个键只能按写入时的那种结构访问 —— 对应 Redis 的 WRONGTYPE。"""
        await memory.set("s", "v")
        with pytest.raises(CacheError, match="不能按 list"):
            await memory.list_push("s", "a")
        with pytest.raises(CacheError, match="不能按 hash"):
            await memory.hash_get("s", "f")
        await memory.list_push("l", "a")
        with pytest.raises(CacheError, match="不能按 string"):
            await memory.get("l")
        with pytest.raises(CacheError, match="不能按 string"):
            await memory.incr("l")

    async def test_get_many_skips_non_string_keys(self, memory: Cache) -> None:
        """照 MGET 的脾气：别的类型当作没取到，不报错。"""
        await memory.set("s", "v")
        await memory.list_push("l", "a")
        assert await memory.get_many(["s", "l", "nope"]) == {"s": "v"}

    async def test_structures_reject_non_string_items(self, memory: Cache) -> None:
        """元素 / 字段值必须是字符串：误把整个列表当元素塞进来会被拦下。"""
        with pytest.raises(CacheError, match="必须是字符串"):
            await memory.list_push("q", ["a", "b"])  # pyright: ignore[reportArgumentType]
        with pytest.raises(CacheError, match="必须是字符串"):
            await memory.hash_set("h", {"f": 1})  # pyright: ignore[reportArgumentType]
        assert await memory.exists("q") is False  # 报错就别留下半截数据
        assert await memory.exists("h") is False


class TestFacade:
    """门面本身：生命周期、状态、没启动就调用。"""

    async def test_operations_before_start_raise(self) -> None:
        facade = Cache()
        with pytest.raises(CacheError):
            await facade.get("k")
        assert facade.running is False

    async def test_start_and_stop_are_idempotent(self) -> None:
        facade = Cache()
        await facade.start()
        await facade.start()  # 重复启动是空操作
        assert facade.running is True
        await facade.stop()
        await facade.stop()
        assert facade.running is False

    async def test_default_backend_is_memory(self, memory: Cache) -> None:
        """不启用 Redis 也能直接用 —— 这是默认姿势。"""
        assert memory.options.backend == "memory"
        assert memory.backend_name == "memory"
        assert memory.degraded is False
        assert await memory.ping() is True

    async def test_configure_rejected_after_start(self, memory: Cache) -> None:
        with pytest.raises(CacheError):
            memory.configure(CacheOptions(backend="redis"))

    async def test_json_roundtrip_keeps_nested_structure(self, memory: Cache) -> None:
        """嵌套结构（哈希的哈希）走 JSON：一个键装得下整份对象。"""
        profile = {"name": "阿一", "tags": ["a", "b"], "meta": {"level": 3, "beta": True}}
        await memory.set_json("profile", profile)
        assert await memory.get_json("profile") == profile
        assert await memory.get_json("nope") is None
        # 落到后端里的就是一段 JSON 文本：普通字符串接口照样能用
        text = await memory.get("profile")
        assert text is not None and json.loads(text) == profile

    async def test_get_json_rejects_plain_string(self, memory: Cache) -> None:
        await memory.set("k", "不是 JSON")
        with pytest.raises(CacheError, match="不是合法 JSON"):
            await memory.get_json("k")

    async def test_default_ttl_applies_to_structures(self) -> None:
        """default_ttl 对列表 / 哈希的新建键同样生效。"""
        facade = Cache(CacheOptions(default_ttl=5))
        await facade.start()
        try:
            await facade.list_push("q", "a")
            await facade.hash_set("h", {"f": "v"})
            for key in ("q", "h"):
                remaining = await facade.ttl(key)
                assert remaining is not None and 0 < remaining <= 5
        finally:
            await facade.stop()


class TestRedisBackend:
    """Redis 后端：可选依赖、连不上时的两种处理，以及本机有服务时的真连。"""

    def test_backends_satisfy_protocol(self) -> None:
        assert isinstance(MemoryCache(), CacheBackend)
        assert isinstance(RedisCache(RedisOptions()), CacheBackend)

    async def test_start_passes_connect_timeout_and_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """建连超时与重试次数要落到驱动上：连不上时不干等系统默认 + 驱动默认的 10 次重试。"""
        pytest.importorskip("redis.asyncio")
        captured: dict[str, object] = {}

        class FakeRedis:  # 替掉驱动里的类：只记录构造参数，ping 当作连上了
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            async def ping(self) -> None: ...

            async def aclose(self) -> None: ...

        import redis.asyncio

        monkeypatch.setattr(redis.asyncio, "Redis", FakeRedis)
        backend = RedisCache(RedisOptions(socket_connect_timeout=3.0, connect_retries=0))
        await backend.start()
        await backend.stop()
        assert captured["socket_connect_timeout"] == 3.0
        assert captured["retry"] is not None  # 显式传了重试策略，不是让驱动用默认 10 次

    async def test_unreachable_redis_raises_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认（不写 fallback_to_memory）连不上也当场报错，报错里提示去配置里改。"""
        stub_redis_refused(monkeypatch)
        facade = Cache(CacheOptions(backend="redis"))
        with pytest.raises(CacheError, match="连不上.*config.toml"):
            await facade.start()
        assert facade.running is False  # 起不来就别留个半死的门面

    async def test_unreachable_redis_raises_without_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式关掉 fallback：start() 当场失败，不留半死门面。"""
        stub_redis_refused(monkeypatch)
        options = CacheOptions(backend="redis", fallback_to_memory=False)
        facade = Cache(options)
        with pytest.raises(CacheError, match="连不上.*config.toml"):
            await facade.start()
        assert facade.running is False

    async def test_unreachable_redis_degrades_only_when_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式打开 fallback_to_memory 才退回本地缓存：上层照旧读写，只是能问出 degraded。"""
        stub_redis_refused(monkeypatch)
        facade = Cache(CacheOptions(backend="redis", fallback_to_memory=True))
        await facade.start()
        try:
            assert facade.degraded is True
            assert facade.backend_name == "memory"
            await facade.set("k", "v")
            assert await facade.get("k") == "v"
        finally:
            await facade.stop()

    async def test_real_redis_roundtrip_if_available(self) -> None:
        """本机有 Redis 就顺带把真后端跑一遍；没有就跳过（不强制装服务）。

        先探一次 TCP 再决定跑不跑：让门面直接去连、失败了再 skip 的话，「本机没 Redis」
        这一常见情况会因为驱动的重试白等二十几秒（见 :func:`stub_redis_refused`）。
        """
        pytest.importorskip("redis")
        options = RedisOptions(socket_timeout=1.0)
        if not redis_reachable(options.host, options.port):  # 探的就是待会儿真连的地址
            pytest.skip("本机没有可用的 Redis，跳过真连用例")
        facade = Cache(
            CacheOptions(
                backend="redis",
                namespace="tickneko-test",
                redis=options,
                fallback_to_memory=True,  # 探得到却连不上时退回内存，便于下方 skip
            )
        )
        await facade.start()
        if facade.degraded:  # 探得到却连不上：多半是本机 Redis 要认证，这种也跳过
            await facade.stop()
            pytest.skip("本机 6379 在听但连不上，跳过真连用例")
        try:
            await facade.clear()
            await facade.set("k", "v", ttl=30)
            assert await facade.get("k") == "v"  # 读回来是 str，不用上层解码
            assert await facade.keys() == ["k"]  # 列出来的是逻辑键，不带 namespace 前缀
            remaining = await facade.ttl("k")
            assert remaining is not None and 0 < remaining <= 30
            await facade.set_many({"a": "1", "b": "2"})
            assert await facade.get_many(["a", "b"]) == {"a": "1", "b": "2"}
            assert await facade.delete_many(["a", "b"]) == 2
            assert await facade.incr("n") == 1
            assert await facade.delete("k") is True
            # 列表：一次性推、区间取、按弹出顺序弹
            assert await facade.list_push("q", "a", "b", ttl=30) == 2
            assert await facade.list_push("q", "c") == 3
            assert await facade.list_range("q") == ["a", "b", "c"]
            assert await facade.list_range("q", -2, -1) == ["b", "c"]
            assert await facade.list_pop("q", 2) == ["c", "b"]
            assert await facade.list_length("q") == 1
            remaining = await facade.ttl("q")
            assert remaining is not None and 0 < remaining <= 30  # 新建时设上的 TTL 还在
            # 哈希：写多个、按字段取、删字段
            assert await facade.hash_set("h", {"name": "阿一", "age": "20"}, ttl=30) == 2
            assert await facade.hash_get("h", "name") == "阿一"
            assert await facade.hash_get_all("h") == {"name": "阿一", "age": "20"}
            assert await facade.hash_delete("h", "age", "nope") == 1
            # JSON 与类型不匹配（Redis 那边报 WRONGTYPE，翻成同一个异常）
            await facade.set_json("j", {"tags": ["a", "b"]})
            assert await facade.get_json("j") == {"tags": ["a", "b"]}
            assert await facade.type("n") == "string"  # 自增建的键
            assert await facade.type("q") == "list"
            assert await facade.type("h") == "hash"
            assert await facade.type("nope") is None  # 不存在的键报 None
            with pytest.raises(CacheError):
                await facade.incr("q")  # 列表键不能按字符串用
        finally:
            await facade.clear()
            await facade.stop()


class TestOptionsFromMapping:
    """配置区域 -> 缓存选项的转换（app.py 就是这么接的）。"""

    def test_defaults_without_config_section(self) -> None:
        """配置里没有 [cache] 这一节时，转出来的选项就是本地内存版。"""
        options = CacheOptions.from_mapping(Settings().cache.model_dump())
        assert options.backend == "memory"
        assert options.namespace == "tickneko"
        assert options.default_ttl == 0.0
        assert options.redis.port == 6379

    def test_reads_region_and_nested_redis(self, tmp_path: Path) -> None:
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [cache]
                    backend = "redis"
                    namespace = "app"
                    default_ttl = 30.0
                    fallback_to_memory = false

                    [cache.redis]
                    host = "10.0.0.9"
                    port = 6390
                    db = 3
                    max_connections = 20
                    """
                ),
            )
        )
        options = CacheOptions.from_mapping(settings.cache.model_dump())
        assert (options.backend, options.namespace) == ("redis", "app")
        assert (options.default_ttl, options.fallback_to_memory) == (30.0, False)
        assert (options.redis.host, options.redis.port, options.redis.db) == ("10.0.0.9", 6390, 3)
        assert options.redis.max_connections == 20

    def test_unknown_keys_are_ignored(self) -> None:
        """多写的项不该让缓存层炸掉：校验那一项归配置系统管。"""
        options = CacheOptions.from_mapping({"backend": "memory", "nonsense": 1, "redis": {"x": 1}})
        assert options.backend == "memory"
        assert options.redis.port == 6379
