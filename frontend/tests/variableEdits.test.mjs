import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  draftValue,
  parseValue,
  listPayload,
  dictPayload,
} from '../src/features/variables/variableEdits.ts'

test('JSON drafts retain false, zero, null and nested values', () => {
  for (const value of [false, 0, null, [false, 0], { nested: null }]) {
    assert.deepEqual(parseValue(draftValue(value)), value)
  }
  assert.equal(parseValue(draftValue('false')), 'false')
  assert.equal(parseValue(draftValue('')), '')
})

test('list full save preserves order, duplicates, types and explicit clearing', () => {
  assert.deepEqual(listPayload(['a', 'a', '', false, 0, null].map(draftValue)), {
    items: ['a', 'a', '', false, 0, null],
  })
  assert.deepEqual(listPayload([]), { items: [] })
})

test('dict full save omits removed fields and rejects duplicates', () => {
  const fields = [
    { field: 'keep', value: draftValue(false) },
    { field: '__proto__', value: draftValue(0) },
  ]
  const payload = dictPayload(fields)
  assert.deepEqual(Object.keys(payload.fields), ['keep', '__proto__'])
  assert.equal(payload.fields.__proto__, 0)
  assert.deepEqual(dictPayload([]), { fields: {} })
  assert.throws(() => dictPayload([fields[0], fields[0]]), /字段名重复/)
  assert.throws(() => dictPayload([{ field: '', value: draftValue('') }]), /字段名不能为空/)
})

test('invalid JSON fails before forming a request', () => {
  const bad = { text: '{oops', format: 'json' }
  assert.throws(() => listPayload([bad]), /JSON 格式不正确/)
  assert.throws(() => dictPayload([{ field: 'x', value: bad }]), /JSON 格式不正确/)
})

test('overflowing JSON numbers are rejected before transport can turn them into null', () => {
  assert.throws(() => parseValue({ text: '1e400', format: 'json' }), /有限值/)
  assert.throws(() => parseValue({ text: '{"value":1e400}', format: 'json' }), /有限值/)
})
