/**
 * 节点目录与几何：**渲染要用的常量与纯函数**都在这里，组件里不再出现这些推导。
 *
 * 分三块：
 *
 * * 目录（``installCatalog`` / ``nodeDef``）——类型定义来自后端 ``GET /workflows/node-types``；
 * * 几何（``NODE_W`` / ``nodeHeight`` / ``portCenterY`` / ``portAbsPos`` / ``edgeCoords``）
 *   ——卡片尺寸与连线端点，渲染与命中判定共用同一套口径；
 * * 杂项工具（``uid`` / ``truncate`` / ``normalizeGraph`` …）。
 *
 * 节点类型与**端口类型**都不在前端定义：后端给什么就画什么，认不出的节点类型只给一对触发口
 * 兜底（保存时会被 ``UNKNOWN_NODE_TYPE`` 拦下）、认不出的端口类型一律淡灰。**节点颜色**也
 * 由后端目录下发（认不出的类型才用兜底色）。这里只留前端自己的东西：运行时端口类型表（由
 * ``installCatalog`` 从目录的 ``port_types`` 装进来）与一个**固有例外**（``constant`` 的常量
 * 就是它的 config 本身）。
 */
import {
  type NodeCategorySpec,
  type NodeFieldSpec,
  type NodePortSpec,
  type NodeTypeSpec,
  type NodeCatalog,
  type PortType,
  type PortTypeSpec,
  type ValidationIssue,
  type ValidationReport,
  type WorkflowEdge,
  type WorkflowGraph,
  type WorkflowNode,
} from '../workflowApi'

export type {
  NodeCategorySpec,
  NodeFieldSpec,
  NodePortSpec,
  NodeTypeSpec,
  NodeCatalog,
  PortType,
  PortTypeSpec,
  ValidationIssue,
  ValidationReport,
}
export type { WorkflowEdge, WorkflowGraph, WorkflowNode }

export type PortSpec = NodePortSpec

/** 卡片上一个点 / 一份坐标 */
export interface Point {
  x: number
  y: number
}

/** 节点 id -> 坐标（渲染 / 框选 / 连线都读它） */
export type Positions = Record<string, Point>

export interface NodeTypeDef {
  type: string
  label: string
  color: string
  defaults: Record<string, unknown>
  inputs: PortSpec[]
  outputs: PortSpec[]
  /** 卡片底部的字段条：后端声明的字段里，名字**不是**端口的那些（手填值，照实显示） */
  constants: string[]
  /** 配置面板照它渲染（含与端口同名的字段：那是「没接线时的手填兜底」） */
  fields: NodeFieldSpec[]
}

//: 端口类型表：从后端目录的 ``port_types`` 装进来（见 installCatalog）。
//: 加端口类型只改后端 —— 配色 / 图例 / 数据流语义都跟着它走，前端不再抄一份。
let PORT_TYPES: Record<string, PortTypeSpec> = {}

//: 认不出的端口类型的兜底色（目录没装 / 后端新加的类型画布还没拉到）
const PORT_FALLBACK_COLOR = '#94a3b8'

/** 端口类型配色：认不出（目录没装 / 后端新加）一律淡灰。 */
export function portColor(type: string): string {
  return PORT_TYPES[type]?.color ?? PORT_FALLBACK_COLOR
}

/** 是不是数据端口（沿边送值）：由后端 ``port_types`` 的 ``data`` 字段决定，trigger 为 false。 */
export function isDataPort(type: string): boolean {
  return PORT_TYPES[type]?.data ?? false
}

/** 两种端口类型能不能互接：**同类互通；泛型端口跟任何数据流端口互接**（不接触发）。
 * 泛型就一个，直接按类型名 ``generic`` 判断（不加标记字段，与后端
 * ``port_types.port_types_compatible`` 同一份语义）；另一端是不是数据流端口查
 * ``data``（trigger 为 false）。认不出的类型按「非数据、非泛型」处理。 */
export function portCompatible(a: PortType, b: PortType): boolean {
  if (a === b) return true
  return (a === 'generic' && isDataPort(b)) || (b === 'generic' && isDataPort(a))
}

