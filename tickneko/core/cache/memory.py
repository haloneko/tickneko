"""本地内存缓存：不启用 Redis 时的落点，也是 Redis 连不上时的降级兜底。

数据只活在当前进程里：换进程就没了，也不跨机器共享；换来的是零依赖、零网络。语义与
Redis 后端完全对齐（见 :mod:`tickneko.core.cache.interfaces`）—— 唯一真正不同的是「没有
别人能看到你写的东西」。过期键由后台任务按 ``sweep_interval`` 扫掉。详见
``docs/cache/cache.md``。
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from typing import NamedTuple, cast

from tickneko.core.cache.logging import cache_logger
from tickneko.core.cache.models import CacheError
from tickneko.core.logger import BaseLogger


class _Entry(NamedTuple):
    """一条记录：结构类型 + 值 + 过期时刻（``None`` = 永不过期）。"""

    kind: str  # "string" / "list" / "hash"
    value: object  # str / list[str] / dict[str, str]
    deadline: float | None


def _slice(items: list[str], start: int, stop: int) -> list[str]:
    """按 Redis 的 ``LRANGE`` 规则切片：两端都含、负数从右数、越界自动裁剪。

    与 Python 切片的差别就在右端含不含：Redis 的 ``stop`` 是**含**的（``0 -1`` 表示整条
    列表），而 Python 的 ``-1`` 意思是「到末尾前一个」，所以这里先转成左闭右开再切。
    """
    length = len(items)
    begin = start + length if start < 0 else start
    end = stop + length if stop < 0 else stop
    begin = max(0, min(begin, length))
    end = max(0, min(end + 1, length))  # Redis 的 stop 含在结果里，所以右端 +1
    return items[begin:end] if begin < end else []


class MemoryCache:
    """进程内存里的键值缓存（带 TTL）。

    存储就是一个 dict：``key -> _Entry``（值 + 结构类型 + 过期时刻），过期时刻为 ``None``
    表示永不过期。读写都在事件循环线程里一口气做完（方法体内没有 ``await``），所以天然
    是原子的，不需要锁。

    过期键两条路清理：读到 / 查到的时候顺手删（惰性），另有后台任务定期扫一遍
    （没人再访问的过期键只能靠它，否则会一直占着内存）。
    """

    def __init__(
        self, *, sweep_interval: float = 30.0, logger: BaseLogger | None = None
    ) -> None:
        self._data: dict[str, _Entry] = {}
        self._sweep_interval: float = sweep_interval
        self._sweeper: asyncio.Task[None] | None = None
        self._logger: BaseLogger | None = logger

    def _log(self) -> BaseLogger:
        """业务日志实例：装配注入的优先，没传就取 ``cache`` 便捷函数（装配槽位）。"""
        return self._logger if self._logger is not None else cache_logger()

    # ---- 生命周期 ----
    async def start(self) -> None:
        """起后台清扫任务；重复调用是空操作。"""
        if self._sweeper is not None and not self._sweeper.done():
            return
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop(self, timeout: float = 5.0) -> None:
        """停后台清扫任务；数据留着（进程内存，本来也不跨进程）。"""
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is None:
            return
        sweeper.cancel()
        # asyncio.wait 不会把子任务被取消当异常抛出来；外层若要取消本协程，
        # 这行 await 自己会抛 CancelledError，正好往上传播（停机信号不能被吞）
        _, pending = await asyncio.wait({sweeper}, timeout=timeout)
        if pending:  # 取消是瞬时的，走到这里说明另有情况，记一笔但不等了
            self._log().warning("内存缓存的清扫任务没在超时内停下")

    async def ping(self) -> bool:
        """本地缓存永远在线。"""
        return True

    # ---- 字符串 ----
    async def get(self, key: str) -> str | None:
        return self._string(key)

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        self._data[key] = _Entry("string", value, self._deadline(ttl))

    async def incr(self, key: str, amount: int = 1) -> int:
        entry = self._typed(key, "string")
        current = "0" if entry is None else cast(str, entry.value)
        try:
            value = int(current) + amount
        except ValueError as exc:  # 值和 Redis 一样要求是整数字符串
            raise CacheError(f"键 {key} 的值不是整数，不能自增：{current!r}") from exc
        deadline = None if entry is None else entry.deadline  # 自增不动 TTL（与 Redis 一致）
        self._data[key] = _Entry("string", str(value), deadline)
        return value

    # ---- 列表 ----
    async def list_push(self, key: str, *values: str, ttl: float | None = None) -> int:
        return self._push(key, values, left=False, ttl=ttl)

    async def list_push_left(self, key: str, *values: str, ttl: float | None = None) -> int:
        return self._push(key, values, left=True, ttl=ttl)

    async def list_range(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        items = self._list(key)
        return [] if items is None else _slice(items, start, stop)

    async def list_length(self, key: str) -> int:
        items = self._list(key)
        return 0 if items is None else len(items)

    async def list_pop(self, key: str, count: int = 1) -> list[str]:
        entry = self._typed(key, "list")
        if entry is None or count <= 0:
            return []
        items = cast(list[str], entry.value)
        take = min(count, len(items))
        popped = items[len(items) - take :]  # 从右侧摘下来
        del items[len(items) - take :]
        if items:
            self._data[key] = _Entry("list", items, entry.deadline)
        else:  # 弹空了键就没了（与 Redis 一致）
            del self._data[key]
        return popped[::-1]  # 按弹出顺序给出：最右侧的先出来

    def _push(self, key: str, values: Sequence[str], *, left: bool, ttl: float | None) -> int:
        self._check_strings(values, "列表元素")
        entry = self._typed(key, "list")
        if not values:  # 空推入当「问长度」：不建键（空列表不占键）
            return 0 if entry is None else len(cast(list[str], entry.value))
        if entry is None:  # 新建的键才用得上 ttl
            items: list[str] = []
            deadline = self._deadline(ttl)
        else:
            items = cast(list[str], entry.value)
            deadline = entry.deadline  # 往已有的键上追加，不动 TTL
        if left:
            items[0:0] = values
        else:
            items.extend(values)
        self._data[key] = _Entry("list", items, deadline)
        return len(items)

    # ---- 哈希 ----
    async def hash_set(self, key: str, items: Mapping[str, str], ttl: float | None = None) -> int:
        for field, value in items.items():
            self._check_strings((field, value), "哈希的字段与值")
        entry = self._typed(key, "hash")
        if entry is None:  # 新建的键才用得上 ttl
            fields: dict[str, str] = {}
            deadline = self._deadline(ttl)
        else:
            fields = cast(dict[str, str], entry.value)
            deadline = entry.deadline  # 往已有的键上追加字段，不动 TTL
        added = sum(1 for field in items if field not in fields)
        fields.update(items)
        self._data[key] = _Entry("hash", fields, deadline)
        return added

    async def hash_get(self, key: str, field: str) -> str | None:
        fields = self._hash(key)
        return None if fields is None else fields.get(field)

    async def hash_get_all(self, key: str) -> dict[str, str]:
        fields = self._hash(key)
        return {} if fields is None else dict(fields)  # 给一份拷贝，外部改了不影响缓存

    async def hash_delete(self, key: str, *fields: str) -> int:
        self._check_strings(fields, "要删的字段")
        entry = self._typed(key, "hash")
        if entry is None:
            return 0
        stored = cast(dict[str, str], entry.value)
        removed = sum(1 for field in fields if stored.pop(field, None) is not None)
        if stored:
            self._data[key] = _Entry("hash", stored, entry.deadline)
        else:  # 字段删空了键就没了（与 Redis 一致）
            del self._data[key]
        return removed

    async def hash_exists(self, key: str, field: str) -> bool:
        fields = self._hash(key)
        return fields is not None and field in fields

    async def hash_length(self, key: str) -> int:
        fields = self._hash(key)
        return 0 if fields is None else len(fields)

    async def hash_keys(self, key: str) -> list[str]:
        fields = self._hash(key)
        return [] if fields is None else list(fields.keys())

    # ---- 通用：删除 / 过期 / 批量 / 列键 ----
    async def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    async def exists(self, key: str) -> bool:
        return self._find(key) is not None

    async def expire(self, key: str, ttl: float) -> bool:
        entry = self._find(key)
        if entry is None:
            return False
        self._data[key] = _Entry(entry.kind, entry.value, self._deadline(ttl))
        return True

    async def ttl(self, key: str) -> float | None:
        entry = self._find(key)
        if entry is None:
            return None
        if entry.deadline is None:
            return math.inf
        return max(0.0, entry.deadline - time.monotonic())

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        # 照 Redis 的 MGET：非字符串类型的键当作没取到，不报 WRONGTYPE
        found: dict[str, str] = {}
        for key in keys:
            entry = self._find(key)
            if entry is not None and entry.kind == "string":
                found[key] = cast(str, entry.value)
        return found

    async def set_many(self, items: Mapping[str, str], ttl: float | None = None) -> None:
        deadline = self._deadline(ttl)
        for key, value in items.items():
            self._data[key] = _Entry("string", value, deadline)

    async def delete_many(self, keys: Sequence[str]) -> int:
        return sum(1 for key in keys if self._data.pop(key, None) is not None)

    async def keys(self, pattern: str = "*") -> list[str]:
        self._purge()  # 顺手把过期的摘掉：列出来的键都在有效期内
        return [key for key in self._data if fnmatchcase(key, pattern)]

    async def clear(self) -> int:
        count = len(self._data)
        self._data.clear()
        return count

    # ---- 内部 ----
    async def _sweep_loop(self) -> None:
        """按时扫一遍过期键（惰性过期兜不住没人再访问的键）。"""
        while True:
            await asyncio.sleep(self._sweep_interval)
            self._purge()

    def _find(self, key: str) -> _Entry | None:
        """取未过期的记录（顺手删掉已过期的），不查结构类型。"""
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.deadline is not None and entry.deadline <= time.monotonic():
            del self._data[key]
            return None
        return entry

    def _typed(self, key: str, kind: str) -> _Entry | None:
        """取未过期的记录，并确认它就是 ``kind`` 那种结构（不是就报 WRONGTYPE）。"""
        entry = self._find(key)
        if entry is not None and entry.kind != kind:
            raise CacheError(f"键 {key} 存的是 {entry.kind}，不能按 {kind} 访问")
        return entry

    def _string(self, key: str) -> str | None:
        entry = self._typed(key, "string")
        return None if entry is None else cast(str, entry.value)

    def _list(self, key: str) -> list[str] | None:
        entry = self._typed(key, "list")
        return None if entry is None else cast(list[str], entry.value)

    def _hash(self, key: str) -> dict[str, str] | None:
        entry = self._typed(key, "hash")
        return None if entry is None else cast(dict[str, str], entry.value)

    @staticmethod
    def _deadline(ttl: float | None) -> float | None:
        """TTL 秒数 -> 过期时刻（``None`` = 永不过期）。"""
        return None if ttl is None else time.monotonic() + ttl

    @staticmethod
    def _check_strings(values: Sequence[object], what: str) -> None:
        """元素 / 字段值必须是 ``str``：收到别的类型就报错，别静默塞进去。

        Redis 那边由驱动抛 ``DataError``（同样是 CacheError），两边结果一致。
        """
        for value in values:
            if not isinstance(value, str):
                raise CacheError(f"{what}必须是字符串，收到 {value!r}")

    def _purge(self) -> int:
        """把已过期的键真正摘掉，返回摘掉的条数。

        #(TODO)仿照 redis 设计：这里该做「抽样删除」——随机取若干个键检查，过期比例够了
        就再来一轮；现在是全量扫一遍，键多了单次扫描会变长。「取值时删除」已经在
        :meth:`_find` 里（读到过期键顺手删）。
        """
        now = time.monotonic()
        stale = [
            key
            for key, entry in self._data.items()
            if entry.deadline is not None and entry.deadline <= now
        ]
        for key in stale:
            del self._data[key]
        return len(stale)
