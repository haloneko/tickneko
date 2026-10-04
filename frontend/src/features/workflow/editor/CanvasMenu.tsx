/**
 * 空白处右键菜单：顶部是「粘贴」（落在右键那一处），下面是**语义分类**的二级菜单，点二级项即在
 * 右键处添加节点。
 *
 * 分类、顺序、中文名全部来自后端目录（``groupByCategory``，与左侧节点面板同一份 —— 两处永远不会
 * 出现两套分类）。前端只管二级菜单往哪边展开：右边放不下就往左翻，下面放不下就往上翻，别让菜单
 * 跑出视口。
 */
import { useState } from 'react'
import { IconChevronRight, IconPaste } from '../../../common/icons'
import { categoryLabel, groupByCategory, nodeDef, type NodeTypeSpec } from './catalog'
import styles from '../WorkflowEditor.module.css'

//: 宽度估算（px）：一级 / 二级菜单大致多宽，用来判断二级菜单往左还是往右翻
const MENU_W = 184
const SUBMENU_W = 184
//: 菜单顶部固定内容的高度：「粘贴」那一行 + 分隔线 + 「添加节点」标题（估算会不会掉出下沿）
const PASTE_H = 32
const SEP_H = 9
const TITLE_H = 27
//: 每个分类一行
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
  /** 剪贴板里有没有能贴的：没有就整行不显示 */
  canPaste: boolean
  /** 在右键那一处粘贴剪贴板内容（位置由父组件补） */
  onPaste: () => void
  onAdd: (type: string) => void
}

export function CanvasMenu({ x, y, items, menuRef, canPaste, onPaste, onAdd }: CanvasMenuProps) {
  /** 当前展开的分类（null = 都收着） */
  const [openCategory, setOpenCategory] = useState<string | null>(null)
  const groups = groupByCategory(items)

  /** 顶部固定内容的高度：「粘贴」那一行可能没有（剪贴板空着就整行不显示） */
  const headH = TITLE_H + (canPaste ? PASTE_H + SEP_H : 0)
  const left = Math.max(8, Math.min(x, window.innerWidth - MENU_W - 8))
  // 高度按行数估：贴到下沿就整份往上挪（分类多的时候不至于把最后几个分类顶出屏幕）
  const menuH = headH + groups.length * ROW_H
  const top = Math.max(8, Math.min(y, Math.max(8, window.innerHeight - menuH - 8)))
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
      {canPaste && (
        <>
          <button className={styles.ctxMenuItem} onClick={onPaste}>
            <IconPaste size={14} />
            粘贴
          </button>
          <div className={styles.ctxMenuSep} />
        </>
      )}
      <div className={styles.ctxMenuTitle}>添加节点</div>
      {groups.map(([category, specs], idx) => {
        // 这一行的二级菜单如果展开，会从行的位置向下长 SUBMENU_MAX_H：装不下就改为向上长
        const rowBottom = top + headH + (idx + 1) * ROW_H
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
