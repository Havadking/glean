import type { ReactNode } from 'react'

export function Pill({ tone = 'neutral', dot, children }: { tone?: 'ok' | 'warn' | 'info' | 'bad' | 'neutral' | 'accent'; dot?: boolean; children: ReactNode }) {
  return <span className={`pill ${tone}`}>{dot && <i className="dot" />}{children}</span>
}

export function Seg<T extends string>({ value, onChange, items }: {
  value: T
  onChange: (v: T) => void
  items: { key: T; label: string; done?: boolean; showDot?: boolean }[]
}) {
  return (
    <div className="seg" role="tablist">
      {items.map((it) => (
        <button key={it.key} role="tab" aria-pressed={value === it.key} onClick={() => onChange(it.key)}>
          {it.showDot && <span className={`st${it.done ? ' done' : ''}`} />}
          {it.label}
        </button>
      ))}
    </div>
  )
}

export function Stats({ items }: { items: { k: string; v: ReactNode; small?: ReactNode; mono?: boolean }[] }) {
  return (
    <div className="stats">
      {items.map((it) => (
        <div className="card stat" key={it.k}>
          <div className="k">{it.k}</div>
          <div className={`v${it.mono ? ' mono' : ''}`}>{it.v}{it.small && <small>{it.small}</small>}</div>
        </div>
      ))}
    </div>
  )
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null
  const msg = error instanceof Error ? error.message : String(error)
  return <div className="errbox">{msg}</div>
}

export function Avatar({ name, grey }: { name: string | null; grey?: boolean }) {
  const ch = (name ?? '?').trim().slice(0, 1).toUpperCase() || '?'
  return <span className={`av${grey || !name ? ' g' : ''}`}>{ch}</span>
}

/** 把 query 在文本里高亮。不区分大小写；空 query 原样返回。 */
export function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim()
  if (!q) return <>{text}</>
  const parts: ReactNode[] = []
  const lower = text.toLowerCase()
  const ql = q.toLowerCase()
  let i = 0
  let k = 0
  while (i < text.length) {
    const j = lower.indexOf(ql, i)
    if (j < 0) { parts.push(text.slice(i)); break }
    if (j > i) parts.push(text.slice(i, j))
    parts.push(<mark key={k++}>{text.slice(j, j + q.length)}</mark>)
    i = j + q.length
  }
  return <>{parts}</>
}

export function countHits(text: string, query: string): number {
  const q = query.trim().toLowerCase()
  if (!q) return 0
  let n = 0
  let i = 0
  const lower = text.toLowerCase()
  for (;;) {
    const j = lower.indexOf(q, i)
    if (j < 0) return n
    n += 1
    i = j + q.length
  }
}
