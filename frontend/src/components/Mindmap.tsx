import { Markmap } from 'markmap-view'
import { useEffect, useRef, useState } from 'react'
import type { MindmapNode } from '../api'

/** markmap 渲染。支持全屏模式、居中、复制 Markdown 大纲、下载 SVG。 */
export function Mindmap({ tree, title, markdown }: { tree: MindmapNode; title: string; markdown?: string }) {
  const svgRef = useRef<SVGSVGElement>(null)
  const mmRef = useRef<Markmap | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    mmRef.current?.destroy()
    svg.innerHTML = ''
    mmRef.current = Markmap.create(svg, {
      autoFit: true, duration: 300, maxWidth: 320, spacingVertical: 8, paddingX: 12, initialExpandLevel: 3,
    }, tree)
    return () => { mmRef.current?.destroy(); mmRef.current = null }
  }, [tree])

  // ESC 键退出全屏
  useEffect(() => {
    if (!fullscreen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setFullscreen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [fullscreen])

  // 状态改变或窗口 resize 后重新适配视图
  useEffect(() => {
    const t = setTimeout(() => mmRef.current?.fit(), 120)
    return () => clearTimeout(t)
  }, [fullscreen])

  const fit = () => mmRef.current?.fit()
  const download = () => {
    const svg = svgRef.current
    if (!svg) return
    const s = new XMLSerializer().serializeToString(svg)
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([s], { type: 'image/svg+xml' }))
    a.download = `${title || 'mindmap'}.svg`
    a.click()
  }

  const copyOutline = async () => {
    if (!markdown) return
    try {
      await navigator.clipboard.writeText(markdown)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* ignore */ }
  }

  return (
    <div className={`mm${fullscreen ? ' fullscreen' : ''}`}>
      <svg ref={svgRef} />
      <div className="bar2">
        <button className="btn sm" onClick={fit}>居中</button>
        {markdown && (
          <button className="btn sm" onClick={copyOutline}>
            {copied ? '已复制大纲' : '复制大纲'}
          </button>
        )}
        <button className="btn sm" onClick={() => setFullscreen((f) => !f)}>
          {fullscreen ? '退出全屏 (ESC)' : '全屏'}
        </button>
        <button className="btn sm" onClick={download}>下载 SVG</button>
      </div>
    </div>
  )
}
