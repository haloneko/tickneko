/**
 * 画布剪贴板：复制 / 剪切 / 粘贴，以及粘贴时的那套「虚影放置」。
 *
 * 两条并行的存放路径（**双重保险**）：
 *
 * * 内存里的 ``clipboardRef`` —— 同步可读，Ctrl+V 立刻有货；
 * * 系统剪贴板 —— 活过刷新、能跨标签页，写入是异步的也可能失败（非安全上下文 / 没权限）。
 *
 * 取的时候**两份都读，谁新听谁的**（比对负载里的复制时刻 ``copiedAt``）；挑中的那份顺手
 * 存回内存，后面几次 Ctrl+V 就不必再读系统剪贴板。
 *
 * 粘贴分两种贴法，共一份几何（``Placing``）：
 *
 * * **Ctrl+V** —— 没有落点信息，先挂成半透明虚影跟鼠标走，左键才落子（``startPlacing`` →
 *   ``dropPlacing``，Esc 走 ``cancelPlacing``）；
 * * **右键「粘贴」** —— 鼠标已经指名落点，一步到位，不做虚影（``pasteAt``）。
 *
 * 依赖以对象传入并存进 ref，回调身份因此稳定。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { copyText, readText } from '../../../lib/clipboard'
import {
  buildClipboardPayload,
  formatCopiedAt,
  parseClipboard,
  pickFresherClipboard,
  serializeClipboard,
  type ClipboardPayload,
} from './clipboard'
import { NODE_W, nodeDef, nodeHeight, uid, type Point, type Positions, type WorkflowGraph } from './catalog'
import type { WorkflowEdge, WorkflowNode } from '../workflowApi'

/**
 * 「待放置的一组节点」：两种贴法共用这份几何。
 *
 * ``x``/``y`` = 组中心当前对准的画布坐标；``cx``/``cy`` = 组中心在这组快照坐标里的位置，
 * 两者之差就是整组要平移的偏移。
 */
export interface Placing {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  cx: number
  cy: number
  x: number
  y: number
}

export interface EditorClipboardDeps {
  graph: WorkflowGraph
  positions: Positions
  pushUndo: (snapshot?: WorkflowGraph, coalesceKey?: string) => void
  setGraph: Dispatch<SetStateAction<WorkflowGraph>>
  setSelectedId: Dispatch<SetStateAction<string | null>>
  setSelectedIds: Dispatch<SetStateAction<Set<string>>>
  pushToast: (type: 'error' | 'info' | 'success', message: string) => void
  /** 当前选择集合（Ctrl+C / Ctrl+X 复制的就是它） */
  getSelectionIds: () => Set<string>
  /** 剪切的那一下删除 */
  removeMany: (ids: string[]) => void
  /** 最近一次画布鼠标位置：Ctrl+V 进入放置模式时拿它当虚影落点 */
  lastPointer: () => Point | null
  /** 立牌子：吞掉落子后浏览器补发的那发 click，别让它当「点空白」清掉刚选中的新节点 */
  suppressNextClick: () => void
}

export interface EditorClipboard {
  /** 剪贴板里有没有能贴的东西：右键菜单据此决定**显不显示「粘贴」** */
  hasClipboard: boolean
  /**
   * 内存那一份**同步**有没有货：Ctrl+V 要赶在这一发事件里 ``preventDefault``，
   * 等不了读系统剪贴板的 promise —— 有货才拦，没货就放行（反正也贴不出东西）。
   */
  hasLocal: () => boolean
  /** 待放置的一组（虚影）；``null`` = 没在放置模式 */
  placing: Placing | null
  /** 复制一组（返回复制到的节点数） */
  copy: (ids: Iterable<string>) => number
  /** Ctrl+C：复制当前选择 */
  copySelection: () => number
  /** 剪切 = 复制 + 删除（返回复制到的节点数；0 = 没得剪，调用方别再删） */
  cut: (ids: Iterable<string>) => number
  /** Ctrl+V：挂虚影进入放置模式 */
  startPlacing: () => void
  /** 右键「粘贴」：在指定处直接落子 */
  pasteAt: (at: Point) => void
  /** 放置模式：虚影组中心跟着鼠标走 */
  movePlacing: (point: Point) => void
  /** 虚影落子：写进图 + 收起虚影 */
  dropPlacing: () => void
  /** 取消放置（Esc / 撤销前） */
  cancelPlacing: () => void
}

