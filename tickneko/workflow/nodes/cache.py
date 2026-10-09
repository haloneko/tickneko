"""缓存节点：把值存进缓存 / 取回来 —— 变量跨执行（跨工作流）传递的通道。

数据沿边走的模型里**每次执行都是全新一趟**：上游送什么就处理什么、跑完就散。想在两次
执行之间记住点什么（「上次处理到哪」「这个群开没开提醒」「攒了几条」），就靠这个节点把
值写进缓存、下一趟取得回来。

config:
    action:  动作（缺省 ``get``）：``get`` = 读一个键；``set`` = 写一个键
    scope:   作用域（缺省 ``workflow``）：``workflow`` = 图级（只有这张图看得见）；
             ``account`` = 账号级（同一账号的工作流共享）
    key:     变量名（必填：接线或手填）
    value:   写入的值（``set`` 用；手填兜底，也能接线）
    default: 读不到时的默认值（``get`` 用；手填兜底，也能接线）

端口（数据沿连线走）：

    ``key``           数据入口：变量名（也能从上游来 —— 按消息内容动态挑槽位）；
    ``value``         数据入口：要写的值（``now`` / ``operator`` 的输出接过来都行）；
    ``default``       数据入口：``get`` 读不到时用的默认值（手填兜底，也能接线）；
    ``cache_value``   出口：``get`` 取到的值 / ``set`` 写下去的值（读不到就是默认值，没填才是空串）。

缓存键**用前缀区分作用域**（同一个缓存里互不打扰）::

    workflow   workflow:graph:{图 id}:{变量名}      例  workflow:graph:3ab9c2…:计数
    account    workflow:acct:{账号 id}:{变量名}     例  workflow:acct:u-admin:日签开关

口径：

* 值按**文本**存（线上送整数会转成字符串）—— 这是缓存层的约定（``tickneko.core.cache``，
  内存与 Redis 两个后端一致）；
* ``get`` 没取到（键还没存过）**不算事故**：往 ``cache_value`` 送 ``default`` 默认值
  （没填就是空串），流程继续；
* ``set`` 空值 = 把变量清成空串（「清空」是合法操作，不是错误）；
* ``key`` 为空（没接线也没手填）当场抛（同 ``http.url``：不知道操作哪个键）；
* **键名不允许冒号**（``key`` 手填 / 接线 / 账号级归属 id 都拦）—— 冒号是键的分段分隔符，
  带进去会把键从中间劈开、造成歧义；账号 id 正常由 ``u-<账号>`` 生成，天然没有冒号；
* 缓存后端不可用（没 ``start()`` / Redis 掉了且没降级）会抛 ``CacheError``：环境问题
  不吞不掩 —— 正式跑由主程序启动缓存（``bootstrap`` 已经做了），离线测试给 ctx 注入
  自己的门面即可（见 ``NodeExecutionContext.cache``）。

小抄::

    每趟 +1:   get 计数 -> operator(+) 1 -> set 计数
    初值:      get 计数（默认值填 1）—— 第一趟还没存过也能往下走
    开关:      get 日签开关 -> condition(== 开) …
    跨图传值:  scope 选 account —— 另一个工作流 get 同一个键就拿到了
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import (
    TRIGGER_PORT,
    ConfigField,
    NodeExecutionContext,
    PortSpec,
    cache_key,
    input_value,
)
from .registry import register_node
from .variable_viewer import VariableContext, VariableRule, text_parameter
from .variable_viewers import StringViewer

#: 允许的动作，**顺序即画布下拉顺序**
CACHE_ACTION_ORDER: tuple[str, ...] = ("get", "set")

#: 允许的作用域，**顺序即画布下拉顺序**
CACHE_SCOPE_ORDER: tuple[str, ...] = ("workflow", "account")

#: 日志里展示键 / 值时的截断长度（长值不刷屏）
CLIP_CHARS: int = 80


def validate_cache_node(node: WorkflowNode) -> list[ValidationIssue]:
    """防呆：动作 / 作用域枚举、手填变量名不带冒号；``key`` 的「必填」由入口管。"""
    issues: list[ValidationIssue] = []

    action = node.config.get("action")
    if isinstance(action, str) and action.strip() and action.strip() not in CACHE_ACTION_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_CACHE_ACTION",
                message=f"cache 节点 {node.id} 的动作 {action!r} 不合法",
                suggestion=f"可选：{' / '.join(CACHE_ACTION_ORDER)}",
            )
        )

    scope = node.config.get("scope")
    if isinstance(scope, str) and scope.strip() and scope.strip() not in CACHE_SCOPE_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_CACHE_SCOPE",
                message=f"cache 节点 {node.id} 的作用域 {scope!r} 不合法",
                suggestion=f"可选：{' / '.join(CACHE_SCOPE_ORDER)}",
            )
        )

    # 手填变量名带冒号 = 保存时就能拦住（接线的值运行时再兜一道，见 exec_cache）
    key = node.config.get("key")
    if isinstance(key, str) and ":" in key:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_CACHE_KEY",
                message=f"cache 节点 {node.id} 的变量名 {key!r} 不能包含冒号",
                suggestion="变量名不允许有:",
            )
        )

    return issues


def _clip(raw: str) -> str:
    """日志里展示键 / 值：太长截断（避免长值刷屏）。"""
    return raw if len(raw) <= CLIP_CHARS else raw[:CLIP_CHARS] + "…"


class CacheViewer(StringViewer):
    """cache 的写动作注册文本查看器，保留运行时的标量编码。"""

    priority = 100
    rules = (VariableRule(actions=("set",)),)

    @classmethod
    async def probe(cls, context: VariableContext) -> bool:
        return False  # 来源丢失后由显式注册的 str / json 基础查看器探测。

    def encode(self, value: Any) -> str:
        return text_parameter(value)


@register_node(
    "cache",
    label="缓存",
    color="#06b6d4",
    order=140,
    category="data",
    # key 既是字段名也是数据入口（同 http.url）：接线或手填都行，两个都没有才报错
    inputs=[
        TRIGGER_PORT,
        PortSpec("key", "message", "变量名", required=True),
        PortSpec("value", "message", "写入的值"),
        PortSpec("default", "message", "读不到时的默认值"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("cache_value", "message", "取到 / 写入的值"),
    ],
    fields=[
        ConfigField("action", "动作", default="get", options=CACHE_ACTION_ORDER),
        ConfigField("scope", "作用域", default="workflow", options=CACHE_SCOPE_ORDER),
        # key 的「必填」由入口（PortSpec.required）管；这里只声明手填兜底
        ConfigField("key", "变量名"),
        ConfigField("value", "写入的值"),
        ConfigField("default", "默认值"),
    ],
    validator=validate_cache_node,
    variable_viewer=CacheViewer,
)
async def exec_cache(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """按 ``action`` 读 / 写一个键，产出 ``cache_value``。"""
    action = str(node.config.get("action", "")).strip() or "get"
    scope = str(node.config.get("scope", "")).strip() or "workflow"
    if action not in CACHE_ACTION_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图（同 http.method 口径）
        raise ValueError(f"节点 {node.id} 的动作不合法：{action!r}")
    if scope not in CACHE_SCOPE_ORDER:
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

    if action == "set":
        raw = input_value(node, ctx, "value", default="")
        text = "" if raw is None else str(raw)
        await ctx.cache.set(full_key, text)
        ctx.logger.info(f"[cache:{node.id}] set {full_key} = {_clip(text)}")
        ctx.log.append(f"[cache] {node.id}: set {full_key} = {_clip(text)}")
        return {"cache_value": text}

    value = await ctx.cache.get(full_key)
    if value is None:
        # 没存过不算事故：有默认值就用默认值（手填兜底 / 也能接线），没有才送空串
        raw_default = input_value(node, ctx, "default", default="")
        fallback = "" if raw_default is None else str(raw_default)
        if fallback:
            ctx.logger.info(f"[cache:{node.id}] {full_key} 还没存过 -> 用默认值 {_clip(fallback)}")
            ctx.log.append(f"[cache] {node.id}: get {full_key} -> (还没存过，用默认值) {_clip(fallback)}")
            return {"cache_value": fallback}
        ctx.logger.info(f"[cache:{node.id}] {full_key} 还没存过")
        ctx.log.append(f"[cache] {node.id}: get {full_key} -> (还没存过)")
        return {"cache_value": ""}
    ctx.logger.info(f"[cache:{node.id}] get {full_key} -> {len(value)} 字符")
    ctx.log.append(f"[cache] {node.id}: get {full_key} -> {_clip(value)}")
    return {"cache_value": value}
