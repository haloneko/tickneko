/** 按查看器展示类型渲染、编辑和调用 add；保存后重新读取，失败保留草稿。 */
import { useRef, useState } from 'react'
import { Modal } from '../../common/Modal'
import { ApiRequestError } from '../../lib/http'
import {
  addVariable,
  viewVariable,
  type VariableEntry,
  type VariableMutation,
} from './variablesApi'
import {
  dictPayload,
  draftValue,
  listPayload,
  parseValue,
  type FieldDraft,
  type ValueDraft,
} from './variableEdits'
import styles from './VariableValueViewer.module.css'

const TYPE_LABELS = { str: '文本', json: 'JSON', list: '列表', dict: '字典' }
const emptyValue = (): ValueDraft => ({ text: '', format: 'text' })

function shown(value: unknown): string {
  if (typeof value === 'string') return value === '' ? '（空字符串）' : value
  return JSON.stringify(value, null, 2) ?? 'null'
}

function recordData(data: unknown): Record<string, unknown> {
  return data !== null && typeof data === 'object' && !Array.isArray(data)
    ? (data as Record<string, unknown>)
    : {}
}

function Preview({ entry, expanded = false }: { entry: VariableEntry; expanded?: boolean }) {
  const className = `${styles.preview} ${expanded ? styles.expanded : ''}`
  switch (entry.type) {
    case 'list': {
      const items: unknown[] = Array.isArray(entry.data) ? entry.data : []
      const visible = expanded ? items : items.slice(0, 4)
      return (
        <div className={className}>
          {items.length === 0 ? (
            '（空列表）'
          ) : (
            <ol className={styles.list}>
              {visible.map((item, index) => (
                <li key={index}>{shown(item)}</li>
              ))}
            </ol>
          )}
          {!expanded && items.length > visible.length && (
            <span className={styles.hint}>还有 {items.length - visible.length} 项</span>
          )}
        </div>
      )
    }
    case 'dict': {
      const fields = Object.entries(recordData(entry.data))
      const visible = expanded ? fields : fields.slice(0, 4)
      return (
        <div className={className}>
          {fields.length === 0 ? (
            '（空字典）'
          ) : (
            <dl className={styles.dictionary}>
              {visible.map(([field, value]) => (
                <div key={field} style={{ display: 'contents' }}>
                  <dt>{field}</dt>
                  <dd>{shown(value)}</dd>
                </div>
              ))}
            </dl>
          )}
          {!expanded && fields.length > visible.length && (
            <span className={styles.hint}>还有 {fields.length - visible.length} 个字段</span>
          )}
        </div>
      )
    }
    case 'json':
      return <pre className={className}>{JSON.stringify(entry.data, null, 2)}</pre>
    case 'str':
      return <div className={className}>{shown(entry.data)}</div>
    default:
      return <span className={styles.hint}>{entry.reason || '暂不支持查看'}</span>
  }
}

function ValueInput({
  value,
  label,
  onChange,
}: {
  value: ValueDraft
  label: string
  onChange: (value: ValueDraft) => void
}) {
  return (
    <div className={styles.valueInput}>
      <textarea
        className={styles.control}
        aria-label={label}
        rows={value.format === 'json' ? 3 : 1}
        value={value.text}
        onChange={(event) => onChange({ ...value, text: event.target.value })}
      />
      <select
        className={styles.control}
        aria-label={`${label}格式`}
        value={value.format}
        onChange={(event) =>
          onChange({ ...value, format: event.target.value as ValueDraft['format'] })
        }
      >
        <option value="text">文本</option>
        <option value="json">JSON</option>
      </select>
    </div>
  )
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请重试'
}

function clearsCollection(params: VariableMutation): boolean {
  return (
    (Array.isArray(params.items) && params.items.length === 0) ||
    ('fields' in params &&
      params.fields !== null &&
      typeof params.fields === 'object' &&
      Object.keys(params.fields).length === 0)
  )
}

