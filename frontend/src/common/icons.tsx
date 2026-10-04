/**
 * 统一线性图标集：同一套线宽（1.7）、圆角、currentColor 取色。
 * 仅用 stroke，不使用 emoji，保证全站视觉一致。
 */
import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement> & { size?: number }

function base({ size = 20, ...rest }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.7,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    ...rest,
  }
}

export const IconDashboard = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3.5" y="3.5" width="7" height="7" rx="1.8" />
    <rect x="13.5" y="3.5" width="7" height="7" rx="1.8" />
    <rect x="3.5" y="13.5" width="7" height="7" rx="1.8" />
    <rect x="13.5" y="13.5" width="7" height="7" rx="1.8" />
  </svg>
)

export const IconRobot = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="4.5" y="7.5" width="15" height="11" rx="3" />
    <path d="M12 7.5V4.5M12 4.5h-2.2M8.7 3.2h.01M12 4.5h2.2M15.3 3.2h.01" />
    <circle cx="9.3" cy="13" r="1.1" fill="currentColor" stroke="none" />
    <circle cx="14.7" cy="13" r="1.1" fill="currentColor" stroke="none" />
    <path d="M9.8 15.7h4.4M2.5 11.5v4M21.5 11.5v4" />
  </svg>
)

export const IconSchedule = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </svg>
)

export const IconLogs = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6 3.5h9L19.5 8v12.5h-13.5z" />
    <path d="M14.5 3.5V8H19" />
    <path d="M8.5 12.5h7M8.5 15.7h7M8.5 9.3h2.5" />
  </svg>
)

export const IconMenu = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </svg>
)

export const IconCollapse = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M10.5 6l-6 6 6 6M16.5 6l-6 6 6 6" />
  </svg>
)

export const IconExpand = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13.5 6l6 6-6 6M7.5 6l6 6-6 6" />
  </svg>
)

export const IconLogout = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M15 4h3.5a1.5 1.5 0 011.5 1.5v13a1.5 1.5 0 01-1.5 1.5H15M10 8l-4 4 4 4M6 12h11" />
  </svg>
)

export const IconSun = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="4.2" />
    <path d="M12 2.8v2.2M12 19v2.2M21.2 12H19M5 12H2.8M18.5 5.5l-1.6 1.6M7.1 16.9l-1.6 1.6M18.5 18.5l-1.6-1.6M7.1 7.1L5.5 5.5" />
  </svg>
)

export const IconMoon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M20.2 14.2A8.2 8.2 0 019.8 3.8 8.2 8.2 0 1020.2 14.2z" />
  </svg>
)

export const IconUser = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="8" r="3.6" />
    <path d="M5 20c.7-3.6 3.5-5.2 7-5.2s6.3 1.6 7 5.2" />
  </svg>
)

export const IconLock = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="5" y="10.5" width="14" height="9.5" rx="2.2" />
    <path d="M8 10.5V8a4 4 0 018 0v2.5" />
  </svg>
)

export const IconEye = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2.5 12C3.6 10.4 7 6 12 6s8.4 4.4 9.5 6c-1.1 1.6-4.5 6-9.5 6s-8.4-4.4-9.5-6z" />
    <circle cx="12" cy="12" r="2.8" />
  </svg>
)

export const IconEyeOff = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 3l18 18" />
    <path d="M10.6 6.2A9.8 9.8 0 0112 6c5 0 8.4 4.4 9.5 6a15 15 0 01-3.3 3.9M6.2 7.6A14.7 14.7 0 002.5 12C3.6 13.6 7 18 12 18c1 0 2-.2 2.8-.5" />
    <path d="M9.9 10a3 3 0 004.2 4.2" />
  </svg>
)

export const IconAlert = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7.5v5.5M12 16.4v.35" />
  </svg>
)

export const IconCheck = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 12.5l5 5L20 6.5" />
  </svg>
)

export const IconInfo = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5" />
    <circle cx="12" cy="7.6" r="1.05" fill="currentColor" stroke="none" />
  </svg>
)

export const IconCopy = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 012-2h8" />
  </svg>
)

/** 粘贴：带夹子的剪贴板（与 IconCopy 区分开，右键菜单里两个动作挨着） */
export const IconPaste = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M9 4.6H7.6A1.6 1.6 0 006 6.2v12.2A1.6 1.6 0 007.6 20h8.8a1.6 1.6 0 001.6-1.6V6.2a1.6 1.6 0 00-1.6-1.6H15" />
    <rect x="9" y="2.8" width="6" height="3.6" rx="1.3" />
  </svg>
)

