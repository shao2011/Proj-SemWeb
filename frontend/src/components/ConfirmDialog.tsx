import { useEffect } from 'react'

interface ConfirmDialogProps {
  title: string
  text: string
  confirmLabel: string
  onCancel: () => void
  onConfirm: () => void
}

export function ConfirmDialog({ title, text, confirmLabel, onCancel, onConfirm }: ConfirmDialogProps) {
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onCancel])

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel()
      }}
    >
      <div className="modal confirm-modal" role="dialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        <p className="confirm-text">{text}</p>
        <div className="confirm-actions">
          <button className="secondary-btn" onClick={onCancel}>
            Cancel
          </button>
          <button className="danger-btn" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
