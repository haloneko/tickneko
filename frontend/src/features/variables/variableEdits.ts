/** 编辑草稿与提交编码：保留 JSON 类型、顺序和重复元素，完整集合保存负责删除。 */
export interface ValueDraft {
  text: string
  format: 'text' | 'json'
}

export interface FieldDraft {
  field: string
  value: ValueDraft
}

export function draftValue(value: unknown): ValueDraft {
  return typeof value === 'string'
    ? { text: value, format: 'text' }
    : { text: JSON.stringify(value, null, 2) ?? 'null', format: 'json' }
}

export function parseValue(value: ValueDraft): unknown {
  if (value.format === 'text') return value.text
  let parsed: unknown
  try {
    parsed = JSON.parse(value.text) as unknown
  } catch {
    throw new Error('JSON 格式不正确，请检查后再提交')
  }
  JSON.stringify(parsed, (_key, entry: unknown) => {
    if (typeof entry === 'number' && !Number.isFinite(entry)) {
      throw new Error('JSON 数字必须是有限值')
    }
    return entry
  })
  return parsed
}

export function listPayload(items: ValueDraft[]): { items: unknown[] } {
  return { items: items.map(parseValue) }
}

export function dictPayload(fields: FieldDraft[]): { fields: Record<string, unknown> } {
  const names = new Set<string>()
  const pairs = fields.map(({ field, value }) => {
    if (!field) throw new Error('字段名不能为空')
    if (names.has(field)) throw new Error(`字段名重复：${field}`)
    names.add(field)
    return [field, parseValue(value)] as const
  })
  return { fields: Object.fromEntries(pairs) }
}
