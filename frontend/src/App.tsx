import { useEffect } from 'react'
import { BrowserRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { Library } from './pages/Library'
import { NewTask } from './pages/NewTask'
import { Settings } from './pages/Settings'
import { Uploader } from './pages/Uploader'
import { Video } from './pages/Video'
import { StoreProvider } from './store'

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

export default function App() {
  return (
    <BrowserRouter>
      <StoreProvider>
        <Hotkeys />
        <div className="shell">
          <Sidebar />
          <main className="main">
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
      </StoreProvider>
    </BrowserRouter>
  )
}
