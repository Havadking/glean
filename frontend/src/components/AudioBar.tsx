import { Pause, Play } from 'lucide-react'
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { fmtDuration } from '../lib/format'

export interface AudioHandle {
  /** 跳到某一秒；play 为 true 时顺便开始播 */
  seek(sec: number, play?: boolean): void
}

const RATES = [1, 1.25, 1.5, 2]
const STEP_SEC = 5

function savedRate(): number {
  try {
    const n = Number(localStorage.getItem('vsum.rate'))
    if (RATES.includes(n)) return n
  } catch { /* ignore */ }
  return 1
}

/** 键盘事件是不是发给某个控件的——那种情况下空格和方向键别抢 */
function inControl(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  return !!el?.closest?.('input, textarea, select, button, a, [contenteditable="true"]')
}

/**
 * 详情页底栏的极简音频播放器：播放/暂停、进度、倍速。
 * 空格播放暂停，← → 前后 5 秒。播到哪一段通过 onTime 回给父组件高亮。
 */
export const AudioBar = forwardRef<AudioHandle, { src: string; onTime?: (sec: number) => void }>(function AudioBar({ src, onTime }, ref) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [rate, setRate] = useState<number>(savedRate)
  const [dragging, setDragging] = useState<number | null>(null)

  useImperativeHandle(ref, () => ({
    seek(sec, play) {
      const a = audioRef.current
      if (!a) return
      a.currentTime = Math.max(0, sec)
      setTime(a.currentTime)
      if (play) void a.play().catch(() => { /* 浏览器拦了就算了 */ })
    },
  }), [])

  useEffect(() => {
    const a = audioRef.current
    if (a) a.playbackRate = rate
    try { localStorage.setItem('vsum.rate', String(rate)) } catch { /* ignore */ }
  }, [rate])

  const toggle = () => {
    const a = audioRef.current
    if (!a) return
    if (a.paused) void a.play().catch(() => { /* ignore */ })
    else a.pause()
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || inControl(e.target)) return
      const a = audioRef.current
      if (!a) return
      if (e.key === ' ' || e.code === 'Space') { e.preventDefault(); toggle() }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); a.currentTime = Math.max(0, a.currentTime - STEP_SEC) }
      else if (e.key === 'ArrowRight') { e.preventDefault(); a.currentTime = Math.min(a.duration || Infinity, a.currentTime + STEP_SEC) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const cycleRate = () => setRate((r) => RATES[(RATES.indexOf(r) + 1) % RATES.length])

  const shown = dragging ?? time
  return (
    <div className="abar" role="region" aria-label="音频播放器">
      <div className="in">
        <audio
          ref={audioRef} src={src} preload="metadata"
          onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
          onLoadedMetadata={(e) => { setDuration(e.currentTarget.duration || 0); e.currentTarget.playbackRate = rate }}
          onDurationChange={(e) => setDuration(e.currentTarget.duration || 0)}
          onTimeUpdate={(e) => { const t = e.currentTarget.currentTime; setTime(t); onTime?.(t) }}
          onEnded={() => setPlaying(false)}
        />
        <button className="iconbtn play" onClick={toggle} title={playing ? '暂停（空格）' : '播放（空格）'} aria-label={playing ? '暂停' : '播放'}>
          {playing ? <Pause /> : <Play />}
        </button>
        <span className="mono t">{fmtDuration(shown)}</span>
        <input
          type="range" min={0} max={duration || 0} step={0.1} value={Math.min(shown, duration || 0)}
          aria-label="进度"
          style={{ '--p': duration ? `${(Math.min(shown, duration) / duration) * 100}%` : '0%' } as React.CSSProperties}
          onChange={(e) => setDragging(Number(e.target.value))}
          onPointerUp={() => { if (dragging != null && audioRef.current) audioRef.current.currentTime = dragging; setDragging(null) }}
          onKeyUp={() => { if (dragging != null && audioRef.current) audioRef.current.currentTime = dragging; setDragging(null) }}
        />
        <span className="mono t">{fmtDuration(duration)}</span>
        <button className="btn ghost sm rate" onClick={cycleRate} title="切换倍速">{rate}x</button>
        <span className="hint">空格 播放/暂停 · ← → 5 秒 · 点时间戳跳过去听</span>
      </div>
    </div>
  )
})
