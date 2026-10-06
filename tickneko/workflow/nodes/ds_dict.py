"""数据结构字典节点：把缓存里的一份字典变量当成可操作的结构 —— new / set / get / contains / remove / keys / length。

和 cache 节点一样，值存在作用域化缓存里（``scope`` + ``key`` 定位，键规则**完全同 cache 节点**），
但它存取的不是文本，而是**任意 JSON 值**（``ctx.cache.set_json`` / ``get_json``）—— 所以叫「数据结构」。
一次执行改的是缓存里那份字典，下一趟还能接着改（跨执行持久化）；写动作（``new`` /
``set`` / ``remove``）把字典**对象**原样从 ``dict_out``（dict 端口）送出，要落文本给下游用
message 端口接 ``get`` 取出的具体值。

config:
    action:  动作（缺省 ``new``）：``new`` 建一份；``set`` / ``get`` / ``contains`` / ``remove``
             按 ``field`` 键名操作；``keys`` 取键列表；``length`` 取条目数
    scope:   作用域（缺省 ``workflow``）：``workflow`` = 图级；``account`` = 账号级
    key:     变量名（必填：接线或手填）
    items:   初值（``new`` 用；接 ``dict`` 端口原样投递，手填兜底是 JSON 文本）
    field:   键名（``set`` / ``get`` / ``contains`` / ``remove`` 用；接线或手填）
    value:   写入的值（``set`` 用；手填兜底，也能接线）
    default: 键不存在时的默认值（``get`` 用）

端口（数据沿连线走）：

    ``key``        数据入口：变量名（也能从上游来）；
    ``items``      数据入口：``new`` 的初值（dict 端口，值沿边原样投递）；
    ``field``      数据入口：键名；
    ``value``      数据入口：``set`` 写入的值；
    ``default``    数据入口：``get`` 键不存在时用的默认值；
    ``dict_out``   出口：字典**对象**（``new``/``set``/``remove`` 后原样沿 dict 端口送出）；
    ``value_out``  出口：取到的值（``get``，message 端口）；
    ``flag``       出口：``contains`` 的结果（文本 ``true`` / ``false``）；
    ``count``      出口：``length`` 的条目数（文本）；
    ``list_out``   出口：``keys`` 的键列表（list 端口，原样投递）。

缓存键用前缀区分作用域（同 cache 节点，互不打扰）::

    workflow   workflow:graph:{图 id}:{变量名}
    account    workflow:acct:{账号 id}:{变量名}

口径：

* 值按**文本**存（``set`` 线上送整数会转成字符串）—— 和 cache 节点一致；
* ``get`` 键不存在**不算事故**：送 ``default``（没填就是空串），流程继续；
* ``field``（键名）为空当场抛（不知道操作哪个键，同 cache 的 ``key``）；
* **变量名不允许冒号**（同 cache 节点）；``key`` 为空当场抛；
* 变量里存的不是字典（比如被别的节点写成 list）→ 本节点**业务失败**（``NodeFailure``），
  下游跳过、流程继续 —— 不是环境事故；
* 缓存后端不可用会抛 ``CacheError``（环境问题不吞不掩，同 cache 节点）。

小抄::

    建配置:      ds-dict(new, key=配置, items 接字典) -> 之后 get 配置[主题色] 随手取
    攒用户数据:  ds-dict(set, key=档案, field 接 用户id, value 接 内容) 每趟追加一条
    查开关:      ds-dict(contains, key=配置, field=静音) -> condition(== true)
    变量互通:    cache.get 只读文本、ds-dict 读写结构 —— 键规则一样，同一份缓存
"""
from __future__ import annotations

import json
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

#: 允许的动作，**顺序即画布下拉顺序**
DS_DICT_ACTION_ORDER: tuple[str, ...] = ("new", "set", "get", "contains", "remove", "keys", "length")

#: 允许的作用域，**顺序即画布下拉顺序**
DS_DICT_SCOPE_ORDER: tuple[str, ...] = ("workflow", "account")

#: 日志里展示键 / 值时的截断长度（长值不刷屏）
CLIP_CHARS: int = 80


def validate_ds_dict_node(node: WorkflowNode) -> list[ValidationIssue]:
    """防呆：动作 / 作用域枚举、手填变量名不带冒号；``key`` 的「必填」由入口管。"""
    issues: list[ValidationIssue] = []

    action = node.config.get("action")
    if isinstance(action, str) and action.strip() and action.strip() not in DS_DICT_ACTION_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_DICT_ACTION",
                message=f"ds-dict 节点 {node.id} 的动作 {action!r} 不合法",
                suggestion=f"可选：{' / '.join(DS_DICT_ACTION_ORDER)}",
            )
        )

    scope = node.config.get("scope")
    if isinstance(scope, str) and scope.strip() and scope.strip() not in DS_DICT_SCOPE_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_DICT_SCOPE",
                message=f"ds-dict 节点 {node.id} 的作用域 {scope!r} 不合法",
                suggestion=f"可选：{' / '.join(DS_DICT_SCOPE_ORDER)}",
            )
        )

    # 手填变量名带冒号 = 保存时就能拦住（接线的值运行时再兜一道，见 exec_ds_dict）
    key = node.config.get("key")
    if isinstance(key, str) and ":" in key:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_DS_DICT_KEY",
                message=f"ds-dict 节点 {node.id} 的变量名 {key!r} 不能包含冒号",
                suggestion="变量名不允许有:",
            )
        )

    return issues


