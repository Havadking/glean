// 全局状态：元信息、库（侧栏要用）、主题。简单的 context，不上状态库。

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, type Job, type Library, type Meta } from './api'

type Theme = 'light' | 'dark' | 'system'

interface Store {
  meta: Meta | null
  library: Library | null
  libraryError: unknown
  refreshLibrary: () => Promise<void>
  refreshMeta: () => Promise<void>
  queue: Job[]
  refreshQueue: () => Promise<void>
  theme: Theme
  isDark: boolean
  setTheme: (t: Theme) => void
}

const Ctx = createContext<Store | null>(null)

function readTheme(): Theme {
  try {
    const v = localStorage.getItem('theme')
    if (v === 'light' || v === 'dark') return v
  } catch { /* 隐私模式等 */ }
  return 'system'
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [library, setLibrary] = useState<Library | null>(null)
  const [libraryError, setLibraryError] = useState<unknown>(null)
  const [queue, setQueue] = useState<Job[]>([])
  const [theme, setThemeState] = useState<Theme>(readTheme)
  const [systemDark, setSystemDark] = useState(() => matchMedia('(prefers-color-scheme: dark)').matches)

  const refreshMeta = useCallback(async () => {
    try {
      setMeta(await api.meta())
    } catch {
      setMeta(null)
    }
  }, [])

  const refreshLibrary = useCallback(async () => {
    try {
      setLibrary(await api.library())
      setLibraryError(null)
    } catch (e) {
      setLibraryError(e)
    }
  }, [])

  const refreshQueue = useCallback(async () => {
    try {
      const res = await api.jobs()
      setQueue(res.jobs)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    void refreshMeta()
    void refreshLibrary()
    void refreshQueue()
  }, [refreshMeta, refreshLibrary, refreshQueue])

  // 当有正在跑或排队的任务时勤轮询（3秒），否则慢轮询（12秒）
  useEffect(() => {
    const hasActive = queue.some((j) => j.status === 'running' || j.status === 'queued')
    const t = setInterval(refreshQueue, hasActive ? 3000 : 12000)
    return () => clearInterval(t)
  }, [refreshQueue, queue])

  useEffect(() => {
    const mq = matchMedia('(prefers-color-scheme: dark)')
    const on = () => setSystemDark(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
  }, [theme])

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t)
    try {
      if (t === 'system') localStorage.removeItem('theme')
      else localStorage.setItem('theme', t)
    } catch { /* ignore */ }
  }, [])

  const isDark = theme === 'dark' || (theme === 'system' && systemDark)
  const value = useMemo(
    () => ({ meta, library, libraryError, refreshLibrary, refreshMeta, queue, refreshQueue, theme, isDark, setTheme }),
    [meta, library, libraryError, refreshLibrary, refreshMeta, queue, refreshQueue, theme, isDark, setTheme],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useStore(): Store {
  const s = useContext(Ctx)
  if (!s) throw new Error('useStore outside provider')
  return s
}
