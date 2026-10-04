/**
 * 右键菜单的状态与关闭：**同一时刻只有一份菜单**（``MenuState`` 是个联合类型：节点上 / 空白处）。
 *
 * 这里只管三件事：记住当前开的是哪一份、点别处（或 Esc）关掉它、把菜单根节点的 ref 递出去
 * （document 上那一下 mousedown 靠它区分「点菜单里」与「点别处」）。
 *
 * 「菜单长什么样」「什么状态显示什么」「点完做什么」都不在这里：前者归 menu.tsx 的配置表，
 * 画出来归 ContextMenuList。打开一份新菜单 = 覆盖旧状态，不用先手动关另一份。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Point } from './catalog'

/**
 * 当前打开的右键菜单：节点上（复制 / 粘贴 / 删除）或空白处（粘贴 / 添加节点）。
 *
 * ``point`` 是右键那一处的**画布坐标**，两份菜单都要用它：空白菜单拿它当新节点的落点，
 * 「粘贴」拿它当整组副本的中心。
 */
export type MenuState =
  | { kind: 'node'; x: number; y: number; ids: string[]; point: Point }
  | { kind: 'canvas'; x: number; y: number; point: Point }

export interface ContextMenuApi {
  state: MenuState | null
  /** 菜单根节点（fixed 定位，挂在页面根部） */
  ref: React.RefObject<HTMLDivElement>
  /** 打开一份菜单（覆盖当前这份）；坐标是视口坐标 */
  open: (next: MenuState) => void
  close: () => void
}

export function useContextMenu(): ContextMenuApi {
  const [state, setState] = useState<MenuState | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  // setState 本身身份稳定：open / close 不会让依赖它们的画布回调每开一次菜单就换身份
  const open = useCallback((next: MenuState) => setState(next), [])
  const close = useCallback(() => setState(null), [])

  useEffect(() => {
    if (!state) return
    const onDown = (e: MouseEvent) => {
      if (ref.current?.contains(e.target as Node)) return
      setState(null)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setState(null)
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [state])

  return { state, ref, open, close }
}
