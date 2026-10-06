/**
 * 归属筛选：管理员按「谁的」看列表时用（工作流列表 / 机器人列表 / 日志页 / 变量页共用）。
 *
 * 选项来自 `GET /owners`：管理员拿到全部用户，普通用户只有自己那一条 —— 所以默认
 * **选项不足两个就整个不渲染**（调用方不必去判断角色，服务端已按登录身份给过）；
 * 需要「即使只剩一档也要显示」的页面（如日志页的「全部 / 仅公共」）传 `renderAlways`，
 * 由调用方自己决定何时渲染（一般按 `isAdmin`）。
 *
 * 默认横排（label 在左，工作流 / 机器人列表的头部工具栏用）；要嵌进筛选表单的话传
 * `layout="vertical"`（label 在上、select 在下，观感对齐表单里的其他字段）。
 */
import type { Owner } from '../lib/ownersApi'
import styles from './OwnerFilter.module.css'

/**
 * 归属的显示名：昵称优先，其次登录账号，最后回落到 id。
 *
 * 列表里显示归属用它 —— 后端只保证 `owner_id` 一定有值，昵称可能是空串（没设过 / 查不到）。
 */
export function ownerName(ownerId: string, owners: Owner[]): string {
  const owner = owners.find((item) => item.owner_id === ownerId)
  if (!owner) return ownerId
  return owner.nickname || owner.account || ownerId
}

interface OwnerFilterProps {
  owners: Owner[]
  /** 选中的归属 id；等于 `allValue` 即「全部」 */
  value: string
  onChange: (ownerId: string) => void
  /** 「全部」这一档的 value（默认空串，老调用方不变） */
  allValue?: string
  /** 「全部」这一档的文案（默认「全部」） */
  allLabel?: string
  /** 额外档位，插在「全部」之后、具体用户之前，如 `{ value: 'public', label: '仅公共日志' }` */
  extraOptions?: Array<{ value: string; label: string }>
  /** 调用方自行控制显隐时传 true，跳过「选项不足两个就不渲染」 */
  renderAlways?: boolean
  /** 布局：`horizontal` 横排（label 在左，默认） / `vertical` 竖排（label 在上，适配筛选表单） */
  layout?: 'horizontal' | 'vertical'
}

export default function OwnerFilter({
  owners,
  value,
  onChange,
  allValue = '',
  allLabel = '全部',
  extraOptions = [],
  renderAlways = false,
  layout = 'horizontal',
}: OwnerFilterProps) {
  if (!renderAlways && owners.length < 2) return null
  const vertical = layout === 'vertical'
  return (
    <label className={vertical ? styles.wrapVertical : styles.wrap}>
      <span className={vertical ? styles.labelVertical : styles.label}>归属</span>
      <select
        className={vertical ? styles.selectVertical : styles.select}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value={allValue}>{allLabel}</option>
        {extraOptions.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
        {owners.map((owner) => (
          <option key={owner.owner_id} value={owner.owner_id}>
            {ownerName(owner.owner_id, owners)}
          </option>
        ))}
      </select>
    </label>
  )
}
