/**
 * 节点右键菜单：视口坐标定位（``position: fixed``），点别处 / Esc 关闭（关闭逻辑在父组件）。
 *
 * 「剪切 / 复制 / 删除」对**这一次右键的节点集合**生效 —— 单选时就是它自己，框选后右键就是整组，
 * 所以三者共用同一份 ``ids``，标签也照数量换措辞；「粘贴」与集合无关，落点由父组件按右键位置给
 * （``onPaste`` 不带参数：菜单不关心画布坐标），而且**剪贴板里没东西就不显示它**（``canPaste``）。
 */
import { IconCopy, IconPaste, IconScissors, IconTrash } from '../../../common/icons'
import styles from '../WorkflowEditor.module.css'

export interface ContextMenuProps {
  x: number
  y: number
  ids: string[]
  menuRef: React.RefObject<HTMLDivElement>
  canPaste: boolean
  onCut: (ids: string[]) => void
  onCopy: (ids: string[]) => void
  /** 在右键那一处粘贴剪贴板内容（位置由父组件补） */
  onPaste: () => void
  onDelete: (ids: string[]) => void
}

export function ContextMenu({
  x,
  y,
  ids,
  menuRef,
  canPaste,
  onCut,
  onCopy,
  onPaste,
  onDelete,
}: ContextMenuProps) {
  const many = ids.length > 1
  return (
    <div
      ref={menuRef}
      className={styles.ctxMenu}
      style={{
        left: Math.max(8, Math.min(x, window.innerWidth - 200)),
        top: Math.max(8, Math.min(y, window.innerHeight - 148)),
      }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <button className={styles.ctxMenuItem} onClick={() => onCut(ids)}>
        <IconScissors size={14} />
        {many ? `剪切选中的 ${ids.length} 个节点` : '剪切节点'}
      </button>
      <button className={styles.ctxMenuItem} onClick={() => onCopy(ids)}>
        <IconCopy size={14} />
        {many ? `复制选中的 ${ids.length} 个节点` : '复制节点'}
      </button>
      {canPaste && (
        <button className={styles.ctxMenuItem} onClick={onPaste}>
          <IconPaste size={14} />
          粘贴
        </button>
      )}
      <button
        className={`${styles.ctxMenuItem} ${styles.ctxMenuItemDanger}`}
        onClick={() => onDelete(ids)}
      >
        <IconTrash size={14} />
        {many ? `删除选中的 ${ids.length} 个节点` : '删除节点'}
      </button>
    </div>
  )
}
