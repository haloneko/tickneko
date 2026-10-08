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
 * * ``editor/useGraphHistory.ts``  撤销栈（Ctrl+Z）
 * * ``editor/useCanvasView.ts``    pan / zoom 与坐标换算
 * * ``editor/useNodeDrag.ts``      拖节点（整组一起挪）
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
 * * ``editor/useCanvasPan.ts``     右键拖动平移（顺带记「点了一下还是拖了一下」）
 * * ``editor/useWorkflowDoc.ts``    暂存 / 校验 / 提交版本 / 发布 / 运行开关（后端那一半）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchNodeCatalog,
  type NodeTypeSpec,
  type PortType,
  type WorkflowEdge,
  type WorkflowGraph,
  type WorkflowNode,
} from './workflowApi'
import { useToast } from '../../common/Toast'
import { copyText, readText } from '../../lib/clipboard'
import { Canvas, type BoxRect } from './editor/Canvas'
import { centeredNodePosition } from './editor/canvasGeometry'
import { ContextMenuHost } from './editor/ContextMenuHost'
import {
  buildClipboardPayload,
  formatCopiedAt,
  parseClipboard,
  pickFresherClipboard,
  serializeClipboard,
  type ClipboardPayload,
} from './editor/clipboard'
import { EdgeLayer } from './editor/EdgeLayer'
import { GhostNode } from './editor/GhostNode'
import { Inspector } from './editor/Inspector'
import { NodeCard } from './editor/NodeCard'
import { Palette } from './editor/Palette'
import { Toolbar } from './editor/Toolbar'
import {
  NODE_W,
  edgeCurve,
  effectivePortTypes,
  emptyGraph,
  installCatalog,
  isEditingTarget,
  issuesByNode,
  nodeDef,
  nodeHeight,
  normalizeGraph,
  portAbsPos,
  portColor,
  portCompatible,
  portEffKey,
  uid,
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
import { useWorkflowDoc } from './editor/useWorkflowDoc'
import styles from './WorkflowEditor.module.css'

/** 没有接线信息时的空集合：身份固定，别让 memo 化的卡片每次拿到一个新 Set */
const NO_WIRED: Set<string> = new Set()

/**
 * 「待放置的一组节点」：两种贴法共用这份几何 —— Ctrl+V 先拿它当虚影（跟鼠标走），
 * 右键「粘贴」直接拿它落子。
 *
 * ``x``/``y`` = 组中心当前对准的画布坐标；``cx``/``cy`` = 组中心在这组快照坐标里的位置，
 * 两者之差就是整组要平移的偏移。
 */
interface Placing {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  cx: number
  cy: number
  x: number
  y: number
}

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
  const [boxSel, setBoxSel] = useState<BoxRect | null>(null)
  /** 右键菜单（节点上 / 空白处）：同一时刻只有一份，状态与关闭都在 hook 里，见 useContextMenu */
  const { state: menuState, ref: menuRef, open: openMenuAt, close: closeMenu } = useContextMenu()
  /**
   * 粘贴虚影（Ctrl+V 放置模式）：剪贴板内容先以半透明预览跟鼠标走，左键落子才真正放图。
   * 右键菜单的「粘贴」不走这一步 —— 鼠标已经指名了落点，一步到位（见 ``pasteAt``）。
   */
  const [placing, setPlacing] = useState<Placing | null>(null)
  /**
   * 剪贴板里有没有能贴的东西 —— 右键菜单据此决定**显不显示「粘贴」**。
   *
   * 它只是界面用的镜像（真值在 ``clipboardRef`` 与系统剪贴板里）：复制 / 剪切写内存时置真；
   * 打开编辑器时探一次系统剪贴板，把上次会话 / 别的标签页复制过的那份也算上。
   */
  const [hasClipboard, setHasClipboard] = useState(false)
  /** 节点库拖出的新节点虚影：x/y = 鼠标的画布坐标（null = 没拖 / 不在画布上） */
  const [newDrag, setNewDrag] = useState<{ type: string; x: number; y: number } | null>(null)
  /**
   * 节点类型目录（后端给的）：拉回来之前**不渲染画布** —— 认不出类型就画不出端口。
   * 失败也不退回一份前端定义（那正是以前漂移的来源），只给一个重试。
   */
  const [palette, setPalette] = useState<NodeTypeSpec[] | null>(null)
  const [catalogFailed, setCatalogFailed] = useState(false)

  // ---- refs（交互过程中的临时状态，改它不触发渲染）----
  const canvasRef = useRef<HTMLDivElement>(null)
  /** 最近一次画布鼠标位置（画布坐标）：Ctrl+V 进入放置模式时拿它当虚影落点 */
  const lastPointerRef = useRef<Point | null>(null)
  /** 框选起点（画布坐标） */
  const boxRef = useRef<Point | null>(null)
  /** 本次空白拖拽是否已越过阈值进入框选（松手时区分「点了一下」与「框选完」） */
  const boxMovedRef = useRef(false)
  /** 框选结束的松手会被浏览器补发一发 click，用它立牌子吞掉（见画布 onClick） */
  const suppressClickRef = useRef(false)
  /**
   * 画布内部剪贴板：Ctrl+C / Ctrl+X 存这里的节点 + 组内连线，Ctrl+V 以虚影放置。
   *
   * 这是**第一重**保险（内存里那份，读写都是同步的）；同一份内容还会写进系统剪贴板
   * （见 copySelection），刷新页面后靠它把内容捞回来。
   */
  const clipboardRef = useRef<ClipboardPayload | null>(null)
  /** 正在拖出的连线：起点端口信息 + 鼠标位置 */
  const connectRef = useRef<{
    nodeId: string
    portId: string
    portType: PortType
    direction: 'in' | 'out'
  } | null>(null)
  const [connectCursor, setConnectCursor] = useState<Point | null>(null)

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
  const placingRef = useLatest(placing)

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

  // ---- 载入 ----
  /** 拉节点目录：面板 / 端口 / 配置字段都按它渲染（只读后端内存里那张注册表，不碰库）。 */
  const loadCatalog = useCallback(async () => {
    setCatalogFailed(false)
    try {
      const { data } = await fetchNodeCatalog()
      setPalette(installCatalog(data))
    } catch {
      setCatalogFailed(true)
    }
  }, [])

  useEffect(() => {
    void loadCatalog()
  }, [loadCatalog])

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

  // ---- 节点面板 ----
  /**
   * 节点库选项按下：拖进画布 → 虚影跟随、松手落子；没拖（纯点击）→ 和以前一样直接添加。
   * 监听挂在 window 上：拖拽路径大半在画布外（左侧栏），画布上的 mousemove 收不到。
   */
  const onPaletteMouseDown = (e: React.MouseEvent, type: string) => {
    if (e.button !== 0) return
    e.preventDefault() // 防文本选中 / 原生拖拽
    const dragStart = { startX: e.clientX, startY: e.clientY, armed: false }
    /** 鼠标在画布可视区里就返回画布矩形（用于坐标换算），否则 null */
    const insideCanvas = (ev: MouseEvent) => {
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return null
      if (
        ev.clientX < rect.left ||
        ev.clientX > rect.right ||
        ev.clientY < rect.top ||
        ev.clientY > rect.bottom
      ) {
        return null
      }
      return rect
    }
    const onMove = (ev: MouseEvent) => {
      if (!dragStart.armed) {
        // 位移超过阈值才算「拖拽」，没超过就还是「点击」
        if (Math.abs(ev.clientX - dragStart.startX) + Math.abs(ev.clientY - dragStart.startY) < 5) {
          return
        }
        dragStart.armed = true
      }
      const rect = insideCanvas(ev)
      if (!rect) {
        setNewDrag(null) // 不在画布上：虚影收起
        return
      }
      setNewDrag({ type, ...toCanvas(ev.clientX, ev.clientY, rect) })
    }
    const onUp = (ev: MouseEvent) => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      if (!dragStart.armed) {
        ops.add(type) // 纯点击：出现在当前可视区中央
        return
      }
      setNewDrag(null)
      const rect = insideCanvas(ev)
      if (!rect) return // 松手在画布外：取消，不添加
      ops.add(type, toCanvas(ev.clientX, ev.clientY, rect))
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  /** 键盘（Enter/Space）触发的 click：detail 为 0；鼠标的交给 mousedown/mouseup 流程 */
  const onPaletteItemClick = (e: React.MouseEvent, type: string) => {
    if (e.detail === 0) ops.add(type)
  }

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
      if (!palette) return // 目录还没拉回来：没有可加的节点，这份菜单画出来也是空的
      clearSelection()
      openMenuAt({ kind: 'canvas', x: e.clientX, y: e.clientY, point })
    },
    [panDragged, palette, openMenuAt, clearSelection, toCanvas],
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
    if (placing) {
      // 放置模式：这一下左键就是「落子」，不进框选
      e.preventDefault()
      dropPlacing()
      return
    }
    // 左键点空白：准备框选（需要拖动超过阈值才真正开始）
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    boxRef.current = toCanvas(e.clientX, e.clientY, rect)
  }

  const onCanvasMouseMove = (e: React.MouseEvent) => {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    if (movePan(e)) return // 正在右键平移：这一发移动归它管
    const point = toCanvas(e.clientX, e.clientY, rect)
    lastPointerRef.current = point
    if (placing) {
      // 放置模式：虚影组中心跟着鼠标走
      setPlacing({ ...placing, x: point.x, y: point.y })
      return
    }
    if (boxRef.current) {
      const start = boxRef.current
      const dx = point.x - start.x
      const dy = point.y - start.y
      // 拖动超过阈值才显示框选
      if (!boxSel && Math.abs(dx) < 5 && Math.abs(dy) < 5) return
      boxMovedRef.current = true
      if (!boxSel) {
        setBoxSel({ x0: start.x, y0: start.y, x1: point.x, y1: point.y })
      } else {
        setBoxSel({ ...boxSel, x1: point.x, y1: point.y })
      }
      // 实时计算选中
      const x0 = Math.min(start.x, point.x)
      const y0 = Math.min(start.y, point.y)
      const x1 = Math.max(start.x, point.x)
      const y1 = Math.max(start.y, point.y)
      const ids = new Set<string>()
      for (const n of graph.nodes) {
        const p = positions[n.id]
        if (!p) continue
        const h = nodeHeight(nodeDef(n.type, n.config))
        if (p.x + NODE_W >= x0 && p.x <= x1 && p.y + h >= y0 && p.y <= y1) ids.add(n.id)
      }
      setSelectedIds(ids)
      return
    }
    if (drag.active()) drag.move(point)
    if (connectRef.current) setConnectCursor(point)
  }

  const onCanvasMouseUp = () => {
    // 真的拖动过节点：松手时把「拖动前」快照记进撤销栈（一次拖动 = 一步）
    drag.finish()
    // 真正拖出过框选：松手后浏览器会补发一发 click，先立牌子让 onClick 跳过清空，
    // 否则刚框选中的节点会被它故意清掉（普通点击不立牌子——那发 click 正是取消选中要用的）
    if (boxRef.current && boxMovedRef.current) {
      suppressClickRef.current = true
      // 框选收尾：整组固化到图层末尾（相对顺序保持原样）
      ops.bringToFront(selectedIds)
    }
    connectRef.current = null
    endPan()
    boxRef.current = null
    boxMovedRef.current = false
    setConnectCursor(null)
    setBoxSel(null)
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
  const onPortMouseDown = useCallback(
    (
      e: React.MouseEvent,
      nodeId: string,
      portId: string,
      portType: PortType,
      direction: 'in' | 'out',
    ) => {
      if (e.button !== 0) return
      if (placingRef.current) return // 放置模式：端口也让路，左键归画布落子
      e.stopPropagation()
      connectRef.current = { nodeId, portId, portType, direction }
      const rect = canvasRef.current?.getBoundingClientRect()
      if (rect) setConnectCursor(toCanvas(e.clientX, e.clientY, rect))
    },
    [toCanvas],
  )

  const onPortMouseUp = useCallback(
    (
      e: React.MouseEvent,
      nodeId: string,
      portId: string,
      portType: PortType,
      direction: 'in' | 'out',
    ) => {
      const pending = connectRef.current
      if (!pending) return
      e.stopPropagation()
      const clear = () => {
        connectRef.current = null
        setConnectCursor(null)
      }
      // 不能连自己
      if (pending.nodeId === nodeId) return clear()
      // 方向必须一进一出
      if (pending.direction === direction) return clear()
      // 类型必须兼容（同类；泛型端口可接任意数据流端口）
      if (!portCompatible(pending.portType, portType)) {
        pushToast('error', `端口类型不匹配：${pending.portType} ≠ ${portType}`)
        return clear()
      }
      // 确定 source / target
      let source: string
      let target: string
      let sourcePort: string
      let targetPort: string
      if (pending.direction === 'out') {
        source = pending.nodeId
        target = nodeId
        sourcePort = pending.portId
        targetPort = portId
      } else {
        source = nodeId
        target = pending.nodeId
        sourcePort = portId
        targetPort = pending.portId
      }
      // 去重：重复连线不占撤销步
      const exists = graphRef.current.edges.some(
        (ed) =>
          ed.source === source &&
          ed.target === target &&
          ed.sourcePort === sourcePort &&
          ed.targetPort === targetPort,
      )
      if (!exists) {
        pushUndo()
        setGraph((g) => ({ ...g, edges: [...g.edges, { source, target, sourcePort, targetPort }] }))
      }
      clear()
    },
    [pushToast, pushUndo],
  )


  // ---- 剪贴板 / 快捷键 ----
  /** 当前选择集合：优先框选集合，其次单击选中的那个（删除 / 复制粘贴同一口径）。 */
  const getSelectionIds = useCallback((): Set<string> => {
    if (selectedIds.size > 0) return selectedIds
    return selectedId ? new Set([selectedId]) : new Set<string>()
  }, [selectedIds, selectedId])

  /**
   * 复制这些节点 + 组内连线；返回复制到的节点数。集合由调用方给：快捷键给的是「当前选择」，
   * 右键菜单给的是「这一次右键的那一组」。
   *
   * **双重保险**：内存里的 clipboardRef（同步可用）+ 系统剪贴板（活过刷新 / 能跨标签页）。
   * 负载里带上**复制时刻**（copiedAt），从系统剪贴板捞回来时能答出这是什么时候拷的。
   * 写系统剪贴板是异步的，也不一定成功（非安全上下文 / 没权限），失败不影响第一重。
   */
  const copyNodes = useCallback(
    (ids: Iterable<string>): number => {
      const set = new Set(ids)
      if (set.size === 0) return 0
      const payload = buildClipboardPayload(
        graph.nodes.filter((n) => set.has(n.id)).map((n) => structuredClone(n)),
        graph.edges
          .filter((e) => set.has(e.source) && set.has(e.target))
          .map((e) => structuredClone(e)),
      )
      clipboardRef.current = payload
      setHasClipboard(true)
      void copyText(serializeClipboard(payload)).catch(() => {
        pushToast('error', '写入系统剪贴板失败（画布内剪贴板仍可用）')
      })
      return payload.nodes.length
    },
    [graph, pushToast],
  )

  /** Ctrl+C / Ctrl+X 走这条：复制的是「当前选择」 */
  const copySelection = useCallback(
    (): number => copyNodes(getSelectionIds()),
    [copyNodes, getSelectionIds],
  )

  /**
   * 剪切 = 复制 + 删除（Ctrl+X / 右键菜单共用）；返回复制到的节点数（0 = 没得剪，调用方别再删）。
   *
   * 先复制后删：复制失败（空选择）就不动图，别把节点删没了却什么都没进剪贴板。
   */
  const cutNodes = useCallback(
    (ids: Iterable<string>): number => {
      const list = [...ids]
      const copied = copyNodes(list)
      if (copied === 0) return 0
      ops.removeMany(list)
      return copied
    },
    [copyNodes, ops.removeMany],
  )

  /**
   * 取当前剪贴板内容：内存与系统剪贴板**都读**，谁新听谁的。
   *
   * * 两份都读到了：用户多半刚在别的标签页 / 别的窗口复制过，比 ``copiedAt``，新的那份赢；
   * * 只读到一份：就用这一份（内存空了说明刷新过页面，只剩系统剪贴板那一份）；
   * * 两份都没读到：返回 ``null``，粘贴什么都不发生。
   *
   * 挑中的那一份顺手存回内存，后面几次 Ctrl+V 不必再读系统剪贴板。
   */
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
        pushToast('success', `已从系统剪贴板取回 ${payload.nodes.length} 个节点（复制于 ${when}）`)
      }
    }
    return payload
  }, [pushToast])

  /**
   * 剪贴板内容 -> 待放置的一组：节点坐标先落到快照上（旧节点用 localStorage 迁来的兜底坐标），
   * 组包围盒中心当作「鼠标抓着的那一点」。``at`` 省略时就用组中心（原位粘贴）。
   */
  const placingFrom = useCallback(
    (clip: ClipboardPayload, at?: Point): Placing => {
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
    },
    [positions],
  )

  /** 落子：把待放置的一组真正写进图（副本换新 id、组内连线重建，新节点成为选中集合）。 */
  const insertPlacing = useCallback(
    (next: Placing) => {
      pushUndo()
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
      setGraph((g) => ({ nodes: [...g.nodes, ...newNodes], edges: [...g.edges, ...newEdges] }))
      setSelectedIds(new Set(newNodes.map((n) => n.id)))
      setSelectedId(null)
    },
    [pushUndo],
  )

  /**
   * Ctrl+V：把剪贴板内容挂成虚影进入「放置模式」——虚影组中心跟着鼠标走，
   * 左键落子（dropPlacing）/ Esc 取消。起点取最近一次画布鼠标位置。
   */
  const startPlacing = useCallback(async () => {
    const clip = await takeClipboard()
    if (!clip || clip.nodes.length === 0) return
    setPlacing(placingFrom(clip, lastPointerRef.current ?? undefined))
  }, [placingFrom, takeClipboard])

  /**
   * 右键「粘贴」：在右键那一处**直接落子**，不做虚影 —— 鼠标已经指名落点了，再让人点一次左键
   * 没有意义（虚影那套是给键盘 Ctrl+V 用的，它没有落点信息）。顺带收掉可能在挂着的虚影。
   */
  const pasteAt = useCallback(
    async (at: Point) => {
      const clip = await takeClipboard()
      if (!clip || clip.nodes.length === 0) {
        pushToast('info', '剪贴板里没有可粘贴的节点')
        return
      }
      insertPlacing(placingFrom(clip, at))
      setPlacing(null)
    },
    [insertPlacing, placingFrom, pushToast, takeClipboard],
  )

  /** 虚影落子：写进图 + 收起虚影（这一步会带出补发 click，见下） */
  const dropPlacing = useCallback(() => {
    if (!placing) return
    insertPlacing(placing)
    setPlacing(null)
    // 落子这一下会带出一发补发 click：立牌子别让它当「点空白」清掉刚选中的新节点
    suppressClickRef.current = true
  }, [insertPlacing, placing])

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

  // 画布快捷键：Delete 删除 / Ctrl+Z 撤销 / Ctrl+S 暂存 / Ctrl+C 复制 / Ctrl+X 剪切 / Ctrl+V 粘贴
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat) return
      const mod = e.ctrlKey || e.metaKey
      const key = e.key.toLowerCase()

      // Esc：正在放置的粘贴虚影取消（不落子）
      if (e.key === 'Escape' && placing) {
        setPlacing(null)
        return
      }

      // Ctrl+S 暂存：输入框里也照常生效（先拦掉浏览器默认的「保存网页」）
      if (mod && key === 's') {
        e.preventDefault()
        if (!drafting) void draft()
        return
      }

      // 输入框 / 下拉里：退格与文本复制粘贴归它们，不抢
      if (isEditingTarget(e.target)) return

      if (mod && key === 'z' && !e.shiftKey) {
        e.preventDefault()
        // 先收掉挂着的虚影，再撤销上一步
        setPlacing(null)
        undo()
        return
      }

      if (mod && (key === 'c' || key === 'x')) {
        // Ctrl+C 复制当前选择；Ctrl+X 走同一条「复制 + 删除」
        const copied = key === 'x' ? cutNodes(getSelectionIds()) : copySelection()
        if (copied === 0) return // 没选中什么就不劫持
        e.preventDefault()
        return
      }

      if (mod && key === 'v') {
        // 两处剪贴板都读（内存 + 系统），谁新用谁；两份都没货就什么都不发生。
        // 内存有货时能同步拦下默认行为；只剩系统剪贴板那一份时要等 promise，
        // 赶不上这一发 preventDefault —— 画布上本来就没有可输入目标（输入框上面
        // 已经放行走了），不拦也不碍事。
        if (clipboardRef.current && clipboardRef.current.nodes.length > 0) e.preventDefault()
        void startPlacing()
        return
      }

      if (e.key === 'Delete' || e.key === 'Backspace') {
        const ids = getSelectionIds()
        if (ids.size === 0) return
        e.preventDefault()
        ops.removeMany([...ids])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [
    drafting,
    draft,
    undo,
    copySelection,
    cutNodes,
    getSelectionIds,
    startPlacing,
    ops.removeMany,
    placing,
  ])

  // ---- 渲染辅助 ----
  const selectedNode = selectedId ? nodeById.get(selectedId) ?? null : null
  const selectedDef = selectedNode ? nodeDef(selectedNode.type, selectedNode.config) : null
  const selectedWired = (selectedId ? wiredByNode.get(selectedId) : undefined) ?? NO_WIRED

  /** 正在拉的临时连线（起点端口 -> 鼠标） */
  const pendingEdge = useMemo(() => {
    const conn = connectRef.current
    if (!conn || !connectCursor) return null
    const node = nodeById.get(conn.nodeId)
    if (!node) return null
    const start = portAbsPos(conn.nodeId, conn.portId, conn.direction, node.type, positions, node.config)
    if (!start) return null
    return {
      path: edgeCurve(start.x, start.y, connectCursor.x, connectCursor.y),
      // 泛型端口拖线也按生效类型显色（placeholder 输出接会话定位就是橙色预览线）
      color: portColor(
        effTypes.get(portEffKey(conn.nodeId, conn.direction, conn.portId)) ?? conn.portType,
      ),
    }
  }, [connectCursor, nodeById, positions, effTypes])

  /** 加载中 / 目录拉不回来：画布位置显示它 */
  const placeholder =
    palette === null || loading ? (
      <div className={styles.loading}>
        {palette !== null ? (
          <>
            <span className="spinner" />
            正在加载…
          </>
        ) : catalogFailed ? (
          <>
            节点类型加载失败
            <button className="btn" onClick={() => void loadCatalog()}>
              重试
            </button>
          </>
        ) : (
          <>
            <span className="spinner" />
            正在加载节点类型…
          </>
        )}
      </div>
    ) : null

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
        {palette !== null && showPalette && (
          <Palette items={palette} onItemMouseDown={onPaletteMouseDown} onItemClick={onPaletteItemClick} />
        )}

        <Canvas
          canvasRef={canvasRef}
          pan={pan}
          zoom={zoom}
          panning={panning}
          placeholder={placeholder}
          empty={graph.nodes.length === 0}
          boxSel={boxSel}
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
            pending={pendingEdge}
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
                onPortMouseDown={onPortMouseDown}
                onPortMouseUp={onPortMouseUp}
              />
            )
          })}

          {/* 粘贴虚影：组内连线 + 节点预览（渲染在真实节点之后，左键落子 / Esc 取消） */}
          {placing && (() => {
            const offX = placing.x - placing.cx
            const offY = placing.y - placing.cy
            const ghostById = new Map(placing.nodes.map((n) => [n.id, n]))
            /** 虚影节点位置（快照坐标 + 当前偏移）：连线按它算端口坐标 */
            const ghostPosOf = (id: string): Point | undefined => {
              const n = ghostById.get(id)
              return n ? { x: (n.x ?? 0) + offX, y: (n.y ?? 0) + offY } : undefined
            }
            return (
              <>
                <EdgeLayer edges={placing.edges} nodeById={ghostById} posOf={ghostPosOf} faint />
                {placing.nodes.map((n) => (
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
          {newDrag && (() => {
            const def = nodeDef(newDrag.type)
            return (
              <GhostNode
                type={newDrag.type}
                left={newDrag.x - NODE_W / 2}
                top={newDrag.y - nodeHeight(def) / 2}
              />
            )
          })()}
        </Canvas>

        {palette !== null && showInspector && (
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
        catalog={palette}
        canPaste={hasClipboard}
        actions={{
          cut: cutNodes,
          copy: copyNodes,
          paste: (at) => {
            void pasteAt(at)
          },
          remove: ops.removeMany,
          add: ops.add,
        }}
        onClose={closeMenu}
      />
    </div>
  )
}
