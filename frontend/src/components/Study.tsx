import DOMPurify from 'dompurify'
import { ArrowLeft, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Film, ScrollText, MapPin, RefreshCw, Send, Trash2 } from 'lucide-react'
import { marked } from 'marked'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api, displayTitle, type StudyChapter, type StudyQuestion, type StudySheet, type StudySource, type StudyState,
  type StudyTerm, type Video,
} from '../api'
import { useJob } from '../hooks/useJob'
import { fmtDuration, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { paragraphAt } from './AskPanel'
import { ErrorBox, Pill } from './ui'

/**
 * 伴读页（DESIGN.md v0.9）：独立的全屏页面 /study/:id，不套侧栏和详情页的头。
 * 布局跟着视频走：竖屏录屏三栏（视频 | 转写 | AI），横屏视频在上、转写在下，右边是学习底稿 / 定位提问。
 */

const QUICK = ['这里在说什么？', '他为什么这么说？', '这和前面讲的矛盾吗？', '能举个例子吗？']

function chapterAt(chapters: StudyChapter[], sec: number): number {
  let idx = -1
  chapters.forEach((c, i) => { if (sec >= c.start) idx = i })
  return chapters.length ? Math.max(0, idx) : -1
}

function readPref(key: string, fallback: boolean): boolean {
  try { const v = localStorage.getItem(key); return v == null ? fallback : v === '1' } catch { return fallback }
}
function writePref(key: string, v: boolean) {
  try { localStorage.setItem(key, v ? '1' : '0') } catch { /* ignore */ }
}

export function Study({ video }: { video: Video }) {
  const id = video.video_id
  const [state, setState] = useState<StudyState | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [tab, setTab] = useState<'sheet' | 'ask'>('sheet')
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(video.duration_sec || 0)
  const [mediaErr, setMediaErr] = useState<string | null>(null)
  // 竖屏录屏（手机直播之类）：视频单独占一栏，不然上下全是黑边
  const [portrait, setPortrait] = useState(false)
  const [aspect, setAspect] = useState(16 / 9)
  const [autoScroll, setAutoScroll] = useState(() => readPref('vsum.study.autoscroll', true))
  const [markTerms, setMarkTerms] = useState(() => readPref('vsum.study.terms', true))
  // 转写默认收起：大部分时间该看的是视频、本章论证线和提问，细读文字时再展开
  const [txOpen, setTxOpen] = useState(() => readPref('vsum.study.tx', false))
  const toggleTx = useCallback(() => setTxOpen((v) => { writePref('vsum.study.tx', !v); return !v }), [])
  const mediaRef = useRef<HTMLMediaElement | null>(null)
  const askRef = useRef<HTMLTextAreaElement>(null)
  const txRef = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    try { setState(await api.study(id)); setErr(null) } catch (e) { setErr(e) }
  }, [id])
  useEffect(() => { setState(null); setTime(0); setMediaErr(null); void load() }, [load])

  // 没有底稿时默认看底稿页（去生成），有了以后也先给底稿：先看前置知识再开始
  const sheet = state?.sheet ?? null
  const chapters = useMemo(() => sheet?.chapters ?? [], [sheet])
  const curCh = chapterAt(chapters, time)

  const seek = useCallback((sec: number, play = true) => {
    const m = mediaRef.current
    setTime(sec)
    if (!m) return
    m.currentTime = sec
    if (play) void m.play().catch(() => { /* 自动播放被拦就算了 */ })
  }, [])

  const focusAsk = useCallback(() => {
    mediaRef.current?.pause()
    setTab('ask')
    requestAnimationFrame(() => askRef.current?.focus())
  }, [])

  // 快捷键：空格 播放/暂停，←/→ 5 秒，Q 在当前位置提问
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const m = mediaRef.current
      // 焦点在播放器上时，空格和方向键交给浏览器自己处理，别切两次
      const onPlayer = t?.tagName === 'VIDEO' || t?.tagName === 'AUDIO'
      if (e.code === 'Space' && m && !onPlayer && t?.tagName !== 'BUTTON') {
        e.preventDefault()
        if (m.paused) void m.play().catch(() => {}); else m.pause()
      } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && m && !onPlayer) {
        e.preventDefault()
        m.currentTime = Math.max(0, m.currentTime + (e.key === 'ArrowLeft' ? -5 : 5))
      } else if (e.key === 'q' || e.key === 'Q') {
        e.preventDefault()
        focusAsk()
      } else if (e.key === 't' || e.key === 'T') {
        e.preventDefault()
        toggleTx()
      }
    }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [focusAsk, toggleTx])

  const onTimeUpdate = () => {
    const m = mediaRef.current
    if (!m) return
    const sec = Math.floor(m.currentTime)
    setTime((cur) => (cur === sec ? cur : sec))
  }

  // 转写：当前段落、自动滚动
  const starts = useMemo(() => video.paragraphs.map((p) => p.start), [video.paragraphs])
  const playingStart = video.paragraphs.length ? paragraphAt(starts, time) : null
  useEffect(() => {
    if (!autoScroll || playingStart == null || !txRef.current) return
    const el = txRef.current.querySelector<HTMLElement>(`[data-ps="${Math.floor(playingStart)}"]`)
    if (!el) return
    const box = txRef.current
    // .st-tx 是 position: relative，offsetTop 已经是相对它的；留出吸顶的表头
    const top = el.offsetTop - 60
    if (Math.abs(box.scrollTop - top) > 40) box.scrollTo({ top, behavior: 'smooth' })
  }, [playingStart, autoScroll, txOpen])

  // 术语在转写里标出来
  const termRe = useMemo(() => {
    const words = (sheet?.glossary ?? []).map((t) => t.term).filter((w) => w.length >= 2)
    if (!words.length) return null
    words.sort((a, b) => b.length - a.length)
    return new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi')
  }, [sheet])
  const termByLower = useMemo(() => new Map((sheet?.glossary ?? []).map((t) => [t.term.toLowerCase(), t])), [sheet])
  const [pop, setPop] = useState<{ term: StudyTerm; x: number; y: number } | null>(null)
  const popTimer = useRef<number | undefined>(undefined)
  const showPop = (e: React.MouseEvent<HTMLElement>, word: string) => {
    const term = termByLower.get(word.toLowerCase())
    if (!term) return
    window.clearTimeout(popTimer.current)
    const r = e.currentTarget.getBoundingClientRect()
    setPop({ term, x: Math.min(r.left, window.innerWidth - 330), y: r.bottom + 6 })
  }
  const hidePop = () => { popTimer.current = window.setTimeout(() => setPop(null), 180) }

  const renderText = (text: string) => {
    if (!markTerms || !termRe) return text
    return text.split(termRe).map((part, i) =>
      i % 2 === 1
        ? <span key={i} className="st-term" onMouseEnter={(e) => showPop(e, part)} onMouseLeave={hidePop}>{part}</span>
        : <Fragment key={i}>{part}</Fragment>)
  }

  const asked = state?.questions ?? []

  const ch = curCh >= 0 ? chapters[curCh] : undefined
  // port：竖屏录屏，视频单独一栏；land：横屏，视频按比例定宽，右侧放本章论证线；flat：没有画面（音频 / 待下载）
  const layout = portrait ? 'port' : state?.media.video && !mediaErr ? 'land' : 'flat'
  const chPct = ch ? Math.min(100, Math.max(0, (time - ch.start) / Math.max(1, ch.end - ch.start) * 100)) : 0
  // 没有画面时转写就是主角，不收
  const focus = !txOpen && layout !== 'flat'

  // 收起时留一行「现在这句」：段落里按播放进度估一句，点一下展开转写
  const para = playingStart != null ? video.paragraphs.find((p) => p.start === playingStart) : undefined
  let sentence = ''
  if (para) {
    const parts = para.text.split(/(?<=[。！？!?])/).filter((x) => x.trim())
    const k = Math.min(parts.length - 1, Math.max(0, Math.floor((time - para.start) / Math.max(1, para.end - para.start) * parts.length)))
    sentence = parts[k] ?? para.text
  }
  const peek = (
    <button className="sp-peek" onClick={toggleTx} title="展开转写（T）">
      <span className="tc">{fmtDuration(para?.start ?? time)}</span>
      <span className="txt">{sentence || '（这里没有转写）'}</span>
      <span className="act"><ChevronUp size={14} /> 展开转写</span>
    </button>
  )

  return (
    <div className="sp-page">
      <header className="sp-top">
        <Link className="iconbtn" to={`/video/${encodeURIComponent(id)}`} title="回到视频详情（速读）" aria-label="返回"><ArrowLeft /></Link>
        <div className="tt">
          <b title={video.title}>{displayTitle(video)}</b>
          <span>{video.uploader ?? ''}{video.uploader ? ' · ' : ''}{fmtDuration(duration || video.duration_sec)}</span>
        </div>
        <span className="sp" />
        <span className="keys"><kbd>空格</kbd> 播放 <kbd>←</kbd><kbd>→</kbd> 5 秒 <kbd>Q</kbd> 提问 <kbd>T</kbd> 转写</span>
        {layout !== 'flat' && (
          <button className={`btn sm${txOpen ? ' primary' : ''}`} onClick={toggleTx} title={txOpen ? '收起转写，专注看视频（T）' : '展开转写，细读原文（T）'}>
            <ScrollText /> {txOpen ? '收起转写' : '展开转写'}
          </button>
        )}
      </header>

      <div className={`sp-main ${layout}${focus ? ' focus' : ''}`} style={{ '--ar': aspect } as React.CSSProperties}>
        <section className="sp-media">
          <div className="sp-mediacol">
            <Player video={video} state={state} mediaRef={mediaRef} onTimeUpdate={onTimeUpdate}
              onMeta={(d, ar) => { setDuration(d); if (ar) { setAspect(ar); setPortrait(ar < 1) } }} mediaErr={mediaErr} setMediaErr={setMediaErr} reload={load} />
            <ChapterStrip chapters={chapters} duration={duration || video.duration_sec} time={time}
              asked={asked.map((q) => q.position).filter((p): p is number => p != null)} onSeek={(s) => seek(s)} />
            {focus && layout === 'land' && peek}
          </div>
          {/* 横屏：转写展开时卡片在视频右边，收起时在视频下面 */}
          {layout === 'land' && ch && <ChapterCard chapters={chapters} idx={curCh} pct={chPct} time={time} onSeek={seek} />}
        </section>

        <section className="sp-read">
          {focus ? (
            layout === 'port' && <>
              {peek}
              {ch && <ChapterCard chapters={chapters} idx={curCh} pct={chPct} time={time} onSeek={seek} upcoming />}
            </>
          ) : <>
          {ch && layout !== 'land' && (
            <div className="sp-chap" title={ch.claim}>
              <span className="no">第 {curCh + 1}/{chapters.length} 章</span>
              <div className="body">
                <div className="t">{ch.title}<span className="tm">{fmtDuration(ch.start)}–{fmtDuration(ch.end)}</span></div>
                {ch.claim && <div className="claim">{ch.claim}</div>}
              </div>
              <button className="iconbtn" title="上一章" aria-label="上一章"
                onClick={() => seek(chapters[Math.max(0, time - ch.start > 3 ? curCh : curCh - 1)].start)}><ChevronLeft /></button>
              <button className="iconbtn" title="下一章" aria-label="下一章" disabled={curCh >= chapters.length - 1}
                onClick={() => chapters[curCh + 1] && seek(chapters[curCh + 1].start)}><ChevronRight /></button>
              <i className="pg" style={{ width: `${chPct}%` }} />
            </div>
          )}
          <div className="st-tx" ref={txRef}>
          <div className="hd">
            转写 · 跟随播放
            <span className="sp" />
            {termRe && <label><input type="checkbox" checked={markTerms} onChange={(e) => { setMarkTerms(e.target.checked); writePref('vsum.study.terms', e.target.checked) }} /> 标出术语</label>}
            <label><input type="checkbox" checked={autoScroll} onChange={(e) => { setAutoScroll(e.target.checked); writePref('vsum.study.autoscroll', e.target.checked) }} /> 自动滚动</label>
            {layout !== 'flat' && <button className="lnk" onClick={toggleTx} title="收起转写，专注看视频（T）"><ChevronDown size={13} /> 收起</button>}
          </div>
          {video.paragraphs.length === 0 && <div className="empty"><b>这条视频没有转写文本</b></div>}
          {video.paragraphs.map((p, i) => {
            const chIdx = chapters.findIndex((c) => c.start >= p.start && c.start < p.end + 0.01 && (i === 0 || c.start > video.paragraphs[i - 1].start))
            const playing = playingStart === p.start
            return (
              <Fragment key={i}>
                {chIdx >= 0 && <div className="st-chapline"><b>第 {chIdx + 1} 章</b>{chapters[chIdx].title}</div>}
                <div className={`st-para${playing ? ' playing' : ''}`} data-ps={Math.floor(p.start)}>
                  <button className="tc" onClick={() => seek(p.start)} title="从这里开始看">{fmtDuration(p.start)}</button>
                  <p>{renderText(p.text)}</p>
                </div>
              </Fragment>
            )
          })}
          </div>
          </>}
        </section>

      <aside className="st-right">
        <div className="st-tabs" role="tablist">
          <button role="tab" aria-selected={tab === 'sheet'} onClick={() => setTab('sheet')}>学习底稿</button>
          <button role="tab" aria-selected={tab === 'ask'} onClick={() => setTab('ask')}>问 {asked.length > 0 && <span className="n">{asked.length}</span>}</button>
        </div>
        <ErrorBox error={err} />
        {!state && !err && <div className="st-pane"><span className="spin" /> 读取中…</div>}
        {state && tab === 'sheet' && <SheetPane id={id} state={state} time={time} onSeek={seek} reload={load} setState={setState}
          expandCurrent={!(layout === 'land' || (focus && layout === 'port'))} />}
        {state && tab === 'ask' && <AskPane state={state} onSeek={seek} reload={load} setState={setState} />}
        {state && (
          <AskBox id={id} state={state} time={time} chapter={curCh >= 0 ? curCh : null} chapterTitle={chapters[curCh]?.title}
            inputRef={askRef} onAsked={(q) => { setState((s) => s ? { ...s, questions: [...s.questions, q] } : s); setTab('ask') }}
            onAsking={() => setTab('ask')} />
        )}
      </aside>

      {pop && (
        <div className="st-pop" style={{ left: pop.x, top: pop.y }}
          onMouseEnter={() => window.clearTimeout(popTimer.current)} onMouseLeave={hidePop}>
          <h4>{pop.term.term}{pop.term.en && <span className="en">{pop.term.en}</span>}</h4>
          {pop.term.video_says && <div className="row"><span className="tagl video">视频里</span>{pop.term.video_says}
            {pop.term.t != null && <button className="tcl" onClick={() => seek(pop.term.t!)}>{fmtDuration(pop.term.t)}</button>}</div>}
          {pop.term.background && <div className="row"><span className="tagl bg">背景</span>{pop.term.background}</div>}
          <div className="src">来自学习底稿 · 出现 {pop.term.mentions} 次</div>
        </div>
      )}
      </div>
    </div>
  )
}

