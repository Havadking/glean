import { ChevronDown, ChevronUp, RotateCcw, SpellCheck2 } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { api, type Video } from '../api'
import { useJob } from '../hooks/useJob'
import { ErrorBox } from './ui'

/**
 * 纠专有名词：转写栏头上的一枚 chip + 展开在段落上方的替换表。
 *
 * 只对语音识别来源显示。没跑过的给一个按钮；跑过的显示"已修正 N 处"，
 * 展开能看每一条（原文 → 改后 · 命中次数 · 模型的依据）并逐条否决 / 恢复。
 * 否决即时生效：阅读视图、搜索索引、总结缓存 key 都跟着变，所以每次都让父组件重读。
 *
 * chip 和表在页面上不挨着（chip 在按钮排里，表要占整栏宽），所以做成 hook 返回两块 JSX。
 */
export function useCorrections(video: Video | null, onChange: () => Promise<void>): { chip: ReactNode; panel: ReactNode } {
  const [open, setOpen] = useState(false)
  const [jobId, setJobId] = useState<string | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const job = useJob(jobId)
  const running = job != null && !['done', 'failed', 'cancelled'].includes(job.status)

  useEffect(() => {
    if (job?.status === 'done') { setJobId(null); setOpen(true); void onChange() }
    if (job?.status === 'failed') setErr(new Error(job.error ?? '纠错失败'))
  }, [job?.status, job?.error, onChange])

  if (!video || video.source_type !== 'asr') return { chip: null, panel: null }
  const c = video.corrections

  const run = async () => {
    setErr(null)
    try {
      const r = await api.runCorrections(video.video_id)
      setJobId(r.job.id)
    } catch (e) { setErr(e) }
  }
  const setState = async (index: number, state: 'applied' | 'rejected') => {
    setBusy(index); setErr(null)
    try {
      await api.setCorrectionState(video.video_id, index, state)
      await onChange()
    } catch (e) { setErr(e) } finally { setBusy(null) }
  }

  const runBtn = (label: string) => (
    <button className="btn ghost sm" onClick={run} disabled={running}
      title="让模型把全文看一遍，只出一张专有名词替换表叠在原文上。原文不动，每条都能否决。1 小时约 3 分钱">
      {running ? <><span className="spin" /> {job?.status === 'queued' ? '排队中' : '纠错中…'}</> : <><SpellCheck2 /> {label}</>}
    </button>
  )

  if (!c) {
    return { chip: runBtn('纠专有名词'), panel: err != null && <ErrorBox error={err} /> }
  }

  const active = c.items.filter((i) => i.state === 'applied')
  const label = active.length === 0
    ? (c.items.length === 0 ? '没有要纠的专有名词' : `已否决全部 ${c.items.length} 条纠错`)
    : `已修正 ${c.applied_hits} 处`

  const chip = (
    <button className={`btn sm${open ? ' primary' : active.length ? '' : ' ghost'}`} onClick={() => setOpen((o) => !o)}
      title={open ? '收起替换表' : `${c.items.length} 条替换，点开看每一条`}>
      <SpellCheck2 /> {label} {open ? <ChevronUp /> : <ChevronDown />}
    </button>
  )
  const panel = (
    <>
      {open && (
        <div className="corrections">
          <div className="ch">
            <span>模型 {c.provider ?? '?'} 给出的替换表。原文没动，去掉勾的那条就不再应用。</span>
            {runBtn('重新纠错')}
          </div>
          {c.items.length === 0 && <div className="none">模型没找到要改的专有名词。</div>}
          {c.items.map((it) => {
            const on = it.state === 'applied'
            return (
              <label className={`ci${on ? '' : ' off'}`} key={it.index}>
                <input type="checkbox" checked={on} disabled={busy === it.index}
                  onChange={() => setState(it.index, on ? 'rejected' : 'applied')} />
                <span className="pair"><del>{it.from}</del><span className="arr">→</span><ins>{it.to}</ins></span>
                <span className="n mono">×{it.hits}</span>
                {it.why && <span className="why">{it.why}</span>}
                {!on && <span className="why" title="点勾恢复"><RotateCcw /> 已否决</span>}
              </label>
            )
          })}
          {err != null && <ErrorBox error={err} />}
        </div>
      )}
      {!open && err != null && <ErrorBox error={err} />}
    </>
  )
  return { chip, panel }
}
