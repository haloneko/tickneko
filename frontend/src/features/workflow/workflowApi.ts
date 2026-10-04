/** 工作流接口：与后端 /api/workflows/* 一一对应。 */
import { http } from '../../lib/http'

// --------------------------------------------------------------------------- 图类型
/**
 * 节点类型**不在这里枚举**：能摆哪些节点由后端注册表说了算，画布启动时拉
 * ``GET /workflows/node-types``（见下面的 fetchNodeCatalog）—— 加一个节点类型只改后端。
 * 所以 ``type`` 就是普通字符串：认不出的类型（旧图 / 扩展没装）画成灰色未知节点，
 * 保存时会被后端校验的 ``UNKNOWN_NODE_TYPE`` 挡下。
 *
 * 端口类型同理（``PortType`` 就是字符串）：有哪几种、每种什么色 / 是不是数据流，全由后端
 * 目录的 ``port_types`` 下发（见下面的 PortTypeSpec / NodeCatalog），前端不再抄一份表。
 */
export type PortType = string

/** 一种端口类型的展示信息（目录接口 ``port_types`` 逐条下发）：
 * ``data=true`` 沿边送值，``false`` 只表达先后（trigger）。 */
export interface PortTypeSpec {
  type: string
  label: string
  color: string
  data: boolean
}

export interface WorkflowNode {
  id: string
  type: string
  config: Record<string, unknown>
  /** 画布坐标：随图持久化（后端快照 / 暂存区都存），但不参与版本 hash */
  x?: number | null
  y?: number | null
}

/**
 * 一条边：上游的**输出端口**接到下游的**输入端口**，值就沿它流。
 *
 * 端口留空按 `trigger` 读（只表达先后的边）；两端端口类型必须相同，后端语义阶段会查。
 */
export interface WorkflowEdge {
  source: string
  target: string
  /** 输出端口 ID（前端连线用；后端会持久化到版本快照里） */
  sourcePort?: string
  /** 输入端口 ID */
  targetPort?: string
  /** 后端返回的下划线写法，normalizeGraph 会归一到驼峰字段 */
  source_port?: string
  target_port?: string
}

export interface WorkflowGraph {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
}

// --------------------------------------------------------------------------- 节点目录
/**
 * 一个端口（画布上的圆点）：id 就是 edge 的 sourcePort / targetPort。
 *
 * `type` 决定它传不传值（message 传、trigger 不传）；`required` 只对输入端口有意义：
 * 画布把没接线的必填入口标出来（后端也会报 INPUT_NOT_CONNECTED）。
 */
export interface NodePortSpec {
  id: string
  /** 端口类型：连线两端必须同类（泛型端口例外 —— 可接任意数据流端口，见 catalog.portCompatible） */
  type: PortType
  label: string
  required: boolean
  /** 透传对：指向同一节点另一侧的端口 id —— 输入输出生效类型永远一致（见 catalog.effectivePortTypes） */
  tie?: string
}

/** config 里的一个字段：画布照它渲染输入框 / 下拉。 */
export interface NodeFieldSpec {
  name: string
  label: string
  required: boolean
  /** default 只在 has_default 时有意义（null 也可能是合法默认值） */
  has_default: boolean
  default: unknown
  /** 有值就是枚举字段：渲染成下拉，顺序即显示顺序 */
  options: string[] | null
  /** 枚举项的显示名（值 -> 界面文字）：值是「跟外部对上号」的那个，不改；没配的项直接显示值 */
  option_labels: Record<string, string> | null
}

/** 一种节点类型：画布的面板项 / 标题 / 端口 / 配置表单全从这里来。 */
export interface NodeTypeSpec {
  type: string
  label: string
  color: string
  role: 'start' | 'end' | 'normal'
  /** 面板顺序（后端已排好：小的在前） */
  order: number
  /**
   * 语义分类（面板按它分组）：trigger / target / constant / action / control / data / end。
   * 认不出的分类照原样显示，不阻断画布。
   */
  category: string
  /** 有没有执行器：声明了但没实现的类型也能存图，跑到它才报错 */
  has_executor: boolean
  min_outgoing: number
  max_outgoing: number | null
  inputs: NodePortSpec[]
  outputs: NodePortSpec[]
  fields: NodeFieldSpec[]
}

