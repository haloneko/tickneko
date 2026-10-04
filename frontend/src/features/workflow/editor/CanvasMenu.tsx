/**
 * 空白处右键菜单：一级是**语义分类**，二级是分类下的节点类型，点二级项即在右键处添加节点。
 *
 * 分类、顺序、中文名全部来自后端目录（``groupByCategory``，与左侧节点面板同一份 —— 两处永远不会
 * 出现两套分类）。前端只管二级菜单往哪边展开：右边放不下就往左翻，下面放不下就往上翻，别让菜单
 * 跑出视口。
 */
import { useState } from 'react'
import { IconChevronRight } from '../../../common/icons'
import { categoryLabel, groupByCategory, nodeDef, type NodeTypeSpec } from './catalog'
import styles from '../WorkflowEditor.module.css'

//: 宽度估算（px）：一级 / 二级菜单大致多宽，用来判断二级菜单往左还是往右翻
const MENU_W = 184
const SUBMENU_W = 184
//: 菜单内部几何（标题高 + 每行高）：估算二级菜单展开后会不会掉出视口下沿
const TITLE_H = 27
const ROW_H = 32
//: 二级菜单最大高度（与 CSS 的 max-height 同一口径）
const SUBMENU_MAX_H = 380

export interface CanvasMenuProps {
  /** 视口坐标（``position: fixed``，与节点右键菜单一致） */
  x: number
  y: number
  /** 节点类型目录（后端给的，与左侧面板同一份） */
  items: NodeTypeSpec[]
  menuRef: React.RefObject<HTMLDivElement>
  onAdd: (type: string) => void
}

export function CanvasMenu({ x, y, items, menuRef, onAdd }: CanvasMenuProps) {
  /** 当前展开的分类（null = 都收着） */
  const [openCategory, setOpenCategory] = useState<string | null>(null)
  const groups = groupByCategory(items)

  const left = Math.max(8, Math.min(x, window.innerWidth - MENU_W - 8))
  const top = Math.max(8, Math.min(y, window.innerHeight - 80))
  // 右边放不下二级菜单：整份菜单往左翻（一级菜单的 x 也夹回视口内）
  const flipX = left + MENU_W + SUBMENU_W > window.innerWidth

  return (
    <div
      ref={menuRef}
      className={styles.ctxMenu}
      style={{ left, top, minWidth: MENU_W }}
      onContextMenu={(e) => e.preventDefault()}
      onMouseLeave={() => setOpenCategory(null)}
    >
      <div className={styles.ctxMenuTitle}>添加节点</div>
      {groups.map(([category, specs], idx) => {
        // 这一行的二级菜单如果展开，会从行的位置向下长 SUBMENU_MAX_H：装不下就改为向上长
        const rowBottom = top + TITLE_H + (idx + 1) * ROW_H
        const flipY = rowBottom + SUBMENU_MAX_H > window.innerHeight - 8
        return (
          <div key={category} className={styles.ctxSubWrap} onMouseEnter={() => setOpenCategory(category)}>
            <button
              className={styles.ctxMenuItem}
              aria-haspopup="menu"
              aria-expanded={openCategory === category}
              onClick={() => setOpenCategory((cur) => (cur === category ? null : category))}
            >
              <span className={`${styles.ctxDot} ${styles.ctxDotGroup}`} />
              {categoryLabel(category)}
              <span className={styles.ctxMenuCount}>{specs.length}</span>
              <span className={styles.ctxMenuArrow}>
                <IconChevronRight size={13} />
              </span>
            </button>

            {openCategory === category && (
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
                      onClick={() => onAdd(spec.type)}
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
    </div>
  )
}
