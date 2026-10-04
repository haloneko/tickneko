/**
 * 右键菜单的**渲染器**：把 :file:`menu.tsx` 里那张配置表画出来，外加定位与二级菜单的翻转。
 *
 * 它不认识任何具体按钮，只会四件事：按状态过滤（``visible``）、算行高与位置、画按钮、收菜单。
 * 连行高都不写死在「标题 27 + 每行 32」那种口径上 —— 位置是按**实际会画出来的那几行**累加出来的，
 * 所以往配置表里加 / 删按钮，菜单位置与二级菜单的展开方向不用跟着改。
 */
import { Fragment, useState } from 'react'
import { IconChevronRight } from '../../../common/icons'
import { categoryLabel, groupByCategory, nodeDef } from './catalog'
import type { MenuActions, MenuContext, MenuEntry } from './menu'
import styles from '../WorkflowEditor.module.css'

//: 各类行的高度（px）：只用来估菜单位置，不参与渲染
const ROW_H = 32
const TITLE_H = 27
const SEP_H = 9
//: 菜单内边距（与 CSS 的 .ctxMenu padding 一致）
const PAD = 4
//: 宽度估算：一级菜单最小宽度 / 二级菜单大致宽度；用来把菜单夹回视口
const MENU_W = 184
const SUBMENU_W = 176
//: 二级菜单最多这么高（与 CSS 的 max-height 同一口径）：装不下就改成向上展开
const SUBMENU_MAX_H = 380
//: 与视口边缘留的空隙
const GAP = 8

export interface ContextMenuListProps {
  /** 视口坐标（``position: fixed``） */
  x: number
  y: number
  menuRef: React.RefObject<HTMLDivElement>
  entries: MenuEntry[]
  ctx: MenuContext
  actions: MenuActions
  /** 点完任一项（含二级项）之后收菜单 */
  onDone: () => void
}

interface Row {
  entry: MenuEntry
  /** 这一行距菜单顶部的偏移（px） */
  offset: number
  h: number
}

/** 这一行有多高：二级菜单那一项要把分组标题与分类行一起算进去。 */
function heightOf(entry: MenuEntry, ctx: MenuContext): number {
  if (entry.kind === 'separator') return SEP_H
  if (entry.kind === 'catalog') {
    return TITLE_H + groupByCategory(ctx.catalog ?? []).length * ROW_H
  }
  return ROW_H
}

/**
 * 先按状态过滤、再去掉没有意义的分隔线（开头 / 结尾 / 连着两根），最后累加出每一行的偏移。
 * 配置里因此不用写「这根线在有粘贴时才显示」这类跟随条件。
 */
function layout(entries: MenuEntry[], ctx: MenuContext): Row[] {
  const alive = entries.filter((entry) => entry.visible?.(ctx) ?? true)
  const kept = alive.filter(
    (entry, i) =>
      entry.kind !== 'separator' ||
      (i > 0 && i < alive.length - 1 && alive[i - 1].kind !== 'separator'),
  )
  const rows: Row[] = []
  let offset = PAD
  for (const entry of kept) {
    const h = heightOf(entry, ctx)
    rows.push({ entry, offset, h })
    offset += h
  }
  return rows
}

export function ContextMenuList({
  x,
  y,
  menuRef,
  entries,
  ctx,
  actions,
  onDone,
}: ContextMenuListProps) {
  /** 当前展开的二级菜单（行 id；null = 都收着） */
  const [openRow, setOpenRow] = useState<string | null>(null)

  const rows = layout(entries, ctx)
  const menuH = rows.reduce((sum, row) => sum + row.h, PAD * 2)
  const left = Math.max(GAP, Math.min(x, window.innerWidth - MENU_W - GAP))
  const top = Math.max(GAP, Math.min(y, Math.max(GAP, window.innerHeight - menuH - GAP)))
  // 右边放不下二级菜单：二级菜单整份往左翻
  const flipX = left + MENU_W + SUBMENU_W > window.innerWidth

  /** 一个可点的普通项：跑动作，然后收菜单 */
  const run = (fn: () => void) => {
    fn()
    onDone()
  }

  const renderRow = ({ entry, offset }: Row) => {
    if (entry.kind === 'separator') return <div key={entry.id} className={styles.ctxMenuSep} />
    if (entry.kind === 'action') {
      return (
        <button
          key={entry.id}
          className={
            entry.danger
              ? `${styles.ctxMenuItem} ${styles.ctxMenuItemDanger}`
              : styles.ctxMenuItem
          }
          onClick={() => run(() => entry.run(ctx, actions))}
        >
          {entry.icon}
          {entry.label(ctx)}
        </button>
      )
    }

    // catalog：一行分组标题 + 每个分类一行，悬停展开该分类下的节点类型
    const groups = groupByCategory(ctx.catalog ?? [])
    return (
      <Fragment key={entry.id}>
        <div className={styles.ctxMenuTitle}>{entry.title}</div>
        {groups.map(([category, specs], idx) => {
          const rowId = `${entry.id}:${category}`
          const open = openRow === rowId
          // 这一行的二级菜单展开后会向下长 SUBMENU_MAX_H：装不下就改成向上长
          const rowBottom = top + offset + TITLE_H + (idx + 1) * ROW_H
          const flipY = rowBottom + SUBMENU_MAX_H > window.innerHeight - GAP
          return (
            <div
              key={rowId}
              className={styles.ctxSubWrap}
              onMouseEnter={() => setOpenRow(rowId)}
            >
              <button
                className={styles.ctxMenuItem}
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={() => setOpenRow(open ? null : rowId)}
              >
                <span className={`${styles.ctxDot} ${styles.ctxDotGroup}`} />
                {categoryLabel(category)}
                <span className={styles.ctxMenuCount}>{specs.length}</span>
                <span className={styles.ctxMenuArrow}>
                  <IconChevronRight size={13} />
                </span>
              </button>

              {open && (
                <div
                  className={[
                    styles.ctxSubMenu,
                    flipX ? styles.ctxSubMenuLeft : '',
                    flipY ? styles.ctxSubMenuUp : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  style={{ minWidth: SUBMENU_W }}
                  role="menu"
                >
                  {specs.map((spec) => {
                    const def = nodeDef(spec.type)
                    return (
                      <button
                        key={spec.type}
                        className={styles.ctxMenuItem}
                        role="menuitem"
                        title={def.label}
                        onClick={() => run(() => actions.add(spec.type, ctx.point))}
                      >
                        <span className={styles.ctxDot} style={{ background: def.color }} />
                        {def.label}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </Fragment>
    )
  }

  return (
    <div
      ref={menuRef}
      className={styles.ctxMenu}
      style={{ left, top, minWidth: MENU_W }}
      onContextMenu={(e) => e.preventDefault()}
      onMouseLeave={() => setOpenRow(null)}
    >
      {rows.map(renderRow)}
    </div>
  )
}
