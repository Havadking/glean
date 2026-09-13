import { Copy, ExternalLink, FolderOpen, Search, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, type Estimate, type Video as VideoT } from '../api'
import { Markdown } from '../components/Markdown'
import { Mindmap } from '../components/Mindmap'
import { ErrorBox, Highlight, Pill, Seg, countHits } from '../components/ui'
import { useJob } from '../hooks/useJob'
import { fmtDuration, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { useStore } from '../store'

export function Video() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const { meta, refreshLibrary } = useStore()
  const [video, setVideo] = useState<VideoT | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [query, setQuery] = useState(() => params.get('q') ?? '')
  const [type, setType] = useState<string>(() => params.get('type') ?? '')
  const [copied, setCopied] = useState(false)
  const [focusStart, setFocusStart] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      const v = await api.video(id)
      setVideo(v)
      setError(null)
      setType((t) => t || Object.keys(v.summaries)[0] || meta?.default_summary_type || 'overall')
    } catch (e) { setError(e) }
  }, [id, meta?.default_summary_type])

  // 换视频或从搜索结果跳进来（t / q / type 变化）时重新读
  const paramKey = params.toString()
  useEffect(() => {
    setVideo(null)
    setType(params.get('type') ?? '')
    setQuery(params.get('q') ?? '')
    const t = params.get('t')
    setFocusStart(t != null && t !== '' ? Number(t) : null)
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, paramKey])

  // 从搜索结果跳进来：滚到那一段并高亮一下
  useEffect(() => {
    if (!video || focusStart == null) return
    const el = document.getElementById(`p-${Math.floor(focusStart)}`)
    if (!el) return
    el.scrollIntoView({ block: 'center' })
    el.classList.add('focus')
    const t = setTimeout(() => el.classList.remove('focus'), 2700)
    return () => clearTimeout(t)
  }, [video, focusStart])

  const hits = useMemo(() => video ? video.paragraphs.reduce((n, p) => n + countHits(p.text, query), 0) : 0, [video, query])

  if (error) return <div className="page"><ErrorBox error={error} /><p><Link to="/library">回到库</Link></p></div>
  if (!video) return <div className="page" style={{ color: 'var(--mute)' }}><span className="spin" /> 读取中…</div>

  const types = meta?.summary_types ?? []
  const summary = video.summaries[type]

  const openFolder = () => api.openFolder(video.video_id).catch((e) => setError(e))
  const remove = async () => {
    if (!confirm(`删除「${video.title}」的转写、总结和产物目录？不可恢复。`)) return
    try {
      await api.deleteVideo(video.video_id)
      await refreshLibrary()
      nav('/library')
    } catch (e) { setError(e) }
  }
  const copyText = async (text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* ignore */ }
  }

  return (
    <>
      <div className="dhead">
        <div className="in">
          <div className="crumb">
            <Link to="/library">库</Link>
            {video.uploader && <><span>›</span><Link to={`/library?up=${encodeURIComponent(video.uploader)}`}>{video.uploader}</Link></>}
          </div>
          <h1>{video.title}</h1>
          <div className="row">
            <div className="meta">
              {video.uploader && <span><b>{video.uploader}</b></span>}
              <span className="mono">{fmtDuration(video.duration_sec)}</span>
              {video.extractor && <span>{video.extractor}</span>}
              <span>{video.source_label}{video.diarized ? ' · 分说话人' : ''}</span>
              {video.language && <span>{video.language}</span>}
              {typeof video.meta.elapsed_sec === 'number' && <span>识别用时 {video.meta.elapsed_sec} 秒</span>}
              {video.upload_date && <span>{video.upload_date} 发布</span>}
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              {video.source_url && <a className="btn" href={video.source_url} target="_blank" rel="noreferrer"><ExternalLink /> 原视频</a>}
              {video.work_dir && <button className="btn" onClick={openFolder}><FolderOpen /> 打开目录</button>}
              <button className="btn ghost danger" onClick={remove} title="删除"><Trash2 /></button>
            </div>
          </div>
        </div>
      </div>

      <div className="split">
        <div className="col left">
          <div className="colhead">
            <h2>转写 <span style={{ color: 'var(--faint)', fontWeight: 400 }}>{video.paragraphs.length} 段 · {video.segment_count} 句</span></h2>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <label className="search"><Search /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="在这条转写里找…" /></label>
              {query.trim() && <Pill tone={hits ? 'neutral' : 'warn'}>{hits} 处</Pill>}
              <button className="btn ghost sm" title="复制全文" onClick={() => copyText(video.paragraphs.map((p) => `[${fmtDuration(p.start)}] ${p.text}`).join('\n\n'))}><Copy /> {copied ? '已复制' : '复制'}</button>
            </div>
          </div>
          {video.paragraphs.length === 0 && <div className="empty"><b>这条视频没有转写文本</b>缓存和产物目录里都没找到。</div>}
          {video.paragraphs.map((p, i) => (
            <div className={`para${query.trim() && countHits(p.text, query) ? ' hit' : ''}`} key={i} id={`p-${Math.floor(p.start)}`}>
              <span className="tc">{fmtDuration(p.start)}</span>
              <p>
                {p.speaker && video.speaker_count > 1 && <span className="sp">{p.speaker}</span>}
                <Highlight text={p.text} query={query} />
              </p>
            </div>
          ))}
        </div>

        <div className="col right">
          <div className="colhead">
            <h2>总结</h2>
            <Seg value={type} onChange={setType}
              items={types.map((t) => ({ key: t.key, label: t.label, showDot: true, done: !!video.summaries[t.key] }))} />
          </div>
          {summary
            ? <SummaryView video={video} type={type} onRegenerated={load} />
            : <GeneratePanel video={video} type={type} hint={types.find((t) => t.key === type)?.hint ?? ''} onDone={load} />}
        </div>
      </div>
    </>
  )
}

