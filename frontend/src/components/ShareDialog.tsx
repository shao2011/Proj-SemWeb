import { useEffect, useState } from 'react'
import {
  addShareRecipient,
  ApiError,
  getThreadShare,
  removeShareRecipient,
  revokeThreadShare,
  shareThread,
} from '../lib/api'
import type { ShareView } from '../lib/types'
import { CheckIcon, CloseIcon, CopyIcon, LinkIcon } from './icons'

interface ShareDialogProps {
  userId: string
  threadId: string
  onClose: () => void
}

function linkFor(shareId: string): string {
  return `${window.location.origin}/share/${shareId}`
}

/**
 * Create, copy, refresh or revoke a chat's public link.
 *
 * The link is a snapshot: it shows the conversation as it was when the link
 * was made. Later turns stay private until the owner presses Update, which is
 * why that is a button and not something the dialog does on open.
 */
export function ShareDialog({ userId, threadId, onClose }: ShareDialogProps) {
  const [share, setShare] = useState<ShareView | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [invitee, setInvitee] = useState('')

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])

  useEffect(() => {
    let cancelled = false
    void getThreadShare(userId, threadId)
      .then((current) => {
        if (!cancelled) setShare(current)
      })
      .catch((problem: unknown) => {
        // 404 is the normal "not shared yet" answer, not a failure.
        if (!cancelled && !(problem instanceof ApiError && problem.status === 404)) {
          setError('Could not load the sharing state.')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [userId, threadId])

  async function run(refresh: boolean) {
    setBusy(true)
    setError(null)
    try {
      setShare(await shareThread(userId, threadId, refresh))
    } catch (problem: unknown) {
      setError(
        problem instanceof ApiError && problem.status === 403
          ? 'Log in to share this chat.'
          : 'Could not create the link. Try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  async function revoke() {
    setBusy(true)
    setError(null)
    try {
      await revokeThreadShare(userId, threadId)
      setShare(null)
      setCopied(false)
    } catch {
      setError('Could not turn the link off. Try again.')
    } finally {
      setBusy(false)
    }
  }

  async function invite() {
    const email = invitee.trim()
    if (email.length === 0) return
    setBusy(true)
    setError(null)
    try {
      setShare(await addShareRecipient(userId, threadId, email))
      setInvitee('')
    } catch (problem: unknown) {
      setError(
        problem instanceof ApiError && problem.status === 422
          ? 'That does not look like an email address.'
          : 'Could not send the invitation. Try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  async function uninvite(email: string) {
    setBusy(true)
    setError(null)
    try {
      await removeShareRecipient(userId, threadId, email)
      setShare((prev) =>
        prev === null
          ? prev
          : { ...prev, recipients: prev.recipients.filter((item) => item !== email) },
      )
    } catch {
      setError('Could not remove that person. Try again.')
    } finally {
      setBusy(false)
    }
  }

  async function setAccess(access: 'link' | 'invite') {
    setBusy(true)
    setError(null)
    try {
      setShare(await shareThread(userId, threadId, false, access))
    } catch {
      setError('Could not change who can open this. Try again.')
    } finally {
      setBusy(false)
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      // Clipboard is unavailable on http origins; the link is shown in a
      // selectable field for exactly this case, so nothing is lost.
      setError('Copy is blocked here. Select the link and copy it manually.')
    }
  }

  const behind = share !== null && share.message_count > share.shared_through_position

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div className="modal share-modal" role="dialog" aria-modal="true" aria-label="Share chat">
        <h2>Share chat</h2>
        <p className="confirm-text">
          {share?.access === 'invite'
            ? 'Only the people you invite can open this. They can read it and carry it on in a copy of their own. Your chat stays yours.'
            : 'Anyone with the link can read this conversation. It shows the chat as it is now, not what you write later.'}
        </p>

        {loading ? (
          <p className="share-note">Loading…</p>
        ) : share === null ? (
          <div className="share-actions">
            <button className="secondary-btn" onClick={onClose}>
              Cancel
            </button>
            <button className="primary-btn" disabled={busy} onClick={() => void run(false)}>
              <LinkIcon size={15} />
              Create link
            </button>
          </div>
        ) : (
          <>
            <div className="share-link-row">
              <input
                className="share-link"
                readOnly
                value={linkFor(share.share_id)}
                aria-label="Share link"
                onFocus={(event) => event.currentTarget.select()}
              />
              <button
                className="primary-btn share-copy"
                onClick={() => void copy(linkFor(share.share_id))}
              >
                {copied ? <CheckIcon size={15} /> : <CopyIcon size={15} />}
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <p className="share-note">
              {share.access === 'invite'
                ? 'Only the people below can open this link.'
                : 'Anyone with this link can open it.'}{' '}
              {share.shared_through_position} message
              {share.shared_through_position === 1 ? '' : 's'} shared
              {behind
                ? ` · ${share.message_count - share.shared_through_position} newer not included`
                : ''}
            </p>

            <div className="share-invite">
              <label className="report-label" htmlFor="share-invite-email">
                Invite by email
              </label>
              <div className="share-link-row">
                <input
                  id="share-invite-email"
                  className="share-link"
                  type="email"
                  placeholder="name@company.com"
                  value={invitee}
                  onChange={(event) => setInvitee(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                      event.preventDefault()
                      void invite()
                    }
                  }}
                />
                <button
                  className="secondary-btn"
                  disabled={busy || invitee.trim().length === 0}
                  onClick={() => void invite()}
                >
                  Invite
                </button>
              </div>

              {share.recipients.length > 0 && (
                <ul className="share-people">
                  {share.recipients.map((email) => (
                    <li key={email}>
                      <span className="share-person">{email}</span>
                      <button
                        className="icon-btn"
                        aria-label={`Remove ${email}`}
                        title="Remove"
                        disabled={busy}
                        onClick={() => void uninvite(email)}
                      >
                        <CloseIcon size={14} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              {share.access === 'invite' ? (
                <button
                  className="share-access-toggle"
                  disabled={busy}
                  onClick={() => void setAccess('link')}
                >
                  Open this link to anyone instead
                </button>
              ) : (
                share.recipients.length > 0 && (
                  <button
                    className="share-access-toggle"
                    disabled={busy}
                    onClick={() => void setAccess('invite')}
                  >
                    Limit it to the people above
                  </button>
                )
              )}
            </div>
            <div className="share-actions">
              <button className="danger-btn" disabled={busy} onClick={() => void revoke()}>
                Turn off link
              </button>
              {behind && (
                <button
                  className="secondary-btn"
                  disabled={busy}
                  onClick={() => void run(true)}
                >
                  Update link
                </button>
              )}
              <button className="primary-btn" onClick={onClose}>
                Done
              </button>
            </div>
          </>
        )}

        {error !== null && <p className="share-error">{error}</p>}
      </div>
    </div>
  )
}
