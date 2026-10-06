/**
 * 公共错误态：接口挂了时整块展示（标题 + 详情 + trace + 重试）。
 *
 * 变量查看页 / 运行日志页的错误块从 JSX 到样式完全一致，抽到这里；error 数据由
 * `common/usePagedQuery` 的 error 提供（同一个形状），也可以手动构造。
 */
import { IconAlert, IconRefresh } from './icons'
import styles from './ErrorBox.module.css'

export interface ErrorState {
  title: string
  detail?: string
  traceId?: string
}

interface ErrorBoxProps {
  error: ErrorState
  /** 点「重试」：通常就是 `usePagedQuery` 的 `refresh(false)` */
  onRetry: () => void
}

export default function ErrorBox({ error, onRetry }: ErrorBoxProps) {
  return (
    <div className={styles.errorBox} role="alert">
      <IconAlert size={20} className={styles.errorIcon} />
      <div className={styles.errorBody}>
        <div className={styles.errorTitle}>{error.title}</div>
        {error.detail && <div className={styles.errorDetail}>{error.detail}</div>}
        {error.traceId && <div className={styles.errorTrace}>trace · {error.traceId}</div>}
        <button type="button" className={`btn ${styles.retryBtn}`} onClick={onRetry}>
          <IconRefresh size={14} />
          重试
        </button>
      </div>
    </div>
  )
}
