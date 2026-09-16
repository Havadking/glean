// 侧栏的 UP 主列表：有分组就按组折叠，没分组保持平铺。
// 拖 UP 主到组头上换组；组头和 UP 主行 hover 出来的「…」能做同样的事，不用进详情页。

import { ChevronRight, FolderMinus, FolderPlus, List, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { useMemo, useState, type DragEvent, type MouseEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { type LibraryGroup } from '../api'
import { useStore } from '../store'
import { GroupMenu, Popover, useUploaderGroups } from './GroupMenu'
import { Avatar } from './ui'

const LS_KEY = 'vsum.sidebar.collapsedUpGroups'
const DND = 'text/x-vsum-uploader'

type MenuSpec = { kind: 'up'; name: string; group: string | null } | { kind: 'group'; name: string }
type Menu = MenuSpec & { anchor: HTMLElement }

export function UploaderTree() {
  const { library } = useStore()
  const { groups: upGroups, setGroup, rename, dissolve } = useUploaderGroups()
  const nav = useNavigate()

  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    try { const raw = localStorage.getItem(LS_KEY); return raw ? new Set(JSON.parse(raw)) : new Set() } catch { return new Set() }
  })
  const [menu, setMenu] = useState<Menu | null>(null)
  const [renaming, setRenaming] = useState<{ from: string; value: string } | null>(null)
  const [dragging, setDragging] = useState<{ name: string; group: string | null } | null>(null)
  const [over, setOver] = useState<string | null>(null)
  const [newFor, setNewFor] = useState<{ uploader: string; value: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const uploaders = useMemo(() => (library?.groups ?? []).filter((g) => g.uploader), [library])
  const { grouped, ungrouped } = useMemo(() => {
    const grouped = new Map<string, LibraryGroup[]>()
    const ungrouped: LibraryGroup[] = []
    for (const g of uploaders) {
      const key = g.group?.trim()
      if (!key) { ungrouped.push(g); continue }
      grouped.set(key, [...(grouped.get(key) ?? []), g])
    }
    return { grouped, ungrouped }
  }, [uploaders])

  if (uploaders.length === 0) return null

  const run = async (fn: () => Promise<void>) => {
    setErr(null)
    try { await fn() } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }

  const toggle = (name: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name); else next.add(name)
      try { localStorage.setItem(LS_KEY, JSON.stringify(Array.from(next))) } catch { /* ignore */ }
      return next
    })
  }

  const openMenu = (e: MouseEvent<HTMLButtonElement>, m: MenuSpec) => {
    e.stopPropagation()
    const anchor = e.currentTarget
    setMenu((cur) => (cur?.anchor === anchor ? null : { ...m, anchor }))
  }

  // ---- 拖拽 ----
  const onDragStart = (e: DragEvent, g: LibraryGroup) => {
    e.dataTransfer.setData(DND, g.uploader!)
    e.dataTransfer.effectAllowed = 'move'
    setDragging({ name: g.uploader!, group: g.group ?? null })
  }
  const onDragEnd = () => { setDragging(null); setOver(null) }
  const dropProps = (key: string, onDrop: (uploader: string) => void) => ({
    onDragOver: (e: DragEvent) => { if (dragging) { e.preventDefault(); e.dataTransfer.dropEffect = 'move' } },
    onDragEnter: (e: DragEvent) => { if (dragging) { e.preventDefault(); setOver(key) } },
    onDragLeave: (e: DragEvent) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setOver((o) => (o === key ? null : o)) },
    onDrop: (e: DragEvent) => {
      e.preventDefault()
      const name = e.dataTransfer.getData(DND) || dragging?.name
      setDragging(null); setOver(null)
      if (name) onDrop(name)
    },
  })

  const row = (g: LibraryGroup) => {
    const to = `/uploader/${encodeURIComponent(g.uploader!)}`
    return (
      <div
        className={`up${dragging?.name === g.uploader ? ' dragging' : ''}`}
        key={g.uploader!}
        role="link"
        tabIndex={0}
        draggable
        onDragStart={(e) => onDragStart(e, g)}
        onDragEnd={onDragEnd}
        onClick={() => nav(to)}
        onKeyDown={(e) => { if (e.key === 'Enter') nav(to) }}
        title={g.uploader!}
      >
        <Avatar name={g.uploader} />
        <span className="n">{g.uploader}</span>
        <span className="c">{g.entries.length}</span>
        <button
          type="button"
          className={`more${menu?.kind === 'up' && menu.name === g.uploader ? ' open' : ''}`}
          title="移到分组"
          onClick={(e) => openMenu(e, { kind: 'up', name: g.uploader!, group: g.group ?? null })}
        >
          <MoreHorizontal />
        </button>
      </div>
    )
  }

  return (
    <>
      <div className="sec">UP 主</div>

      {upGroups.map((ug) => {
        const items = grouped.get(ug.name) ?? []
        const closed = collapsed.has(ug.name)
        const videos = items.reduce((n, g) => n + g.entries.length, 0)
        const isRenaming = renaming?.from === ug.name
        return (
          <div className="sgrp" key={ug.name}>
            <div
              className={`sgh${closed ? ' closed' : ''}${over === `g:${ug.name}` ? ' over' : ''}`}
              role="button"
              tabIndex={0}
              onClick={() => { if (!isRenaming) toggle(ug.name) }}
              onKeyDown={(e) => { if (!isRenaming && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggle(ug.name) } }}
              {...dropProps(`g:${ug.name}`, (u) => void run(() => setGroup(u, ug.name)))}
            >
              <ChevronRight className="chev" />
              {isRenaming ? (
                <input
                  className="sin"
                  autoFocus
                  value={renaming.value}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => setRenaming({ from: ug.name, value: e.target.value })}
                  onBlur={() => setRenaming(null)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') { e.preventDefault(); const v = renaming.value; setRenaming(null); void run(() => rename(ug.name, v)) }
                    if (e.key === 'Escape') setRenaming(null)
                  }}
                />
              ) : (
                <span className="n">{ug.name}</span>
              )}
              <span className="c">{videos}</span>
              <button
                type="button"
                className={`more${menu?.kind === 'group' && menu.name === ug.name ? ' open' : ''}`}
                title="分组操作"
                onClick={(e) => openMenu(e, { kind: 'group', name: ug.name })}
              >
                <MoreHorizontal />
              </button>
            </div>
            {!closed && <div className="sgm">{items.map(row)}</div>}
          </div>
        )
      })}

      {upGroups.length > 0 && ungrouped.length > 0 && <div className="sdiv" />}
      {ungrouped.map(row)}

      {dragging && (
        <div className="sdrops">
          <div className={`sdrop${over === 'new' ? ' over' : ''}`} {...dropProps('new', (u) => setNewFor({ uploader: u, value: '' }))}>
            <FolderPlus /> 拖到这里新建分组
          </div>
          {dragging.group && (
            <div className={`sdrop${over === 'none' ? ' over' : ''}`} {...dropProps('none', (u) => void run(() => setGroup(u, null)))}>
              <FolderMinus /> 移出「{dragging.group}」
            </div>
          )}
        </div>
      )}

      {newFor && (
        <div className="snew">
          <span className="lbl">把 {newFor.uploader} 放进新分组</span>
          <input
            className="sin"
            autoFocus
            value={newFor.value}
            placeholder="分组名，回车确认"
            onChange={(e) => setNewFor({ ...newFor, value: e.target.value })}
            onBlur={() => setNewFor(null)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); const { uploader, value } = newFor; setNewFor(null); if (value.trim()) void run(() => setGroup(uploader, value)) }
              if (e.key === 'Escape') setNewFor(null)
            }}
          />
        </div>
      )}

      {err && <div className="serr">{err}</div>}

      {menu?.kind === 'up' && (
        <GroupMenu
          anchor={menu.anchor}
          current={menu.group}
          groups={upGroups}
          onPick={(g) => run(() => setGroup(menu.name, g))}
          onClose={() => setMenu(null)}
        />
      )}
      {menu?.kind === 'group' && (
        <Popover anchor={menu.anchor} onClose={() => setMenu(null)}>
          <div className="ph">{menu.name}</div>
          <button type="button" role="menuitem" className="pi" onClick={() => { setMenu(null); nav(`/library?group=${encodeURIComponent(menu.name)}`) }}>
            <List /> 在库里只看这组
          </button>
          <button type="button" role="menuitem" className="pi" onClick={() => { setMenu(null); setRenaming({ from: menu.name, value: menu.name }) }}>
            <Pencil /> 重命名
          </button>
          <div className="pd" />
          <button
            type="button"
            role="menuitem"
            className="pi danger"
            onClick={() => {
              const name = menu.name
              setMenu(null)
              if (confirm(`解散「${name}」？里面的 UP 主回到未分组，视频不受影响。`)) void run(() => dissolve(name))
            }}
          >
            <Trash2 /> 解散分组
          </button>
        </Popover>
      )}
    </>
  )
}
