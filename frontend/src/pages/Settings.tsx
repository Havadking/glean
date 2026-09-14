import { useEffect, useState } from 'react'
import { api, type PricingConfig, type Storage, type TestLlmResult, type Usage } from '../api'
import { fmtBytes, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { useStore } from '../store'

export function Settings() {
  const { meta, theme, setTheme, library, refreshMeta, refreshLibrary } = useStore()
  const [usage, setUsage] = useState<Usage | null>(null)
  const [storage, setStorage] = useState<Storage | null>(null)
  const [clearing, setClearing] = useState(false)
  const [cleared, setCleared] = useState<string | null>(null)

  // 价格配置
  const [pricing, setPricing] = useState<PricingConfig | null>(null)
  const [inputPrice, setInputPrice] = useState<string>('')
  const [outputPrice, setOutputPrice] = useState<string>('')
  const [currency, setCurrency] = useState<string>('¥')
  const [savingPrice, setSavingPrice] = useState(false)
  const [priceNotice, setPriceNotice] = useState<string | null>(null)
  const [testingLlm, setTestingLlm] = useState(false)
  const [testResult, setTestResult] = useState<TestLlmResult | null>(null)

  useEffect(() => {
    api.usage(30).then(setUsage).catch(() => setUsage(null))
    api.storage().then(setStorage).catch(() => setStorage(null))
    api.getPricing().then((p) => {
      setPricing(p)
      setInputPrice(p.price_input_per_m != null ? String(p.price_input_per_m) : '')
      setOutputPrice(p.price_output_per_m != null ? String(p.price_output_per_m) : '')
      setCurrency(p.currency || '¥')
    }).catch(() => {})
  }, [])

  const clearAudio = async () => {
    if (!storage || !confirm(`删掉 ${storage.audio_dirs} 个视频的音频缓存（${fmtBytes(storage.audio_bytes)}）？转写和总结都在，只有强制重跑识别时才需要重新下载。`)) return
    setClearing(true)
    try {
      const r = await api.clearAudio()
      setCleared(`已释放 ${fmtBytes(r.freed_bytes)}`)
      setStorage(await api.storage())
    } catch { setCleared('删除失败') } finally { setClearing(false) }
  }

  const applyPreset = (inP: number | null, outP: number | null, curr = '¥') => {
    setInputPrice(inP != null ? String(inP) : '')
    setOutputPrice(outP != null ? String(outP) : '')
    setCurrency(curr)
  }

  const savePricing = async () => {
    setSavingPrice(true)
    setPriceNotice(null)
    try {
      const inVal = inputPrice.trim() === '' ? null : Number(inputPrice)
      const outVal = outputPrice.trim() === '' ? null : Number(outputPrice)
      if (inVal != null && (isNaN(inVal) || inVal < 0)) throw new Error('输入单价必须为非负数')
      if (outVal != null && (isNaN(outVal) || outVal < 0)) throw new Error('输出单价必须为非负数')
      const updated = await api.updatePricing({
        price_input_per_m: inVal,
        price_output_per_m: outVal,
        currency: currency.trim() || '¥',
      })
      setPricing(updated)
      setPriceNotice('价格配置已保存并即时生效')
      await refreshMeta()
      await refreshLibrary()
      setTimeout(() => setPriceNotice(null), 3000)
    } catch (e: unknown) {
      const err = e as { message?: string }
      setPriceNotice(`保存失败: ${err.message || String(e)}`)
    } finally {
      setSavingPrice(false)
    }
  }

  const testLlmConnection = async () => {
    setTestingLlm(true)
    setTestResult(null)
    try {
      const res = await api.testLlm()
      setTestResult(res)
    } catch (e: unknown) {
      const err = e as { message?: string }
      setTestResult({ ok: false, latency_ms: 0, error: err.message || String(e) })
    } finally {
      setTestingLlm(false)
    }
  }

  // 模拟单价花费估算
  const parsedIn = parseFloat(inputPrice) || 0
  const parsedOut = parseFloat(outputPrice) || 0
  const hasPrice = inputPrice.trim() !== '' || outputPrice.trim() !== ''
  const estCost = (inTok: number, outTok: number) => {
    if (!hasPrice) return null
    return fmtMoney((inTok * parsedIn + outTok * parsedOut) / 1e6, currency)
  }

  const titles = new Map((library?.groups ?? []).flatMap((g) => g.entries).map((e) => [e.video_id, e.title]))
  const kindLabel = (k: string) => ({ summary: '总结', qa: '问视频', uploader_qa: '问 UP 主' }[k] ?? k)

  return (
    <div className="page">
      <div className="ph">
        <div>
          <h1>设置</h1>
          <p>模型、识别、价格都在项目目录的 <code className="mono">config.yaml</code> 里管理，支持在线即时配置。</p>
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

      {/* 模型价格与计费换算 */}
      <h2 style={{ fontSize: 15, margin: '26px 0 10px' }}>模型计费与价格设置</h2>
      <div className="card" style={{ padding: '20px 24px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14, flexWrap: 'wrap', gap: 10 }}>
          <div style={{ fontSize: 13, color: 'var(--mute)' }}>
            当前总结服务：<b style={{ color: 'var(--ink)' }}>{pricing?.model || meta?.provider || 'DeepSeek'}</b>（修改后全站即时生效，自动保存回 config.yaml）
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <button className="btn sm" onClick={testLlmConnection} disabled={testingLlm}>
              {testingLlm ? '测试中…' : '测试 API 连接'}
            </button>
            {testResult && (
              <span className={`pill ${testResult.ok ? 'ok' : 'bad'}`}>
                {testResult.ok ? `正常 (${testResult.latency_ms}ms)` : `连接失败: ${testResult.error || '错误'}`}
              </span>
            )}
          </div>
        </div>

        {/* 快捷预设 */}
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 18, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12.5, color: 'var(--mute)' }}>快捷预设：</span>
          <button className="btn sm ghost" onClick={() => applyPreset(2.0, 3.0, '¥')}>DeepSeek 标准 (¥2 / ¥3)</button>
          <button className="btn sm ghost" onClick={() => applyPreset(0.5, 3.0, '¥')}>DeepSeek 缓存优惠 (¥0.5 / ¥3)</button>
          <button className="btn sm ghost" onClick={() => applyPreset(0.8, 2.0, '¥')}>通义 Qwen-Plus (¥0.8 / ¥2)</button>
          <button className="btn sm ghost" onClick={() => applyPreset(null, null, '¥')}>清空（仅显示 Token）</button>
        </div>

        {/* 价格输入表单 */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 16, marginBottom: 18 }}>
          <div>
            <label style={{ display: 'block', fontSize: 12.5, color: 'var(--mute)', marginBottom: 6 }}>
              每百万输入 Token 单价 ({currency} / M Tokens)
            </label>
            <input
              type="number"
              step="0.1"
              min="0"
              value={inputPrice}
              onChange={(e) => setInputPrice(e.target.value)}
              placeholder="例如 2.0 (留空不换算)"
              style={{ width: '100%', height: 34, padding: '0 10px', borderRadius: 6, border: '1px solid var(--line)', background: 'var(--surface)' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: 12.5, color: 'var(--mute)', marginBottom: 6 }}>
              每百万输出 Token 单价 ({currency} / M Tokens)
            </label>
            <input
              type="number"
              step="0.1"
              min="0"
              value={outputPrice}
              onChange={(e) => setOutputPrice(e.target.value)}
              placeholder="例如 3.0 (留空不换算)"
              style={{ width: '100%', height: 34, padding: '0 10px', borderRadius: 6, border: '1px solid var(--line)', background: 'var(--surface)' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: 12.5, color: 'var(--mute)', marginBottom: 6 }}>
              货币符号
            </label>
            <select
              value={currency}
              onChange={(e) => setCurrency(e.target.value)}
              style={{ width: '100%', height: 34, padding: '0 10px', borderRadius: 6, border: '1px solid var(--line)', background: 'var(--surface)' }}
            >
              <option value="¥">人民币 (¥)</option>
              <option value="$">美元 ($)</option>
            </select>
          </div>
        </div>

        {/* 实时估算参考对照卡 */}
        <div style={{ background: 'var(--surface-2)', padding: '12px 16px', borderRadius: 8, border: '1px solid var(--line-2)', marginBottom: 18, fontSize: 12.5 }}>
          <div style={{ color: 'var(--mute)', marginBottom: 6 }}>
            <b>实时成本参考</b>（中文视频口语独白按 ~3.2 tokens/秒，输出约 1,200 tokens 计算）：
          </div>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }} className="mono">
            <span>5 分钟 (~1k 入 / 1.2k 出): <b>{estCost(1000, 1200) ?? '—'}</b></span>
            <span>15 分钟 (~3k 入 / 1.2k 出): <b>{estCost(3000, 1200) ?? '—'}</b></span>
            <span>30 分钟 (~6k 入 / 1.2k 出): <b>{estCost(6000, 1200) ?? '—'}</b></span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button className="btn primary" onClick={savePricing} disabled={savingPrice}>
            {savingPrice ? '保存中…' : '保存价格设置'}
          </button>
          {priceNotice && (
            <span style={{ fontSize: 13, color: priceNotice.includes('失败') ? 'var(--bad)' : 'var(--ok)' }}>
              {priceNotice}
            </span>
          )}
        </div>
      </div>

      {storage && (
        <>
          <h2 style={{ fontSize: 15, margin: '26px 0 10px' }}>存储</h2>
          <div className="card">
            <dl className="kv">
              <dt>音频缓存</dt>
              <dd style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <span className="mono">{fmtBytes(storage.audio_bytes)}</span>
                <span style={{ color: 'var(--mute)' }}>{storage.audio_dirs} 个视频。识别完就用不上了，删掉不影响转写和总结</span>
                <button className="btn sm" onClick={clearAudio} disabled={clearing || storage.audio_bytes === 0}>{clearing ? '删除中…' : '清理音频缓存'}</button>
                {cleared && <span style={{ color: 'var(--ok)', fontSize: 12.5 }}>{cleared}</span>}
              </dd>
              <dt>转写和总结</dt><dd className="mono">{fmtBytes(storage.other_bytes)}</dd>
              <dt>缓存库</dt><dd className="mono">{fmtBytes(storage.cache_bytes)}</dd>
            </dl>
          </div>
        </>
      )}

      {usage && (
        <>
          <h2 style={{ fontSize: 15, margin: '26px 0 10px' }}>花费</h2>
          <div className="stats" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
            <div className="card stat"><div className="k">本月</div><div className="v mono">{fmtMoney(usage.month.cost, usage.currency) ?? '—'}<small>{usage.month.calls} 次</small></div></div>
            <div className="card stat"><div className="k">累计</div><div className="v mono">{fmtMoney(usage.total.cost, usage.currency) ?? '—'}<small>{usage.total.calls} 次</small></div></div>
            <div className="card stat"><div className="k">累计 token</div><div className="v mono">{fmtTokens(usage.total.input_tokens)}<small>入 · {fmtTokens(usage.total.output_tokens)} 出</small></div></div>
          </div>
          {!usage.priced && <p style={{ color: 'var(--mute)', fontSize: 12.5 }}>没配单价，只记 token。在上面单价设置中填入价格就会换算成钱。</p>}
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
