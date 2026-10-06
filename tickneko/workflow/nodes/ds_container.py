"""复合结构节点：把缓存里的一份容器变量（list / map 一体）当可操作的结构。

有顺序（``list_get`` 按下标、``map_keys`` 按插入序），能检测存在（``contains``）；
遍历以后单独加，顺序遍历用「下标取 + operator 自增」。

和键值对系列节点（``ds-dict-*``）一样走作用域化缓存（``scope`` + ``key`` 定位），
``ctx.cache.set_json`` / ``get_json`` 存取任意 JSON；list 元素 / map 值按**文本**存
（与 cache 节点同一口径）。

config:
    action:   ``list_append`` / ``list_get`` / ``list_contains`` / ``list_remove`` /
              ``list_length`` 操作列表；``map_set`` / ``map_get`` / ``map_contains`` /
              ``map_remove`` / ``map_keys`` / ``map_length`` 操作字典（缺省 ``list_append``）
    scope:    作用域（缺省 ``workflow``）：``workflow`` = 图级；``account`` = 账号级
    key:      变量名（必填：接线或手填）
    item:     追加的元素（``list_append``）
    index:    下标（``list_get`` / ``list_remove``；负数从后往前）
    field:    键名（map 组）
    value:    写入的值（``list_append`` / ``map_set``）
    default:  取不到时的默认值（``list_get`` / ``map_get``）

端口（数据沿连线走，没有全局变量）：
    ``key`` / ``item`` / ``index`` / ``field`` / ``value`` / ``default``  数据入口；
    ``list_out``   出口：列表**对象**（list 组写动作）或 ``map_keys`` 键列表（list 端口）；
    ``dict_out``   出口：字典**对象**（map 组写动作，dict 端口）；
    ``value_out``  出口：取到的值（``list_get`` / ``map_get``，message 端口）；
    ``flag``       出口：``contains`` 结果（``true`` / ``false``）；
    ``count``      出口：``length`` 结果。

口径：
* ``get`` 越界 / 键不存在**不算事故**：送 ``default``（没填空串）；``index`` / ``field``
  格式错当场抛 —— 是配置写错，不是数据问题；
* ``list_remove`` 越界不删（日志注明），流程继续；
* 变量里存的不是对应结构 → **业务失败**（``NodeFailure``），下游跳过、流程继续；
* 变量名不允许冒号、账号级作用域没有归属当场抛（同 cache 节点）。

小抄::

    攒队列:   ds-container(list_append) -> list_length 看积压
    按序消费: list_get(index 接 operator 自增) -> 送下游 -> list_remove 顺手清掉
    查开关:   map_contains(field=静音) -> condition(== true)
    配置读写: map_set(field=主题色) -> map_get 随手取
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
from .registry import register_node

#: 允许的动作，**顺序即画布下拉顺序**（list 组在前、map 组在后）
DS_CONTAINER_ACTION_ORDER: tuple[str, ...] = (
    "list_append",
    "list_get",
    "list_contains",
    "list_remove",
    "list_length",
    "map_set",
    "map_get",
    "map_contains",
    "map_remove",
    "map_keys",
    "map_length",
)

#: list 组 / map 组：运行期按组决定读出来的是列表还是字典
LIST_ACTIONS: frozenset[str] = frozenset(
    ("list_append", "list_get", "list_contains", "list_remove", "list_length")
)
MAP_ACTIONS: frozenset[str] = frozenset(
    ("map_set", "map_get", "map_contains", "map_remove", "map_keys", "map_length")
)

#: 允许的作用域，**顺序即画布下拉顺序**
DS_CONTAINER_SCOPE_ORDER: tuple[str, ...] = ("workflow", "account")

#: 日志里展示键 / 值时的截断长度（长值不刷屏）
CLIP_CHARS: int = 80


def validate_ds_container_node(node: WorkflowNode) -> list[ValidationIssue]:
    """防呆：动作 / 作用域枚举、手填变量名不带冒号；``key`` 的「必填」由入口管。"""
    issues: list[ValidationIssue] = []

    action = node.config.get("action")
    if isinstance(action, str) and action.strip() and action.strip() not in DS_CONTAINER_ACTION_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_CONTAINER_ACTION",
                message=f"ds-container 节点 {node.id} 的动作 {action!r} 不合法",
                suggestion=f"可选：{' / '.join(DS_CONTAINER_ACTION_ORDER)}",
            )
        )

    scope = node.config.get("scope")
    if isinstance(scope, str) and scope.strip() and scope.strip() not in DS_CONTAINER_SCOPE_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_CONTAINER_SCOPE",
                message=f"ds-container 节点 {node.id} 的作用域 {scope!r} 不合法",
                suggestion=f"可选：{' / '.join(DS_CONTAINER_SCOPE_ORDER)}",
            )
        )

    key = node.config.get("key")
    if isinstance(key, str) and ":" in key:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_CONTAINER_KEY",
                message=f"ds-container 节点 {node.id} 的变量名 {key!r} 不能包含冒号",
                suggestion="变量名不允许有:",
            )
        )

    return issues


def _clip(raw: str) -> str:
    return raw if len(raw) <= CLIP_CHARS else raw[:CLIP_CHARS] + "…"


def _log(ctx: NodeExecutionContext, node: WorkflowNode, msg: str) -> None:
    ctx.logger.info(f"[ds-container:{node.id}] {msg}")
    ctx.log.append(f"[ds-container] {node.id}: {msg}")


def _text(raw: object) -> str:
    return "" if raw is None else str(raw)


def _required_field(node: WorkflowNode, ctx: NodeExecutionContext) -> str:
    """map 组的键名：接线或手填，没有就当场抛（不知道操作哪个键）。"""
    field = _text(input_value(node, ctx, "field", default=""))
    if not field:
        raise ValueError(f"节点 {node.id} 的 field（键名）为空（入口没接线，config 里也没填）")
    return field


def _parse_index(node: WorkflowNode, ctx: NodeExecutionContext, length: int) -> int:
    """下标：格式错当场抛（配置问题）；负数归一；越界交给调用方（数据问题走宽松分支）。"""
    raw = input_value(node, ctx, "index", default="")
    text = str(raw).strip()
    if not text:
        raise ValueError(f"节点 {node.id} 的 index 为空（入口没接线，config 里也没填）")
    try:
        idx = int(text)
    except ValueError:
        raise ValueError(f"节点 {node.id} 的 index 不是整数：{text!r}")
    if idx < 0:
        idx += length
    return idx


async def _load_list(node: WorkflowNode, ctx: NodeExecutionContext, full_key: str) -> list[str]:
    raw = await ctx.cache.get_json(full_key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise NodeFailure(f"节点 {node.id} 的变量 {full_key} 不是列表（是 {type(raw).__name__}）")
    return raw


async def _load_dict(node: WorkflowNode, ctx: NodeExecutionContext, full_key: str) -> dict[str, str]:
    raw = await ctx.cache.get_json(full_key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise NodeFailure(f"节点 {node.id} 的变量 {full_key} 不是字典（是 {type(raw).__name__}）")
    return raw


@register_node(
    "ds-container",
    label="复合数据结构",
    color="#a855f7",
    order=152,
    category="data",
    # key 既是字段名也是数据入口（同 cache.key）：接线或手填都行，两个都没有才报错
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名", required=True),
        PortSpec("item", "message", "追加的元素"),
        PortSpec("index", "message", "下标"),
        PortSpec("field", "message", "键名"),
        PortSpec("value", "message", "写入的值"),
        PortSpec("default", "message", "取不到时的默认值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("list_out", "list", "列表对象（list 组写动作 / map_keys）"),
        PortSpec("dict_out", "dict", "字典对象（map 组写动作后送出）"),
        PortSpec("value_out", "message", "取到的值（list_get / map_get）"),
        PortSpec("flag", "message", "是否存在（true/false）"),
        PortSpec("count", "message", "长度"),
    ],
    fields=[
        ConfigField("action", "动作", default="list_append", options=DS_CONTAINER_ACTION_ORDER),
        ConfigField("scope", "作用域", default="workflow", options=DS_CONTAINER_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("item", "追加的元素"),
        ConfigField("index", "下标"),
        ConfigField("field", "键名"),
        ConfigField("value", "写入的值"),
        ConfigField("default", "取不到时的默认值"),
    ],
    validator=validate_ds_container_node,
)
async def exec_ds_container(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """按 ``action`` 读写缓存里的一份 list / map 容器变量。"""
    action = str(node.config.get("action", "")).strip() or "list_append"
    scope = str(node.config.get("scope", "")).strip() or "workflow"
    if action not in DS_CONTAINER_ACTION_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图（同 http.method 口径）
        raise ValueError(f"节点 {node.id} 的动作不合法：{action!r}")
    if scope not in DS_CONTAINER_SCOPE_ORDER:
        raise ValueError(f"节点 {node.id} 的作用域不合法：{scope!r}")

    key = str(input_value(node, ctx, "key", default="")).strip()
    if not key:
        raise ValueError(f"节点 {node.id} 的 key 为空（入口没接线，config 里也没填）")
    if ":" in key:
        raise ValueError(
            f"节点 {node.id} 的变量名 {key!r} 不能包含冒号"
            "手填或上游送来的都不行）"
        )

    full_key = cache_key(node, ctx, scope, key)
    is_map = action in MAP_ACTIONS

    if is_map:
        data: Any = await _load_dict(node, ctx, full_key)
    else:
        data = await _load_list(node, ctx, full_key)

    if action == "list_append":
        item = _text(input_value(node, ctx, "item", default=""))
        data.append(item)
        await ctx.cache.set_json(full_key, data)
        _log(ctx, node, f"list_append {full_key} <- {_clip(item)}")
        return {"list_out": data}

    if action == "list_get":
        idx = _parse_index(node, ctx, len(data))
        if 0 <= idx < len(data):
            value = data[idx]
            _log(ctx, node, f"list_get {full_key}[{idx}] -> {_clip(str(value))}")
        else:
            value = _text(input_value(node, ctx, "default", default=""))
            _log(ctx, node, f"list_get {full_key}[{idx}] -> (越界，用默认值) {_clip(value)}")
        return {"value_out": str(value)}

    if action == "list_contains":
        item = _text(input_value(node, ctx, "item", default=""))
        found = item in data
        _log(ctx, node, f"list_contains {full_key}[{_clip(item)}] -> {found}")
        return {"flag": "true" if found else "false"}

    if action == "list_remove":
        idx = _parse_index(node, ctx, len(data))
        if 0 <= idx < len(data):
            gone = data.pop(idx)
            await ctx.cache.set_json(full_key, data)
            _log(ctx, node, f"list_remove {full_key}[{idx}] -> 删了 {_clip(str(gone))}")
        else:
            _log(ctx, node, f"list_remove {full_key}[{idx}] -> 越界，没删")
        return {"list_out": data}

    if action == "list_length":
        _log(ctx, node, f"list_length {full_key} -> {len(data)}")
        return {"count": str(len(data))}

    if action == "map_set":
        field = _required_field(node, ctx)
        value = _text(input_value(node, ctx, "value", default=""))
        data[field] = value
        await ctx.cache.set_json(full_key, data)
        _log(ctx, node, f"map_set {full_key}[{field}] = {_clip(value)}")
        return {"dict_out": data}

    if action == "map_get":
        field = _required_field(node, ctx)
        value = data.get(field)
        if value is None:
            value = _text(input_value(node, ctx, "default", default=""))
            _log(ctx, node, f"map_get {full_key}[{field}] -> (没有，用默认值) {_clip(value)}")
        else:
            _log(ctx, node, f"map_get {full_key}[{field}] -> {_clip(str(value))}")
        return {"value_out": str(value)}

    if action == "map_contains":
        field = _required_field(node, ctx)
        found = field in data
        _log(ctx, node, f"map_contains {full_key}[{field}] -> {found}")
        return {"flag": "true" if found else "false"}

    if action == "map_remove":
        field = _required_field(node, ctx)
        gone = data.pop(field, None)
        await ctx.cache.set_json(full_key, data)
        _log(ctx, node, f"map_remove {full_key}[{field}] -> {'删了' if gone is not None else '本来就没有'}")
        return {"dict_out": data}

    if action == "map_keys":
        keys = list(data.keys())
        _log(ctx, node, f"map_keys {full_key} -> {len(keys)} 个")
        return {"list_out": keys}

    if action == "map_length":
        _log(ctx, node, f"map_length {full_key} -> {len(data)}")
        return {"count": str(len(data))}

    raise ValueError(f"节点 {node.id} 的动作不合法：{action!r}")
