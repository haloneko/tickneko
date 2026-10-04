/**
 * 右键拖动平移：把「一次右键手势」的按下 / 移动 / 松手三个阶段收在这一个 hook 里。
 *
 * 它存在的理由是右键**一个键两种用途**：点一下弹菜单，按住拖是平移 —— 靠同一次手势里「动没动」
 * 区分，而那个「动没动」的标记以前散在四处（按下置否、移动置是、两个 contextmenu 各判一次）。
 * 现在只在这里维护：谁想知道就问一次 ``dragged()``。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Point } from './catalog'

/** 位移超过这个距离才算「拖动」，没超过就还是「点了一下」 */
const DRAG_THRESHOLD = 3

export interface CanvasPanApi {
  /** 正在右键平移（画布光标变抓手） */
  panning: boolean
  begin: (e: { clientX: number; clientY: number }) => void
  /** 返回 true = 这一发 mousemove 归平移管，调用方别再往下处理 */
  move: (e: { clientX: number; clientY: number }) => boolean
  end: () => void
  /**
   * 本次右键手势是否真的拖动过。
   *
   * 注意它**不随松手复位**，要等下一次 ``begin``：Windows 上 ``contextmenu`` 是松开右键之后才发的，
   * 松手就复位的话，「右键拖了一下」会被当成「点了一下」而弹出菜单。
   */
  dragged: () => boolean
}

export interface CanvasPanOptions {
  /** 当前平移量（按下时记起点用） */
  pan: Point
  setPan: (pan: Point) => void
  /** 本次手势第一次越过阈值时回调（调用方在这里收掉挂着的菜单） */
  onDragStart?: () => void
}

export function useCanvasPan({ pan, setPan, onDragStart }: CanvasPanOptions): CanvasPanApi {
  const [panning, setPanning] = useState(false)
  /** 本次手势的起点；``null`` = 没在右键拖动 */
  const gestureRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)
  /** 本次手势是否已越过阈值（跨手势保留，见 ``dragged`` 的说明） */
  const draggedRef = useRef(false)
  // 回调里要读最新值（pan 每移动一次都在变）又不想让回调换身份，统一走 ref
  const latest = useRef({ pan, setPan, onDragStart })
  useEffect(() => {
    latest.current = { pan, setPan, onDragStart }
  }, [pan, setPan, onDragStart])

  const begin = useCallback((e: { clientX: number; clientY: number }) => {
    draggedRef.current = false
    gestureRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      panX: latest.current.pan.x,
      panY: latest.current.pan.y,
    }
    setPanning(true)
  }, [])

  const move = useCallback((e: { clientX: number; clientY: number }) => {
    const gesture = gestureRef.current
    if (!gesture) return false
    const dx = e.clientX - gesture.startX
    const dy = e.clientY - gesture.startY
    if (!draggedRef.current && Math.abs(dx) + Math.abs(dy) > DRAG_THRESHOLD) {
      draggedRef.current = true
      latest.current.onDragStart?.() // 真的开始拖了：挂着的菜单收掉
    }
    latest.current.setPan({ x: gesture.panX + dx, y: gesture.panY + dy })
    return true
  }, [])

  const end = useCallback(() => {
    gestureRef.current = null
    setPanning(false)
  }, [])

  const dragged = useCallback(() => draggedRef.current, [])

  // 函数身份全部稳定（panning 变了才换整体身份）：画布回调的依赖里可以放心放它们
  return useMemo(() => ({ panning, begin, move, end, dragged }), [panning, begin, move, end, dragged])
}
