"""列表队列节点：缓存里的一份 list 变量当可操作队列 —— 按动作拆 7 个独立节点。

``ds-list-append`` / ``ds-list-push-left`` / ``ds-list-get`` / ``ds-list-contains`` /
``ds-list-pop`` / ``ds-list-pop-left`` / ``ds-list-length``：一个节点一个动作，端口只留
自己那部分，**不用选动作**；写动作不需要先建对象：``append`` 遇到变量不存在自动建空
队列（隐式创建），没有 new。

和 cache 节点一样，值存在作用域化缓存里（``scope`` + ``key`` 定位，键规则**完全同
cache 节点**），但它存的是队列，靠 RPUSH / LPUSH / LRANGE / LLEN / RPOP / LPOP 服务端
原子操作改（O(1)，不整表搬）。一次执行改的是缓存里那份队列，下一趟还能接着改（跨执行
持久化）。

**对象名传递**：每个节点都提供「变量名」入口（``key``，可**接线**也可**手填**，线上送的
优先）和「对象名」出口（``obj_out``，把**本次操作的变量名**原样送出）。把上一个列表节点
的 ``obj_out`` 接给下一个节点的 ``key``，就是**同一个对象接着改**（作用域由各节点自己
的 config 决定）。不走 list 端口 —— 数据沿 message 线走，画布上只有 message 类型。

config:
    scope:   作用域（缺省 ``workflow``）：``workflow`` = 图级；``account`` = 账号级
    key:     变量名（接线或手填，两个都没有才报错）
    item:    追加 / 头插的元素、contains 查的元素（接线或手填）
    index:   下标（``get`` 用；负数从后往前）
    default: 取不到时的默认值（``get`` / ``pop`` / ``pop_left`` 用）

节点 / 端口（数据沿连线走）：

    ds-list-append（追加队尾）    入 ``key`` / ``item``             出 ``obj_out``
    ds-list-push-left（头插）     入 ``key`` / ``item``             出 ``obj_out``
    ds-list-get（按下标取）       入 ``key`` / ``index`` / ``default``  出 ``obj_out`` / ``value_out``
    ds-list-contains（查存在）    入 ``key`` / ``item``             出 ``true`` / ``false``（触发）+ ``obj_out`` / ``flag``
    ds-list-pop（队尾弹出）       入 ``key`` / ``default``          出 ``obj_out`` / ``value_out``
    ds-list-pop-left（队头弹出）  入 ``key`` / ``default``          出 ``obj_out`` / ``value_out``
    ds-list-length（长度）        入 ``key``                       出 ``obj_out`` / ``count``

缓存键用前缀区分作用域（同 cache 节点，互不打扰）::

    workflow   workflow:graph:{图 id}:{变量名}
    account    workflow:acct:{账号 id}:{变量名}

一份逻辑变量在缓存里拆成三个物理键（Redis 的 key 类型互斥，list 和 hash 不能共存）：
    ``<key>``        队列本体（list 结构，走 RPUSH / LRANGE / LLEN / RPOP / LPOP 原子操作）；
    ``<key>:meta``   计数辅助（hash 结构），``count`` 字段由队列动作**自动联动**；
    ``<key>:idx``    存在性索引（hash 结构，元素 -> 出现次数），让 ``contains`` 走 O(1) ``HEXISTS``。

联动规则（"添加计数加、弹出计数减、长度算 list、contains 走索引"）：
* ``append`` / ``push_left``：元素推进队列，``meta.count`` **+1**，``idx`` 出现次数 **+1**；
* ``pop`` / ``pop_left``：从队列弹出，``meta.count`` **-1**，``idx`` 出现次数 **-1**（归零删索引）；
* ``contains``：直接 ``HEXISTS <key>:idx`` —— **O(1)**，不扫队列；
* ``length``：返回 **list 实际长度**（``LLEN``），不读计数。

口径：

* 值按**文本**存（``item`` 线上送整数会转成字符串）—— 和 cache 节点一致；
* **底层是 list 结构**：追加 / 头插 / 按下标取 / 双端弹出 / 数长度都是服务端单键原子操作
  （RPUSH / LPUSH / LRANGE / RPOP / LPOP / LLEN），**不整表搬数据**；``contains`` 走辅助
  索引 hash（元素出现次数），O(1)，不扫队列；
* **写动作隐式创建**：``append`` / ``push_left`` 遇到变量不存在自动建空队列，不用先建（没有 new）；
* **变量名 / 元素都能接线**：线上送来的优先，没接线才用 config 里手填的值（``input_value``
  语义，同 cache 的 ``key``）；既不接线又不手填 → 运行时当场抛；
* **对象名传递**：``obj_out`` 把本次操作的变量名原样送出，接给下一个列表节点的 ``key``
  就是接着改同一个对象 —— 不用重复填变量名；
* **查存在是分流节点**（同 ``condition`` / ``ds-dict-contains``）：存在走 ``true`` 触发出口、
  不存在走 ``false``，没走的分支整段跳过（``[skip]`` 留痕，排查「后面为什么没跑」先看它）；
  ``flag`` / ``obj_out`` 数据出口照常送值（走哪条数据边都拿得到）；
* ``get`` 越界 / ``pop`` 弹空**不算事故**：送 ``default``（没填就是空串），流程继续；
  ``index`` 格式错当场抛；
* **计数 / 索引是辅助**（读改写组合、非原子），list 本体与长度才是权威——以 ``LLEN`` 为准；
* 索引存**出现次数**而非存在标记：list 允许重复元素，pop 掉一个还有别的在场，纯标记会误报；
* **不做按中间下标删除**这种 O(N) 整表重建（要消费就双端弹出）；
* **变量名不允许冒号**（同 cache 节点）；``key`` 为空当场抛；
* 变量里存的不是列表（比如被别的节点写成 hash）→ 本节点**业务失败**（``NodeFailure``），
  下游跳过、流程继续 —— 不是环境事故；
* 缓存后端不可用会抛 ``CacheError``（环境问题不吞不掩，同 cache 节点）。

小抄::

    攒队列:   ds-list-append(key=待处理, item 接 内容) -> ds-list-length 看积压
    按序消费: ds-list-pop-left 队头取 -> 送下游；要 LIFO 就 ds-list-pop 队尾取
    双端队列: ds-list-push-left 塞队头 -> ds-list-pop 从队尾取
    判重/查在不在: ds-list-contains(item) -> true 出口接处理逻辑，false 出口接忽略（O(1) 走索引）
"""
from __future__ import annotations

