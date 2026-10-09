"""节点拥有的变量查看契约；注册、探测与编辑口径见 docs/variables/variables.md。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from tickneko.core.cache.interfaces import CacheBackend

from ..models import WorkflowNode

VariableViewType = Literal["json", "list", "dict", "str"]
VariableScope = Literal["graph", "account"]


@dataclass(frozen=True)
class VariableContext:
    """经鉴权绑定的变量身份；请求参数不能覆盖作用域、连接或物理键。"""

    cache: CacheBackend
    scope: VariableScope
    owner_id: str
    workflow_id: str
    key: str
    node: WorkflowNode | None = None

    @property
    def full_key(self) -> str:
        ident = self.owner_id if self.scope == "account" else self.workflow_id
        segment = "acct" if self.scope == "account" else "graph"
        return f"workflow:{segment}:{ident}:{self.key}"


@dataclass(frozen=True)
class VariableRule:
    """变量归属规则：节点配置字段，或能在来源丢失后识别的逻辑名称模式。"""

    key_field: str = "key"
    scope_field: str = "scope"
    action_field: str = "action"
    actions: tuple[str, ...] = ()
    name_pattern: str = ""
    scopes: tuple[VariableScope, ...] = ("graph", "account")

    def matches(self, context: VariableContext) -> bool:
        if context.scope not in self.scopes:
            return False
        if self.name_pattern and re.fullmatch(self.name_pattern, context.key) is None:
            return False
        node = context.node
        if node is None:
            return bool(self.name_pattern)
        scope = str(node.config.get(self.scope_field, "")).strip() or "workflow"
        expected = "account" if context.scope == "account" else "workflow"
        name = str(node.config.get(self.key_field, "")).strip()
        if scope != expected or name != context.key:
            return False
        return not self.actions or node.config.get(self.action_field) in self.actions


@dataclass(frozen=True)
class VariableView:
    """展示类型与物理类型分离；length 由查看器从本体读取。"""

    type: VariableViewType | None
    data: Any = None
    length: int | None = None
    editable: bool = False
    reason: str = ""


class VariableViewer:
    """子类声明规则与只读 probe，实现 view / add；同优先级冲突禁止写入。"""

    view_type: ClassVar[VariableViewType] = "str"
    rules: ClassVar[tuple[VariableRule, ...]] = (VariableRule(),)
    priority: ClassVar[int] = 100
    storage_types: ClassVar[tuple[str, ...]] = ()
    probe_editable: ClassVar[bool] = True

    def __init__(
        self, context: VariableContext, *, editable: bool = True, reason: str = ""
    ) -> None:
        self.context = context
        self.editable = editable
        self.reason = reason

    @classmethod
    def match(cls, context: VariableContext) -> bool:
        return any(rule.matches(context) for rule in cls.rules)

    @classmethod
    async def probe(cls, context: VariableContext) -> bool:
        """没有来源节点时探测；不得创建、重建或修改任何键。"""
        return False

    @classmethod
    async def accepts(cls, context: VariableContext) -> bool:
        if not cls.storage_types:
            return True
        kind = await context.cache.type(context.full_key)
        return kind is None or kind in cls.storage_types

    def result(self, data: Any, *, length: int | None = None) -> VariableView:
        return VariableView(self.view_type, data, length, self.editable, self.reason)

    async def view(self) -> VariableView:
        raise NotImplementedError

    async def add(self, /, **params: Any) -> None:
        raise NotImplementedError

    def validate_params(self, params: dict[str, Any], *forms: set[str]) -> None:
        """按字段存在性校验整份请求；空值不等于缺参。"""
        if not self.editable:
            raise ValueError(self.reason or "该变量只允许查看")
        keys = set(params)
        if keys in forms:
            return
        allowed = set().union(*forms)
        unknown = keys - allowed
        if unknown:
            raise ValueError(f"不支持的参数：{', '.join(sorted(unknown))}")
        choices = " 或 ".join(" + ".join(sorted(form)) for form in forms)
        raise ValueError(f"请提交 {choices}，参数不能缺少或混用")

    async def write_ttl(self) -> float | None:
        """编辑保留已有本体的 TTL；新键沿用缓存默认值。"""
        ttl = await self.context.cache.ttl(self.context.full_key)
        return 0.0 if ttl is not None and math.isinf(ttl) else ttl


def text_parameter(value: Any) -> str:
    """文本节点的编码：沿用 None → 空串、标量 → str，拒绝嵌套结构。"""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError("文本值必须是字符串、数字、布尔值或 null")  # noqa: TRY004 查看器统一以 ValueError 返回 422。
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("数字必须是有限值")
    return str(value)
