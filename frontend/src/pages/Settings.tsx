import { useEffect, useState } from 'react'
import { api, type Usage } from '../api'
import { fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { useStore } from '../store'

export function Settings() {
  const { meta, theme, setTheme, library } = useStore()
  const [usage, setUsage] = useState<Usage | null>(null)
  useEffect(() => { api.usage(30).then(setUsage).catch(() => setUsage(null)) }, [])
  const titles = new Map((library?.groups ?? []).flatMap((g) => g.entries).map((e) => [e.video_id, e.title]))
  const kindLabel = (k: string) => ({ summary: '总结', qa: '问视频', uploader_qa: '问 UP 主' }[k] ?? k)
  return (
    <div className="page">
      <div className="ph">
        <div>
          <h1>设置</h1>
          <p>模型、识别、价格都在项目目录的 <code className="mono">config.yaml</code> 里改，改完不用重启。</p>
        </div>
      </div>
      <div className="card">
        <dl className="kv">
          <dt>外观</dt>
          <dd>
            <div className="seg">
              {(['system', 'light', 'dark'] as const).map((t) => (
                <button key={t} aria-pressed={theme === t} onClick={() => setTheme(t)}>{{ system: '跟随系统', light: '浅色', dark: '深色' }[t]}</button>
              ))}
            </div>
          </dd>
          <dt>总结模型</dt><dd>{meta?.provider ?? '—'}</dd>
          <dt>识别模型</dt><dd>{meta?.asr_default ?? '—'}{meta ? `（说话人分离：${String(meta.diarize_default)}）` : ''}</dd>
          <dt>默认总结类型</dt><dd>{meta?.summary_types.find((t) => t.key === meta.default_summary_type)?.label ?? meta?.default_summary_type ?? '—'}</dd>
          <dt>价格换算</dt><dd>{meta?.priced ? '已配置，界面会显示预计花费' : '未配置，只显示 token 数'}</dd>
          <dt>产物目录</dt><dd className="mono">{meta?.output_dir ?? '—'}</dd>
          <dt>版本</dt><dd>{meta?.app} {meta?.version}</dd>
        </dl>
      </div>

      {usage && (
        <>
          <h2 style={{ fontSize: 15, margin: '26px 0 10px' }}>花费</h2>
          <div className="stats" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
            <div className="card stat"><div className="k">本月</div><div className="v mono">{fmtMoney(usage.month.cost, usage.currency) ?? '—'}<small>{usage.month.calls} 次</small></div></div>
            <div className="card stat"><div className="k">累计</div><div className="v mono">{fmtMoney(usage.total.cost, usage.currency) ?? '—'}<small>{usage.total.calls} 次</small></div></div>
            <div className="card stat"><div className="k">累计 token</div><div className="v mono">{fmtTokens(usage.total.input_tokens)}<small>入 · {fmtTokens(usage.total.output_tokens)} 出</small></div></div>
          </div>
          {!usage.priced && <p style={{ color: 'var(--mute)', fontSize: 12.5 }}>没配单价，只记 token。在 config.yaml 的 summarizer 下填 price_input_per_m / price_output_per_m 就会换算成钱。</p>}
          {usage.recent.length > 0 && (
            <div className="card">
              {usage.recent.map((r, i) => (
                <div className="urow" key={i}>
                  <span className="k">{kindLabel(r.kind)}</span>
                  <span className="t" title={r.detail ?? ''}>{titles.get(r.video_id) ?? r.video_id}{r.kind === 'qa' && r.detail ? ` · ${r.detail}` : ''}</span>
                  <span className="mono n">{fmtTokens(r.input_tokens)} / {fmtTokens(r.output_tokens)}</span>
                  <span className="mono n">{fmtMoney(r.cost, usage.currency) ?? ''}</span>
                  <span className="w">{fmtWhen(r.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
