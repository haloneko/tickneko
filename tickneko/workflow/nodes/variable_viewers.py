"""显式注册的基础查看器；节点可复用或覆写，见 docs/variables/variables.md。"""

from __future__ import annotations

import json
from typing import Any

from .registry import register_variable_viewer
from .variable_viewer import VariableContext, VariableView, VariableViewer


def _invalid_constant(value: str) -> None:
    raise ValueError(f"非法 JSON 数字：{value}")


class StringViewer(VariableViewer):
    view_type = "str"
    priority = 0
    storage_types = ("string",)

    @classmethod
    async def probe(cls, context: VariableContext) -> bool:
        return await context.cache.type(context.full_key) == "string"

    def encode(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value 必须是字符串")  # noqa: TRY004 查看器统一校验口径。
        return value

    async def view(self) -> VariableView:
        data = await self.context.cache.get(self.context.full_key)
        return self.result("" if data is None else data)

    async def add(self, /, **params: Any) -> None:
        self.validate_params(params, {"value"})
        value = self.encode(params["value"])
        await self.context.cache.set(
            self.context.full_key, value, ttl=await self.write_ttl()
        )


class JsonViewer(StringViewer):
    view_type = "json"
    priority = 10

    @classmethod
    async def probe(cls, context: VariableContext) -> bool:
        if not await StringViewer.probe(context):
            return False
        value = await context.cache.get(context.full_key)
        if value is None:
            return False
        try:
            json.loads(value, parse_constant=_invalid_constant)
        except ValueError:
            return False
        return True

    def encode(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"value 必须是合法 JSON：{exc}") from exc

    async def view(self) -> VariableView:
        value = await self.context.cache.get(self.context.full_key)
        return self.result(
            None
            if value is None
            else json.loads(value, parse_constant=_invalid_constant)
        )


class ListViewer(VariableViewer):
    view_type = "list"
    priority = 0
    storage_types = ("list",)
    # 无来源 / 无键族证据的 list 可能是缺少辅助键的 ds-list，禁止通用写入。
    probe_editable = False

    @classmethod
    async def probe(cls, context: VariableContext) -> bool:
        return await context.cache.type(context.full_key) == "list"

    def encode(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("列表元素必须是字符串")  # noqa: TRY004
        return value

    def items_from(self, params: dict[str, Any]) -> list[str]:
        self.validate_params(params, {"item"}, {"items"})
        if "item" in params:
            return [self.encode(params["item"])]
        items = params["items"]
        if not isinstance(items, list):
            raise ValueError("items 必须是数组")  # noqa: TRY004
        return [self.encode(item) for item in items]

    async def view(self) -> VariableView:
        cache, key = self.context.cache, self.context.full_key
        data = await cache.list_range(key)
        return self.result(data, length=await cache.list_length(key))

    async def add(self, /, **params: Any) -> None:
        items = self.items_from(params)  # 整份校验和编码完成后才允许写入。
        cache, key = self.context.cache, self.context.full_key
        ttl = await self.write_ttl()
        if "items" in params:
            await cache.delete(key)
        if items:
            await cache.list_push_right(key, *items, ttl=ttl)


class DictViewer(VariableViewer):
    view_type = "dict"
    priority = 0
    storage_types = ("hash",)

    @classmethod
    async def probe(cls, context: VariableContext) -> bool:
        return await context.cache.type(context.full_key) == "hash"

    def encode(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("字典的值必须是字符串")  # noqa: TRY004
        return value

    def fields_from(self, params: dict[str, Any]) -> dict[str, str]:
        self.validate_params(params, {"field", "value"}, {"fields"})
        if "fields" in params:
            fields = params["fields"]
            if not isinstance(fields, dict):
                raise ValueError("fields 必须是对象")
        else:
            field = params["field"]
            if not isinstance(field, str) or not field:
                raise ValueError("field 必须是非空字符串")
            fields = {field: params["value"]}
        if any(not isinstance(field, str) or not field for field in fields):
            raise ValueError("字典字段名必须是非空字符串")
        return {field: self.encode(value) for field, value in fields.items()}

    async def view(self) -> VariableView:
        return self.result(await self.context.cache.hash_get_all(self.context.full_key))

    async def add(self, /, **params: Any) -> None:
        fields = self.fields_from(params)
        cache, key = self.context.cache, self.context.full_key
        ttl = await self.write_ttl()
        if "fields" in params:
            await cache.delete(key)
        if fields:
            await cache.hash_set(key, fields, ttl=ttl)


for _viewer in (StringViewer, JsonViewer, ListViewer, DictViewer):
    register_variable_viewer(_viewer)
