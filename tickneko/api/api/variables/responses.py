"""
变量查看接口的响应体。
包括单个条目和一页
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class VariableData(BaseModel):
    """一个工作流变量。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 作用域：``graph`` = 图级（只有这张图看得见）；``account`` = 账号级（同账号的工作流共享）
    scope: str = Field(description="作用域：graph / account")
    #: 归属者 id：账号级 = 账号 id；图级 = 那张图的 owner；空串 = 查不到归属（图已被删）
    owner_id: str = Field(default="", description="归属 id")
    #: 所属工作流 id；账号级变量不属于某一张图，是空串
    workflow_id: str = Field(default="", description="所属工作流 id（账号级为空串）")
    #: 变量名（缓存键前缀之后的那一段）
    key: str = Field(description="变量名")
    #: 当前值（缓存层按文本存，见 docs/cache/cache.md）
    value: str = Field(default="", description="变量当前值")
    #: 剩余存活秒数；``None`` = 永不过期
    ttl: float | None = Field(default=None, description="剩余秒数；null = 永不过期")


class VariablePage(BaseModel):
    """变量列表的**一页**：本页条目 + 命中总数（前端据此算总页数、做页码跳转）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 本页的变量（按作用域 / 归属 / 所属图 / 变量名排序，同一条件两次查顺序一致）
    items: list[VariableData] = Field(description="本页变量")
    #: 命中条件的**总条数**（不受本页 ``limit`` / ``offset`` 限制）
    total: int = Field(description="命中总条数")
