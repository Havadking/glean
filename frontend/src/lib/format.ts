export function fmtDuration(sec: number): string {
  const s = Math.max(0, Math.round(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const mm = h ? String(m).padStart(2, '0') : String(m).padStart(2, '0')
  return (h ? `${h}:` : '') + `${mm}:${String(r).padStart(2, '0')}`
}

export function fmtMinutes(sec: number): string {
  return String(Math.round(sec / 60))
}

/** ISO 时间 → "09-12 22:30"，本地时区 */
export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function fmtMoney(cost: number | null | undefined, currency = '¥'): string | null {
  if (cost == null) return null
  return `${currency}${cost < 0.01 ? cost.toFixed(3) : cost.toFixed(2)}`
}

export function fmtTokens(n: number | undefined): string {
  if (n == null) return ''
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

export function fmtBytes(n: number): string {
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(0)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

export function fmtSeconds(sec: number): string {
  if (sec < 60) return `${Math.round(sec)} 秒`
  return `${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒`
}
