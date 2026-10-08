/**
 * 节点类型目录（后端给的）：面板 / 端口 / 配置字段都按它渲染。
 *
 * 拉回来之前**不渲染画布** —— 认不出类型就画不出端口；失败也不退回一份前端定义
 * （那正是以前前后端漂移的来源），只在占位里给一个重试。
 *
 * 拉到之后 ``installCatalog`` 把它装进 ``catalog`` 模块：之后 ``nodeDef`` / ``portAbsPos``
 * 这些纯函数才认得这些类型。
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { fetchNodeCatalog, type NodeTypeSpec } from '../workflowApi'
import { installCatalog } from './catalog'
import styles from '../WorkflowEditor.module.css'

export interface NodeCatalog {
  /** 目录（``null`` = 还没拉回来 / 拉失败了） */
  palette: NodeTypeSpec[] | null
  /** 加载中 / 拉不回来时画布位置显示的占位；``null`` = 不用占位 */
  placeholder: ReactNode
  /** 重新拉一次（占位里那个「重试」按钮） */
  reload: () => void
}

/** 参数：工作流本身还在加载吗（目录拉回来了也得等图到位） */
export function useNodeCatalog(loading: boolean): NodeCatalog {
  const [palette, setPalette] = useState<NodeTypeSpec[] | null>(null)
  const [failed, setFailed] = useState(false)

  const load = useCallback(async () => {
    setFailed(false)
    try {
      const { data } = await fetchNodeCatalog()
      setPalette(installCatalog(data))
    } catch {
      setFailed(true)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const placeholder =
    palette === null || loading ? (
      <div className={styles.loading}>
        {palette !== null ? (
          <>
            <span className="spinner" />
            正在加载…
          </>
        ) : failed ? (
          <>
            节点类型加载失败
            <button className="btn" onClick={() => void load()}>
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

  return { palette, placeholder, reload: load }
}
