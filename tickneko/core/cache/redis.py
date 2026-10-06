"""Redis 后端：异步 IO 执行命令，键统一加命名空间前缀。

驱动是 redis-py 的异步客户端（可选依赖，``pip install "tickneko[redis]"``）：一条命令
一个协程，不阻塞事件循环。适配器多做两件事：所有键拼上 ``<namespace>:`` 前缀（上层
看到的键始终不含前缀）；驱动的 ``RedisError`` 一律翻成
:class:`~tickneko.core.cache.models.CacheError`。另外 ``decode_responses=True`` 让读回来
的是 ``str`` 而非 ``bytes``，``keys()`` 走 ``SCAN`` 而不是 ``KEYS``。详见
``docs/cache/cache.md``。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from tickneko.core.cache.models import CacheError, RedisOptions

if TYPE_CHECKING:  # 驱动是可选依赖，只在类型检查时导入
    from redis.asyncio import Redis


class RedisCache:
    """redis-py 异步客户端封装（符合 :class:`~tickneko.core.cache.interfaces.CacheBackend`）。"""

    def __init__(self, options: RedisOptions, *, namespace: str = "tickneko") -> None:
        self._options: RedisOptions = options
        self._prefix: str = f"{namespace}:" if namespace else ""
        self._client: Redis | None = None
        self._errors: type[Exception] = Exception  # 驱动异常基类，start() 里换成 RedisError

    # ---- 生命周期 ----
    async def start(self) -> None:
        """连上 Redis（顺手 ping 一次探活）；重复调用是空操作。

        :raises CacheError: 没装 redis 包，或连不上（服务没起、密码不对、地址写错）。
        """
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis
            from redis.backoff import ExponentialBackoff
            from redis.exceptions import RedisError
            from redis.retry import Retry
        except ImportError as exc:
            hint = "RedisCache 需要 redis 包：pip install redis（或 pip install 'tickneko[redis]'）"
            raise CacheError(hint) from exc

        options = self._options
        client = Redis(
            host=options.host,
            port=options.port,
            db=options.db,
            username=options.username or None,  # Redis 6+ ACL；空 = 默认用户
            password=options.password or None,  # 空 = 不认证
            socket_timeout=options.socket_timeout,
            # 驱动默认对连接错误做 10 次指数退避重试、建连超时走系统默认（实测要干等
            # 二十几秒才报错）；这里显式给短建连超时 + 少重试，Redis 起不来时尽快失败
            socket_connect_timeout=options.socket_connect_timeout,
            retry=Retry(ExponentialBackoff(), retries=options.connect_retries),
            max_connections=options.max_connections,
            decode_responses=True,  # 读回来就是 str，和内存后端一致
        )
        self._errors = RedisError
        try:
            await client.ping()
        except RedisError as exc:
            await client.aclose()  # 连不上就别留着连接池
            raise CacheError(
                f"Redis 连不上 {options.host}:{options.port}/{options.db}：{exc}"
            ) from exc
        self._client = client

    async def stop(self, timeout: float = 5.0) -> None:
        """断开连接池；重复调用是空操作。``timeout`` 只为与其它后端对齐签名。"""
        client, self._client = self._client, None
        if client is None:
            return
        await client.aclose()

    async def ping(self) -> bool:
        try:
            await self._call("ping")
        except CacheError:
            return False
        return True

    # ---- 数据操作 ----
    async def get(self, key: str) -> str | None:
        value = await self._call("get", self._full(key))
        return None if value is None else str(value)

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        await self._call("set", self._full(key), value, ex=self._ex(ttl))

    async def delete(self, key: str) -> bool:
        return int(await self._call("delete", self._full(key))) > 0

    async def exists(self, key: str) -> bool:
        return int(await self._call("exists", self._full(key))) > 0

    async def expire(self, key: str, ttl: float) -> bool:
        return bool(await self._call("expire", self._full(key), timedelta(seconds=ttl)))

    async def ttl(self, key: str) -> float | None:
        seconds = float(await self._call("ttl", self._full(key)))
        if seconds < 0:  # 协议里 -2 = 键不存在、-1 = 没设过期
            return None if seconds == -2 else float("inf")
        return seconds

    async def type(self, key: str) -> str | None:
        """键的结构类型（``"string"`` / ``"list"`` / ``"hash"``）；不存在返回 ``None``。"""
        raw = await self._call("type", self._full(key))
        return None if raw == "none" else str(raw)

    async def incr(self, key: str, amount: int = 1) -> int:
        return int(await self._call("incrby", self._full(key), amount))

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        if not keys:
            return {}
        values: list[object] = await self._call("mget", [self._full(key) for key in keys])
        return {
            key: str(value)
            for key, value in zip(keys, values, strict=True)
            if value is not None
        }

    async def set_many(self, items: Mapping[str, str], ttl: float | None = None) -> None:
        if not items:
            return
        client = self._require_client()
        ex = self._ex(ttl)
        # 走 pipeline：一趟网络往返发完整批，不在每条命令上各等一次 RTT
        pipe = client.pipeline(transaction=False)
        for key, value in items.items():
            pipe.set(self._full(key), value, ex=ex)
        try:
            await pipe.execute()
        except self._errors as exc:
            raise CacheError(f"Redis 批量写入失败：{exc}") from exc

    async def delete_many(self, keys: Sequence[str]) -> int:
        if not keys:
            return 0
        return int(await self._call("delete", *[self._full(key) for key in keys]))

    async def keys(self, pattern: str = "*") -> list[str]:
        client = self._require_client()
        try:
            # SCAN 游标遍历：KEYS 在大库上会阻塞整个 Redis（返回的键带前缀，去掉再还）
            return [self._strip(key) async for key in client.scan_iter(match=self._full(pattern))]
        except self._errors as exc:
            raise CacheError(f"Redis 扫描键失败：{exc}") from exc

    async def clear(self) -> int:
        """只清本命名空间下的键（共享实例时不动别人的数据）。"""
        found = await self.keys()
        return await self.delete_many(found) if found else 0

    # ---- 列表 ----
    async def list_push(self, key: str, *values: str, ttl: float | None = None) -> int:
        return await self._push(key, values, left=False, ttl=ttl)

    async def list_push_left(self, key: str, *values: str, ttl: float | None = None) -> int:
        return await self._push(key, values, left=True, ttl=ttl)

    async def list_range(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        items = await self._call("lrange", self._full(key), start, stop)
        return [str(item) for item in items]

    async def list_length(self, key: str) -> int:
        return int(await self._call("llen", self._full(key)))

    async def list_pop(self, key: str, count: int = 1) -> list[str]:
        if count <= 0:
            return []
        # 带 count 的 RPOP 要 Redis 6.2+；更早的服务端会由驱动报错，翻成 CacheError 抛出去
        popped = await self._call("rpop", self._full(key), count)
        return [] if popped is None else [str(item) for item in popped]

    async def _push(self, key: str, values: Sequence[str], *, left: bool, ttl: float | None) -> int:
        """推元素（``RPUSH`` / ``LPUSH``）；``ttl`` 只在键不存在时用。

        空推入不发给 Redis（``RPUSH`` 至少要一个元素）：当「问长度」处理，与内存后端一致。
        """
        if not values:
            return await self.list_length(key)
        full = self._full(key)
        client = self._require_client()
        # 一趟往返问两件事：键在不在（决定要不要设 TTL）、推完多长
        pipe = client.pipeline(transaction=False)
        pipe.exists(full)
        if left:
            pipe.lpush(full, *values)
        else:
            pipe.rpush(full, *values)
        try:
            existed, length = await pipe.execute()
        except self._errors as exc:
            raise CacheError(f"Redis 推入列表失败：{exc}") from exc
        if not existed and ttl is not None:  # 已有键的 TTL 不动（与 Redis 一致）
            await self._call("expire", full, timedelta(seconds=ttl))
        return int(length)

    # ---- 哈希 ----
    async def hash_set(self, key: str, items: Mapping[str, str], ttl: float | None = None) -> int:
        if not items:  # HSET 不给字段会报错：空映射当「没新增」
            return 0
        full = self._full(key)
        client = self._require_client()
        pipe = client.pipeline(transaction=False)
        pipe.exists(full)
        # 驱动 stub 把 mapping 声明成了 TypeVar 组合，字段串类型推断不出来，压掉这条告警
        pipe.hset(full, mapping=dict(items))  # pyright: ignore[reportArgumentType]
        try:
            existed, added = await pipe.execute()
        except self._errors as exc:
            raise CacheError(f"Redis 写哈希失败：{exc}") from exc
        if not existed and ttl is not None:  # 已有键的 TTL 不动（与 Redis 一致）
            await self._call("expire", full, timedelta(seconds=ttl))
        return int(added)

    async def hash_get(self, key: str, field: str) -> str | None:
        value = await self._call("hget", self._full(key), field)
        return None if value is None else str(value)

    async def hash_get_all(self, key: str) -> dict[str, str]:
        raw = await self._call("hgetall", self._full(key))
        return {str(field): str(value) for field, value in raw.items()}

    async def hash_delete(self, key: str, *fields: str) -> int:
        if not fields:
            return 0
        return int(await self._call("hdel", self._full(key), *fields))

    async def hash_exists(self, key: str, field: str) -> bool:
        return bool(await self._call("hexists", self._full(key), field))

    async def hash_length(self, key: str) -> int:
        return int(await self._call("hlen", self._full(key)))

    async def hash_keys(self, key: str) -> list[str]:
        raw = await self._call("hkeys", self._full(key))
        return [str(field) for field in raw]

    # ---- 内部 ----
    @staticmethod
    def _ex(ttl: float | None) -> timedelta | None:
        """TTL 秒数 -> redis-py 要的 ``ex`` 参数（``None`` = 不设过期）。

        redis-py 收 ``timedelta`` 时会按毫秒精度下发，所以小数秒不会被截断。
        """
        return None if ttl is None else timedelta(seconds=ttl)

    def _full(self, key: str) -> str:
        """逻辑键 -> Redis 上的真键（拼命名空间前缀）。"""
        return f"{self._prefix}{key}"

    def _strip(self, key: str) -> str:
        """Redis 上的真键 -> 逻辑键（去掉前缀再交给上层）。"""
        return key[len(self._prefix) :] if self._prefix else key

    def _require_client(self) -> Redis:
        if self._client is None:
            raise CacheError("Redis 缓存还没启动：先 await cache.start()")
        return self._client

    async def _call(self, method: str, *args: object, **kwargs: object) -> Any:
        """把一条命令丢给驱动；驱动层的异常统一翻成 :class:`CacheError`。"""
        client = self._require_client()
        try:
            return await getattr(client, method)(*args, **kwargs)
        except self._errors as exc:
            raise CacheError(f"Redis 命令 {method} 失败：{exc}") from exc
