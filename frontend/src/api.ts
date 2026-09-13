// 后端接口的类型和调用。所有路径都在 /api 下，开发时由 vite 代理到 7860。

export interface SummaryType { key: string; label: string; hint: string }
export interface AsrChoice { value: string; label: string; note: string }

export interface Meta {
  app: string
  version: string
  summary_types: SummaryType[]
  default_summary_type: string
  asr_choices: AsrChoice[]
  asr_default: string
  diarize_default: string | boolean
  provider: string
  currency: string
  priced: boolean
  output_dir: string
}

export interface SummaryRef { type: string; label: string; provider: string; created_at: string }

export interface Entry {
  video_id: string
  title: string
  source_url: string
  extractor: string | null
  uploader: string | null
  upload_date: string | null
  thumbnail: string | null
  duration_sec: number
  source_type: 'subtitle' | 'asr'
  source_label: string
  asr_model: string | null
  diarized: boolean
  language: string | null
  segment_count: number
  speaker_count: number
  created_at: string
  work_dir: string | null
  summaries: SummaryRef[]
}

export interface LibraryStats { videos: number; duration_sec: number; summaries: number; mindmaps: number }
export interface LibraryGroup { uploader: string | null; entries: Entry[] }
export interface Library { stats: LibraryStats; groups: LibraryGroup[] }

export interface Paragraph { start: number; end: number; text: string; speaker: string | null }
export interface MindmapNode { content: string; children: MindmapNode[] }
export interface Summary {
  type: string
  label: string
  provider: string
  created_at: string
  content: string
  source: 'cache' | 'file'
  tree?: MindmapNode
}
export interface Video extends Omit<Entry, 'summaries'> {
  meta: Record<string, unknown>
  paragraphs: Paragraph[]
  summaries: Record<string, Summary>
}

export interface Estimate {
  provider?: string
  transcript_tokens?: number
  input_tokens: number
  output_tokens: number
  calls?: number
  strategy?: string
  cost: number | null
  currency: string
  asr_sec?: number
}

export interface Probe {
  url: string
  video_id: string
  title: string
  uploader: string | null
  upload_date: string | null
  thumbnail: string | null
  duration_sec: number
  extractor: string
  subtitle: { language: string; auto: boolean } | null
  transcript_cached: boolean
  already_in_library: boolean
  summaries_done: string[]
  estimate: Estimate
}

export interface SearchHit { video_id: string; kind: 'transcript' | 'summary'; ref: string; start: number; snippet: string }
export interface SearchVideo { video_id: string; title: string; uploader: string | null; thumbnail: string | null; duration_sec: number; hits: SearchHit[] }
export interface SearchResult { query: string; videos: SearchVideo[]; transcript_hits: number; summary_hits: number }

export interface Question {
  id: number
  question: string
  answer: string
  provider: string
  citations: number[]
  created_at: string
  cost?: number | null
  truncated?: boolean
}
export interface QuestionsResponse {
  estimate: { transcript_tokens: number; input_tokens: number; truncated: boolean; cost: number | null; currency: string; provider: string } | { error: string }
  questions: Question[]
}

export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
export interface Job {
  id: string
  kind: 'process' | 'summarize'
  title: string
  params: Record<string, unknown>
  status: JobStatus
  stage: string | null
  stage_detail: string
  progress: number | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  result: Record<string, unknown> | null
  events?: JobEvent[]
}
export interface JobEvent {
  seq: number
  kind: 'stage' | 'progress' | 'log' | 'status'
  ts: string
  stage?: string
  detail?: string
  progress?: number
  level?: string
  message?: string
  status?: JobStatus
  error?: string | null
  elapsed_sec?: number
  result?: Record<string, unknown> | null
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!res.ok) {
    let msg = res.statusText
    try {
      const body = await res.json()
      msg = body.error ?? body.detail ?? msg
      if (typeof msg !== 'string') msg = JSON.stringify(msg)
    } catch { /* 不是 JSON 就用状态文本 */ }
    throw new ApiError(res.status, msg)
  }
  return res.json() as Promise<T>
}

const post = <T,>(path: string, body: unknown) =>
  call<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  meta: () => call<Meta>('/meta'),
  library: () => call<Library>('/library'),
  video: (id: string) => call<Video>(`/videos/${encodeURIComponent(id)}`),
  estimate: (id: string, type: string) =>
    call<Estimate>(`/videos/${encodeURIComponent(id)}/estimate?type=${encodeURIComponent(type)}`),
  deleteVideo: (id: string) => call<{ removed_dir: boolean }>(`/videos/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  openFolder: (id: string) => post<{ ok: boolean }>(`/videos/${encodeURIComponent(id)}/open`, {}),
  probe: (url: string) => post<Probe>('/probe', { url }),
  createJob: (body: { url: string; asr_model?: string | null; diarize?: string | boolean | null; summary_type?: string | null; force?: boolean; force_asr?: boolean }) =>
    post<{ job: Job; duplicate: boolean }>('/jobs', body),
  summarize: (id: string, body: { type: string; language?: string; extra?: string | null; force?: boolean }) =>
    post<{ job: Job | null; duplicate: boolean; cached: boolean; summary?: { type: string; content: string; provider: string } }>(
      `/videos/${encodeURIComponent(id)}/summaries`, body),
  questions: (id: string) => call<QuestionsResponse>(`/videos/${encodeURIComponent(id)}/questions`),
  ask: (id: string, question: string, history: { question: string; answer: string }[]) =>
    post<Question & { created_at: string }>(`/videos/${encodeURIComponent(id)}/ask`, { question, history }),
  deleteQuestion: (id: number) => call<{ deleted: boolean }>(`/questions/${id}`, { method: 'DELETE' }),
  search: (q: string, signal?: AbortSignal) => call<SearchResult>(`/search?q=${encodeURIComponent(q)}`, { signal }),
  jobs: () => call<{ jobs: Job[] }>('/jobs'),
  job: (id: string) => call<Job>(`/jobs/${id}`),
  cancelJob: (id: string) => call<{ cancelled: boolean }>(`/jobs/${id}`, { method: 'DELETE' }),
}

/** 订阅任务事件。返回取消函数。状态到终态后自动关闭。 */
export function subscribeJob(
  jobId: string,
  onEvent: (ev: JobEvent) => void,
  onClose?: () => void,
  after = 0,
): () => void {
  const es = new EventSource(`/api/jobs/${jobId}/events?after=${after}`)
  const handler = (e: MessageEvent) => {
    const ev = JSON.parse(e.data) as JobEvent
    onEvent(ev)
    if (ev.kind === 'status' && ev.status && ['done', 'failed', 'cancelled'].includes(ev.status)) {
      es.close()
      onClose?.()
    }
  }
  for (const kind of ['stage', 'progress', 'log', 'status']) es.addEventListener(kind, handler as EventListener)
  es.onerror = () => { /* 浏览器会自动重连，带 Last-Event-ID */ }
  return () => es.close()
}