/** 端口生效类型表的 key：``节点:方向:端口``（见 :func:`effectivePortTypes`）。 */
export function portEffKey(nodeId: string, direction: 'in' | 'out', portId: string): string {
  return `${nodeId}:${direction}:${portId}`
}

/**
 * 每个端口的**生效类型**（泛型端口「接什么显什么」）：
 *
 * * 非 generic 端口 = 自身声明的类型；
 * * generic 端口按优先级取：① 自己接的边（**输入**取连进来那条边的源端口生效类型，
 *   **输出**取连出去那条边的目标端口生效类型）；② 没接到具体类型时看**透传对**
 *   （``tie`` 指向同一节点另一侧的端口）—— 输入没接线但输出接了，输入显示输出的类型，
 *   反之亦然，两端永远一致；③ 都没有就还是 generic。对方是 generic 就顺着继续追
 *   （同一端口不重复展开，环直接兜底 generic）。
 *
 * 由父组件按 ``edges`` 变化用 memo 算一次，连线层直接拿这个 Map 用。**卡片别直接拿它当
 * prop** —— 它的引用随 ``graph.nodes`` 变（改一下 config 就算），Map 换新引用会让 memo
 * 化的卡片整片重渲染；卡片侧改传「本节点的签名字符串」（按值比较，见 ``WorkflowEditor``
 * 的 ``effSigByNode``），只有自己那几个端口的类型真变了才重渲染。
 */
export function effectivePortTypes(
  nodeById: Map<string, WorkflowNode>,
  edges: WorkflowEdge[],
): Map<string, string> {
  const byIn = new Map<string, WorkflowEdge[]>()
  const byOut = new Map<string, WorkflowEdge[]>()
  for (const edge of edges) {
    const inKey = portEffKey(edge.target, 'in', edge.targetPort || DEFAULT_PORT)
    const outKey = portEffKey(edge.source, 'out', edge.sourcePort || DEFAULT_PORT)
    const push = (map: Map<string, WorkflowEdge[]>, key: string) => {
      const list = map.get(key)
      if (list) list.push(edge)
      else map.set(key, [edge])
    }
    push(byIn, inKey)
    push(byOut, outKey)
  }

  const memo = new Map<string, string>()
  const resolving = new Set<string>()

  const portSpec = (
    nodeId: string,
    direction: 'in' | 'out',
    portId: string,
  ): PortSpec | null => {
    const node = nodeById.get(nodeId)
    if (!node) return null
    const ports = nodeDef(node.type, node.config)[direction === 'in' ? 'inputs' : 'outputs']
    return ports.find((p) => p.id === portId) ?? null
  }

  const resolve = (nodeId: string, direction: 'in' | 'out', portId: string): string => {
    const key = portEffKey(nodeId, direction, portId)
    const hit = memo.get(key)
    if (hit !== undefined) return hit
    if (resolving.has(key)) return 'generic' // 环：顺着追到原地，兜底不展开
    resolving.add(key)
    const spec = portSpec(nodeId, direction, portId)
    let result = spec?.type ?? 'generic'
    if (result === 'generic') {
      // ① 自己接的边：另一端是具体类型就跟着它
      for (const edge of (direction === 'in' ? byIn.get(key) : byOut.get(key)) ?? []) {
        const other =
          direction === 'in'
            ? resolve(edge.source, 'out', edge.sourcePort || DEFAULT_PORT)
            : resolve(edge.target, 'in', edge.targetPort || DEFAULT_PORT)
        if (other !== 'generic') {
          result = other
          break
        }
      }
      // ② 没接到具体类型：透传对 —— 看同一节点另一侧的配对端口（tie），输入输出保持一致
      if (result === 'generic' && spec?.tie) {
        const other = resolve(nodeId, direction === 'in' ? 'out' : 'in', spec.tie)
        if (other !== 'generic') result = other
      }
    }
    memo.set(key, result)
    resolving.delete(key)
    return result
  }

  // 所有端口都解析（不只是有边的）：透传对的另一侧没接线也得算出来（输入没接、输出接了时
  // 输入也要显示输出那边定下的类型）。memo 化，重复 resolve 直接命中。
  for (const node of nodeById.values()) {
    const def = nodeDef(node.type, node.config)
    for (const p of def.inputs) resolve(node.id, 'in', p.id)
    for (const p of def.outputs) resolve(node.id, 'out', p.id)
  }
  return memo
}