from typing import Any

from tickneko.core.cache.models import CacheError

from ..models import ValidationIssue, WorkflowNode
from .base import (
    TRIGGER_PORT,
    ConfigField,
    NodeExecutionContext,
    NodeFailure,
    PortSpec,
    cache_key,
    input_value,
)
from .registry import register_node

#: 允许的作用域，**顺序即画布下拉顺序**
DS_LIST_SCOPE_ORDER: tuple[str, ...] = ("workflow", "account")

#: 元数据里联动维护的计数字段名（内部辅助，不暴露 map 操作）
META_COUNT_FIELD: str = "count"

#: 日志里展示键 / 值时的截断长度（长值不刷屏）
CLIP_CHARS: int = 80


def validate_ds_list_node(node: WorkflowNode) -> list[ValidationIssue]:
    """防呆：作用域枚举、手填变量名不带冒号；``key`` 的「必填」由入口管。"""
    issues: list[ValidationIssue] = []

    scope = node.config.get("scope")
    if isinstance(scope, str) and scope.strip() and scope.strip() not in DS_LIST_SCOPE_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_LIST_SCOPE",
                message=f"列表节点 {node.id} 的作用域 {scope!r} 不合法",
                suggestion=f"可选：{' / '.join(DS_LIST_SCOPE_ORDER)}",
            )
        )

    # 手填变量名带冒号 = 保存时就能拦住（接线的值运行时再兜一道，见各 exec）
    key = node.config.get("key")
    if isinstance(key, str) and ":" in key:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_LIST_KEY",
                message=f"列表节点 {node.id} 的变量名 {key!r} 不能包含冒号",
                suggestion="变量名不允许有:",
            )
        )

    return issues