export default function VariableValueViewer({
  entry,
  onChanged,
}: {
  entry: VariableEntry
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [current, setCurrent] = useState(entry)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [dirty, setDirty] = useState(false)
  const [text, setText] = useState('')
  const [items, setItems] = useState<ValueDraft[]>([])
  const [fields, setFields] = useState<FieldDraft[]>([])
  const [newValue, setNewValue] = useState<ValueDraft>(emptyValue)
  const [newField, setNewField] = useState('')

  function applySnapshot(snapshot: VariableEntry) {
    setCurrent(snapshot)
    setText(
      snapshot.type === 'str' && typeof snapshot.data === 'string'
        ? snapshot.data
        : (JSON.stringify(snapshot.data, null, 2) ?? 'null'),
    )
    setItems(
      Array.isArray(snapshot.data) && snapshot.type === 'list' ? snapshot.data.map(draftValue) : [],
    )
    setFields(
      snapshot.type === 'dict'
        ? Object.entries(recordData(snapshot.data)).map(([field, value]) => ({
            field,
            value: draftValue(value),
          }))
        : [],
    )
    setNewValue(emptyValue())
    setNewField('')
    setDirty(false)
  }

  async function load() {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError('')
    setNotice('')
    try {
      applySnapshot((await viewVariable(entry)).data)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
      busyRef.current = false
    }
  }

  function showEditor() {
    applySnapshot(entry)
    setOpen(true)
    void load()
  }

  async function modify(params: VariableMutation) {
    if (busyRef.current || !current.editable) return
    busyRef.current = true
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const saved = (await addVariable(entry, params)).data
      let latest: VariableEntry
      try {
        latest = (await viewVariable(entry)).data
      } catch (err) {
        if (err instanceof ApiRequestError && err.status === 404 && clearsCollection(params)) {
          // 空集合不占存储键；来源也丢失时保留成功保存的空快照，并刷新列表。
          latest = { ...saved, editable: false, reason: '变量已清空，节点重新写入后可继续编辑' }
        } else {
          setError(`已保存，但重新读取失败：${errorMessage(err)}`)
          onChanged()
          return
        }
      }
      applySnapshot(latest)
      setNotice('已保存')
      onChanged()
    } catch (err) {
      setError(errorMessage(err)) // 包括服务端 422；不重置任何编辑草稿。
    } finally {
      setBusy(false)
      busyRef.current = false
    }
  }

  function save() {
    try {
      switch (current.type) {
        case 'str':
          void modify({ value: text })
          break
        case 'json':
          void modify({ value: parseValue({ text, format: 'json' }) })
          break
        case 'list':
          void modify(listPayload(items))
          break
        case 'dict':
          void modify(dictPayload(fields))
          break
      }
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  function add() {
    try {
      const value = parseValue(newValue)
      if (current.type === 'list') void modify({ item: value })
      else {
        if (!newField) throw new Error('字段名不能为空')
        void modify({ field: newField, value })
      }
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  function edited() {
    setDirty(true)
    setNotice('')
    setError('')
  }

  return (
    <div>
      <Preview entry={entry} />
      <div className={styles.toolbar}>
        {entry.type && (
          <span className={styles.hint}>
            {TYPE_LABELS[entry.type]}
            {entry.type === 'list' && entry.length !== null ? ` · ${entry.length} 项` : ''}
          </span>
        )}
        {entry.type && (
          <button type="button" className="btn" onClick={showEditor}>
            {entry.editable ? '编辑' : '查看'}
          </button>
        )}
        {!entry.editable && entry.reason && entry.type && (
          <span className={styles.hint}>{entry.reason}</span>
        )}
      </div>
      {open && (
        <Modal
          title={`变量 · ${entry.key}`}
          busy={busy}
          onClose={() => setOpen(false)}
          footer={
            <>
              <button
                type="button"
                className="btn"
                disabled={busy || dirty}
                onClick={() => void load()}
              >
                重新读取
              </button>
              {current.editable && (
                <button type="button" className="btn btn-primary" disabled={busy} onClick={save}>
                  {busy ? '提交中…' : '保存'}
                </button>
              )}
            </>
          }
        >
          {busy && (
            <p className={styles.hint} role="status">
              正在读取或保存…
            </p>
          )}
          {error && (
            <p className={styles.error} role="alert">
              {error}
            </p>
          )}
          {notice && <p role="status">{notice}</p>}
          {current.type && (
            <p className={styles.hint}>
              {TYPE_LABELS[current.type]}
              {current.type === 'list' && current.length !== null
                ? ` · 当前 ${current.length} 项`
                : ''}
            </p>
          )}
          {!current.editable ? (
            <>
              <p className={styles.hint}>{current.reason}</p>
              <Preview entry={current} expanded />
            </>
          ) : (
            <fieldset disabled={busy} className={styles.editor}>
              {(current.type === 'str' || current.type === 'json') && (
                <textarea
                  className={styles.control}
                  aria-label="变量值"
                  rows={8}
                  value={text}
                  onChange={(event) => {
                    setText(event.target.value)
                    edited()
                  }}
                />
              )}
              {current.type === 'list' && (
                <>
                  {items.length === 0 && <p className={styles.hint}>空列表</p>}
                  {items.map((item, index) => (
                    <div className={styles.item} key={index}>
                      <div className={styles.itemHead}>
                        <span>第 {index + 1} 项</span>
                        <button
                          type="button"
                          className="btn"
                          aria-label={`删除第 ${index + 1} 项`}
                          onClick={() => {
                            setItems((values) => values.filter((_, i) => i !== index))
                            edited()
                          }}
                        >
                          删除
                        </button>
                      </div>
                      <ValueInput
                        value={item}
                        label={`第 ${index + 1} 项`}
                        onChange={(value) => {
                          setItems((values) => values.map((old, i) => (i === index ? value : old)))
                          edited()
                        }}
                      />
                    </div>
                  ))}
                  <div className={styles.toolbar}>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => {
                        setItems([])
                        edited()
                      }}
                    >
                      清空列表
                    </button>
                    <span className={styles.hint}>保存当前完整列表，保留顺序和重复项</span>
                  </div>
                </>
              )}
              {current.type === 'dict' && (
                <>
                  {fields.length === 0 && <p className={styles.hint}>空字典</p>}
                  {fields.map((field, index) => (
                    <div className={styles.item} key={index}>
                      <div className={styles.itemHead}>
                        <input
                          className={styles.control}
                          aria-label={`字段 ${index + 1} 的名称`}
                          value={field.field}
                          onChange={(event) => {
                            setFields((values) =>
                              values.map((old, i) =>
                                i === index ? { ...old, field: event.target.value } : old,
                              ),
                            )
                            edited()
                          }}
                        />
                        <button
                          type="button"
                          className="btn"
                          aria-label={`删除字段 ${index + 1}`}
                          onClick={() => {
                            setFields((values) => values.filter((_, i) => i !== index))
                            edited()
                          }}
                        >
                          删除
                        </button>
                      </div>
                      <ValueInput
                        value={field.value}
                        label={`字段 ${index + 1} 的值`}
                        onChange={(value) => {
                          setFields((values) =>
                            values.map((old, i) => (i === index ? { ...old, value } : old)),
                          )
                          edited()
                        }}
                      />
                    </div>
                  ))}
                  <div className={styles.toolbar}>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => {
                        setFields([])
                        edited()
                      }}
                    >
                      清空字典
                    </button>
                    <span className={styles.hint}>保存所有字段，删除的字段会一并生效</span>
                  </div>
                </>
              )}
              {(current.type === 'list' || current.type === 'dict') && (
                <div className={styles.newItem}>
                  <strong>{current.type === 'list' ? '新增元素' : '新增或修改字段'}</strong>
                  {current.type === 'dict' && (
                    <input
                      className={styles.control}
                      aria-label="新增字段名"
                      placeholder="字段名"
                      value={newField}
                      onChange={(event) => setNewField(event.target.value)}
                    />
                  )}
                  <ValueInput value={newValue} label="新增值" onChange={setNewValue} />
                  <div className={styles.toolbar}>
                    <button type="button" className="btn" disabled={dirty} onClick={add}>
                      新增
                    </button>
                    {dirty && <span className={styles.hint}>请先保存当前修改，再新增</span>}
                  </div>
                </div>
              )}
            </fieldset>
          )}
        </Modal>
      )}
    </div>
  )
}
