"""缓存层示例：一套 API 两种后端、TTL 语义、降级、配置接入。

运行::

    python examples/cache_demo.py

跑完约 1 分钟（本机起了 Redis 时约 15 秒）—— 大头在第 7 段：连接被拒之后驱动还会重试，
每次要等二十几秒。最后一段「真连 Redis」可选，先探一下端口，没人听就直接跳过。

缓存层对外只有一个门面 :class:`~tickneko.core.cache.core.Cache`：业务代码只写 ``get`` /
``set`` / ``list_push_right`` / ``hash_set`` 这一套，背后是 Redis 还是进程内存由 ``backend``
决定 —— 两边语义对齐（键是 ``str``，值是字符串 / 列表 / 哈希三种结构之一、TTL 按秒），
换后端不用改业务代码。``tickneko.core.cache`` 里的 ``cache`` 就是 ``Cache()`` 的进程级单例
（和 ``app.py`` 里用的是同一个）。

按顺序演示这几件事：

0. **默认就能用**：``backend`` 默认 ``memory``，不装 Redis、不起服务也能直接读写；但
   **不隐式启动** —— 没 ``start()`` 就调数据接口会抛 :class:`CacheError`；
1. **TTL 三态**：``ttl()`` 用 ``None`` 表示键不存在、``inf`` 表示永不过期、其余是剩余秒数；
   ``expire()`` 能给已有的键补过期时间（键不存在则返回 ``False``）；
2. **选项从哪来**：``CacheOptions.from_mapping(...)`` 吃一份普通映射（``app.py`` 喂的是
   ``Settings.cache.model_dump()``），缓存层自己不读配置文件；``default_ttl`` 是 ``set()``
   不带 ttl 时的兜底；
3. **批量与自增**：``set_many`` / ``get_many`` / ``delete_many`` 成批走；``incr`` 是原子的，
   值不是整数字符串时和 Redis 一样报错；
4. **结构化数据**：列表（``list_push_right`` / ``list_pop_left`` / ``list_range``）与哈希
   （``hash_set`` / ``hash_get_all`` / ``hash_delete``）；嵌套结构（哈希的哈希）用
   ``set_json`` / ``get_json`` 存 —— 一个键只能按写入时的那种结构访问，换一种会报错；
5. **列键与清空**：``keys()`` 支持通配符（列出来的是有效期内、不带前缀的逻辑键），
   ``clear()`` 清掉全部并返回条数；
6. **配置冻结在 start 那一刻**：启动后再 ``configure()`` 直接抛错，要改配置先 ``stop()``；
   停机是幂等的，停机后数据接口拒绝服务；
7. **Redis 连不上时的两条路**：默认**当场报错**，报错信息里提示去 ``config.toml`` 的
   ``[cache.redis]`` 检查；只有显式 ``fallback_to_memory = true`` 才退回内存（记一条
   warning，上层无感，要上报就问 ``degraded``）；
8. **真连（可选）**：本机起了 Redis 就真跑一遍，顺带看命名空间怎么把同名键隔开。
"""

import asyncio
import socket
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings  # noqa: E402
from tickneko.core.cache import (  # noqa: E402
    Cache,
    CacheError,
    CacheOptions,
    RedisOptions,
    cache,
)
from tickneko.core.logger import BaseLogger, LogCore, configure, default_core  # noqa: E402
from tickneko.core.logger import manager as log_manager  # noqa: E402

#: 业务日志实例：main 里先 configure() 建进程默认核心，再取它
log: BaseLogger


def enable_line_buffering() -> None:
    """让 stdout 逐行刷出，输出被重定向 / 管道捕获时行序才不乱。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(line_buffering=True)


async def settle(core: LogCore) -> None:
    """等日志真正落到控制台，让小标题和它下面的日志行对齐。

    写入是**非阻塞**的：``info()`` 只把记录推进队列，由分发器按 ``dispatch_timeout``
    批量取走，所以 ``print`` 与日志行之间本来有一小段延迟差。示例里每处要对着日志说话
    的地方等一下（生产环境不用管，正常业务不关心落地时机）。
    """
    await asyncio.sleep(0.1)
    await core.flush()


def free_port() -> int:
    """要一个当前没人监听的端口：连它会被立刻拒绝，用来模拟「Redis 没起」。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        address = cast(tuple[str, int], sock.getsockname())
        return address[1]


