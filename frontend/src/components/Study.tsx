import DOMPurify from 'dompurify'
import {
  ArrowLeft, BookOpen, ChevronDown, ChevronLeft, ChevronRight, Film, ListTree, MapPin, MessageCircle, PanelRightClose,
  PanelRightOpen, RefreshCw, ScrollText, Send, Trash2, X,
} from 'lucide-react'
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
 * 视频是主角：横屏视频按高度撑满，侧栏（本章 / 问 / 底稿 / 转写，提问框常驻底部）用剩下的宽度，
 * 不够才收成图标按需浮出；竖屏录屏三栏「视频 | 本章 | 侧栏」。规则见 computeGeo。
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

type Tab = 'chap' | 'ask' | 'sheet' | 'tx'
type Kind = 'land' | 'port' | 'flat'

// 布局尺寸（和 index.css 里 .sp2 那一段对应）
const TOP_H = 40
const PAD_X = 16
const PAD_Y = 12
const STRIP_H = 34       // 章节时间轴（带当前章标签）
const LINE_H = 54        // 一行本章
const GAP = 10
const RAIL_W = 52
const MIN_PANEL = 360
const MAX_PANEL = 480
const WIDE_PANEL = 640   // 转写「加宽阅读」
const MAX_GIVE = 0.1     // 为了让侧栏常驻，视频最多让出多少宽度
const FULL_CARD_MIN = 170

interface Geo {
  kind: Kind
  panel: number          // 0 = 侧栏收成图标，按需浮出
  w: number              // 视频盒子尺寸（flat 时不用）
  h: number
  card: 'line' | 'full'
  midTwo: boolean        // 竖屏：中间那栏够宽，分成「本章 | 章节目录」两块
}

/**
 * 布局规则（视觉稿 docs/mockup/v0.9-study-v2.html）：
 * 视频先按高度撑满；右边剩下的宽度够就放常驻侧栏；差一点就让视频让出不超过 10%；
 * 差得多才把侧栏收成一列图标、按需浮出。竖屏录屏视频按高度撑满一栏，中间竖排本章。
 */
function computeGeo(vw: number, vh: number, kind: Kind, ar: number, forced: boolean, wide: boolean): Geo {
  const H = vh - TOP_H - PAD_Y * 2
  if (kind === 'port') {
    const h = Math.max(200, H - STRIP_H - GAP)
    const w = Math.floor(h * ar)
    const avail = vw - PAD_X * 2 - w - GAP
    // 宽屏上中间那栏会很宽：侧栏跟着放宽一些（最多 560），剩下的给本章
    let panel = forced ? 0 : wide ? WIDE_PANEL : Math.min(560, Math.max(400, Math.round(avail * 0.32)))
    // 中间那栏（本章）至少留 320
    if (panel && avail - GAP - panel < 320) panel = 0
    const mid = avail - (panel ? panel + GAP : RAIL_W)
    return { kind, panel, w, h: Math.floor(h), card: 'full', midTwo: mid >= 1000 }
  }
  if (kind === 'flat') {
    return { kind, panel: forced ? 0 : wide ? WIDE_PANEL : 420, w: 0, h: 0, card: 'full', midTwo: false }
  }
  const wByH = Math.floor((H - STRIP_H - LINE_H - GAP * 2) * ar)
  const free = vw - PAD_X * 2 - wByH
  let panel = 0
  if (!forced) {
    if (wide) panel = WIDE_PANEL
    else {
      const want = Math.min(MAX_PANEL, Math.max(MIN_PANEL, free))
      if (want - free <= wByH * MAX_GIVE) panel = want
      else if (MIN_PANEL - free <= wByH * MAX_GIVE) panel = MIN_PANEL
    }
  }
  const W = vw - PAD_X * 2 - (panel ? panel + GAP : RAIL_W)
  const w = Math.max(320, Math.min(W, wByH))
  const h = Math.floor(w / ar)
  const rest = H - h - STRIP_H - GAP
  return { kind, panel, w: Math.floor(w), h, card: rest >= FULL_CARD_MIN ? 'full' : 'line', midTwo: false }
}

