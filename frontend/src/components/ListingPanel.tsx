import { Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api, type Listing, type ListingEntry } from '../api'
import { fmtDuration, fmtMinutes, fmtMoney } from '../lib/format'
import { useStore } from '../store'
import { ErrorBox, Pill } from './ui'

const TOKENS_PER_SEC = 3.2

/** 合集 / UP 主空间：一页一页翻，勾选要处理的，排队。 */
export function ListingPanel({ listing, url, onSubmitted }: {
  listing: Listing
  url: string
  onSubmitted: (queued: number) => void
}) {
  const { meta } = useStore()
  const [pages, setPages] = useState<Listing[]>([listing])
  const [keyword, setKeyword] = useState(listing.keyword ?? '')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<unknown>(null)
  const [selected, setSelected] = useState<Set<string>>(() => new Set(
    listing.entries.filter((e) => !e.in_library && !e.queued).map((e) => e.video_id)))
  const [summaryType, setSummaryType] = useState<string>('')
  const [asr, setAsr] = useState<string>('')
  const [correctTerms, setCorrectTerms] = useState<boolean>(false)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (meta) {
      setSummaryType((t) => t || meta.default_summary_type); setAsr((a) => a || meta.asr_default)
      setCorrectTerms(meta.correct_terms_default)
    }
  }, [meta])
  useEffect(() => { setPages([listing]); setKeyword(listing.keyword ?? '') }, [listing])

  const entries = useMemo(() => {
    const seen = new Set<string>()
    const out: ListingEntry[] = []
    for (const p of pages) for (const e of p.entries) if (!seen.has(e.video_id)) { seen.add(e.video_id); out.push(e) }
    return out
  }, [pages])
  const last = pages[pages.length - 1]
  const canSearch = listing.list_kind === 'space'

  const reload = async (kw: string) => {
    setLoading(true); setErr(null)
    try {
      const p = await api.probe(url, 1, kw)
      if (p.kind !== 'list') throw new Error('这个链接现在返回的不是列表')
      setPages([p])
      setSelected(new Set(p.entries.filter((e) => !e.in_library && !e.queued).map((e) => e.video_id)))
    } catch (e) { setErr(e) } finally { setLoading(false) }
  }
  const more = async () => {
    setLoading(true); setErr(null)
    try {
      const p = await api.probe(url, last.page + 1, keyword)
      if (p.kind !== 'list') return
      setPages((ps) => [...ps, p])
      setSelected((s) => { const n = new Set(s); p.entries.forEach((e) => { if (!e.in_library && !e.queued) n.add(e.video_id) }); return n })
    } catch (e) { setErr(e) } finally { setLoading(false) }
  }

  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const selectAll = (only: 'new' | 'all' | 'none') => setSelected(new Set(
    only === 'none' ? [] : entries.filter((e) => only === 'all' || (!e.in_library && !e.queued)).map((e) => e.video_id)))

  const chosen = entries.filter((e) => selected.has(e.video_id))
  const totalSec = chosen.reduce((n, e) => n + (e.duration_sec ?? 0), 0)
  const cost = meta?.priced && summaryType ? fmtMoney(totalSec * TOKENS_PER_SEC * 2 / 1e6 + chosen.length * 1200 * 3 / 1e6, meta.currency) : null

  const submit = async () => {
    if (chosen.length === 0) return
    setSubmitting(true); setErr(null)
    try {
      const r = await api.batch({
        items: chosen.map((e) => ({ url: e.url, title: e.title })),
        summary_type: summaryType || null, asr_model: asr || null, correct_terms: correctTerms,
      })
      setPages((ps) => ps.map((p) => ({ ...p, entries: p.entries.map((e) => selected.has(e.video_id) ? { ...e, queued: true } : e) })))
      setSelected(new Set())
      onSubmitted(r.queued)
    } catch (e) { setErr(e) } finally { setSubmitting(false) }
  }

  return (
    <div className="card listing">
      <div className="lh">
        <div>
          <h2>{listing.title}</h2>
          <div className="meta">
            {last.total != null && <span>共 {last.total} 条</span>}
            <span>已加载 {entries.length} 条</span>
            <span>库里有 {entries.filter((e) => e.in_library).length} 条</span>
          </div>
        </div>
        {canSearch && (
          <form className="search lsearch" onSubmit={(e) => { e.preventDefault(); void reload(keyword.trim()) }}>
            <Search />
            <input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="在这个 UP 主的投稿里搜…"
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); void reload(keyword.trim()) } }} />
            <button type="submit" className="btn ghost sm" disabled={loading}>{loading ? <span className="spin" /> : '搜'}</button>
          </form>
        )}
      </div>

      <ErrorBox error={err} />

      <div className="ltools">
        <button className="btn ghost sm" onClick={() => selectAll('new')}>选没处理的</button>
        <button className="btn ghost sm" onClick={() => selectAll('all')}>全选</button>
        <button className="btn ghost sm" onClick={() => selectAll('none')}>清空</button>
      </div>

      <div className="lrows">
        {entries.map((e) => {
          const off = e.queued
          return (
            <label className={`lrow${selected.has(e.video_id) ? ' on' : ''}${off ? ' off' : ''}`} key={e.video_id}>
              <input type="checkbox" checked={selected.has(e.video_id)} disabled={off} onChange={() => toggle(e.video_id)} />
              <span className="th">{e.thumbnail && <img src={e.thumbnail} alt="" referrerPolicy="no-referrer" loading="lazy" />}</span>
              <span className="ti">
                <span className="t" title={e.title}>{e.title}</span>
                <span className="s">
                  {e.duration_sec != null && <span className="mono">{fmtDuration(e.duration_sec)}</span>}
                  {e.upload_date && <span>{e.upload_date}</span>}
                </span>
              </span>
              <span className="badges">
                {e.queued && <Pill tone="accent" dot>排队中</Pill>}
                {e.in_library && !e.queued && <Pill tone="info">已处理{e.summaries_done.length ? ` · ${e.summaries_done.length} 份总结` : ''}</Pill>}
              </span>
            </label>
          )
        })}
        {entries.length === 0 && !loading && <div className="qa-empty">这一页没有视频。</div>}
      </div>

      {last.has_more && (
        <div className="lmore">
          <button className="btn sm" onClick={more} disabled={loading}>{loading ? <><span className="spin" /> 读取中</> : '再加载 30 条'}</button>
        </div>
      )}

      <div className="opts lfoot">
        <div className="opt">
          <label htmlFor="basr">识别模型</label>
          <select id="basr" className="sel" value={asr} onChange={(e) => setAsr(e.target.value)}>
            {meta?.asr_choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
        </div>
        <div className="opt">
          <label htmlFor="bst">处理完就总结</label>
          <select id="bst" className="sel" value={summaryType} onChange={(e) => setSummaryType(e.target.value)}>
            {meta?.summary_types.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
            <option value="">不总结</option>
          </select>
        </div>
        <label className="opt check" title="走语音识别的视频识别完让模型纠专有名词，叠在原文上、逐条可否决。1 小时约 3 分钱">
          <input type="checkbox" checked={correctTerms} onChange={(e) => setCorrectTerms(e.target.checked)} />
          纠专有名词
        </label>
        <div className="opt" style={{ marginLeft: 'auto' }}>
          <span style={{ color: 'var(--mute)', marginRight: 10 }}>
            已选 {chosen.length} 条 · {fmtMinutes(totalSec)} 分钟{cost ? ` · 预计 ${cost}` : ''}
          </span>
          <button className="btn primary" onClick={submit} disabled={chosen.length === 0 || submitting}>
            {submitting ? '排队中…' : `排队处理 ${chosen.length} 条`}
          </button>
        </div>
      </div>
    </div>
  )
}