def _clip(raw: str) -> str:
    """日志里展示键 / 值：太长截断（避免长值刷屏）。"""
    return raw if len(raw) <= CLIP_CHARS else raw[:CLIP_CHARS] + "…"


def _log(ctx: NodeExecutionContext, node: WorkflowNode, msg: str) -> None:
    ctx.logger.info(f"[列表:{node.id}] {msg}")
    ctx.log.append(f"[列表] {node.id}: {msg}")


def _text(raw: object) -> str:
    return "" if raw is None else str(raw)


def _var_name(node: WorkflowNode, ctx: NodeExecutionContext) -> str:
    """变量名（对象名）：接线或手填，strip 后返回。不做校验（校验在 _prepare）。"""
    return str(input_value(node, ctx, "key", default="")).strip()


def _prepare(node: WorkflowNode, ctx: NodeExecutionContext) -> str:
    """解析并校验 scope / key，返回队列本体缓存键；配置错误当场抛。"""
    scope = str(node.config.get("scope", "")).strip() or "workflow"
    if scope not in DS_LIST_SCOPE_ORDER:
        raise ValueError(f"节点 {node.id} 的作用域不合法：{scope!r}")

    key = _var_name(node, ctx)
    if not key:
        raise ValueError(f"节点 {node.id} 的 key 为空（入口没接线，config 里也没填）")
    if ":" in key:
        raise ValueError(
            f"节点 {node.id} 的变量名 {key!r} 不能包含冒号"
            "（手填或上游送来的都不行）"
        )
    return cache_key(node, ctx, scope, key)


async def _ensure_list(node: WorkflowNode, ctx: NodeExecutionContext, full_key: str) -> None:
    """确认队列键是列表结构：不是 → NodeFailure（业务失败，下游跳过）。

    用一次 ``LLEN`` 探测结构（O(1)，不拉数据）：键不存在返回 0；键是别的结构抛
    ``CacheError`` —— 这时再看键在不在，在就说明存了别的结构，不在就是并发被删，按空处理。
    """
    try:
        await ctx.cache.list_length(full_key)
    except CacheError:
        if await ctx.cache.exists(full_key):
            raise NodeFailure(
                f"节点 {node.id} 的变量 {full_key} 不是列表（存的是别的结构）"
            ) from None
        # 键已经被删掉：按空列表继续（下面的操作对不存在键天然返回空/0）


async def _bump_count(node: WorkflowNode, ctx: NodeExecutionContext, full_key: str, delta: int) -> None:
    """联动内部计数：加元素 +1、弹元素 -1（弹空不调）。读改写组合，非原子，长度以 list 为准。"""
    meta_key = f"{full_key}:meta"
    current = await ctx.cache.hash_get(meta_key, META_COUNT_FIELD)
    n = 0
    if current is not None:
        try:
            n = int(current)
        except ValueError:
            n = 0  # 计数被写坏（非数字）就当 0，不炸
    n = max(0, n + delta)
    await ctx.cache.hash_set(meta_key, {META_COUNT_FIELD: str(n)})


async def _bump_index(node: WorkflowNode, ctx: NodeExecutionContext, full_key: str, item: str, delta: int) -> None:
    """维护存在性索引（<key>:idx，hash 元素->出现次数）：加元素 +1、弹元素 -1，归零删索引。

    O(1)，让 ``contains`` 走 HEXISTS 而不是全量扫队列；存次数是为了顶住重复元素。
    """
    idx_key = f"{full_key}:idx"
    current = await ctx.cache.hash_get(idx_key, item)
    n = 0
    if current is not None:
        try:
            n = int(current)
        except ValueError:
            n = 0  # 索引被写坏（非数字）就当 0，不炸
    n += delta
    if n <= 0:
        await ctx.cache.hash_delete(idx_key, item)
    else:
        await ctx.cache.hash_set(idx_key, {item: str(n)})