/** 剪切：剪刀（两个刃口对着上下两个环） */
export const IconScissors = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="6.4" cy="6.4" r="2.7" />
    <circle cx="6.4" cy="17.6" r="2.7" />
    <path d="M20 4.4L8.6 15.8M20 19.6L8.6 8.2M14.6 13.8L12 12" />
  </svg>
)

export const IconExternal = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M7 17L17 7M9 7h8v8" />
  </svg>
)

export const IconChevronDown = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6 9.5l6 6 6-6" />
  </svg>
)

export const IconChevronLeft = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M14.5 6l-6 6 6 6" />
  </svg>
)

export const IconChevronRight = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M9.5 6l6 6-6 6" />
  </svg>
)

export const IconChevronsLeft = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12.5 6l-6 6 6 6M18 6l-6 6 6 6" />
  </svg>
)

export const IconChevronsRight = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M11.5 6l6 6-6 6M6 6l6 6-6 6" />
  </svg>
)

export const IconBolt = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13 3L5 13h6l-1 8 8-10h-6l1-8z" />
  </svg>
)

export const IconKey = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="7.5" cy="16.5" r="3.5" />
    <path d="M10 14l9.5-9.5M16 7l2.5 2.5M13.8 9.2l2.5 2.5" />
  </svg>
)

export const IconPlus = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
)

export const IconRefresh = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M20 12a8 8 0 10-2.9 6.2" />
    <path d="M20 5.5V12h-6.2" />
  </svg>
)

export const IconTrash = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4.5 7h15M9.5 7V4.8h5V7M6.5 7l.9 12.2h9.2L17.5 7" />
    <path d="M10.4 10.6v5.5M13.6 10.6v5.5" />
  </svg>
)

export const IconTerminal = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="4.5" width="18" height="15" rx="2.2" />
    <path d="M7 9.3l3.2 2.9L7 15.1M12.8 15.4H17" />
  </svg>
)

export const IconDevices = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="2.8" y="5" width="13" height="9.5" rx="1.8" />
    <path d="M6.5 17.5h5.6M9.3 14.5v3" />
    <rect x="16.6" y="9.5" width="4.8" height="10" rx="1.4" />
  </svg>
)

export const IconMonitor = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="4.5" width="18" height="12" rx="2" />
    <path d="M9 20.5h6M12 16.5v4" />
  </svg>
)

export const IconPhone = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="7" y="2.8" width="10" height="18.4" rx="2.4" />
    <path d="M10.8 5.5h2.4" />
    <circle cx="12" cy="18.2" r="0.9" fill="currentColor" stroke="none" />
  </svg>
)

export const IconClose = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6 6l12 12M18 6L6 18" />
  </svg>
)

export const IconClock = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l2.8 1.8" />
  </svg>
)

export const IconPin = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 21s6.5-5.6 6.5-11a6.5 6.5 0 1 0-13 0C5.5 15.4 12 21 12 21z" />
    <circle cx="12" cy="10" r="2.3" />
  </svg>
)

export const IconEdit = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3z" />
    <path d="M13.5 6.5l3 3" />
  </svg>
)

export const IconArrowLeft = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M19 12H5M12 19l-7-7 7-7" />
  </svg>
)

export const IconSave = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M5 4h11l3 3v13H5z" />
    <path d="M8 4v5h7V4M8 20v-6h8v6" />
  </svg>
)

export const IconSettings = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="3.2" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.5 1z" />
  </svg>
)

export const IconCamera = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 7.5h3.2l1.8-2.7h6l1.8 2.7H20a1.5 1.5 0 0 1 1.5 1.5v11a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5v-11A1.5 1.5 0 0 1 4 7.5z" />
    <circle cx="12" cy="13.5" r="3.5" />
    <path d="M18.5 10.5h.01" />
  </svg>
)

/** 品牌图标（feather terminal 线条）：单色 currentColor，颜色随外层主题令牌变化 */
export const IconLogo = (p: IconProps) => (
  <svg {...base(p)}>
    <polyline points="4 17 10 11 4 5" />
    <line x1="12" y1="19" x2="20" y2="19" />
  </svg>
)
