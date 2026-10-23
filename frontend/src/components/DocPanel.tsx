import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeHighlight from 'rehype-highlight'
import { getDocumentMarkdown, getDocumentMedia, listDocuments } from '../lib/api'
import type { Project, ProjectFile } from '../lib/types'
import { DISCARD_EDITS_PROMPT, DocBlockEditor } from './DocBlockEditor'
import { AlertIcon, PanelRightIcon, PencilIcon, SpinnerIcon } from './icons'

/**
 * A citation jump request: which document to show and which blocks to
 * highlight. `nonce` distinguishes two clicks on the same citation, so the
 * panel re-scrolls even when nothing else changed.
 */
export interface DocFocus {
  docId: string
  blockIds: string[]
  nonce: number
}

interface DocPanelProps {
  userId: string
  project: Project
  focus?: DocFocus | null
}

type Tab = 'preview' | 'markdown'

const REMARK_PLUGINS = [remarkGfm, remarkMath]
const REHYPE_PLUGINS = [rehypeKatex, rehypeHighlight]

const ABSOLUTE_SRC = /^(?:[a-z]+:|\/)/i

/**
 * An image embedded in a document, e.g. a picture sitting on a spreadsheet.
 *
 * The markdown holds the media store key, not a URL, and the route serving it
 * needs the identity headers -- so the browser cannot fetch it by itself and
 * the bytes are pulled here and handed over as an object URL.
 */
function DocImage({
  userId,
  projectId,
  src,
  alt,
}: {
  userId: string
  projectId: string
  src?: string
  alt?: string
}) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (src === undefined || src === '') return
    if (ABSOLUTE_SRC.test(src)) {
      setUrl(src)
      return
    }
    let cancelled = false
    let objectUrl: string | null = null
    setUrl(null)
    setFailed(false)
    getDocumentMedia(userId, projectId, src)
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl)
    }
  }, [userId, projectId, src])

  if (failed) return <span className="doc-image-missing">{alt ?? 'Image'}</span>
  if (url === null) return <span className="doc-image-loading">{alt ?? 'Image'}</span>
  return <img className="doc-image" src={url} alt={alt ?? ''} />
}

const WIDTH_KEY = 'rag.docPanelWidth'
const MIN_WIDTH = 320
const DEFAULT_WIDTH = 480

function maxWidth(): number {
  return Math.min(900, Math.round(window.innerWidth * 0.7))
}

function initialWidth(): number {
  const saved = Number(localStorage.getItem(WIDTH_KEY))
  if (!Number.isFinite(saved) || saved < MIN_WIDTH) return DEFAULT_WIDTH
  return Math.min(saved, maxWidth())
}