def _clip(raw: str) -> str:
    """日志里展示键 / 值：太长截断（避免长值刷屏）。"""
    return raw if len(raw) <= CLIP_CHARS else raw[:CLIP_CHARS] + "…"


def _log(ctx: NodeExecutionContext, node: WorkflowNode, msg: str) -> None:
    ctx.logger.info(f"[ds-dict:{node.id}] {msg}")
    ctx.log.append(f"[ds-dict] {node.id}: {msg}")


def _text(raw: object) -> str:
    return "" if raw is None else str(raw)


def _required_field(node: WorkflowNode, ctx: NodeExecutionContext) -> str:
    """键名：接线或手填，没有就当场抛（不知道操作哪个键）。"""
    field = _text(input_value(node, ctx, "field", default=""))
    if not field:
        raise ValueError(f"节点 {node.id} 的 field（键名）为空（入口没接线，config 里也没填）")
    return field


async def _load_dict(
    node: WorkflowNode, ctx: NodeExecutionContext, full_key: str
) -> dict[str, str]:
    """读缓存里的字典：没存过当空字典；存的是别的结构 → 业务失败（下游跳过，流程继续）。"""
    raw = await ctx.cache.get_json(full_key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise NodeFailure(f"节点 {node.id} 的变量 {full_key} 不是字典（是 {type(raw).__name__}）")
    return raw


def _items_to_dict(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, str]:
    """``new`` 的初值：接 dict 端口原样投递；手填兜底是 JSON 文本；没有就是空字典。"""
    raw = input_value(node, ctx, "items", default="")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"节点 {node.id} 的 items 不是合法 JSON：{_clip(raw)}")
        if not isinstance(parsed, dict):
            raise ValueError(f"节点 {node.id} 的 items 要的是 JSON 对象：{_clip(raw)}")
        return parsed
    return {}


@register_node(
    "ds-dict",
    label="数据结构字典",
    color="#0891b2",
    order=150,
    category="data",
    # key 既是字段名也是数据入口（同 cache.key）：接线或手填都行，两个都没有才报错
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名", required=True),
        PortSpec("items", "dict", "初值（new 用）"),
        PortSpec("field", "message", "键名"),
        PortSpec("value", "message", "写入的值"),
        PortSpec("default", "message", "键不存在时的默认值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("dict_out", "dict", "字典对象（new/set/remove 后送出）"),
        PortSpec("value_out", "message", "取到的值（get）"),
        PortSpec("flag", "message", "是否存在（true/false）"),
        PortSpec("count", "message", "条目数"),
        PortSpec("list_out", "list", "键列表"),
    ],
    fields=[
        ConfigField("action", "动作", default="new", options=DS_DICT_ACTION_ORDER),
        ConfigField("scope", "作用域", default="workflow", options=DS_DICT_SCOPE_ORDER),
        ConfigField("key", "变量名"),
        ConfigField("items", "初值（JSON 文本）"),
        ConfigField("field", "键名"),
        ConfigField("value", "值"),
        ConfigField("default", "键不存在时的默认值"),
    ],
    validator=validate_ds_dict_node,
)
async def exec_ds_dict(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """按 ``action`` 读写缓存里的一份字典变量。"""
    action = str(node.config.get("action", "")).strip() or "new"
    scope = str(node.config.get("scope", "")).strip() or "workflow"
    if action not in DS_DICT_ACTION_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图（同 http.method 口径）
        raise ValueError(f"节点 {node.id} 的动作不合法：{action!r}")
    if scope not in DS_DICT_SCOPE_ORDER:
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

    if action == "new":
        data = _items_to_dict(node, ctx)
        await ctx.cache.set_json(full_key, data)
        _log(ctx, node, f"new {full_key} <- {_clip(json.dumps(data, ensure_ascii=False))}")
        return {"dict_out": data}

    data = await _load_dict(node, ctx, full_key)

    if action == "set":
        field = _required_field(node, ctx)
        value = _text(input_value(node, ctx, "value", default=""))
        data[field] = value
        await ctx.cache.set_json(full_key, data)
        _log(ctx, node, f"set {full_key}[{field}] = {_clip(value)}")
        return {"dict_out": data}

    if action == "get":
        field = _required_field(node, ctx)
        value = data.get(field)
        if value is None:
            # 键不存在不算事故：有默认值就用默认值（手填兜底 / 也能接线），没有才送空串
            raw_default = input_value(node, ctx, "default", default="")
            value = _text(raw_default)
            _log(ctx, node, f"get {full_key}[{field}] -> (没有，用默认值) {_clip(value)}")
        else:
            _log(ctx, node, f"get {full_key}[{field}] -> {_clip(str(value))}")
        return {"value_out": str(value)}

    if action == "contains":
        field = _required_field(node, ctx)
        found = field in data
        _log(ctx, node, f"contains {full_key}[{field}] -> {found}")
        return {"flag": "true" if found else "false"}

    if action == "remove":
        field = _required_field(node, ctx)
        gone = data.pop(field, None)
        await ctx.cache.set_json(full_key, data)
        _log(ctx, node, f"remove {full_key}[{field}] -> {'删了' if gone is not None else '本来就没有'}")
        return {"dict_out": data}

    if action == "keys":
        keys = list(data.keys())
        _log(ctx, node, f"keys {full_key} -> {len(keys)} 个")
        return {"list_out": keys}

    if action == "length":
        _log(ctx, node, f"length {full_key} -> {len(data)}")
        return {"count": str(len(data))}

    raise ValueError(f"节点 {node.id} 的动作不合法：{action!r}")
