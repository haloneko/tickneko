/**
 * 端口拉线：从一个端口按下拖到另一个端口松开，落下一条边。
 *
 * 落线前的三条铁律（顺序即判定顺序）：不能连自己、方向必须一进一出、端口类型要兼容
 * （同类，或泛型端口接任意数据流端口）。重复连线**不占撤销步**——静默忽略。
 *
 * 拉线过程中那条跟着鼠标走的临时线（``pending``）也在这里算好：它要读端口绝对坐标与
 * 生效类型，交给外面拼只会把这三张表（nodeById / positions / effTypes）漏出去。
 *
 * 依赖以对象传入并存进 ref，回调身份因此稳定（节点卡片是 memo 化的）。
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type MouseEvent as ReactMouseEvent,
  type SetStateAction,
} from 'react'
import {
  edgeCurve,
  portAbsPos,
  portColor,
  portCompatible,
  portEffKey,
  type Point,
  type Positions,
  type PortType,
  type WorkflowGraph,
  type WorkflowNode,
} from './catalog'

/** 拉线的起点（按下时那个端口） */
interface PendingPort {
  nodeId: string
  portId: string
  portType: PortType
  direction: 'in' | 'out'
}

export interface PortConnectDeps {
  /** 当前图（连这条边之前先查重） */
  graph: WorkflowGraph
  setGraph: Dispatch<SetStateAction<WorkflowGraph>>
  pushUndo: (snapshot?: WorkflowGraph, coalesceKey?: string) => void
  /** 端口类型不兼容时提示一句 */
  pushToast: (type: 'error', message: string) => void
  /**
   * ``true`` = 此刻端口不让路（粘贴放置模式下左键归画布落子）。
   * 松手那条不需要它：起点没记下（被拦在按下那一步）时本来什么都不发生。
   */
  blocked: () => boolean
  /** 屏幕坐标 -> 画布坐标；指针不在画布可视区内返回 ``null`` */
  toCanvasAt: (clientX: number, clientY: number) => Point | null
  /** 以下三张表：算临时连线的起点坐标与显色用 */
  nodeById: Map<string, WorkflowNode>
  positions: Positions
  effTypes: Map<string, PortType>
}

export interface PortConnect {
  /** 正在拉的临时连线（渲染用；``null`` = 没在拉） */
  pending: { path: string; color: string } | null
  /** 端口按下：开始拉线 */
  start: (e: ReactMouseEvent, nodeId: string, portId: string, portType: PortType, direction: 'in' | 'out') => void
  /** 端口松开：尝试落线（不合法就静默收掉） */
  drop: (e: ReactMouseEvent, nodeId: string, portId: string, portType: PortType, direction: 'in' | 'out') => void
  /** 鼠标移动：临时线跟着走到新的画布坐标 */
  move: (point: Point) => void
  /** 取消：松手在空白处 */
  cancel: () => void
}

export function usePortConnect(deps: PortConnectDeps): PortConnect {
  const depsRef = useRef(deps)
  useEffect(() => {
    depsRef.current = deps
  })

  const pendingRef = useRef<PendingPort | null>(null)
  /** 鼠标当前位置（画布坐标）：临时线的终点；``null`` = 没在拉 */
  const [cursor, setCursor] = useState<Point | null>(null)

  const clear = useCallback(() => {
    pendingRef.current = null
    setCursor(null)
  }, [])

  const start = useCallback(
    (
      e: ReactMouseEvent,
      nodeId: string,
      portId: string,
      portType: PortType,
      direction: 'in' | 'out',
    ) => {
      if (e.button !== 0) return
      if (depsRef.current.blocked()) return // 放置模式：端口也让路，左键归画布落子
      e.stopPropagation()
      pendingRef.current = { nodeId, portId, portType, direction }
      const point = depsRef.current.toCanvasAt(e.clientX, e.clientY)
      if (point) setCursor(point)
    },
    [],
  )

  const drop = useCallback(
    (
      e: ReactMouseEvent,
      nodeId: string,
      portId: string,
      portType: PortType,
      direction: 'in' | 'out',
    ) => {
      const pending = pendingRef.current
      if (!pending) return
      e.stopPropagation()
      // 不能连自己
      if (pending.nodeId === nodeId) return clear()
      // 方向必须一进一出
      if (pending.direction === direction) return clear()
      // 类型必须兼容（同类；泛型端口可接任意数据流端口）
      if (!portCompatible(pending.portType, portType)) {
        depsRef.current.pushToast('error', `端口类型不匹配：${pending.portType} ≠ ${portType}`)
        return clear()
      }
      // pending 是出口就按 pending 那边当 source，否则反过来
      const out = pending.direction === 'out'
      const edge = {
        source: out ? pending.nodeId : nodeId,
        target: out ? nodeId : pending.nodeId,
        sourcePort: out ? pending.portId : portId,
        targetPort: out ? portId : pending.portId,
      }
      const d = depsRef.current
      const exists = d.graph.edges.some(
        (e2) =>
          e2.source === edge.source &&
          e2.target === edge.target &&
          e2.sourcePort === edge.sourcePort &&
          e2.targetPort === edge.targetPort,
      )
      if (!exists) {
        d.pushUndo()
        d.setGraph((g) => ({ ...g, edges: [...g.edges, edge] }))
      }
      clear()
    },
    [clear],
  )

  const move = useCallback((point: Point) => {
    if (pendingRef.current) setCursor(point)
  }, [])

  const cancel = useCallback(() => clear(), [clear])

  /** 正在拉的临时连线（起点端口 -> 鼠标） */
  const pending = useMemo(() => {
    const conn = pendingRef.current
    if (!conn || !cursor) return null
    const d = depsRef.current
    const node = d.nodeById.get(conn.nodeId)
    if (!node) return null
    const start2 = portAbsPos(
      conn.nodeId,
      conn.portId,
      conn.direction,
      node.type,
      d.positions,
      node.config,
    )
    if (!start2) return null
    return {
      path: edgeCurve(start2.x, start2.y, cursor.x, cursor.y),
      // 泛型端口拖线也按生效类型显色（placeholder 输出接会话定位就是橙色预览线）
      color: portColor(
        d.effTypes.get(portEffKey(conn.nodeId, conn.direction, conn.portId)) ?? conn.portType,
      ),
    }
  }, [cursor, deps.nodeById, deps.positions, deps.effTypes])

  // pending 变了才换整体身份：临时线要真的重渲染，其余时候回调保持稳定
  return useMemo(() => ({ pending, start, drop, move, cancel }), [pending, start, drop, move, cancel])
}