// ---------- 播放器 ----------

function Player({ video, state, mediaRef, onTimeUpdate, onMeta, mediaErr, setMediaErr, reload }: {
  video: Video; state: StudyState | null; mediaRef: React.MutableRefObject<HTMLMediaElement | null>
  onTimeUpdate: () => void; onMeta: (duration: number, aspect: number | null) => void
  mediaErr: string | null; setMediaErr: (s: string | null) => void; reload: () => Promise<void>
}) {
  const media = state?.media
  const [jobId, setJobId] = useState<string | null>(null)
  const job = useJob(jobId)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<unknown>(null)

  useEffect(() => {
    if (job?.status === 'done') { setJobId(null); setBusy(null); void reload() }
    if (job?.status === 'failed') { setBusy(null) }
  }, [job?.status, reload])

  // 「存视频」走的是另一套下载队列，这里隔几秒看一眼下好了没有
  useEffect(() => {
    if (busy !== 'download') return
    const t = window.setInterval(() => { void reload() }, 5000)
    return () => window.clearInterval(t)
  }, [busy, reload])
  useEffect(() => { if (media?.video && busy === 'download') setBusy(null) }, [media?.video, busy])

  const remux = async () => {
    setErr(null)
    try {
      const r = await api.remux(video.video_id)
      if (r.cached) { await reload(); return }
      if (r.job) { setJobId(r.job.id); setBusy('remux') }
    } catch (e) { setErr(e) }
  }
  const download = async () => {
    if (!video.source_url) return
    setErr(null)
    try { await api.saveVideo(video.source_url); setBusy('download') } catch (e) { setErr(e) }
  }

  const common = {
    ref: (el: HTMLMediaElement | null) => { mediaRef.current = el },
    onTimeUpdate, onSeeked: onTimeUpdate,
    onLoadedMetadata: (e: React.SyntheticEvent<HTMLMediaElement>) => {
      setMediaErr(null)
      const el = e.currentTarget as HTMLVideoElement
      onMeta(el.duration || 0, el.videoHeight ? el.videoWidth / el.videoHeight : null)
    },
    preload: 'metadata' as const,
    controls: true,
  }

  if (media?.video && !mediaErr) {
    return (
      <div className="st-player">
        <video {...common} src={api.mediaUrl(video.video_id)}
          onError={() => setMediaErr('浏览器放不了这个视频（多半是编码不支持，比如 HEVC 没有硬件解码）')} />
      </div>
    )
  }

  return (
    <div className="st-player">
      <div className="st-noscreen">
        <Film />
        <div>
          {!state ? <span><span className="spin" /> 读取中…</span>
            : mediaErr ? <b>{mediaErr}</b>
            : media?.remuxable ? <><b>原文件是 {media.source_ext}，浏览器放不了</b><span>无损转成 mp4（不重新编码，几十秒），放在产物目录里</span></>
            : media?.downloadable ? <><b>还没存过这条视频的画面</b><span>存一份到「存视频」目录（按设置里的清晰度），下好自动出现</span></>
            : <><b>没有能播放的视频文件</b><span>{video.is_local ? '原文件找不到了，挪过地方的话把新路径在新任务里再加一次' : '在设置里配好「存视频」目录后可以下一份'}</span></>}
          {job?.status === 'failed' && <span className="bad">{job.error}</span>}
          <ErrorBox error={err} />
          <div className="acts">
            {media?.remuxable && !mediaErr && (
              <button className="btn primary" onClick={remux} disabled={busy != null}>
                {busy === 'remux' ? <><span className="spin" /> 转换中…</> : '转成 mp4'}
              </button>
            )}
            {media?.downloadable && (
              <button className="btn primary" onClick={download} disabled={busy != null}>
                {busy === 'download' ? <><span className="spin" /> 下载中…</> : '存视频'}
              </button>
            )}
            {media?.audio && <span className="hint">下面先用音频听</span>}
          </div>
        </div>
      </div>
      {media?.audio && <audio {...common} src={api.audioUrl(video.video_id)} />}
    </div>
  )
}

