/**
 * 工作流画布编辑器：把状态（图 / 选中 / 版本 / 校验）与交互编排起来，**画与算不在这里**。
 *
 * 三层数据：暂存区（draft，随时存、不校验）→ 版本（校验后写不可变快照）→ 发布（只挪指针）。
 * 端口约定：trigger 控制流（绿）、message 数据流（蓝），两端类型必须匹配才能连。
 * 节点坐标存在节点 x/y 上，随图一起提交。
 *
 * 文件怎么分的（本文件只留下「编排」）：
 *
 * * ``editor/catalog.ts``          目录与几何：``nodeDef`` / 尺寸 / 连线端点 / 各类纯函数
 * * ``editor/clipboard.ts``        复制粘贴的负载（节点 + 连线 + 复制时刻）与解析
 * * ``editor/useNodeCatalog.tsx``  节点类型目录（后端给的）+ 加载 / 失败占位
 * * ``editor/useGraphHistory.ts``  撤销栈（Ctrl+Z）
 * * ``editor/useCanvasView.ts``    pan / zoom 与坐标换算
 * * ``editor/useCanvasPan.ts``     右键拖动平移（顺带记「点了一下还是拖了一下」）
 * * ``editor/useNodeOps.ts``       改图那一半：加 / 删 / 改配置 / 挪位置 / 图层顺序
 * * ``editor/useNodeDrag.ts``      拖节点（整组一起挪）
 * * ``editor/useBoxSelect.ts``     框选：矩形、实时选中、松手固化图层
 * * ``editor/usePortConnect.ts``   端口拉线与落线（含跟着鼠标走的那条临时线）
 * * ``editor/usePaletteDrag.ts``   从节点库拖出：虚影跟随与落子
 * * ``editor/useEditorClipboard.ts`` 复制 / 剪切 / 粘贴，以及粘贴时的虚影放置
 * * ``editor/useEditorShortcuts.ts`` 画布快捷键（Delete / Ctrl+Z / Ctrl+S / C / X / V）
 * * ``editor/Canvas.tsx``          画布本体（两层变换 + 内容插槽）
 * * ``editor/EdgeLayer.tsx``       连线层（真图与粘贴虚影共用）
 * * ``editor/NodeCard.tsx``        节点卡片（memo 化：拖动时只重渲染被拖的那几个）
 * * ``editor/GhostNode.tsx``       虚影卡片（粘贴预览 / 从节点库拖出）
 * * ``editor/Palette.tsx``         节点面板
 * * ``editor/Inspector.tsx``       配置面板 + 校验报告 + 版本历史
 * * ``editor/Toolbar.tsx``         顶部工具栏
 * * ``editor/menu.tsx``            右键菜单配置表：有什么按钮、什么状态显示什么、点了做什么
 * * ``editor/ContextMenuList.tsx`` 右键菜单渲染器（不认识具体按钮，只看配置）
 * * ``editor/ContextMenuHost.tsx`` 右键菜单的唯一出口：采集状态 + 选配置表
 * * ``editor/useContextMenu.ts``   菜单状态 + 点别处 / Esc 关闭
 * * ``editor/useWorkflowDoc.ts``    暂存 / 校验 / 提交版本 / 发布 / 运行开关（后端那一半）
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import type { WorkflowGraph } from './workflowApi'
import { useToast } from '../../common/Toast'
import { Canvas } from './editor/Canvas'
import { centeredNodePosition } from './editor/canvasGeometry'
import { ContextMenuHost } from './editor/ContextMenuHost'
import { EdgeLayer } from './editor/EdgeLayer'
import { GhostNode } from './editor/GhostNode'
import { Inspector } from './editor/Inspector'
import { NodeCard } from './editor/NodeCard'
import { Palette } from './editor/Palette'
import { Toolbar } from './editor/Toolbar'
import {
  NODE_W,
  effectivePortTypes,
  emptyGraph,
  issuesByNode,
  nodeDef,
  nodeHeight,
  normalizeGraph,
  portEffKey,
  wiredPortsByNode,
  type Point,
  type Positions,
} from './editor/catalog'
import { useContextMenu } from './editor/useContextMenu'
import { useCanvasPan } from './editor/useCanvasPan'
import { useCanvasView } from './editor/useCanvasView'
import { useGraphHistory } from './editor/useGraphHistory'
import { useLatest } from './editor/useLatest'
import { useNodeDrag } from './editor/useNodeDrag'
import { useNodeOps } from './editor/useNodeOps'
import { usePaletteDrag } from './editor/usePaletteDrag'
import { useBoxSelect } from './editor/useBoxSelect'
import { usePortConnect } from './editor/usePortConnect'
import { useEditorClipboard } from './editor/useEditorClipboard'
import { useNodeCatalog } from './editor/useNodeCatalog'
import { useEditorShortcuts } from './editor/useEditorShortcuts'
import { useWorkflowDoc } from './editor/useWorkflowDoc'
import styles from './WorkflowEditor.module.css'

/** 没有接线信息时的空集合：身份固定，别让 memo 化的卡片每次拿到一个新 Set */
const NO_WIRED: Set<string> = new Set()

