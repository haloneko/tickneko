"""变量查看的 HTTP 入口：列出工作流「缓存」节点写下的变量。

    GET <prefix>/variables    列出变量（可按作用域 / 归属 / 变量名筛）

要登录：``Authorization: Bearer <token>``，令牌是 ``POST /auth/login`` 给的那个。
**登录了还不算完，还得看范围**（与日志 / 工作流列表一个口径）：带 ``admin`` 角色的人不限
（所有归属的变量都看得到，可用 ``?owner_id=`` 缩到某个归属），其余人只看得见**自己名下**的
—— 显式写别人的归属 → **403**，不写就只看自己的。

变量住在缓存里，键由 cache 节点定：``workflow:graph:{图 id}:{变量名}``（图级，只有这张图
看得见）/ ``workflow:acct:{账号 id}:{变量名}``（账号级，同账号的工作流共享），口径见
``docs/cache/cache.md``。本层把键**反解**成结构化字段：

* 账号级变量的归属就是键里的账号 id；
* 图级变量只有图 id，归属要拿它去工作流存储查（查不到 —— 图已被删 —— 就是空归属：
  普通用户看不到，管理员看得到）。

**只读**：本接口不改、不删、不建任何变量。响应给**一页** ``{items, total}``，与日志检索同形。
"""
from __future__ import annotations

import json
import math
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from tickneko.core.cache import Cache

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode, HttpStatus
from ...common.models import ApiResponse, ErrorResponse
from ..auth.dependencies import CurrentUserDep
from ..onebot.dependencies import ensure_can_touch
from ..workflow.dependencies import WorkflowStoreDep, owner_filter_of
from ..workflow.protocols import WorkflowStoreLike
from .dependencies import CacheDep
from .responses import VariableData, VariablePage

router: APIRouter = APIRouter(prefix="/variables", tags=["变量查看"])

#: 允许的作用域（响应里的口径）：graph = 图级、account = 账号级
ALL_SCOPES: tuple[str, ...] = ("graph", "account")

#: 一次最多给多少条：缓存里的变量可能很多，再多请用 ``offset`` 翻页
MAX_LIMIT: int = 500

#: 工作流变量的键前缀（核心层不含 Redis 命名空间那一段，见 docs/cache/cache.md）
_PREFIX: str = "workflow:"

#: 键里的作用域段 -> 响应里的作用域：cache 节点写 ``acct``，对外说 ``account``
_KEY_SCOPES: dict[str, str] = {"graph": "graph", "acct": "account"}


def _parse_cache_key(raw: str) -> tuple[str, str, str] | None:
    """把缓存键拆成 ``(作用域, 图 id / 账号 id, 变量名)``；不是工作流变量就返回 ``None``。

    键口径是 ``workflow:graph:{图 id}:{变量名}`` / ``workflow:acct:{账号 id}:{变量名}``。
    用 ``maxsplit=3``：变量名里万一混进冒号（cache 节点本来会拦）也只是并进最后一段，
    不会把整条键判成不认识的、凭空少一个变量。
    """
    parts = raw.split(":", 3)
    if len(parts) != 4 or parts[0] != "workflow":
        return None
    scope = _KEY_SCOPES.get(parts[1])
    if scope is None:
        return None
    return scope, parts[2], parts[3]