function ChapterStrip({ chapters, duration, time, asked, onSeek }: {
  chapters: StudyChapter[]; duration: number; time: number; asked: number[]; onSeek: (s: number) => void
}) {
  const [hover, setHover] = useState<{ x: number; text: string } | null>(null)
  if (!duration || !chapters.length) return null
  const cur = chapterAt(chapters, time)
  const at = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    return { x: e.clientX - r.left, sec: (e.clientX - r.left) / r.width * duration }
  }
  return (
    <div className="st-strip" onMouseLeave={() => setHover(null)}
      onMouseMove={(e) => { const { x, sec } = at(e); setHover({ x, text: `${fmtDuration(sec)} · ${chapters[chapterAt(chapters, sec)]?.title ?? ''}` }) }}
      onClick={(e) => onSeek(at(e).sec)}>
      {chapters.map((c, i) => (
        <div key={i} className={`seg${i === cur ? ' cur' : i < cur ? ' done' : ''}`}
          style={{ left: `${c.start / duration * 100}%`, width: `${Math.max(0.3, (c.end - c.start) / duration * 100)}%` }} />
      ))}
      <div className="head" style={{ left: `${Math.min(100, time / duration * 100)}%` }} />
      {asked.map((p, i) => <div key={i} className="qdot" style={{ left: `${p / duration * 100}%` }} title={`你在 ${fmtDuration(p)} 问过`} />)}
      {hover && <div className="tip" style={{ left: hover.x }}>{hover.text}</div>}
    </div>
  )
}

