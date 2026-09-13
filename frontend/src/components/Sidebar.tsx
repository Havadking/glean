import { Plus, List, Settings } from 'lucide-react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useStore } from '../store'
import { Avatar } from './ui'

export function Sidebar() {
  const { meta, library, isDark, setTheme } = useStore()
  const nav = useNavigate()
  const groups = library?.groups ?? []
  const recent = groups.flatMap((g) => g.entries).sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, 5)

  return (
    <aside className="side">
      <div className="brand">
        <span className="mark" />
        {meta?.app ?? '拾光笺'}
        <span className="ver">v{meta?.version ?? ''}</span>
      </div>
      <nav className="nav">
        <NavLink to="/" end className={({ isActive }) => (isActive ? 'on' : '')}>
          <Plus /> 新任务 <span className="kbd">N</span>
        </NavLink>
        <NavLink to="/library" className={({ isActive }) => (isActive ? 'on' : '')}>
          <List /> 库 <span className="kbd">L</span>
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => (isActive ? 'on' : '')}>
          <Settings /> 设置
        </NavLink>
      </nav>

      {groups.some((g) => g.uploader) && (
        <>
          <div className="sec">UP 主</div>
          {groups.filter((g) => g.uploader).map((g) => (
            <button className="up" key={g.uploader!} onClick={() => nav(`/library?up=${encodeURIComponent(g.uploader!)}`)}>
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

      <div className="foot">
        <div className="row">
          <span>深色模式</span>
          <button className="toggle" role="switch" aria-checked={isDark} aria-label="深色模式"
            onClick={() => setTheme(isDark ? 'light' : 'dark')} />
        </div>
        <div className="row"><span>模型</span><span title={meta?.provider}>{meta?.provider ?? '—'}</span></div>
      </div>
    </aside>
  )
}
