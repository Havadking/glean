import { Check, Loader2, Pencil, Plus, Sparkles, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import {
  api,
  displayTitle,
  type AsrConfig,
  type Storage,
  type TestLlmResult,
  type Usage,
} from '../api'
import { fmtBytes, fmtMoney, fmtTokens, fmtWhen } from '../lib/format'
import { useStore } from '../store'

export interface SavedModel {
  id: string
  name: string // 界面显示名称，如 "gemini 3.8 flash", "DeepSeek v4.1"
  modelId: string // 实际发给 API 的模型标识，如 "deepseek-chat", "gemini-1.5-flash"
  baseUrl: string
  provider: string
  apiKey?: string
  apiKeyEnv: string
}

const TEMPLATES = [
  {
    label: 'Google Gemini',
    name: 'Google Gemini 1.5 Flash',
    modelId: 'gemini-1.5-flash',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai/',
    apiKeyEnv: 'GEMINI_API_KEY',
  },
  {
    label: 'DeepSeek',
    name: 'DeepSeek V3',
    modelId: 'deepseek-chat',
    baseUrl: 'https://api.deepseek.com/v1',
    apiKeyEnv: 'DEEPSEEK_API_KEY',
  },
  {
    label: '通义千问',
    name: '通义千问 Qwen-Plus',
    modelId: 'qwen-plus',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    apiKeyEnv: 'DASHSCOPE_API_KEY',
  },
  {
    label: '月之暗面 Kimi',
    name: '月之暗面 Kimi',
    modelId: 'moonshot-v1-32k',
    baseUrl: 'https://api.moonshot.cn/v1',
    apiKeyEnv: 'MOONSHOT_API_KEY',
  },
  {
    label: '本地 Ollama',
    name: 'Ollama 本地模型',
    modelId: 'qwen2.5:7b',
    baseUrl: 'http://localhost:11434/v1',
    apiKeyEnv: 'OLLAMA_API_KEY',
  },
]

export function Settings() {
  const { meta, theme, setTheme, library, refreshMeta, refreshLibrary } = useStore()
  const [usage, setUsage] = useState<Usage | null>(null)
  const [storage, setStorage] = useState<Storage | null>(null)
  const [clearing, setClearing] = useState(false)
  const [cleared, setCleared] = useState<string | null>(null)

  // 用户自主维护的模型列表（无硬编码默认列表）
  const [models, setModels] = useState<SavedModel[]>(() => {
    try {
      const raw = localStorage.getItem('vsum.userModels')
      return raw ? JSON.parse(raw) : []
    } catch {
      return []
    }
  })

  // 弹窗状态与表单
  const [showModal, setShowModal] = useState(false)
  const [editingModelId, setEditingModelId] = useState<string | null>(null)
  const [formName, setFormName] = useState('')
  const [formModelId, setFormModelId] = useState('')
  const [formBaseUrl, setFormBaseUrl] = useState('')
  const [formApiKey, setFormApiKey] = useState('')
  const [formApiKeyEnv, setFormApiKeyEnv] = useState('OPENAI_API_KEY')
  const [modalNotice, setModalNotice] = useState<string | null>(null)
  const [modalTesting, setModalTesting] = useState(false)
  const [modalTestResult, setModalTestResult] = useState<TestLlmResult | null>(null)

  // 当前生效模型测试与切换状态
  const [testingCurrent, setTestingCurrent] = useState(false)
  const [currentTestResult, setCurrentTestResult] = useState<TestLlmResult | null>(null)
  const [switching, setSwitching] = useState(false)
  const [switchNotice, setSwitchNotice] = useState<string | null>(null)

  // ASR 状态
  const [asrConfig, setAsrConfig] = useState<AsrConfig | null>(null)
  const [asrProvider, setAsrProvider] = useState<string>('funasr')
  const [asrModel, setAsrModel] = useState<string>('fun-asr-nano')
  const [asrDevice, setAsrDevice] = useState<string>('auto')
  const [asrDiarize, setAsrDiarize] = useState<string>('auto')
  const [savingAsr, setSavingAsr] = useState(false)
  const [asrNotice, setAsrNotice] = useState<string | null>(null)

  // 当前激活模型匹配
  const activeModel = useMemo(() => {
    const currentName = meta?.model || ''
    return (
      models.find((m) => m.name === currentName || m.modelId === currentName) || {
        id: 'current',
        name: currentName || '未配置',
        modelId: currentName,
        baseUrl: '',
        provider: 'openai',
        apiKeyEnv: 'OPENAI_API_KEY',
      }
    )
  }, [meta?.model, models])

  useEffect(() => {
    api.usage(30).then(setUsage).catch(() => setUsage(null))
    api.storage().then(setStorage).catch(() => setStorage(null))

    // 初始化模型列表：若本地完全为空，从当前 config.yaml 读取当前模型放入列表中
    api.getLlmConfig().then((cfg) => {
      setModels((prev) => {
        if (prev.length > 0) return prev
        const initial: SavedModel = {
          id: `model-${Date.now()}`,
          name: cfg.name || cfg.model || '当前模型',
          modelId: cfg.model || 'deepseek-chat',
          baseUrl: cfg.base_url || '',
          provider: cfg.provider || 'openai',
          apiKeyEnv: cfg.api_key_env || 'OPENAI_API_KEY',
        }
        try {
          localStorage.setItem('vsum.userModels', JSON.stringify([initial]))
        } catch {}
        return [initial]
      })
    }).catch(() => {})

    api.getAsrConfig().then((cfg) => {
      setAsrConfig(cfg)
      setAsrProvider(cfg.provider)
      setAsrModel(cfg.model)
      setAsrDevice(cfg.device)
      setAsrDiarize(String(cfg.diarize ?? 'auto'))
    }).catch(() => {})
  }, [])

  // 自由一键切换模型
  const selectModel = async (target: SavedModel) => {
    setSwitching(true)
    setSwitchNotice(null)
    setCurrentTestResult(null)
    try {
      await api.updateLlmConfig({
        name: target.name,
        model: target.modelId || target.name,
        provider: target.provider || 'openai',
        base_url: target.baseUrl || null,
        api_key_env: target.apiKeyEnv || 'OPENAI_API_KEY',
        api_key: target.apiKey || undefined,
      })
      await refreshMeta()
      await refreshLibrary()
      setSwitchNotice(`已成功切换至「${target.name}」！`)
      setTimeout(() => setSwitchNotice(null), 3000)
    } catch (e: unknown) {
      const err = e as { message?: string }
      setSwitchNotice(`切换失败: ${err.message || String(e)}`)
    } finally {
      setSwitching(false)
    }
  }

  // 快捷填入模板（仅新增模式使用）
  const loadTemplate = (tpl: typeof TEMPLATES[0]) => {
    setFormName(tpl.name)
    setFormModelId(tpl.modelId)
    setFormBaseUrl(tpl.baseUrl)
    setFormApiKeyEnv(tpl.apiKeyEnv)
    setModalTestResult(null)
    setModalNotice(null)
  }

  // 打开添加弹窗
  const openAddModal = () => {
    setEditingModelId(null)
    setFormName('')
    setFormModelId('')
    setFormBaseUrl('')
    setFormApiKey('')
    setFormApiKeyEnv('OPENAI_API_KEY')
    setModalNotice(null)
    setModalTestResult(null)
    setShowModal(true)
  }

  // 打开修改弹窗
  const openEditModal = (m: SavedModel) => {
    setEditingModelId(m.id)
    setFormName(m.name)
    setFormModelId(m.modelId)
    setFormBaseUrl(m.baseUrl)
    setFormApiKey(m.apiKey || '')
    setFormApiKeyEnv(m.apiKeyEnv || 'OPENAI_API_KEY')
    setModalNotice(null)
    setModalTestResult(null)
    setShowModal(true)
  }

  // 弹窗内测试连接
  const testInModal = async () => {
    setModalTesting(true)
    setModalTestResult(null)
    setModalNotice(null)
    try {
      const res = await api.testLlm({
        provider: 'openai',
        model: formModelId.trim() || formName.trim(),
        base_url: formBaseUrl.trim() || null,
        api_key: formApiKey.trim() || undefined,
        api_key_env: formApiKeyEnv.trim() || undefined,
      })
      setModalTestResult(res)
    } catch (e: unknown) {
      const err = e as { message?: string }
      setModalTestResult({ ok: false, latency_ms: 0, error: err.message || String(e) })
    } finally {
      setModalTesting(false)
    }
  }

  // 保存模型（新增或修改）
  const handleSaveModel = async () => {
    const name = formName.trim()
    if (!name) {
      setModalNotice('请填写模型显示名称（如 gemini 3.8 flash 或 DeepSeek v4.1）')
      return
    }

    if (editingModelId) {
      // 修改已有模型
      const updatedModel: SavedModel = {
        id: editingModelId,
        name,
        modelId: formModelId.trim() || name,
        baseUrl: formBaseUrl.trim(),
        provider: 'openai',
        apiKey: formApiKey.trim() || undefined,
        apiKeyEnv: formApiKeyEnv.trim() || 'OPENAI_API_KEY',
      }
      const updatedList = models.map((m) => (m.id === editingModelId ? updatedModel : m))
      setModels(updatedList)
      try {
        localStorage.setItem('vsum.userModels', JSON.stringify(updatedList))
      } catch {}

      setShowModal(false)

      // 如果修改的是当前正在生效的模型，立即同步后端
      const isCurrentlyActive = (meta?.model && (meta.model === formName || activeModel.id === editingModelId))
      if (isCurrentlyActive) {
        await selectModel(updatedModel)
      } else {
        setSwitchNotice(`模型「${name}」修改已保存！`)
        setTimeout(() => setSwitchNotice(null), 3000)
      }
    } else {
      // 添加新模型
      const newModel: SavedModel = {
        id: `custom-${Date.now()}`,
        name,
        modelId: formModelId.trim() || name,
        baseUrl: formBaseUrl.trim(),
        provider: 'openai',
        apiKey: formApiKey.trim() || undefined,
        apiKeyEnv: formApiKeyEnv.trim() || 'OPENAI_API_KEY',
      }
      const updatedList = [...models, newModel]
      setModels(updatedList)
      try {
        localStorage.setItem('vsum.userModels', JSON.stringify(updatedList))
      } catch {}

      setShowModal(false)
      // 添加后直接切换使用该模型
      await selectModel(newModel)
    }
  }

  // 删除模型
  const deleteModel = (id: string) => {
    const target = models.find((m) => m.id === id)
    if (!target) return
    if (!confirm(`确定删除模型「${target.name}」？`)) return

    const updatedList = models.filter((m) => m.id !== id)
    setModels(updatedList)
    try {
      localStorage.setItem('vsum.userModels', JSON.stringify(updatedList))
    } catch {}

    // 如果删除了当前激活模型，且还有其他模型，自动切到第一个
    if ((meta?.model === target.name || activeModel.id === id) && updatedList.length > 0) {
      selectModel(updatedList[0])
    }
  }

  // 测试当前生效模型
  const testCurrent = async () => {
    setTestingCurrent(true)
    setCurrentTestResult(null)
    try {
      const res = await api.testLlm()
      setCurrentTestResult(res)
    } catch (e: unknown) {
      const err = e as { message?: string }
      setCurrentTestResult({ ok: false, latency_ms: 0, error: err.message || String(e) })
    } finally {
      setTestingCurrent(false)
    }
  }

  // 保存 ASR
  const saveAsr = async () => {
    setSavingAsr(true)
    setAsrNotice(null)
    try {
      const updated = await api.updateAsrConfig({
        provider: asrProvider,
        model: asrModel.trim(),
        device: asrDevice,
        diarize: asrDiarize,
      })
      setAsrConfig(updated)
      setAsrNotice('语音识别设置已保存！')
      await refreshMeta()
      setTimeout(() => setAsrNotice(null), 3000)
    } catch (e: unknown) {
      const err = e as { message?: string }
      setAsrNotice(`保存失败: ${err.message || String(e)}`)
    } finally {
      setSavingAsr(false)
    }
  }

  const clearAudio = async () => {
    if (!storage || !confirm(`删掉 ${storage.audio_dirs} 个视频的音频缓存（${fmtBytes(storage.audio_bytes)}）？转写和总结都在，只有强制重跑识别时才需要重新下载。`)) return
    setClearing(true)
    try {
      const r = await api.clearAudio()
      setCleared(`已释放 ${fmtBytes(r.freed_bytes)}`)
      setStorage(await api.storage())
    } catch { setCleared('删除失败') } finally { setClearing(false) }
  }

  const titles = new Map((library?.groups ?? []).flatMap((g) => g.entries).map((e) => [e.video_id, displayTitle(e)]))
  const kindLabel = (k: string) => ({ summary: '总结', qa: '问视频', uploader_qa: '问 UP 主' }[k] ?? k)

  return (
    <div className="page">
      <div className="ph">
        <div>
          <h1>设置</h1>
          <p>在线自由添加与管理总结大模型、配置语音识别引擎，全站与左下角即时生效。</p>
        </div>
      </div>

      {/* 概览卡片 */}
      <div className="card">
        <dl className="kv">
          <dt>外观风格</dt>
          <dd>
            <div className="seg">
              {(['system', 'light', 'dark'] as const).map((t) => (
                <button key={t} aria-pressed={theme === t} onClick={() => setTheme(t)}>
                  {{ system: '跟随系统', light: '浅色', dark: '深色' }[t]}
                </button>
              ))}
            </div>
          </dd>
          <dt>当前使用大模型</dt>
          <dd>
            <b style={{ color: 'var(--accent)', fontSize: 14 }}>{meta?.model || meta?.provider || '—'}</b>
            <span style={{ color: 'var(--mute)', marginLeft: 8, fontSize: 12 }}>（左下角侧栏实时同步此选择）</span>
          </dd>
          <dt>当前识别模型</dt>
          <dd>{meta?.asr_default ?? '—'}{meta ? `（说话人分离：${String(meta.diarize_default)}）` : ''}</dd>
          <dt>默认总结类型</dt>
          <dd>{meta?.summary_types.find((t) => t.key === meta.default_summary_type)?.label ?? meta?.default_summary_type ?? '—'}</dd>
          <dt>产物目录</dt>
          <dd className="mono">{meta?.output_dir ?? '—'}</dd>
          <dt>软件版本</dt>
          <dd>{meta?.app} {meta?.version}</dd>
        </dl>
      </div>

      {/* 总结大模型管理区域 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', margin: '28px 0 10px', flexWrap: 'wrap', gap: 10 }}>
        <div>
          <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Sparkles size={18} color="var(--accent)" />
            我的大模型列表
          </h2>
          <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--mute)' }}>
            点击卡片可自由切换；点击卡片上的「修改」可调整配置；点击右上角「+ 添加大模型」随时扩充。
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn sm" onClick={testCurrent} disabled={testingCurrent}>
            {testingCurrent ? <><Loader2 size={13} className="spin" /> 测试中…</> : '测试当前连接'}
          </button>
          <button className="btn sm primary" onClick={openAddModal}>
            <Plus size={14} /> 添加大模型
          </button>
        </div>
      </div>

      {/* 状态与切换提示 */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12, minHeight: 24, flexWrap: 'wrap' }}>
        {switching && (
          <span className="pill info">
            <Loader2 size={12} className="spin" /> 正在切换模型…
          </span>
        )}
        {switchNotice && (
          <span className={`pill ${switchNotice.includes('失败') ? 'bad' : 'ok'}`}>
            {switchNotice}
          </span>
        )}
        {currentTestResult && (
          <span className={`pill ${currentTestResult.ok ? 'ok' : 'bad'}`}>
            {currentTestResult.ok
              ? `当前连接正常 (${currentTestResult.latency_ms}ms)`
              : `连接异常: ${currentTestResult.error || '失败'}`}
          </span>
        )}
      </div>

      {/* 模型卡片网格 */}
      <div className="model-grid">
        {models.map((m) => {
          const isActive = (meta?.model && (meta.model === m.name || meta.model === m.modelId)) ||
            (!meta?.model && activeModel.id === m.id)
          return (
            <div
              key={m.id}
              className={`model-card ${isActive ? 'active' : ''}`}
              onClick={() => !isActive && !switching && selectModel(m)}
              title={isActive ? '当前正在使用此模型' : '点击立即切换为此模型'}
            >
              <div className="m-title">
                <span>{m.name}</span>
                {isActive && (
                  <span className="pill ok" style={{ height: 20, fontSize: 11, padding: '0 6px', gap: 2 }}>
                    <Check size={12} /> 使用中
                  </span>
                )}
              </div>
              <div className="m-id">{m.modelId}</div>
              <div className="m-url" title={m.baseUrl}>
                {m.baseUrl ? m.baseUrl.replace(/^https?:\/\//, '').split('/')[0] : '官方默认接口'}
              </div>

              {/* 操作按钮栏：修改与删除 */}
              <div className="m-actions">
                <span className="m-tag">{m.provider || 'openai'}</span>
                <div className="m-btns">
                  <button
                    type="button"
                    className="m-btn"
                    title="修改此模型配置"
                    onClick={(e) => {
                      e.stopPropagation()
                      openEditModal(m)
                    }}
                  >
                    <Pencil size={11} />
                    <span>修改</span>
                  </button>
                  <button
                    type="button"
                    className="m-btn del"
                    title="删除此模型"
                    onClick={(e) => {
                      e.stopPropagation()
                      deleteModel(m.id)
                    }}
                  >
                    <Trash2 size={11} />
                    <span>删除</span>
                  </button>
                </div>
              </div>
            </div>
          )
        })}

        {/* 添加新模型卡片 */}
        <div className="model-card add-btn" onClick={openAddModal}>
          <Plus size={18} />
          <span style={{ fontSize: 13.5, fontWeight: 500 }}>添加大模型</span>
        </div>
      </div>

      {/* 添加 / 修改大模型弹窗 Modal */}
      {showModal && (
        <div className="modal-backdrop" onClick={() => setShowModal(false)}>
          <div className="modal-box" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h3 style={{ margin: 0 }}>{editingModelId ? `修改大模型：${formName || '配置'}` : '添加大模型'}</h3>
              <button
                className="btn sm ghost"
                style={{ padding: 4, height: 28, width: 28 }}
                onClick={() => setShowModal(false)}
              >
                <X size={16} />
              </button>
            </div>

            {!editingModelId && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12.5, color: 'var(--mute)', marginBottom: 8 }}>
                  常用模板（点击快捷填入）：
                </div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {TEMPLATES.map((t) => (
                    <button
                      key={t.label}
                      type="button"
                      className="btn sm ghost"
                      style={{ fontSize: 12, padding: '4px 8px' }}
                      onClick={() => loadTemplate(t)}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div className="cfg-form" style={{ gridTemplateColumns: '1fr', gap: 12 }}>
              <div className="cfg-field">
                <label>
                  模型显示名称 *
                  <span className="hint">例如 gemini 3.8 flash 或 DeepSeek v4.1（侧栏显示此名）</span>
                </label>
                <input
                  type="text"
                  value={formName}
                  onChange={(e) => setFormName(e.target.value)}
                  placeholder="如 gemini 3.8 flash / DeepSeek v4.1"
                  autoFocus
                />
              </div>

              <div className="cfg-field">
                <label>
                  实际模型标识 (Model ID)
                  <span className="hint">发给 API 接口的真实模型名，留空则默认同上方名称</span>
                </label>
                <input
                  type="text"
                  value={formModelId}
                  onChange={(e) => setFormModelId(e.target.value)}
                  placeholder="如 gemini-1.5-flash 或 deepseek-chat"
                />
              </div>

              <div className="cfg-field">
                <label>
                  接口地址 (Base URL)
                  <span className="hint">API 根地址</span>
                </label>
                <input
                  type="text"
                  value={formBaseUrl}
                  onChange={(e) => setFormBaseUrl(e.target.value)}
                  placeholder="如 https://generativelanguage.googleapis.com/v1beta/openai/ 或 https://api.deepseek.com/v1"
                />
              </div>

              <div className="cfg-field">
                <label>
                  API 密钥 (API Key)
                  <span className="hint">安全存储于本地 .env；若密钥未变或已配置可留空</span>
                </label>
                <input
                  type="password"
                  value={formApiKey}
                  onChange={(e) => setFormApiKey(e.target.value)}
                  placeholder="sk-..."
                />
              </div>

              <div className="cfg-field">
                <label>
                  密钥环境变量名 (api_key_env)
                </label>
                <input
                  type="text"
                  value={formApiKeyEnv}
                  onChange={(e) => setFormApiKeyEnv(e.target.value)}
                  placeholder="GEMINI_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY"
                />
              </div>
            </div>

            {modalNotice && (
              <div style={{ color: 'var(--bad)', fontSize: 13, marginTop: 10 }}>
                {modalNotice}
              </div>
            )}

            {modalTestResult && (
              <div style={{ marginTop: 12 }}>
                <span className={`pill ${modalTestResult.ok ? 'ok' : 'bad'}`}>
                  {modalTestResult.ok
                    ? `测试连接正常 (${modalTestResult.latency_ms}ms)`
                    : `测试失败: ${modalTestResult.error || '错误'}`}
                </span>
              </div>
            )}

            <div className="modal-foot">
              <button
                type="button"
                className="btn sm"
                onClick={testInModal}
                disabled={modalTesting}
              >
                {modalTesting ? '正在测试…' : '测试连接'}
              </button>
              <button
                type="button"
                className="btn sm ghost"
                onClick={() => setShowModal(false)}
              >
                取消
              </button>
              <button
                type="button"
                className="btn sm primary"
                onClick={handleSaveModel}
              >
                {editingModelId ? '保存修改' : '确认添加并使用'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 语音识别 (ASR) 模型设置 */}
      <h2 style={{ fontSize: 16, fontWeight: 600, margin: '28px 0 10px' }}>语音识别 (ASR) 设置</h2>
      <div className="card" style={{ padding: '20px 24px' }}>
        <div style={{ fontSize: 13, color: 'var(--mute)', marginBottom: 14 }}>
          当前生效引擎：<b style={{ color: 'var(--ink)' }}>{asrConfig?.model || meta?.asr_default || '—'}</b>
          <span style={{ marginLeft: 8, fontSize: 12 }}>（保存后下次提取转写任务自动生效）</span>
        </div>
        <div className="cfg-form">
          <div className="cfg-field">
            <label>识别引擎 (Provider)</label>
            <select
              value={asrProvider}
              onChange={(e) => {
                const p = e.target.value
                setAsrProvider(p)
                if (p === 'funasr') setAsrModel('fun-asr-nano')
                else if (p === 'whisper') setAsrModel('large-v3')
              }}
            >
              <option value="funasr">FunASR (极速，中文/日韩英，推荐)</option>
              <option value="whisper">Faster-Whisper (多语种，近百种语言兜底)</option>
            </select>
          </div>

          <div className="cfg-field">
            <label>识别模型 (Model)</label>
            {asrProvider === 'funasr' ? (
              <select value={asrModel} onChange={(e) => setAsrModel(e.target.value)}>
                <option value="fun-asr-nano">fun-asr-nano (准，专名少错，吃词表热词，默认推荐)</option>
                <option value="sensevoice-small">sensevoice-small (极速多语言)</option>
                <option value="paraformer-zh">paraformer-zh (中文精调，支持说话人分离)</option>
              </select>
            ) : (
              <select value={asrModel} onChange={(e) => setAsrModel(e.target.value)}>
                <option value="large-v3">large-v3 (高精度多语种，推荐)</option>
                <option value="medium">medium (平衡)</option>
                <option value="small">small (较快)</option>
                <option value="base">base (轻量)</option>
                <option value="tiny">tiny (极轻量)</option>
              </select>
            )}
          </div>

          <div className="cfg-field">
            <label>计算设备 (Device)</label>
            <select value={asrDevice} onChange={(e) => setAsrDevice(e.target.value)}>
              <option value="auto">auto (自动检测 GPU / CPU)</option>
              <option value="cuda">cuda (NVIDIA 独立显卡加速)</option>
              <option value="cpu">cpu (处理器计算)</option>
            </select>
          </div>

          <div className="cfg-field">
            <label>说话人分离 (Diarization)</label>
            <select value={asrDiarize} onChange={(e) => setAsrDiarize(e.target.value)}>
              <option value="auto">auto (仅在选择分角色总结时开启，推荐)</option>
              <option value="true">true (始终开启说话人分离)</option>
              <option value="false">false (关闭角色分离，提速 50%)</option>
            </select>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 14 }}>
          <button className="btn primary" onClick={saveAsr} disabled={savingAsr}>
            {savingAsr ? '保存中…' : '保存识别配置'}
          </button>
          {asrNotice && (
            <span style={{ fontSize: 13, color: asrNotice.includes('失败') ? 'var(--bad)' : 'var(--ok)' }}>
              {asrNotice}
            </span>
          )}
        </div>
      </div>

      {/* 存储管理 */}
      {storage && (
        <>
          <h2 style={{ fontSize: 16, fontWeight: 600, margin: '28px 0 10px' }}>存储管理</h2>
          <div className="card">
            <dl className="kv">
              <dt>音频缓存</dt>
              <dd style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <span className="mono">{fmtBytes(storage.audio_bytes)}</span>
                <span style={{ color: 'var(--mute)' }}>{storage.audio_dirs} 个视频。转写完成后已提取文本，清理不影响已有总结。</span>
                <button className="btn sm" onClick={clearAudio} disabled={clearing || storage.audio_bytes === 0}>
                  {clearing ? '删除中…' : '清理音频缓存'}
                </button>
                {cleared && <span style={{ color: 'var(--ok)', fontSize: 12.5 }}>{cleared}</span>}
              </dd>
              <dt>转写与总结数据</dt>
              <dd className="mono">{fmtBytes(storage.other_bytes)}</dd>
              <dt>缓存数据库</dt>
              <dd className="mono">{fmtBytes(storage.cache_bytes)}</dd>
            </dl>
          </div>
        </>
      )}

      {/* 调用统计 */}
      {usage && (
        <>
          <h2 style={{ fontSize: 16, fontWeight: 600, margin: '28px 0 10px' }}>调用统计</h2>
          <div className="stats" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
            <div className="card stat">
              <div className="k">本月调用</div>
              <div className="v mono">
                {usage.month.calls} <small>次</small>
              </div>
            </div>
            <div className="card stat">
              <div className="k">累计调用</div>
              <div className="v mono">
                {usage.total.calls} <small>次</small>
              </div>
            </div>
            <div className="card stat">
              <div className="k">累计 Token</div>
              <div className="v mono">
                {fmtTokens(usage.total.input_tokens)}
                <small>入 · {fmtTokens(usage.total.output_tokens)} 出</small>
              </div>
            </div>
          </div>
          {usage.recent.length > 0 && (
            <div className="card" style={{ marginTop: 12 }}>
              {usage.recent.map((r, i) => (
                <div className="urow" key={i}>
                  <span className="k">{kindLabel(r.kind)}</span>
                  <span className="t" title={r.detail ?? ''}>
                    {titles.get(r.video_id) ?? r.video_id}
                    {r.kind === 'qa' && r.detail ? ` · ${r.detail}` : ''}
                  </span>
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
