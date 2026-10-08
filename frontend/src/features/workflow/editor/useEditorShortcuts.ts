/**
 * 画布快捷键：Delete 删除 / Ctrl+Z 撤销 / Ctrl+S 暂存 / Ctrl+C 复制 / Ctrl+X 剪切 / Ctrl+V 粘贴。
 *
 * 两条约定：
 *
 * * **输入框里不抢**：退格与文本复制粘贴归输入框自己（``isEditingTarget``），只有 Ctrl+S
 *   例外 —— 它在哪儿都生效，顺手把浏览器默认的「保存网页」也拦了；
 * * 动作从 ``actions`` 里取**最新**那份（存 ref），监听因此只在挂载时注册一次：
 *   不用每次某个动作换了身份就摘挂一遍监听。
 */
import { useEffect, useRef } from 'react'
import { isEditingTarget } from './catalog'

export interface ShortcutActions {
  /** 正在放置粘贴虚影吗（Esc 取消它） */
  isPlacing: () => boolean
  cancelPlacing: () => void
  /** 暂存到后端 */
  draft: () => void
  /** 暂存中（避免连点重复提交） */
  drafting: boolean
  undo: () => void
  /** 返回复制到的节点数（0 = 没选中什么） */
  copySelection: () => number
  cut: (ids: Iterable<string>) => number
  getSelectionIds: () => Set<string>
  startPlacing: () => void
  /** 内存剪贴板里有没有货：Ctrl+V 能不能同步拦下默认行为 */
  hasLocalClipboard: () => boolean
  removeMany: (ids: string[]) => void
}

export function useEditorShortcuts(actions: ShortcutActions): void {
  const actionsRef = useRef(actions)
  useEffect(() => {
    actionsRef.current = actions
  })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat) return
      const mod = e.ctrlKey || e.metaKey
      const key = e.key.toLowerCase()
      const a = actionsRef.current

      // Esc：正在放置的粘贴虚影取消（不落子）
      if (e.key === 'Escape' && a.isPlacing()) {
        a.cancelPlacing()
        return
      }

      // Ctrl+S 暂存：输入框里也照常生效（先拦掉浏览器默认的「保存网页」）
      if (mod && key === 's') {
        e.preventDefault()
        if (!a.drafting) void a.draft()
        return
      }

      // 输入框 / 下拉里：退格与文本复制粘贴归它们，不抢
      if (isEditingTarget(e.target)) return

      if (mod && key === 'z' && !e.shiftKey) {
        e.preventDefault()
        // 先收掉挂着的虚影，再撤销上一步
        a.cancelPlacing()
        a.undo()
        return
      }

      if (mod && (key === 'c' || key === 'x')) {
        // Ctrl+C 复制当前选择；Ctrl+X 走同一条「复制 + 删除」
        const copied = key === 'x' ? a.cut(a.getSelectionIds()) : a.copySelection()
        if (copied === 0) return // 没选中什么就不劫持
        e.preventDefault()
        return
      }

      if (mod && key === 'v') {
        // 两处剪贴板都读（内存 + 系统），谁新用谁；两份都没货就什么都不发生。
        // 内存有货时能同步拦下默认行为；只剩系统剪贴板那一份时要等 promise，
        // 赶不上这一发 preventDefault —— 画布上本来就没有可输入目标（输入框上面
        // 已经放行走了），不拦也不碍事。
        if (a.hasLocalClipboard()) e.preventDefault()
        a.startPlacing()
        return
      }

      if (e.key === 'Delete' || e.key === 'Backspace') {
        const ids = a.getSelectionIds()
        if (ids.size === 0) return
        e.preventDefault()
        a.removeMany([...ids])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
