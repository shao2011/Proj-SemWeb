import { useEffect, useMemo, useRef, useState } from 'react'
import type { Project, Thread } from '../lib/types'
import { ChatIcon, CloseIcon, ProjectsIcon, SearchIcon } from './icons'

interface SearchDialogProps {
  threads: Thread[]
  projects: Project[]
  onSelectThread: (threadId: string) => void
  onSelectProject: (projectId: string) => void
  onClose: () => void
}

interface ResultItem {
  kind: 'thread' | 'project'
  id: string
  title: string
  updatedAt: string
}

const MAX_RESULTS = 30
const DAY_MS = 86_400_000

function relativeLabel(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const days = Math.floor((Date.now() - date.getTime()) / DAY_MS)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return 'Past week'
  if (days < 30) return 'Past month'
  return 'Older'
}

export function SearchDialog({
  threads,
  projects,
  onSelectThread,
  onSelectProject,
  onClose,
}: SearchDialogProps) {
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])

  const results = useMemo<ResultItem[]>(() => {
    const needle = query.trim().toLowerCase()
    const items: ResultItem[] = [
      ...threads.map((thread) => ({
        kind: 'thread' as const,
        id: thread.thread_id,
        title: thread.title,
        updatedAt: thread.updated_at,
      })),
      ...projects.map((project) => ({
        kind: 'project' as const,
        id: project.project_id,
        title: project.name,
        updatedAt: project.updated_at,
      })),
    ]
    return items
      .filter((item) => needle.length === 0 || item.title.toLowerCase().includes(needle))
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
      .slice(0, MAX_RESULTS)
  }, [threads, projects, query])

  useEffect(() => {
    setActiveIndex(0)
  }, [results.length, query])

  function select(item: ResultItem) {
    if (item.kind === 'thread') onSelectThread(item.id)
    else onSelectProject(item.id)
    onClose()
  }

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        className="search-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Search chats and projects"
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown') {
            event.preventDefault()
            setActiveIndex((index) => Math.min(index + 1, results.length - 1))
          } else if (event.key === 'ArrowUp') {
            event.preventDefault()
            setActiveIndex((index) => Math.max(index - 1, 0))
          } else if (event.key === 'Enter') {
            const item = results[activeIndex]
            if (item) select(item)
          }
        }}
      >
        <div className="search-input-row">
          <SearchIcon size={17} />
          <input
            ref={inputRef}
            className="search-input"
            type="text"
            id="global-search"
            name="search"
            placeholder="Search chats and projects"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>

        {results.length === 0 ? (
          <p className="search-empty">No results found</p>
        ) : (
          <div className="search-results">
            {results.map((item, index) => (
              <button
                key={`${item.kind}:${item.id}`}
                className={index === activeIndex ? 'search-result-row active' : 'search-result-row'}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => select(item)}
              >
                {item.kind === 'thread' ? <ChatIcon size={16} /> : <ProjectsIcon size={16} />}
                <span className="search-result-title">{item.title}</span>
                {index === activeIndex && <span className="search-result-hint">Enter</span>}
                <span className="search-result-time">{relativeLabel(item.updatedAt)}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