# --------------------------------------------------------------------------- 节点注册
@register_node(
    "ds-list-append",
    label="列表·追加队尾",
    color="#a855f7",
    order=156,
    category="ds_list",
    # key / item 既是字段名也是数据入口（同 cache.key）：接线或手填都行，两个都没有才报错
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("item", "message", "追加的元素"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_LIST_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("item", "追加的元素"),
    ],
    validator=validate_ds_list_node,
)
async def exec_ds_list_append(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """追加队尾：RPUSH 推进，计数 / 索引联动 +1；变量不存在自动建空队列。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    item = _text(input_value(node, ctx, "item", default=""))
    await _ensure_list(node, ctx, full_key)
    await ctx.cache.list_push_right(full_key, item)
    await _bump_count(node, ctx, full_key, 1)
    await _bump_index(node, ctx, full_key, item, 1)
    _log(ctx, node, f"append {full_key} <- {_clip(item)}（计数+1，索引+1）")
    return {"obj_out": name}


@register_node(
    "ds-list-push-left",
    label="列表·头插",
    color="#a855f7",
    order=157,
    category="ds_list",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("item", "message", "插入的元素"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_LIST_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("item", "插入的元素"),
    ],
    validator=validate_ds_list_node,
)
async def exec_ds_list_push_left(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """头插：LPUSH 从头部推进，计数 / 索引联动 +1。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    item = _text(input_value(node, ctx, "item", default=""))
    await _ensure_list(node, ctx, full_key)
    await ctx.cache.list_push_left(full_key, item)
    await _bump_count(node, ctx, full_key, 1)
    await _bump_index(node, ctx, full_key, item, 1)
    _log(ctx, node, f"push_left {full_key} <- {_clip(item)}（计数+1，索引+1）")
    return {"obj_out": name}


@register_node(
    "ds-list-get",
    label="列表·按下标取",
    color="#a855f7",
    order=158,
    category="ds_list",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("index", "message", "下标"),
        PortSpec("default", "message", "越界时的默认值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("value_out", "message", "取到的值"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_LIST_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("index", "下标"),
        ConfigField("default", "越界时的默认值"),
    ],
    validator=validate_ds_list_node,
)
async def exec_ds_list_get(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """按下标取：LRANGE 单元素读；越界不算事故，送 ``default``；负数从后往前。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    raw = str(input_value(node, ctx, "index", default="")).strip()
    if not raw:
        raise ValueError(f"节点 {node.id} 的 index 为空（入口没接线，config 里也没填）")
    try:
        idx = int(raw)
    except ValueError:
        raise ValueError(f"节点 {node.id} 的 index 不是整数：{raw!r}")
    await _ensure_list(node, ctx, full_key)
    hits = await ctx.cache.list_range(full_key, idx, idx)  # LRANGE：负数从右数、越界自动裁剪
    if hits:
        value = hits[0]
        _log(ctx, node, f"get {full_key}[{idx}] -> {_clip(value)}")
    else:
        value = _text(input_value(node, ctx, "default", default=""))
        _log(ctx, node, f"get {full_key}[{idx}] -> (越界，用默认值) {_clip(value)}")
    return {"obj_out": name, "value_out": value}


@register_node(
    "ds-list-contains",
    label="列表·查存在",
    color="#a855f7",
    order=159,
    category="ds_list",
    # 分流节点（同 condition / ds-dict-contains）：存在走 true 触发出口、不存在走 false，
    # 没走的分支整段跳过；另有一个恒触发的 ``trigger`` 出口：查存在只是"顺手看一眼"，不挡流程主链
    branching=True,
    min_outgoing=1,
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("item", "message", "要查的元素"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("true", "trigger", "存在"),
        PortSpec("false", "trigger", "不存在"),
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("flag", "message", "是否存在"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_LIST_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("item", "要查的元素"),
    ],
    validator=validate_ds_list_node,
)
async def exec_ds_list_contains(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """查存在：分流节点，走存在性索引 HEXISTS，存在走 ``true``、不存在走 ``false``。

    ``trigger`` 出口恒触发（查存在不挡流程主链，可当「无条件继续」用）；
    true / false 二选一照旧剪枝。
    """
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    item = _text(input_value(node, ctx, "item", default=""))
    await _ensure_list(node, ctx, full_key)
    found = await ctx.cache.hash_exists(f"{full_key}:idx", item)
    _log(ctx, node, f"contains {full_key}[{_clip(item)}] -> {found}（走索引）")
    return {
        "trigger": True,
        "true" if found else "false": True,
        "obj_out": name,
        "flag": "true" if found else "false",
    }


@register_node(
    "ds-list-pop",
    label="列表·队尾弹出",
    color="#a855f7",
    order=160,
    category="ds_list",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("default", "message", "弹空时的默认值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("value_out", "message", "弹出的值"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_LIST_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("default", "弹空时的默认值"),
    ],
    validator=validate_ds_list_node,
)
async def exec_ds_list_pop(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """队尾弹出：RPOP 取走，计数 / 索引联动 -1；弹空不算事故，送 ``default``。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    await _ensure_list(node, ctx, full_key)
    popped = await ctx.cache.list_pop_right(full_key)
    if popped:
        value = popped[0]
        await _bump_count(node, ctx, full_key, -1)
        await _bump_index(node, ctx, full_key, value, -1)
        _log(ctx, node, f"pop {full_key} -> 弹出 {_clip(value)}（计数-1，索引-1）")
    else:
        value = _text(input_value(node, ctx, "default", default=""))
        _log(ctx, node, f"pop {full_key} -> (空了，用默认值) {_clip(value)}")
    return {"obj_out": name, "value_out": value}


@register_node(
    "ds-list-pop-left",
    label="列表·队头弹出",
    color="#a855f7",
    order=161,
    category="ds_list",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("default", "message", "弹空时的默认值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("value_out", "message", "弹出的值"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_LIST_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("default", "弹空时的默认值"),
    ],
    validator=validate_ds_list_node,
)
async def exec_ds_list_pop_left(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """队头弹出：LPOP 取走，计数 / 索引联动 -1；弹空不算事故，送 ``default``。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    await _ensure_list(node, ctx, full_key)
    popped = await ctx.cache.list_pop_left(full_key)
    if popped:
        value = popped[0]
        await _bump_count(node, ctx, full_key, -1)
        await _bump_index(node, ctx, full_key, value, -1)
        _log(ctx, node, f"pop_left {full_key} -> 弹出 {_clip(value)}（计数-1，索引-1）")
    else:
        value = _text(input_value(node, ctx, "default", default=""))
        _log(ctx, node, f"pop_left {full_key} -> (空了，用默认值) {_clip(value)}")
    return {"obj_out": name, "value_out": value}


@register_node(
    "ds-list-length",
    label="列表·长度",
    color="#a855f7",
    order=162,
    category="ds_list",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("count", "message", "长度"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_LIST_SCOPE_ORDER),
        ConfigField("key", "变量名"),
    ],
    validator=validate_ds_list_node,
)
async def exec_ds_list_length(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """长度：LLEN 服务端数，**算 list 本体**、不读计数辅助。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    await _ensure_list(node, ctx, full_key)
    count = await ctx.cache.list_length(full_key)
    _log(ctx, node, f"length {full_key} -> {count}（算 list 长度）")
    return {"obj_out": name, "count": str(count)}
