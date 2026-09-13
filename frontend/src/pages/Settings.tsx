import { useStore } from '../store'

export function Settings() {
  const { meta, theme, setTheme } = useStore()
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
    </div>
  )
}
