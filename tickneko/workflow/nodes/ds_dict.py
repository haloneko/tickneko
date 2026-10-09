"""键值对节点：缓存里的一份字典变量当可操作结构 —— 按动作拆 6 个独立节点。

``ds-dict-set`` / ``ds-dict-get`` / ``ds-dict-contains`` / ``ds-dict-remove`` /
``ds-dict-keys`` / ``ds-dict-length``：一个节点一个动作，端口只留自己那部分；
**写动作不需要先建对象**：``set`` / ``remove`` 遇到变量不存在自动建空字典（隐式创建），
没有 new。

和 cache 节点一样，值存在作用域化缓存里（``scope`` + ``key`` 定位，键规则**完全同
cache 节点**），但它存取的不是文本，而是**任意 JSON 值**（``set_json`` / ``get_json``）。
一次执行改的是缓存里那份字典，下一趟还能接着改（跨执行持久化）。

**对象名传递**：每个节点都提供「变量名」入口（``key``，可**接线**也可**手填**，线上送的
优先）和「对象名」出口（``obj_out``，把**本次操作的变量名**原样送出）。把上一个键值对
节点的 ``obj_out`` 接给下一个节点的 ``key``，就是**同一个对象接着改**（作用域由各节点自己
的 config 决定）。不走 dict 端口 —— 数据沿 message 线走，画布上只有 message 类型。

config:
    scope:   作用域（缺省 ``workflow``）：``workflow`` = 图级；``account`` = 账号级
    key:     变量名（接线或手填，两个都没有才报错）
    field:   键名（``set`` / ``get`` / ``contains`` / ``remove`` 用；接线或手填）
    value:   写入的值（``set`` 用；手填兜底，也能接线）
    default: 键不存在时的默认值（``get`` 用）

节点 / 端口（数据沿连线走，类型都是 message）：

    ds-dict-set（键值对·设值）    入 ``key`` / ``field`` / ``value``     出 ``obj_out``
    ds-dict-get（键值对·取值）    入 ``key`` / ``field`` / ``default``   出 ``obj_out`` / ``value_out``
    ds-dict-contains（查存在）    入 ``key`` / ``field``                 出 ``true`` / ``false``（触发）+ ``obj_out`` / ``flag``
    ds-dict-remove（键值对·删除） 入 ``key`` / ``field``                 出 ``obj_out``
    ds-dict-keys（取键列表）      入 ``key``                             出 ``obj_out`` / ``list_out``
    ds-dict-length（条目数）      入 ``key``                             出 ``obj_out`` / ``count``

缓存键用前缀区分作用域（同 cache 节点，互不打扰）::

    workflow   workflow:graph:{图 id}:{变量名}
    account    workflow:acct:{账号 id}:{变量名}

口径：

* 值按**文本**存（``set`` 线上送整数会转成字符串）—— 和 cache 节点一致；
* **底层是哈希结构**：写字段 / 读字段 / 查存在 / 删字段 / 数条目都是服务端单键操作
  （HSET / HGET / HEXISTS / HDEL / HLEN），**不整表搬数据** —— 字段上千万也 O(1)；
  ``keys`` 列的是字段名（HKEYS），不搬值；
* **写动作隐式创建**：``set`` / ``remove`` 遇到变量不存在自动建空字典，不用先建（没有 new）；
* **变量名 / 键名都能接线**：线上送来的优先，没接线才用 config 里手填的值（``input_value``
  语义，同 cache 的 ``key``）；既不接线又不手填 → 运行时当场抛；
* **对象名传递**：``obj_out`` 把本次操作的变量名原样送出，接给下一个键值对节点的 ``key``
  就是接着改同一个对象 —— 不用重复填变量名，也不用 dict 端口；
* **查存在是分流节点**（同 ``condition``）：存在走 ``true`` 触发出口、不存在走 ``false``，
  没走的分支整段跳过（``[skip]`` 留痕，排查「后面为什么没跑」先看它）；``flag`` / ``obj_out``
  数据出口照常送值（走哪条数据边都拿得到）；
* ``get`` 键不存在**不算事故**：送 ``default``（没填就是空串），流程继续；
* **变量名不允许冒号**（同 cache 节点）；``key`` 为空当场抛；``field``（键名）为空当场抛
  （不知道操作哪个键）；
* 变量里存的不是字典（比如被别的节点写成 list）→ 本节点**业务失败**（``NodeFailure``），
  下游跳过、流程继续 —— 不是环境事故；
* 缓存后端不可用会抛 ``CacheError``（环境问题不吞不掩，同 cache 节点）。

小抄::

    链式攒数据:  ds-dict-set(key=用户库, field 接 用户id, value 接 内容)
                 -> 第二个 ds-dict-set 的 key 接第一个的 obj_out，接着往同一个对象里写
    查开关:      ds-dict-contains(field=静音) -> true 出口接处理逻辑，false 出口接忽略
    变量互通:    cache.get 只读文本、键值对节点读写结构 —— 键规则一样，同一份缓存
"""
from __future__ import annotations