interface WorkflowEditorProps {
  workflowId: string
  onClose: () => void
}

export default function WorkflowEditor({ workflowId, onClose }: WorkflowEditorProps) {
  const { pushToast } = useToast()

  // ---- 图与后端状态 ----
  const [graph, setGraph] = useState<WorkflowGraph>(emptyGraph())
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // ---- 界面 ----
  const [showPalette, setShowPalette] = useState(true)
  const [showInspector, setShowInspector] = useState(true)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  /** 右键菜单（节点上 / 空白处）：同一时刻只有一份，状态与关闭都在 hook 里，见 useContextMenu */
  const { state: menuState, ref: menuRef, open: openMenuAt, close: closeMenu } = useContextMenu()


  // ---- refs（交互过程中的临时状态，改它不触发渲染）----
  const canvasRef = useRef<HTMLDivElement>(null)
  /** 最近一次画布鼠标位置（画布坐标）：Ctrl+V 进入放置模式时拿它当虚影落点 */
  const lastPointerRef = useRef<Point | null>(null)
  /** 框选结束 / 落子虚影的松手会被浏览器补发一发 click，用它立牌子吞掉（见画布 onClick） */
  const suppressClickRef = useRef(false)
  // ---- hooks ----
  const { pan, zoom, setPan, toCanvas, zoomAt, reset: resetView } = useCanvasView()
  const { pushUndo, undo: popUndo, reset: resetHistory } = useGraphHistory(graph)
  /**
   * 右键拖动平移。起点／「这次是点还是拖」都归它记；拖动一开始就把挂着的菜单收掉
   * （mac 上 contextmenu 是右键按下即发，不在这儿收会出现「菜单挂着、画布还在拖」）。
   */
  const { panning, begin: beginPan, move: movePan, end: endPan, dragged: panDragged } =
    useCanvasPan({ pan, setPan, onDragStart: closeMenu })
  /** 打开工作流：撤销栈归零（Ctrl+Z 不会跨工作流回退） */
  const onLoaded = useCallback(
    (loaded: WorkflowGraph) => {
      resetHistory()
      setGraph(normalizeGraph(loaded))
    },
    [resetHistory],
  )
  const doc = useWorkflowDoc({ workflowId, graph, pushToast, onLoaded })
  const {
    definition,
    versions,
    report,
    loading,
    saving,
    switching,
    drafting,
    validating,
    draft,
    validate,
    save,
    publish,
    toggleEnabled,
  } = doc

  /** 坐标直接从节点 x/y 派生（渲染 / 框选 / 连线都读它）；没存过坐标的节点落在原点。 */
  const positions = useMemo<Positions>(() => {
    const map: Positions = {}
    for (const n of graph.nodes) {
      map[n.id] = {
        x: typeof n.x === 'number' ? n.x : 0,
        y: typeof n.y === 'number' ? n.y : 0,
      }
    }
    return map
  }, [graph.nodes])

  // 一次建索引，别在渲染里对每个节点 / 每条边扫一遍全表（那会让画布变成 O(节点 × 边)）
  const nodeById = useMemo(
    () => new Map(graph.nodes.map((n) => [n.id, n])),
    [graph.nodes],
  )
  const wiredByNode = useMemo(() => wiredPortsByNode(graph.edges), [graph.edges])
  const effTypes = useMemo(() => effectivePortTypes(nodeById, graph.edges), [nodeById, graph.edges])
  // 每个节点的「端口生效类型签名」：一个按值比较的字符串（'|' 分隔，前 inputs 后 outputs）。
  // 直接把 effTypes 这个 Map 传给卡片的话，它的引用随 graph.nodes 变（改一下 config 就算），
  // memo 化的卡片会整片重渲染 —— 传签名则只有自己端口类型真变了的卡片才重渲染。
  const effSigByNode = useMemo(() => {
    const map = new Map<string, string>()
    for (const node of graph.nodes) {
      const def = nodeDef(node.type, node.config)
      map.set(
        node.id,
        [
          ...def.inputs.map((p) => effTypes.get(portEffKey(node.id, 'in', p.id)) ?? p.type),
          ...def.outputs.map((p) => effTypes.get(portEffKey(node.id, 'out', p.id)) ?? p.type),
        ].join('|'),
      )
    }
    return map
  }, [graph.nodes, effTypes])
  const errorByNode = useMemo(() => issuesByNode(report), [report])

  // 回调里要读最新值又不换身份（换身份会让 memo 化的卡片白重渲染），统一走 ref
  const graphRef = useLatest(graph)
  const positionsRef = useLatest(positions)
  const selectedIdsRef = useLatest(selectedIds)

  /**
   * 「点击添加」的落点：当前可视区中央对应的节点左上角（见 ``useNodeOps.centerOf``）。
   * 要读画布 DOM 矩形与当前 pan / zoom —— 那是视图层的事，留在编排层算好递进去。
   */
  const centerOf = useCallback(
    (size: { width: number; height: number }) => {
      const viewport = canvasRef.current?.getBoundingClientRect()
      if (!viewport) return null
      return centeredNodePosition(viewport, size, pan, zoom)
    },
    [pan, zoom],
  )

  /** 改图的那一半（加 / 删 / 改配置 / 挪位置 / 图层顺序）都在 useNodeOps 里。 */
  const ops = useNodeOps({
    pushUndo,
    setGraph,
    setSelectedId,
    setSelectedIds,
    centerOf,
  })

  const drag = useNodeDrag({
    moveNodes: ops.moveNodes,
    bringToFront: ops.bringToFront,
    pushUndo,
    setSelectedId,
    setSelectedIds,
  })

  /** 框选：矩形与「框住了谁」都在 useBoxSelect 里，这里只把图与坐标递进去。 */
  const box = useBoxSelect({
    graph,
    positions,
    setSelectedIds,
    bringToFront: ops.bringToFront,
  })

  // ---- 载入 ----
  /** 节点类型目录：拉目录 + 加载占位都在 useNodeCatalog 里（它要等图也加载完）。 */
  const nodeCatalog = useNodeCatalog(loading)

  // ---- 节点面板 ----
  /** 屏幕坐标 -> 画布坐标；指针不在画布可视区内返回 null（拖拽落点判定用） */
  const toCanvasAt = useCallback(
    (clientX: number, clientY: number) => {
      const rect = canvasRef.current?.getBoundingClientRect()
      return rect ? toCanvas(clientX, clientY, rect) : null
    },
    [toCanvas],
  )

  /** 节点库拖出 / 点击添加：虚影跟随与落子都在 usePaletteDrag 里。 */
  const paletteDrag = usePaletteDrag({ add: ops.add, toCanvasAt })

  // ---- 选择集合 ----
  /** 当前选择集合：优先框选集合，其次单击选中的那个（删除 / 复制粘贴同一口径）。 */
  const getSelectionIds = useCallback((): Set<string> => {
    if (selectedIds.size > 0) return selectedIds
    return selectedId ? new Set([selectedId]) : new Set<string>()
  }, [selectedIds, selectedId])

  // ---- 剪贴板 ----
  /** 复制 / 剪切 / 粘贴与那套「虚影放置」，都在 useEditorClipboard 里。 */
  const clip = useEditorClipboard({
    graph,
    positions,
    pushUndo,
    setGraph,
    setSelectedId,
    setSelectedIds,
    pushToast,
    getSelectionIds,
    removeMany: ops.removeMany,
    lastPointer: () => lastPointerRef.current,
    suppressNextClick: () => {
      suppressClickRef.current = true
    },
  })
  /** 回调里要读最新的「有没有在放置」，又不想让回调换身份（见 useLatest） */
  const placingRef = useLatest(clip.placing)

  // ---- 画布事件 ----
  /** 点空白 / 右键空白：同一件事（清空选中） */
  const clearSelection = useCallback(() => {
    setSelectedId(null)
    setSelectedIds(new Set())
  }, [])

  /**
   * 右键的**唯一入口**：节点与空白共用（``nodeId === null`` = 空白）。
   *
   * 这里只做「按目标决定弹哪一份菜单」这件事 —— 菜单状态与关闭归 useContextMenu，
   * 「这一下是点还是拖（拖动平移就不弹）」归 useCanvasPan，两边都不在这函数里自己记账。
   */
  const onContextMenu = useCallback(
    (e: React.MouseEvent, nodeId: string | null) => {
      e.preventDefault()
      e.stopPropagation() // 节点上的右键不再冒泡到画布，免得又弹一份空白菜单
      if (panDragged()) return // 这一发右键是拖动平移收尾，不弹菜单
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return
      // 右键那一处的画布坐标：空白菜单拿它当新节点落点，「粘贴」拿它当整组副本的中心
      const point = toCanvas(e.clientX, e.clientY, rect)

      if (nodeId) {
        // 点在框选集合内 = 对整组操作；集合外 = 先让它成为当前选择（只它一个）
        const current = selectedIdsRef.current
        const ids = current.has(nodeId) ? [...current] : [nodeId]
        setSelectedId(nodeId)
        if (!current.has(nodeId) && current.size > 0) setSelectedIds(new Set())
        openMenuAt({ kind: 'node', x: e.clientX, y: e.clientY, ids, point })
        return
      }

      // 空白处：与左键点空白同义（清空选中）
      if (!nodeCatalog.palette) return // 目录还没拉回来：没有可加的节点，这份菜单画出来也是空的
      clearSelection()
      openMenuAt({ kind: 'canvas', x: e.clientX, y: e.clientY, point })
    },
    [panDragged, nodeCatalog.palette, openMenuAt, clearSelection, toCanvas],
  )

  /** 节点卡片那份签名（``nodeId`` 必填）；空白处那份见下 */
  const onNodeContextMenu = onContextMenu
  const onCanvasContextMenu = useCallback(
    (e: React.MouseEvent) => onContextMenu(e, null),
    [onContextMenu],
  )

  const onNodeMouseDown = useCallback(
    (e: React.MouseEvent, nodeId: string) => {
      if (e.button !== 0) return // 非左键交给画布处理（右键平移）
      if (placingRef.current) return // 放置模式：左键让给画布落子（不 stopPropagation，冒泡上去）
      if ((e.target as HTMLElement).dataset.role === 'port') return
      e.stopPropagation()
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return
      drag.start({
        nodeId,
        canvasPoint: toCanvas(e.clientX, e.clientY, rect),
        positions: positionsRef.current,
        selectedIds: selectedIdsRef.current,
        graph: graphRef.current,
      })
    },
    [drag, toCanvas],
  )

  const onNodeClick = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    // 落子虚影带出的补发 click：落在节点上也算消费掉，别留到下次点空白
    suppressClickRef.current = false
  }, [])

  const onCanvasMouseDown = (e: React.MouseEvent) => {
    if (e.button === 2) {
      // 右键：按下即开始平移；松手那发 contextmenu 靠 panDragged() 判断该不该弹菜单
      e.preventDefault()
      beginPan(e)
      return
    }
    if (e.button !== 0) return
    if (clip.placing) {
      // 放置模式：这一下左键就是「落子」，不进框选
      e.preventDefault()
      clip.dropPlacing()
      return
    }
    // 左键点空白：准备框选（需要拖动超过阈值才真正开始）
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    box.start(toCanvas(e.clientX, e.clientY, rect))
  }

  const onCanvasMouseMove = (e: React.MouseEvent) => {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    if (movePan(e)) return // 正在右键平移：这一发移动归它管
    const point = toCanvas(e.clientX, e.clientY, rect)
    lastPointerRef.current = point
    if (clip.placing) {
      // 放置模式：虚影组中心跟着鼠标走
      clip.movePlacing(point)
      return
    }
    if (box.move(point)) return // 框选手势：这一发移动归它管
    if (drag.active()) drag.move(point)
    connect.move(point)
  }

  const onCanvasMouseUp = () => {
    // 真的拖动过节点：松手时把「拖动前」快照记进撤销栈（一次拖动 = 一步）
    drag.finish()
    // 真正拖出过框选：松手后浏览器会补发一发 click，先立牌子让 onClick 跳过清空，
    // 否则刚框选中的节点会被它故意清掉（普通点击不立牌子——那发 click 正是取消选中要用的）
    if (box.finish()) suppressClickRef.current = true
    connect.cancel() // 松手在空白处：拉到一半的线收掉
    endPan()
  }

  const onCanvasWheel = (e: React.WheelEvent) => {
    e.preventDefault()
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    const mx = e.clientX - rect.left
    const my = e.clientY - rect.top
    zoomAt(zoom * (1 + -e.deltaY * 0.0015), mx, my)
  }

  /** 点空白：清空选中（框选 / 落子刚补发的那一发不算） */
  const onCanvasClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      return
    }
    clearSelection()
  }

  // ---- 端口连线 ----
  /** 拉线 / 落线 / 那条跟着鼠标走的临时线，都在 usePortConnect 里。 */
  const connect = usePortConnect({
    graph,
    setGraph,
    pushUndo,
    pushToast,
    toCanvasAt,
    blocked: () => placingRef.current !== null, // 放置模式：端口让路，左键归画布落子
    nodeById,
    positions,
    effTypes,
  })


  // ---- 撤销 / 快捷键 ----



  /** Ctrl+Z：弹回上一份快照；选中态收敛到快照里仍存在的节点。 */
  const undo = useCallback(() => {
    const snapshot = popUndo()
    if (!snapshot) return
    setGraph(snapshot)
    const ids = new Set(snapshot.nodes.map((n) => n.id))
    setSelectedIds((cur) => {
      const next = new Set([...cur].filter((id) => ids.has(id)))
      return next.size === cur.size ? cur : next
    })
    setSelectedId((cur) => (cur && !ids.has(cur) ? null : cur))
  }, [popUndo])

  // 画布快捷键：Delete / Ctrl+Z / Ctrl+S / Ctrl+C / Ctrl+X / Ctrl+V，都在 useEditorShortcuts 里
  useEditorShortcuts({
    isPlacing: () => clip.placing !== null,
    cancelPlacing: clip.cancelPlacing,
    draft,
    drafting,
    undo,
    copySelection: clip.copySelection,
    cut: clip.cut,
    getSelectionIds,
    startPlacing: clip.startPlacing,
    hasLocalClipboard: clip.hasLocal,
    removeMany: ops.removeMany,
  })

  // ---- 渲染辅助 ----
  /** 节点库拖出的虚影（null = 没在拖 / 不在画布上） */
  const paletteGhost = paletteDrag.ghost
  /** 粘贴虚影那一组（null = 没在放置模式） */
  const ghostPlacing = clip.placing
  const selectedNode = selectedId ? nodeById.get(selectedId) ?? null : null
  const selectedDef = selectedNode ? nodeDef(selectedNode.type, selectedNode.config) : null
  const selectedWired = (selectedId ? wiredByNode.get(selectedId) : undefined) ?? NO_WIRED

  // 连线层按 id 取坐标：直接闭包读当前 positions（用 ref 会慢一帧，拖动时线会跟不上节点）
  const posOf = useCallback((id: string) => positions[id], [positions])

  return (
    <div className={styles.page}>
      <Toolbar
        definition={definition}
        drafting={drafting}
        validating={validating}
        saving={saving}
        switching={switching}
        zoomPercent={Math.round(zoom * 100)}
        showPalette={showPalette}
        showInspector={showInspector}
        onClose={onClose}
        onDraft={draft}
        onValidate={validate}
        onSave={save}
        onPublish={publish}
        onToggleEnabled={toggleEnabled}
        onTogglePalette={() => setShowPalette((v) => !v)}
        onToggleInspector={() => setShowInspector((v) => !v)}
        onResetView={resetView}
      />

      <div className={styles.body}>
        {nodeCatalog.palette !== null && showPalette && (
          <Palette
            items={nodeCatalog.palette}
            onItemMouseDown={paletteDrag.onItemMouseDown}
            onItemClick={paletteDrag.onItemClick}
          />
        )}

        <Canvas
          canvasRef={canvasRef}
          pan={pan}
          zoom={zoom}
          panning={panning}
          placeholder={nodeCatalog.placeholder}
          empty={graph.nodes.length === 0}
          boxSel={box.rect}
          onMouseDown={onCanvasMouseDown}
          onMouseMove={onCanvasMouseMove}
          onMouseUp={onCanvasMouseUp}
          onWheel={onCanvasWheel}
          onClick={onCanvasClick}
          onContextMenu={onCanvasContextMenu}
        >
          <EdgeLayer
            edges={graph.edges}
            nodeById={nodeById}
            posOf={posOf}
            onDelete={ops.removeEdge}
            pending={connect.pending}
            effTypes={effTypes}
          />

          {graph.nodes.map((node) => {
            const pos = positions[node.id] ?? { x: 0, y: 0 }
            return (
              <NodeCard
                key={node.id}
                node={node}
                x={pos.x}
                y={pos.y}
                selected={selectedId === node.id}
                boxSelected={selectedIds.has(node.id)}
                issues={errorByNode.get(node.id) ?? null}
                wired={wiredByNode.get(node.id) ?? NO_WIRED}
                effSig={effSigByNode.get(node.id) ?? ''}
                onMouseDown={onNodeMouseDown}
                onClick={onNodeClick}
                onContextMenu={onNodeContextMenu}
                onPortMouseDown={connect.start}
                onPortMouseUp={connect.drop}
              />
            )
          })}

          {/* 粘贴虚影：组内连线 + 节点预览（渲染在真实节点之后，左键落子 / Esc 取消） */}
          {ghostPlacing && (() => {
            const offX = ghostPlacing.x - ghostPlacing.cx
            const offY = ghostPlacing.y - ghostPlacing.cy
            const ghostById = new Map(ghostPlacing.nodes.map((n) => [n.id, n]))
            /** 虚影节点位置（快照坐标 + 当前偏移）：连线按它算端口坐标 */
            const ghostPosOf = (id: string): Point | undefined => {
              const n = ghostById.get(id)
              return n ? { x: (n.x ?? 0) + offX, y: (n.y ?? 0) + offY } : undefined
            }
            return (
              <>
                <EdgeLayer edges={ghostPlacing.edges} nodeById={ghostById} posOf={ghostPosOf} faint />
                {ghostPlacing.nodes.map((n) => (
                  <GhostNode
                    // 加前缀：复制场景下虚影 id 与图里原节点相同，直接当 key 会撞车
                    key={`ghost-${n.id}`}
                    type={n.type}
                    config={n.config}
                    left={(n.x ?? 0) + offX}
                    top={(n.y ?? 0) + offY}
                  />
                ))}
              </>
            )
          })()}

          {/* 节点库拖出的新节点虚影（中心跟着鼠标；松手在画布上才真正添加） */}
          {paletteGhost && (() => {
            const def = nodeDef(paletteGhost.type)
            return (
              <GhostNode
                type={paletteGhost.type}
                left={paletteGhost.x - NODE_W / 2}
                top={paletteGhost.y - nodeHeight(def) / 2}
              />
            )
          })()}
        </Canvas>

        {nodeCatalog.palette !== null && showInspector && (
          <Inspector
            node={selectedNode}
            def={selectedDef}
            wired={selectedWired}
            effTypes={effTypes}
            report={report}
            versions={versions}
            onUpdate={ops.updateConfig}
            onDelete={ops.remove}
          />
        )}
      </div>

      {/*
        右键菜单：内容与「什么状态显示什么」都在 editor/menu.tsx 的配置表里，这里只把动作的实现
        和采集到的状态递进去（点完任一项由渲染器统一收菜单）。
      */}
      <ContextMenuHost
        state={menuState}
        menuRef={menuRef}
        catalog={nodeCatalog.palette}
        canPaste={clip.hasClipboard}
        actions={{
          cut: clip.cut,
          copy: clip.copy,
          paste: clip.pasteAt,
          remove: ops.removeMany,
          add: ops.add,
        }}
        onClose={closeMenu}
      />
    </div>
  )
}
