import { Plus, Sparkles, X } from 'lucide-react'
import { useState, type MouseEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, type Tag } from '../api'
import { ErrorBox } from './ui'

/** 一枚标签。给了 to 就是链接（库里按标签筛选），给了 onRemove 就带 ×。 */
export function TagChip({ tag, source, active, count, to, onClick, onRemove }: {
  tag: string
  source?: 'ai' | 'user'
  active?: boolean
  count?: number
  to?: string
  onClick?: () => void
  onRemove?: () => void
}) {
  const cls = `tag${source === 'user' ? ' user' : ''}${active ? ' on' : ''}${onClick || to ? ' click' : ''}`
  const body = <>
    <span className="h">#</span>{tag}
    {count != null && <span className="c">{count}</span>}
  </>
  const stop = (e: MouseEvent) => { e.stopPropagation() }
  return (
    <span className={cls} title={source === 'user' ? '手动加的' : source === 'ai' ? 'AI 打的' : undefined} onClick={stop}>
      {to
        ? <Link to={to} onClick={stop}>{body}</Link>
        : onClick
          ? <button type="button" onClick={(e) => { stop(e); onClick() }}>{body}</button>
          : <span>{body}</span>}
      {onRemove && <button type="button" className="x" title="去掉这个标签" aria-label={`去掉标签 ${tag}`} onClick={(e) => { stop(e); onRemove() }}><X /></button>}
    </span>
  )
}

/** 详情页的标签编辑：AI 标签 + 手动标签，能加能删，能让 AI 重新打。 */
export function TagEditor({ videoId, tags, onChange, hasMaterial }: {
  videoId: string
  tags: Tag[]
  onChange: (tags: Tag[]) => void
  hasMaterial: boolean
}) {
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState<'add' | 'gen' | null>(null)
  const [err, setErr] = useState<unknown>(null)

  const add = async () => {
    const t = draft.trim()
    if (!t) { setAdding(false); return }
    setBusy('add'); setErr(null)
    try {
      const r = await api.addTag(videoId, t)
      onChange(r.tags); setDraft('')
    } catch (e) { setErr(e) } finally { setBusy(null) }
  }
  const remove = async (tag: string) => {
    setErr(null)
    try { onChange((await api.removeTag(videoId, tag)).tags) } catch (e) { setErr(e) }
  }
  const generate = async () => {
    setBusy('gen'); setErr(null)
    try { onChange((await api.generateTags(videoId)).tags) } catch (e) { setErr(e) } finally { setBusy(null) }
  }

  return (
    <div className="tags">
      {tags.map((t) => (
        <TagChip key={t.tag} tag={t.tag} source={t.source} to={`/library?tag=${encodeURIComponent(t.tag)}`} onRemove={() => remove(t.tag)} />
      ))}
      {adding
        ? <input className="tagin" autoFocus value={draft} placeholder="标签，回车确认" maxLength={20} disabled={busy === 'add'}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => { if (!draft.trim()) setAdding(false) }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); void add() }
              if (e.key === 'Escape') { setDraft(''); setAdding(false) }
            }} />
        : <button type="button" className="tagbtn" onClick={() => setAdding(true)}><Plus /> 加标签</button>}
      <button type="button" className="tagbtn" onClick={generate} disabled={busy != null || !hasMaterial}
        title={hasMaterial ? (tags.some((t) => t.source === 'ai') ? '让 AI 重新打一遍。手动加的和删过的不动' : '让 AI 从总结里提几个主题词') : '这条没有转写和总结，AI 没材料可用'}>
        {busy === 'gen' ? <span className="spin" /> : <Sparkles />} {tags.some((t) => t.source === 'ai') ? 'AI 重打' : 'AI 打标签'}
      </button>
      <ErrorBox error={err} />
    </div>
  )
}
