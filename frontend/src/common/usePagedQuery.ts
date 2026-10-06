/**
 * 「筛选 + 分页列表」页面的公共数据层。
 *
 * 变量查看页 / 运行日志页（以及未来所有「条件查询 + 翻页」的列表页）都手写过一遍几乎相同的
 * 逻辑：请求序号防旧响应覆盖、loading / 错误归一、页码 / 每页条数联动、数据变少时页码越界
 * 收口。这里把它们收进一个 hook：
 *
 * - 页面管「草稿 vs 已生效筛选」（点「查询」才同步）、拉归属、以及各自的行渲染；
 * - hook 管「已生效筛选 + 页码 + 每页条数 → 拉取 + 展示状态」，筛选变了 / 翻页 / 改条数都会
 *   自动重拉；
 * - silent 刷新（自动刷新场景）：不闪骨架屏，失败也不清掉已有列表。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiRequestError } from '../lib/http'
import { totalPagesOf } from './Pagination'

/** 错误态：供页面直接塞给 `<ErrorBox>`。 */
export type PagedError = {
  title: string
  detail?: string
  traceId?: string
} | null

export interface PagedQueryOptions<TItem, TFilter> {
  /** 已生效的筛选（点「查询」后同步）；引用变了就带着当前页码重拉 */
  applied: TFilter
  /** 组装请求参数：base 里 limit/offset 已按页码算好，只往里塞筛选字段 */
  buildParams: (
    filters: TFilter,
    base: { limit: number; offset: number },
  ) => Record<string, string | number>
  /** 真正发请求：收到完整参数，返回本页条目 + 命中总数（页面对 api 响应做一层 .data 适配） */
  fetchPage: (
    params: Record<string, string | number>,
  ) => Promise<{ items: TItem[]; total: number }>
  /** 默认每页条数 */
  defaultPageSize?: number
  /** 非 ApiRequestError 时的兜底标题，如「变量加载失败」 */
  fallbackErrorTitle: string
  /** 503（后端某个件没接入）时的标题，如「变量看不了」 */
  notAvailableTitle: string
}

export interface PagedQueryResult<TItem> {
  items: TItem[]
  total: number
  /** 当前页（从 1 开始） */
  page: number
  pageSize: number
  loading: boolean
  error: PagedError
  /** 手动 / 定时刷新当前页；silent=true 不闪骨架、失败不清列表 */
  refresh: (silent?: boolean) => void
  /** 翻页：页码越界由 hook 收口，不用页面操心 */
  setPage: (page: number) => void
  /** 改每页条数：自动回到第 1 页 */
  setPageSize: (size: number) => void
}

/** 把各种抛出来的东西归一成展示用的错误态：503 走「没接好」话术，其余照抄消息。 */
function normalizeError(
  err: unknown,
  fallbackTitle: string,
  notAvailableTitle: string,
): NonNullable<PagedError> {
  if (err instanceof ApiRequestError) {
    if (err.status === 503) {
      return { title: notAvailableTitle, detail: err.message, traceId: err.traceId }
    }
    return { title: err.message, traceId: err.traceId }
  }
  return { title: err instanceof Error ? err.message : fallbackTitle }
}

export function usePagedQuery<TItem, TFilter>({
  applied,
  buildParams,
  fetchPage,
  defaultPageSize = 50,
  fallbackErrorTitle,
  notAvailableTitle,
}: PagedQueryOptions<TItem, TFilter>): PagedQueryResult<TItem> {
  const [items, setItems] = useState<TItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(defaultPageSize)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<PagedError>(null)

  // 组装参数 / 发请求这两个函数每次渲染都是新引用，但 effect 只想认 applied/page/pageSize：
  // 放进 ref 取最新，避免因函数重建反复触发重拉。
  const depsRef = useRef({ buildParams, fetchPage })
  depsRef.current = { buildParams, fetchPage }
  const errorsRef = useRef({ fallbackErrorTitle, notAvailableTitle })
  errorsRef.current = { fallbackErrorTitle, notAvailableTitle }
  // 请求序号：只有「最新一次」的结果会被采用，旧响应丢弃，不盖新页面
  const requestSeq = useRef(0)

  // 「跑一次」的实现每次渲染都重建（闭包捕获当次 applied/page/pageSize），通过 ref 暴露，
  // 让 refresh 永远能拿到调用那一刻最新的参数。
  const runRef = useRef<(silent?: boolean) => void>(() => {})
  runRef.current = (silent = false) => {
    const seq = requestSeq.current + 1
    requestSeq.current = seq
    if (!silent) setLoading(true)
    const { buildParams: build, fetchPage: fetch } = depsRef.current
    const { fallbackErrorTitle: fallback, notAvailableTitle: na } = errorsRef.current
    const params = build(applied, { limit: pageSize, offset: (page - 1) * pageSize })
    fetch(params)
      .then((res) => {
        if (seq !== requestSeq.current) return // 已有更新的请求在飞，这次结果作废
        setItems(res.items)
        setTotal(res.total)
        setError(null)
      })
      .catch((err: unknown) => {
        if (seq !== requestSeq.current) return
        if (!silent) {
          setItems([])
          setTotal(0)
          setError(normalizeError(err, fallback, na))
        }
      })
      .finally(() => {
        if (seq === requestSeq.current) setLoading(false)
      })
  }

  // 已生效筛选 / 页码 / 每页条数任一变化就重拉：首次加载、点查询、翻页、改条数都走这里
  useEffect(() => {
    runRef.current()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applied, page, pageSize])

  const totalPages = useMemo(() => totalPagesOf(total, pageSize), [total, pageSize])

  // 数据变少（如自动刷新时旧日志被清理）会让当前页越界：收口到最后一页，别停在空白页
  useEffect(() => {
    setPage((current) => (current > totalPages ? totalPages : current))
  }, [totalPages])

  const refresh = useCallback((silent = false) => runRef.current(silent), [])
  const changePage = useCallback((target: number) => setPage(target), [])
  const changePageSize = useCallback(
    (size: number) => {
      setPage(1) // 改条数后 offset 会落到不存在的位置：回第 1 页
      setPageSize(size)
    },
    [],
  )

  return {
    items,
    total,
    page,
    pageSize,
    loading,
    error,
    refresh,
    setPage: changePage,
    setPageSize: changePageSize,
  }
}
