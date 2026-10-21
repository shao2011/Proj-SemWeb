import {
  lazy,
  Suspense,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactElement,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeHighlight from 'rehype-highlight'
import 'katex/dist/katex.min.css'
import 'highlight.js/styles/github-dark.min.css'
import type { ChatMessage, QueryHit } from '../lib/types'
import {
  BranchIcon,
  CheckIcon,
  ChevronIcon,
  CopyIcon,
  FlagIcon,
  MoreIcon,
  PencilIcon,
  RetryIcon,
  ThumbDownIcon,
  ThumbUpIcon,
} from './icons'

// mermaid pulls in its own diagram-layout dependencies and is large; most
// messages never contain a diagram, so it loads on demand instead of
// bloating every chat load.
const Mermaid = lazy(() => import('./Mermaid').then((module) => ({ default: module.Mermaid })))

export interface MessageVersion {
  current: number
  total: number
  onPrev: () => void
  onNext: () => void
}

interface MessageProps {
  message: ChatMessage
  isError?: boolean
  streaming?: boolean
  onEdit?: (text: string) => void
  onRegenerate?: () => void
  onCitationJump?: (hit: QueryHit) => void
  version?: MessageVersion
  /** Public share view: the vote buttons go nowhere there, so they go away. */
  readOnly?: boolean
  /** Branch a new chat ending at this message. Absent while a turn is running. */
  onFork?: () => void
  onReport?: () => void
}

function isElementWithProps(
  node: ReactNode,
): node is ReactElement<{ className?: string; children?: ReactNode }> {
  return typeof node === 'object' && node !== null && 'props' in node
}

function textContent(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textContent).join('')
  if (isElementWithProps(node)) return textContent(node.props.children)
  return ''
}

// --- inline citations --------------------------------------------------------

interface HastNode {
  type: string
  tagName?: string
  value?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}

/**
 * Turns the citation markers the model was instructed to emit ("[cite:1]",
 * "[cite:2]") into <cite data-cite="1"> elements so the renderer below can
 * draw chips. The prefixed form exists so literal bracketed numbers in answer
 * text never collide with citations. Only numbers within the evidence range
 * convert; anything else stays literal text, and code/pre subtrees are never
 * touched.
 */
function rehypeCiteMarkers(options: { max: number }) {
  const pattern = /\[cite:(\d{1,2})\]/g

  function split(value: string): HastNode[] | null {
    pattern.lastIndex = 0
    const out: HastNode[] = []
    let last = 0
    let changed = false
    for (let match = pattern.exec(value); match !== null; match = pattern.exec(value)) {
      const index = Number(match[1])
      if (index < 1 || index > options.max) continue
      changed = true
      if (match.index > last) out.push({ type: 'text', value: value.slice(last, match.index) })
      out.push({
        type: 'element',
        tagName: 'cite',
        properties: { 'data-cite': String(index) },
        children: [{ type: 'text', value: String(index) }],
      })
      last = match.index + match[0].length
    }
    if (!changed) return null
    if (last < value.length) out.push({ type: 'text', value: value.slice(last) })
    return out
  }

  function walk(node: HastNode): void {
    if (node.tagName === 'code' || node.tagName === 'pre') return
    const children = node.children
    if (children === undefined) return
    for (let i = children.length - 1; i >= 0; i -= 1) {
      const child = children[i]!
      if (child.type === 'text' && typeof child.value === 'string') {
        const replaced = split(child.value)
        if (replaced !== null) children.splice(i, 1, ...replaced)
      } else {
        walk(child)
      }
    }
  }

  return () => (tree: HastNode) => walk(tree)
}

/**
 * Rendered in a portal at a fixed position: the chat body is a scroll
 * container, so an absolutely-positioned popover near its edges gets clipped
 * (and can vanish under the sidebar). The position is measured from the chip,
 * clamped into the viewport, and flipped below the chip when the space above
 * is too small.
 */
function CitePop({ anchor, hit }: { anchor: DOMRect; hit: QueryHit }) {
  const ref = useRef<HTMLDivElement>(null)
  const [style, setStyle] = useState<CSSProperties>({ left: -9999, top: 0 })

  useLayoutEffect(() => {
    const node = ref.current
    if (node === null) return
    const { width, height } = node.getBoundingClientRect()
    const left = Math.min(
      Math.max(anchor.left + anchor.width / 2 - width / 2, 8),
      window.innerWidth - width - 8,
    )
    const above = anchor.top - height - 7
    setStyle({ left, top: above >= 8 ? above : anchor.bottom + 7 })
  }, [anchor])

  return createPortal(
    <div className="cite-pop" style={style} role="tooltip" ref={ref}>
      <span className="cite-pop-head">{hit.breadcrumb || hit.doc_id}</span>
      <span className="cite-pop-text">
        {hit.source_text.length > 240 ? `${hit.source_text.slice(0, 240)}…` : hit.source_text}
      </span>
    </div>,
    document.body,
  )
}