function useViewport() {
  const [vp, setVp] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }))
  useEffect(() => {
    const on = () => setVp((v) => (v.w === window.innerWidth && v.h === window.innerHeight ? v : { w: window.innerWidth, h: window.innerHeight }))
    window.addEventListener('resize', on)
    // 有些情况下（浏览器缩放、开发者工具的设备模拟）resize 事件不可靠，根元素尺寸变化再兜一层
    const ro = new ResizeObserver(on)
    ro.observe(document.documentElement)
    return () => { window.removeEventListener('resize', on); ro.disconnect() }
  }, [])
  return vp
}

function readNum(key: string): number | null {
  try { const v = localStorage.getItem(key); return v == null ? null : Number(v) } catch { return null }
}

export function Study({ video }: { video: Video }) {
  const id = video.video_id
  const [state, setState] = useState<StudyState | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [tab, setTabRaw] = useState<Tab>('chap')
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(video.duration_sec || 0)
  const [mediaErr, setMediaErr] = useState<string | null>(null)
  const [aspect, setAspect] = useState(16 / 9)
  const [forced, setForced] = useState(() => readPref('vsum.study.cinema', false))
  const [drawer, setDrawer] = useState(false)
  const [wide, setWide] = useState(false)
  const [paused, setPaused] = useState(true)
  const [toast, setToast] = useState<{ text: string; action?: { label: string; run: () => void } } | null>(null)
  const [chipUntil, setChipUntil] = useState(0)
  const mediaRef = useRef<HTMLMediaElement | null>(null)
  const askRef = useRef<HTMLTextAreaElement>(null)
  const vp = useViewport()

  const load = useCallback(async () => {
    try { setState(await api.study(id)); setErr(null) } catch (e) { setErr(e) }
  }, [id])
  useEffect(() => { setState(null); setTime(0); setMediaErr(null); void load() }, [load])

  const sheet = state?.sheet ?? null
  const chapters = useMemo(() => sheet?.chapters ?? [], [sheet])
  const curCh = chapterAt(chapters, time)
  const ch = curCh >= 0 ? chapters[curCh] : undefined
  const chPct = ch ? Math.min(100, Math.max(0, (time - ch.start) / Math.max(1, ch.end - ch.start) * 100)) : 0

  const kind: Kind = state?.media.video && !mediaErr ? (aspect < 1 ? 'port' : 'land') : 'flat'
  const geo = computeGeo(vp.w, vp.h, kind, aspect, forced, wide && tab === 'tx')
  const docked = geo.panel > 0
  // 「本章」标签只在本章被压成一行时出现；别的布局里本章已经整块摆在画面上
  const hasChapTab = kind === 'land' && geo.card === 'line' && !!ch
  // 没有画面时转写直接铺在舞台上，侧栏就不再放一份
  const tabs: Tab[] = kind === 'flat' ? ['ask', 'sheet'] : hasChapTab ? ['chap', 'ask', 'sheet', 'tx'] : ['ask', 'sheet', 'tx']
  const cur: Tab = tabs.includes(tab) ? tab : 'ask'

  const setTab = useCallback((t: Tab) => { setTabRaw(t); if (t !== 'tx') setWide(false) }, [])
  // 打开侧栏的某个标签：常驻时切标签，收起时浮出
  const openTab = useCallback((t: Tab) => {
    setTab(t)
    if (!docked) setDrawer(true)
  }, [docked, setTab])

  const seek = useCallback((sec: number, play = true) => {
    const m = mediaRef.current
    setTime(sec)
    if (!m) return
    m.currentTime = sec
    if (play) void m.play().catch(() => { /* 自动播放被拦就算了 */ })
  }, [])

  // 浮出的侧栏是这次渲染才挂上去的，等它挂好再把光标放进提问框
  const [focusReq, setFocusReq] = useState(0)
  useEffect(() => {
    if (!focusReq || !askRef.current) return
    askRef.current.focus()
    setFocusReq(0)
  }, [focusReq, drawer])
  const focusAsk = useCallback(() => {
    mediaRef.current?.pause()
    openTab('ask')
    setFocusReq((n) => n + 1)
  }, [openTab])

  const toggleForced = useCallback(() => {
    writePref('vsum.study.cinema', !forced)
    setForced(!forced)
    setDrawer(false)
  }, [forced])

  // 快捷键：空格 播放/暂停，←/→ 5 秒，Q 提问，T 转写，F 收起/展开侧栏，Esc 收回浮出的侧栏
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)
      if (e.key === 'Escape') {
        if (typing) (t as HTMLElement).blur()
        setDrawer(false)
        return
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return
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
        openTab('tx')
      } else if (e.key === 'f' || e.key === 'F') {
        e.preventDefault()
        toggleForced()
      }
    }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [focusAsk, openTab, toggleForced])

  // 记住看到哪了：长视频关了再开接着看
  const posKey = `vsum.study.pos.${id}`
  const onTimeUpdate = () => {
    const m = mediaRef.current
    if (!m) return
    const sec = Math.floor(m.currentTime)
    setTime((c) => {
      if (c !== sec && sec % 5 === 0) { try { localStorage.setItem(posKey, String(sec)) } catch { /* ignore */ } }
      return c === sec ? c : sec
    })
  }
  const onMeta = (d: number, ar: number | null) => {
    setDuration(d)
    if (ar) setAspect(ar)
    const saved = readNum(posKey)
    const m = mediaRef.current
    if (m && saved && saved > 30 && saved < d - 30) {
      m.currentTime = saved
      setTime(saved)
      setToast({ text: `接着上次看到的 ${fmtDuration(saved)}`, action: { label: '从头开始', run: () => { seek(0, false); setToast(null) } } })
      window.setTimeout(() => setToast((t) => (t?.text.startsWith('接着') ? null : t)), 6000)
    }
  }

  // 进入新的一章：视频左上角的章节标签亮几秒
  const lastCh = useRef(-1)
  useEffect(() => {
    if (curCh < 0) return
    if (lastCh.current !== -1 && lastCh.current !== curCh) setChipUntil(Date.now() + 4000)
    lastCh.current = curCh
  }, [curCh])
  const [, tick] = useState(0)
  useEffect(() => {
    if (!chipUntil) return
    const t = window.setTimeout(() => tick((x) => x + 1), Math.max(0, chipUntil - Date.now()) + 50)
    return () => window.clearTimeout(t)
  }, [chipUntil])
  const chipOn = !!ch && (paused || Date.now() < chipUntil)

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
    setPop({ term, x: Math.max(8, Math.min(r.left, window.innerWidth - 340)), y: Math.min(r.bottom + 6, window.innerHeight - 220) })
  }
  const hidePop = () => { popTimer.current = window.setTimeout(() => setPop(null), 180) }

  const renderTerms = termRe ? (text: string) => text.split(termRe).map((part, i) =>
    i % 2 === 1
      ? <span key={i} className="st-term" onMouseEnter={(e) => showPop(e, part)} onMouseLeave={hidePop}>{part}</span>
      : <Fragment key={i}>{part}</Fragment>) : null

  const asked = state?.questions ?? []
  const askedPos = asked.map((q) => q.position).filter((p): p is number => p != null)

  const strip = (
    <ChapterStrip chapters={chapters} duration={duration || video.duration_sec} time={time} asked={askedPos} onSeek={(s) => seek(s)} />
  )
  const player = (
    <div className="sp2-video" style={kind === 'flat' ? undefined : { width: geo.w, height: geo.h }}>
      <Player video={video} state={state} mediaRef={mediaRef} onTimeUpdate={onTimeUpdate} onMeta={onMeta}
        onPlayState={setPaused} mediaErr={mediaErr} setMediaErr={setMediaErr} reload={load} />
      {kind !== 'flat' && ch && (
        <div className={`sp2-chip${chipOn ? ' on' : ''}`}>
          <span className="n">{curCh + 1}/{chapters.length}</span><b>{ch.title}</b>
        </div>
      )}
      {toast && (
        <div className="sp2-toast">
          {toast.text}
          {toast.action && <button onClick={toast.action.run}>{toast.action.label}</button>}
          <button className="x" onClick={() => setToast(null)} aria-label="关闭">×</button>
        </div>
      )}
    </div>
  )

  const panel = state && (
    <>
      <div className="st-tabs" role="tablist">
        {hasChapTab && <button role="tab" aria-selected={cur === 'chap'} onClick={() => setTab('chap')}>本章</button>}
        <button role="tab" aria-selected={cur === 'ask'} onClick={() => setTab('ask')}>问 {asked.length > 0 && <span className="n">{asked.length}</span>}</button>
        <button role="tab" aria-selected={cur === 'sheet'} onClick={() => setTab('sheet')}>底稿</button>
        {kind !== 'flat' && <button role="tab" aria-selected={cur === 'tx'} onClick={() => setTab('tx')}>转写</button>}
        <span className="sp" />
        {docked
          ? kind !== 'flat' && <button className="iconbtn" onClick={toggleForced} title="收起侧栏，视频更大（F）" aria-label="收起侧栏"><PanelRightClose /></button>
          : <button className="iconbtn" onClick={() => setDrawer(false)} title="收回（Esc）" aria-label="收回"><X /></button>}
      </div>
      {cur === 'chap' && ch && (
        <div className="st-pane"><ChapterBody chapters={chapters} idx={curCh} pct={chPct} time={time} onSeek={seek} /></div>
      )}
      {cur === 'sheet' && <SheetPane id={id} state={state} time={time} onSeek={seek} reload={load} setState={setState} expandCurrent={false} />}
      {cur === 'ask' && <AskPane state={state} onSeek={seek} reload={load} setState={setState} />}
      {cur === 'tx' && kind !== 'flat' && (
        <TranscriptPane video={video} chapters={chapters} time={time} onSeek={seek} renderTerms={renderTerms}
          wide={wide} onWide={() => setWide((v) => !v)} />
      )}
      <AskBox id={id} state={state} time={time} chapter={curCh >= 0 ? curCh : null} chapterTitle={ch?.title}
        inputRef={askRef} onAsked={(q) => { setState((s) => s ? { ...s, questions: [...s.questions, q] } : s); setTab('ask') }}
        onAsking={() => setTab('ask')} />
    </>
  )

  return (
    <div className={`sp2 ${kind}${docked ? '' : ' undocked'}`} style={{ '--panel': `${geo.panel}px` } as React.CSSProperties}>
      <header className="sp2-top">
        <Link className="iconbtn" to={`/video/${encodeURIComponent(id)}`} title="回到视频详情（速读）" aria-label="返回"><ArrowLeft /></Link>
        <div className="tt">
          <b title={video.title}>{displayTitle(video)}</b>
          <span>{video.uploader ?? ''}{video.uploader ? ' · ' : ''}{fmtDuration(duration || video.duration_sec)}</span>
        </div>
        <span className="sp" />
        <span className="keys"><kbd>空格</kbd>播放<kbd>←</kbd><kbd>→</kbd>5 秒<kbd>Q</kbd>提问<kbd>T</kbd>转写{kind !== 'flat' && <><kbd>F</kbd>{docked ? '收起侧栏' : '展开侧栏'}</>}</span>
      </header>

      <div className="sp2-body">
        <main className="sp2-stage">
          {kind === 'port' ? <>
            <div className="sp2-vcol" style={{ width: geo.w }}>{player}{strip}</div>
            <div className={`sp2-mid${geo.midTwo ? ' two' : ''}`}>{ch
              ? <>
                  <div className="sp2-midcard"><ChapterBody chapters={chapters} idx={curCh} pct={chPct} time={time} onSeek={seek} noNext={geo.midTwo} /></div>
                  {geo.midTwo && <div className="sp2-midcard"><ChapterIndex chapters={chapters} idx={curCh} onSeek={seek} /></div>}
                </>
              : <NoSheetHint state={state} onOpen={() => openTab('sheet')} />}</div>
          </> : <>
            <div className="sp2-vcol" style={kind === 'land' ? { width: geo.w } : undefined}>{player}{strip}</div>
            {ch
              ? geo.card === 'line'
                ? <ChapterLine chapters={chapters} idx={curCh} pct={chPct} time={time} onSeek={seek} width={geo.w}
                    onExpand={() => openTab('chap')} expanded={cur === 'chap' && (docked || drawer)} />
                : <HChapterCard chapters={chapters} idx={curCh} pct={chPct} time={time} onSeek={seek}
                    width={kind === 'land' ? geo.w : undefined} />
              : <NoSheetHint state={state} onOpen={() => openTab('sheet')} />}
            {kind === 'flat' && (
              <div className="sp2-flattx">
                <TranscriptPane video={video} chapters={chapters} time={time} onSeek={seek} renderTerms={renderTerms}
                  wide={false} onWide={null} />
              </div>
            )}
          </>}
        </main>

        {docked
          ? <aside className="st-right sp2-side">{panel}<ErrorBox error={err} />{!state && !err && <div className="st-pane"><span className="spin" /> 读取中…</div>}</aside>
          : <>
              <nav className="sp2-rail">
                {hasChapTab && <button className={drawer && cur === 'chap' ? 'on' : ''} onClick={() => drawer && cur === 'chap' ? setDrawer(false) : openTab('chap')} title="本章论证"><ListTree /></button>}
                <button className={drawer && cur === 'ask' ? 'on' : ''} onClick={() => drawer && cur === 'ask' ? setDrawer(false) : focusAsk()} title="问（Q）">
                  <MessageCircle />{asked.length > 0 && <span className="dot">{asked.length}</span>}
                </button>
                <button className={drawer && cur === 'sheet' ? 'on' : ''} onClick={() => drawer && cur === 'sheet' ? setDrawer(false) : openTab('sheet')} title="底稿"><BookOpen /></button>
                <button className={drawer && cur === 'tx' ? 'on' : ''} onClick={() => drawer && cur === 'tx' ? setDrawer(false) : openTab('tx')} title="转写（T）"><ScrollText /></button>
                <hr />
                <button onClick={toggleForced} title={forced ? '展开侧栏（F）' : '屏幕太窄，侧栏按需浮出'} disabled={!forced}><PanelRightOpen /></button>
              </nav>
              {drawer && <aside className="st-right sp2-side floating">{panel}</aside>}
            </>}
      </div>

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
  )
}

