import { useId, useMemo } from 'react'
import { useStore } from '../store'

/** 主播 / UP 主输入框：下拉里是库里已有的人，也能直接敲个新名字。选已有的人就共享他的词表和分组。 */
export function UploaderInput({ value, onChange, autoFocus, onKeyDown, placeholder = '选已有的或输入新名字' }: {
  value: string
  onChange: (v: string) => void
  autoFocus?: boolean
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void
  placeholder?: string
}) {
  const { library } = useStore()
  const listId = useId()
  const names = useMemo(() => {
    const seen = new Map<string, number>()
    for (const g of library?.groups ?? []) if (g.uploader) seen.set(g.uploader, g.entries.length)
    // 视频多的排前面
    return [...seen.entries()].sort((a, b) => b[1] - a[1]).map(([n]) => n)
  }, [library])
  return (
    <>
      <input className="tin" list={listId} value={value} onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} autoFocus={autoFocus} onKeyDown={onKeyDown} spellCheck={false} aria-label="主播" />
      <datalist id={listId}>{names.map((n) => <option key={n} value={n} />)}</datalist>
    </>
  )
}