function CitationChip({
  index,
  hit,
  onJump,
}: {
  index: number
  hit: QueryHit | undefined
  onJump?: () => void
}) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const chipRef = useRef<HTMLButtonElement>(null)

  // Reloaded messages carry citations but not the live hits, so there is no
  // snippet to pop over or document position to jump to yet — a plain chip
  // still reads far better than a raw [cite:n] marker.
  if (hit === undefined) {
    return <span className="cite-chip static">{index}</span>
  }

  return (
    <span
      className="cite"
      onMouseEnter={() => {
        const rect = chipRef.current?.getBoundingClientRect()
        if (rect !== undefined) setAnchor(rect)
      }}
      onMouseLeave={() => setAnchor(null)}
    >
      <button
        ref={chipRef}
        className="cite-chip"
        aria-label={`Source ${index}: ${hit.breadcrumb || hit.doc_id}`}
        title="Show in document"
        onClick={onJump}
      >
        {index}
      </button>
      {anchor !== null && <CitePop anchor={anchor} hit={hit} />}
    </span>
  )
}

// --- copy --------------------------------------------------------------------

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard access denied (http origin or permissions); nothing to show.
    }
  }

  return (
    <button className="action-btn" aria-label={label} title={label} onClick={() => void copy()}>
      {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

function CodeBlock({ language, code, children }: { language?: string; code: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard unavailable; button silently does nothing.
    }
  }

  return (
    <div className="code-block">
      <div className="code-head">
        <span className="code-lang">{language ?? 'text'}</span>
        <button
          className="code-copy"
          aria-label="Copy code"
          title="Copy code"
          onClick={() => void copy()}
        >
          {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="md-pre">{children}</pre>
    </div>
  )
}

// --- markdown ----------------------------------------------------------------

function markdownComponents(
  streaming: boolean,
  hits: QueryHit[],
  onJump: ((hit: QueryHit) => void) | undefined,
): Components {
  return {
    pre({ children }) {
      const child = Array.isArray(children) ? children[0] : children
      const className = isElementWithProps(child) ? child.props.className : undefined
      const language = /language-(\w+)/.exec(className ?? '')?.[1]
      const code = textContent(child)

      if (language === 'mermaid') {
        return streaming ? (
          <pre className="md-pre">
            <code>{code}</code>
          </pre>
        ) : (
          <Suspense fallback={<div className="mermaid-loading">Loading diagram…</div>}>
            <Mermaid code={code.trim()} />
          </Suspense>
        )
      }
      return (
        <CodeBlock language={language} code={code}>
          {children}
        </CodeBlock>
      )
    },
    code({ className, children }) {
      return <code className={className}>{children}</code>
    },
    a({ href, children }) {
      return (
        <a href={href} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      )
    },
    cite(props) {
      const record = props as Record<string, unknown>
      const raw = record['data-cite'] ?? record['dataCite']
      const index = Number(raw)
      if (!Number.isInteger(index)) return <cite>{props.children}</cite>
      const hit = hits[index - 1]
      return (
        <CitationChip
          index={index}
          hit={hit}
          onJump={hit !== undefined && onJump !== undefined ? () => onJump(hit) : undefined}
        />
      )
    },
  }
}

const MARKDOWN_PLUGINS = [remarkGfm, remarkMath]

function Markdown({
  text,
  streaming,
  hits,
  citeCount,
  onJump,
}: {
  text: string
  streaming: boolean
  hits: QueryHit[]
  citeCount: number
  onJump: ((hit: QueryHit) => void) | undefined
}) {
  const max = Math.max(hits.length, citeCount)
  const rehypePlugins = useMemo(
    () =>
      max > 0
        ? [rehypeKatex, rehypeHighlight, rehypeCiteMarkers({ max })]
        : [rehypeKatex, rehypeHighlight],
    [max],
  )
  const components = useMemo(
    () => markdownComponents(streaming, hits, onJump),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [streaming, hits, onJump],
  )
  return (
    <ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS} rehypePlugins={rehypePlugins} components={components}>
      {text}
    </ReactMarkdown>
  )
}

// --- user message ------------------------------------------------------------

function UserMessage({ message, onEdit }: { message: ChatMessage; onEdit?: (text: string) => void }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(message.text)

  function submit() {
    const text = draft.trim()
    if (text.length === 0) return
    setEditing(false)
    onEdit?.(text)
  }

  if (editing) {
    return (
      <div className="msg user">
        <div className="bubble edit-bubble">
          <textarea
            className="edit-area"
            value={draft}
            rows={Math.min(8, Math.max(2, draft.split('\n').length))}
            autoFocus
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                submit()
              } else if (event.key === 'Escape') {
                setEditing(false)
                setDraft(message.text)
              }
            }}
          />
          <div className="edit-actions">
            <button
              className="action-btn"
              onClick={() => {
                setEditing(false)
                setDraft(message.text)
              }}
            >
              Cancel
            </button>
            <button className="action-btn primary" onClick={submit}>
              Send
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="msg user">
      <div className="user-stack">
        <div className="bubble">{message.text}</div>
        <div className="msg-actions">
          <CopyButton text={message.text} label="Copy message" />
          {onEdit !== undefined && (
            <button
              className="action-btn"
              aria-label="Edit message"
              title="Edit and resend"
              onClick={() => {
                setDraft(message.text)
                setEditing(true)
              }}
            >
              <PencilIcon size={14} />
              Edit
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * Display-only for now: the vote lives in component state and is not sent
 * anywhere, per the current scope. Wire an API call here when feedback
 * persistence lands.
 */
function Feedback() {
  const [vote, setVote] = useState<'up' | 'down' | null>(null)

  return (
    <>
      <button
        className={vote === 'up' ? 'action-btn voted' : 'action-btn'}
        aria-label="Good response"
        aria-pressed={vote === 'up'}
        title="Good response"
        onClick={() => setVote((prev) => (prev === 'up' ? null : 'up'))}
      >
        <ThumbUpIcon size={14} />
      </button>
      <button
        className={vote === 'down' ? 'action-btn voted' : 'action-btn'}
        aria-label="Bad response"
        aria-pressed={vote === 'down'}
        title="Bad response"
        onClick={() => setVote((prev) => (prev === 'down' ? null : 'down'))}
      >
        <ThumbDownIcon size={14} />
      </button>
    </>
  )
}

/**
 * The overflow menu at the right of an assistant message's actions.
 *
 * Flipped upward when the drop-down would be clipped by the bottom of the
 * scroll viewport, the same problem the sidebar's row menu has: the chat body
 * clips its children, so a menu on the last message would otherwise open into
 * nothing.
 */
function MessageMenu({ onFork, onReport }: { onFork?: () => void; onReport: () => void }) {
  const [open, setOpen] = useState(false)
  const [up, setUp] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => {
      if (ref.current !== null && !ref.current.contains(event.target as Node)) {
        setOpen(false)
      }
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

  return (
    <div className="msg-menu-wrap" ref={ref}>
      <button
        className="action-btn"
        aria-label="More actions"
        title="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(event) => {
          const below = window.innerHeight - event.currentTarget.getBoundingClientRect().bottom
          setUp(below < 130)
          setOpen((prev) => !prev)
        }}
      >
        <MoreIcon size={14} />
      </button>
      {open && (
        <div className={up ? 'recent-menu msg-menu up' : 'recent-menu msg-menu'} role="menu">
          {onFork !== undefined && (
            <button
              className="recent-menu-item"
              role="menuitem"
              onClick={() => {
                setOpen(false)
                onFork()
              }}
            >
              <BranchIcon size={16} />
              Continue in a new chat
            </button>
          )}
          <button
            className="recent-menu-item"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              onReport()
            }}
          >
            <FlagIcon size={16} />
            Report message
          </button>
        </div>
      )}
    </div>
  )
}

