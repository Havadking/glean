import { useEffect, useReducer } from 'react'
import { api, subscribeJob, type Job, type JobEvent, type JobStatus } from '../api'

export interface StageRecord { name: string; detail: string; startedAt: number; endedAt?: number }
export interface LogLine { ts: string; level: string; message: string }

export interface JobView {
  id: string
  title: string
  status: JobStatus
  stages: StageRecord[]
  progress: number | null
  logs: LogLine[]
  error: string | null
  result: Record<string, unknown> | null
  elapsed: number | null
  lastSeq: number
}

type Action = { type: 'reset'; job: Job | null } | { type: 'event'; ev: JobEvent }

function empty(id: string): JobView {
  return { id, title: '', status: 'queued', stages: [], progress: null, logs: [], error: null, result: null, elapsed: null, lastSeq: 0 }
}

function apply(v: JobView, ev: JobEvent): JobView {
  if (ev.seq <= v.lastSeq) return v
  const t = Date.parse(ev.ts)
  const next = { ...v, lastSeq: ev.seq }
  switch (ev.kind) {
    case 'stage': {
      const stages = v.stages.map((s) => (s.endedAt ? s : { ...s, endedAt: t }))
      stages.push({ name: ev.stage!, detail: ev.detail ?? '', startedAt: t })
      return { ...next, stages, progress: null }
    }
    case 'progress':
      return { ...next, progress: ev.progress ?? null }
    case 'log':
      return { ...next, logs: [...v.logs, { ts: ev.ts, level: ev.level ?? 'info', message: ev.message ?? '' }] }
    case 'status': {
      const status = ev.status ?? v.status
      const finished = ['done', 'failed', 'cancelled'].includes(status)
      const stages = finished ? v.stages.map((s) => (s.endedAt ? s : { ...s, endedAt: t })) : v.stages
      return {
        ...next, status, stages,
        error: ev.error ?? v.error,
        result: ev.result ?? v.result,
        elapsed: ev.elapsed_sec ?? v.elapsed,
        progress: status === 'done' ? 1 : v.progress,
      }
    }
  }
}

function reducer(v: JobView, a: Action): JobView {
  if (a.type === 'reset') {
    if (!a.job) return empty('')
    let view = { ...empty(a.job.id), title: a.job.title, status: a.job.status, error: a.job.error, result: a.job.result }
    for (const ev of a.job.events ?? []) view = apply(view, ev)
    return view
  }
  return apply(v, a.ev)
}

/** 订阅一个任务：先拉一次全量（含历史事件），再走 SSE 续上。 */
export function useJob(jobId: string | null): JobView | null {
  const [view, dispatch] = useReducer(reducer, null, () => empty(''))

  useEffect(() => {
    if (!jobId) { dispatch({ type: 'reset', job: null }); return }
    let stop: (() => void) | null = null
    let cancelled = false
    api.job(jobId).then((job) => {
      if (cancelled) return
      dispatch({ type: 'reset', job })
      const done = ['done', 'failed', 'cancelled'].includes(job.status)
      if (!done) {
        const last = job.events?.length ? job.events[job.events.length - 1].seq : 0
        stop = subscribeJob(jobId, (ev) => dispatch({ type: 'event', ev }), undefined, last)
      }
    }).catch(() => dispatch({ type: 'reset', job: null }))
    return () => { cancelled = true; stop?.() }
  }, [jobId])

  return jobId && view.id ? view : null
}
