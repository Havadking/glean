import { BookOpenCheck, BookmarkPlus, Check, ChevronDown, RefreshCw } from 'lucide-react'
import { useRef, useState } from 'react'
import { api, type ObsidianNote, type ObsidianPart } from '../api'
import { useStore } from '../store'
import { Popover } from './GroupMenu'

const PARTS: { key: ObsidianPart; label: string }[] = [
  { key: 'summaries', label: '总结' },
  { key: 'qa', label: '问答' },
  { key: 'transcript', label: '转写全文' },
]

/**
 * 存到 Obsidian：没存过是「存到 Obsidian」，存过变成「在 Obsidian 中打开」。
 * 旁边的小箭头挑这次带哪些内容、或者把最新的总结重新存一遍（原地更新，不动「我的笔记」）。
 */
export function ObsidianButton({ videoId, note, onSaved }: {
  videoId: string
  note: ObsidianNote | null
  onSaved: (note: ObsidianNote | null) => void
}) {
  const { meta } = useStore()
  const caret = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const [include, setInclude] = useState<ObsidianPart[]>(() => meta?.obsidian_include ?? PARTS.map((p) => p.key))
  const [saving, setSaving] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)

  if (!meta?.obsidian_enabled) return null

  const save = async () => {
    setOpen(false)
    setSaving(true)
    try {
      const r = await api.saveToObsidian(videoId, include)
      onSaved(r.obsidian)
      setFlash(r.action === 'created' ? '已存入' : '已更新')
      setTimeout(() => setFlash(null), 1800)
    } catch (e) {
      // 存失败不该把整页换成错误框，弹一下就行（多半是库路径不对）
      alert(`存到 Obsidian 失败：${e instanceof Error ? e.message : String(e)}`)
    } finally { setSaving(false) }
  }
  const toggle = (k: ObsidianPart) =>
    setInclude((cur) => cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k])

  return (
    <div className="btnsplit">
      {note ? (
        <a className="btn" href={note.uri} title={`库里的 ${note.path}`}>
          {flash ? <><Check /> {flash}</> : <><BookOpenCheck /> 在 Obsidian 中打开</>}
        </a>
      ) : (
        <button className="btn" onClick={() => void save()} disabled={saving || include.length === 0}
          title="写成库里的一篇笔记：总结、问答、转写，带封面和能跳回原视频的时间戳">
          {saving ? <><span className="spin" /> 存入中…</> : <><BookmarkPlus /> 存到 Obsidian</>}
        </button>
      )}
      <button className="btn" ref={caret} onClick={() => setOpen((o) => !o)} aria-label="存入选项" disabled={saving}>
        {saving && note ? <span className="spin" /> : <ChevronDown />}
      </button>
      {open && caret.current && (
        <Popover anchor={caret.current} onClose={() => setOpen(false)}>
          <div className="ph">{note ? note.path : '这次带上'}</div>
          {PARTS.map((p) => (
            <button type="button" role="menuitemcheckbox" aria-checked={include.includes(p.key)} key={p.key}
              className={`pi${include.includes(p.key) ? ' on' : ''}`} onClick={() => toggle(p.key)}>
              <span className="sp">{p.label}</span>
              {include.includes(p.key) && <Check />}
            </button>
          ))}
          <div className="pd" />
          <button type="button" role="menuitem" className="pi" onClick={() => void save()} disabled={include.length === 0}>
            {note ? <><RefreshCw /> 重新存一遍（保留我的笔记）</> : <><BookmarkPlus /> 存到 Obsidian</>}
          </button>
        </Popover>
      )}
    </div>
  )
}
