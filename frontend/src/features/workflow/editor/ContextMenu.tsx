/**
 * 节点右键菜单：视口坐标定位（``position: fixed``），点别处 / Esc 关闭（关闭逻辑在父组件）。
 *
 * 两个动作都对**这一次右键的节点集合**生效 —— 单选时就是它自己，框选后右键就是整组，所以
 * 「复制 / 删除」共用同一份 ``ids``，标签也照数量换措辞。
 */
import { IconCopy, IconTrash } from '../../../common/icons'
import styles from '../WorkflowEditor.module.css'

export interface ContextMenuProps {
  x: number
  y: number
  ids: string[]
  menuRef: React.RefObject<HTMLDivElement>
  onCopy: (ids: string[]) => void
  onDelete: (ids: string[]) => void
}

export function ContextMenu({ x, y, ids, menuRef, onCopy, onDelete }: ContextMenuProps) {
  return (
    <div
      ref={menuRef}
      className={styles.ctxMenu}
      style={{
        left: Math.max(8, Math.min(x, window.innerWidth - 200)),
        top: Math.max(8, Math.min(y, window.innerHeight - 84)),
      }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <button className={styles.ctxMenuItem} onClick={() => onCopy(ids)}>
        <IconCopy size={14} />
        {ids.length > 1 ? `复制选中的 ${ids.length} 个节点` : '复制节点'}
      </button>
      <button
        className={`${styles.ctxMenuItem} ${styles.ctxMenuItemDanger}`}
        onClick={() => onDelete(ids)}
      >
        <IconTrash size={14} />
        {ids.length > 1 ? `删除选中的 ${ids.length} 个节点` : '删除节点'}
      </button>
    </div>
  )
}
