/**
 * 变量查看 API：GET /api/variables。
 *
 * 与后端契约一一对应（见 tickneko/api/api/variables/router.py）：
 * - 响应是一页 `{ items, total }`，total 是命中总数，前端据此算总页数、做页码跳转；
 * - 变量按作用域分两档：`graph`（图级，只有这张图看得见）/ `account`（账号级，同账号共享）；
 * - 非管理员后端强制只看自己的 owner_id，指定别人会 403 —— 所以普通用户不传 owner_id；
 * - 缓存没接入时后端回 503（变量全在缓存里，没有它答不了）。
 */
import { http } from '../../lib/http'

/** 变量作用域（后端口径）。顺序即界面下拉顺序。 */
export const VARIABLE_SCOPES = ['graph', 'account'] as const
export type VariableScope = (typeof VARIABLE_SCOPES)[number]

/** 一个工作流变量：字段原样来自后端，不做二次拼串。 */
export interface VariableEntry {
  /** 作用域：graph = 图级；account = 账号级 */
  scope: VariableScope | string
  /** 归属者 id；空串 = 查不到归属（图已被删） */
  owner_id: string
  /** 所属工作流 id；账号级变量为空串 */
  workflow_id: string
  /** 变量名 */
  key: string
  /** 当前值（按文本存） */
  value: string
  /** 剩余秒数；null = 永不过期 */
  ttl: number | null
}

/** 变量列表的一页：本页条目 + 命中总数（总数只跟筛选条件有关）。 */
export interface VariablePage {
  items: VariableEntry[]
  total: number
}

/** 查询参数：全部可选；字段名即后端 query 参数名。 */
export interface VariableSearchParams {
  /** 归属者精确匹配（仅管理员有意义；普通用户后端强制只看自己，传了也不生效） */
  owner_id?: string
  /** 作用域：graph / account；不传 = 全部 */
  scope?: string
  /** 变量名模糊匹配 */
  query?: string
  limit?: number
  offset?: number
}

/** 列出变量。params 里 undefined 的字段不发。 */
export function listVariables(params: VariableSearchParams = {}) {
  return http.get<VariablePage>('/variables', { params })
}