// ---------- 学习底稿 ----------

function SheetPane({ id, state, time, onSeek, reload, setState, expandCurrent }: {
  id: string; state: StudyState; time: number; onSeek: (s: number, play?: boolean) => void
  reload: () => Promise<void>; setState: React.Dispatch<React.SetStateAction<StudyState | null>>
  // 横屏时视频旁边已经有本章卡，这里就别再自动展开当前章，免得同一段内容出现两遍
  expandCurrent: boolean
}) {
  // 手动展开看的章节（不用跳过去播放就能读别的章的论证线）
  const [opened, setOpened] = useState<Set<number>>(() => new Set())
  const toggle = (i: number) => setOpened((s) => {
    const n = new Set(s)
    if (n.has(i)) n.delete(i); else n.add(i)
    return n
  })
  const [jobId, setJobId] = useState<string | null>(state.job?.id ?? null)
  const job = useJob(jobId)
  const [err, setErr] = useState<unknown>(null)
  const running = job != null && !['done', 'failed', 'cancelled'].includes(job.status)

  useEffect(() => { if (state.job && !jobId) setJobId(state.job.id) }, [state.job, jobId])
  useEffect(() => {
    if (job?.status === 'done') { setJobId(null); void reload() }
  }, [job?.status, reload])

  const generate = async (force = false) => {
    if (force && !confirm('重新调用模型生成一份底稿，会花钱。之前标的「已懂」会保留。继续？')) return
    setErr(null)
    try {
      const r = await api.generateStudy(id, force)
      if (r.cached) { await reload(); return }
      if (r.job) setJobId(r.job.id)
    } catch (e) { setErr(e) }
  }
  const toggleKnown = async (term: string, known: boolean) => {
    try {
      const r = await api.setKnown(id, term, known)
      setState((s) => s && s.sheet ? { ...s, sheet: { ...s.sheet, prerequisites: r.prerequisites } } : s)
    } catch (e) { setErr(e) }
  }

  const est = 'error' in state.estimate ? null : state.estimate
  const stage = job?.stages[job.stages.length - 1]?.detail
  const sheet = state.sheet

  if (!sheet) {
    return (
      <div className="st-pane">
        {job?.status === 'failed' && <div className="errbox" style={{ marginBottom: 12 }}>{job.error}</div>}
        <ErrorBox error={err} />
        <div className="st-gen">
          <b>先生成一份学习底稿</b>
          <p>看之前要懂的概念、每章的论证线（论点 → 证据 → 结论）、术语表。生成一次，之后提问、复看都免费复用。</p>
          {'error' in state.estimate
            ? <Pill tone="bad">{state.estimate.error}</Pill>
            : est && <p className="mono">转写 {fmtTokens(est.transcript_tokens)} tokens · {est.calls} 次调用 · 预计 {fmtMoney(est.cost, est.currency) ?? `${fmtTokens(est.input_tokens)} tokens 输入`}</p>}
          <button className="btn primary" onClick={() => generate()} disabled={running || !est}>
            {running ? <><span className="spin" /> {job?.status === 'queued' ? '排队中' : stage || '生成中…'}</> : '生成学习底稿'}
          </button>
        </div>
      </div>
    )
  }

  const meta = state.sheet_meta
  const curCh = chapterAt(sheet.chapters, time)
  const known = sheet.prerequisites.filter((p) => p.known).length
  return (
    <div className="st-pane">
      <div className="st-genbar">
        <span className="ok" />
        <span title={`${meta?.provider ?? ''} 生成`}>底稿已生成 · {fmtWhen(meta?.created_at)}</span>
        {(meta?.stale || meta?.outdated) && <Pill tone="warn">{meta?.outdated ? '底稿格式更新了' : '转写改过'}，可以重新生成</Pill>}
        <span className="sp" />
        {running
          ? <span><span className="spin" /> {stage || '重新生成中…'}</span>
          : <button className="lnk" onClick={() => generate(true)}><RefreshCw size={12} /> 重新生成</button>}
      </div>
      {job?.status === 'failed' && <div className="errbox" style={{ margin: '10px 0' }}>{job.error}</div>}
      <ErrorBox error={err} />

      {sheet.prerequisites.length > 0 && <>
        <div className="st-sec">看之前 · 前置知识<span className="sp" /><span className="r">已懂 {known} / {sheet.prerequisites.length}</span></div>
        <div className="st-pre">
          {sheet.prerequisites.map((p) => (
            <details key={p.term} className={p.known ? 'known' : ''} open={!p.known && p === sheet.prerequisites.find((x) => !x.known)}>
              <summary>
                <button className="chk" aria-label={p.known ? '标为没懂' : '标为已懂'} title={p.known ? '标为没懂' : '标为已懂'}
                  onClick={(e) => { e.preventDefault(); void toggleKnown(p.term, !p.known) }} />
                <span className="nm">{p.term}</span>
                <span className="hint">{p.known ? '已懂' : p.chapter ? `第 ${p.chapter} 章用到` : ''}</span>
              </summary>
              <div className="d"><span className="tagl bg">背景</span>{p.explain}</div>
            </details>
          ))}
        </div>
      </>}

      <div className="st-sec">章节与论证线<span className="sp" /><span className="r">{sheet.chapters.length} 章</span></div>
      <div className="st-chs">
        {sheet.chapters.map((c, i) => (
          <div key={i} className={`ch${i === curCh ? ' cur' : i < curCh ? ' done' : ''}`}>
            <div className="rowwrap">
              <button className="row" onClick={() => onSeek(c.start)} title="跳到这一章">
                <span className="tm">{fmtDuration(c.start)}</span>
                <span className="nm">{i + 1}. {c.title}</span>
              </button>
              <button className={`iconbtn exp${opened.has(i) || (expandCurrent && i === curCh) ? ' on' : ''}`}
                title="展开 / 收起这一章的论证线" aria-label="展开论证线" onClick={() => toggle(i)}><ChevronDown /></button>
            </div>
            {(opened.has(i) || (expandCurrent && i === curCh)) && <Argument c={c} onSeek={onSeek} />}
          </div>
        ))}
      </div>

      {sheet.glossary.length > 0 && <>
        <div className="st-sec">术语表<span className="sp" /><span className="r">{sheet.glossary.length} 条</span></div>
        <div className="st-gl">
          {sheet.glossary.map((t) => (
            <div className="gi" key={t.term}>
              <div className="h"><b>{t.term}</b>{t.en && <span className="en">{t.en}</span>}<span className="cnt">出现 {t.mentions} 次</span></div>
              {t.video_says && <div className="v"><span className="tagl video">视频里</span>{t.video_says}
                {t.t != null && <> <button className="tcl" onClick={() => onSeek(t.t!)}>{fmtDuration(t.t)}</button></>}</div>}
              {t.background && <div className="bg"><span className="tagl bg">背景</span>{t.background}</div>}
            </div>
          ))}
        </div>
      </>}
      <p className="st-foot">「视频里」只依据转写；「背景」是模型自己的知识，没联网核实过。</p>
    </div>
  )
}