export function DocPanel({ userId, project, focus = null }: DocPanelProps) {
  const [files, setFiles] = useState<ProjectFile[]>([])
  // Until the list answers, an empty `files` means "not loaded", not "none".
  const [filesLoaded, setFilesLoaded] = useState(false)
  const [filesOpen, setFilesOpen] = useState(false)
  const [width, setWidth] = useState(initialWidth)
  const [docId, setDocId] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('preview')
  const [markdown, setMarkdown] = useState<string | null>(null)
  const [anchors, setAnchors] = useState<Record<string, [number, number]>>({})
  const [error, setError] = useState<string | null>(null)
  const [editTarget, setEditTarget] = useState<{ projectId: string; docId: string } | null>(null)
  const [editDirty, setEditDirty] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const highlightRef = useRef<HTMLDivElement>(null)

  const components: Components = useMemo(
    () => ({
      img: ({ src, alt }) => (
        <DocImage
          userId={userId}
          projectId={project.project_id}
          src={typeof src === 'string' ? src : undefined}
          alt={alt}
        />
      ),
    }),
    [userId, project.project_id],
  )

  function applyWidth(next: number) {
    const clamped = Math.min(Math.max(next, MIN_WIDTH), maxWidth())
    setWidth(clamped)
    localStorage.setItem(WIDTH_KEY, String(clamped))
  }

  function startResize(event: React.MouseEvent) {
    event.preventDefault()
    const move = (moveEvent: MouseEvent) => {
      applyWidth(window.innerWidth - moveEvent.clientX)
    }
    const stop = () => {
      document.removeEventListener('mousemove', move)
      document.removeEventListener('mouseup', stop)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    document.addEventListener('mousemove', move)
    document.addEventListener('mouseup', stop)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }

  useEffect(() => {
    let cancelled = false
    setFilesLoaded(false)
    listDocuments(userId, project.project_id)
      .then((loaded) => {
        if (!cancelled) setFiles(loaded)
      })
      .catch(() => {
        if (!cancelled) setFiles([])
      })
      .finally(() => {
        if (!cancelled) setFilesLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [userId, project.project_id])

  function leaveEdit() {
    setEditTarget(null)
    setEditDirty(false)
  }

  // A citation jump steers the panel to its document and the Preview tab.
  useEffect(() => {
    if (focus === null) return
    if (editing && editDirty && !window.confirm(DISCARD_EDITS_PROMPT)) return
    leaveEdit()
    setDocId(focus.docId)
    setTab('preview')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus])

  const readable = files.filter(
    (file) => file.status === 'completed' && file.doc_id !== null,
  )
  const selected = readable.find((file) => file.doc_id === docId) ?? readable[0] ?? null
  const selectedDocId = selected?.doc_id ?? null
  const canEdit = project.role === 'owner' || project.role === 'editor'
  // Edit mode belongs to one document in one project, so any change of either
  // (including a project switch while the old file list is still showing) ends it.
  const editing =
    canEdit &&
    editTarget !== null &&
    editTarget.projectId === project.project_id &&
    editTarget.docId === selectedDocId

  useEffect(() => {
    if (editTarget !== null && !editing) leaveEdit()
  }, [editTarget, editing])

  useEffect(() => {
    if (selectedDocId === null) return
    let cancelled = false
    setMarkdown(null)
    setAnchors({})
    setError(null)
    getDocumentMarkdown(userId, project.project_id, selectedDocId)
      .then((loaded) => {
        if (cancelled) return
        setMarkdown(loaded.markdown)
        setAnchors(loaded.anchors)
      })
      .catch((requestError: unknown) => {
        if (!cancelled) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : 'Could not load this document.',
          )
        }
      })
    return () => {
      cancelled = true
    }
  }, [userId, project.project_id, selectedDocId, reloadKey])

  // The highlighted character range for the focused citation, valid only when
  // the focused document is the one on screen and its anchors are loaded.
  let range: [number, number] | null = null
  if (focus !== null && focus.docId === selectedDocId && markdown !== null) {
    const spans = focus.blockIds
      .map((blockId) => anchors[blockId])
      .filter((span): span is [number, number] => span !== undefined)
    if (spans.length > 0) {
      range = [
        Math.min(...spans.map(([start]) => start)),
        Math.max(...spans.map(([, end]) => end)),
      ]
    }
  }

  useEffect(() => {
    if (range === null) return
    highlightRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.nonce, markdown, tab, editing])

  return (
    <aside className="doc-panel" style={{ width }} aria-label="Project documents">
      <div
        className="doc-resize"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize documents panel"
        tabIndex={0}
        onMouseDown={startResize}
        onKeyDown={(event) => {
          if (event.key === 'ArrowLeft') applyWidth(width + 24)
          else if (event.key === 'ArrowRight') applyWidth(width - 24)
        }}
      />
      <div className="doc-head">
        <span className="doc-title" title={selected?.filename}>
          {selected?.filename ?? 'Documents'}
        </span>
        {!editing && (
          <div className="doc-tabs" role="tablist">
            <button
              role="tab"
              aria-selected={tab === 'preview'}
              className={tab === 'preview' ? 'doc-tab active' : 'doc-tab'}
              onClick={() => setTab('preview')}
            >
              Preview
            </button>
            <button
              role="tab"
              aria-selected={tab === 'markdown'}
              className={tab === 'markdown' ? 'doc-tab active' : 'doc-tab'}
              onClick={() => setTab('markdown')}
            >
              Markdown
            </button>
          </div>
        )}
        {!editing && canEdit && selectedDocId !== null && (
          <button
            className="icon-btn"
            aria-label="Edit document"
            title="Edit document"
            onClick={() => setEditTarget({ projectId: project.project_id, docId: selectedDocId })}
          >
            <PencilIcon size={16} />
          </button>
        )}
        <button
          className={filesOpen ? 'icon-btn active' : 'icon-btn'}
          aria-label="Project files"
          title="Project files"
          aria-pressed={filesOpen}
          onClick={() => setFilesOpen((prev) => !prev)}
        >
          <PanelRightIcon size={17} />
        </button>
      </div>

      <div className="doc-split">
        {editing && selectedDocId !== null ? (
          <DocBlockEditor
            key={selectedDocId}
            userId={userId}
            projectId={project.project_id}
            docId={selectedDocId}
            onDirtyChange={setEditDirty}
            onDone={(saved) => {
              leaveEdit()
              if (saved) setReloadKey((key) => key + 1)
            }}
          />
        ) : (
          <div className="doc-body">
            {selected === null && !filesLoaded ? (
              <p className="empty-hint">Loading…</p>
            ) : selected === null ? (
              <p className="empty-hint">No indexed documents in this project yet.</p>
            ) : error !== null ? (
              <p className="doc-error">{error}</p>
            ) : markdown === null ? (
              <p className="empty-hint">Loading…</p>
            ) : tab === 'preview' ? (
              <div className="markdown-body doc-preview">
                {range === null ? (
                  <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={components}>
                    {markdown}
                  </ReactMarkdown>
                ) : (
                  // Split at block-aligned anchor offsets, so each slice is
                  // whole markdown blocks and renders the same as the full text.
                  <>
                    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={components}>
                      {markdown.slice(0, range[0])}
                    </ReactMarkdown>
                    <div className="doc-highlight" ref={highlightRef}>
                      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={components}>
                        {markdown.slice(range[0], range[1])}
                      </ReactMarkdown>
                    </div>
                    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={components}>
                      {markdown.slice(range[1])}
                    </ReactMarkdown>
                  </>
                )}
              </div>
            ) : (
              <pre className="doc-raw">{markdown}</pre>
            )}
          </div>
        )}

        {filesOpen && (
          <div className="doc-files" aria-label="Project files">
            <p className="doc-files-label">Files</p>
            {files.length === 0 ? (
              <p className="empty-hint">No files yet.</p>
            ) : (
              files.map((file) => {
                const openable = file.status === 'completed' && file.doc_id !== null
                const isActive = openable && file.doc_id === selectedDocId
                return (
                  <button
                    key={file.file_id}
                    className={isActive ? 'doc-file-row active' : 'doc-file-row'}
                    disabled={!openable}
                    title={file.error ?? file.filename}
                    onClick={() => {
                      if (file.doc_id === selectedDocId) return
                      if (editing && editDirty && !window.confirm(DISCARD_EDITS_PROMPT)) return
                      leaveEdit()
                      setDocId(file.doc_id)
                    }}
                  >
                    <span className="doc-file-name">{file.filename}</span>
                    {file.status === 'failed' && <AlertIcon size={14} />}
                    {(file.status === 'processing' || file.status === 'pending') && (
                      <SpinnerIcon size={14} />
                    )}
                  </button>
                )
              })
            )}
          </div>
        )}
      </div>
    </aside>
  )
}
