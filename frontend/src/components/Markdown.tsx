import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useMemo } from 'react'

marked.setOptions({ gfm: true, breaks: false })

/** 总结正文是模型写的 Markdown；渲染前过一遍 DOMPurify。 */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false }) as string), [text])
  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />
}
