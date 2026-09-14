import DOMPurify from 'dompurify'
import { marked } from 'marked'

const IDX_RE = /【(\d{1,3})】/g

/**
 * 【2】这类引用变成链接，指到那条视频。问 UP 主和回顾页共用。
 * compact 只显示编号（标题放 title 里）—— 回顾里引用很密，带标题的话正文就看不见了。
 */
export function citeHtml(answer: string, byIndex: Map<number, { video_id: string; title: string }>, compact = false): string {
  const html = answer.replace(IDX_RE, (_m, n: string) => {
    const v = byIndex.get(Number(n))
    if (!v) return `<span class="cite">${n}</span>`
    const t = v.title.replace(/"/g, '&quot;')
    if (compact) return `<a class="cite" href="/video/${encodeURIComponent(v.video_id)}" title="${t}">${n}</a>`
    return `<a class="cite vcite" href="/video/${encodeURIComponent(v.video_id)}" title="${t}">${n} · ${t.length > 18 ? t.slice(0, 18) + '…' : t}</a>`
  })
  return DOMPurify.sanitize(marked.parse(html, { async: false }) as string)
}