function NoSheetHint({ state, onOpen }: { state: StudyState | null; onOpen: () => void }) {
  if (!state) return <div className="sp2-nosheet"><span className="spin" /> 读取中…</div>
  return (
    <button className="sp2-nosheet" onClick={onOpen}>
      <b>还没有学习底稿</b>
      <span>生成后这里会显示当前章节的论点、证据和结论，提问也会更准。点这里去生成 ›</span>
    </button>
  )
}

/** 一行本章：章号、标题、论点（截断），底边是本章进度。点「展开论证」看侧栏的完整论证 */
function ChapterLine({ chapters, idx, pct, time, onSeek, width, onExpand, expanded }: {
  chapters: StudyChapter[]; idx: number; pct: number; time: number; onSeek: (s: number) => void
  width: number; onExpand: () => void; expanded: boolean
}) {
  const ch = chapters[idx]
  return (
    <div className="sp2-line" style={{ width }}>
      <span className="no">第 {idx + 1}/{chapters.length} 章</span>
      <b className="t">{ch.title}</b>
      <span className="claim" title={ch.claim}>{ch.claim}</span>
      <button className="iconbtn" title="上一章" aria-label="上一章"
        onClick={() => onSeek(chapters[Math.max(0, time - ch.start > 3 ? idx : idx - 1)].start)}><ChevronLeft /></button>
      <button className="iconbtn" title="下一章" aria-label="下一章" disabled={idx >= chapters.length - 1}
        onClick={() => chapters[idx + 1] && onSeek(chapters[idx + 1].start)}><ChevronRight /></button>
      {!expanded && <button className="more" onClick={onExpand}>展开论证 ›</button>}
      <i className="pg" style={{ width: `${pct}%` }} />
    </div>
  )
}