from typing import Any

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
from tickneko.core.cache.models import CacheError
from .registry import register_node
from .variable_viewer import text_parameter
from .variable_viewers import DictViewer

#: 允许的作用域，**顺序即画布下拉顺序**
DS_DICT_SCOPE_ORDER: tuple[str, ...] = ("workflow", "account")

#: 日志里展示键 / 值时的截断长度（长值不刷屏）
CLIP_CHARS: int = 80


def validate_ds_dict_node(node: WorkflowNode) -> list[ValidationIssue]:
    """防呆：作用域枚举、手填变量名不带冒号；``key`` 的「必填」由入口管。"""
    issues: list[ValidationIssue] = []

    scope = node.config.get("scope")
    if isinstance(scope, str) and scope.strip() and scope.strip() not in DS_DICT_SCOPE_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_DICT_SCOPE",
                message=f"键值对节点 {node.id} 的作用域 {scope!r} 不合法",
                suggestion=f"可选：{' / '.join(DS_DICT_SCOPE_ORDER)}",
            )
        )

    # 手填变量名带冒号 = 保存时就能拦住（接线的值运行时再兜一道，见各 exec）
    key = node.config.get("key")
    if isinstance(key, str) and ":" in key:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_DICT_KEY",
                message=f"键值对节点 {node.id} 的变量名 {key!r} 不能包含冒号",
                suggestion="变量名不允许有:",
            )
        )

    return issues


def _clip(raw: str) -> str:
    """日志里展示键 / 值：太长截断（避免长值刷屏）。"""
    return raw if len(raw) <= CLIP_CHARS else raw[:CLIP_CHARS] + "…"


def _log(ctx: NodeExecutionContext, node: WorkflowNode, msg: str) -> None:
    ctx.logger.info(f"[键值对:{node.id}] {msg}")
    ctx.log.append(f"[键值对] {node.id}: {msg}")


def _text(raw: object) -> str:
    return "" if raw is None else str(raw)


def _var_name(node: WorkflowNode, ctx: NodeExecutionContext) -> str:
    """变量名（对象名）：接线或手填，strip 后返回。不做校验（校验在 _prepare）。"""
    return str(input_value(node, ctx, "key", default="")).strip()


def _required_field(node: WorkflowNode, ctx: NodeExecutionContext) -> str:
    """键名：接线或手填，没有就当场抛（不知道操作哪个键）。"""
    field = _text(input_value(node, ctx, "field", default=""))
    if not field:
        raise ValueError(f"节点 {node.id} 的 field（键名）为空（入口没接线，config 里也没填）")
    return field


def _prepare(node: WorkflowNode, ctx: NodeExecutionContext) -> str:
    """解析并校验 scope / key，返回完整缓存键；配置错误当场抛。"""
    scope = str(node.config.get("scope", "")).strip() or "workflow"
    if scope not in DS_DICT_SCOPE_ORDER:
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


async def _ensure_hash(
    node: WorkflowNode, ctx: NodeExecutionContext, full_key: str
) -> None:
    """确认缓存键是哈希结构：不是 → NodeFailure（业务失败，下游跳过）；不存在/字段无关紧要。

    用一次 ``HEXISTS`` 探测结构（O(1)，不拉数据）：键不存在返回 False，键是别的结构抛
    ``CacheError`` —— 这时再看键在不在，在就说明存了别的结构，不在就是并发被删，按空处理。
    """
    try:
        await ctx.cache.hash_exists(full_key, "*")
    except CacheError:
        if await ctx.cache.exists(full_key):
            raise NodeFailure(
                f"节点 {node.id} 的变量 {full_key} 不是字典（存的是别的结构）"
            ) from None
        # 键已经被删掉：按空字典继续（下面的操作对不存在键天然返回空/0）


# --------------------------------------------------------------------------- 节点注册
class DsDictViewer(DictViewer):
    """字典节点拥有文本编码；完整 fields 保存删除未提交的旧字段。"""

    priority = 100

    def encode(self, value: Any) -> str:
        return _text(text_parameter(value))


