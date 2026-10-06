/**
 * 变量查看页：看工作流「缓存」节点写下的变量。
 *
 * 变量按作用域分两档（口径见 docs/cache/cache.md）：
 * - 图级，只属于某一张图；账号级，同一账号下的工作流共享；
 * - 缓存后端（内存 / Redis）与键前缀由后端管，这里只做只读展示。
 *
 * 后端尚未开放查看接口 —— 先用统一骨架顶上，接口就绪后替换内容区。
 */
import PlaceholderPage from '../../common/PlaceholderPage'
import { IconVariables } from '../../common/icons'

export default function VariablesPage() {
  return (
    <PlaceholderPage
      icon={IconVariables}
      title="变量查看"
      description="查看工作流「缓存」节点写入的变量：图级只属于某一张图，账号级在同一账号的工作流之间共享。"
      features={[
        '按作用域浏览：图级 / 账号级',
        '按变量名、所属图或账号检索',
        '查看变量的当前值与最近写入情况',
        '查看剩余有效期（TTL），必要时手动清理',
      ]}
    />
  )
}
