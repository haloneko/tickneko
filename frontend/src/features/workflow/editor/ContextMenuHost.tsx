/**
 * 右键菜单的**唯一出口**：按 ``state.kind`` 决定画哪一份菜单。
 *
 * 两份菜单组件（节点剪切 / 复制 / 粘贴 / 删除、空白处粘贴 + 添加节点）各自独立，调用方不必管
 * 「现在开的是哪一份」，也不用维护两个状态、两个 ref —— 只把状态、动作与一个开关（剪贴板里
 * 有没有能贴的）递进来。
 */
import { ContextMenu } from './ContextMenu'
import { CanvasMenu } from './CanvasMenu'
import type { Point, NodeTypeSpec } from './catalog'
import type { MenuState } from './useContextMenu'

export interface ContextMenuHostProps {
  state: MenuState | null
  menuRef: React.RefObject<HTMLDivElement>
  /** 空白菜单要用的节点目录：还没拉回来就没得可加，这时不画 */
  catalog: NodeTypeSpec[] | null
  /** 剪贴板里有没有能贴的：没有就不显示那两处「粘贴」（见 WorkflowEditor 的 hasClipboard） */
  canPaste: boolean
  /** 节点菜单：剪切（复制 + 删除，单选就是它自己，多选就是整组） */
  onCutNodes: (ids: string[]) => void
  /** 节点菜单：复制（单选就是它自己，多选就是整组） */
  onCopyNodes: (ids: string[]) => void
  /** 两份菜单的「粘贴」：在 ``at`` 这一处放下剪贴板里那一组 */
  onPasteAt: (at: Point) => void
  /** 节点菜单：删除（可能是一组） */
  onDeleteNodes: (ids: string[]) => void
  /** 空白菜单：在 ``at`` 这一处添加该类型的节点 */
  onAddNode: (type: string, at: Point) => void
}

export function ContextMenuHost({
  state,
  menuRef,
  catalog,
  canPaste,
  onCutNodes,
  onCopyNodes,
  onPasteAt,
  onDeleteNodes,
  onAddNode,
}: ContextMenuHostProps) {
  if (!state) return null
  if (state.kind === 'node') {
    return (
      <ContextMenu
        x={state.x}
        y={state.y}
        ids={state.ids}
        menuRef={menuRef}
        canPaste={canPaste}
        onCut={onCutNodes}
        onCopy={onCopyNodes}
        onPaste={() => onPasteAt(state.point)}
        onDelete={onDeleteNodes}
      />
    )
  }
  if (!catalog) return null
  return (
    <CanvasMenu
      x={state.x}
      y={state.y}
      items={catalog}
      menuRef={menuRef}
      canPaste={canPaste}
      onPaste={() => onPasteAt(state.point)}
      onAdd={(type) => onAddNode(type, state.point)}
    />
  )
}
