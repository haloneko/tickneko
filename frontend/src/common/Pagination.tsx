/**
 * 通用分页条：总数 + 每页条数 + 首/尾 + 上一页/下一页 + 页码（超长自动省略）+ 跳页。
 *
 * 只认「第几页」（从 1 开始），后端那套 limit / offset 由调用方换算（见 logsApi 的
 * buildParams）。页码窗口按总页数算：页数少就全列出来，页数多就只留首尾与当前页附近，
 * 中间用省略号 —— 长列表也不会撑出一条几千个按钮的工具栏。
 *
 * 越界页码（数据变少导致当前页超出范围）**不在这里纠正**：组件只按 totalPages 渲染，
 * 交给持有页码的一方收口（`common/usePagedQuery` 里做），免得一个展示组件偷偷改别人的状态。
 */
import { useEffect, useState } from 'react'
import {
  IconChevronLeft,
  IconChevronRight,
  IconChevronsLeft,
  IconChevronsRight,
} from './icons'
import styles from './Pagination.module.css'

/** 当前页左右各多显示几个页码（当前页附近铺开的宽度）。 */
const SIBLINGS = 1

export interface PaginationProps {
  /** 当前页（从 1 开始） */
  page: number
  /** 每页条数 */
  pageSize: number
  /** 命中总数（用来算总页数） */
  total: number
  /** 翻页：只会收到 ``1 .. totalPages`` 之间的页码 */
  onChange: (page: number) => void
  /** 每页条数候选值；不传就不显示这一项 */
  pageSizeOptions?: readonly number[]
  /** 改每页条数（是否顺带回到第 1 页由调用方决定） */
  onPageSizeChange?: (size: number) => void
  /** 请求进行中等场合：整条不可点 */
  disabled?: boolean
  /** 追加的类名（摆位交给调用方） */
  className?: string
}

/** 总页数：至少 1 页（空结果也是「第 1 页，共 0 条」）。 */
export function totalPagesOf(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / pageSize))
}

/**
 * 页码窗口：首尾各留一页、当前页左右各 ``SIBLINGS`` 页，中间的缺口用 ``'gap'`` 占位。
 *
 * 例（20 页，当前第 10 页）：``1 2 gap 9 10 11 gap 19 20``。
 */
export function pageWindow(page: number, totalPages: number): (number | 'gap')[] {
  const wanted = new Set<number>([1, totalPages])
  for (let offset = 0; offset <= SIBLINGS; offset++) {
    if (page - offset >= 1) wanted.add(page - offset)
    if (page + offset <= totalPages) wanted.add(page + offset)
  }
  // 当前页贴着首/尾时，另一侧多铺一格，窗口不至于只剩「1 2 gap 20」
  for (let offset = 0; offset <= SIBLINGS; offset++) {
    if (page <= SIBLINGS + 1 && 1 + offset <= totalPages) wanted.add(1 + offset)
    if (page >= totalPages - SIBLINGS && totalPages - offset >= 1) {
      wanted.add(totalPages - offset)
    }
  }

  const sorted = [...wanted].filter((n) => n >= 1 && n <= totalPages).sort((a, b) => a - b)
  const window: (number | 'gap')[] = []
  let previous = 0
  for (const n of sorted) {
    if (previous && n - previous > 1) window.push('gap')
    window.push(n)
    previous = n
  }
  return window
}

export default function Pagination({
  page,
  pageSize,
  total,
  onChange,
  pageSizeOptions,
  onPageSizeChange,
  disabled = false,
  className,
}: PaginationProps) {
  const totalPages = totalPagesOf(total, pageSize)
  // 跳页输入框：跟着当前页走（翻页后回到「当前页」那个数，不留上一次的手输内容）
  const [jump, setJump] = useState(() => String(page))
  useEffect(() => setJump(String(page)), [page])

  const goto = (target: number) => {
    const clamped = Math.min(Math.max(Math.trunc(target), 1), totalPages)
    if (clamped !== page) onChange(clamped)
  }

  const commitJump = () => {
    const typed = Number(jump.trim())
    if (!Number.isFinite(typed)) {
      setJump(String(page)) // 不是数字：还原，不跳
      return
    }
    goto(typed)
    setJump(String(Math.min(Math.max(Math.trunc(typed), 1), totalPages)))
  }

  const atFirst = disabled || page <= 1
  const atLast = disabled || page >= totalPages

  return (
    <div className={`${styles.pager} ${className ?? ''}`}>
      <div className={styles.summary}>
        <span className={styles.total}>
          共 <b>{total}</b> 条 · 第 <b>{page}</b> / {totalPages} 页
        </span>
        {pageSizeOptions && pageSizeOptions.length > 0 && onPageSizeChange && (
          <label className={styles.size}>
            每页
            <select
              className={styles.select}
              value={pageSize}
              onChange={(e) => onPageSizeChange(Number(e.target.value))}
              disabled={disabled}
            >
              {pageSizeOptions.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
            条
          </label>
        )}
      </div>

      <div className={styles.controls}>
        <nav className={styles.nav} aria-label="分页">
          <button
            type="button"
            className={styles.step}
            onClick={() => goto(1)}
            disabled={atFirst}
            aria-label="第一页"
            title="第一页"
          >
            <IconChevronsLeft size={15} />
          </button>
          <button
            type="button"
            className={styles.step}
            onClick={() => goto(page - 1)}
            disabled={atFirst}
            aria-label="上一页"
            title="上一页"
          >
            <IconChevronLeft size={15} />
          </button>

          {pageWindow(page, totalPages).map((item, index) =>
            item === 'gap' ? (
              <span key={`gap-${index}`} className={styles.gap} aria-hidden="true">
                …
              </span>
            ) : (
              <button
                key={item}
                type="button"
                className={`${styles.page} ${item === page ? styles.pageActive : ''}`}
                onClick={() => goto(item)}
                disabled={disabled}
                aria-current={item === page ? 'page' : undefined}
                aria-label={`第 ${item} 页`}
              >
                {item}
              </button>
            ),
          )}

          <button
            type="button"
            className={styles.step}
            onClick={() => goto(page + 1)}
            disabled={atLast}
            aria-label="下一页"
            title="下一页"
          >
            <IconChevronRight size={15} />
          </button>
          <button
            type="button"
            className={styles.step}
            onClick={() => goto(totalPages)}
            disabled={atLast}
            aria-label="最后一页"
            title="最后一页"
          >
            <IconChevronsRight size={15} />
          </button>
        </nav>

        <form
          className={styles.jump}
          onSubmit={(event) => {
            event.preventDefault()
            commitJump()
          }}
        >
          <span className={styles.jumpLabel}>跳到</span>
          <input
            className={styles.jumpInput}
            value={jump}
            onChange={(e) => setJump(e.target.value)}
            onBlur={commitJump}
            disabled={disabled}
            inputMode="numeric"
            aria-label="页码"
            title="输入页码后回车"
          />
          <span className={styles.jumpLabel}>页</span>
        </form>
      </div>
    </div>
  )
}
