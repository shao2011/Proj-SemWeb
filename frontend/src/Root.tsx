import { useEffect, useRef, useState } from 'react'
import { onIdTokenChanged, signInWithPopup } from 'firebase/auth'
import App from './App'
import { AdminPage } from './components/AdminPage'
import { LoginPage } from './components/LoginPage'
import { SharePage } from './components/SharePage'
import { firebaseAuth, googleProvider } from './lib/firebase'
import { claimAccount, updateProfile } from './lib/api'
import {
  clearPendingClaim,
  isLoggedIn,
  isUserVerified,
  loadPendingClaim,
  loadUserId,
  markUserVerified,
  saveAuthToken,
  savePendingClaim,
  saveUserId,
} from './lib/storage'

const POPUP_CLOSE_FALLBACK_MS = 2500
const POPUP_CLOSED_CODES = new Set([
  'auth/popup-closed-by-user',
  'auth/cancelled-popup-request',
])

function firebaseErrorCode(error: unknown): string | null {
  if (typeof error !== 'object' || error === null || !('code' in error)) {
    return null
  }
  const candidate = (error as { code: unknown }).code
  return typeof candidate === 'string' ? candidate : null
}

/**
 * No router: `/login` is the only named route. Everything else is the app —
 * logged out it renders in browse mode (the sidebar shows a Log in button and
 * sending a message redirects to `/login`).
 */
export default function Root() {
  const [pathname, setPathname] = useState(() => window.location.pathname)
  const [loggedIn, setLoggedInState] = useState(() => isLoggedIn())
  const [loginBusy, setLoginBusy] = useState(false)
  const [loginError, setLoginError] = useState<string | null>(null)
  const loginAttemptRef = useRef(0)

  useEffect(() => {
    const onPopState = () => setPathname(window.location.pathname)
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  // Retry a claim that failed at login (see savePendingClaim). Runs once per
  // mount; success or a definitive server refusal clears the marker.
  useEffect(() => {
    if (!loggedIn) return
    const pending = loadPendingClaim()
    if (pending === null) return
    if (pending === loadUserId()) {
      clearPendingClaim()
      return
    }
    void claimAccount(pending)
      .then(() => clearPendingClaim())
      .catch((error: unknown) => {
        // 4xx means the server refused it for good (already claimed, not
        // anonymous-shaped); keep only transient failures for another try.
        if (
          error instanceof Object &&
          'status' in error &&
          typeof error.status === 'number' &&
          error.status >= 400 &&
          error.status < 500
        ) {
          clearPendingClaim()
        }
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loggedIn])

  // Firebase ID tokens expire after an hour. The SDK keeps the session alive
  // and mints fresh tokens; this listener keeps localStorage (which every
  // API call reads its Bearer from) in sync, so long-lived tabs and reloads
  // keep working without a re-login.
  useEffect(() => {
    return onIdTokenChanged(firebaseAuth, (user) => {
      void (async () => {
        if (user !== null && isLoggedIn()) {
          saveAuthToken(await user.getIdToken())
        }
      })()
    })
  }, [])

  // Visiting `/login` while already signed in skips straight past it.
  useEffect(() => {
    if (loggedIn && pathname === '/login') {
      window.history.replaceState({}, '', '/')
      setPathname('/')
    }
  }, [loggedIn, pathname])

  async function loginWithGoogle() {
    const attempt = loginAttemptRef.current + 1
    loginAttemptRef.current = attempt
    setLoginError(null)
    setLoginBusy(true)
    const unlockTimer = window.setTimeout(() => {
      if (loginAttemptRef.current === attempt) {
        setLoginBusy(false)
      }
    }, POPUP_CLOSE_FALLBACK_MS)
    try {
      const credential = await signInWithPopup(firebaseAuth, googleProvider)
      if (loginAttemptRef.current !== attempt) return
      const token = await credential.user.getIdToken()
      // Claim the anonymous history before switching identities: the token
      // must already be stored (the endpoint reads the target uid from it),
      // and the old UUID must still be at hand. Only a genuinely anonymous
      // id is ever claimed — a previously-verified uid lingering on this
      // machine belongs to another account and must not be absorbed. A
      // failed claim only costs the anonymous history, so it must never
      // block the login itself.
      const previousUserId = loadUserId()
      saveAuthToken(token)
      if (!isUserVerified() && previousUserId !== credential.user.uid) {
        // Parked first, cleared on success: a claim lost to a network blip
        // is retried on the next startup instead of orphaning the history.
        savePendingClaim(previousUserId)
        try {
          await claimAccount(previousUserId)
          clearPendingClaim()
        } catch (error) {
          console.error('Claiming the anonymous history failed:', error)
        }
      }
      saveUserId(credential.user.uid)
      markUserVerified()
      // The Google profile lands in user settings so the sidebar and the
      // Settings dialog show the real name/email. Best-effort like the
      // claim: a failure here must not block the login.
      try {
        await updateProfile(
          credential.user.uid,
          credential.user.displayName,
          credential.user.email,
        )
      } catch (error) {
        console.error('Storing the login profile failed:', error)
      }
      setLoggedInState(true)
    } catch (error) {
      if (loginAttemptRef.current !== attempt) return
      const code = firebaseErrorCode(error)
      if (code !== null && POPUP_CLOSED_CODES.has(code)) {
        return
      }
      setLoginError('Google sign-in failed. Please try again.')
    } finally {
      window.clearTimeout(unlockTimer)
      if (loginAttemptRef.current === attempt) {
        setLoginBusy(false)
      }
    }
  }

  // Used by the in-app login modal: signs in without navigating, so the
  // app keeps its state (open thread, pending message) across the switch.
  async function loginInPlace() {
    await loginWithGoogle()
  }

  async function enterApp() {
    await loginWithGoogle()
    if (!isLoggedIn()) return
    window.history.pushState({}, '', '/')
    setPathname('/')
  }

  // Before every other branch, and before `App`: a share link is opened by
  // people with no account, and mounting the app shell would create an
  // anonymous identity and start fetching a stranger's own threads. An invite
  // link still needs the login machinery that lives here, which is why this
  // is a branch in Root rather than a separate entry point.
  const share = /^\/share\/([A-Za-z0-9_-]+)\/?$/.exec(pathname)
  if (share !== null) {
    return (
      <SharePage
        shareId={share[1]!}
        loggedIn={loggedIn}
        userId={loggedIn ? loadUserId() : null}
        onLogin={() => void loginInPlace()}
        loginBusy={loginBusy}
        loginError={loginError}
      />
    )
  }

  // Also outside the app shell: the admin screen manages the system, not one
  // account's chats. Login happens in place so the URL survives the redirect.
  if (/^\/admin\/?$/.test(pathname)) {
    if (!loggedIn) {
      return (
        <LoginPage
          onLogin={() => void loginInPlace()}
          busy={loginBusy}
          error={loginError}
        />
      )
    }
    return <AdminPage userId={loadUserId()} />
  }

  if (!loggedIn && pathname === '/login') {
    return <LoginPage onLogin={enterApp} busy={loginBusy} error={loginError} />
  }
  return (
    <App
      loggedIn={loggedIn}
      onLogin={() => void loginInPlace()}
      loginBusy={loginBusy}
      loginError={loginError}
    />
  )
}