/** 一种语义分类（目录接口 ``categories`` 逐条下发）：画布面板按它分组。
 * ``name`` 是节点标的机器分类名；``label`` 是后端 ``CATEGORY_LABELS`` 里的显示名。 */
export interface NodeCategorySpec {
  name: string
  label: string
}

export interface NodeCatalog {
  nodes: NodeTypeSpec[]
  /** 语义分类清单：画布面板的分组（顺序即显示顺序）全从这儿来 */
  categories: NodeCategorySpec[]
  /** 端口类型清单：端口配色 / 面板图例 / 数据流语义全从这儿来 */
  port_types: PortTypeSpec[]
}

// --------------------------------------------------------------------------- 校验
export interface ValidationIssue {
  nodeId: string
  code: string
  message: string
  suggestion: string
}

export interface ValidationReport {
  valid: boolean
  stage: string | null
  errors: ValidationIssue[]
}

// --------------------------------------------------------------------------- 工作流定义 / 版本
export interface WorkflowData {
  id: string
  owner_id: string
  /** 归属的昵称（空串 = 查不到 / 没设过）：管理员看的是全库，列表里靠它认人 */
  owner_name: string
  name: string
  status: 'draft' | 'published'
  /** **运行开关**：发布 ≠ 运行 —— 默认关，拨开才真的按已发布版本跑 */
  enabled: boolean
  /** **实例策略**（工作流设置）：false = 单实例（上次没跑完跳过本次），true = 多实例（允许叠加） */
  multi_instance: boolean
  current_version: number
  published_version: number
  /** 暂存区最近保存时间（0 = 没暂存过） */
  draft_updated_at: number
  /** 编辑器当前指向：draft = 暂存区，version = 最新提交版本 */
  current_ref: 'draft' | 'version'
  created_at: number
  updated_at: number
}

export interface WorkflowDraftData {
  /** 暂存图；从没暂存过为 null */
  graph: WorkflowGraph | null
  updated_at: number
}

export interface WorkflowVersionData {
  id: string
  workflow_id: string
  owner_id: string
  version: number
  graph: WorkflowGraph
  checksum: string
  note: string
  created_at: number
}

export interface SaveVersionResultData {
  workflow: WorkflowData
  version: WorkflowVersionData
  created: boolean
}

/** 已发布的那一份：定义（含**运行开关**）+ 版本快照（含图）。 */
export interface PublishedWorkflowData {
  workflow: WorkflowData
  version: WorkflowVersionData
}

// --------------------------------------------------------------------------- 接口
/**
 * GET /workflows/node-types：节点类型目录（画布的面板 / 端口 / 配置表单都照它渲染）。
 *
 * 只读后端内存里的注册表，不碰库；要放在 `/workflows/{id}` 之前，后端已经这么声明了。
 */
export function fetchNodeCatalog() {
  return http.get<NodeCatalog>('/workflows/node-types')
}

/** POST /workflows/validate：只校验不入库（画布点「校验」时用）。 */
export function validateGraph(graph: WorkflowGraph) {
  return http.post<ValidationReport, { graph: WorkflowGraph }>('/workflows/validate', { graph })
}

/** POST /workflows：新建工作流（只要名字，图之后逐版存）。 */
export function createWorkflow(name: string) {
  return http.post<WorkflowData, { name: string }>('/workflows', { name })
}

/**
 * GET /workflows：列表（普通用户只看自己的）。
 *
 * `ownerId` 只对管理员有意义（普通用户传了也只看得见自己的）：按归属筛时传它。
 */
