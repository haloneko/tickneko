"""缓存门面：上层只认这一套 API，Redis 与本地内存的差异在这里被抹平。

用哪个后端由 :class:`~tickneko.core.cache.models.CacheOptions` 的 ``backend`` 决定，两个
后端都符合 :class:`~tickneko.core.cache.interfaces.CacheBackend`，所以本类的方法就是无脑
转发，唯一多做的一件事是把「``ttl=None`` 该用哪个 TTL」按配置定下来。配了 Redis 而它
连不上时默认**当场报错**，只有显式打开 ``fallback_to_memory`` 才退回内存（要上报就问
:attr:`Cache.degraded`）。后端差异与降级详见 ``docs/cache/cache.md``。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from tickneko.core.cache.interfaces import CacheBackend
from tickneko.core.cache.logging import cache_logger
from tickneko.core.cache.memory import MemoryCache
from tickneko.core.cache.models import CacheError, CacheOptions
from tickneko.core.cache.redis import RedisCache
from tickneko.core.logger import BaseLogger


class Cache:
    """缓存门面；进程级实例见 :data:`tickneko.core.cache.manager.cache`。

    生命周期与调度器一致：:meth:`start` / :meth:`stop` 都是显式的、重复调用是空操作。
    **不隐式启动**：没 ``start()`` 就调用数据接口会抛
    :class:`~tickneko.core.cache.models.CacheError` —— 连不连得上应该在启动阶段就见分晓，
    而不是等到哪一次 ``get`` 才炸。

    日志实例由装配层传入（``logger=``，也可以事后 :meth:`attach_logger`）；没传就用
    本模块的便捷函数（:func:`tickneko.core.cache.logging.cache_logger`），业务代码不直接
    ``default_core()``。
    """

    def __init__(
        self, options: CacheOptions | None = None, *, logger: BaseLogger | None = None
    ) -> None:
        self._options: CacheOptions = options or CacheOptions()
        self._backend: CacheBackend | None = None
        self._degraded: bool = False  # 配了 Redis 但退回了内存
        self._logger: BaseLogger | None = logger

    # ---- 日志注入 ----
    def attach_logger(self, logger: BaseLogger) -> None:
        """注入日志实例（装配层建完核心后调用；单例场景用，构造参数传了就不用再调）。"""
        self._logger = logger

    def _log(self) -> BaseLogger:
        """业务日志实例：装配注入的优先，没传就取 ``cache`` 便捷函数（默认核心）。"""
        return self._logger if self._logger is not None else cache_logger()

    # ---- 生命周期 ----
    def configure(self, options: CacheOptions) -> None:
        """换一份选项（配置由 ``app.py`` 从 ``[cache]`` 区域转进来）。

        :raises CacheError: 已经启动过了 —— 配置冻结在 :meth:`start` 那一刻，
            要改就先 :meth:`stop`。
        """
        if self._backend is not None:
            raise CacheError("缓存已经启动，改配置要先 await cache.stop()")
        self._options = options

    async def start(self) -> None:
        """按选项建后端并连上；重复调用是空操作。

        ``backend="redis"`` 而 Redis 连不上时：默认**直接抛** :class:`CacheError`（报错信息里
        带「检查配置」的提示）让启动阶段失败；只有显式打开 ``fallback_to_memory`` 才退回内存
        并记一条 warning（上层无感）。降级只在这一刻判断一次，之后不自动重连 —— 要重新试就
        ``stop()`` 再 ``start()``。
        """
        if self._backend is not None:
            return
        options = self._options
        backend: CacheBackend
        if options.backend != "redis":
            backend = MemoryCache(sweep_interval=options.sweep_interval, logger=self._log())
        else:
            redis_backend = RedisCache(options.redis, namespace=options.namespace)
            try:
                await redis_backend.start()
            except CacheError as exc:
                if not options.fallback_to_memory:
                    raise CacheError(
                        f"{exc}（请检查 config.toml 的 [cache.redis] 配置；"
                        "若确实想退回本地缓存，可在 [cache] 里设 fallback_to_memory = true）"
                    ) from exc
                self._degraded = True
                self._log().warning("Redis 起不来，退回本地缓存", error=str(exc) or repr(exc))
                backend = MemoryCache(sweep_interval=options.sweep_interval, logger=self._log())
            else:
                backend = redis_backend
        await backend.start()  # redis 分支已经起过，这里是空操作
        self._backend = backend
        self._log().info(f"缓存就绪：{self.backend_name}", namespace=options.namespace)

    async def stop(self, timeout: float = 5.0) -> None:
        """停后端（内存后端停清扫任务、Redis 后端断连接池）；重复调用是空操作。

        数据不动：内存后端不跨进程、本来就没有持久化可言，Redis 那边由服务端自己管。
        """
        backend, self._backend = self._backend, None
        if backend is None:
            return
        await backend.stop(timeout)

    # ---- 状态 ----
    @property
    def running(self) -> bool:
        """是否已启动。"""
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        """实际在用的后端：``"redis"`` / ``"memory"``；没启动时返回选项里配的那个。"""
        if isinstance(self._backend, RedisCache):
            return "redis"
        if isinstance(self._backend, MemoryCache):
            return "memory"
        return self._options.backend

    @property
    def degraded(self) -> bool:
        """是否发生过降级（配了 Redis 但退回了内存）—— 需要上报就报这个。"""
        return self._degraded

    @property
    def options(self) -> CacheOptions:
        """当前这份选项。"""
        return self._options

    async def ping(self) -> bool:
        """后端是否可用，不抛异常（内存后端恒为真）。"""
        return await self._require().ping()

    # ---- 数据操作（转发给当前后端）----
    async def get(self, key: str) -> str | None:
        """取值；键不存在或已过期返回 ``None``。"""
        return await self._require().get(key)

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        """写值；``ttl`` 留空则用配置里的 ``default_ttl``（为 0 表示永不过期）。"""
        await self._require().set(key, value, self._resolve_ttl(ttl))

    async def delete(self, key: str) -> bool:
        """删一个键，返回是否删掉了。"""
        return await self._require().delete(key)

    async def exists(self, key: str) -> bool:
        """键是否存在（已过期的算不存在）。"""
        return await self._require().exists(key)

    async def expire(self, key: str, ttl: float) -> bool:
        """给已有的键设过期时间，返回是否设上了（键不存在则 ``False``）。"""
        return await self._require().expire(key, ttl)

    async def ttl(self, key: str) -> float | None:
        """剩余存活秒数；``None`` = 键不存在，``math.inf`` = 永不过期。"""
        return await self._require().ttl(key)

    async def incr(self, key: str, amount: int = 1) -> int:
        """原子自增（键不存在时从 0 起算），返回自增后的值。"""
        return await self._require().incr(key, amount)

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        """批量取值，只返回拿到的那些键。"""
        return await self._require().get_many(keys)

    async def set_many(self, items: Mapping[str, str], ttl: float | None = None) -> None:
        """批量写值，同一个 TTL 作用于这一批。"""
        await self._require().set_many(items, self._resolve_ttl(ttl))

    async def delete_many(self, keys: Sequence[str]) -> int:
        """批量删除，返回删掉的键数。"""
        return await self._require().delete_many(keys)

    async def keys(self, pattern: str = "*") -> list[str]:
        """按通配符列键（``*`` / ``?`` / ``[abc]``），默认全部。"""
        return await self._require().keys(pattern)

    async def clear(self) -> int:
        """清掉本缓存的所有键（Redis 后端只清自己命名空间下的），返回清掉的键数。"""
        return await self._require().clear()

    # ---- 列表（Redis 的 list）----
    async def list_push(self, key: str, *values: str, ttl: float | None = None) -> int:
        """从右侧推入元素，返回推入后的长度；``ttl`` 只在键不存在时用。"""
        return await self._require().list_push(key, *values, ttl=self._resolve_ttl(ttl))

    async def list_push_left(self, key: str, *values: str, ttl: float | None = None) -> int:
        """从左侧推入元素（队列的另一端），其余同 :meth:`list_push`。"""
        return await self._require().list_push_left(key, *values, ttl=self._resolve_ttl(ttl))

    async def list_range(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        """取下标区间内的元素（两端都含，负数从右数）；键不存在返回空列表。"""
        return await self._require().list_range(key, start, stop)

    async def list_length(self, key: str) -> int:
        """列表长度（键不存在算 0）。"""
        return await self._require().list_length(key)

    async def list_pop(self, key: str, count: int = 1) -> list[str]:
        """从右侧弹出至多 ``count`` 个元素，按弹出顺序返回；弹空之后键就没了。"""
        return await self._require().list_pop(key, count)

    # ---- 哈希（Redis 的 hash）----
    async def hash_set(self, key: str, items: Mapping[str, str], ttl: float | None = None) -> int:
        """写字段（一次可写多个），返回新增的字段数；``ttl`` 只在键不存在时用。"""
        return await self._require().hash_set(key, items, self._resolve_ttl(ttl))

    async def hash_get(self, key: str, field: str) -> str | None:
        """取一个字段；键或字段不存在返回 ``None``。"""
        return await self._require().hash_get(key, field)

    async def hash_get_all(self, key: str) -> dict[str, str]:
        """取回整个哈希（键不存在返回空字典）。"""
        return await self._require().hash_get_all(key)

    async def hash_delete(self, key: str, *fields: str) -> int:
        """删若干个字段，返回真删掉的个数；字段被删空的键就没了。"""
        return await self._require().hash_delete(key, *fields)

    async def hash_exists(self, key: str, field: str) -> bool:
        """字段在不在（键或字段不存在返回 ``False``）。"""
        return await self._require().hash_exists(key, field)

    async def hash_length(self, key: str) -> int:
        """哈希的字段数（键不存在算 0）。"""
        return await self._require().hash_length(key)

    async def hash_keys(self, key: str) -> list[str]:
        """列字段名（键不存在返回空列表）。"""
        return await self._require().hash_keys(key)

    # ---- JSON（序列化在门面做，后端里存的还是一段字符串）----
    async def set_json(self, key: str, value: object, ttl: float | None = None) -> None:
        """把任意可 JSON 序列化的值写进去；``ttl`` 规则同 :meth:`set`。

        落到后端里的就是一段 JSON 文本，所以它本质是个普通字符串键：``get`` / ``ttl`` /
        ``delete`` 照样能用。嵌套结构（哈希的哈希、对象数组）都走这条路 —— Redis 的哈希
        字段同样只放得下字符串，嵌不进去。要写 ``datetime`` 之类非 JSON 类型，自己先转成
        字符串（本层只认标准 JSON）。
        """
        text = json.dumps(value, ensure_ascii=False)
        await self._require().set(key, text, self._resolve_ttl(ttl))

    async def get_json(self, key: str) -> object | None:
        """读回 :meth:`set_json` 写的值；键不存在返回 ``None``。

        :raises CacheError: 键存在但内容不是合法 JSON（多半是被当普通字符串写过）。
        """
        text = await self._require().get(key)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CacheError(f"键 {key} 的内容不是合法 JSON：{exc}") from exc

    # ---- 内部 ----
    def _require(self) -> CacheBackend:
        """取当前后端；没启动就报错（不隐式启动）。"""
        if self._backend is None:
            raise CacheError("缓存还没启动：先 await cache.start()")
        return self._backend

    def _resolve_ttl(self, ttl: float | None) -> float | None:
        """定下这次写入的 TTL：``None`` 用配置默认值，小于等于 0 都当「永不过期」。"""
        resolved = self._options.default_ttl if ttl is None else ttl
        return resolved if resolved > 0 else None
