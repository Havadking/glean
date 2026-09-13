import { FolderOpen, Plus, Search, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, type Entry } from '../api'
import { Avatar, ErrorBox, Pill, Seg, Stats } from '../components/ui'
import { fmtDuration, fmtMinutes, fmtWhen } from '../lib/format'
import { useStore } from '../store'

type Filter = 'all' | 'mindmap' | 'nosummary'

export function Library() {
  const { library, libraryError, refreshLibrary } = useStore()
  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [err, setErr] = useState<unknown>(null)
  const nav = useNavigate()
  const up = params.get('up')

  const groups = useMemo(() => {
    if (!library) return []
    const q = query.trim().toLowerCase()
    return library.groups
      .filter((g) => !up || g.uploader === up)
      .map((g) => ({
        ...g,
        entries: g.entries.filter((e) => {
          if (q && !e.title.toLowerCase().includes(q) && !(e.uploader ?? '').toLowerCase().includes(q)) return false
          if (filter === 'mindmap' && !e.summaries.some((s) => s.type === 'mindmap')) return false
          if (filter === 'nosummary' && e.summaries.length > 0) return false
          return true
        }),
      }))
      .filter((g) => g.entries.length > 0)
  }, [library, query, filter, up])

  const remove = async (e: Entry) => {
    if (!confirm(`删除「${e.title}」的转写、总结和产物目录？不可恢复。`)) return
    try { await api.deleteVideo(e.video_id); await refreshLibrary() } catch (x) { setErr(x) }
  }

  const s = library?.stats

  return (
    <div className="page">
      <div className="ph">
        <div>
          <h1>库</h1>
          <p>处理过的视频都在这里，按 UP 主归堆。</p>
        </div>
        <Link className="btn primary" to="/"><Plus /> 新任务</Link>
      </div>

      {s && (
        <Stats items={[
          { k: '视频', v: s.videos },
          { k: '转写时长', v: fmtMinutes(s.duration_sec), small: '分钟' },
          { k: '总结', v: s.summaries, small: s.mindmaps ? `${s.mindmaps} 张导图` : undefined },
          { k: 'UP 主', v: library!.groups.filter((g) => g.uploader).length },
        ]} />
      )}

      <div className="libbar">
        <label className="search"><Search /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜标题、UP 主…" /></label>
        <Seg value={filter} onChange={setFilter} items={[{ key: 'all', label: '全部' }, { key: 'mindmap', label: '有导图' }, { key: 'nosummary', label: '还没总结' }]} />
        {up && <button className="btn sm" onClick={() => setParams({})}>只看 {up} ✕</button>}
        <span className="sp" />
      </div>

      <ErrorBox error={libraryError ?? err} />

      {library && library.groups.length === 0 && (
        <div className="empty"><b>还没有处理过的视频</b>去「新任务」贴一个链接。<div><Link className="btn primary" to="/">新任务</Link></div></div>
      )}
      {library && library.groups.length > 0 && groups.length === 0 && (
        <div className="empty"><b>没有匹配的</b>换个词，或者清掉筛选。</div>
      )}

      {groups.map((g) => (
        <div className="group" key={g.uploader ?? '__none'}>
          <div className="gh">
            <Avatar name={g.uploader} grey={!g.uploader} />
            {g.uploader ?? '其他'}
            <span className="c">{g.entries.length} 条 · {fmtMinutes(g.entries.reduce((n, e) => n + e.duration_sec, 0))} 分钟</span>
            <span className="sp" />
          </div>
          <div className="list">
            {g.entries.map((e) => (
              <div className="item" key={e.video_id} role="link" tabIndex={0}
                onClick={() => nav(`/video/${encodeURIComponent(e.video_id)}`)}
                onKeyDown={(ev) => { if (ev.key === 'Enter') nav(`/video/${encodeURIComponent(e.video_id)}`) }}
                style={{ cursor: 'pointer' }}>
                <div className="th">{e.thumbnail && <img src={e.thumbnail} alt="" referrerPolicy="no-referrer" loading="lazy" />}</div>
                <div className="ti">
                  <div className="t" title={e.title}>{e.title}</div>
                  <div className="s">
                    <span className="mono">{fmtDuration(e.duration_sec)}</span>
                    <span>{e.source_label}{e.diarized ? ' · 分说话人' : ''}</span>
                    {e.language && <span>{e.language}</span>}
                  </div>
                </div>
                <div className="badges">
                  {e.summaries.length === 0 && <Pill tone="neutral">没总结</Pill>}
                  {e.summaries.map((sm) => <Pill tone="ok" key={sm.type}>{sm.label}</Pill>)}
                </div>
                <div className="when">{fmtWhen(e.created_at)}</div>
                <div className="act" onClick={(ev) => ev.stopPropagation()}>
                  {e.work_dir && <button className="iconbtn" title="打开目录" onClick={() => api.openFolder(e.video_id).catch(setErr)}><FolderOpen /></button>}
                  <button className="iconbtn danger" title="删除" onClick={() => remove(e)}><Trash2 /></button>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