/** 面板图例用的端口类型清单：顺序就是后端 ``PORT_TYPES`` 的顺序。 */
export function portTypeLegend(): PortTypeSpec[] {
  return Object.values(PORT_TYPES)
}

//: 面板分组：从后端目录的 ``categories`` 装进来（见 installCatalog）—— 后端从节点注册
//: 自动收集分类（顺序 = 面板顺序，标签 = 后端 CATEGORY_LABELS 里的中文名），画布不再抄一份
let CATEGORIES: NodeCategorySpec[] = []

/** 分类显示名：目录下发的 label；查不到（兜底组 / 后端没给）原样显示机器名。 */
export function categoryLabel(name: string): string {
  if (name === 'other') return '其它'
  return CATEGORIES.find((c) => c.name === name)?.label ?? name
}

/** 按语义分类给面板项分组：返回「分类 -> 该项列表」，顺序照目录下发的 ``categories``、
 * 组内按原序；目录里没有的分类（后端新加、画布还没拉到）归到最后的「其它」组。 */
export function groupByCategory(items: NodeTypeSpec[]): Array<[string, NodeTypeSpec[]]> {
  const buckets = new Map<string, NodeTypeSpec[]>()
  for (const item of items) {
    const key = CATEGORIES.some((c) => c.name === item.category) ? item.category : 'other'
    const list = buckets.get(key) ?? []
    list.push(item)
    buckets.set(key, list)
  }
  const order = [...CATEGORIES.map((c) => c.name), ...(buckets.has('other') ? ['other'] : [])]
  return order.flatMap((key) => (buckets.has(key) ? [[key, buckets.get(key)!] as [string, NodeTypeSpec[]]] : []))
}

//: 边没写端口时的口径：按「触发 -> 触发」读（与后端 graph.DEFAULT_EDGE_PORT 一致）
export const DEFAULT_PORT = 'trigger'

//: 认不出的节点类型 / 后端没配色的兜底色
const DEFAULT_COLOR = '#64748b'



// --------------------------------------------------------------------------- 尺寸
export const NODE_W = 168
const HEADER_H = 34
const PORT_ROW_H = 24
// 常量区只服务于「框选命中估算」（卡片高度本身已由内容撑开）：按每个胶囊独占一行的
// 宽裕口径算，框选宁多勿漏
const CONST_GAP = 13
const CONST_ROW_H = 22

export { PORT_ROW_H }

// --------------------------------------------------------------------------- 目录
//: 拉回来的目录：类型 -> 规格（渲染时按类型取；面板顺序也来自它）
let CATALOG: Record<string, NodeTypeSpec> = {}

/** 后端没登记这个类型时的兜底端口：能画、能接线，保存时被 ``UNKNOWN_NODE_TYPE`` 拦下。 */
const UNKNOWN_PORTS: PortSpec[] = [
  { id: 'trigger', type: 'trigger', label: '触发', required: false },
]

/** 装目录（编辑器加载时调一次），返回已按后端 order 排好的面板项列表。 */
export function installCatalog(catalog: NodeCatalog): NodeTypeSpec[] {
  CATALOG = Object.fromEntries(catalog.nodes.map((item) => [item.type, item]))
  CATEGORIES = catalog.categories
  PORT_TYPES = Object.fromEntries(catalog.port_types.map((item) => [item.type, item]))
  DEF_CACHE.clear() // 目录换了：推导结果全部作废
  return [...catalog.nodes].sort((a, b) => a.order - b.order)
}

/**
 * ``nodeDef`` 的推导结果缓存：一次渲染里每个节点都要算一遍（卡片 + 连线 + 框选），
 * 而结果只取决于**节点类型**（触发器拆成三个之后，不再有「形状随 config 变」的类型），
 * 装目录时才需要清。
 */
const DEF_CACHE = new Map<string, NodeTypeDef>()

/**
 * 取节点类型定义（渲染用）：端口 / 字段 / 中文名 / 顺序 / 颜色全部来自后端目录（认不出的
 * 类型用兜底色），并按 config 处理上面说的两个固有例外。
 */