/** 横排本章卡：视频下面宽而矮的空间，左边章名，右边论点 | 证据 | 结论 */
function HChapterCard({ chapters, idx, pct, time, onSeek, width }: {
  chapters: StudyChapter[]; idx: number; pct: number; time: number; onSeek: (s: number) => void; width?: number
}) {
  const ch = chapters[idx]
  const next = chapters[idx + 1]
  return (
    <div className="sp2-hcard" style={width ? { width } : undefined}>
      <div className="head">
        <span className="no">第 {idx + 1}/{chapters.length} 章</span>
        <h3>{ch.title}</h3>
        <span className="tm">{fmtDuration(ch.start)} – {fmtDuration(ch.end)}</span>
        <div className="pg"><i style={{ width: `${pct}%` }} /></div>
        <div className="nav">
          <button onClick={() => onSeek(chapters[Math.max(0, time - ch.start > 3 ? idx : idx - 1)].start)}>‹ 上一章</button>
          <button disabled={!next} onClick={() => next && onSeek(next.start)}>下一章 ›</button>
        </div>
      </div>
      <div><span className="k">论点</span><p>{ch.claim}</p></div>
      <div><span className="k">证据</span><ul>{ch.evidence.map((e, j) => (
        <li key={j}>{e.text}{e.t != null && <> <button className="tcl" onClick={() => onSeek(e.t!)}>{fmtDuration(e.t)}</button></>}</li>
      ))}</ul></div>
      <div><span className="k">结论</span><p>{ch.conclusion}</p>
        {next && <button className="next" onClick={() => onSeek(next.start)}>下一章 · <span className="mono">{fmtDuration(next.start)}</span> <b>{next.title}</b></button>}
      </div>
    </div>
  )
}

