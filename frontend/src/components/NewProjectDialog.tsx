import { useEffect, useRef, useState } from 'react'
import type { Provider } from '../lib/types'
import { CloseIcon, DocumentsIcon, SmilePlusIcon, TrashIcon, UploadIcon } from './icons'
import { PROJECT_COLORS, PROJECT_ICONS, ProjectIcon } from './projectIcons'

const ACCEPT = '.pdf,.docx,.xlsx,.pptx'
const MAX_BYTES = 25 * 1024 * 1024

interface NewProjectDialogProps {
  providers: Provider[]
  busy: boolean
  onClose: () => void
  onCreate: (
    provider: Provider,
    files: File[],
    icon: string | null,
    color: string | null,
    name: string,
  ) => void
}

function readableSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * Files cannot be uploaded before the project exists: the upload route is
 * `/projects/{id}/documents`. So they are queued here and sent by the caller
 * after the project is created.
 */
export function NewProjectDialog({
  providers,
  busy,
  onClose,
  onCreate,
}: NewProjectDialogProps) {
  const [provider, setProvider] = useState<Provider | ''>(providers[0] ?? '')
  const [files, setFiles] = useState<File[]>([])
  const [rejected, setRejected] = useState<string[]>([])
  const [dragging, setDragging] = useState(false)
  const [icon, setIcon] = useState<string | null>(null)
  const [color, setColor] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [pickerOpen, setPickerOpen] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!pickerOpen) return
    const close = (event: MouseEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setPickerOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [pickerOpen])

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [busy, onClose])

  const effectiveProvider = provider === '' ? providers[0] : provider
  const canCreate = effectiveProvider !== undefined && !busy

  function addFiles(incoming: FileList | null) {
    if (incoming === null) return
    const accepted: File[] = []
    const refused: string[] = []
    for (const file of Array.from(incoming)) {
      const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
      if (!ACCEPT.split(',').includes(extension)) {
        refused.push(`${file.name} (định dạng không hỗ trợ)`)
      } else if (file.size > MAX_BYTES) {
        refused.push(`${file.name} (vượt 25 MB)`)
      } else {
        accepted.push(file)
      }
    }
    setRejected(refused)
    setFiles((prev) => {
      const seen = new Set(prev.map((item) => `${item.name}:${item.size}`))
      return [...prev, ...accepted.filter((f) => !seen.has(`${f.name}:${f.size}`))]
    })
  }

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose()
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-label="New project">
        <div className="modal-head">
          <h2>New project</h2>
          <button
            className="icon-btn"
            aria-label="Close"
            disabled={busy}
            onClick={onClose}
          >
            <CloseIcon />
          </button>
        </div>

        <p className="lede">
          Upload materials to give the project context. Documents are parsed,
          chunked and indexed so answers in this project can cite them.
        </p>

        <div className="name-field">
          <div className="icon-picker" ref={pickerRef}>
            <button
              className="icon-picker-btn"
              aria-label="Choose icon and color"
              aria-expanded={pickerOpen}
              title="Choose icon and color"
              disabled={busy}
              style={color !== null && icon !== null ? { color } : undefined}
              onClick={() => setPickerOpen((prev) => !prev)}
            >
              {icon === null ? (
                <SmilePlusIcon size={19} />
              ) : (
                <ProjectIcon icon={icon} color={color} size={19} />
              )}
            </button>
            {pickerOpen && (
            <div className="icon-picker-pop">
              <div className="picker-colors">
                {PROJECT_COLORS.map((swatch) => (
                  <button
                    key={swatch}
                    className={swatch === color ? 'picker-swatch active' : 'picker-swatch'}
                    aria-label={`Color ${swatch}`}
                    style={{ background: swatch }}
                    onClick={() => setColor(swatch)}
                  />
                ))}
              </div>
              <div className="picker-icons">
                {PROJECT_ICONS.map(({ key, Icon }) => (
                  <button
                    key={key}
                    className={key === (icon ?? 'folder') ? 'picker-icon active' : 'picker-icon'}
                    aria-label={`Icon ${key}`}
                    style={color !== null ? { color } : undefined}
                    onClick={() => setIcon(key)}
                  >
                    <Icon size={18} />
                  </button>
                ))}
              </div>
              <button className="picker-done" onClick={() => setPickerOpen(false)}>
                Done
              </button>
            </div>
            )}
          </div>
          <input
            id="new-project-name"
            className="name-input"
            type="text"
            placeholder="New Project"
            maxLength={200}
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
        </div>

        <label className="field-label" htmlFor="new-project-provider">
          Embedding provider
        </label>
        <select
          id="new-project-provider"
          className="field"
          style={{ width: '100%' }}
          value={effectiveProvider ?? ''}
          disabled={busy}
          onChange={(event) => setProvider(event.target.value as Provider)}
        >
          {providers.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
        <p className="field-hint">
          Pinned at creation and cannot be changed later.
        </p>

        <div
          className={dragging ? 'dropzone dragging' : 'dropzone'}
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault()
            setDragging(false)
            if (!busy) addFiles(event.dataTransfer.files)
          }}
        >
          <input
            ref={fileRef}
            type="file"
            multiple
            accept={ACCEPT}
            data-testid="project-files-input"
            style={{ display: 'none' }}
            onChange={(event) => {
              addFiles(event.target.files)
              event.target.value = ''
            }}
          />
          <p className="dropzone-title">or drop your files</p>
          <p className="dropzone-sub">pdf, docx, xlsx, pptx, up to 25 MB each</p>
          <button
            className="secondary-btn"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
          >
            <UploadIcon />
            Upload files
          </button>
        </div>

        {rejected.length > 0 && (
          <div className="notice error">
            {rejected.map((item) => (
              <div key={item}>{item}</div>
            ))}
          </div>
        )}

        {files.length > 0 && (
          <div className="file-queue">
            {files.map((file) => (
              <div className="file-row" key={`${file.name}:${file.size}`}>
                <DocumentsIcon size={15} />
                <span className="file-name">{file.name}</span>
                <span className="file-size">{readableSize(file.size)}</span>
                <button
                  className="icon-btn"
                  aria-label={`Remove ${file.name}`}
                  disabled={busy}
                  onClick={() =>
                    setFiles((prev) => prev.filter((item) => item !== file))
                  }
                >
                  <TrashIcon />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="modal-foot">
          <button className="secondary-btn" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary-btn"
            disabled={!canCreate}
            onClick={() =>
              onCreate(effectiveProvider as Provider, files, icon, color, name.trim())
            }
          >
            {busy
              ? 'Creating...'
              : files.length > 0
                ? `Create and index ${files.length} file${files.length === 1 ? '' : 's'}`
                : 'Create project'}
          </button>
        </div>
      </div>
    </div>
  )
}
