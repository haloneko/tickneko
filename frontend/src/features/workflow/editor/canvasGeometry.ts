import type { Point } from './catalog'

interface Size {
  width: number
  height: number
}

/** 点击添加以当前可视区中心为锚点，而不是固定在画布原点附近。 */
export function centeredNodePosition(viewport: Size, node: Size, pan: Point, zoom: number): Point {
  const margin = 16
  // 卡片比窗口还大时至少把左上角和标题留在视野里，不改变用户的缩放。
  const screenX = Math.max(Math.min(margin, viewport.width / 2), (viewport.width - node.width * zoom) / 2)
  const screenY = Math.max(Math.min(margin, viewport.height / 2), (viewport.height - node.height * zoom) / 2)
  return { x: (screenX - pan.x) / zoom, y: (screenY - pan.y) / zoom }
}

/** 网格与节点使用同一份镜头变换；点距与点半径均随缩放变化。 */
export function canvasGridStyle(pan: Point, zoom: number) {
  const spacing = 20 * zoom
  return {
    backgroundImage: `radial-gradient(circle, var(--border) ${zoom}px, transparent ${zoom}px)`,
    backgroundSize: `${spacing}px ${spacing}px`,
    backgroundPosition: `${pan.x}px ${pan.y}px`,
  }
}
