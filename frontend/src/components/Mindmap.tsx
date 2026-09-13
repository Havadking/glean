import { Markmap } from 'markmap-view'
import { useEffect, useRef } from 'react'
import type { MindmapNode } from '../api'

/** markmap 渲染。树一变就整个重画（节点不多，没必要做增量）。 */
export function Mindmap({ tree, title }: { tree: MindmapNode; title: string }) {
  const svgRef = useRef<SVGSVGElement>(null)
  const mmRef = useRef<Markmap | null>(null)

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

  return (
    <div className="mm">
      <svg ref={svgRef} />
      <div className="bar2">
        <button className="btn sm" onClick={fit}>居中</button>
        <button className="btn sm" onClick={download}>下载 SVG</button>
      </div>
    </div>
  )
}
