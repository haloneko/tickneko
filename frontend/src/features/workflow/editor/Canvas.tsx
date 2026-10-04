/**
 * 画布本体：事件面 + 两层变换 + 内容插槽。
 *
 * 两层是刻意拆开的：
 *
 * * 外层 ``canvasViewport`` 只做**平移**（``transform: translate``）—— 位移不会让文字变糊，
 *   可以放心交给合成器加速；
 * * 内层 ``canvasContent`` 用 **CSS ``zoom``** 缩放 —— 按新尺寸重新排版，文字与 SVG 都是
 *   矢量重绘，放到多大都清晰。``transform: scale`` 只是把已经栅格化的图层拉大（再加上
 *   will-change，浏览器干脆一直拿旧图拉伸），放大就是糊的、要等一次重绘才清楚。
 *
 * 两条变换合起来与原来的 ``translate(pan) scale(zoom)`` 数学等价，坐标换算不用改。
 */
import type { ReactNode } from 'react'
import type { Point } from './catalog'
import { canvasGridStyle } from './canvasGeometry'
import styles from '../WorkflowEditor.module.css'

export interface BoxRect {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface CanvasProps {
  canvasRef: React.RefObject<HTMLDivElement>
  pan: Point
  zoom: number
  /** 正在右键平移（光标变抓手；读的是 ref，父组件在这里给个布尔） */
  panning: boolean
  /** 加载中 / 目录加载失败时显示它，代替画布内容 */
  placeholder?: ReactNode
  /** 一个节点都没有时的提示 */
  empty: boolean
  /** 框选矩形（null = 没在框选） */
  boxSel: BoxRect | null
  onMouseDown: (event: React.MouseEvent) => void
  onMouseMove: (event: React.MouseEvent) => void
  onMouseUp: () => void
  onWheel: (event: React.WheelEvent) => void
  onClick: () => void
  children: ReactNode
}

export function Canvas({
  canvasRef,
  pan,
  zoom,
  panning,
  placeholder,
  empty,
  boxSel,
  onMouseDown,
  onMouseMove,
  onMouseUp,
  onWheel,
  onClick,
  children,
}: CanvasProps) {
  return (
    <div
      ref={canvasRef}
      className={styles.canvas}
      style={{ cursor: panning ? 'grabbing' : 'default', ...canvasGridStyle(pan, zoom) }}
      onContextMenu={(e) => e.preventDefault()}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onWheel={onWheel}
      onClick={onClick}
    >
      {placeholder ?? (
        <div
          className={styles.canvasViewport}
          style={{ transform: `translate(${pan.x}px, ${pan.y}px)` }}
        >
          <div className={styles.canvasContent} style={{ zoom: String(zoom) }}>
            {children}
            {empty && <div className={styles.empty}>从左侧点节点名添加到画布</div>}
            {boxSel && <BoxSelection rect={boxSel} />}
          </div>
        </div>
      )}
    </div>
  )
}

/** 框选矩形：渲染在节点之后（DOM 序天然在最上），不需要 z-index */
function BoxSelection({ rect }: { rect: BoxRect }) {
  const x = Math.min(rect.x0, rect.x1)
  const y = Math.min(rect.y0, rect.y1)
  const w = Math.abs(rect.x1 - rect.x0)
  const h = Math.abs(rect.y1 - rect.y0)
  return (
    <svg className={styles.edges} style={{ pointerEvents: 'none' }}>
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        fill="rgba(99, 102, 241, 0.08)"
        stroke="var(--accent)"
        strokeWidth="1"
        strokeDasharray="4 3"
      />
    </svg>
  )
}
