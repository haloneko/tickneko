"""变量查看路由的注入件：取缓存门面。

变量住在缓存里（``tickneko.core.cache``），键由 cache 节点定，形如
``workflow:graph:{图 id}:{变量名}`` / ``workflow:acct:{账号 id}:{变量名}``
（口径见 ``docs/cache/cache.md``）。

装配时主程序把**进程级**缓存门面挂到 ``app.state.cache`` 上（同一个门面就是业务在用的那份，
见 :func:`tickneko.bootstrap.run`）；没挂的场合（直接 ``create_app`` 的测试 / 示例）回 **503
并说清楚** —— 「没接入」和「出错了」对排障是两回事。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request, status

from tickneko.core.cache import Cache

from ...common.errors import ApiError, ErrorCode


class _AppState(Protocol):
    """挂在 ``app.state`` 上、本模块要用到的东西（由 :func:`tickneko.api.create_app` 写入）。"""

    #: 缓存门面；``None`` = 没装配（见 :func:`get_cache`）
    cache: Cache | None


class _App(Protocol):
    """FastAPI 的 ``app``，这里只关心它身上的 ``state``。"""

    state: _AppState


def get_cache(request: Request) -> Cache:
    """取缓存门面：装配时挂在 ``app.state.cache`` 上。

    没挂上就回 503：变量全在缓存里，没有它连「有多少变量」都答不了 —— 静默给一个空列表，
    查的人只会当成「真的一个变量都没有」。
    """
    app = cast("_App", request.app)
    cache = getattr(app.state, "cache", None)
    if cache is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "缓存未接入：主程序没有把缓存传给 create_app",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return cache


#: 依赖简写：路由函数里写 ``cache: CacheDep`` 即可
CacheDep = Annotated[Cache, Depends(get_cache)]


__all__ = [
    "CacheDep",
    "get_cache",
]