/** 侧栏「本章」标签：竖排论证 + 接下来几章 */
function ChapterBody({ chapters, idx, pct, time, onSeek, noNext }: {
  chapters: StudyChapter[]; idx: number; pct: number; time: number; onSeek: (s: number) => void; noNext?: boolean
}) {
  const ch = chapters[idx]
  return (
    <div className="sp2-cbody">
      <div className="hd">
        <span className="no">第 {idx + 1}/{chapters.length} 章</span>
        <span className="tm">{fmtDuration(ch.start)} – {fmtDuration(ch.end)} · 已看 {Math.round(pct)}%</span>
      </div>
      <h3>{ch.title}</h3>
      <div className="pg"><i style={{ width: `${pct}%` }} /></div>
      <div className="nav">
        <button onClick={() => onSeek(chapters[Math.max(0, time - ch.start > 3 ? idx : idx - 1)].start)}>‹ 上一章</button>
        <button disabled={idx >= chapters.length - 1} onClick={() => chapters[idx + 1] && onSeek(chapters[idx + 1].start)}>下一章 ›</button>
      </div>
      <Argument c={ch} onSeek={onSeek} />
      {!noNext && idx + 1 < chapters.length && (
        <div className="next">
          <div className="lbl">接下来</div>
          {chapters.slice(idx + 1, idx + 4).map((c, j) => (
            <button key={j} className="nx" onClick={() => onSeek(c.start)}>
              <span className="tm">{fmtDuration(c.start)}</span>
              <span><b>{c.title}</b>{c.claim && <span className="cl">{c.claim}</span>}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 全片章节目录：当前章高亮并带论点，点哪章跳哪章。竖屏宽屏时放在中间那栏右半边 */
function ChapterIndex({ chapters, idx, onSeek }: { chapters: StudyChapter[]; idx: number; onSeek: (s: number) => void }) {
  const boxRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const box = boxRef.current
    const el = box?.querySelector<HTMLElement>('.ci.cur')
    if (box && el) box.scrollTo({ top: el.offsetTop - box.clientHeight / 3, behavior: 'smooth' })
  }, [idx])
  return (
    <div className="sp2-index" ref={boxRef}>
      <div className="lbl">全片 {chapters.length} 章</div>
      {chapters.map((c, i) => (
        <button key={i} className={`ci${i === idx ? ' cur' : i < idx ? ' done' : ''}`} onClick={() => onSeek(c.start)}>
          <span className="tm">{fmtDuration(c.start)}</span>
          <span><b>{i + 1}. {c.title}</b>{(i === idx || i === idx + 1) && c.claim && <span className="cl">{c.claim}</span>}</span>
        </button>
      ))}
    </div>
  )
}

/** 侧栏「转写」标签：跟随播放、术语标虚线；细读时可以加宽 */
function TranscriptPane({ video, chapters, time, onSeek, renderTerms, wide, onWide }: {
  video: Video; chapters: StudyChapter[]; time: number; onSeek: (s: number) => void
  renderTerms: ((text: string) => React.ReactNode) | null; wide: boolean; onWide: (() => void) | null
}) {
  const [autoScroll, setAutoScroll] = useState(() => readPref('vsum.study.autoscroll', true))
  const [markTerms, setMarkTerms] = useState(() => readPref('vsum.study.terms', true))
  const boxRef = useRef<HTMLDivElement>(null)
  const starts = useMemo(() => video.paragraphs.map((p) => p.start), [video.paragraphs])
  const playingStart = video.paragraphs.length ? paragraphAt(starts, time) : null
  useEffect(() => {
    if (!autoScroll || playingStart == null || !boxRef.current) return
    const box = boxRef.current
    const el = box.querySelector<HTMLElement>(`[data-ps="${Math.floor(playingStart)}"]`)
    if (!el) return
    const top = el.offsetTop - 70
    if (Math.abs(box.scrollTop - top) > 40) box.scrollTo({ top, behavior: 'smooth' })
  }, [playingStart, autoScroll, wide])

  return (
    <div className="st-pane sp2-tx" ref={boxRef}>
      <div className="hd">
        {renderTerms && <label><input type="checkbox" checked={markTerms} onChange={(e) => { setMarkTerms(e.target.checked); writePref('vsum.study.terms', e.target.checked) }} /> 标出术语</label>}
        <label><input type="checkbox" checked={autoScroll} onChange={(e) => { setAutoScroll(e.target.checked); writePref('vsum.study.autoscroll', e.target.checked) }} /> 跟随播放</label>
        <span className="sp" />
        {onWide && <button className="lnk" onClick={onWide} title={wide ? '恢复侧栏宽度' : '侧栏加宽，细读原文（视频会让出位置）'}>{wide ? '恢复宽度' : '加宽阅读 ⇔'}</button>}
      </div>
      {video.paragraphs.length === 0 && <div className="st-empty">这条视频没有转写文本。</div>}
      {video.paragraphs.map((p, i) => {
        const chIdx = chapters.findIndex((c) => c.start >= p.start && c.start < p.end + 0.01 && (i === 0 || c.start > video.paragraphs[i - 1].start))
        return (
          <Fragment key={i}>
            {chIdx >= 0 && <div className="st-chapline"><b>第 {chIdx + 1} 章</b>{chapters[chIdx].title}</div>}
            <div className={`st-para${playingStart === p.start ? ' playing' : ''}`} data-ps={Math.floor(p.start)}>
              <button className="tc" onClick={() => onSeek(p.start)} title="从这里开始看">{fmtDuration(p.start)}</button>
              <p>{markTerms && renderTerms ? renderTerms(p.text) : p.text}</p>
            </div>
          </Fragment>
        )
      })}
    </div>
  )
}

// ---------- 播放器 ----------

/** 把上次的音量、静音、倍速套到播放器上（键和 AudioBar 一致） */
function restoreMediaPrefs(el: HTMLMediaElement) {
  try {
    const v = localStorage.getItem('vsum.volume')
    const n = Number(v)
    if (v != null && n >= 0 && n <= 1) el.volume = n
    el.muted = localStorage.getItem('vsum.muted') === '1'
    const r = Number(localStorage.getItem('vsum.rate'))
    if (r >= 0.25 && r <= 4) el.playbackRate = r
  } catch { /* ignore */ }
}

function Player({ video, state, mediaRef, onTimeUpdate, onMeta, onPlayState, mediaErr, setMediaErr, reload }: {
  video: Video; state: StudyState | null; mediaRef: React.MutableRefObject<HTMLMediaElement | null>
  onTimeUpdate: () => void; onMeta: (duration: number, aspect: number | null) => void
  onPlayState: (paused: boolean) => void
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
    onPlay: () => onPlayState(false), onPause: () => onPlayState(true), onEnded: () => onPlayState(true),
    onLoadedMetadata: (e: React.SyntheticEvent<HTMLMediaElement>) => {
      setMediaErr(null)
      const el = e.currentTarget as HTMLVideoElement
      restoreMediaPrefs(el)
      onMeta(el.duration || 0, el.videoHeight ? el.videoWidth / el.videoHeight : null)
    },
    // 音量、静音、倍速：和详情页的播放栏共用一份设置，哪边调了另一边也跟着
    onVolumeChange: (e: React.SyntheticEvent<HTMLMediaElement>) => {
      try {
        localStorage.setItem('vsum.volume', String(e.currentTarget.volume))
        localStorage.setItem('vsum.muted', e.currentTarget.muted ? '1' : '0')
      } catch { /* ignore */ }
    },
    onRateChange: (e: React.SyntheticEvent<HTMLMediaElement>) => {
      try { localStorage.setItem('vsum.rate', String(e.currentTarget.playbackRate)) } catch { /* ignore */ }
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
      {cur >= 0 && chapters[cur] && (
        <span className="lbl" style={{ left: `${Math.min(88, Math.max(12, (chapters[cur].start + chapters[cur].end) / 2 / duration * 100))}%` }}>
          {cur + 1} · {chapters[cur].title}
        </span>
      )}
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
