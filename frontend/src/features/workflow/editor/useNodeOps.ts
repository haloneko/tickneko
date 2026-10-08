/**
 * 节点操作：加 / 删 / 改配置 / 挪位置 / 图层顺序。
 *
 * 这是**改图**的那一半：画布上的交互（拖节点 / 框选 / 连线 / 剪贴板）最终都落到这里，
 * 自己不碰鼠标事件。两条约定：
 *
 * * **会改图的操作都在改图之前压一步撤销**（``pushUndo``），一步操作 = 一步 Ctrl+Z；
 * * 改配置那条用 ``cfg:{id}:{key}`` 当合并键 —— 连续打字不逐字符占栈（见 useGraphHistory）。
 *
 * 依赖（``pushUndo`` / ``setGraph`` / 两个 setSelected / ``centerOf``）以对象传入并存进 ref：
 * 本 hook 返回的回调因此**身份稳定**，传给 memo 化的节点卡片不会让它白重渲染。
 */
import { useCallback, useEffect, useMemo, useRef, type Dispatch, type SetStateAction } from 'react'
import {
  NODE_W,
  nodeDef,
  nodeHeight,
  uid,
  type Point,
  type Positions,
  type WorkflowGraph,
} from './catalog'
import type { WorkflowEdge, WorkflowNode } from '../workflowApi'

/** 节点尺寸（``centerOf`` 用它算「视野中央对应的左上角」）。 */
export interface NodeSize {
  width: number
  height: number
}

export interface NodeOpsDeps {
  pushUndo: (snapshot?: WorkflowGraph, coalesceKey?: string) => void
  setGraph: Dispatch<SetStateAction<WorkflowGraph>>
  setSelectedId: Dispatch<SetStateAction<string | null>>
  setSelectedIds: Dispatch<SetStateAction<Set<string>>>
  /**
   * 「点击添加」的落点：当前可视区中央对应的节点左上角。
   *
   * 为什么要外面算：它要读画布的 DOM 矩形与当前 pan / zoom，那是视图层的事，
   * 本 hook 不认识 DOM。拿不到视口就返回 ``null``（调用方据此放弃添加）。
   */
  centerOf: (size: NodeSize) => Point | null
}

export interface NodeOps {
  /** 加一个节点：``pos`` 给值 = 拖拽落点（按中心算），不给 = 落在当前视野中央 */
  add: (type: string, pos?: Point) => void
  /** 删一个节点（连带两端连线） */
  remove: (id: string) => void
  /** 按 id 批量删（连带两端连线），并清掉指向它们的选中态 */
  removeMany: (ids: string[]) => void
  /** 删一条连线 */
  removeEdge: (edge: WorkflowEdge) => void
  /** 改一个配置字段 */
  updateConfig: (id: string, key: string, value: unknown) => void
  /** 批量挪位置（拖节点用）：直接改节点 x/y，随图一起持久化 */
  moveNodes: (next: Positions) => void
  /**
   * 提到图层最上：把节点挪到数组末尾（渲染序 = DOM 序 = 图层序）。
   * 不是临时样式——松手 / 取消选中后顺序依然保持。
   */
  bringToFront: (ids: Iterable<string>) => void
}

export function useNodeOps(deps: NodeOpsDeps): NodeOps {
  const depsRef = useRef(deps)
  useEffect(() => {
    depsRef.current = deps
  })

  const add = useCallback((type: string, pos?: Point) => {
    const d = depsRef.current
    const def = nodeDef(type)
    const height = nodeHeight(def)
    // 拖拽落点按中心对齐；点击添加固定在当前屏幕视野中央
    const position = pos
      ? { x: pos.x - NODE_W / 2, y: pos.y - height / 2 }
      : d.centerOf({ width: NODE_W, height })
    if (!position) return
    const id = uid(type)
    d.pushUndo()
    const node: WorkflowNode = {
      id,
      type: def.type,
      config: { ...def.defaults },
      ...position,
    }
    d.setGraph((g) => ({ ...g, nodes: [...g.nodes, node] }))
    d.setSelectedId(id)
  }, [])

  const remove = useCallback((id: string) => {
    const d = depsRef.current
    d.pushUndo()
    d.setGraph((g) => ({
      nodes: g.nodes.filter((n) => n.id !== id),
      edges: g.edges.filter((e) => e.source !== id && e.target !== id),
    }))
    d.setSelectedId((cur) => (cur === id ? null : cur))
  }, [])

  const removeMany = useCallback((ids: string[]) => {
    if (ids.length === 0) return
    const d = depsRef.current
    const set = new Set(ids)
    d.pushUndo()
    d.setGraph((g) => ({
      nodes: g.nodes.filter((n) => !set.has(n.id)),
      edges: g.edges.filter((e) => !set.has(e.source) && !set.has(e.target)),
    }))
    d.setSelectedIds((cur) => {
      const next = new Set([...cur].filter((id) => !set.has(id)))
      return next.size === cur.size ? cur : next
    })
    d.setSelectedId((cur) => (cur && set.has(cur) ? null : cur))
  }, [])

  const removeEdge = useCallback((edge: WorkflowEdge) => {
      const d = depsRef.current
      d.pushUndo()
      d.setGraph((g) => ({
        ...g,
        edges: g.edges.filter(
          (e) =>
            !(
              e.source === edge.source &&
              e.target === edge.target &&
              e.sourcePort === edge.sourcePort &&
              e.targetPort === edge.targetPort
            ),
        ),
      }))
  }, [])

  const updateConfig = useCallback((id: string, key: string, value: unknown) => {
    const d = depsRef.current
    // 连续打字合并成一步撤销（同一节点的同一字段）
    d.pushUndo(undefined, `cfg:${id}:${key}`)
    d.setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => (n.id === id ? { ...n, config: { ...n.config, [key]: value } } : n)),
    }))
  }, [])

  const moveNodes = useCallback((next: Positions) => {
    depsRef.current.setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => {
        const p = next[n.id]
        return p ? { ...n, x: p.x, y: p.y } : n
      }),
    }))
  }, [])

  const bringToFront = useCallback((ids: Iterable<string>) => {
    const set = new Set(ids)
    if (set.size === 0) return
    depsRef.current.setGraph((g) => {
      const front = g.nodes.filter((n) => set.has(n.id))
      if (front.length === 0) return g
      const rest = g.nodes.filter((n) => !set.has(n.id))
      const next = [...rest, ...front]
      // 本来就在末尾（相对顺序没变）就不动，省一次重渲染
      if (next.every((n, i) => n === g.nodes[i])) return g
      return { ...g, nodes: next }
    })
  }, [])

  return useMemo(
    () => ({ add, remove, removeMany, removeEdge, updateConfig, moveNodes, bringToFront }),
    [add, remove, removeMany, removeEdge, updateConfig, moveNodes, bringToFront],
  )
}
