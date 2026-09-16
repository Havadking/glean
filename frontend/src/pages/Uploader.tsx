import { ChevronDown, Folder, FolderPlus, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, displayTitle, type Question, type UploaderInfo } from '../api'
import { GroupMenu, useUploaderGroups } from '../components/GroupMenu'
import { Avatar, ErrorBox, Pill } from '../components/ui'
import { citeHtml } from '../lib/cite'
import { fmtDuration, fmtMinutes, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'

export function Uploader() {
  const { name = '' } = useParams()
  const { groups: allGroups, setGroup } = useUploaderGroups()
  const [groupMenu, setGroupMenu] = useState<HTMLElement | null>(null)
  const [data, setData] = useState<UploaderInfo | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [q, setQ] = useState('')
  const [asking, setAsking] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => api.uploader(name).then(setData).catch(setErr), [name])
  useEffect(() => { setData(null); setErr(null); void load() }, [load])

  const changeGroup = async (g: string | null) => {
    try { await setGroup(name, g); setData((d) => (d ? { ...d, group: g?.trim() || null } : d)) } catch (e) { setErr(e) }
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
          <h1 style={{ display: 'flex', alignItems: 'center', gap: 10 }}><Avatar name={name} size={28} /> {name}</h1>
          <div className="row">
            <div className="meta">
              <span>{data.videos.length} 条视频</span>
              <span>{fmtMinutes(totalSec)} 分钟</span>
              <button
                type="button"
                className={`gtrig${data.group ? '' : ' none'}${groupMenu ? ' open' : ''}`}
                title={data.group ? '换个分组' : '把这位 UP 主放进一个分组，侧栏和库里就按组归堆'}
                onClick={(e) => { const el = e.currentTarget; setGroupMenu((m) => (m ? null : el)) }}
              >
                {data.group ? <><Folder /> {data.group}</> : <><FolderPlus /> 加入分组</>}
                <ChevronDown className="dd" />
              </button>
              {groupMenu && (
                <GroupMenu anchor={groupMenu} current={data.group} groups={allGroups} onPick={changeGroup} onClose={() => setGroupMenu(null)} />
              )}
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
                  <div className="t" title={v.remark ? `${v.remark} (原名: ${v.title})` : v.title}>{displayTitle(v)}</div>
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
