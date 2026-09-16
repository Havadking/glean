// UP 主分组：拿分组列表 + 改分组的动作，以及侧栏 / UP 主页共用的小弹出菜单。

import { Check, FolderMinus, Plus } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'
import { useStore } from '../store'

export interface UploaderGroup { name: string; uploaders: number; videos: number }

/** 库里的分组（按中文排序）和改分组的动作；改完刷新库，侧栏和库页一起更新。 */
export function useUploaderGroups() {
  const { library, refreshLibrary } = useStore()
  const groups = useMemo<UploaderGroup[]>(() => {
    const m = new Map<string, UploaderGroup>()
    for (const name of library?.uploader_groups ?? []) m.set(name, { name, uploaders: 0, videos: 0 })
    for (const g of library?.groups ?? []) {
      if (!g.uploader || !g.group) continue
      const it = m.get(g.group) ?? { name: g.group, uploaders: 0, videos: 0 }
      it.uploaders += 1
      it.videos += g.entries.length
      m.set(g.group, it)
    }
    return Array.from(m.values()).sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  }, [library])

  const setGroup = useCallback(async (uploader: string, group: string | null) => {
    await api.setUploaderGroup(uploader, group?.trim() || null)
    await refreshLibrary()
  }, [refreshLibrary])
  const rename = useCallback(async (from: string, to: string) => {
    const t = to.trim()
    if (!t || t === from) return
    await api.renameUploaderGroup(from, t)
    await refreshLibrary()
  }, [refreshLibrary])
  const dissolve = useCallback(async (name: string) => {
    await api.deleteUploaderGroup(name)
    await refreshLibrary()
  }, [refreshLibrary])

  return { groups, setGroup, rename, dissolve }
}

/** 贴在 anchor 下面的小弹层，用 portal 挂到 body，不会被侧栏的滚动裁掉；点外面或 Esc 关。 */
export function Popover({ anchor, onClose, children }: { anchor: HTMLElement; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const a = anchor.getBoundingClientRect()
    const r = el.getBoundingClientRect()
    let top = a.bottom + 4
    if (top + r.height > window.innerHeight - 8) top = Math.max(8, a.top - r.height - 4)
    let left = a.left
    if (left + r.width > window.innerWidth - 8) left = Math.max(8, window.innerWidth - 8 - r.width)
    setPos({ top, left })
  }, [anchor])

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (ref.current?.contains(t) || anchor.contains(t)) return
      onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [anchor, onClose])

  return createPortal(
    <div className="pop" ref={ref} role="menu" style={pos ? { top: pos.top, left: pos.left } : { top: 0, left: 0, visibility: 'hidden' }}>
      {children}
    </div>,
    document.body,
  )
}

/** 给某位 UP 主挑分组：已有的打勾，能移出，也能当场新建。 */
export function GroupMenu({ anchor, current, groups, onPick, onClose }: {
  anchor: HTMLElement
  current: string | null | undefined
  groups: UploaderGroup[]
  onPick: (group: string | null) => Promise<void> | void
  onClose: () => void
}) {
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const pick = async (g: string | null) => { await onPick(g); onClose() }
  const create = () => { const v = name.trim(); if (v) void pick(v) }
  return (
    <Popover anchor={anchor} onClose={onClose}>
      <div className="ph">移到分组</div>
      {groups.map((g) => (
        <button type="button" role="menuitem" className={`pi${current === g.name ? ' on' : ''}`} key={g.name} onClick={() => void pick(g.name)}>
          <span className="sp">{g.name}</span>
          {current === g.name ? <Check /> : <span className="n">{g.uploaders}</span>}
        </button>
      ))}
      {groups.length > 0 && <div className="pd" />}
      {creating ? (
        <div className="pin">
          <input
            autoFocus
            value={name}
            placeholder="新分组名，回车确认"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); create() }
              if (e.key === 'Escape') { e.stopPropagation(); setCreating(false); setName('') }
            }}
          />
        </div>
      ) : (
        <button type="button" role="menuitem" className="pi" onClick={() => setCreating(true)}><Plus /> 新建分组…</button>
      )}
      {current && (
        <button type="button" role="menuitem" className="pi" onClick={() => void pick(null)}><FolderMinus /> 移出「{current}」</button>
      )}
    </Popover>
  )
}