export function listWorkflows(ownerId = '') {
  return http.get<WorkflowData[]>('/workflows', {
    params: ownerId ? { owner_id: ownerId } : undefined,
  })
}

/** GET /workflows/{id}：单个工作流详情。 */
export function getWorkflow(id: string) {
  return http.get<WorkflowData>(`/workflows/${encodeURIComponent(id)}`)
}

/** PATCH /workflows/{id}：改名。 */
export function renameWorkflow(id: string, name: string) {
  return http.patch<WorkflowData, { name: string }>(`/workflows/${encodeURIComponent(id)}`, { name })
}

/** DELETE /workflows/{id}：删除（级联删版本）。 */
export function deleteWorkflow(id: string) {
  return http.del<void>(`/workflows/${encodeURIComponent(id)}`)
}

/** POST /workflows/{id}/versions：提交一版；校验不通过返回 200 + 校验报告（valid=false），不写库。 */
export function saveVersion(id: string, graph: WorkflowGraph, note = '') {
  return http.post<SaveVersionResultData | ValidationReport, { graph: WorkflowGraph; note: string }>(
    `/workflows/${encodeURIComponent(id)}/versions`,
    { graph, note },
  )
}

/** PUT /workflows/{id}/draft：暂存编辑中的图（不做业务校验，半张图也能存）。 */
export function saveDraft(id: string, graph: WorkflowGraph) {
  return http.put<WorkflowData, { graph: WorkflowGraph }>(
    `/workflows/${encodeURIComponent(id)}/draft`,
    { graph },
  )
}

/** GET /workflows/{id}/draft：读暂存区（没暂存过时 graph 为 null）。 */
export function getDraft(id: string) {
  return http.get<WorkflowDraftData>(`/workflows/${encodeURIComponent(id)}/draft`)
}

/** GET /workflows/{id}/versions：版本历史（倒序，最新在前）。 */
export function listVersions(id: string) {
  return http.get<WorkflowVersionData[]>(`/workflows/${encodeURIComponent(id)}/versions`)
}

/** GET /workflows/{id}/versions/{version}：单个版本快照。 */
export function getVersion(id: string, version: number) {
  return http.get<WorkflowVersionData>(`/workflows/${encodeURIComponent(id)}/versions/${version}`)
}

/** POST /workflows/{id}/publish：发布指定版本；不传 version 发布最新版。 */
export function publishWorkflow(id: string, version?: number) {
  const body: { version?: number } = {}
  if (version !== undefined) body.version = version
  return http.post<WorkflowData, { version?: number }>(
    `/workflows/${encodeURIComponent(id)}/publish`,
    body,
  )
}

/** PUT /workflows/{id}/enabled：拨**运行开关**（发布 ≠ 运行：默认不跑，拨开才跑）。 */
export function setWorkflowEnabled(id: string, enabled: boolean) {
  return http.put<WorkflowData, { enabled: boolean }>(
    `/workflows/${encodeURIComponent(id)}/enabled`,
    { enabled },
  )
}

/** GET /workflows/{id}/published：已发布的那一份（开关状态 + 版本 + 图）。 */
export function getPublishedWorkflow(id: string) {
  return http.get<PublishedWorkflowData>(`/workflows/${encodeURIComponent(id)}/published`)
}

/**
 * PUT /workflows/{id}/settings：改工作流**设置**（现在只有「实例策略」）。
 *
 * 以后加设置就往这个请求体里加字段（后端请求体允许额外字段），函数签名不用动。
 */
export interface WorkflowSettings {
  /** false = 单实例（上一次还没跑完就跳过本次）；true = 多实例（到点就开新实例，允许叠加） */
  multi_instance: boolean
}

export function setWorkflowSettings(id: string, settings: WorkflowSettings) {
  return http.put<WorkflowData, WorkflowSettings>(
    `/workflows/${encodeURIComponent(id)}/settings`,
    settings,
  )
}