export function useEditorClipboard(deps: EditorClipboardDeps): EditorClipboard {
  const depsRef = useRef(deps)
  useEffect(() => {
    depsRef.current = deps
  })

  /**
   * 画布内部剪贴板：Ctrl+C / Ctrl+X 存这里的节点 + 组内连线，Ctrl+V 以虚影放置。
   * 这是**第一重**保险（读写同步）；同一份内容还会写进系统剪贴板（见 ``copy``）。
   */
  const clipboardRef = useRef<ClipboardPayload | null>(null)
  /**
   * 剪贴板里有没有能贴的东西 —— 只是界面用的镜像（真值在 ``clipboardRef`` 与系统剪贴板里）：
   * 复制 / 剪切写内存时置真；打开编辑器时探一次系统剪贴板，把上次会话 / 别的标签页复制过的
   * 那份也算上。
   */
  const [hasClipboard, setHasClipboard] = useState(false)
  const [placing, setPlacing] = useState<Placing | null>(null)
  /** 虚影的同步镜像：落子那一瞬间要读它，读 state 会慢一帧 */
  const placingRef = useRef<Placing | null>(null)
  useEffect(() => {
    placingRef.current = placing
  }, [placing])

  /**
   * 打开编辑器时探一次**系统**剪贴板：上一会话 / 别的标签页里复制的那份也能贴。
   * 读不到（没权限、非安全上下文、Firefox 要手势）就当没有 —— 菜单里先不显示「粘贴」，
   * 复制 / 剪切或成功 Ctrl+V 一次之后自然会显出来。
   */
  useEffect(() => {
    let alive = true
    void readText().then((text) => {
      if (alive && parseClipboard(text)) setHasClipboard(true)
    })
    return () => {
      alive = false
    }
  }, [])

  const copy = useCallback((ids: Iterable<string>): number => {
    const d = depsRef.current
    const set = new Set(ids)
    if (set.size === 0) return 0
    const payload = buildClipboardPayload(
      d.graph.nodes.filter((n) => set.has(n.id)).map((n) => structuredClone(n)),
      d.graph.edges
        .filter((e) => set.has(e.source) && set.has(e.target))
        .map((e) => structuredClone(e)),
    )
    clipboardRef.current = payload
    setHasClipboard(true)
    void copyText(serializeClipboard(payload)).catch(() => {
      d.pushToast('error', '写入系统剪贴板失败（画布内剪贴板仍可用）')
    })
    return payload.nodes.length
  }, [])

  const copySelection = useCallback((): number => copy(depsRef.current.getSelectionIds()), [copy])

  const cut = useCallback(
    (ids: Iterable<string>): number => {
      const list = [...ids]
      const copied = copy(list)
      // 先复制后删：复制失败（空选择）就不动图，别把节点删没了却什么都没进剪贴板
      if (copied === 0) return 0
      depsRef.current.removeMany(list)
      return copied
    },
    [copy],
  )

  /** 取当前剪贴板内容：内存与系统剪贴板都读，谁新听谁的；两份都没有就 ``null``。 */
  const takeClipboard = useCallback(async (): Promise<ClipboardPayload | null> => {
    const local =
      clipboardRef.current && clipboardRef.current.nodes.length > 0 ? clipboardRef.current : null
    const system = parseClipboard(await readText())
    const payload = pickFresherClipboard(local, system)
    if (!payload) return null
    clipboardRef.current = payload
    setHasClipboard(true)
    if (payload !== local) {
      // 用的是系统剪贴板那一份：说清它是哪儿来的、什么时候拷的
      const when = formatCopiedAt(payload.copiedAt)
      if (when) {
        depsRef.current.pushToast(
          'success',
          `已从系统剪贴板取回 ${payload.nodes.length} 个节点（复制于 ${when}）`,
        )
      }
    }
    return payload
  }, [])

  /**
   * 剪贴板内容 -> 待放置的一组：节点坐标先落到快照上（旧节点用 localStorage 迁来的兜底坐标），
   * 组包围盒中心当作「鼠标抓着的那一点」。``at`` 省略时就用组中心（原位粘贴）。
   */
  const placingFrom = useCallback((clip: ClipboardPayload, at?: Point): Placing => {
    const { positions } = depsRef.current
    const base = (n: WorkflowNode): Point => positions[n.id] ?? { x: n.x ?? 0, y: n.y ?? 0 }
    const nodes = clip.nodes.map((n) => {
      const b = base(n)
      return { ...n, x: b.x, y: b.y }
    })
    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    for (const n of nodes) {
      const nx = n.x ?? 0
      const ny = n.y ?? 0
      minX = Math.min(minX, nx)
      minY = Math.min(minY, ny)
      maxX = Math.max(maxX, nx + NODE_W)
      maxY = Math.max(maxY, ny + nodeHeight(nodeDef(n.type, n.config)))
    }
    const cx = (minX + maxX) / 2
    const cy = (minY + maxY) / 2
    const target = at ?? { x: cx, y: cy }
    return { nodes, edges: clip.edges, cx, cy, x: target.x, y: target.y }
  }, [])

  /** 落子：把待放置的一组真正写进图（副本换新 id、组内连线重建，新节点成为选中集合）。 */
  const insertPlacing = useCallback(
    (next: Placing) => {
      const d = depsRef.current
      d.pushUndo()
      const offX = next.x - next.cx
      const offY = next.y - next.cy
      const idMap = new Map<string, string>()
      const newNodes = next.nodes.map((n) => {
        const id = uid(n.type)
        idMap.set(n.id, id)
        return {
          ...n,
          id,
          config: structuredClone(n.config),
          x: (n.x ?? 0) + offX,
          y: (n.y ?? 0) + offY,
        }
      })
      const newEdges = next.edges.map((e) => ({
        ...e,
        source: idMap.get(e.source) ?? e.source,
        target: idMap.get(e.target) ?? e.target,
      }))
      d.setGraph((g) => ({ nodes: [...g.nodes, ...newNodes], edges: [...g.edges, ...newEdges] }))
      d.setSelectedIds(new Set(newNodes.map((n) => n.id)))
      d.setSelectedId(null)
    },
    [],
  )

  const startPlacing = useCallback(() => {
    void takeClipboard().then((clip) => {
      if (!clip || clip.nodes.length === 0) return
      setPlacing(placingFrom(clip, depsRef.current.lastPointer() ?? undefined))
    })
  }, [placingFrom, takeClipboard])

  const pasteAt = useCallback(
    (at: Point) => {
      void takeClipboard().then((clip) => {
        if (!clip || clip.nodes.length === 0) {
          depsRef.current.pushToast('info', '剪贴板里没有可粘贴的节点')
          return
        }
        insertPlacing(placingFrom(clip, at))
        setPlacing(null)
      })
    },
    [insertPlacing, placingFrom, takeClipboard],
  )

  const movePlacing = useCallback((point: Point) => {
    setPlacing((cur) => (cur ? { ...cur, x: point.x, y: point.y } : cur))
  }, [])

  const dropPlacing = useCallback(() => {
    const cur = placingRef.current
    if (!cur) return
    insertPlacing(cur)
    setPlacing(null)
    placingRef.current = null
    // 落子这一下会带出一发补发 click：立牌子别让它当「点空白」清掉刚选中的新节点
    depsRef.current.suppressNextClick()
  }, [insertPlacing])

  const cancelPlacing = useCallback(() => {
    setPlacing(null)
    placingRef.current = null
  }, [])

  const hasLocal = useCallback(
    () => (clipboardRef.current?.nodes.length ?? 0) > 0,
    [],
  )

  return useMemo(
    () => ({
      hasClipboard,
      hasLocal,
      placing,
      copy,
      copySelection,
      cut,
      startPlacing,
      pasteAt,
      movePlacing,
      dropPlacing,
      cancelPlacing,
    }),
    [
      hasClipboard,
      hasLocal,
      placing,
      copy,
      copySelection,
      cut,
      startPlacing,
      pasteAt,
      movePlacing,
      dropPlacing,
      cancelPlacing,
    ],
  )
}
