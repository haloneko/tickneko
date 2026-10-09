"""变量 HTTP 入口：鉴权、完整参数转发与错误封装；见 docs/variables/variables.md。"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, Query, Request, status

from tickneko.workflow.nodes.variable_viewer import VariableContext

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode, HttpStatus
from ...common.models import ApiResponse, ErrorResponse
from ...services.auth.models import CurrentUser
from ...services.variables import VariableNotEditable, VariableNotFound, VariableService
from ..auth.dependencies import CurrentUserDep
from ..onebot.dependencies import ensure_can_touch
from ..workflow.dependencies import WorkflowStoreDep, owner_filter_of
from .dependencies import CacheDep
from .responses import VariableData, VariablePage

router = APIRouter(prefix="/variables", tags=["变量查看"])
ALL_SCOPES: tuple[str, ...] = ("graph", "account")
MAX_LIMIT = 500
_T = TypeVar("_T")

_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "没登录 / 令牌无效",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "不能操作别人的变量",
    },
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "变量不存在"},
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "查看器不允许修改",
    },
    HttpStatus.UNPROCESSABLE_ENTITY: {
        "model": ErrorResponse,
        "description": "变量身份或查看器参数错误",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "缓存未接入",
    },
}


async def _call(operation: Awaitable[_T]) -> _T:
    """统一调用边界；存储故障保持原来的异常处理，不伪装成参数错误。"""
    try:
        return await operation
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            str(exc),
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        ) from exc
    except VariableNotFound as exc:
        raise ApiError(
            ErrorCode.HTTP_ERROR, str(exc), status_code=status.HTTP_404_NOT_FOUND
        ) from exc
    except VariableNotEditable as exc:
        raise ApiError(
            ErrorCode.HTTP_ERROR, str(exc), status_code=status.HTTP_409_CONFLICT
        ) from exc


async def _context(
    service: VariableService,
    user: CurrentUser,
    *,
    scope: str,
    key: str,
    owner_id: str | None,
    workflow_id: str,
) -> VariableContext:
    if owner_id is not None:
        ensure_can_touch(user, owner_id)
    context = await _call(
        service.context(
            scope=scope,
            key=key,
            owner_id=user.user.id if owner_id is None else owner_id,
            workflow_id=workflow_id,
        )
    )
    # 图归属来自真实存储；调用方不能通过 owner_id 给孤儿图伪造归属。
    ensure_can_touch(user, context.owner_id)
    return context


@router.get(
    "",
    response_model=ApiResponse[VariablePage],
    summary="列出逻辑变量",
    responses=_RESPONSES,
)
async def list_variables(
    request: Request,
    user: CurrentUserDep,
    cache: CacheDep,
    store: WorkflowStoreDep,
    owner_id: str | None = None,
    scope: Annotated[str | None, Query(description="graph / account")] = None,
    query: Annotated[str | None, Query(description="变量名模糊匹配")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[VariablePage]:
    if scope is not None and scope not in ALL_SCOPES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "作用域要 graph/account 之一",
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        )
    if owner_id is not None:
        ensure_can_touch(user, owner_id)
    service = VariableService(cache, store)
    snapshots, total = await _call(
        service.page(
            scope=scope,
            keyword=(query or "").strip(),
            owner=owner_filter_of(user, owner_id),
            limit=limit,
            offset=offset,
        )
    )
    items = [VariableData.from_snapshot(snapshot) for snapshot in snapshots]
    return ApiResponse[VariablePage](
        data=VariablePage(items=items, total=total), trace_id=trace_id_of(request)
    )


@router.get(
    "/value",
    response_model=ApiResponse[VariableData],
    summary="查看变量",
    responses=_RESPONSES,
)
async def view_variable(
    request: Request,
    user: CurrentUserDep,
    cache: CacheDep,
    store: WorkflowStoreDep,
    scope: str,
    key: str,
    owner_id: str | None = None,
    workflow_id: str = "",
) -> ApiResponse[VariableData]:
    service = VariableService(cache, store)
    context = await _context(
        service, user, scope=scope, key=key, owner_id=owner_id, workflow_id=workflow_id
    )
    snapshot = await _call(service.view(context))
    return ApiResponse[VariableData](
        data=VariableData.from_snapshot(snapshot), trace_id=trace_id_of(request)
    )


@router.post(
    "/add",
    response_model=ApiResponse[VariableData],
    summary="新增或保存变量",
    responses=_RESPONSES,
)
async def add_variable(
    request: Request,
    user: CurrentUserDep,
    cache: CacheDep,
    store: WorkflowStoreDep,
    params: Annotated[dict[str, Any], Body()],
    scope: str,
    key: str,
    owner_id: str | None = None,
    workflow_id: str = "",
) -> ApiResponse[VariableData]:
    service = VariableService(cache, store)
    context = await _context(
        service, user, scope=scope, key=key, owner_id=owner_id, workflow_id=workflow_id
    )
    snapshot = await _call(service.add(context, params))
    return ApiResponse[VariableData](
        data=VariableData.from_snapshot(snapshot), trace_id=trace_id_of(request)
    )
