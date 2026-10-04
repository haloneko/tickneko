import assert from 'node:assert/strict'
import { test } from 'node:test'
import { canvasGridStyle, centeredNodePosition } from '../src/features/workflow/editor/canvasGeometry.ts'

const viewport = { width: 1280, height: 720 }
const node = { width: 200, height: 150 }
for (const zoom of [0.25, 0.5, 1, 2, 3]) {
  for (const pan of [{ x: 0, y: 0 }, { x: -8000, y: 4000 }, { x: 2500, y: -5000 }]) {
    test(`click placement stays centered and visible at zoom ${zoom}, pan ${JSON.stringify(pan)}`, () => {
      const position = centeredNodePosition(viewport, node, pan, zoom)
      const screen = { x: position.x * zoom + pan.x, y: position.y * zoom + pan.y }
      assert.ok(Math.abs(screen.x + node.width * zoom / 2 - viewport.width / 2) < 1e-8)
      assert.ok(Math.abs(screen.y + node.height * zoom / 2 - viewport.height / 2) < 1e-8)
      assert.ok(screen.x >= 0 && screen.y >= 0)
      assert.ok(screen.x + node.width * zoom <= viewport.width)
      assert.ok(screen.y + node.height * zoom <= viewport.height)
      assert.deepEqual(centeredNodePosition(viewport, node, pan, zoom), position)
    })
    test(`grid follows the same zoom ${zoom} and pan ${JSON.stringify(pan)}`, () => {
      const style = canvasGridStyle(pan, zoom)
      assert.equal(style.backgroundSize, `${20 * zoom}px ${20 * zoom}px`)
      assert.equal(style.backgroundPosition, `${pan.x}px ${pan.y}px`)
      assert.equal(style.backgroundImage, `radial-gradient(circle, var(--border) ${zoom}px, transparent ${zoom}px)`)
    })
  }
}

test('oversized nodes retain a visible top-left/header instead of spawning wholly off-screen', () => {
  const position = centeredNodePosition({ width: 300, height: 200 }, { width: 220, height: 400 }, { x: 900, y: -600 }, 3)
  assert.equal(position.x * 3 + 900, 16)
  assert.equal(position.y * 3 - 600, 16)
})
