import { useState } from 'react'
import type { ProjectFile } from '../lib/types'
import { AlertIcon, DocumentsIcon, SearchIcon, SpinnerIcon } from './icons'

export interface LibraryEntry {
  file: ProjectFile
  projectName: string
}

/**
 * Every file today comes from an upload; chatbot-generated files land here
 * once that feature ships, carrying their own source on the backend record.
 */
function sourceOf(_file: ProjectFile): 'uploaded' | 'generated' {
  return 'uploaded'
}

type SourceFilter = 'all' | 'uploaded' | 'generated'

const FILTERS: { id: SourceFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'uploaded', label: 'Uploaded' },
  { id: 'generated', label: 'Generated' },
]

interface LibraryPanelProps {
  entries: LibraryEntry[]
  loading: boolean
}

export function LibraryPanel({ entries, loading }: LibraryPanelProps) {
  const [filter, setFilter] = useState<SourceFilter>('all')
  const [query, setQuery] = useState('')

  const q = query.trim().toLowerCase()
  const visible = entries.filter(({ file }) => {
    if (filter !== 'all' && sourceOf(file) !== filter) return false
    if (q.length > 0 && !file.filename.toLowerCase().includes(q)) return false
    return true
  })

  return (
    <div className="panel wide">
      <div className="panel-head">
        <h2>Library</h2>
        <div className="panel-actions">
          <div className="library-search">
            <SearchIcon size={15} />
            <input
              value={query}
              placeholder="Search"
              aria-label="Search library"
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
        </div>
      </div>

      <div className="tabs">
        {FILTERS.map(({ id, label }) => (
          <button
            key={id}
            className={filter === id ? 'tab active' : 'tab'}
            aria-current={filter === id ? 'page' : undefined}
            onClick={() => setFilter(id)}
          >
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="empty-hint">Loading documents…</p>
      ) : visible.length === 0 ? (
        <div className="empty-state">
          <DocumentsIcon size={40} />
          <p className="empty-title">
            {entries.length === 0 ? 'No documents yet' : 'Nothing matches'}
          </p>
          <p className="empty-sub">
            {entries.length === 0
              ? 'Files you upload to a project, and files the chatbot creates for you, will show up here.'
              : filter === 'generated'
                ? 'The chatbot has not created any files yet.'
                : 'Try a different search or filter.'}
          </p>
        </div>
      ) : (
        <div className="library-list">
          {visible.map(({ file, projectName }) => (
            <div className="card library-row" key={file.file_id}>
              {file.status === 'failed' ? (
                <span className="rail-status-error">
                  <AlertIcon size={16} />
                </span>
              ) : file.status === 'completed' ? (
                <DocumentsIcon size={16} />
              ) : (
                <SpinnerIcon size={16} />
              )}
              <span className="card-main">
                <span className="card-title" title={file.filename}>
                  {file.filename}
                </span>
                <span className="card-sub">
                  {projectName} · {new Date(file.created_at).toLocaleDateString()}
                </span>
              </span>
              <span className="badge">{sourceOf(file)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
