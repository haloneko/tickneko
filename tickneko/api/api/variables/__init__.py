"""变量查看入口：``GET <prefix>/variables``（列出工作流「缓存」节点写下的变量）。

只读：这一组接口不改 / 不删任何变量，仅按登录身份把可见范围内的变量列出来。
"""
from __future__ import annotations

from .router import router

__all__ = ["router"]
