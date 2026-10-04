/**
 * 右键菜单的**配置表**：菜单里有什么、什么状态显示什么、点了做什么 —— 全在这一个文件里。
 *
 * 关键是把「要采集什么状态」想清楚：按钮能看到的只有 :interface:`MenuContext` 这一份**已经采集好**
 * 的状态（右键命中了哪几个节点、剪贴板里有没有能贴的、节点目录到了没有）。于是：
 *
 * * 加一个新按钮 = 往下面那张表里加一条，``visible`` 里写明白它在什么状态下出现；
 * * 只有当按钮要用到**新状态**时，才回头往 ``MenuContext`` 上加一个字段（采集处也只有一处，
 *   见 ``ContextMenuHost``）；
 * * 渲染那一侧（``ContextMenuList``）不认识任何具体按钮，连行高都不写死 —— 位置是按「实际会画出来
 *   的那几行」累加着算的，所以改配置不会把菜单位置算歪。
 *
 * 动作本身不在这里实现：配置只说「调用 ``act.copy``」，实现由编辑器给（见 WorkflowEditor）。
 */
import type { ReactNode } from 'react'
import { IconCopy, IconPaste, IconScissors, IconTrash } from '../../../common/icons'
import type { NodeTypeSpec, Point } from './catalog'

/** 采集给菜单看的**全部状态**：按钮的显示与文案只允许依赖它。 */
export interface MenuContext {
  /** 开的是哪一份菜单：节点上（动作针对 ``ids``）或空白处 */
  kind: 'node' | 'canvas'
  /** 右键命中的节点集合（空白菜单恒为空数组） */
  ids: string[]
  /** 右键那一处的**画布坐标**：添加节点 / 粘贴的落点 */
  point: Point
  /** 剪贴板里有没有能贴的节点 */
  canPaste: boolean
  /** 节点类型目录（「添加节点」的二级菜单用它；还没拉回来时是 null） */
  catalog: NodeTypeSpec[] | null
}

/** 菜单动作：配置里只声明「叫什么」，实现由编辑器接（编辑器也因此不用再管关菜单）。 */
export interface MenuActions {
  cut: (ids: string[]) => void
  copy: (ids: string[]) => void
  /** 在 ``at`` 这一处放下剪贴板里那一组 */
  paste: (at: Point) => void
  remove: (ids: string[]) => void
  /** 在 ``at`` 这一处添加一个该类型的节点 */
  add: (type: string, at: Point) => void
}

interface EntryBase {
  id: string
  /** 不写 = 恒显示；写了就按采集到的状态决定（「什么状态显示什么」都在这） */
  visible?: (ctx: MenuContext) => boolean
}

export type MenuEntry =
  | (EntryBase & {
      kind: 'action'
      icon: ReactNode
      label: (ctx: MenuContext) => string
      /** 危险动作（删除这类）用红色 */
      danger?: boolean
      run: (ctx: MenuContext, act: MenuActions) => void
    })
  /** 分组细线；渲染时会自动去掉开头 / 结尾 / 连着两根的那种，所以不写 visible 也不会留下孤儿线 */
  | (EntryBase & { kind: 'separator' })
  /** 「分类 ▸ 节点类型」两级菜单，内容来自节点目录（与左侧节点面板同一份） */
  | (EntryBase & { kind: 'catalog'; title: string })

/** 图标统一尺寸 */
const ICON = 14

/** 动作文案里的主语：单选是「节点」，多选是「选中的 N 个节点」。 */
function subject(ids: string[]): string {
  return ids.length > 1 ? `选中的 ${ids.length} 个节点` : '节点'
}

/** 节点上：剪切 / 复制 / 粘贴 / 删除 —— 前三个对「这一次右键的那一组」生效。 */
export const NODE_MENU: MenuEntry[] = [
  {
    kind: 'action',
    id: 'cut',
    icon: <IconScissors size={ICON} />,
    label: (ctx) => `剪切${subject(ctx.ids)}`,
    run: (ctx, act) => act.cut(ctx.ids),
  },
  {
    kind: 'action',
    id: 'copy',
    icon: <IconCopy size={ICON} />,
    label: (ctx) => `复制${subject(ctx.ids)}`,
    run: (ctx, act) => act.copy(ctx.ids),
  },
  {
    kind: 'action',
    id: 'paste',
    icon: <IconPaste size={ICON} />,
    label: () => '粘贴',
    // 剪贴板里没东西可贴时整项不出现（状态由编辑器采集）
    visible: (ctx) => ctx.canPaste,
    run: (ctx, act) => act.paste(ctx.point),
  },
  {
    kind: 'action',
    id: 'delete',
    icon: <IconTrash size={ICON} />,
    label: (ctx) => `删除${subject(ctx.ids)}`,
    danger: true,
    run: (ctx, act) => act.remove(ctx.ids),
  },
]

/** 空白处：粘贴 + 按分类添加节点。 */
export const CANVAS_MENU: MenuEntry[] = [
  {
    kind: 'action',
    id: 'paste',
    icon: <IconPaste size={ICON} />,
    label: () => '粘贴',
    visible: (ctx) => ctx.canPaste,
    run: (ctx, act) => act.paste(ctx.point),
  },
  { kind: 'separator', id: 'paste-add' },
  {
    kind: 'catalog',
    id: 'add',
    title: '添加节点',
    // 目录还没拉回来（或后端一个类型都没登记）就没什么可加
    visible: (ctx) => (ctx.catalog?.length ?? 0) > 0,
  },
]

/** 菜单种类 -> 配置表：以后多一份菜单（连线 / 分组…）在这里加一行就行。 */
export const MENUS: Record<MenuContext['kind'], MenuEntry[]> = {
  node: NODE_MENU,
  canvas: CANVAS_MENU,
}
