import { useCallback, useEffect, useState } from 'react'
import { PanelLeftOpen } from 'lucide-react'
import { BrowserRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { Library } from './pages/Library'
import { NewTask } from './pages/NewTask'
import { Settings } from './pages/Settings'
import { Uploader } from './pages/Uploader'
import { Video } from './pages/Video'
import { StoreProvider } from './store'

// 与 index.css 里 @media (max-width: 980px) 保持一致
const NARROW = '(max-width: 980px)'

function useMedia(query: string) {
  const [match, setMatch] = useState(() => matchMedia(query).matches)
  useEffect(() => {
    const mq = matchMedia(query)
    const on = () => setMatch(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [query])
  return match
}

function readCollapsed() {
  try { return localStorage.getItem('sidebar') === 'collapsed' } catch { return false }
}

function Hotkeys() {
  const nav = useNavigate()
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === 'n') nav('/')
      if (e.key === 'l') nav('/library')
    }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [nav])
  return null
}

// 侧栏两种形态：宽屏固定在左边，可收起（记住）；窄屏/竖屏是抽屉，从顶栏按钮拉出。
function Shell() {
  const narrow = useMedia(NARROW)
  const [collapsed, setCollapsed] = useState(readCollapsed)
  // 记录抽屉是在哪个路径打开的：换页或切回宽屏后自然失效，不用 effect 去关
  const [openedAt, setOpenedAt] = useState<string | null>(null)
  const loc = useLocation()
  const drawer = narrow && openedAt === loc.pathname

  const hidden = narrow ? !drawer : collapsed
  const toggle = useCallback(() => {
    if (narrow) { setOpenedAt((v) => (v === loc.pathname ? null : loc.pathname)); return }
    setCollapsed((v) => {
      try { localStorage.setItem('sidebar', v ? 'docked' : 'collapsed') } catch { /* ignore */ }
      return !v
    })
  }, [narrow, loc.pathname])
  const closeDrawer = useCallback(() => setOpenedAt(null), [])

  useEffect(() => {
    if (!drawer) return
    const on = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpenedAt(null) }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [drawer])

  return (
    <div className={`shell${collapsed ? ' collapsed' : ''}${drawer ? ' drawer' : ''}`}>
      <Sidebar narrow={narrow} onHide={toggle} />
      <div className="backdrop" onClick={closeDrawer} aria-hidden="true" />
      <main className="main">
        {hidden && (
          <div className="topbar">
            <button className="iconbtn" onClick={toggle} aria-label="打开侧栏" aria-expanded={false} title="打开侧栏">
              <PanelLeftOpen />
            </button>
          </div>
        )}
        <Routes>
          <Route path="/" element={<NewTask />} />
          <Route path="/library" element={<Library />} />
          <Route path="/video/:id" element={<Video />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/uploader/:name" element={<Uploader />} />
          <Route path="*" element={<NewTask />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <StoreProvider>
        <Hotkeys />
        <Shell />
      </StoreProvider>
    </BrowserRouter>
  )
}
