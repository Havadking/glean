import { ArrowUpDown, ChevronDown, ChevronsUpDown, FolderOpen, Plus, Search, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, type Entry, type SearchResult } from '../api'
import { Avatar, ErrorBox, Highlight, Pill, Seg, Stats } from '../components/ui'
import { fmtDuration, fmtMinutes, fmtMoney, fmtWhen } from '../lib/format'
import { useStore } from '../store'

type Filter = 'all' | 'mindmap' | 'nosummary'
type SortKey = 'created_desc' | 'created_asc' | 'duration_desc' | 'duration_asc' | 'cost_desc' | 'title_asc'

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: 'created_desc', label: '最新添加' },
  { key: 'created_asc', label: '最早添加' },
  { key: 'duration_desc', label: '时长最长' },
  { key: 'duration_asc', label: '时长最短' },
  { key: 'cost_desc', label: '花费最高' },
  { key: 'title_asc', label: '标题名称 (A-Z)' },
]

function sortEntries(items: Entry[], sort: SortKey): Entry[] {
  return [...items].sort((a, b) => {
    switch (sort) {
      case 'created_desc':
        return (b.created_at || '').localeCompare(a.created_at || '')
      case 'created_asc':
        return (a.created_at || '').localeCompare(b.created_at || '')
      case 'duration_desc':
        return (b.duration_sec ?? 0) - (a.duration_sec ?? 0)
      case 'duration_asc':
        return (a.duration_sec ?? 0) - (b.duration_sec ?? 0)
      case 'cost_desc':
        return (b.cost ?? 0) - (a.cost ?? 0)
      case 'title_asc':
        return (a.title || '').localeCompare(b.title || '', 'zh-CN')
      default:
        return 0
    }
  })
}

