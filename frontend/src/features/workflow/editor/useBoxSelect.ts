/**
 * 框选：左键在空白处拖一个矩形，松手把框住的节点固化成当前选中集合。
 *
 * 一个键两种用途：位移没超过阈值算「点了一下」（松手那发 click 归取消选中），
 * 越过了才算框选 —— 框选完浏览器会补发一发 click，得由调用方吞掉，否则刚选中的
 * 又被它清掉（所以 :meth:`BoxSelect.finish` 要返回「这次真的框选过吗」）。
 *
 * 命中判定按节点的**包围盒**（``NODE_W`` × ``nodeHeight``）：只要压到边就算框住，
 * 和拖节点的口径一致。
 *
 * 依赖（``graph`` / ``positions`` / ``setSelectedIds`` / ``bringToFront``）以对象传入并
 * 存进 ref，回调身份因此稳定。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { NODE_W, nodeDef, nodeHeight, type Point, type Positions, type WorkflowGraph } from './catalog'
import type { BoxRect } from './Canvas'

/** 位移阈值（画布坐标）：没超过它就还当「点了一下」 */
const DRAG_THRESHOLD = 5

export interface BoxSelectDeps {
  graph: WorkflowGraph
  positions: Positions
  setSelectedIds: Dispatch<SetStateAction<Set<string>>>
  /** 框选收尾时把这一组提到图层末尾（相对顺序保持原样） */
  bringToFront: (ids: Iterable<string>) => void
}

export interface BoxSelect {
  /** 当前框选矩形（渲染用；``null`` = 没在框选） */
  rect: BoxRect | null
  /** 左键在空白按下：记起点，还没真的开始框选 */
  start: (point: Point) => void
  /** 鼠标移动：返回 ``true`` = 这一发归框选管，调用方别再往下处理 */
  move: (point: Point) => boolean
  /** 松手：收起矩形；返回 ``true`` = 这次真的框选过（调用方吞掉补发的 click） */
  finish: () => boolean
}

export function useBoxSelect(deps: BoxSelectDeps): BoxSelect {
  const depsRef = useRef(deps)
  useEffect(() => {
    depsRef.current = deps
  })

  const [rect, setRect] = useState<BoxRect | null>(null)
  /** 框选起点（画布坐标）；``null`` = 这次手势没在框选 */
  const startRef = useRef<Point | null>(null)
  /** 矩形已建立（越过了阈值）—— 用 ref 判，别在 move 里读 state（那会慢一帧） */
  const rectRef = useRef<BoxRect | null>(null)
  /** 这次手势是否真的框选过（松手那一发 click 要不要吞掉，由它说了算） */
  const movedRef = useRef(false)
  /** 本次框选选中的节点（松手时按它固化图层顺序） */
  const idsRef = useRef<Set<string> | null>(null)

  const move = useCallback((point: Point): boolean => {
    const start = startRef.current
    if (!start) return false
    const dx = point.x - start.x
    const dy = point.y - start.y
    if (!rectRef.current && Math.abs(dx) < DRAG_THRESHOLD && Math.abs(dy) < DRAG_THRESHOLD) {
      return true // 还没越过阈值：仍算框选手势，但什么都不做
    }
    movedRef.current = true
    const next: BoxRect = rectRef.current
      ? { ...rectRef.current, x1: point.x, y1: point.y }
      : { x0: start.x, y0: start.y, x1: point.x, y1: point.y }
    rectRef.current = next
    setRect(next)

    // 实时计算选中（矩形可能是反着拖的，先归一成左上 / 右下）
    const x0 = Math.min(start.x, point.x)
    const y0 = Math.min(start.y, point.y)
    const x1 = Math.max(start.x, point.x)
    const y1 = Math.max(start.y, point.y)
    const d = depsRef.current
    const ids = new Set<string>()
    for (const n of d.graph.nodes) {
      const p = d.positions[n.id]
      if (!p) continue
      const h = nodeHeight(nodeDef(n.type, n.config))
      if (p.x + NODE_W >= x0 && p.x <= x1 && p.y + h >= y0 && p.y <= y1) ids.add(n.id)
    }
    idsRef.current = ids
    d.setSelectedIds(ids)
    return true
  }, [])

  const start = useCallback((point: Point) => {
    startRef.current = point
    rectRef.current = null
    movedRef.current = false
    idsRef.current = null
  }, [])

  const finish = useCallback((): boolean => {
    const boxed = startRef.current !== null && movedRef.current
    const ids = idsRef.current
    startRef.current = null
    rectRef.current = null
    movedRef.current = false
    idsRef.current = null
    setRect(null)
    if (!boxed || !ids) return false
    depsRef.current.bringToFront(ids)
    return true
  }, [])

  // rect 变了才换整体身份：矩形要真的重渲染，其余时候回调保持稳定
  return useMemo(() => ({ rect, start, move, finish }), [rect, start, move, finish])
}
