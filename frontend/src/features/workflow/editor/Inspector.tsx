/**
 * 配置面板（悬浮在画布右侧）：选中节点的端口信息与配置字段 + 校验报告 + 版本历史。
 *
 * 字段清单来自后端目录：有 ``options`` 的渲染成下拉，其余是输入框；与数据入口**同名**的
 * 字段照常可填 —— 那是「没接线时的手填兜底」，标签上写明当前这个值是线上来的还是手填的。
 * 后端没声明、但 config 里确实存在的键也照旧给个输入框，别让它在界面上消失。
 */
import { IconTrash } from '../../../common/icons'
import { CronPicker } from '../../../common/CronPicker'
import {
  hasDedicatedEditor,
  isDataPort,
  portColor,
  portEffKey,
  type NodeTypeDef,
  type ValidationReport,
  type WorkflowNode,
} from './catalog'
import type { WorkflowVersionData } from '../workflowApi'
import styles from '../WorkflowEditor.module.css'

export interface InspectorProps {
  node: WorkflowNode | null
  def: NodeTypeDef | null
  /** 选中节点已经接上线的入口（没写端口的边按 trigger 算） */
  wired: Set<string>
  /** 端口生效类型表（见 ``catalog.effectivePortTypes``）：泛型端口接什么显什么类型/颜色 */
  effTypes: Map<string, string>
  /** 校验报告（null 或已通过 = 不显示错误面板） */
  report: ValidationReport | null
  versions: WorkflowVersionData[]
  onUpdate: (nodeId: string, key: string, value: unknown) => void
  onDelete: (nodeId: string) => void
}

export function Inspector({
  node,
  def,
  wired,
  effTypes,
  report,
  versions,
  onUpdate,
  onDelete,
}: InspectorProps) {
  return (
    <aside className={styles.inspector}>
      {node && def ? (
        <>
          <div className={styles.inspectorHead}>
            <span className={styles.inspectorTitle}>{def.label}</span>
            <button className={styles.iconBtn} onClick={() => onDelete(node.id)}>
              <IconTrash size={14} />
            </button>
          </div>

          {/* 端口信息 */}
          <div className={styles.portInfo}>
            <div className={styles.portInfoTitle}>端口</div>
            {def.inputs.length > 0 && (
              <div className={styles.portInfoSection}>
                <span className={styles.portInfoLabel}>输入</span>
                {def.inputs.map((p) => {
                  const connected = wired.has(p.id)
                  const eff = effTypes.get(portEffKey(node.id, 'in', p.id)) ?? p.type
                  return (
                    <span
                      className={styles.portInfoItem}
                      key={p.id}
                      style={{ color: portColor(eff) }}
                    >
                      ● {p.label}（{eff}）
                      {isDataPort(p.type) ? (connected ? ' · 已接线' : ' · 未接线') : ''}
                      {p.required && !connected ? ' · 必填！' : ''}
                    </span>
                  )
                })}
              </div>
            )}
            {def.outputs.length > 0 && (
              <div className={styles.portInfoSection}>
                <span className={styles.portInfoLabel}>输出</span>
                {def.outputs.map((p) => {
                  const eff = effTypes.get(portEffKey(node.id, 'out', p.id)) ?? p.type
                  return (
                    <span className={styles.portInfoItem} key={p.id} style={{ color: portColor(eff) }}>
                      ● {p.label}（{eff}）
                    </span>
                  )
                })}
              </div>
            )}
            <div className={styles.constHint}>
              值沿连线走：上游的 message 输出端口接到本节点的 message 输入端口。
              带 * 的必填入口没接线时，用下面同名字段手填。
            </div>
          </div>

          <div className={styles.field}>
            <label className={styles.label}>节点 ID</label>
            <input className={styles.input} value={node.id} disabled />
          </div>
          {/* cron：定时触发器的专属字段，走可视化选择器（手填表达式太容易写错） */}
          {node.type === 'trigger-time' && (
            <div className={styles.field}>
              <label className={styles.label}>cron 表达式</label>
              <CronPicker
                // 换节点就换一个新的（组件内部记着「用户选了哪个模式」，不该带到别的节点上）
                key={node.id}
                value={String(node.config.cron ?? '')}
                onChange={(cron) => onUpdate(node.id, 'cron', cron)}
              />
            </div>
          )}
          {def.fields
            .filter((field) => !hasDedicatedEditor(node.type, field.name))
            .map((field) => {
              const asInput = def.inputs.find((p) => p.id === field.name && p.type === 'message')
              const fromWire = asInput !== undefined && wired.has(field.name)
              return (
                <div className={styles.field} key={field.name}>
                  <label className={styles.label}>
                    {field.label}
                    {asInput && (fromWire ? '（来自连线，已覆盖）' : '（没接线时手填）')}
                  </label>
                  {field.options ? (
                    <select
                      className={styles.input}
                      value={String(node.config[field.name] ?? '')}
                      onChange={(e) => onUpdate(node.id, field.name, e.target.value)}
                    >
                      {field.options.map((option) => (
                        <option key={option} value={option}>
                          {/* 显示名来自后端（option_labels）：值是「跟外部对上号」的那个，不改 */}
                          {field.option_labels?.[option] ?? option}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      className={styles.input}
                      value={String(node.config[field.name] ?? '')}
                      onChange={(e) => onUpdate(node.id, field.name, e.target.value)}
                    />
                  )}
                </div>
              )
            })}
          {/* 后端没声明的键（手写图 / 扩展塞进来的）：照旧给个输入框 */}
          {Object.keys(node.config)
            .filter(
              (key) =>
                !hasDedicatedEditor(node.type, key) &&
                !def.fields.some((f) => f.name === key),
            )
            .map((key) => (
              <div className={styles.field} key={`extra-${key}`}>
                <label className={styles.label}>{key}</label>
                <input
                  className={styles.input}
                  value={String(node.config[key] ?? '')}
                  onChange={(e) => onUpdate(node.id, key, e.target.value)}
                />
              </div>
            ))}
        </>
      ) : (
        <div className={styles.inspectorEmpty}>选中一个节点以编辑配置</div>
      )}

      {report && !report.valid && (
        <div className={styles.errorPanel}>
          <div className={styles.errorTitle}>
            校验失败（{report.stage}）· {report.errors.length} 个问题
          </div>
          {report.errors.map((issue, i) => (
            <div key={i} className={styles.errorItem}>
              <div className={styles.errorCode}>{issue.code}</div>
              <div className={styles.errorMsg}>{issue.message}</div>
              {issue.suggestion && <div className={styles.errorSug}>{issue.suggestion}</div>}
            </div>
          ))}
        </div>
      )}

      {versions.length > 0 && (
        <div className={styles.versions}>
          <div className={styles.versionsTitle}>版本历史</div>
          {versions.map((v) => (
            <div key={v.version} className={styles.versionItem}>
              <span className={styles.versionNum}>v{v.version}</span>
              <span className={styles.versionNote}>{v.note || '—'}</span>
            </div>
          ))}
        </div>
      )}
    </aside>
  )
}