@register_node(
    "ds-dict-set",
    label="键值对·设值",
    color="#0891b2",
    order=150,
    category="ds_dict",
    # key / field 既是字段名也是数据入口（同 cache.key）：接线或手填都行，两个都没有才报错
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("field", "message", "键名"),
        PortSpec("value", "message", "写入的值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_DICT_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("field", "键名"),
        ConfigField("value", "值"),
    ],
    validator=validate_ds_dict_node,
    variable_viewer=DsDictViewer,
)
async def exec_ds_dict_set(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """设值：写 ``field=value``（HSET 增量写，不碰其他字段）；变量不存在自动建空字典。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    field = _required_field(node, ctx)
    value = _text(input_value(node, ctx, "value", default=""))
    await _ensure_hash(node, ctx, full_key)
    added = await ctx.cache.hash_set(full_key, {field: value})
    _log(ctx, node, f"set {full_key}[{field}] = {_clip(value)}{'（隐式建字典）' if added else ''}")
    return {"obj_out": name}


@register_node(
    "ds-dict-get",
    label="键值对·取值",
    color="#0891b2",
    order=151,
    category="ds_dict",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("field", "message", "键名"),
        PortSpec("default", "message", "键不存在时的默认值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("value_out", "message", "取到的值"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_DICT_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("field", "键名"),
        ConfigField("default", "键不存在时的默认值"),
    ],
    validator=validate_ds_dict_node,
    variable_viewer=DsDictViewer,
)
async def exec_ds_dict_get(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """取值：HGET 单字段读（不整表拉取）；键不存在**不算事故**，送 ``default``。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    field = _required_field(node, ctx)
    await _ensure_hash(node, ctx, full_key)
    value = await ctx.cache.hash_get(full_key, field)
    if value is None:
        # 键不存在不算事故：有默认值就用默认值（手填兜底 / 也能接线），没有才送空串
        raw_default = input_value(node, ctx, "default", default="")
        value = _text(raw_default)
        _log(ctx, node, f"get {full_key}[{field}] -> (没有，用默认值) {_clip(value)}")
    else:
        _log(ctx, node, f"get {full_key}[{field}] -> {_clip(value)}")
    return {"obj_out": name, "value_out": str(value)}


@register_node(
    "ds-dict-contains",
    label="键值对·查存在",
    color="#0891b2",
    order=152,
    category="ds_dict",
    # 分流节点（同 condition）：存在走 true 触发出口、不存在走 false，没走的分支整段跳过；
    # 另有一个恒触发的 ``trigger`` 出口：查存在只是"顺手看一眼"，不挡流程主链
    branching=True,
    min_outgoing=1,
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("field", "message", "键名"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("true", "trigger", "存在"),
        PortSpec("false", "trigger", "不存在"),
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("flag", "message", "是否存在（true/false）"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_DICT_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("field", "键名"),
    ],
    validator=validate_ds_dict_node,
    variable_viewer=DsDictViewer,
)
async def exec_ds_dict_contains(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """查存在：分流节点，HEXISTS 探测（O(1)），存在走 ``true``、不存在走 ``false``。

    ``trigger`` 出口恒触发（查存在不挡流程主链，可当「无条件继续」用）；
    true / false 二选一照旧剪枝。
    """
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    field = _required_field(node, ctx)
    await _ensure_hash(node, ctx, full_key)
    found = await ctx.cache.hash_exists(full_key, field)
    _log(ctx, node, f"contains {full_key}[{field}] -> {found}")
    return {
        "trigger": True,
        "true" if found else "false": True,
        "obj_out": name,
        "flag": "true" if found else "false",
    }


@register_node(
    "ds-dict-remove",
    label="键值对·删除",
    color="#0891b2",
    order=153,
    category="ds_dict",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
        PortSpec("field", "message", "键名"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_DICT_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("field", "键名"),
    ],
    validator=validate_ds_dict_node,
    variable_viewer=DsDictViewer,
)
async def exec_ds_dict_remove(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """删除键：HDEL 单字段删（不整表重写）；不存在的键不报错（日志注明）。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    field = _required_field(node, ctx)
    await _ensure_hash(node, ctx, full_key)
    removed = await ctx.cache.hash_delete(full_key, field)
    _log(ctx, node, f"remove {full_key}[{field}] -> {'删了' if removed else '本来就没有'}")
    return {"obj_out": name}


@register_node(
    "ds-dict-keys",
    label="键值对·取键列表",
    color="#0891b2",
    order=154,
    category="ds_dict",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "对象名"),
        PortSpec("list_out", "list", "键列表"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_DICT_SCOPE_ORDER),
        ConfigField("key", "变量名"),
    ],
    validator=validate_ds_dict_node,
    variable_viewer=DsDictViewer,
)
async def exec_ds_dict_keys(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """取键列表：HKEYS 只列字段名（不搬值），沿 list 端口原样送出。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    await _ensure_hash(node, ctx, full_key)
    keys = await ctx.cache.hash_keys(full_key)
    _log(ctx, node, f"keys {full_key} -> {len(keys)} 个")
    return {"obj_out": name, "list_out": keys}


@register_node(
    "ds-dict-length",
    label="键值对·条目数",
    color="#0891b2",
    order=155,
    category="ds_dict",
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("obj_out", "message", "变量名"),
        PortSpec("count", "message", "条目数"),
    ],
    fields=[
        ConfigField("scope", "作用域", default="workflow", options=DS_DICT_SCOPE_ORDER),
        ConfigField("key", "变量名"),
    ],
    validator=validate_ds_dict_node,
    variable_viewer=DsDictViewer,
)
async def exec_ds_dict_length(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """条目数：HLEN 服务端数（O(1)，不拉数据），文本化送出。"""
    full_key = _prepare(node, ctx)
    name = _var_name(node, ctx)
    await _ensure_hash(node, ctx, full_key)
    count = await ctx.cache.hash_length(full_key)
    _log(ctx, node, f"length {full_key} -> {count}")
    return {"obj_out": name, "count": str(count)}
