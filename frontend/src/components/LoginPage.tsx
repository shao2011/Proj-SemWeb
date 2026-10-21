import { useEffect } from 'react'
import { AppleIcon, CloseIcon, FacebookIcon, GoogleIcon, MailIcon, PhoneIcon } from './icons'

export function LoginOptions({
  onLogin,
  busy,
}: {
  onLogin: () => void
  busy: boolean
}) {
  return (
    <>
      <div className="login-providers">
        <button className="login-provider" onClick={onLogin} disabled={busy}>
          <span className="login-provider-icon">
            <GoogleIcon size={22} />
          </span>
          {busy ? 'Signing in...' : 'Continue with Google'}
        </button>
        <button className="login-provider" disabled>
          <span className="login-provider-icon">
            <AppleIcon size={24} />
          </span>
          Continue with Apple
        </button>
        <button className="login-provider" disabled>
          <span className="login-provider-icon">
            <FacebookIcon size={22} />
          </span>
          Continue with Facebook
        </button>
      </div>

      <div className="login-or">
        <span className="login-or-line" />
        or
        <span className="login-or-line" />
      </div>

      <div className="login-alts">
        <button className="login-alt" disabled>
          <span className="login-alt-circle">
            <MailIcon size={24} />
          </span>
          Email
        </button>
        <button className="login-alt" disabled>
          <span className="login-alt-circle">
            <PhoneIcon size={24} />
          </span>
          Phone
        </button>
      </div>

      <p className="login-terms">
        By continuing, you agree to the <a href="#">Terms of Service</a> and
        <br />
        acknowledge the <a href="#">Privacy Policy</a>.
      </p>
    </>
  )
}

interface LoginPageProps {
  onLogin: () => void
  busy?: boolean
  error?: string | null
}

/** Pi-style sign-in page (referenced from pi.ai/onboarding/login). */
export function LoginPage({ onLogin, busy = false, error = null }: LoginPageProps) {
  return (
    <div className="login-page">
      <div className="login-card">
        <h1 className="login-title">Sign up or log in</h1>
        <LoginOptions onLogin={onLogin} busy={busy} />
        {error !== null && <p className="error-message">{error}</p>}
      </div>
    </div>
  )
}

interface LoginModalProps {
  title?: string
  onLogin: () => void
  onClose: () => void
  busy?: boolean
  error?: string | null
}

/**
 * The same sign-in options as a dialog, so logging in never navigates away —
 * whatever the visitor was doing (an unsent message, an open thread) is still
 * there when the modal closes.
 */
export function LoginModal({
  title = 'Sign up or log in',
  onLogin,
  onClose,
  busy = false,
  error = null,
}: LoginModalProps) {
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div className="modal login-modal" role="dialog" aria-modal="true" aria-label={title}>
        <button className="icon-btn login-modal-close" aria-label="Close" onClick={onClose}>
          <CloseIcon />
        </button>
        <h1 className="login-title">{title}</h1>
        <LoginOptions onLogin={onLogin} busy={busy} />
        {error !== null && <p className="error-message">{error}</p>}
      </div>
    </div>
  )
}
