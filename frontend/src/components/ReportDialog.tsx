import { useEffect, useState } from 'react'
import { reportMessage } from '../lib/api'
import type { ReportReason } from '../lib/types'

const REASONS: { value: ReportReason; label: string }[] = [
  { value: 'harmful', label: 'Harmful or unsafe' },
  { value: 'false_information', label: 'False information' },
  { value: 'not_helpful', label: 'Not helpful' },
  { value: 'other', label: 'Something else' },
]

const DETAILS_LIMIT = 1000

interface ReportDialogProps {
  userId: string
  threadId: string
  messagePosition: number | null
  onClose: () => void
  onSent: () => void
}

export function ReportDialog({
  userId,
  threadId,
  messagePosition,
  onClose,
  onSent,
}: ReportDialogProps) {
  const [reason, setReason] = useState<ReportReason | ''>('')
  const [details, setDetails] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])

  async function submit() {
    if (reason === '') return
    setBusy(true)
    setError(null)
    try {
      await reportMessage(userId, threadId, messagePosition, reason, details.trim())
      onSent()
    } catch {
      setError('Could not send the report. Try again.')
      setBusy(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div className="modal report-modal" role="dialog" aria-modal="true" aria-label="Report message">
        <h2>Report message</h2>

        <label className="report-label" htmlFor="report-reason">
          What type of content are you reporting?
        </label>
        <select
          id="report-reason"
          className="report-select"
          value={reason}
          onChange={(event) => setReason(event.target.value as ReportReason)}
        >
          <option value="" disabled>
            Select an option
          </option>
          {REASONS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>

        <label className="report-label" htmlFor="report-details">
          Provide details: (optional)
        </label>
        <textarea
          id="report-details"
          className="report-details"
          rows={5}
          maxLength={DETAILS_LIMIT}
          placeholder="Please provide more details"
          value={details}
          onChange={(event) => setDetails(event.target.value)}
        />
        <p className="report-count">
          {details.length}/{DETAILS_LIMIT}
        </p>

        <p className="report-note">
          Submitting this feedback will send the entire conversation to EslothAI to
          improve.
        </p>

        {error !== null && <p className="share-error">{error}</p>}

        <div className="report-actions">
          <button className="report-cancel" onClick={onClose}>
            Cancel
          </button>
          {/* Disabled until a type is picked: the dropdown is the one field the
              server requires, and a Submit that 422s teaches nothing. */}
          <button
            className="primary-btn"
            disabled={reason === '' || busy}
            onClick={() => void submit()}
          >
            Submit
          </button>
        </div>
      </div>
    </div>
  )
}
