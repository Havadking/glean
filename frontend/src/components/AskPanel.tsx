import DOMPurify from 'dompurify'
import { Trash2 } from 'lucide-react'
import { marked } from 'marked'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type Question, type QuestionsResponse } from '../api'
import { fmtDuration, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { ErrorBox, Pill } from './ui'

// 回答里的 [03:27] 变成可点的引用；先替换成 HTML 再交给 marked，DOMPurify 保留 data- 属性
const TS_RE = /\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]/g
function citeHtml(answer: string): string {
  const withCites = answer.replace(TS_RE, (_m, a: string, b: string, c?: string) => {
    const secs = c != null ? Number(a) * 3600 + Number(b) * 60 + Number(c) : Number(a) * 60 + Number(b)
    const label = c != null ? `${a}:${b}:${c}` : `${a}:${b}`
    return `<a class="cite" data-t="${secs}" href="#t=${secs}">${label}</a>`
  })
  return DOMPurify.sanitize(marked.parse(withCites, { async: false }) as string)
}

export function AskPanel({ videoId, onJump }: { videoId: string; onJump: (sec: number) => void }) {
  const [data, setData] = useState<QuestionsResponse | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [q, setQ] = useState('')
  const [asking, setAsking] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => api.questions(videoId).then(setData).catch(setErr), [videoId])
  useEffect(() => { setData(null); setErr(null); void load() }, [load])
  useEffect(() => { inputRef.current?.focus() }, [data?.questions.length])

  const ask = async () => {
    const question = q.trim()
    if (!question || asking) return
    setAsking(true); setErr(null)
    try {
      const history = (data?.questions ?? []).slice(-4).map((t) => ({ question: t.question, answer: t.answer }))
      const a = await api.ask(videoId, question, history)
      setData((d) => d ? { ...d, questions: [...d.questions, a] } : d)
      setQ('')
      requestAnimationFrame(() => listRef.current?.lastElementChild?.scrollIntoView({ block: 'nearest' }))
    } catch (e) { setErr(e) } finally { setAsking(false) }
  }

  const remove = async (id: number) => {
    try {
      await api.deleteQuestion(id)
      setData((d) => d ? { ...d, questions: d.questions.filter((x) => x.id !== id) } : d)
    } catch (e) { setErr(e) }
  }

  const onClickAnswer = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest('a.cite') as HTMLAnchorElement | null
    if (!a) return
    e.preventDefault()
    onJump(Number(a.dataset.t))
  }

  const est = data?.estimate
  const money = est && !('error' in est) ? fmtMoney(est.cost, est.currency) : null

  return (
    <div className="qa">
      <div className="status">
        {est && 'error' in est
          ? <Pill tone="bad">{est.error}</Pill>
          : <>
              <span>只依据这条转写回答，每条结论附时间戳，点了跳原文</span>
              {est && <span className="mono">每问约 {money ?? `${fmtTokens(est.input_tokens)} tokens`}</span>}
              {est?.truncated && <Pill tone="warn">转写太长，只带了前半部分</Pill>}
            </>}
      </div>

      <ErrorBox error={err} />

      <div className="qa-list" ref={listRef}>
        {data && data.questions.length === 0 && !asking && (
          <div className="qa-empty">还没问过。试试「她推荐的喷雾是哪个牌子」「有哪些避坑的点」。</div>
        )}
        {data?.questions.map((t: Question) => (
          <div className="turn" key={t.id}>
            <div className="qq">
              <span>{t.question}</span>
              <button className="iconbtn danger" title="删除这条" onClick={() => remove(t.id)}><Trash2 /></button>
            </div>
            <div className="aa" onClick={onClickAnswer} dangerouslySetInnerHTML={{ __html: citeHtml(t.answer) }} />
            <div className="af">
              {t.citations.length > 0
                ? <span>引用 {t.citations.map((c) => (
                    <button className="cite" key={String(c)} onClick={() => onJump(Number(c))}>{fmtDuration(Number(c))}</button>
                  ))}</span>
                : <span className="warnish">没有引用时间戳——回答可能不在转写里</span>}
              <span className="sp" />
              <span>{fmtWhen(t.created_at)}</span>
            </div>
          </div>
        ))}
        {asking && <div className="turn"><div className="qq"><span>{q.trim()}</span></div><div className="aa thinking"><span className="spin" /> 在读转写…</div></div>}
      </div>

      <form className="ask" onSubmit={(e) => { e.preventDefault(); void ask() }}>
        <div className="box">
          <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder="问这条视频…" disabled={asking} aria-label="问视频"
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); void ask() } }} />
          <button className="btn primary sm" type="submit" disabled={!q.trim() || asking}>{asking ? '…' : '问'}</button>
        </div>
      </form>
    </div>
  )
}

/** 引用落在哪一段：起始时间不超过 sec 的最后一段的起始时间 */
export function paragraphAt(paragraphStarts: number[], sec: number): number {
  let target = paragraphStarts[0] ?? 0
  for (const s of paragraphStarts) { if (s <= sec) target = s; else break }
  return target
}

/** 点时间戳：滚到那一段并闪一下；有本地音频时（给了 seek）再从那一秒开始播 */
export function useCitationJump(paragraphStarts: number[], seek?: (sec: number) => void) {
  return useMemo(() => (sec: number) => {
    seek?.(sec)
    const el = document.getElementById(`p-${Math.floor(paragraphAt(paragraphStarts, sec))}`)
    if (!el) return
    el.scrollIntoView({ block: 'center', behavior: 'smooth' })
    el.classList.remove('focus')
    void el.offsetWidth
    el.classList.add('focus')
    setTimeout(() => el.classList.remove('focus'), 2700)
  }, [paragraphStarts, seek])
}