async def _collect(
    cache: Cache,
    store: WorkflowStoreLike,
    *,
    scope: str | None,
    keyword: str,
    owner: str | None,
) -> list[tuple[str, VariableData]]:
    """枚举缓存里的工作流变量、按条件筛掉，返回 ``(缓存键, 变量)`` 并排好序。

    图级变量的归属要拿图 id 去工作流存储查 —— 一次请求内同一张图只查一次（``graph_owners``）。
    """
    rows: list[tuple[str, VariableData]] = []
    graph_owners: dict[str, str] = {}
    for raw in await cache.keys(f"{_PREFIX}*"):
        parsed = _parse_cache_key(raw)
        if parsed is None:
            continue
        item_scope, ident, name = parsed
        if scope is not None and scope != item_scope:
            continue
        if keyword and keyword not in name:
            continue
        if item_scope == "graph":
            workflow_id = ident
            if ident not in graph_owners:
                record = await store.get(ident)
                graph_owners[ident] = record.owner_id if record is not None else ""
            row_owner = graph_owners[ident]
        else:
            workflow_id = ""
            row_owner = ident
        if owner is not None and row_owner != owner:
            continue
        rows.append((raw, VariableData(scope=item_scope, owner_id=row_owner,
                                       workflow_id=workflow_id, key=name)))
    # 同一条件两次查顺序一致：作用域 -> 归属 -> 所属图 -> 变量名（值 / TTL 每页再取）
    rows.sort(key=lambda row: (row[1].scope, row[1].owner_id, row[1].workflow_id, row[1].key))
    return rows


async def _read_value(cache: Cache, raw: str) -> str:
    """按键的结构类型取「展示用」的值：字符串原样，哈希 / 列表序列化成 JSON 文本。

    变量接口以前只认字符串键，对哈希发 ``GET`` 会撞 WRONGTYPE 把整页打成 500；现在
    先 ``cache.type`` 探结构再分派 —— 三种结构都能看，键不存在 / 空值给空串。
    """
    kind = await cache.type(raw)
    if kind == "hash":
        return json.dumps(await cache.hash_get_all(raw), ensure_ascii=False)
    if kind == "list":
        return json.dumps(await cache.list_range(raw), ensure_ascii=False)
    return (await cache.get(raw)) or ""


@router.get(
    "",
    response_model=ApiResponse[VariablePage],
    summary="列出工作流变量",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "没登录 / 令牌无效",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "普通用户要看别人的归属名下的变量",
        },
        HttpStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "作用域写错",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "缓存没接入",
        },
    },
)
async def list_variables(
    request: Request,
    user: CurrentUserDep,  # 先鉴权：没登录就 401，不往外说有哪些变量
    cache: CacheDep,
    store: WorkflowStoreDep,
    owner_id: str | None = None,
    scope: Annotated[
        str | None, Query(description="按作用域筛：graph（图级）/ account（账号级）")
    ] = None,
    query: Annotated[str | None, Query(description="变量名模糊匹配")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="最多给多少条")] = 100,
    offset: Annotated[int, Query(ge=0, description="跳过前多少条（翻页用）")] = 0,
) -> ApiResponse[VariablePage]:
    """列出可见范围内的变量。

    非管理员**不写** ``owner_id`` 时默认只看自己的；显式要别人的归属 → 403（同日志 /
    OneBot 那组接口）。管理员不写就是全部归属，写 ``?owner_id=`` 就缩到那一个人。
    """
    trace_id: str = trace_id_of(request)

    if scope is not None and scope not in ALL_SCOPES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"作用域要 {'/'.join(ALL_SCOPES)} 之一，收到 {scope!r}",
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        )

    # 归属把关：显式写别人的 -> 403；不写则普通用户只看自己、管理员看全部
    if owner_id is not None:
        ensure_can_touch(user, owner_id)
    chosen_owner = owner_filter_of(user, owner_id)

    rows = await _collect(
        cache, store, scope=scope, keyword=(query or "").strip(), owner=chosen_owner
    )
    page = rows[offset : offset + limit]
    #(TODO)现在是强耦合，以后改成由注册节点处理变量
    items: list[VariableData] = []
    for raw, row in page:
        value = await _read_value(cache, raw)
        ttl = await cache.ttl(raw)
        # 缓存层的「永不过期」是 math.inf；JSON 里没有 Infinity，归一成 None（前端按「不过期」显示）
        remaining = None if ttl is None or ttl == math.inf else ttl
        items.append(row.model_copy(update={"value": value, "ttl": remaining}))

    return ApiResponse[VariablePage](
        data=VariablePage(items=items, total=len(rows)), trace_id=trace_id
    )
