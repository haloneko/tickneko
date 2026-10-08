/**
 * 从节点库拖一个节点进画布：拖动时虚影跟鼠标走，松手落在画布上才真的添加。
 *
 * 监听为什么挂在 ``window`` 上：拖拽路径大半在画布**外**（起点是左侧节点面板），
 * 画布自己的 mousemove 收不到这一段。
 *
 * 一个键两种用途：位移没超过阈值算「点了一下」（和直接点击添加一样），超过了才算拖拽。
 * 松手在画布外 = 取消，不添加。
 *
 * 依赖（``add`` / ``toCanvasAt``）以对象传入并存进 ref，回调身份因此稳定。
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import type { Point } from './catalog'

/** 拖动阈值（像素）：位移超过它才算「拖拽」，没超过就还是「点击」 */
const DRAG_THRESHOLD = 5

export interface PaletteDragDeps {
  /** 加节点：``pos`` 给值 = 拖拽落点，不给 = 落在当前视野中央 */
  add: (type: string, pos?: Point) => void
  /** 屏幕坐标 -> 画布坐标；指针不在画布可视区内返回 ``null`` */
  toCanvasAt: (clientX: number, clientY: number) => Point | null
}

export interface PaletteDrag {
  /** 正在拖出的新节点虚影：x/y = 鼠标的画布坐标（``null`` = 没拖 / 不在画布上） */
  ghost: { type: string; x: number; y: number } | null
  /** 节点库选项按下 */
  onItemMouseDown: (e: ReactMouseEvent, type: string) => void
  /** 键盘（Enter/Space）触发的那一下 click：鼠标的交给 mousedown/mouseup 流程 */
  onItemClick: (e: ReactMouseEvent, type: string) => void
}

export function usePaletteDrag(deps: PaletteDragDeps): PaletteDrag {
  const depsRef = useRef(deps)
  useEffect(() => {
    depsRef.current = deps
  })

  const [ghost, setGhost] = useState<{ type: string; x: number; y: number } | null>(null)
  /** 本次拖拽挂上去的 window 监听怎么摘（下一次拖拽前 / 组件卸载时收掉） */
  const detachRef = useRef<(() => void) | null>(null)

  const detach = useCallback(() => {
    detachRef.current?.()
    detachRef.current = null
  }, [])

  // 卸载时还挂着监听（拖到一半组件没了）：顺手摘掉
  useEffect(() => detach, [detach])

  const onItemMouseDown = useCallback(
    (e: ReactMouseEvent, type: string) => {
      if (e.button !== 0) return
      e.preventDefault() // 防文本选中 / 原生拖拽
      detach()
      const start = { startX: e.clientX, startY: e.clientY, armed: false }

      const onMove = (ev: MouseEvent) => {
        if (!start.armed) {
          if (
            Math.abs(ev.clientX - start.startX) + Math.abs(ev.clientY - start.startY) <
            DRAG_THRESHOLD
          ) {
            return
          }
          start.armed = true
        }
        const point = depsRef.current.toCanvasAt(ev.clientX, ev.clientY)
        setGhost(point ? { type, ...point } : null) // 不在画布上：虚影收起
      }

      const onUp = (ev: MouseEvent) => {
        detach()
        if (!start.armed) {
          depsRef.current.add(type) // 纯点击：出现在当前可视区中央
          return
        }
        setGhost(null)
        const point = depsRef.current.toCanvasAt(ev.clientX, ev.clientY)
        if (!point) return // 松手在画布外：取消，不添加
        depsRef.current.add(type, point)
      }

      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
      detachRef.current = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }
    },
    [detach],
  )

  const onItemClick = useCallback((e: ReactMouseEvent, type: string) => {
    // 键盘（Enter/Space）触发的 click：detail 为 0；鼠标那一下交给 mousedown/mouseup 流程
    if (e.detail === 0) depsRef.current.add(type)
  }, [])

  // ghost 变了才换整体身份：虚影要真的重渲染，其余时候回调保持稳定
  return useMemo(
    () => ({ ghost, onItemMouseDown, onItemClick }),
    [ghost, onItemMouseDown, onItemClick],
  )
}
