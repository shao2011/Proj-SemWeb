import { useEffect, useRef, useState } from 'react'

export interface OutlineEntry {
  id: string
  preview: string
}

interface ThreadOutlineProps {
  entries: OutlineEntry[]
  onJump: (id: string) => void
}

// The rail is a fixed 36x70 box, which fits this many dashes; the popup
// always lists every turn, so nothing becomes unreachable.
const MAX_DASHES = 8

export function ThreadOutline({ entries, onJump }: ThreadOutlineProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  if (entries.length < 2) return null

  return (
    <div className="outline" ref={rootRef}>
      <button
        className="outline-rail"
        aria-label="Conversation outline"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        {entries.slice(0, MAX_DASHES).map((entry) => (
          <span key={entry.id} className="outline-dash" />
        ))}
      </button>
      {open && (
        <div className="outline-pop" role="menu" aria-label="Jump to message">
          {entries.map((entry) => (
            <button
              key={entry.id}
              className="outline-item"
              role="menuitem"
              title={entry.preview}
              onClick={() => {
                setOpen(false)
                onJump(entry.id)
              }}
            >
              {entry.preview}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
