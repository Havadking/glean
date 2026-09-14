import { Link as LinkIcon } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, type Listing, type Probe } from '../api'
import { ListingPanel } from '../components/ListingPanel'
import { ErrorBox, Pill } from '../components/ui'
import { useJob, type JobView } from '../hooks/useJob'
import { fmtDuration, fmtMoney, fmtSeconds, fmtTokens } from '../lib/format'
import { useStore } from '../store'

const JOB_KEY = 'vsum.currentJob'

export function NewTask() {
  const { meta, refreshLibrary, queue, refreshQueue } = useStore()
  const nav = useNavigate()
  const [url, setUrl] = useState('')
  const [probe, setProbe] = useState<Probe | null>(null)
  const [listing, setListing] = useState<Listing | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [probing, setProbing] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [asr, setAsr] = useState<string>('')
  const [diarize, setDiarize] = useState<string>('auto')
  const [summaryType, setSummaryType] = useState<string>('')
  const [jobId, setJobId] = useState<string | null>(() => sessionStorage.getItem(JOB_KEY))
  const inputRef = useRef<HTMLInputElement>(null)
  const job = useJob(jobId)

  useEffect(() => {
    if (meta) {
      setAsr((a) => a || meta.asr_default)
      setSummaryType((t) => t || meta.default_summary_type)
      setDiarize(String(meta.diarize_default))
    }
  }, [meta])

  const jobActive = job != null && !['done', 'failed', 'cancelled'].includes(job.status)

  useEffect(() => {
    if (job?.status === 'done') void refreshLibrary()
  }, [job?.status, refreshLibrary])

  useEffect(() => { inputRef.current?.focus() }, [])

  const doProbe = async () => {
    const u = url.trim()
    if (!u) return
    setProbing(true); setError(null); setProbe(null); setListing(null); setNotice(null)
    try {
      const r = await api.probe(u)
      if (r.kind === 'list') setListing(r)
      else setProbe(r)
    } catch (e) { setError(e) } finally { setProbing(false) }
  }

  const start = async () => {
    if (!probe) return
    setError(null)
    try {
      const r = await api.createJob({
        url: probe.url, asr_model: asr || null, diarize: diarize === 'auto' ? 'auto' : diarize === 'true',
        summary_type: summaryType || null,
      })
      sessionStorage.setItem(JOB_KEY, r.job.id)
      setJobId(r.job.id)
    } catch (e) { setError(e) }
  }

  const clear = () => { setUrl(''); setProbe(null); setListing(null); setError(null); setNotice(null); inputRef.current?.focus() }
  const busy = jobActive
  const pending = queue.filter((j) => j.status === 'queued' || j.status === 'running')

  return (
    <div className="page">
      <div className="ph">
        <div>
          <h1>新任务</h1>
          <p>贴一个视频链接，或者合集 / UP 主空间的链接。字幕能拿到就不跑语音识别，处理过的直接读缓存。</p>
        </div>
      </div>

      <form className="card urlbox" onSubmit={(e) => { e.preventDefault(); void doProbe() }}>
        <LinkIcon />
        <input ref={inputRef} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="视频 / 合集 / UP 主空间链接，抖音分享口令直接整段贴"
          spellCheck={false} aria-label="视频链接" />
        {url && <button type="button" className="btn ghost" onClick={clear}>清空</button>}
        <button type="submit" className="btn primary" disabled={!url.trim() || probing}>
          {probing ? <><span className="spin" /> 探测中</> : '探测'}
        </button>
      </form>

      <ErrorBox error={error} />
      {notice && <div className="status"><Pill tone="ok" dot>{notice}</Pill><span>串行跑，相邻两个之间隔几秒；关掉页面也会继续，重启服务也能续上。</span></div>}

      {listing && (
        <ListingPanel listing={listing} url={url.trim()} onSubmitted={(n) => { setNotice(`已排队 ${n} 条`); void refreshQueue() }} />
      )}

      {probe && (
        <div className="card probe">
          <div className="cover">
            {probe.thumbnail && <img src={probe.thumbnail} alt="" referrerPolicy="no-referrer" />}
            <span className="dur mono">{fmtDuration(probe.duration_sec)}</span>
          </div>
          <div>
            <h2>{probe.title}</h2>
            <div className="meta">
              {probe.uploader && <span><b>{probe.uploader}</b></span>}
              <span>{probe.extractor}</span>
              <span className="mono">{probe.video_id}</span>
              {probe.upload_date && <span>{probe.upload_date} 发布</span>}
              {probe.subtitle
                ? <Pill tone="ok" dot>有{probe.subtitle.auto ? '自动' : ''}字幕 · {probe.subtitle.language}</Pill>
                : <Pill tone="warn" dot>没有字幕，走语音识别</Pill>}
              {probe.transcript_cached && <Pill tone="info">处理过 · 转写命中缓存</Pill>}
              {!probe.transcript_cached && probe.already_in_library && <Pill tone="info">库里有这条，设置不同会重跑</Pill>}
            </div>
            <div className="opts">
              <div className="opt">
                <label htmlFor="asr">识别模型</label>
                <select id="asr" className="sel" value={asr} onChange={(e) => setAsr(e.target.value)} disabled={!!probe.subtitle || probe.transcript_cached}>
                  {meta?.asr_choices.map((c) => <option key={c.value} value={c.value}>{c.label}（{c.note}）</option>)}
                </select>
              </div>
              <div className="opt">
                <label htmlFor="dia">说话人分离</label>
                <select id="dia" className="sel" value={diarize} onChange={(e) => setDiarize(e.target.value)} disabled={!!probe.subtitle || probe.transcript_cached}>
                  <option value="auto">自动</option><option value="true">开</option><option value="false">关</option>
                </select>
              </div>
              <div className="opt">
                <label htmlFor="st">处理完就总结</label>
                <select id="st" className="sel" value={summaryType} onChange={(e) => setSummaryType(e.target.value)}>
                  {meta?.summary_types.map((t) => (
                    <option key={t.key} value={t.key}>{t.label}{probe.summaries_done.includes(t.key) ? '（已有）' : ''}</option>
                  ))}
                  <option value="">不总结</option>
                </select>
              </div>
              <div className="opt" style={{ marginLeft: 'auto' }}>
                <EstimateLine probe={probe} summaryType={summaryType} />
                <button className="btn primary" onClick={start} disabled={busy}>开始</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {job && <JobPanel job={job} onOpen={(id) => nav(`/video/${encodeURIComponent(id)}`)} />}

      {pending.length > 0 && (
        <div className="queue">
          <h3>队列 <Pill tone="neutral">{pending.length}</Pill>
            <span style={{ flex: 1 }} />
            {pending.some((j) => j.status === 'queued') && (
              <button className="btn ghost sm" onClick={() => api.cancelQueued().then(refreshQueue)}>清空排队的</button>
            )}
          </h3>
          <div className="card">
            {pending.map((j) => (
              <div className="qrow" key={j.id}>
                <span className="t">{j.title}<small>{j.kind === 'summarize' ? '总结' : j.kind === 'audio' ? '音频' : '转写'}</small></span>
                {j.status === 'running'
                  ? <Pill tone="accent" dot>{j.stage_detail || '进行中'}</Pill>
                  : <Pill tone="neutral">排队</Pill>}
                {j.status === 'queued'
                  ? <button className="btn ghost sm" onClick={() => api.cancelJob(j.id).then(() => void refreshQueue())}>移除</button>
                  : <button className="btn ghost sm" onClick={() => { sessionStorage.setItem(JOB_KEY, j.id); setJobId(j.id) }}>查看</button>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function EstimateLine({ probe, summaryType }: { probe: Probe; summaryType: string }) {
  const e = probe.estimate
  const parts: string[] = []
  if (summaryType) {
    const m = fmtMoney(e.cost, e.currency)
    parts.push(m ? `预计 ${m}` : `约 ${fmtTokens(e.input_tokens)} tokens`)
  } else parts.push('不调模型，不花钱')
  if (e.asr_sec) parts.push(`识别约 ${fmtSeconds(e.asr_sec)}`)
  return <span style={{ color: 'var(--mute)', marginRight: 10 }}>{parts.join(' · ')}</span>
}

const STAGE_ORDER: { key: string; label: string; alt?: string[] }[] = [
  { key: 'probe', label: '探测' },
  { key: 'download', label: '下载音频', alt: ['subtitle'] },
  { key: 'transcribe', label: '语音识别' },
  { key: 'summarize', label: '总结' },
]

function JobPanel({ job, onOpen }: { job: JobView; onOpen: (videoId: string) => void }) {
  const finished = ['done', 'failed', 'cancelled'].includes(job.status)
  const videoId = job.result?.video_id as string | undefined
  const now = useNow(!finished)

  const cells = useMemo(() => {
    const idxOf = (name: string) => STAGE_ORDER.findIndex((s) => s.key === name || s.alt?.includes(name))
    // 走到了最远的哪一格：前面没记录的格子都算跳过（探测复用了、命中缓存不用下载）
    const reached = Math.max(-1, ...job.stages.map((r) => idxOf(r.name)))
    return STAGE_ORDER.map((s, idx) => {
      const recs = job.stages.filter((r) => r.name === s.key || s.alt?.includes(r.name))
      const rec = recs[recs.length - 1]
      const isCurrent = job.stages.length > 0 && job.stages[job.stages.length - 1] === rec
      let state: 'done' | 'run' | 'todo' | 'fail' = 'todo'
      let detail = rec?.detail ?? ''
      if (rec) state = isCurrent && !finished ? 'run' : 'done'
      if (rec && isCurrent && job.status === 'failed') state = 'fail'
      if (!rec && idx < reached) { state = 'done'; detail = s.key === 'probe' ? '用了刚才的探测结果' : '不需要' }
      if (rec?.name === 'subtitle') detail = '有字幕，不用识别'
      if (s.key === 'summarize' && !rec && finished) {
        detail = job.result?.summary_skipped ? String(job.result.summary_skipped) : '没选总结'
      }
      const secs = rec ? ((rec.endedAt ?? now) - rec.startedAt) / 1000 : null
      return { ...s, state, detail, secs, showBar: state === 'run' && s.key === 'transcribe' && job.progress != null }
    })
  }, [job, finished, now])

  return (
    <>
      <div className="card stages">
        {cells.map((c, i) => (
          <div className={`stage ${c.state}`} key={c.key}>
            <div className="t"><span className="i">{c.state === 'done' ? '✓' : c.state === 'fail' ? '!' : c.state === 'run' ? '' : i + 1}</span>{c.label}</div>
            <div className="d" title={c.detail}>
              {c.detail}{c.secs != null && c.state !== 'run' ? ` · ${c.secs.toFixed(1)} 秒` : ''}
              {c.state === 'run' && job.progress != null ? ` · ${Math.round(job.progress * 100)}%` : ''}
            </div>
            {c.showBar && <div className="bar"><i style={{ width: `${Math.round((job.progress ?? 0) * 100)}%` }} /></div>}
          </div>
        ))}
      </div>

      {job.status === 'failed' && <div className="errbox" style={{ marginTop: 12 }}>{job.error}</div>}
      {job.status === 'done' && videoId && (
        <div className="status" style={{ marginTop: 12 }}>
          <Pill tone="ok" dot>完成</Pill>
          <span>{job.title}</span>
          {job.elapsed != null && <span className="mono">{job.elapsed.toFixed(1)} 秒</span>}
          <span className="sp" />
          <button className="btn primary sm" onClick={() => onOpen(videoId)}>打开</button>
        </div>
      )}
      {job.status === 'queued' && (
        <div className="status" style={{ marginTop: 12 }}><Pill tone="neutral">排队中</Pill><span>前面还有任务在跑</span></div>
      )}

      <details className="logwrap">
        <summary>运行日志 <Pill tone="neutral">{job.logs.length} 行</Pill></summary>
        <LogBox lines={job.logs} />
      </details>
      {finished && <p style={{ marginTop: 14, fontSize: 12.5, color: 'var(--faint)' }}>处理过的视频都在 <Link to="/library" style={{ color: 'var(--accent-ink)' }}>库</Link> 里。</p>}
    </>
  )
}

function LogBox({ lines }: { lines: { ts: string; level: string; message: string }[] }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { const el = ref.current; if (el) el.scrollTop = el.scrollHeight }, [lines.length])
  return (
    <div className="log mono" ref={ref}>
      {lines.map((l, i) => (
        <div key={i} className={l.level}><span className="ts">{fmtClock(l.ts)}</span>  {l.message}</div>
      ))}
    </div>
  )
}

function fmtClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(11, 19)
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(t)
  }, [active])
  return now
}
