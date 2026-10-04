/**
 * 右键菜单的**唯一出口**：按 ``state.kind`` 决定画哪一份菜单。
 *
 * 两份菜单组件（节点删除 / 空白处添加节点）各自独立，调用方不必管「现在开的是哪一份」，也不用
 * 维护两个状态、两个 ref —— 只把状态与两个动作（删节点 / 加节点）递进来。
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
  /** 节点菜单：删除（可能是一组） */
  onDeleteNodes: (ids: string[]) => void
  /** 空白菜单：在 ``at`` 这一处添加该类型的节点 */
  onAddNode: (type: string, at: Point) => void
}

export function ContextMenuHost({
  state,
  menuRef,
  catalog,
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
      onAdd={(type) => onAddNode(type, state.point)}
    />
  )
}
