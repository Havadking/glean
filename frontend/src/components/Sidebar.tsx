import { Plus, List, Settings, PanelLeftClose, X, CalendarDays } from 'lucide-react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useStore } from '../store'
import { Avatar } from './ui'

export function Sidebar({ narrow, onHide }: { narrow: boolean; onHide: () => void }) {
  const { meta, library, isDark, setTheme, queue } = useStore()
  const nav = useNavigate()
  const groups = library?.groups ?? []
  const recent = groups.flatMap((g) => g.entries).sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, 5)

  const runningJob = queue.find((j) => j.status === 'running')
  // 本周收藏了东西但回顾还没生成 / 已经过期：给个小点提醒
  const rv = library?.review
  const reviewDot = !!rv && rv.videos > 0 && (!rv.generated || rv.stale)
  const queuedJobs = queue.filter((j) => j.status === 'queued')

  const stageName = (s?: string | null) => {
    if (!s) return ''
    const m: Record<string, string> = {
      probe: '探测',
      subtitle: '下载字幕',
      download: '提取音频',
      transcribe: '语音识别',
      polish: '纠专有名词',
      summarize: 'AI 总结',
      tags: '打标签',
    }
    return m[s] || s
  }

  return (
    <aside className="side">
      <div className="brand">
        <span className="mark" />
        {meta?.app ?? '拾光笺'}
        <span className="ver">v{meta?.version ?? ''}</span>
        <button className="iconbtn hide" onClick={onHide} aria-label={narrow ? '关闭侧栏' : '收起侧栏'} title={narrow ? '关闭' : '收起侧栏'}>
          {narrow ? <X /> : <PanelLeftClose />}
        </button>
      </div>
      <nav className="nav">
        <NavLink to="/" end className={({ isActive }) => (isActive ? 'on' : '')}>
          <Plus /> 新任务 <span className="kbd">N</span>
        </NavLink>
        <NavLink to="/library" className={({ isActive }) => (isActive ? 'on' : '')}>
          <List /> 库 <span className="kbd">L</span>
        </NavLink>
        <NavLink to="/review" className={({ isActive }) => (isActive ? 'on' : '')} title={reviewDot ? (rv?.generated ? '本周又收藏了新东西，回顾可以更新了' : `本周收藏了 ${rv?.videos} 条，还没生成回顾`) : undefined}>
          <CalendarDays /> 回顾 {reviewDot && <span className="ndot" />}<span className="kbd">R</span>
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => (isActive ? 'on' : '')}>
          <Settings /> 设置
        </NavLink>
      </nav>

      {groups.some((g) => g.uploader) && (
        <>
          <div className="sec">UP 主</div>
          {groups.filter((g) => g.uploader).map((g) => (
            <button className="up" key={g.uploader!} onClick={() => nav(`/uploader/${encodeURIComponent(g.uploader!)}`)}>
              <Avatar name={g.uploader} />
              <span className="n">{g.uploader}</span>
              <span className="c">{g.entries.length}</span>
            </button>
          ))}
        </>
      )}

      {recent.length > 0 && (
        <>
          <div className="sec">最近</div>
          {recent.map((e) => (
            <NavLink className={({ isActive }) => `up${isActive ? ' on' : ''}`} to={`/video/${encodeURIComponent(e.video_id)}`} key={e.video_id} title={e.title}>
              <span className="n" style={{ paddingLeft: 2 }}>{e.title}</span>
            </NavLink>
          ))}
        </>
      )}

      {(runningJob || queuedJobs.length > 0) && (
        <div className="mini-tracker" onClick={() => nav('/')} title="点击查看任务详情与队列">
          <div className="mt-head">
            <span className="dot ok" />
            <span className="mt-stage">{runningJob ? (stageName(runningJob.stage) || '处理中') : '排队中'}</span>
            {runningJob?.progress != null && <span className="mono mt-pct">{Math.round(runningJob.progress * 100)}%</span>}
            {queuedJobs.length > 0 && <span className="mt-q">+{queuedJobs.length} 排队</span>}
          </div>
          <div className="mt-title">{runningJob?.title || queuedJobs[0]?.title}</div>
          {runningJob?.progress != null && (
            <div className="mt-bar"><i style={{ width: `${Math.round(runningJob.progress * 100)}%` }} /></div>
          )}
        </div>
      )}

      <div className="foot">
        <div className="row">
          <span>深色模式</span>
          <button className="toggle" role="switch" aria-checked={isDark} aria-label="深色模式"
            onClick={() => setTheme(isDark ? 'light' : 'dark')} />
        </div>
        <div className="row">
          <span>模型</span>
          <span title={meta?.model ? `${meta.model} (${meta.provider})` : meta?.provider}>
            {meta?.model || meta?.provider || '—'}
          </span>
        </div>
      </div>
    </aside>
  )
}