export function nodeDef(type: string, _config?: Record<string, unknown>): NodeTypeDef {
  // ``_config`` 留着只为调用方签名不变：触发器拆成三个节点之后，**形状不再随 config 变**
  // （以前 start 的端口要看 config.trigger），所以它不参与推导，也不进缓存键。
  const key = type
  const cached = DEF_CACHE.get(key)
  if (cached) return cached

  const def = computeNodeDef(type)
  DEF_CACHE.set(key, def)
  return def
}

function computeNodeDef(type: string): NodeTypeDef {
  const spec = CATALOG[type]
  if (spec === undefined) {
    // 认不出的类型：不猜它的端口，只给一对触发口让它还能画出来（这是兜底，不是定义）
    return {
      type,
      label: type,
      color: DEFAULT_COLOR,
      defaults: {},
      inputs: UNKNOWN_PORTS,
      outputs: UNKNOWN_PORTS,
      constants: [],
      fields: [],
    }
  }

  const portIds = new Set([...spec.inputs, ...spec.outputs].map((port) => port.id))
  const defaults: Record<string, unknown> = {}
  for (const field of spec.fields) {
    if (field.has_default) defaults[field.name] = field.default
  }

  const base: NodeTypeDef = {
    type: spec.type,
    label: spec.label,
    color: spec.color || DEFAULT_COLOR,
    defaults,
    inputs: spec.inputs,
    outputs: spec.outputs,
    // 卡片底部的字段条：与端口同名的字段（log 的 message）是**数据入口**，值从线上来，
    // 卡片上不重复显示；其余字段（level / method / timeout / value…）是手填值，照实显示
    constants: spec.fields
      .filter((field) => !portIds.has(field.name))
      .map((field) => field.name),
    // 配置面板**照单全收**：与端口同名的字段也要能填 —— 那是「没接线时的手填兜底」
    fields: [...spec.fields],
  }

  return base
}

/**
 * 哪些字段**有专门的编辑器**，通用渲染要跳过（不然会出现两个控件）。
 *
 * 目前只有定时触发器的 ``cron``：走 :mod:`common/CronPicker` 可视化选择 ——
 * 手填表达式太容易写错。
 */
export function hasDedicatedEditor(nodeType: string, fieldName: string): boolean {
  return nodeType === 'trigger-time' && fieldName === 'cron'
}

// --------------------------------------------------------------------------- 几何
export function nodeHeight(def: NodeTypeDef): number {
  const portRows = Math.max(def.inputs.length, def.outputs.length)
  const constH = def.constants.length > 0 ? CONST_GAP + def.constants.length * CONST_ROW_H : 0
  return HEADER_H + Math.max(portRows, 1) * PORT_ROW_H + constH
}

/** 端口在节点内的 Y 偏移（圆心）。 */
export function portCenterY(
  def: NodeTypeDef,
  direction: 'in' | 'out',
  portId: string,
): number {
  const ports = direction === 'in' ? def.inputs : def.outputs
  const idx = ports.findIndex((p) => p.id === portId)
  if (idx === -1) return HEADER_H + PORT_ROW_H / 2
  return HEADER_H + idx * PORT_ROW_H + PORT_ROW_H / 2
}

/** 端口在画布上的绝对坐标。 */
export function portAbsPos(
  nodeId: string,
  portId: string,
  direction: 'in' | 'out',
  nodeType: string,
  positions: Positions,
  config?: Record<string, unknown>,
): Point | null {
  const pos = positions[nodeId]
  if (!pos) return null
  const def = nodeDef(nodeType, config)
  const y = pos.y + portCenterY(def, direction, portId)
  const x = direction === 'in' ? pos.x : pos.x + NODE_W
  return { x, y }
}

/**
 * 连线的贝塞尔路径：水平控制点让线平滑绕行（目标在左边也画得出自然的 S 形）。
 * 控制点偏移随水平距离伸缩，太近也不小于 36px，保证曲线不塌成直角。
 */
