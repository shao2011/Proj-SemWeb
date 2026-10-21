import { useEffect, useState } from 'react'
import { MEMORY_KEYS } from '../lib/types'
import type { Memory, MemoryType } from '../lib/types'
import { CloseIcon, PencilIcon, TrashIcon } from './icons'

/**
 * The "About you" fields write scope=user memories on known keys. Posting a key
 * that already exists supersedes the old row on the backend, so saving twice
 * updates rather than duplicating.
 */
export const ABOUT_FIELDS: { key: string; type: MemoryType; label: string; placeholder: string }[] = [
  {
    key: 'nickname',
    type: 'fact',
    label: 'What should the assistant call you?',
    placeholder: 'Your Name',
  },
  {
    key: 'occupation',
    type: 'fact',
    label: 'What do you do?',
    placeholder: 'AI Engineer',
  },
  {
    key: 'response_language',
    type: 'preference',
    label: 'Preferred response language',
    placeholder: 'English',
  },
  {
    key: 'response_format',
    type: 'preference',
    label: 'Preferred response style',
    placeholder: 'Short paragraphs, code first, no filler',
  },
]

const KEY_LABELS: Record<string, string> = {
  response_language: 'Response language',
  response_length: 'Response length',
  response_format: 'Response style',
  tone: 'Tone',
  nickname: 'Nickname',
  occupation: 'Occupation',
  organization: 'Organization',
  focus_area: 'Focus area',
  expertise_level: 'Expertise level',
}

export function keyLabel(key: string): string {
  const known = KEY_LABELS[key]
  if (known !== undefined) return known
  if (!key.startsWith('other:')) return key
  const words = key.slice(6).replace(/[-_]/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

interface AboutFieldProps {
  label: string
  placeholder: string
  stored: string
  busy: boolean
  onSave: (value: string) => Promise<boolean>
}

export function AboutField({ label, placeholder, stored, busy, onSave }: AboutFieldProps) {
  const [draft, setDraft] = useState(stored)

  // The stored value arrives after the first render, and changes again when a
  // save supersedes it.
  useEffect(() => {
    setDraft(stored)
  }, [stored])

  const trimmed = draft.trim()
  const changed = trimmed.length > 0 && trimmed !== stored

  return (
    <div className="field-block">
      <label className="field-label" htmlFor={`about-${label}`}>
        {label}
      </label>
      <div className="form-row" style={{ marginTop: 0 }}>
        <input
          id={`about-${label}`}
          className="field"
          style={{ flex: 1 }}
          placeholder={placeholder}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          className="primary-btn"
          disabled={!changed || busy}
          onClick={() => void onSave(trimmed)}
        >
          Save
        </button>
      </div>
    </div>
  )
}

interface ManageMemoryDialogProps {
  memories: Memory[]
  busy: boolean
  onClose: () => void
  onAdd: (type: MemoryType, key: string, value: string) => Promise<boolean>
  onEdit: (memoryId: string, value: string) => Promise<boolean>
  onDelete: (memoryId: string) => void
}

export function ManageMemoryDialog({
  memories,
  busy,
  onClose,
  onAdd,
  onEdit,
  onDelete,
}: ManageMemoryDialogProps) {
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [newKey, setNewKey] = useState<string>(MEMORY_KEYS[0])
  const [customKey, setCustomKey] = useState('')
  const [newType, setNewType] = useState<MemoryType>('preference')
  const [newValue, setNewValue] = useState('')

  const key = newKey === 'other' ? `other:${customKey.trim()}` : newKey
  const canAdd =
    newValue.trim().length > 0 && (newKey !== 'other' || /^[a-z0-9][a-z0-9_-]*$/.test(customKey.trim()))

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-label="Manage memory"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-head">
          <h2>Memory</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>

        {memories.length === 0 ? (
          <p className="empty-hint">Nothing saved yet.</p>
        ) : (
          <div className="memory-list">
            {memories.map((memory) => (
              <div className="memory-card" key={memory.memory_id}>
                <span className="memory-head">
                  <span className="memory-key">{keyLabel(memory.key)}</span>
                  <span className={`badge ${memory.type}`}>{memory.type}</span>
                  <span className="memory-source">{memory.source.replace('_', ' ')}</span>
                </span>

                {editing === memory.memory_id ? (
                  <>
                    <textarea
                      className="field memory-input"
                      aria-label={`Edit ${keyLabel(memory.key)}`}
                      rows={2}
                      autoFocus
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                    />
                    <span className="memory-actions">
                      <button className="icon-btn" onClick={() => setEditing(null)}>
                        Cancel
                      </button>
                      <button
                        className="primary-btn"
                        disabled={draft.trim().length === 0 || busy}
                        onClick={() => {
                          void onEdit(memory.memory_id, draft.trim()).then((saved) => {
                            // A rejected edit keeps the editor open with the
                            // draft intact, so the value is not lost.
                            if (saved) setEditing(null)
                          })
                        }}
                      >
                        Save
                      </button>
                    </span>
                  </>
                ) : (
                  <span className="memory-body">
                    <span className="memory-value">{memory.value}</span>
                    <span className="memory-actions">
                      <button
                        className="icon-btn"
                        aria-label={`Edit ${keyLabel(memory.key)}`}
                        onClick={() => {
                          setEditing(memory.memory_id)
                          setDraft(memory.value)
                        }}
                      >
                        <PencilIcon />
                      </button>
                      <button
                        className="icon-btn"
                        aria-label={`Delete ${keyLabel(memory.key)}`}
                        onClick={() => onDelete(memory.memory_id)}
                      >
                        <TrashIcon />
                      </button>
                    </span>
                  </span>
                )}
              </div>
            ))}
          </div>
        )}

        <h3 className="settings-heading">Add a memory</h3>
        <div className="form-row" style={{ marginTop: 0 }}>
          <select
            className="field"
            style={{ width: 172 }}
            aria-label="Memory key"
            value={newKey}
            onChange={(event) => setNewKey(event.target.value)}
          >
            {MEMORY_KEYS.map((item) => (
              <option key={item} value={item}>
                {keyLabel(item)}
              </option>
            ))}
            <option value="other">Other…</option>
          </select>
          <select
            className="field"
            aria-label="Memory type"
            value={newType}
            onChange={(event) => setNewType(event.target.value as MemoryType)}
          >
            <option value="preference">preference</option>
            <option value="fact">fact</option>
            <option value="pin">pin</option>
          </select>
        </div>

        {newKey === 'other' && (
          <div className="field-block">
            <label className="field-label" htmlFor="custom-key">
              Custom key
            </label>
            <input
              id="custom-key"
              className="field"
              style={{ width: '100%' }}
              placeholder="favourite-editor"
              value={customKey}
              onChange={(event) => setCustomKey(event.target.value.toLowerCase())}
            />
            <p className="field-hint">Lowercase letters, digits, hyphen and underscore.</p>
          </div>
        )}

        <div className="form-row" style={{ marginTop: 10 }}>
          <input
            className="field"
            style={{ flex: 1 }}
            aria-label="Memory value"
            placeholder="Always answer in Vietnamese"
            value={newValue}
            onChange={(event) => setNewValue(event.target.value)}
          />
          <button
            className="primary-btn"
            disabled={!canAdd || busy}
            onClick={() => {
              void onAdd(newType, key, newValue.trim()).then((saved) => {
                if (!saved) return
                setNewValue('')
                setCustomKey('')
              })
            }}
          >
            Add
          </button>
        </div>
      </div>
    </div>
  )
}
