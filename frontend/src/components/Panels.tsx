import { useEffect, useRef, useState } from 'react'
import type {
  FileProcessingStatus,
  PendingUpload,
  Project,
  ProjectFile,
  Provider,
  Thread,
} from '../lib/types'
import {
  AlertIcon,
  ChatIcon,
  CheckIcon,
  ChevronIcon,
  CloseIcon,
  DocumentsIcon,
  LockIcon,
  MoreIcon,
  PencilIcon,
  PlusIcon,
  SearchIcon,
  SpinnerIcon,
  StackIcon,
  TrashIcon,
  UserIcon,
} from './icons'

type ProjectTab = 'yours' | 'shared'
type ProjectSort = 'updated' | 'created' | 'name'

const TABS: { key: ProjectTab; label: string }[] = [
  { key: 'yours', label: 'Your projects' },
  { key: 'shared', label: 'Shared with you' },
]

const SORTS: { key: ProjectSort; label: string }[] = [
  { key: 'updated', label: 'Last updated' },
  { key: 'created', label: 'Date created' },
  { key: 'name', label: 'Name' },
]

interface ProjectsPanelProps {
  projects: Project[]
  activeProjectId: string | null
  providers: Provider[]
  onSelect: (projectId: string) => void
  onNewProject: () => void
  onDelete: (projectId: string) => void
}

