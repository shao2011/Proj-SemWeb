import { GoogleIcon } from './icons'

interface LoginPageProps {
  onLogin: () => void
  onGoHome: () => void
}

export function LoginPage({ onLogin, onGoHome }: LoginPageProps) {
  return (
    <div className="auth-page">
      <div className="auth-left">
        <button className="brand auth-brand" onClick={onGoHome}>
          <img className="brand-logo" src="/logo.png" alt="" />
          <span className="wordmark">EslothAI</span>
        </button>

        <h1>Question what&rsquo;s next</h1>
        <p className="auth-quote">Your thinking partner for big ambitions</p>
      </div>

      <div className="auth-right">
        <div className="auth-card">
          <h2>Sign in to EslothAI Chat</h2>
          <p className="auth-card-sub">Continue with Google to get started.</p>

          <button className="secondary-btn login-google-btn" onClick={onLogin}>
            <GoogleIcon />
            Continue with Google
          </button>

          <div className="auth-divider" />

          <p className="login-legal">
            By continuing, you acknowledge EslothAI&rsquo;s{' '}
            <a href="#" onClick={(event) => event.preventDefault()}>
              Privacy Policy
            </a>
            .
          </p>
        </div>
      </div>
    </div>
  )
}
