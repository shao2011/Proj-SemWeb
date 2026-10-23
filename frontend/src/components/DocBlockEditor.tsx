import { useEffect, useId, useRef, useState } from 'react'
import { ApiError, getDocumentBlocks, updateDocumentBlocks } from '../lib/api'
import { errorMessage } from '../lib/errors'
import type { DocumentBlocks } from '../lib/types'
import { SpinnerIcon } from './icons'

interface DocBlockEditorProps {
  userId: string
  projectId: string
  docId: string
  onDone: (saved: boolean) => void
  onDirtyChange: (dirty: boolean) => void
}

export const DISCARD_EDITS_PROMPT = 'Discard your unsaved edits?'

// The backend's 404 detail for a document stored before the blocks sidecar.
const LEGACY_DETAIL = 'Document is not editable'

function messageOf(error: unknown, fallback: string): string {
  return errorMessage(error, fallback)
}

function typeLabel(type: string): string {
  const words = type.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function rowsFor(text: string): number {
  return Math.min(14, Math.max(2, text.split('\n').length + Math.floor(text.length / 80)))
}

export function DocBlockEditor({
  userId,
  projectId,
  docId,
  onDone,
  onDirtyChange,
}: DocBlockEditorProps) {
  const idPrefix = useId()
  const [loaded, setLoaded] = useState<DocumentBlocks | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [legacy, setLegacy] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loadCount, setLoadCount] = useState(0)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoaded(null)
    setLegacy(false)
    setLoadError(null)
    setSaveError(null)
    setConflict(false)
    getDocumentBlocks(userId, projectId, docId)
      .then((blocks) => {
        if (cancelled) return
        setDrafts(Object.fromEntries(blocks.blocks.map((block) => [block.block_id, block.text])))
        setLoaded(blocks)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        if (error instanceof ApiError && error.status === 404 && error.message === LEGACY_DETAIL) {
          setLegacy(true)
        } else {
          setLoadError(messageOf(error, 'Could not load this document.'))
        }
      })
    return () => {
      cancelled = true
    }
  }, [userId, projectId, docId, loadCount])

  const changed = (loaded?.blocks ?? []).filter(
    (block) => (drafts[block.block_id] ?? block.text) !== block.text,
  )
  const blankIds = new Set(
    changed
      .filter((block) => (drafts[block.block_id] ?? '').trim() === '')
      .map((block) => block.block_id),
  )
  const dirty = changed.length > 0

  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])

  function cancel() {
    if (dirty && !window.confirm(DISCARD_EDITS_PROMPT)) return
    onDone(false)
  }

  function reload() {
    if (dirty && !window.confirm('Reload the latest version? Your edits will be discarded.')) return
    setLoadCount((count) => count + 1)
  }

  async function save() {
    if (loaded === null || saving || conflict || blankIds.size > 0) return
    if (!dirty) {
      onDone(false)
      return
    }
    setSaving(true)
    setSaveError(null)
    setConflict(false)
    try {
      await updateDocumentBlocks(
        userId,
        projectId,
        docId,
        loaded.version,
        changed.map((block) => ({
          block_id: block.block_id,
          text: drafts[block.block_id] ?? block.text,
        })),
      )
      if (alive.current) onDone(true)
    } catch (error) {
      if (!alive.current) return
      setConflict(error instanceof ApiError && error.status === 409)
      setSaveError(messageOf(error, 'Could not save your edits.'))
      setSaving(false)
    }
  }

  if (legacy || loadError !== null) {
    return (
      <div className="doc-edit">
        <div className="doc-edit-status">
          {legacy ? (
            <p className="empty-hint">Uploaded before editing was available. Re-upload to edit.</p>
          ) : (
            <p className="doc-error">{loadError}</p>
          )}
          <button className="secondary-btn" onClick={() => onDone(false)}>
            Back
          </button>
        </div>
      </div>
    )
  }

  if (loaded === null) {
    return (
      <div className="doc-edit">
        <div className="doc-edit-status">
          <p className="empty-hint">Loading…</p>
        </div>
      </div>
    )
  }

  return (
    <div className="doc-edit">
      <div className="doc-edit-bar">
        <button className="secondary-btn" disabled={saving} onClick={cancel}>
          Cancel
        </button>
        <button
          className="primary-btn"
          disabled={saving || conflict || blankIds.size > 0}
          onClick={() => void save()}
        >
          {saving ? (
            <>
              <SpinnerIcon size={14} />
              Re-indexing…
            </>
          ) : (
            'Save'
          )}
        </button>
      </div>
      {saveError !== null && (
        <div className="notice error doc-edit-error" role="alert">
          <span>{saveError}</span>
          {conflict && (
            <button className="secondary-btn" disabled={saving} onClick={reload}>
              Reload
            </button>
          )}
        </div>
      )}
      <div className="doc-edit-list">
        {loaded.blocks.length === 0 ? (
          <p className="empty-hint">This document has no editable text.</p>
        ) : (
          loaded.blocks.map((block, index) => {
            const id = `${idPrefix}-${index}`
            const hintId = `${id}-hint`
            const draft = drafts[block.block_id] ?? block.text
            const blank = blankIds.has(block.block_id)
            return (
              <div key={block.block_id} className="doc-edit-block">
                <label className="doc-edit-type" htmlFor={id}>
                  {typeLabel(block.type)} {index + 1}
                </label>
                <textarea
                  id={id}
                  className="doc-edit-text"
                  value={draft}
                  rows={rowsFor(draft)}
                  disabled={saving}
                  aria-invalid={blank}
                  aria-describedby={blank ? hintId : undefined}
                  onChange={(event) => {
                    const value = event.target.value
                    setDrafts((prev) => ({ ...prev, [block.block_id]: value }))
                  }}
                />
                {blank && (
                  <p id={hintId} className="doc-edit-hint">
                    Text can't be blank.
                  </p>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
