import { useEffect, useState } from 'react'
import { ApiError, forkSharedChat, getSharedChat } from '../lib/api'
import { savePendingOpenThread } from '../lib/storage'
import type { ChatMessage, SharedChat } from '../lib/types'
import { LoginOptions } from './LoginPage'
import { Message } from './Message'
import { Burst } from './icons'

type Gate = 'loading' | 'ok' | 'login' | 'forbidden' | 'missing'

interface SharePageProps {
  shareId: string
  loggedIn: boolean
  /** Present only when signed in; never sent for a public link. */
  userId: string | null
  onLogin: () => void
  loginBusy?: boolean
  loginError?: string | null
}

/**
 * A shared chat, opened from a link.
 *
 * Rendered without mounting the app: no threads or projects are fetched and,
 * for a signed-out reader, no identity is created. A public link shows the
 * transcript and nothing else. An invite link needs a login, and once the
 * reader's verified address is on the list they can take the conversation into
 * a copy of their own — the copy is theirs, so the owner's chat never changes.
 */
export function SharePage({
  shareId,
  loggedIn,
  userId,
  onLogin,
  loginBusy = false,
  loginError = null,
}: SharePageProps) {
  const [chat, setChat] = useState<SharedChat | null>(null)
  const [gate, setGate] = useState<Gate>('loading')
  const [continuing, setContinuing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Belt and braces with the server's X-Robots-Tag: a crawler that renders
  // the page rather than reading the API still sees the directive.
  useEffect(() => {
    const meta = document.createElement('meta')
    meta.name = 'robots'
    meta.content = 'noindex, nofollow'
    document.head.appendChild(meta)
    return () => {
      meta.remove()
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    setGate('loading')
    void getSharedChat(shareId, loggedIn ? userId : null)
      .then((loaded) => {
        if (cancelled) return
        setChat(loaded)
        setGate('ok')
        document.title = `${loaded.title} · EslothAI`
      })
      .catch((problem: unknown) => {
        if (cancelled) return
        const status = problem instanceof ApiError ? problem.status : 0
        // 401 means the link is private and we have no proof of who this is;
        // 403 means we do, and it is the wrong account.
        setGate(status === 401 ? 'login' : status === 403 ? 'forbidden' : 'missing')
      })
    return () => {
      cancelled = true
    }
    // Re-runs after a login lands, which is what turns the gate into the chat.
  }, [shareId, loggedIn, userId])

  async function continueHere() {
    if (userId === null) return
    setContinuing(true)
    setError(null)
    try {
      const forked = await forkSharedChat(userId, shareId)
      // No router to carry the id, so it is parked for the app to pick up.
      savePendingOpenThread(forked.thread_id)
      window.location.href = '/'
    } catch {
      setError('Could not start your copy. Try again.')
      setContinuing(false)
    }
  }

  return (
    <div className="share-page">
      <header className="share-head">
        <a className="share-brand" href="/">
          <Burst size={20} />
          EslothAI
        </a>
        {gate === 'ok' && chat?.can_continue === true ? (
          <button
            className="primary-btn share-cta"
            disabled={continuing}
            onClick={() => void continueHere()}
          >
            {continuing ? 'Opening…' : 'Continue in your own chat'}
          </button>
        ) : (
          <a className="primary-btn share-cta" href="/">
            Try EslothAI
          </a>
        )}
      </header>

      <main className="share-body">
        {gate === 'loading' ? (
          <p className="share-empty">Loading…</p>
        ) : gate === 'login' ? (
          <div className="share-gate">
            <h1>This chat was shared with specific people</h1>
            <p className="share-gate-sub">
              Log in with the address it was sent to and it will open here.
            </p>
            <LoginOptions onLogin={onLogin} busy={loginBusy} />
            {loginError !== null && <p className="error-message">{loginError}</p>}
          </div>
        ) : gate === 'forbidden' ? (
          <div className="share-empty">
            <h1>Not shared with this account</h1>
            <p>
              You are signed in, but this chat was shared with a different email
              address. Switch accounts and open the link again.
            </p>
          </div>
        ) : gate === 'missing' ? (
          <div className="share-empty">
            <h1>Link not available</h1>
            <p>
              This link is no longer valid. The owner may have turned it off or
              deleted the chat.
            </p>
          </div>
        ) : chat === null ? null : (
          <>
            <h1 className="share-title">{chat.title}</h1>
            <div className="thread">
              {chat.messages.map((message, index) => {
                const rendered: ChatMessage = {
                  id: `shared-${index}`,
                  role: message.role,
                  text: message.content,
                }
                return <Message key={rendered.id} message={rendered} readOnly />
              })}
              {chat.truncated && (
                <p className="share-note">
                  Showing the first {chat.messages.length} of {chat.message_count}{' '}
                  messages.
                </p>
              )}
              {chat.can_continue && (
                <p className="share-note">
                  Continuing makes your own copy of this conversation. Anything you
                  write there stays yours.
                </p>
              )}
              {error !== null && <p className="share-error">{error}</p>}
            </div>
          </>
        )}
      </main>
    </div>
  )
}
