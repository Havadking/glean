import { ChevronLeft, ChevronRight, RefreshCw, Sparkles } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, displayTitle, type Review as ReviewT, type ReviewPeriod } from '../api'
import { TagChip } from '../components/Tags'
import { Avatar, ErrorBox, Pill, Seg } from '../components/ui'
import { citeHtml } from '../lib/cite'
import { fmtDuration, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { useStore } from '../store'

function readPeriod(): ReviewPeriod {
  try { return localStorage.getItem('vsum.reviewPeriod') === 'day' ? 'day' : 'week' } catch { return 'week' }
}

/**
 * 回顾页。按需生成：打开时这段时间有视频但还没有回顾，就自动生成一次；
 * 之后有新视频进来只提示「有 N 条新的」，点了才更新 —— 别每次打开都花钱。
 */
export function Review() {
  const { refreshLibrary } = useStore()
  const [params, setParams] = useSearchParams()
  const [period, setPeriodState] = useState<ReviewPeriod>(() => (params.get('period') as ReviewPeriod) || readPeriod())
  const key = params.get('key') ?? ''
  const [data, setData] = useState<ReviewT | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [generating, setGenerating] = useState(false)
  // 这次会话里已经自动生成过哪些周期，别在同一段时间反复自动生成
  const autoDone = useRef(new Set<string>())

  const setPeriod = (p: ReviewPeriod) => {
    setPeriodState(p)
    try { localStorage.setItem('vsum.reviewPeriod', p) } catch { /* ignore */ }
    setParams({ period: p })
  }
  const go = (k: string) => setParams({ period, key: k })

  const load = useCallback(async () => {
    try { setData(await api.review(period, key)); setErr(null) } catch (e) { setErr(e) }
  }, [period, key])
  useEffect(() => { setData(null); void load() }, [load])

  const generate = useCallback(async (force = false) => {
    if (!data) return
    setGenerating(true); setErr(null)
    try {
      const r = await api.generateReview(data.period, data.key, force)
      setData((d) => d ? { ...d, digest: r.digest, periods: d.periods.map((p) => p.key === d.key ? { ...p, generated: true } : p) } : d)
      void refreshLibrary()
    } catch (e) { setErr(e) } finally { setGenerating(false) }
  }, [data, refreshLibrary])

  // 自动生成：有材料、还没有回顾、这次会话没试过
  useEffect(() => {
    if (!data || data.digest || generating) return
    const usable = data.videos.length - data.no_material
    const k = `${data.period}:${data.key}`
    if (usable === 0 || autoDone.current.has(k)) return
    autoDone.current.add(k)
    void generate()
  }, [data, generating, generate])

  const byIndex = useMemo(() => new Map((data?.videos ?? []).map((v) => [v.index, v])), [data])

  if (err && !data) return <div className="page"><ErrorBox error={err} /><p><Link to="/library">回到库</Link></p></div>
  if (!data) return <div className="page" style={{ color: 'var(--mute)' }}><span className="spin" /> 读取中…</div>

  const idx = data.periods.findIndex((p) => p.key === data.key)
  const newer = idx > 0 ? data.periods[idx - 1] : null
  const older = idx >= 0 && idx < data.periods.length - 1 ? data.periods[idx + 1] : null
  const isCurrent = data.key === data.current
  const money = fmtMoney(data.estimate.cost, data.estimate.currency)
  const usable = data.videos.length - data.no_material
  const tagCounts = new Map<string, number>()
  for (const v of data.videos) for (const t of v.tags) tagCounts.set(t, (tagCounts.get(t) ?? 0) + 1)
  const topTags = [...tagCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)

  return (
    <div className="detail">
      <div className="dhead">
        <div className="in">
          <div className="crumb"><span>回顾</span><span>›</span><span>{period === 'week' ? '按周' : '按天'}</span></div>
          <div className="rvnav">
            <button className="iconbtn" onClick={() => older && go(older.key)} disabled={!older} title={older ? `${older.label} · ${older.videos} 条` : '再往前没有了'}><ChevronLeft /></button>
            <h1>{isCurrent ? (period === 'week' ? '本周' : '今天') : data.label}{isCurrent && <span className="sub">{data.label}</span>}</h1>
            <button className="iconbtn" onClick={() => newer && go(newer.key)} disabled={!newer} title={newer ? `${newer.label} · ${newer.videos} 条` : '已经是最近的了'}><ChevronRight /></button>
            {!isCurrent && <button className="btn ghost sm" onClick={() => setParams({ period })}>回到{period === 'week' ? '本周' : '今天'}</button>}
            <span className="sp" />
            <Seg value={period} onChange={setPeriod} items={[{ key: 'week', label: '周' }, { key: 'day', label: '日' }]} />
          </div>
          <div className="row">
            <div className="meta">
              <span>{data.videos.length} 条视频</span>
              {data.no_material > 0 && <Pill tone="warn">{data.no_material} 条没有转写也没有总结</Pill>}
              {topTags.length > 0 && <span className="tags sm">{topTags.map(([t, n]) => <TagChip key={t} tag={t} count={n} to={`/library?tag=${encodeURIComponent(t)}`} />)}</span>}
            </div>
          </div>
        </div>
      </div>

      <div className="split">
        <div className="col left" style={{ width: '42%' }}>
          <div className="colhead"><h2>收藏的 <span style={{ color: 'var(--faint)', fontWeight: 400 }}>按处理时间</span></h2></div>
          {data.videos.length === 0 && (
            <div className="empty"><b>这段时间没收藏东西</b>{isCurrent ? '去「新任务」贴一个链接。' : '换一段时间看看。'}</div>
          )}
          {data.videos.length > 0 && (
            <div className="list">
              {data.videos.map((v) => (
                <Link className="item" to={`/video/${encodeURIComponent(v.video_id)}`} key={v.video_id} style={{ gridTemplateColumns: '28px 64px 1fr' }}>
                  <span className="mono" style={{ color: 'var(--faint)', fontSize: 12 }}>{v.index}</span>
                  <div className="th">{v.thumbnail && <img src={v.thumbnail} alt="" referrerPolicy="no-referrer" loading="lazy" />}</div>
                  <div className="ti">
                    <div className="t" title={v.remark ? `${v.remark} (原名: ${v.title})` : v.title}>{displayTitle(v)}</div>
                    <div className="s">
                      {v.uploader && <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}><Avatar name={v.uploader} size={14} />{v.uploader}</span>}
                      <span className="mono">{fmtDuration(v.duration_sec)}</span>
                      <span>{fmtWhen(v.created_at)}</span>
                      {v.material === 'none' && <Pill tone="warn">没材料</Pill>}
                      {v.material === 'transcript' && <Pill tone="neutral">只有转写</Pill>}
                    </div>
                    {v.tags.length > 0 && <div className="tags sm">{v.tags.map((t) => <TagChip key={t} tag={t} to={`/library?tag=${encodeURIComponent(t)}`} />)}</div>}
                  </div>
                </Link>
              ))}
            </div>
          )}
          <p style={{ fontSize: 12.5, color: 'var(--faint)', marginTop: 12 }}>
            回顾用的是每条视频的总结（优先「总体」），没总结的用转写开头。生成一次约 {money ?? `${fmtTokens(data.estimate.input_tokens)} tokens`}。
          </p>
        </div>

        <div className="col right">
          <div className="colhead">
            <h2>AI 回顾</h2>
            {data.digest && !generating && (
              <button className="btn ghost sm" onClick={() => generate(true)} title="不管有没有新视频，重新写一份（会花钱）"><RefreshCw /> 重新生成</button>
            )}
          </div>
          <ErrorBox error={err} />
          {generating && (
            <div className="status"><span className="spin" /> 在读这 {usable} 条视频的总结，写回顾…</div>
          )}
          {data.digest?.stale && !generating && (
            <div className="status">
              <Pill tone="warn" dot>有变化</Pill>
              <span>{data.digest.new_count > 0 ? `这段时间又收藏了 ${data.digest.new_count} 条，回顾还是旧的` : '有视频被删了，回顾还是旧的'}</span>
              <span className="sp" />
              <button className="btn primary sm" onClick={() => generate(false)}><Sparkles /> 更新回顾 · {money ?? `${fmtTokens(data.estimate.input_tokens)} tokens`}</button>
            </div>
          )}
          {data.digest
            ? (
              <article className="sum">
                <div dangerouslySetInnerHTML={{ __html: citeHtml(data.digest.content, byIndex, true) }} />
                <div className="prov">
                  <span>只依据这 {data.digest.video_ids.length} 条视频的总结，未使用外部知识</span>
                  <span>{data.digest.provider}</span>
                  <span>{fmtWhen(data.digest.created_at)}</span>
                </div>
              </article>
            )
            : !generating && (
              <div className="empty">
                <b>{usable === 0 ? '没有可用的材料' : '还没有这段时间的回顾'}</b>
                {usable === 0
                  ? (data.videos.length === 0 ? '这段时间没收藏视频。' : '这些视频既没有转写也没有总结。')
                  : <>{usable} 条视频的总结进上下文，预计 <span className="mono">{money ?? `${fmtTokens(data.estimate.input_tokens)} tokens`}</span></>}
                {usable > 0 && <div><button className="btn primary" onClick={() => generate(false)}><Sparkles /> 生成回顾</button></div>}
              </div>
            )}
        </div>
      </div>
    </div>
  )
}