export function edgeCurve(x1: number, y1: number, x2: number, y2: number): string {
  const bend = Math.min(120, Math.max(36, Math.abs(x2 - x1) / 2))
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`
}

export interface EdgeCoords {
  x1: number
  y1: number
  x2: number
  y2: number
}

/**
 * 一条连线的两端坐标；``nodeById`` / ``posOf`` 可以换成别的来源 —— 粘贴虚影就是拿
 * 剪贴板里的节点与偏移后的坐标来画预览的。
 */
export function edgeCoords(
  edge: WorkflowEdge,
  nodeById: Map<string, WorkflowNode>,
  posOf: (id: string) => Point | undefined,
): EdgeCoords | null {
  const srcNode = nodeById.get(edge.source)
  const tgtNode = nodeById.get(edge.target)
  if (!srcNode || !tgtNode) return null
  const sp = posOf(edge.source)
  const tp = posOf(edge.target)
  if (!sp || !tp) return null
  // 没写端口的边按「触发 -> 触发」画（与后端口径一致）
  const sourcePortId = edge.sourcePort || DEFAULT_PORT
  const targetPortId = edge.targetPort || DEFAULT_PORT
  const srcDef = nodeDef(srcNode.type, srcNode.config)
  const tgtDef = nodeDef(tgtNode.type, tgtNode.config)
  return {
    x1: sp.x + NODE_W,
    y1: sp.y + portCenterY(srcDef, 'out', sourcePortId),
    x2: tp.x,
    y2: tp.y + portCenterY(tgtDef, 'in', targetPortId),
  }
}

/** 连线颜色：取起点那个输出端口的类型色（认不出就淡灰）。泛型端口带上
 * ``effectivePortTypes`` 算的生效类型表就跟着变（接出去是什么就画什么色），不带则退回
 * 声明类型（粘贴虚影那一路，半透明预览不需要跟随）。 */
export function edgeColor(
  edge: WorkflowEdge,
  nodeById: Map<string, WorkflowNode>,
  effTypes?: Map<string, string>,
): string {
  const srcNode = nodeById.get(edge.source)
  if (!srcNode) return 'var(--text-3)'
  const outPort = edge.sourcePort ?? DEFAULT_PORT
  const eff = effTypes?.get(portEffKey(edge.source, 'out', outPort))
  if (eff) return portColor(eff)
  const port = nodeDef(srcNode.type, srcNode.config).outputs.find((p) => p.id === outPort)
  return port ? portColor(port.type) : 'var(--text-3)'
}

// --------------------------------------------------------------------------- 工具
export function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`
}

/** 焦点在输入框 / 下拉 / 可编辑元素里时：画布快捷键要让位给文本编辑 */
export function isEditingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  if (!el) return false
  return (
    el.tagName === 'INPUT' ||
    el.tagName === 'TEXTAREA' ||
    el.tagName === 'SELECT' ||
    el.isContentEditable
  )
}

export function emptyGraph(): WorkflowGraph {
  return { nodes: [], edges: [] }
}

/**
 * 后端形态归一：边的端口字段后端是 source_port/target_port，统一成前端用的驼峰写法。
 *
 * 端口留空不在这里补：后端按 ``trigger`` 读（只表达先后的边），画布照同一口径显示 ——
 * 见 :data:`DEFAULT_PORT`。
 */
export function normalizeGraph(g: WorkflowGraph): WorkflowGraph {
  const normEdge = (e: WorkflowEdge): WorkflowEdge => ({
    source: e.source,
    target: e.target,
    sourcePort: e.sourcePort ?? e.source_port,
    targetPort: e.targetPort ?? e.target_port,
  })

  return { nodes: g.nodes, edges: g.edges.map(normEdge) }
}

export function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

/** 校验报告按节点分组（卡片上的红色角标读它）。 */
export function issuesByNode(report: { errors: ValidationIssue[] } | null): Map<string, ValidationIssue[]> {
  const map = new Map<string, ValidationIssue[]>()
  if (!report) return map
  for (const issue of report.errors) {
    const list = map.get(issue.nodeId) ?? []
    list.push(issue)
    map.set(issue.nodeId, list)
  }
  return map
}

/**
 * 每个节点「已经接上线的入口」：一次遍历边分组，别在渲染里对每个节点扫一遍全边
 * （那会让画布变成 O(节点 × 边)）。
 */
export function wiredPortsByNode(edges: WorkflowEdge[]): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>()
  for (const edge of edges) {
    const set = map.get(edge.target) ?? new Set<string>()
    set.add(edge.targetPort || DEFAULT_PORT)
    map.set(edge.target, set)
  }
  return map
}
