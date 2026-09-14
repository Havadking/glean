import { Pause, Play, Volume1, Volume2, VolumeX } from 'lucide-react'
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { fmtDuration } from '../lib/format'

export interface AudioHandle {
  /** 跳到某一秒；play 为 true 时顺便开始播 */
  seek(sec: number, play?: boolean): void
}

const RATES = [1, 1.25, 1.5, 2]
const STEP_SEC = 5
const VOLUME_STEP = 0.1

function savedRate(): number {
  try {
    const n = Number(localStorage.getItem('vsum.rate'))
    if (RATES.includes(n)) return n
  } catch { /* ignore */ }
  return 1
}

function savedVolume(): number {
  try {
    const n = Number(localStorage.getItem('vsum.volume'))
    if (localStorage.getItem('vsum.volume') != null && n >= 0 && n <= 1) return n
  } catch { /* ignore */ }
  return 1
}

function savedMuted(): boolean {
  try { return localStorage.getItem('vsum.muted') === '1' } catch { return false }
}

/** 键盘事件是不是发给某个控件的——那种情况下空格和方向键别抢 */
function inControl(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  return !!el?.closest?.('input, textarea, select, button, a, [contenteditable="true"]')
}

/**
 * 详情页底栏的极简音频播放器：播放/暂停、进度、音量、倍速。
 * 空格播放暂停，← → 前后 5 秒，↑ ↓ 音量，M 静音。播到哪一段通过 onTime 回给父组件高亮。
 * 音量、静音、倍速都记在浏览器里，换视频不用重调。
 */
export const AudioBar = forwardRef<AudioHandle, { src: string; onTime?: (sec: number) => void }>(function AudioBar({ src, onTime }, ref) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [rate, setRate] = useState<number>(savedRate)
  const [volume, setVolume] = useState<number>(savedVolume)
  const [muted, setMuted] = useState<boolean>(savedMuted)
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

  useEffect(() => {
    const a = audioRef.current
    if (a) { a.volume = volume; a.muted = muted }
    try {
      localStorage.setItem('vsum.volume', String(volume))
      localStorage.setItem('vsum.muted', muted ? '1' : '0')
    } catch { /* ignore */ }
  }, [volume, muted])

  const toggle = () => {
    const a = audioRef.current
    if (!a) return
    if (a.paused) void a.play().catch(() => { /* ignore */ })
    else a.pause()
  }
  // 拖到 0 等于静音；静音时再动滑块就解除静音
  const changeVolume = (v: number) => {
    const clamped = Math.min(1, Math.max(0, Math.round(v * 100) / 100))
    setVolume(clamped)
    setMuted(clamped === 0)
  }
  const toggleMute = () => {
    if (muted || volume === 0) { setMuted(false); if (volume === 0) setVolume(0.5) }
    else setMuted(true)
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || inControl(e.target)) return
      const a = audioRef.current
      if (!a) return
      if (e.key === ' ' || e.code === 'Space') { e.preventDefault(); toggle() }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); a.currentTime = Math.max(0, a.currentTime - STEP_SEC) }
      else if (e.key === 'ArrowRight') { e.preventDefault(); a.currentTime = Math.min(a.duration || Infinity, a.currentTime + STEP_SEC) }
      else if (e.key === 'ArrowUp') { e.preventDefault(); changeVolume((muted ? 0 : volume) + VOLUME_STEP) }
      else if (e.key === 'ArrowDown') { e.preventDefault(); changeVolume(volume - VOLUME_STEP) }
      else if (e.key === 'm' || e.key === 'M') { e.preventDefault(); toggleMute() }
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
          onLoadedMetadata={(e) => { setDuration(e.currentTarget.duration || 0); e.currentTarget.playbackRate = rate; e.currentTarget.volume = volume; e.currentTarget.muted = muted }}
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
        <div className="vol" title={muted ? '已静音（M）' : `音量 ${Math.round(volume * 100)}%（↑ ↓ 调节，M 静音）`}>
          <button className="iconbtn" onClick={toggleMute} aria-label={muted ? '取消静音' : '静音'}>
            {muted || volume === 0 ? <VolumeX /> : volume < 0.5 ? <Volume1 /> : <Volume2 />}
          </button>
          <input
            type="range" min={0} max={1} step={0.01} value={muted ? 0 : volume}
            aria-label="音量"
            style={{ '--p': `${(muted ? 0 : volume) * 100}%` } as React.CSSProperties}
            onChange={(e) => changeVolume(Number(e.target.value))}
          />
        </div>
        <button className="btn ghost sm rate" onClick={cycleRate} title="切换倍速">{rate}x</button>
        <span className="hint">空格 播放/暂停 · ← → 5 秒 · ↑ ↓ 音量 · 点时间戳跳过去听</span>
      </div>
    </div>
  )
})