export function Library() {
  const { library, libraryError, refreshLibrary, meta } = useStore()
  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState(() => params.get('q') ?? '')
  const [filter, setFilter] = useState<Filter>('all')
  const [sortBy, setSortBy] = useState<SortKey>(() => {
    const saved = localStorage.getItem('vsum.sortBy')
    if (saved && ['created_desc', 'created_asc', 'duration_desc', 'duration_asc', 'cost_desc', 'title_asc'].includes(saved)) {
      return saved as SortKey
    }
    return 'created_desc'
  })
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem('vsum.collapsedGroups')
      return raw ? new Set(JSON.parse(raw)) : new Set()
    } catch {
      return new Set()
    }
  })
  const [err, setErr] = useState<unknown>(null)
  const [result, setResult] = useState<SearchResult | null>(null)
  const [searching, setSearching] = useState(false)
  const nav = useNavigate()
  const up = params.get('up')

  const handleSortChange = (newSort: SortKey) => {
    setSortBy(newSort)
    try {
      localStorage.setItem('vsum.sortBy', newSort)
    } catch {}
  }

  const toggleGroup = (key: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      try {
        localStorage.setItem('vsum.collapsedGroups', JSON.stringify(Array.from(next)))
      } catch {}
      return next
    })
  }

  const isGroupCollapsed = (key: string) => {
    if (query.trim()) return false
    return collapsedGroups.has(key)
  }

  // 全文搜索：停 250ms 再发，上一次没回来的作废
  useEffect(() => {
    const q = query.trim()
    if (!q) { setResult(null); setSearching(false); return }
    const ctrl = new AbortController()
    setSearching(true)
    const t = setTimeout(() => {
      api.search(q, ctrl.signal)
        .then((r) => { setResult(r); setSearching(false) })
        .catch((e: unknown) => {
          if ((e as { name?: string })?.name !== 'AbortError') { setErr(e); setSearching(false) }
        })
    }, 250)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [query])

  const typeLabel = (key: string) => meta?.summary_types.find((t) => t.key === key)?.label ?? key

  const groups = useMemo(() => {
    if (!library) return []
    const q = query.trim().toLowerCase()
    const processed = library.groups
      .filter((g) => !up || g.uploader === up)
      .map((g) => {
        const filteredEntries = g.entries.filter((e) => {
          if (q && !e.title.toLowerCase().includes(q) && !(e.uploader ?? '').toLowerCase().includes(q)) return false
          if (filter === 'mindmap' && !e.summaries.some((s) => s.type === 'mindmap')) return false
          if (filter === 'nosummary' && e.summaries.length > 0) return false
          return true
        })
        return {
          ...g,
          entries: sortEntries(filteredEntries, sortBy),
        }
      })
      .filter((g) => g.entries.length > 0)

    return processed.sort((a, b) => {
      if (!a.uploader && b.uploader) return 1
      if (a.uploader && !b.uploader) return -1

      switch (sortBy) {
        case 'created_desc': {
          const tA = a.entries[0]?.created_at || ''
          const tB = b.entries[0]?.created_at || ''
          return tB.localeCompare(tA)
        }
        case 'created_asc': {
          const tA = a.entries[0]?.created_at || ''
          const tB = b.entries[0]?.created_at || ''
          return tA.localeCompare(tB)
        }
        case 'duration_desc': {
          const maxA = Math.max(...a.entries.map((e) => e.duration_sec ?? 0), 0)
          const maxB = Math.max(...b.entries.map((e) => e.duration_sec ?? 0), 0)
          return maxB - maxA
        }
        case 'duration_asc': {
          const minA = Math.min(...a.entries.map((e) => e.duration_sec ?? 0), Infinity)
          const minB = Math.min(...b.entries.map((e) => e.duration_sec ?? 0), Infinity)
          return minA - minB
        }
        case 'cost_desc': {
          const totalA = a.entries.reduce((acc, e) => acc + (e.cost ?? 0), 0)
          const totalB = b.entries.reduce((acc, e) => acc + (e.cost ?? 0), 0)
          return totalB - totalA
        }
        case 'title_asc': {
          return (a.uploader || '').localeCompare(b.uploader || '', 'zh-CN')
        }
        default:
          return 0
      }
    })
  }, [library, query, filter, up, sortBy])

  const allCollapsed = groups.length > 0 && groups.every((g) => isGroupCollapsed(g.uploader ?? '__none'))

  const toggleAllGroups = (collapse: boolean) => {
    setCollapsedGroups(() => {
      const next = collapse ? new Set(groups.map((g) => g.uploader ?? '__none')) : new Set<string>()
      try {
        localStorage.setItem('vsum.collapsedGroups', JSON.stringify(Array.from(next)))
      } catch {}
      return next
    })
  }

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
          { k: '累计花费', v: fmtMoney(s.cost, s.currency) ?? '—', small: s.calls ? `${s.calls} 次调用` : undefined, mono: true },
        ]} />
      )}

      <div className="libbar">
        <label className="search"><Search /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜标题、转写、总结…" /></label>
        <Seg value={filter} onChange={setFilter} items={[{ key: 'all', label: '全部' }, { key: 'mindmap', label: '有导图' }, { key: 'nosummary', label: '还没总结' }]} />
        <div className="lib-sort">
          <ArrowUpDown size={14} className="sort-icon" />
          <select
            className="sel"
            value={sortBy}
            onChange={(e) => handleSortChange(e.target.value as SortKey)}
            title="排序方式"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.key} value={o.key}>{o.label}</option>
            ))}
          </select>
        </div>
        {up && <button className="btn sm" onClick={() => setParams({})}>只看 {up} ✕</button>}
        <span className="sp" />
        {groups.length > 1 && !query.trim() && (
          <button
            className="btn ghost sm"
            onClick={() => toggleAllGroups(!allCollapsed)}
            title={allCollapsed ? '展开所有 UP 主分组' : '折叠所有 UP 主分组'}
          >
            <ChevronsUpDown size={14} />
            {allCollapsed ? '全部展开' : '全部折叠'}
          </button>
        )}
      </div>

      <ErrorBox error={libraryError ?? err} />

      {query.trim() && (
        <div className="card hit">
          <div className="h">
            {searching && !result ? <span className="spin" /> : null}
            {result && (result.transcript_hits + result.summary_hits > 0
              ? <Pill tone="accent">转写 {result.transcript_hits} 处 · 总结 {result.summary_hits} 处 · {result.videos.length} 条视频</Pill>
              : <Pill tone="neutral">正文里没有「{result.query}」</Pill>)}
            <span>搜的是转写和总结正文，不花钱</span>
          </div>
          {result?.videos.map((v) => (
            <div className="hv" key={v.video_id}>
              <div className="hvt">
                <Link to={`/video/${encodeURIComponent(v.video_id)}?q=${encodeURIComponent(query.trim())}`}>{v.title}</Link>
                {v.uploader && <span className="c">{v.uploader}</span>}
                <span className="c mono">{fmtDuration(v.duration_sec)}</span>
                <span className="c">{v.hits.length} 处</span>
              </div>
              {v.hits.slice(0, 6).map((h, i) => (
                <Link className="r" key={i}
                  to={h.kind === 'transcript'
                    ? `/video/${encodeURIComponent(v.video_id)}?t=${h.start}&q=${encodeURIComponent(query.trim())}`
                    : `/video/${encodeURIComponent(v.video_id)}?type=${encodeURIComponent(h.ref)}&q=${encodeURIComponent(query.trim())}`}>
                  <span className="q"><Highlight text={h.snippet} query={query} /></span>
                  <span className="w">{h.kind === 'transcript' ? <span className="mono">{fmtDuration(h.start)}</span> : `总结 · ${typeLabel(h.ref)}`}</span>
                </Link>
              ))}
              {v.hits.length > 6 && <div className="more">还有 {v.hits.length - 6} 处，打开视频看全部</div>}
            </div>
          ))}
        </div>
      )}

      {library && library.groups.length === 0 && (
        <div className="empty"><b>还没有处理过的视频</b>去「新任务」贴一个链接。<div><Link className="btn primary" to="/">新任务</Link></div></div>
      )}
      {library && library.groups.length > 0 && groups.length === 0 && !(result && result.videos.length > 0) && (
        <div className="empty"><b>没有匹配的</b>换个词，或者清掉筛选。</div>
      )}

      {groups.map((g) => {
        const groupKey = g.uploader ?? '__none'
        const isCollapsed = isGroupCollapsed(groupKey)
        const groupDuration = g.entries.reduce((n, e) => n + e.duration_sec, 0)
        const groupCost = g.entries.reduce((n, e) => n + (e.cost ?? 0), 0)

        return (
          <div className={`group ${isCollapsed ? 'is-collapsed' : ''}`} key={groupKey}>
            <div className="gh">
              <div
                role="button"
                tabIndex={0}
                className="gh-toggle"
                onClick={() => toggleGroup(groupKey)}
                onKeyDown={(ev) => {
                  if (ev.key === 'Enter' || ev.key === ' ') {
                    ev.preventDefault()
                    toggleGroup(groupKey)
                  }
                }}
                title={isCollapsed ? '点击展开分组' : '点击折叠分组'}
              >
                <ChevronDown className={`gh-chevron ${isCollapsed ? 'collapsed' : ''}`} />
                <Avatar name={g.uploader} grey={!g.uploader} />
                <span>{g.uploader ?? '其他'}</span>
                <span className="c">
                  {g.entries.length} 条 · {fmtMinutes(groupDuration)} 分钟
                  {groupCost > 0 && ` · ${fmtMoney(groupCost, s?.currency)}`}
                </span>
                {isCollapsed && <span className="gh-badge">已折叠</span>}
              </div>
              <span className="sp" />
              {g.uploader && (
                <Link
                  className="btn ghost sm"
                  to={`/uploader/${encodeURIComponent(g.uploader)}`}
                  onClick={(ev) => ev.stopPropagation()}
                >
                  问 TA · 跨视频
                </Link>
              )}
            </div>
            {!isCollapsed && (
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
                        {e.cost != null && e.cost > 0 && <span className="mono">{fmtMoney(e.cost, s?.currency)}</span>}
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
            )}
          </div>
        )
      })}
    </div>
  )
}