function ChapterCard({ chapters, idx, pct, time, onSeek, upcoming }: {
  chapters: StudyChapter[]; idx: number; pct: number; time: number; onSeek: (s: number) => void; upcoming?: boolean
}) {
  const ch = chapters[idx]
  const next = upcoming ? chapters.slice(idx + 1, idx + 4) : []
  return (
    <div className={`sp-chapcard${upcoming ? ' big' : ''}`}><div className="in">
      <div className="hd">
        <span className="no">第 {idx + 1}/{chapters.length} 章</span>
        <span className="tm">{fmtDuration(ch.start)}–{fmtDuration(ch.end)}</span>
        <span className="sp" />
        <button className="iconbtn" title="上一章" aria-label="上一章"
          onClick={() => onSeek(chapters[Math.max(0, time - ch.start > 3 ? idx : idx - 1)].start)}><ChevronLeft /></button>
        <button className="iconbtn" title="下一章" aria-label="下一章" disabled={idx >= chapters.length - 1}
          onClick={() => chapters[idx + 1] && onSeek(chapters[idx + 1].start)}><ChevronRight /></button>
      </div>
      <div className="t">{ch.title}</div>
      <div className="pgbar"><i style={{ width: `${pct}%` }} /></div>
      <div className="argwrap">
        <Argument c={ch} onSeek={onSeek} />
        {next.length > 0 && (
          <div className="next">
            <div className="lbl">接下来</div>
            {next.map((c, j) => (
              <button key={j} className="nx" onClick={() => onSeek(c.start)}>
                <span className="tm">{fmtDuration(c.start)}</span>
                <span><b>{c.title}</b>{c.claim && <span className="cl">{c.claim}</span>}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div></div>
  )
}

function Argument({ c, onSeek }: { c: StudyChapter; onSeek: (s: number) => void }) {
  return (
    <div className="arg">
      {c.claim && <div className="st"><span className="k">论点</span><div>{c.claim}</div></div>}
      {c.evidence.length > 0 && (
        <div className="st"><span className="k">证据</span>
          <ul>{c.evidence.map((e, j) => (
            <li key={j}>{e.text}{e.t != null && <> <button className="tcl" onClick={() => onSeek(e.t!)}>{fmtDuration(e.t)}</button></>}</li>
          ))}</ul>
        </div>
      )}
      {c.conclusion && <div className="st"><span className="k">结论</span><div>{c.conclusion}</div></div>}
    </div>
  )
}

// ---------- 提问 ----------

const TS_RE = /\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]/g
const SRC_RE = /〔(\d{1,2})〕/g

function answerHtml(md: string, sources: StudySource[]): string {
  const byN = new Map(sources.map((s) => [s.n, s]))
  const withLinks = md
    // **「…」**，这种紧挨中文标点的加粗，CommonMark 的定界规则认不出来，会原样露出星号
    .replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
    .replace(TS_RE, (_m, a: string, b: string, c?: string) => {
      const secs = c != null ? Number(a) * 3600 + Number(b) * 60 + Number(c) : Number(a) * 60 + Number(b)
      return `<a class="cite" data-t="${secs}" href="#t=${secs}">${c != null ? `${a}:${b}:${c}` : `${a}:${b}`}</a>`
    })
    .replace(SRC_RE, (_m, n: string) => {
      const s = byN.get(Number(n))
      if (!s) return `<sup class="srcref">${n}</sup>`
      const title = s.title.replace(/"/g, '&quot;')
      return `<a class="srcref" href="${encodeURI(s.url)}" target="_blank" rel="noreferrer" title="${title}">${n}</a>`
    })
  return DOMPurify.sanitize(marked.parse(withLinks, { async: false }) as string, { ADD_ATTR: ['target'] })
}

type Block = { kind: 'video' | 'bg' | 'conflict' | 'plain'; body: string }
function splitAnswer(md: string): Block[] {
  const blocks: Block[] = []
  const parts = md.split(/^#{2,4}\s*/m)
  parts.forEach((part, i) => {
    if (!part.trim()) return
    if (i === 0 && !/^#{2,4}\s/.test(md.trimStart())) { blocks.push({ kind: 'plain', body: part }); return }
    const nl = part.indexOf('\n')
    const head = (nl < 0 ? part : part.slice(0, nl)).trim()
    const body = nl < 0 ? '' : part.slice(nl + 1)
    const kind = head.includes('视频里') ? 'video' : head.includes('背景') ? 'bg' : head.includes('出入') ? 'conflict' : 'plain'
    blocks.push({ kind, body: kind === 'plain' ? `### ${part}` : body })
  })
  return blocks
}

function Answer({ q, onSeek }: { q: StudyQuestion; onSeek: (s: number) => void }) {
  const blocks = useMemo(() => splitAnswer(q.answer), [q.answer])
  const onClick = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest('a.cite') as HTMLAnchorElement | null
    if (!a) return
    e.preventDefault()
    onSeek(Number(a.dataset.t))
  }
  const cited = q.sources.filter((s) => q.answer.includes(`〔${s.n}〕`))
  return (
    <div className="a" onClick={onClick}>
      {blocks.map((b, i) => (
        <div key={i} className={`blk ${b.kind}`}>
          {b.kind === 'video' && <div className="bh">视频里说</div>}
          {b.kind === 'bg' && <div className="bh">
            {cited.length > 0 ? <span className="tagl web">联网</span> : <span className="tagl unv">模型知识 · 未核实</span>}背景补充
          </div>}
          {b.kind === 'conflict' && <div className="bh">有出入</div>}
          <div dangerouslySetInnerHTML={{ __html: answerHtml(b.body, q.sources) }} />
          {b.kind === 'bg' && cited.length > 0 && (
            <div className="srcs">
              <span>检索 {q.searches.length} 次{cited.length < q.sources.length ? ` · 引用了 ${cited.length} 条` : ''} · 没标编号的句子是模型自己的知识</span>
              {cited.map((s) => <a key={s.n} href={s.url} target="_blank" rel="noreferrer">〔{s.n}〕{s.title}</a>)}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function AskPane({ state, onSeek, setState }: {
  state: StudyState; onSeek: (s: number) => void; reload: () => Promise<void>
  setState: React.Dispatch<React.SetStateAction<StudyState | null>>
}) {
  const listRef = useRef<HTMLDivElement>(null)
  const [err, setErr] = useState<unknown>(null)
  const chapters = state.sheet?.chapters ?? []
  useEffect(() => {
    listRef.current?.lastElementChild?.scrollIntoView({ block: 'nearest' })
  }, [state.questions.length])

  const remove = async (qid: number | null) => {
    if (qid == null) return
    try {
      await api.deleteQuestion(qid)
      setState((s) => s ? { ...s, questions: s.questions.filter((x) => x.id !== qid) } : s)
    } catch (e) { setErr(e) }
  }

  return (
    <div className="st-pane" ref={listRef}>
      <ErrorBox error={err} />
      {state.questions.length === 0 && (
        <div className="st-empty">
          看到卡住的地方，暂停按 <kbd>Q</kbd> 直接问。AI 能看到全片转写、学习底稿和你当前位置附近的原文。
          {!state.sheet && <><br />先生成学习底稿，回答会更准（它知道章节结构）。</>}
        </div>
      )}
      {state.questions.map((q) => {
        const ch = q.chapter ?? (q.position != null ? chapterAt(chapters, q.position) : -1)
        const used = q.used
        return (
          <div className="st-turn" key={q.id ?? q.created_at}>
            <div className="q">
              <span className="at">
                {q.position != null && <button className="tcl" onClick={() => onSeek(q.position!)}>{fmtDuration(q.position)}</button>}
                {ch != null && ch >= 0 && chapters[ch] && <span>第 {ch + 1} 章</span>}
                {q.spoiler_guard && <span>防剧透</span>}
              </span>
              <div className="bubble">{q.question}</div>
            </div>
            <Answer q={q} onSeek={onSeek} />
            <div className="afoot">
              {used && <span className="mono" title={`${used.calls} 次调用`}>
                输入 {fmtTokens(used.input_tokens)}{used.cached_tokens ? `（缓存命中 ${fmtTokens(used.cached_tokens)}）` : ''} · 输出 {fmtTokens(used.output_tokens)}
                {used.cost != null ? ` · ${fmtMoney(used.cost, used.currency)}` : ''}
              </span>}
              {q.context_mode === 'outline' && <Pill tone="warn">视频太长，只带了附近几章原文</Pill>}
              <span className="sp" />
              <span>{fmtWhen(q.created_at)}</span>
              <button className="iconbtn danger" title="删除这条" onClick={() => remove(q.id)}><Trash2 /></button>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function AskBox({ id, state, time, chapter, chapterTitle, inputRef, onAsked, onAsking }: {
  id: string; state: StudyState; time: number; chapter: number | null; chapterTitle?: string
  inputRef: React.RefObject<HTMLTextAreaElement | null>; onAsked: (q: StudyQuestion) => void; onAsking: () => void
}) {
  const [q, setQ] = useState('')
  const [asking, setAsking] = useState<string | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [web, setWeb] = useState(() => readPref('vsum.study.web', true))
  const [spoiler, setSpoiler] = useState(() => readPref('vsum.study.spoiler', false))
  const webOn = web && state.websearch.enabled

  const ask = async (text?: string) => {
    const question = (text ?? q).trim()
    if (!question || asking) return
    setAsking(question); setErr(null); onAsking()
    try {
      const a = await api.studyAsk(id, { question, position: time, spoiler_guard: spoiler, web: webOn })
      onAsked(a)
      if (!text) {
        setQ('')
        if (inputRef.current) inputRef.current.style.height = ''
      }
    } catch (e) { setErr(e) } finally { setAsking(null) }
  }

  const est = state.ask_estimate
  const webTitle = !state.websearch.configured
    ? `没配检索服务：在 .env 里填 ${state.websearch.key_env} 就能联网（${state.websearch.provider}）`
    : !state.websearch.enabled ? '当前模型不支持工具调用，联网用不了' : '模型觉得需要时会自己检索，每问最多几次'

  return (
    <div className="st-askbox">
      {asking && <div className="st-asking"><span className="spin" /> 「{asking.length > 24 ? asking.slice(0, 24) + '…' : asking}」{webOn ? '在想，可能会联网查一下…' : '在想…'}</div>}
      <ErrorBox error={err} />
      <div className="quick">
        {QUICK.map((t) => <button key={t} onClick={() => ask(t)} disabled={!!asking}>{t.replace(/[？?]$/, '')}</button>)}
      </div>
      <div className="box">
        <span className="pin"><MapPin size={12} />{fmtDuration(time)}{chapter != null && chapterTitle ? ` · 第 ${chapter + 1} 章` : ''}</span>
        <textarea ref={inputRef} rows={1} value={q} disabled={!!asking}
          onChange={(e) => {
            setQ(e.target.value)
            const el = e.target
            el.style.height = 'auto'
            el.style.height = `${Math.min(el.scrollHeight, 140)}px`
          }}
          placeholder="在这里卡住了？直接问。Enter 发送，Shift+Enter 换行"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void ask() }
            if (e.key === 'Escape') (e.target as HTMLTextAreaElement).blur()
          }} />
        <div className="row">
          <button className={`tg${webOn ? ' on' : ''}`} disabled={!state.websearch.enabled} title={webTitle}
            onClick={() => { setWeb(!web); writePref('vsum.study.web', !web) }}><span className="sw" />联网</button>
          <button className={`tg${spoiler ? ' on' : ''}`} title="开了以后，后面才讲到的内容只给时间点，不展开"
            onClick={() => { setSpoiler(!spoiler); writePref('vsum.study.spoiler', !spoiler) }}><span className="sw" />防剧透</button>
          <span className="sp" />
          {est && <span className="mono" title={`固定前缀约 ${fmtTokens(est.prefix_tokens)} tokens，同一条视频第二问起大部分命中缓存`}>
            约 {fmtMoney(est.cost, est.currency) ?? `${fmtTokens(est.input_tokens)} tok`}
          </span>}
          <button className="send" onClick={() => ask()} disabled={!q.trim() || !!asking} aria-label="发送"><Send size={15} /></button>
        </div>
      </div>
    </div>
  )
}

export type { StudySheet }
