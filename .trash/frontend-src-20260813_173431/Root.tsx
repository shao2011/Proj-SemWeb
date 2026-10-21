import { useEffect, useState } from 'react'
import App from './App'
import { HomePage } from './components/HomePage'
import { LoginPage } from './components/LoginPage'
import { isLoggedIn, setLoggedIn, TEST_AUTH_TOKEN } from './lib/storage'

/**
 * No router: this app has exactly two routes worth naming, `/` and `/login`.
 * Everything else behaves like `/` while logged out (there are no other
 * deep-linkable pages) and like the app itself while logged in.
 */
export default function Root() {
  const [pathname, setPathname] = useState(() => window.location.pathname)
  const [loggedIn, setLoggedInState] = useState(() => isLoggedIn())

  useEffect(() => {
    const onPopState = () => setPathname(window.location.pathname)
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  // Matches eslothai.com/login's own behavior (confirmed live): visiting
  // `/login` while already signed in skips straight past it.
  useEffect(() => {
    if (loggedIn && pathname === '/login') {
      window.history.replaceState({}, '', '/')
      setPathname('/')
    }
  }, [loggedIn, pathname])

  useEffect(() => {
    if (!loggedIn) {
      console.info(
        `[dev] Skip login: localStorage.setItem('rag.authToken', '${TEST_AUTH_TOKEN}'); location.reload()`,
      )
    }
  }, [loggedIn])

  function goToLogin() {
    window.history.pushState({}, '', '/login')
    setPathname('/login')
  }

  function goHome() {
    window.history.pushState({}, '', '/')
    setPathname('/')
  }

  function enterApp() {
    setLoggedIn(true)
    setLoggedInState(true)
    window.history.pushState({}, '', '/')
    setPathname('/')
  }

  if (loggedIn) {
    return <App />
  }
  if (pathname === '/login') {
    return <LoginPage onLogin={enterApp} onGoHome={goHome} />
  }
  return <HomePage onGoToLogin={goToLogin} />
}