def can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    """探一下端口有没有人在听：几百毫秒就出结果，比让驱动去重试快得多。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def show(tag: str, facade: Cache) -> None:
    """打印门面此刻的状态：实际在用哪个后端、配的是哪个、有没有降级。"""
    live = f"running={facade.running} 实际后端={facade.backend_name}"
    configured = f"配的后端={facade.options.backend} degraded={facade.degraded}"
    print(f"  {tag}：{live} {configured}")


# --------------------------------------------------------------------------- 主流程
async def main() -> None:
    global log  # 日志实例要在 configure() 之后取，见下面两行

    enable_line_buffering()
    # 先建进程默认核心再取日志实例：缓存层自己记的 warning（比如 Redis 连不上而退回内存）
    # 也进这个核心，所以它的提示和业务日志会出现在同一个控制台上
    core: LogCore = configure("tickneko", console_color=True, dispatch_timeout=0.05)
    await core.start()
    log = default_core().child("demo.cache")

    try:
        # ---------------------------------------------------------------- 0. 默认
        print("=== 0. 默认就是本地内存：start() 之后即可读写，不用装 Redis ===")
        log.info("缓存示例开始")
        print("  约定：键、值都是 str，TTL 按秒 —— 两个后端守的是同一份约定（见 interfaces.py）。")
        try:
            await cache.get("greeting")
        except CacheError as exc:
            print(f"  还没 start() 就调数据接口：{exc}")
        print("  这就是「不隐式启动」：起不起得来要在启动阶段见分晓，而不是等哪次 get 才炸。")
        await cache.start()
        await cache.start()  # 重复启动是空操作
        await settle(core)  # 等「缓存就绪：memory」那行日志落地
        show("启动后", cache)
        await cache.set("greeting", "你好")
        await cache.set("unread", "0", ttl=5)
        print(f"  get('greeting') = {await cache.get('greeting')!r}")
        print(f"  exists('greeting') = {await cache.exists('greeting')}")
        print(f"  get('没写过的键') = {await cache.get('没写过的键')!r}（键不存在就是 None）")

        # ------------------------------------------------------------------ 1. TTL
        print("\n=== 1. TTL 三态：None = 键不存在 / inf = 永不过期 / 其余是剩余秒数 ===")
        await cache.set("forever", "不设过期")
        print(f"  set 不带 ttl -> ttl('forever') = {await cache.ttl('forever')}（inf）")
        await cache.set("brief", "只活 1 秒", ttl=1.0)
        print(f"  刚写完 -> ttl('brief') = {await cache.ttl('brief'):.2f}")
        await asyncio.sleep(1.1)
        brief_ttl = await cache.ttl("brief")
        print(f"  1.1 秒后 -> ttl('brief') = {brief_ttl}，get('brief') = {await cache.get('brief')!r}")
        print("  过期是真过期：读到过期键会顺手删掉，没人再访问的过期键由后台清扫任务收走。")
        renewed = await cache.expire("forever", 1.0)
        missing = await cache.expire("没写过的键", 1.0)
        print(f"  expire() 给已有的键补过期 -> {renewed}；键不存在则返回 False -> {missing}")

        # ------------------------------------------------------------ 2. 选项从哪来
        print("\n=== 2. 选项从哪来：一份普通映射（app.py 喂的是 Settings.cache.model_dump()） ===")
        defaults = CacheOptions.from_mapping(Settings().cache.model_dump())
        shown = f"backend={defaults.backend} namespace={defaults.namespace}"
        print(f"  没配 [cache] 区域时转出来就是默认值：{shown} default_ttl={defaults.default_ttl}")
        print("  缓存层不 import 配置模块，两边靠这份映射解耦（校验那一项归配置系统管）。")
        short_lived = Cache(CacheOptions.from_mapping({"default_ttl": 1.0}))
        await short_lived.start()
        await short_lived.set("k", "1 秒后自然消失")
        print(f"  default_ttl=1.0 的门面：set 没给 ttl，ttl('k') = {await short_lived.ttl('k'):.2f}")
        await asyncio.sleep(1.1)
        print(f"  1.1 秒后 -> get('k') = {await short_lived.get('k')!r}")
        await short_lived.stop()
        still_there = await cache.get("greeting")
        print(f"  它是自己 new 的独立实例，进程单例不受影响：cache 里 'greeting' 还在 = {still_there!r}")

        # ---------------------------------------------------------- 3. 批量与自增
        print("\n=== 3. 批量与自增：成批走一趟，incr 是原子的 ===")
        await cache.set_many({"user:1": "阿一", "user:2": "阿二", "user:3": "阿三"}, ttl=30)
        found = await cache.get_many(["user:1", "user:2", "没写过的"])
        print(f"  get_many(['user:1', 'user:2', '没写过的']) = {found}")
        print("  只返回拿到的键：没写过的那个干脆不出现，调用方按 key 取就行。")
        deleted = await cache.delete_many(["user:1", "user:2"])
        print(f"  delete_many(['user:1', 'user:2']) = {deleted}")
        hits = 0
        for _ in range(3):
            hits = await cache.incr("hits")
        print(f"  incr('hits') 连做三次 -> {hits}（键不存在时从 0 起算）")
        await cache.set("not_a_number", "abc")
        try:
            await cache.incr("not_a_number")
        except CacheError as exc:
            print(f"  值不是整数字符串时 incr 报错（和 Redis 一致）：{exc}")

        # ------------------------------------------------ 4. 列表 / 哈希 / JSON
        print("\n=== 4. 结构化数据：列表、哈希，嵌套结构走 JSON ===")
        await cache.list_push_right("queue", "任务一", "任务二", ttl=30)
        pushed = await cache.list_push_right("queue", "任务三")
        items = await cache.list_range("queue")
        print(f"  list_push_right 后再推一个返回 {pushed}，整条列表 = {items}")
        tail = await cache.list_range("queue", -2, -1)
        print(f"  list_range('queue', -2, -1) = {tail}（负数从右数，两端都含）")
        popped = await cache.list_pop_right("queue", 2)
        print(f"  list_pop_right('queue', 2) = {popped}（按弹出顺序：最右侧的先出来）")
        remaining = await cache.ttl("queue")
        left = await cache.list_length("queue")
        print(f"  剩 {left} 个；TTL 还是 {remaining:.1f} 秒 —— 往已有的键上追加不刷新过期时间")
        added = await cache.hash_set("user:1", {"name": "阿一", "age": "20"}, ttl=30)
        name = await cache.hash_get("user:1", "name")
        print(f"  hash_set 返回新增字段数 {added}，hash_get('user:1', 'name') = {name!r}")
        print(f"  整个哈希 = {await cache.hash_get_all('user:1')}")
        dropped = await cache.hash_delete("user:1", "age", "nope")
        print(f"  hash_delete 删掉 {dropped} 个字段（不存在的字段不算）")
        profile = {"name": "阿一", "tags": ["vip", "beta"], "meta": {"level": 3}}
        await cache.set_json("profile", profile)
        print(f"  set_json / get_json 装嵌套结构：{await cache.get_json('profile')}")
        print(f"        它在后端里就是一段 JSON 文本：get('profile') = {await cache.get('profile')}")
        try:
            await cache.incr("queue")  # 列表键不能按字符串用
        except CacheError as exc:
            print(f"  一个键只能按一种结构访问，对列表键做 incr 会报：{exc}")
        print("  弹空 / 字段删空之后键就没了 —— 这一点也照 Redis 来。")

        # ---------------------------------------------------------- 5. 列键与清空
        print("\n=== 5. keys() / clear()：列键支持通配符，清空返回条数 ===")
        print(f"  keys('user:*') = {sorted(await cache.keys('user:*'))}")
        print(f"  keys() 全部 = {sorted(await cache.keys())}")
        print(f"  clear() 清掉 {await cache.clear()} 个键，之后 keys() = {await cache.keys()}")
        print("  内存后端的 keys() 会顺手摘掉过期键，所以列出来的都在有效期内。")

        # ---------------------------------------------------------- 6. 配置冻结
        print("\n=== 6. 配置冻结在 start 那一刻：改配置得先 stop() ===")
        try:
            cache.configure(CacheOptions(backend="redis"))
        except CacheError as exc:
            print(f"  启动后 configure() 直接抛：{exc}")
        await cache.stop()
        await cache.stop()  # 重复停机也是空操作
        show("停机后", cache)
        try:
            await cache.get("greeting")
        except CacheError as exc:
            print(f"  停机后调数据接口：{exc}")
        cache.configure(CacheOptions(namespace="demo"))
        print(f"  stop() 之后再 configure() 才行，现在配的是 namespace={cache.options.namespace}")

        # ------------------------------------------------------------ 7. 连不上的两条路
        print("\n=== 7. 配了 Redis 但连不上：默认当场报错，显式打开 fallback 才退回内存 ===")
        unreachable = RedisOptions(host="127.0.0.1", port=free_port(), socket_timeout=1.0)
        print("  用一个没人监听的端口冒充「Redis 没起」，两条路各试一遍：")
        print("  （未配超时前驱动会对连接错误重试多次、建连超时走系统默认，要等二十几秒；")
        print("   cache.redis 的 socket_connect_timeout / connect_retries 就是为缩短这个等待。）")
        strict = Cache(CacheOptions(backend="redis", redis=unreachable))
        try:
            await strict.start()
        except CacheError as exc:
            print(f"  默认（fallback_to_memory=False）start() 当场抛：{exc}")
        show("起不来之后", strict)
        print("  连不上就停在没启动的状态（running 仍是 False），不留一个半死的门面。")
        graceful = Cache(
            CacheOptions(backend="redis", fallback_to_memory=True, redis=unreachable)
        )
        await graceful.start()
        await settle(core)  # 等那条 warning 落地
        show("显式 fallback_to_memory=True", graceful)
        await graceful.set("k", "v")
        print(f"  退回内存后读写照旧：get('k') = {await graceful.get('k')!r}")
        print("  上层不用为此写第二个分支，要上报就问 degraded。")
        await graceful.stop()

        # ------------------------------------------------------------ 8. 真连（可选）
        print("\n=== 8. 真连 Redis（可选）：本机有服务就真跑一遍 ===")
        if not can_connect("127.0.0.1", 6379):
            print("  6379 上没人听（本机没起 Redis），这一段跳过 —— 探端口几百毫秒就出结果，")
            print("  比让驱动去试连再等重试快得多。想真跑：pip install \"tickneko[redis]\" 装驱动，")
            print("  起个 redis-server（或 docker run -p 6379:6379 redis）再运行本示例。")
        else:
            live = Cache(
                CacheOptions(
                    backend="redis",
                    namespace="tickneko-demo",
                    redis=RedisOptions(socket_timeout=1.0),
                )
            )
            await live.start()
            if live.degraded:
                print("  端口通着但没握手成功（密码 / 版本之类），同样跳过真连。")
            else:
                await live.clear()
                await live.set("k", "真后端", ttl=30)
                print(f"  get('k') = {await live.get('k')!r}（decode_responses=True，读回来就是 str）")
                print(f"  keys() = {sorted(await live.keys())}（逻辑键，前缀在适配器内部拼）")
                other = Cache(
                    CacheOptions(
                        backend="redis",
                        namespace="tickneko-demo-other",
                        redis=RedisOptions(socket_timeout=1.0),
                    )
                )
                await other.start()
                await other.set("k", "换个命名空间")
                mine, theirs = await live.get("k"), await other.get("k")
                print(f"  同一个 Redis、另一个命名空间写同名键：本实例 get('k') 仍是 {mine!r}")
                print(f"        对方 get('k') = {theirs!r} —— 命名空间把同名键隔开了")
                await other.clear()  # 只清自己命名空间下的键
                await other.stop()
                await live.set_many({"a": "1", "b": "2"})
                print(f"  批量：get_many(['a', 'b', 'none']) = {await live.get_many(['a', 'b', 'none'])}")
                print(f"        delete_many(['a', 'b']) = {await live.delete_many(['a', 'b'])}")
                print(f"  incr('n') = {await live.incr('n')}")
                print(f"  clear() 清掉 {await live.clear()} 个键（只清自己命名空间下的）")
            await live.stop()
    finally:
        await cache.stop()
        await log_manager.stop()  # 停机冲刷日志余量
        print("\n=== 完：缓存与日志核心都已停掉 ===")


if __name__ == "__main__":
    asyncio.run(main())
