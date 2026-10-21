const USER_KEY = 'rag.userId'
const AUTH_TOKEN_KEY = 'rag.authToken'

/**
 * The one shape the backend accepts through the X-User-ID fallback. Mirrors
 * the server's `ANON_ID_RE`; identities in any other shape require a login
 * token.
 */
const ANON_ID_PATTERN = /^anon_[0-9a-f]{32}$/

function mintAnonymousId(): string {
  return `anon_${crypto.randomUUID().replaceAll('-', '')}`
}

export function loadUserId(): string {
  const stored = localStorage.getItem(USER_KEY)?.trim() ?? ''
  if (stored.length > 0) {
    // A verified uid is whatever the login said; anything else must be a
    // well-formed anonymous id or the backend will refuse the fallback.
    // Pre-prefix UUIDs from older builds get re-minted here.
    if (isUserVerified() || ANON_ID_PATTERN.test(stored)) return stored
  }
  const generated = mintAnonymousId()
  localStorage.setItem(USER_KEY, generated)
  return generated
}

export function saveUserId(userId: string): void {
  localStorage.setItem(USER_KEY, userId)
}

const USER_VERIFIED_KEY = 'rag.userVerified'

/**
 * Whether the stored userId came from a real login (a Firebase uid) rather
 * than the anonymous UUID. Claiming history on login must only ever target an
 * anonymous id: claiming a previously-verified id would hand one account's
 * data to whoever logs in next on the same machine.
 */
export function isUserVerified(): boolean {
  return localStorage.getItem(USER_VERIFIED_KEY) === 'true'
}

export function markUserVerified(): void {
  localStorage.setItem(USER_VERIFIED_KEY, 'true')
}

/**
 * A fresh anonymous identity, for logout: the previous uid must not linger as
 * the browse-mode identity (shared machines would expose the account's data
 * through the X-User-ID fallback), and the next login must not claim it.
 */
export function resetIdentity(): void {
  localStorage.setItem(USER_KEY, mintAnonymousId())
  localStorage.removeItem(USER_VERIFIED_KEY)
  clearPendingClaim()
  // The snapshot belongs to the identity being dropped; on a shared machine
  // it must not greet the next person with the previous account's sidebar.
  clearUiSnapshot()
}

const PENDING_CLAIM_KEY = 'rag.pendingClaim'

/**
 * The anonymous id whose history still has to be claimed. Written before the
 * claim call and cleared on success, so a claim that fails at login (network
 * blip) is retried on the next startup instead of orphaning the history.
 */
export function savePendingClaim(anonymousUserId: string): void {
  localStorage.setItem(PENDING_CLAIM_KEY, anonymousUserId)
}

export function loadPendingClaim(): string | null {
  const stored = localStorage.getItem(PENDING_CLAIM_KEY)?.trim() ?? ''
  return stored.length > 0 ? stored : null
}

export function clearPendingClaim(): void {
  localStorage.removeItem(PENDING_CLAIM_KEY)
}

export function loadAuthToken(): string | null {
  const stored = localStorage.getItem(AUTH_TOKEN_KEY)
  if (stored === null) return null
  const token = stored.trim()
  return token.length > 0 ? token : null
}

export function saveAuthToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_KEY, token)
}

export function clearAuthToken(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY)
}

export function saveSession(userId: string, token: string): void {
  saveUserId(userId)
  saveAuthToken(token)
}

export function isLoggedIn(): boolean {
  return loadAuthToken() !== null
}

export function setLoggedIn(value: boolean): void {
  if (!value) clearAuthToken()
}

const ANON_COUNT_KEY = 'rag.anonMessageCount'

/** How many messages an anonymous visitor may send before login is required. */
export const ANON_MESSAGE_LIMIT = 3

export function anonMessageCount(): number {
  const stored = Number(localStorage.getItem(ANON_COUNT_KEY))
  return Number.isFinite(stored) && stored > 0 ? Math.floor(stored) : 0
}

export function bumpAnonMessageCount(): void {
  localStorage.setItem(ANON_COUNT_KEY, String(anonMessageCount() + 1))
}

const OPEN_THREAD_KEY = 'rag.openThreadId'

/**
 * A thread the app should open on its next start. Written by the shared-chat
 * page when a reader continues a conversation in their own copy: there is no
 * router to carry the id, and the copy is worthless if it lands the reader on
 * an empty new chat. Read exactly once.
 */
export function savePendingOpenThread(threadId: string): void {
  localStorage.setItem(OPEN_THREAD_KEY, threadId)
}

export function takePendingOpenThread(): string | null {
  const stored = localStorage.getItem(OPEN_THREAD_KEY)?.trim() ?? ''
  localStorage.removeItem(OPEN_THREAD_KEY)
  return stored.length > 0 ? stored : null
}

const LANGUAGE_KEY = 'rag.language'

export type Language = 'en' | 'vi'

export function loadLanguage(): Language {
  return localStorage.getItem(LANGUAGE_KEY) === 'vi' ? 'vi' : 'en'
}

export function saveLanguage(language: Language): void {
  localStorage.setItem(LANGUAGE_KEY, language)
}

export function titleFrom(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= 42) return flat
  return `${flat.slice(0, 42).trimEnd()}...`
}

const UI_SNAPSHOT_KEY = 'rag.uiSnapshot'

export interface UiSnapshot {
  userId: string
  displayName: string | null
  threads: unknown[]
  projects: unknown[]
}

/**
 * The sidebar's last known shape, rendered instantly on the next start while
 * the real data loads behind it. This is what hides the 1.5-2.5s the Firebase
 * SDK spends restoring a cold session over the network: without it the app
 * sits on placeholders looking broken for exactly that long. Names and titles
 * only -- never message content, and never another user's snapshot (the
 * userId check below).
 */
export function saveUiSnapshot(snapshot: UiSnapshot): void {
  try {
    localStorage.setItem(UI_SNAPSHOT_KEY, JSON.stringify(snapshot))
  } catch {
    // Quota exceeded: the snapshot is a nicety, never worth an error.
  }
}

export function loadUiSnapshot(userId: string): UiSnapshot | null {
  const raw = localStorage.getItem(UI_SNAPSHOT_KEY)
  if (raw === null) return null
  try {
    const parsed = JSON.parse(raw) as UiSnapshot
    if (parsed.userId !== userId) return null
    return {
      userId: parsed.userId,
      displayName: typeof parsed.displayName === 'string' ? parsed.displayName : null,
      threads: Array.isArray(parsed.threads) ? parsed.threads : [],
      projects: Array.isArray(parsed.projects) ? parsed.projects : [],
    }
  } catch {
    return null
  }
}

export function clearUiSnapshot(): void {
  localStorage.removeItem(UI_SNAPSHOT_KEY)
}
