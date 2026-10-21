import { useEffect, useRef, useState } from 'react'
import type { ModelOption } from '../lib/types'
import { ContextGauge } from './ContextGauge'
import { CheckIcon, ChevronIcon, PlusIcon, SendIcon, StopIcon } from './icons'

interface ComposerProps {
  disabled: boolean
  busy: boolean
  placeholder?: string
  onSend: (text: string) => void
  onAttach: (file: File) => void
  onStop?: () => void
  /** The catalog from GET /models; empty hides the picker. */
  models?: ModelOption[]
  /** Picked model id, or null for the server's configured default. */
  modelId?: string | null
  onModelChange?: (modelId: string | null) => void
  /** Server-computed context gauge; null hides it. */
  contextPercent?: number | null
  compacting?: boolean
  onCompact?: () => void
}

const ACCEPT = '.pdf,.docx,.xlsx,.pptx'

const AUTO = { id: null, label: 'Auto', hint: 'The server picks its configured model' }

/** Rows shown before the "Other models" fold. */
const PRIMARY_MODELS = 3

/** Menu height ceiling. Roughly the base rows plus a peek of the expanded
 * fold: opening "Other models" grows the menu a little, then scrolls. */
const MENU_MAX_HEIGHT = 330

export function Composer({
  disabled,
  busy,
  placeholder = 'How can I help you today?',
  onSend,
  onAttach,
  onStop,
  models = [],
  modelId = null,
  onModelChange,
  contextPercent = null,
  compacting = false,
  onCompact,
}: ComposerProps) {
  const [text, setText] = useState('')
  const [llmOpen, setLlmOpen] = useState(false)
  // True when there is not enough room above the pill for the menu (the
  // project screen's composer sits near the top), so it opens downward.
  const [llmMenuDown, setLlmMenuDown] = useState(false)
  // Whatever direction it opens, the menu gets the exact space available on
  // that side and scrolls internally -- expanding "Other models" can never
  // push it past the viewport edge.
  const [llmMenuMaxHeight, setLlmMenuMaxHeight] = useState<number | null>(null)
  const [othersOpen, setOthersOpen] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const llmRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = textareaRef.current
    if (node === null) return
    node.style.height = 'auto'
    node.style.height = `${node.scrollHeight}px`
  }, [text])

  useEffect(() => {
    if (!llmOpen) return
    const close = (event: MouseEvent) => {
      if (!llmRef.current?.contains(event.target as Node)) setLlmOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setLlmOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [llmOpen])

  const canSend = text.trim().length > 0 && !disabled && !busy

  function submit() {
    if (!canSend) return
    onSend(text.trim())
    setText('')
  }

  const primaryModels = models.slice(0, PRIMARY_MODELS)
  const otherModels = models.slice(PRIMARY_MODELS)

  /** The box that actually clips the menu: the nearest scrollable ancestor.
   * An absolutely-positioned menu inside an `overflow-y: auto` container is
   * cut at the container's edge, not the viewport's, so measuring against
   * the viewport over-promised space and the expanded menu lost its top. */
  function clipBounds(node: HTMLElement): { top: number; bottom: number } {
    let el = node.parentElement
    while (el !== null) {
      const style = getComputedStyle(el)
      if (/(auto|scroll|hidden)/.test(style.overflowY + style.overflow)) {
        const rect = el.getBoundingClientRect()
        return {
          top: Math.max(rect.top, 0),
          bottom: Math.min(rect.bottom, window.innerHeight),
        }
      }
      el = el.parentElement
    }
    return { top: 0, bottom: window.innerHeight }
  }

  function openLlmMenu(pill: HTMLElement) {
    const rect = pill.getBoundingClientRect()
    const bounds = clipBounds(pill)
    const roomAbove = rect.top - bounds.top - 12
    const roomBelow = bounds.bottom - rect.bottom - 12
    // Upward by default; downward only when the space above cannot hold even
    // the capped menu and below is the better side (the project screen's
    // composer sits near the top).
    const down = roomAbove < MENU_MAX_HEIGHT && roomBelow > roomAbove
    setLlmMenuDown(down)
    // Capped, not sized to all available room: expanding "Other models" adds
    // a little height and then scrolls internally, instead of the menu
    // stretching across the screen.
    setLlmMenuMaxHeight(
      Math.max(160, Math.min(down ? roomBelow : roomAbove, MENU_MAX_HEIGHT)),
    )
    // A pick hidden behind the fold must be visible the moment the menu
    // opens, or its checkmark is invisible.
    setOthersOpen(otherModels.some((item) => item.id === modelId))
    setLlmOpen((prev) => !prev)
  }

  function modelRow(option: { id: string | null; label: string; hint: string }) {
    return (
      <button
        key={option.id ?? 'auto'}
        className="model-menu-item"
        role="menuitemradio"
        aria-checked={option.id === modelId}
        onClick={() => {
          onModelChange?.(option.id)
          setLlmOpen(false)
        }}
      >
        <span className="model-menu-text">
          <span className="model-menu-name">{option.label}</span>
          <span className="model-menu-hint">{option.hint}</span>
        </span>
        {option.id === modelId && <CheckIcon />}
      </button>
    )
  }

  return (
    <div className="composer">
      <textarea
        ref={textareaRef}
        rows={1}
        value={text}
        placeholder={placeholder}
        aria-label="Message"
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            submit()
          }
        }}
      />
      <div className="composer-row">
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPT}
          data-testid="file-input"
          style={{ display: 'none' }}
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) onAttach(file)
            event.target.value = ''
          }}
        />
        <button
          className="round-btn"
          aria-label="Upload a document"
          title="Upload PDF, DOCX, XLSX or PPTX"
          disabled={disabled || busy}
          onClick={() => fileRef.current?.click()}
        >
          <PlusIcon />
        </button>
        <span className="spacer" />
        <ContextGauge
          percent={contextPercent}
          compacting={compacting}
          onCompact={() => onCompact?.()}
        />
        {models.length > 0 && (
          <div className="model-select" ref={llmRef}>
            <button
              className={llmOpen ? 'model-pill open' : 'model-pill'}
              aria-haspopup="menu"
              aria-expanded={llmOpen}
              aria-label="Choose model"
              title="Model for this chat"
              onClick={(event) => openLlmMenu(event.currentTarget)}
            >
              {models.find((item) => item.id === modelId)?.label ?? AUTO.label}
              <ChevronIcon />
            </button>
            {llmOpen && (
              <div
                className={llmMenuDown ? 'model-menu down' : 'model-menu'}
                role="menu"
                style={
                  llmMenuMaxHeight !== null
                    ? { maxHeight: llmMenuMaxHeight, overflowY: 'auto' }
                    : undefined
                }
              >
                {modelRow(AUTO)}
                {primaryModels.map((item) =>
                  modelRow({ id: item.id, label: item.label, hint: item.id }),
                )}
                {otherModels.length > 0 && (
                  <button
                    className="model-menu-item model-menu-more"
                    aria-expanded={othersOpen}
                    onClick={() => setOthersOpen((prev) => !prev)}
                  >
                    <span className="model-menu-text">
                      <span className="model-menu-name">Other models</span>
                    </span>
                    <span className={othersOpen ? 'more-chevron open' : 'more-chevron'}>
                      <ChevronIcon />
                    </span>
                  </button>
                )}
                {othersOpen &&
                  otherModels.map((item) =>
                    modelRow({ id: item.id, label: item.label, hint: item.id }),
                  )}
              </div>
            )}
          </div>
        )}
        {busy && onStop !== undefined ? (
          <button
            className="round-btn send-btn"
            aria-label="Stop generating"
            title="Stop generating"
            onClick={onStop}
          >
            <StopIcon />
          </button>
        ) : (
          <button
            className="round-btn send-btn"
            aria-label="Send message"
            disabled={!canSend}
            onClick={submit}
          >
            <SendIcon />
          </button>
        )}
      </div>
    </div>
  )
}