function VersionPager({ version }: { version: MessageVersion }) {
  return (
    <span className="version-pager">
      <button
        className="pager-btn pager-prev"
        aria-label="Previous version"
        disabled={version.current <= 1}
        onClick={version.onPrev}
      >
        <ChevronIcon size={14} />
      </button>
      <span className="pager-count">
        {version.current}/{version.total}
      </span>
      <button
        className="pager-btn pager-next"
        aria-label="Next version"
        disabled={version.current >= version.total}
        onClick={version.onNext}
      >
        <ChevronIcon size={14} />
      </button>
    </span>
  )
}

export function Message({
  message,
  isError = false,
  streaming = false,
  onEdit,
  onRegenerate,
  onCitationJump,
  version,
  readOnly = false,
  onFork,
  onReport,
}: MessageProps) {
  if (message.role === 'user') {
    return <UserMessage message={message} onEdit={onEdit} />
  }

  const hits = message.hits ?? []
  const className = isError ? 'msg assistant error' : 'msg assistant'

  return (
    <div className={className}>
      <div className="bubble markdown-body">
        {isError ? (
          message.text
        ) : (
          <Markdown
            text={message.text}
            streaming={streaming}
            hits={hits}
            citeCount={message.citations?.length ?? 0}
            onJump={onCitationJump}
          />
        )}
      </div>
      {(message.citations?.length ?? 0) > 0 && (
        <p className="sources-count">
          {message.citations!.length} source{message.citations!.length === 1 ? '' : 's'}
        </p>
      )}
      {!streaming && (
        <div className="msg-actions">
          {version !== undefined && version.total > 1 && <VersionPager version={version} />}
          {!isError && <CopyButton text={message.text} label="Copy response" />}
          {onRegenerate !== undefined && (
            <button
              className="action-btn"
              aria-label="Regenerate response"
              title="Ask again"
              onClick={onRegenerate}
            >
              <RetryIcon size={14} />
              Retry
            </button>
          )}
          {!isError && !readOnly && <Feedback />}
          {!isError && !readOnly && onReport !== undefined && (
            <MessageMenu onFork={onFork} onReport={onReport} />
          )}
        </div>
      )}
    </div>
  )
}

export function Typing() {
  return (
    <div className="msg assistant">
      <div className="typing" role="status" aria-label="Thinking">
        <span />
        <span />
        <span />
      </div>
    </div>
  )
}
