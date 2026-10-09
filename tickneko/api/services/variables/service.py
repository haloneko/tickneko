"""变量发现、来源反查与只读探测；不解释存储值，见 docs/variables/variables.md。"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Protocol

from tickneko.core.cache.interfaces import CacheBackend
from tickneko.workflow.models import (
    WorkflowDefinitionRecord,
    WorkflowNode,
    WorkflowVersionRecord,
)
from tickneko.workflow.nodes.registry import get_spec, registered_variable_viewers
from tickneko.workflow.nodes.variable_viewer import (
    VariableContext,
    VariableRule,
    VariableView,
    VariableViewer,
)


class VariableStore(Protocol):
    """只依赖读取定义与版本；不把工作流存储实现或 HTTP 引入分派。"""

    async def get(self, workflow_id: str) -> WorkflowDefinitionRecord | None: ...

    async def get_version(
        self, workflow_id: str, version: int
    ) -> WorkflowVersionRecord | None: ...


class VariableNotFound(LookupError):
    """本体不存在且没有有效的来源声明。"""


class VariableNotEditable(RuntimeError):
    """来源未开放查看器、规则冲突或无法安全确定写入语义。"""


@dataclass(frozen=True)
class VariableSnapshot:
    context: VariableContext
    view: VariableView
    ttl: float | None


@dataclass(frozen=True)
class _Resolution:
    viewer: VariableViewer | None
    reason: str = ""


def parse_variable_key(raw: str) -> tuple[str, str, str] | None:
    """缓存门面已剥去 namespace；先解作用域，再过滤含冒号的辅助变量名。"""
    parts = raw.split(":", 3)
    scopes = {"graph": "graph", "acct": "account"}
    if len(parts) != 4 or parts[0] != "workflow" or parts[1] not in scopes:
        return None
    if not parts[2] or not parts[3] or ":" in parts[3]:
        return None
    return scopes[parts[1]], parts[2], parts[3]


class VariableService:
    def __init__(self, cache: CacheBackend, store: VariableStore) -> None:
        self.cache = cache
        self.store = store
        self._records: dict[str, WorkflowDefinitionRecord | None] = {}
        self._nodes: dict[str, list[WorkflowNode] | None] = {}

    async def _record(self, workflow_id: str) -> WorkflowDefinitionRecord | None:
        if workflow_id not in self._records:
            self._records[workflow_id] = await self.store.get(workflow_id)
        return self._records[workflow_id]

    async def context(
        self, *, scope: str, key: str, owner_id: str = "", workflow_id: str = ""
    ) -> VariableContext:
        if scope not in ("graph", "account"):
            raise ValueError("作用域要 graph/account 之一")
        if not key or ":" in key:
            raise ValueError("变量名必须非空且不能包含冒号")
        if scope == "account":
            if not owner_id or ":" in owner_id or workflow_id:
                raise ValueError("账号级变量需要合法 owner_id，不能指定 workflow_id")
            return VariableContext(self.cache, "account", owner_id, "", key)
        if not workflow_id or ":" in workflow_id:
            raise ValueError("图级变量需要合法 workflow_id")
        record = await self._record(workflow_id)
        return VariableContext(
            self.cache, "graph", record.owner_id if record else "", workflow_id, key
        )

    async def discover(
        self, *, scope: str | None, keyword: str, owner: str | None
    ) -> list[VariableContext]:
        rows: dict[tuple[str, str, str], VariableContext] = {}
        for raw in await self.cache.keys("workflow:*"):
            parsed = parse_variable_key(raw)
            if parsed is None:
                continue
            item_scope, ident, name = parsed
            if (scope is not None and scope != item_scope) or (
                keyword and keyword not in name
            ):
                continue
            context = await self.context(
                scope=item_scope,
                key=name,
                owner_id=ident if item_scope == "account" else "",
                workflow_id=ident if item_scope == "graph" else "",
            )
            if owner is None or context.owner_id == owner:
                rows[parsed] = context
        return sorted(
            rows.values(),
            key=lambda row: (row.scope, row.owner_id, row.workflow_id, row.key),
        )

    async def _source_nodes(
        self, context: VariableContext
    ) -> list[WorkflowNode] | None:
        if context.scope == "account":
            return None
        ident = context.workflow_id
        if ident in self._nodes:
            return self._nodes[ident]
        record = await self._record(ident)
        nodes: list[WorkflowNode] | None = None
        if record is not None:
            # 运行时使用已发布快照；未发布时依次检查提交版本、暂存区。
            version_number = record.published_version or record.current_version
            if version_number:
                version = await self.store.get_version(ident, version_number)
                if version is not None:
                    try:
                        nodes = version.graph().nodes
                    except ValueError:
                        pass  # 无法反查，不把不存在 / 已失效的来源变成 500。
            elif record.draft_graph_json:
                try:
                    graph = record.draft_graph()
                    if graph is not None:
                        nodes = [
                            WorkflowNode.model_validate(node.model_dump())
                            for node in graph.nodes
                        ]
                except ValueError:
                    pass
        self._nodes[ident] = nodes
        return nodes

    async def _probes(self, context: VariableContext) -> list[type[VariableViewer]]:
        return [
            viewer
            for viewer in registered_variable_viewers()
            if await viewer.probe(context)
        ]

    async def _choose(
        self,
        context: VariableContext,
        viewers: dict[type[VariableViewer], VariableContext],
        *,
        probed: bool,
    ) -> _Resolution:
        priority = max(viewer.priority for viewer in viewers)
        top = [viewer for viewer in viewers if viewer.priority == priority]
        if len(top) > 1:
            reason = "多个变量查看器的规则冲突，不能确定安全的修改方式"
            # 用唯一的低优先级探测结果只读展示；不按模块导入顺序挑写入者。
            lower = {
                viewer: context
                for viewer in await self._probes(context)
                if viewer.priority < priority
            }
            if lower:
                resolution = await self._choose(context, lower, probed=True)
                if resolution.viewer is not None:
                    resolution.viewer.editable = False
                    resolution.viewer.reason = reason
                    return _Resolution(resolution.viewer, reason)
            return _Resolution(None, reason)
        viewer_type = top[0]
        bound = viewers[viewer_type]
        if not await viewer_type.accepts(bound):
            return _Resolution(None, "变量本体与来源节点声明的存储类型不一致")
        editable = not probed or viewer_type.probe_editable
        reason = (
            "来源已丢失，无法确定列表的键族联动规则，只允许查看" if not editable else ""
        )
        return _Resolution(viewer_type(bound, editable=editable, reason=reason), reason)

    async def resolve(self, context: VariableContext) -> _Resolution:
        nodes = await self._source_nodes(context)
        if nodes is not None:
            candidates: dict[type[VariableViewer], VariableContext] = {}
            for node in sorted(nodes, key=lambda node: node.id):
                spec = get_spec(node.type)
                if spec is None:
                    continue  # 扩展模块已卸载 / 节点类型失效：走真实 probe。
                bound = replace(context, node=node)
                if spec.variable_viewer is None:
                    if VariableRule().matches(bound):
                        return _Resolution(None, "来源节点未开放变量查看器")
                elif spec.variable_viewer.match(bound):
                    candidates.setdefault(spec.variable_viewer, bound)
            if candidates:
                return await self._choose(context, candidates, probed=False)
        probes = {viewer: context for viewer in await self._probes(context)}
        if probes:
            return await self._choose(context, probes, probed=True)
        return _Resolution(None, "没有能处理该变量的查看器")

    async def _snapshot(
        self, context: VariableContext, view: VariableView
    ) -> VariableSnapshot:
        ttl = await self.cache.ttl(context.full_key)
        return VariableSnapshot(
            context, view, ttl if ttl is not None and math.isfinite(ttl) else None
        )

    async def view(self, context: VariableContext) -> VariableSnapshot:
        resolution = await self.resolve(context)
        if resolution.viewer is None:
            if not await self.cache.exists(context.full_key):
                raise VariableNotFound("变量不存在")
            view = VariableView(None, reason=resolution.reason)
        else:
            view = await resolution.viewer.view()
        return await self._snapshot(context, view)

    async def page(
        self,
        *,
        scope: str | None,
        keyword: str,
        owner: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[VariableSnapshot], int]:
        rows = await self.discover(scope=scope, keyword=keyword, owner=owner)
        snapshots: list[VariableSnapshot] = []
        for row in rows[offset : offset + limit]:
            try:
                snapshots.append(await self.view(row))
            except VariableNotFound:
                pass  # 枚举后过期的本体不把整页变成 404；总数是本次发现时的快照。
        return snapshots, len(rows)

    async def add(
        self, context: VariableContext, params: dict[str, Any]
    ) -> VariableSnapshot:
        resolution = await self.resolve(context)
        viewer = resolution.viewer
        if viewer is None or not viewer.editable:
            if viewer is None and not await self.cache.exists(context.full_key):
                raise VariableNotFound("变量不存在")
            raise VariableNotEditable(resolution.reason or "该变量只允许查看")
        await viewer.add(**params)
        return await self._snapshot(context, await viewer.view())