export function ProjectsPanel({
  projects,
  activeProjectId,
  providers,
  onSelect,
  onNewProject,
  onDelete,
}: ProjectsPanelProps) {
  const [tab, setTab] = useState<ProjectTab>('yours')
  const [sort, setSort] = useState<ProjectSort>('updated')
  const [searching, setSearching] = useState(false)
  const [query, setQuery] = useState('')
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (menuFor === null) return
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuFor(null)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuFor(null)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [menuFor])

  const needle = query.trim().toLowerCase()
  // One request feeds both tabs: the backend returns owned and shared projects
  // together, each carrying the caller's role.
  const visible = projects
    .filter((item) => (tab === 'yours' ? item.role === 'owner' : item.role !== 'owner'))
    .filter((item) => needle.length === 0 || item.name.toLowerCase().includes(needle))
    .slice()
    .sort((a, b) => {
      if (sort === 'name') return a.name.localeCompare(b.name)
      const field = sort === 'created' ? 'created_at' : 'updated_at'
      return b[field].localeCompare(a[field])
    })

  return (
    <div className="panel wide">
      <div className="panel-head">
        <h2>Projects</h2>
        <div className="panel-actions">
          <button
            className="icon-btn"
            aria-label="Search projects"
            onClick={() => {
              setSearching((prev) => !prev)
              if (searching) setQuery('')
            }}
          >
            <SearchIcon size={17} />
          </button>
          <button className="sort-pill" onClick={() => setSort(nextSort(sort))}>
            Sort by <strong>{SORTS.find((item) => item.key === sort)?.label}</strong>
            <ChevronIcon />
          </button>
          <button className="light-btn" onClick={onNewProject}>
            New project
          </button>
        </div>
      </div>

      {searching && (
        <input
          className="field"
          style={{ width: '100%', marginBottom: 14 }}
          placeholder="Search projects by name"
          aria-label="Search projects"
          autoFocus
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      )}

      <div className="tabs">
        {TABS.map((item) => (
          <button
            key={item.key}
            className={tab === item.key ? 'tab active' : 'tab'}
            aria-current={tab === item.key ? 'page' : undefined}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === 'shared' && visible.length === 0 ? (
        <div className="empty-state">
          <StackIcon />
          <p className="empty-title">Nothing shared with you yet</p>
          <p className="empty-sub">
            When someone adds your email to a project, it shows up here with the
            role they gave you.
          </p>
        </div>
      ) : tab === 'yours' && providers.length === 0 ? (
        <p className="notice error">
          No provider is configured on the backend. Set COHERE_API_KEY,
          VOYAGE_API_KEY or JINA_API_KEY, then run <code>docker compose up -d</code>{' '}
          again.
        </p>
      ) : visible.length === 0 ? (
        <div className="empty-state">
          <StackIcon />
          <p className="empty-title">
            {needle.length > 0 ? 'No project matches that name' : 'Looking to start a project?'}
          </p>
          <p className="empty-sub">
            Upload materials, pin an embedding provider, and keep conversations in
            one place.
          </p>
          {needle.length === 0 && (
            <button className="secondary-btn" onClick={onNewProject}>
              New project
            </button>
          )}
        </div>
      ) : (
        <div className="project-grid">
          {visible.map((project) => {
            const isOpen = menuFor === project.project_id
            return (
              <div
                key={project.project_id}
                className={
                  project.project_id === activeProjectId || isOpen
                    ? 'project-card active'
                    : 'project-card'
                }
              >
                <button
                  className="project-card-hit"
                  onClick={() => onSelect(project.project_id)}
                >
                  <span className="project-card-top">
                    <span className="project-card-name">{project.name}</span>
                    <LockIcon />
                  </span>
                  <span className="project-card-meta">
                    {formatDate(project.updated_at)} ·{' '}
                    {project.embedding_profile.provider}
                  </span>
                  {project.project_id === activeProjectId && (
                    <span className="badge">Active</span>
                  )}
                </button>
                {project.role === 'owner' && (
                <button
                  className={isOpen ? 'project-card-more open' : 'project-card-more'}
                  aria-label={`Options for ${project.name}`}
                  aria-expanded={isOpen}
                  onClick={() => setMenuFor(isOpen ? null : project.project_id)}
                >
                  <MoreIcon />
                </button>
                )}
                {isOpen && project.role === 'owner' && (
                  <div className="project-card-menu" ref={menuRef} role="menu">
                    <button
                      className="recent-menu-item danger"
                      role="menuitem"
                      onClick={() => {
                        setMenuFor(null)
                        onDelete(project.project_id)
                      }}
                    >
                      <TrashIcon />
                      Delete
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function nextSort(current: ProjectSort): ProjectSort {
  const index = SORTS.findIndex((item) => item.key === current)
  return SORTS[(index + 1) % SORTS.length]!.key
}

function formatDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const UPLOAD_ACCEPT = '.pdf,.docx,.xlsx,.pptx'

/**
 * One row in the files rail, shared by a file already known to the backend
 * and one still queued from project creation: a spinner while it is pending
 * or processing, a check once indexed, an alert if it failed.
 */
function RailFileRow({
  name,
  status,
  error,
}: {
  name: string
  status: FileProcessingStatus
  error?: string | null
}) {
  return (
    <li className="rail-file">
      <DocumentsIcon size={16} />
      <span className="rail-file-name" title={name}>
        {name}
      </span>
      {status === 'completed' ? (
        <span className="rail-status-check" title="Indexed">
          <CheckIcon size={12} />
        </span>
      ) : status === 'failed' ? (
        <span className="rail-status-error" title={error ?? 'Failed to index'}>
          <AlertIcon size={15} />
        </span>
      ) : (
        <SpinnerIcon size={15} />
      )}
    </li>
  )
}

interface ProjectPanelProps {
  project: Project
  threads: Thread[]
  files: ProjectFile[]
  pendingUploads: PendingUpload[]
  busy: boolean
  composer: React.ReactNode
  onOpenThread: (threadId: string) => void
  onUpload: (file: File) => void
  onRename: (name: string) => Promise<boolean>
  /** Owner-only; absent for members, who cannot manage the list. */
  onManageMembers?: () => void
}

/**
 * A single project: its own composer, the chats that belong to it, and the
 * indexed files. A project is only reachable from here, so a chat started on
 * the home screen can never pick one up by accident.
 */
export function ProjectPanel({
  project,
  threads,
  files,
  pendingUploads,
  busy,
  composer,
  onOpenThread,
  onUpload,
  onRename,
  onManageMembers,
}: ProjectPanelProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState(project.name)
  const [savingName, setSavingName] = useState(false)

  async function saveName() {
    const trimmed = nameDraft.trim()
    if (trimmed.length === 0 || trimmed === project.name) {
      setEditingName(false)
      return
    }
    setSavingName(true)
    const ok = await onRename(trimmed)
    setSavingName(false)
    if (ok) setEditingName(false)
  }

  const canEdit = project.role !== 'viewer'

  return (
    <div className="panel wide">
      {project.role !== 'owner' && (
        <p className="project-role-note">
          Shared with you as {project.role === 'editor' ? 'an editor' : 'a viewer'}.
          {project.role === 'viewer'
            ? ' You can read the documents and ask questions.'
            : ' You can add and remove documents.'}
        </p>
      )}
      <div className="project-title">
        {editingName ? (
          <div className="project-name-edit">
            <input
              className="field project-name-input"
              value={nameDraft}
              autoFocus
              disabled={savingName}
              onChange={(event) => setNameDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void saveName()
                if (event.key === 'Escape') {
                  setNameDraft(project.name)
                  setEditingName(false)
                }
              }}
            />
            <button
              className="icon-btn"
              aria-label="Save project name"
              disabled={savingName}
              onClick={() => void saveName()}
            >
              <CheckIcon />
            </button>
            <button
              className="icon-btn"
              aria-label="Cancel rename"
              disabled={savingName}
              onClick={() => {
                setNameDraft(project.name)
                setEditingName(false)
              }}
            >
              <CloseIcon size={15} />
            </button>
          </div>
        ) : (
          <div className="project-name-edit">
            <h2>{project.name}</h2>
            {canEdit && (
              <button
                className="icon-btn project-name-trigger"
                aria-label="Rename project"
                onClick={() => {
                  setNameDraft(project.name)
                  setEditingName(true)
                }}
              >
                <PencilIcon />
              </button>
            )}
          </div>
        )}
        <p className="project-meta">
          <LockIcon />
          {project.role !== 'owner'
            ? 'Shared with you'
            : project.member_count === 0
              ? 'Private'
              : `Shared with ${project.member_count} ${
                  project.member_count === 1 ? 'person' : 'people'
                }`}{' '}
          · created {formatDate(project.created_at)}
        </p>
      </div>

      <div className="project-layout">
        <div className="project-main">
          {composer}

          <h3 className="project-section">Chats and tasks</h3>
          {threads.length === 0 ? (
            <p className="empty-hint">
              No chats in this project yet. Send a message above and it answers
              from these files.
            </p>
          ) : (
            threads.map((thread) => (
              <button
                className="card"
                key={thread.thread_id}
                onClick={() => onOpenThread(thread.thread_id)}
              >
                <ChatIcon size={16} />
                <span className="card-main">
                  <span className="card-title">{thread.title}</span>
                  <span className="card-sub">
                    {new Date(thread.updated_at).toLocaleString()}
                  </span>
                </span>
              </button>
            ))
          )}
        </div>

        <aside className="project-rail">
          <div className="rail-card">
            <div className="rail-head">
              <span className="rail-title">Files</span>
              {onManageMembers !== undefined && (
                <button
                  className="icon-btn"
                  aria-label="People in this project"
                  title="People in this project"
                  onClick={onManageMembers}
                >
                  <UserIcon size={16} />
                </button>
              )}
              {canEdit && (
                <button
                  className="icon-btn"
                  aria-label="Upload a document"
                  title="Upload PDF, DOCX, XLSX or PPTX"
                  disabled={busy}
                  onClick={() => fileRef.current?.click()}
                >
                  <PlusIcon size={16} />
                </button>
              )}
              <input
                ref={fileRef}
                type="file"
                accept={UPLOAD_ACCEPT}
                data-testid="rail-file-input"
                style={{ display: 'none' }}
                onChange={(event) => {
                  const picked = event.target.files?.[0]
                  if (picked) onUpload(picked)
                  event.target.value = ''
                }}
              />
            </div>

            {pendingUploads.length === 0 && files.length === 0 ? (
              <p className="rail-empty">
                Add PDF, DOCX, XLSX or PPTX files to reference in this project.
              </p>
            ) : (
              <ul className="rail-files">
                {pendingUploads.length > 0
                  ? pendingUploads.map((item) => (
                      <RailFileRow
                        key={item.key}
                        name={item.name}
                        status={
                          item.status === 'uploading'
                            ? 'processing'
                            : item.status === 'done'
                              ? 'completed'
                              : 'failed'
                        }
                      />
                    ))
                  : files.map((file) => (
                      <RailFileRow
                        key={file.file_id}
                        name={file.filename}
                        status={file.status}
                        error={file.error}
                      />
                    ))}
              </ul>
            )}
          </div>
        </aside>
      </div>
    </div>
  )
}

interface ChatsPanelProps {
  threads: Thread[]
  onSelect: (threadId: string) => void
  onDelete: (threadId: string) => void
}

export function ChatsPanel({ threads, onSelect, onDelete }: ChatsPanelProps) {
  const [filter, setFilter] = useState('')
  const needle = filter.trim().toLowerCase()
  const visible =
    needle.length === 0
      ? threads
      : threads.filter((thread) => thread.title.toLowerCase().includes(needle))

  return (
    <div className="panel">
      <h2>Your chat history</h2>
      <p className="lede">{threads.length} conversation{threads.length === 1 ? '' : 's'} stored in this browser.</p>

      <input
        className="field"
        style={{ width: '100%', marginBottom: 16 }}
        placeholder="Search conversations"
        aria-label="Search conversations"
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />

      {visible.length === 0 ? (
        <p className="empty-hint">Nothing here.</p>
      ) : (
        visible.map((thread) => (
          <div className="card" key={thread.thread_id}>
            <span className="card-main">
              <span className="card-title">{thread.title}</span>
              <span className="card-sub">
                {thread.project_id === null ? 'Plain chat' : 'Project chat'} ·{' '}
                {new Date(thread.updated_at).toLocaleString()}
              </span>
            </span>
            <button className="icon-btn" onClick={() => onSelect(thread.thread_id)}>
              Open
            </button>
            <button
              className="icon-btn"
              aria-label={`Delete ${thread.title}`}
              onClick={() => onDelete(thread.thread_id)}
            >
              <TrashIcon />
            </button>
          </div>
        ))
      )}
    </div>
  )
}

interface SettingsPanelProps {
  userId: string
  providers: Provider[]
  onSaveUserId: (userId: string) => void
}

export function SettingsPanel({ userId, providers, onSaveUserId }: SettingsPanelProps) {
  const [draft, setDraft] = useState(userId)
  const changed = draft.trim().length > 0 && draft.trim() !== userId

  return (
    <div className="panel">
      <h2>Customize</h2>

      <p className="lede">
        Your user ID is sent as the X-User-ID header. Projects and documents are private to it.
      </p>

      <div className="form-row" style={{ marginTop: 0 }}>
        <input
          className="field"
          aria-label="User ID"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          className="primary-btn"
          disabled={!changed}
          onClick={() => onSaveUserId(draft.trim())}
        >
          Save
        </button>
      </div>

      <div className="notice">
        Providers configured on the backend:{' '}
        {providers.length === 0 ? 'none' : providers.join(', ')}
      </div>
    </div>
  )
}
