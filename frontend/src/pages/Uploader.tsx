import { Check, ChevronDown, Folder, Plus, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type Question, type UploaderInfo } from '../api'
import { Avatar, ErrorBox, Pill } from '../components/ui'
import { citeHtml } from '../lib/cite'
import { fmtDuration, fmtMinutes, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { useStore } from '../store'

function UploaderGroupPicker({
  name,
  group,
  allGroups,
  onGroupChange,
}: {
  name: string
  group?: string | null
  allGroups: string[]
  onGroupChange: (next: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const [newGroupInput, setNewGroupInput] = useState('')
  const [saving, setSaving] = useState(false)
  const popoverRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDocClick = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  const selectGroup = async (target: string | null) => {
    setSaving(true)
    try {
      await api.setUploaderGroup(name, target)
      onGroupChange(target)
      setOpen(false)
    } finally {
      setSaving(false)
    }
  }

  const handleAddNew = async () => {
    const val = newGroupInput.trim()
    if (!val) return
    setNewGroupInput('')
    await selectGroup(val)
  }

  return (
    <div className="group-picker-wrap" ref={popoverRef}>
      <button
        type="button"
        className="group-picker-btn"
        onClick={() => setOpen((o) => !o)}
        title="点击设置 UP 主分组"
        disabled={saving}
      >
        <Folder size={13} style={{ color: group ? 'var(--accent)' : 'var(--mute)' }} />
        <span>{group ? group : '未分组'}</span>
        <ChevronDown size={11} style={{ opacity: 0.7 }} />
      </button>

      {open && (
        <div className="group-picker-popup">
          <div className="group-picker-title">UP 主分组</div>
          {allGroups.map((g) => (
            <button
              key={g}
              type="button"
              className={`group-picker-item ${group === g ? 'active' : ''}`}
              onClick={() => selectGroup(g)}
            >
              <span>{g}</span>
              {group === g && <Check size={12} />}
            </button>
          ))}
          <button
            type="button"
            className={`group-picker-item ${!group ? 'active' : ''}`}
            onClick={() => selectGroup(null)}
          >
            <span style={{ color: 'var(--mute)' }}>未分组</span>
            {!group && <Check size={12} />}
          </button>
          <div className="group-picker-new">
            <input
              value={newGroupInput}
              onChange={(e) => setNewGroupInput(e.target.value)}
              placeholder="新建分组…"
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  void handleAddNew()
                }
              }}
            />
            <button type="button" onClick={() => void handleAddNew()} disabled={!newGroupInput.trim()} title="添加分组">
              <Plus size={12} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export function Uploader() {
  const { name = '' } = useParams()
  const { library, refreshLibrary } = useStore()
  const [data, setData] = useState<UploaderInfo | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [q, setQ] = useState('')
  const [asking, setAsking] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => api.uploader(name).then(setData).catch(setErr), [name])
  useEffect(() => { setData(null); setErr(null); void load() }, [load])

  const allGroups = useMemo(() => {
    const set = new Set<string>()
    for (const g of library?.uploader_groups ?? []) set.add(g)
    for (const g of library?.groups ?? []) if (g.group) set.add(g.group)
    return Array.from(set).sort((a, b) => a.localeCompare(b, 'zh-CN'))
  }, [library])

  const handleGroupChange = (newGroup: string | null) => {
    setData((d) => (d ? { ...d, group: newGroup } : d))
    void refreshLibrary()
  }

  const byIndex = useMemo(() => new Map((data?.videos ?? []).map((v) => [v.index, v])), [data])

  const ask = async () => {
    const question = q.trim()
    if (!question || asking || !data) return
    setAsking(true); setErr(null)
    try {
      const history = data.questions.slice(-4).map((t) => ({ question: t.question, answer: t.answer }))
      const a = await api.askUploader(name, question, history)
      setData((d) => d ? { ...d, questions: [...d.questions, a] } : d)
      setQ('')
      requestAnimationFrame(() => listRef.current?.lastElementChild?.scrollIntoView({ block: 'nearest' }))
    } catch (e) { setErr(e) } finally { setAsking(false) }
  }
  const remove = async (id: number) => {
    try { await api.deleteQuestion(id); setData((d) => d ? { ...d, questions: d.questions.filter((x) => x.id !== id) } : d) } catch (e) { setErr(e) }
  }

  if (err && !data) return <div className="page"><ErrorBox error={err} /><p><Link to="/library">回到库</Link></p></div>
  if (!data) return <div className="page" style={{ color: 'var(--mute)' }}><span className="spin" /> 读取中…</div>

  const totalSec = data.videos.reduce((n, v) => n + v.duration_sec, 0)
  const money = fmtMoney(data.estimate.cost, data.estimate.currency)

  return (
    <div className="detail">
      <div className="dhead">
        <div className="in">
          <div className="crumb"><Link to="/library">库</Link><span>›</span><span>UP 主</span></div>
          <h1 style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Avatar name={name} size={28} /> {name}
            <UploaderGroupPicker
              name={name}
              group={data.group}
              allGroups={allGroups}
              onGroupChange={handleGroupChange}
            />
          </h1>
          <div className="row">
            <div className="meta">
              <span>{data.videos.length} 条视频</span>
              <span>{fmtMinutes(totalSec)} 分钟</span>
              {data.no_summary > 0 && <Pill tone="warn">{data.no_summary} 条还没总结，只能用转写开头</Pill>}
            </div>
            <Link className="btn" to={`/library?up=${encodeURIComponent(name)}`}>在库里筛选</Link>
          </div>
        </div>
      </div>

      <div className="split">
        <div className="col left" style={{ width: '54%' }}>
          <div className="colhead"><h2>材料 <span style={{ color: 'var(--faint)', fontWeight: 400 }}>按发布日期</span></h2></div>
          <div className="list">
            {data.videos.map((v) => (
              <Link className="item" to={`/video/${encodeURIComponent(v.video_id)}`} key={v.video_id} style={{ gridTemplateColumns: '28px 1fr auto' }}>
                <span className="mono" style={{ color: 'var(--faint)', fontSize: 12 }}>{v.index}</span>
                <div className="ti">
                  <div className="t" title={v.title}>{v.title}</div>
                  <div className="s">
                    {v.upload_date && <span>{v.upload_date}</span>}
                    <span className="mono">{fmtDuration(v.duration_sec)}</span>
                    <span>{v.material === 'transcript' ? '转写开头' : `用${materialLabel(v.material)}`}</span>
                    <span className="mono">{fmtTokens(v.tokens)} tokens</span>
                  </div>
                </div>
                {v.material === 'transcript' ? <Pill tone="warn">没总结</Pill> : <span />}
              </Link>
            ))}
          </div>
          <p style={{ fontSize: 12.5, color: 'var(--faint)', marginTop: 12 }}>
            问答用的是每条视频的总结（优先「总体」），不是整篇转写——这样问一次约 {money ?? `${fmtTokens(data.estimate.input_tokens)} tokens`}。想让回答更全，先给没总结的视频生成「总体」。
          </p>
        </div>

        <div className="col right">
          <div className="colhead"><h2>问 {name}</h2></div>
          <div className="qa">
            <div className="status">
              <span>只依据这 {data.videos.length} 条视频的内容回答，每条结论标来自哪条，点了打开</span>
              <span className="mono">每问约 {money ?? `${fmtTokens(data.estimate.input_tokens)} tokens`}</span>
            </div>
            <ErrorBox error={err} />
            <div className="qa-list" ref={listRef}>
              {data.questions.length === 0 && !asking && (
                <div className="qa-empty">还没问过。试试「她关于防晒的观点是什么，前后有没有变化」「她推荐过哪些产品」。</div>
              )}
              {data.questions.map((t: Question) => (
                <div className="turn" key={t.id}>
                  <div className="qq"><span>{t.question}</span><button className="iconbtn danger" title="删除这条" onClick={() => remove(t.id)}><Trash2 /></button></div>
                  <div className="aa" dangerouslySetInnerHTML={{ __html: citeHtml(t.answer, byIndex) }} />
                  <div className="af">
                    {t.citations.length > 0
                      ? <span>引用了 {t.citations.length} 条视频</span>
                      : <span className="warnish">没有引用——回答可能不在这些视频里</span>}
                    <span className="sp" /><span>{fmtWhen(t.created_at)}</span>
                  </div>
                </div>
              ))}
              {asking && <div className="turn"><div className="qq"><span>{q.trim()}</span></div><div className="aa thinking"><span className="spin" /> 在读 {data.videos.length} 条视频的总结…</div></div>}
            </div>
            <form className="ask" onSubmit={(e) => { e.preventDefault(); void ask() }}>
              <div className="box">
                <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder={`问 ${name}…`} disabled={asking} aria-label="问 UP 主"
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); void ask() } }} />
                <button className="btn primary sm" type="submit" disabled={!q.trim() || asking || data.videos.length === 0}>{asking ? '…' : '问'}</button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  )
}

function materialLabel(k: string): string {
  return ({ overall: '总体', key_points: '要点', timeline: '时间线', by_speaker: '分角色', mindmap: '思维导图' } as Record<string, string>)[k] ?? k
}