function SummaryView({ video, type, onRegenerated }: { video: VideoT; type: string; onRegenerated: () => Promise<void> }) {
  const s = video.summaries[type]
  const [jobId, setJobId] = useState<string | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [copied, setCopied] = useState(false)
  const job = useJob(jobId)
  const running = job != null && !['done', 'failed', 'cancelled'].includes(job.status)

  useEffect(() => {
    if (job?.status === 'done') { setJobId(null); void onRegenerated() }
  }, [job?.status, onRegenerated])

  const regenerate = async () => {
    if (!confirm('重新调用模型生成一份，会花钱。继续？')) return
    setErr(null)
    try {
      const r = await api.summarize(video.video_id, { type, force: true })
      if (r.job) setJobId(r.job.id)
    } catch (e) { setErr(e) }
  }
  const copy = async () => {
    try { await navigator.clipboard.writeText(s.content); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* ignore */ }
  }

  return (
    <>
      <div className="status">
        <Pill tone="ok" dot>已生成</Pill>
        <span title={s.provider}>{s.provider}</span>
        {s.type === 'mindmap' && s.tree && <span>{countNodes(s.tree)} 个节点</span>}
        <span>{fmtWhen(s.created_at)}</span>
        <span className="sp" />
        {running
          ? <span><span className="spin" /> 重新生成中…</span>
          : <button className="btn ghost sm" onClick={regenerate}>重新生成</button>}
      </div>
      {job?.status === 'failed' && <div className="errbox" style={{ marginBottom: 12 }}>{job.error}</div>}
      <ErrorBox error={err} />
      {s.type === 'mindmap' && s.tree
        ? <Mindmap tree={s.tree} title={video.title} />
        : <article className="sum">
            <Markdown text={s.content} />
            <div className="prov">
              <span>只依据转写生成，未使用外部知识</span>
              <span className="sp" />
              <button className="btn ghost sm" onClick={copy}>{copied ? '已复制' : '复制 Markdown'}</button>
            </div>
          </article>}
      {s.type === 'mindmap' && (
        <details className="logwrap"><summary>大纲源文件</summary>
          <article className="sum" style={{ marginTop: 8 }}><Markdown text={s.content} /></article>
        </details>
      )}
    </>
  )
}

function GeneratePanel({ video, type, hint, onDone }: { video: VideoT; type: string; hint: string; onDone: () => Promise<void> }) {
  const { meta } = useStore()
  const [est, setEst] = useState<Estimate | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const job = useJob(jobId)
  const running = job != null && !['done', 'failed', 'cancelled'].includes(job.status)
  const label = meta?.summary_types.find((t) => t.key === type)?.label ?? type

  useEffect(() => {
    setEst(null); setErr(null)
    api.estimate(video.video_id, type).then(setEst).catch(setErr)
  }, [video.video_id, type])

  useEffect(() => {
    if (job?.status === 'done') { setJobId(null); void onDone() }
  }, [job?.status, onDone])

  const generate = async () => {
    setErr(null)
    try {
      const r = await api.summarize(video.video_id, { type })
      if (r.cached) { await onDone(); return }
      if (r.job) setJobId(r.job.id)
    } catch (e) { setErr(e) }
  }

  const money = est ? fmtMoney(est.cost, est.currency) : null
  return (
    <>
      <div className="status">
        <Pill tone="neutral">还没生成</Pill>
        <span>{hint}</span>
        <span className="sp" />
      </div>
      {job?.status === 'failed' && <div className="errbox" style={{ marginBottom: 12 }}>{job.error}</div>}
      <ErrorBox error={err} />
      <div className="empty">
        <b>这条视频还没有{label}</b>
        {est
          ? <>转写 {fmtTokens(est.transcript_tokens)} tokens，{est.calls === 1 ? '单次调用' : `${est.calls} 次调用`}，预计 <span className="mono">{money ?? `${fmtTokens(est.input_tokens)} tokens`}</span></>
          : <span className="spin" />}
        <div>
          <button className="btn primary" onClick={generate} disabled={running || !est}>
            {running ? <><span className="spin" /> {job?.status === 'queued' ? '排队中' : '生成中…'}</> : '生成'}
          </button>
        </div>
        {video.speaker_count === 0 && type === 'by_speaker' && (
          <p style={{ marginTop: 14, fontSize: 12.5, color: 'var(--warn)' }}>这份转写没有说话人标签，模型只能靠语气和称呼推断角色。</p>
        )}
      </div>
    </>
  )
}

function countNodes(n: { children: { children: unknown[] }[] }): number {
  let c = 1
  for (const ch of n.children) c += countNodes(ch as never)
  return c
}
