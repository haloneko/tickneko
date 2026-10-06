/** 侧边栏导航配置（单一数据源，侧栏 / 顶栏标题 / 路由都从这里取）。 */
import type { ComponentType, SVGProps } from 'react'
import {
  IconBolt,
  IconDashboard,
  IconDevices,
  IconLogs,
  IconRobot,
  IconSettings,
  IconTerminal,
  IconVariables,
} from '../../common/icons'

export interface NavItem {
  /** 路由路径 */
  path: string
  /** 菜单与顶栏标题文案 */
  label: string
  /** 线性图标组件 */
  icon: ComponentType<SVGProps<SVGSVGElement> & { size?: number }>
  /** 根路径需 end 匹配，避免任意页面都高亮“工作台” */
  end?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { path: '/', label: '工作台', icon: IconDashboard, end: true },
  { path: '/sessions', label: '登录设备', icon: IconDevices },
  { path: '/bots', label: '机器人', icon: IconRobot },
  { path: '/workflows', label: '工作流', icon: IconBolt },
  { path: '/variables', label: '变量查看', icon: IconVariables },
  { path: '/logs', label: '运行日志', icon: IconLogs },
  { path: '/debug', label: 'WS 调试', icon: IconTerminal },
  { path: '/profile', label: '个人设置', icon: IconSettings },
]
