import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, displayTitle, type Video } from '../api'
import { Study } from '../components/Study'
import { ErrorBox } from '../components/ui'

/** 伴读：独立全屏页，不在侧栏的壳里（DESIGN.md v0.9）。 */
export function StudyPage() {
  const { id = '' } = useParams()
  const [video, setVideo] = useState<Video | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    let alive = true
    setVideo(null); setError(null)
    api.video(id).then((v) => { if (alive) setVideo(v) }).catch((e) => { if (alive) setError(e) })
    return () => { alive = false }
  }, [id])

  useEffect(() => {
    if (video) document.title = `伴读 · ${displayTitle(video)}`
    return () => { document.title = '拾光笺' }
  }, [video])

  if (error) return <div className="page"><ErrorBox error={error} /><p><Link to="/library">回到库</Link></p></div>
  if (!video) return <div className="page" style={{ color: 'var(--mute)' }}><span className="spin" /> 读取中…</div>
  return <Study key={video.video_id} video={video} />
}
