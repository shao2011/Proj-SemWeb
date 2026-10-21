import type {
  AnswerResponse,
  CompactThreadResponse,
  HealthResponse,
  Memory,
  MemoryScope,
  MemoryType,
  ModelCatalog,
  Project,
  ProjectFile,
  ProjectMember,
  Provider,
  QueryResponse,
  ReportReason,
  ShareAccess,
  SharedChat,
  SharedWithMeItem,
  ShareView,
  Thread,
  ThreadDetail,
  ThreadMessage,
  UploadBatchSummary,
  UploadFileResult,
  UserSettings,
} from './types'
import { signOut } from 'firebase/auth'
import { firebaseAuth } from './firebase'
import { loadAuthToken, resetIdentity, saveAuthToken, setLoggedIn } from './storage'

export const API_BASE = '/api'

/**
 * Fetch with identity headers, retrying exactly once on 401 with a
 * force-refreshed Firebase token. Covers the request fired right as the
 * hourly token expires (a tab waking from sleep) without a re-login.
 */
/** The token's own expiry, read locally so a stale one is never sent. */
function tokenExpiresWithin(token: string, seconds: number): boolean {
  const payload = token.split('.')[1]
  if (payload === undefined) return false
  try {
    const claims = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/'))) as {
      exp?: unknown
    }
    if (typeof claims.exp !== 'number') return false
    return claims.exp - Date.now() / 1000 < seconds
  } catch {
    return false
  }
}

/**
 * The stored token, refreshed up front when it is about to expire. Doing this
 * before the request (instead of reacting to a 401) matters at startup: the
 * opening burst is many parallel requests, and reacting meant each of them
 * paid a 401 round-trip and forced its own token mint. Here the expiry is
 * read locally, and the SDK deduplicates concurrent non-forced refreshes, so
 * the burst shares one mint. Anonymous visitors (no token) skip all of it.
 */
async function freshToken(): Promise<string | null> {
  const stored = loadAuthToken()
  if (stored === null) return null
  if (!tokenExpiresWithin(stored, 60)) return stored
  await firebaseAuth.authStateReady()
  const user = firebaseAuth.currentUser
  // No session to refresh from: send the stale token and let the 401
  // backstop below decide whether the session is dead.
  if (user === null) return stored
  try {
    const minted = await user.getIdToken()
    if (minted !== stored) saveAuthToken(minted)
    return minted
  } catch {
    return stored
  }
}

async function fetchWithAuth(
  path: string,
  userId: string,
  init: RequestInit,
): Promise<Response> {
  const attempt = (token: string | null) => {
    const headers = new Headers(init.headers)
    headers.set('X-User-ID', userId)
    if (token !== null) headers.set('Authorization', `Bearer ${token}`)
    if (init.body !== undefined && !(init.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json')
    }
    return fetch(`${API_BASE}${path}`, { ...init, headers })
  }

  const token = await freshToken()
  let response = await attempt(token)
  if (response.status === 401 && token !== null) {
    // Wait for the SDK before judging the session. At startup the stored
    // token can be expired while Firebase is still restoring the session
    // from IndexedDB (a network round-trip), and `currentUser` is null for
    // that whole window -- a healthy login that only *looks* dead. This
    // await also makes the app's opening burst of requests recover on their
    // own retry instead of failing until something reloads.
    await firebaseAuth.authStateReady()
    const user = firebaseAuth.currentUser
    if (user !== null) {
      try {
        const fresh = await user.getIdToken(true)
        if (fresh !== token) {
          saveAuthToken(fresh)
          response = await attempt(fresh)
        }
      } catch {
        // Refresh failed; the original 401 stands.
      }
    }
    // Still 401 after the SDK settled: the stored token is dead and no fresh
    // one could be minted (no Firebase session, or its refresh failed). The
    // account is unreachable, so keeping the app "logged in" would just spam
    // 401s with a stale identity. Log out the same way the sidebar button
    // does. The hourly-expiry case never lands here: it refreshed above.
    if (response.status === 401) {
      expireSession()
    }
  }
  return response
}

// Once: every in-flight request hits the same dead session at the same time,
// and one reload is the whole remedy.
let sessionExpired = false

function expireSession(): void {
  if (sessionExpired) return
  sessionExpired = true
  setLoggedIn(false)
  // A fresh anonymous identity, exactly like a manual logout: the uid must
  // not linger as the browse-mode identity, and the backend refuses the
  // X-User-ID fallback for non-anonymous ids anyway.
  resetIdentity()
  // Reload in place rather than jumping to '/': a reader on a share link
  // stays on it, and the page comes back anonymous either way.
  void signOut(firebaseAuth).finally(() => {
    window.location.reload()
  })
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

interface FastApiDetailItem {
  msg?: string
}

function readDetail(body: unknown, fallback: string): string {
  if (typeof body !== 'object' || body === null) return fallback
  const detail = (body as { detail?: unknown }).detail
  if (typeof detail === 'string' && detail.length > 0) return detail
  if (Array.isArray(detail)) {
    const messages = (detail as FastApiDetailItem[])
      .map((item) => item?.msg)
      .filter((msg): msg is string => typeof msg === 'string' && msg.length > 0)
    if (messages.length > 0) return messages.join(', ')
  }
  return fallback
}

async function request<T>(path: string, userId: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetchWithAuth(path, userId, init)
  } catch {
    throw new ApiError(0, 'Cannot reach the API. Is the backend running on port 8000?')
  }

  if (response.status === 204) return undefined as T

  const raw = await response.text()
  let body: unknown = null
  if (raw.length > 0) {
    try {
      body = JSON.parse(raw)
    } catch {
      body = null
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, readDetail(body, `Request failed (${response.status})`))
  }
  return body as T
}

async function streamRequest(
  path: string,
  userId: string,
  init: RequestInit,
  onEvent: (event: string, payload: Record<string, unknown>) => void,
): Promise<void> {
  let response: Response
  try {
    response = await fetchWithAuth(path, userId, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'Cannot reach the API. Is the backend running on port 8000?')
  }

  if (!response.ok) {
    const raw = await response.text()
    let body: unknown = null
    try {
      body = JSON.parse(raw)
    } catch {
      body = null
    }
    throw new ApiError(response.status, readDetail(body, `Request failed (${response.status})`))
  }
  if (response.body === null) {
    throw new ApiError(0, 'The API returned no stream')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatch = (block: string): void => {
    let event = ''
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).trim())
    }
    if (data.length === 0) return
    let payload: Record<string, unknown>
    try {
      payload = JSON.parse(data.join('\n')) as Record<string, unknown>
    } catch {
      return
    }
    onEvent(event, payload)
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let split = buffer.indexOf('\n\n')
    while (split !== -1) {
      dispatch(buffer.slice(0, split))
      buffer = buffer.slice(split + 2)
      split = buffer.indexOf('\n\n')
    }
  }
  if (buffer.trim().length > 0) dispatch(buffer)
}

export function getHealth(userId: string): Promise<HealthResponse> {
  return request<HealthResponse>('/health', userId)
}

export interface ClaimSummary {
  projects: number
  threads: number
  memories: number
}

/** Store the login provider's profile (name, email) in user settings. */
export function updateProfile(
  userId: string,
  displayName: string | null,
  email: string | null,
): Promise<UserSettings> {
  return request<UserSettings>('/settings', userId, {
    method: 'PATCH',
    body: JSON.stringify({ display_name: displayName, email }),
  })
}

/**
 * Move everything the anonymous UUID owns to the logged-in account. The
 * backend takes the target identity from the Bearer token, so this must be
 * called after the Firebase token is stored.
 */
export function claimAccount(anonymousUserId: string): Promise<ClaimSummary> {
  return request<ClaimSummary>('/auth/claim', anonymousUserId, {
    method: 'POST',
    body: JSON.stringify({ anonymous_user_id: anonymousUserId }),
  })
}

export function listProjects(userId: string): Promise<Project[]> {
  return request<Project[]>('/projects', userId)
}

export function createProject(
  userId: string,
  name: string,
  provider: Provider,
  icon: string | null = null,
  color: string | null = null,
): Promise<Project> {
  return request<Project>('/projects', userId, {
    method: 'POST',
    body: JSON.stringify({ name, provider, icon, color }),
  })
}

export function renameProject(
  userId: string,
  projectId: string,
  name: string,
): Promise<Project> {
  return request<Project>(`/projects/${encodeURIComponent(projectId)}`, userId, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  })
}

export function deleteProject(userId: string, projectId: string): Promise<void> {
  return request<void>(`/projects/${encodeURIComponent(projectId)}`, userId, {
    method: 'DELETE',
  })
}

// --- project members -------------------------------------------------------

export function listProjectMembers(
  userId: string,
  projectId: string,
): Promise<ProjectMember[]> {
  return request<ProjectMember[]>(
    `/projects/${encodeURIComponent(projectId)}/members`,
    userId,
  )
}

/** Add somebody, or change the role they already have. */
export function addProjectMember(
  userId: string,
  projectId: string,
  email: string,
  role: 'viewer' | 'editor',
): Promise<ProjectMember[]> {
  return request<ProjectMember[]>(
    `/projects/${encodeURIComponent(projectId)}/members`,
    userId,
    { method: 'POST', body: JSON.stringify({ email, role }) },
  )
}

export function removeProjectMember(
  userId: string,
  projectId: string,
  email: string,
): Promise<void> {
  return request<void>(
    `/projects/${encodeURIComponent(projectId)}/members`,
    userId,
    { method: 'DELETE', body: JSON.stringify({ email }) },
  )
}

/** Hand the project over. The caller stays on as an editor. */
export function transferProject(
  userId: string,
  projectId: string,
  email: string,
): Promise<Project> {
  return request<Project>(
    `/projects/${encodeURIComponent(projectId)}/transfer`,
    userId,
    { method: 'POST', body: JSON.stringify({ email }) },
  )
}

export function listDocuments(userId: string, projectId: string): Promise<ProjectFile[]> {
  return request<ProjectFile[]>(`/projects/${encodeURIComponent(projectId)}/documents`, userId)
}

export interface DocumentMarkdown {
  doc_id: string
  markdown: string
  anchors: Record<string, [number, number]>
}

export function getDocumentMarkdown(
  userId: string,
  projectId: string,
  docId: string,
): Promise<DocumentMarkdown> {
  return request<DocumentMarkdown>(
    `/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(docId)}/markdown`,
    userId,
  )
}

/**
 * Fetch a stored image referenced from a document's markdown.
 *
 * The route requires the identity headers, so the bytes cannot be reached by
 * pointing an `<img src>` at it: the caller turns this blob into an object URL
 * instead.
 */
export async function getDocumentMedia(
  userId: string,
  projectId: string,
  key: string,
): Promise<Blob> {
  const response = await fetchWithAuth(
    `/projects/${encodeURIComponent(projectId)}/media/${key
      .split('/')
      .map(encodeURIComponent)
      .join('/')}`,
    userId,
    {},
  )
  if (!response.ok) {
    throw new ApiError(response.status, 'Could not load this image.')
  }
  return response.blob()
}

export interface UploadHandlers {
  onResult?: (result: UploadFileResult) => void
}

export async function uploadDocuments(
  userId: string,
  projectId: string,
  files: File[],
  handlers: UploadHandlers = {},
): Promise<UploadBatchSummary> {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  let summary: UploadBatchSummary | null = null
  await streamRequest(
    `/projects/${encodeURIComponent(projectId)}/documents`,
    userId,
    { method: 'POST', body: form },
    (event, payload) => {
      if (event === 'file_result') {
        handlers.onResult?.(payload as unknown as UploadFileResult)
      } else if (event === 'done') {
        summary = payload as unknown as UploadBatchSummary
      }
    },
  )
  if (summary === null) throw new ApiError(0, 'Upload stream ended before completion')
  return summary
}

export function askAnswer(
  userId: string,
  projectId: string,
  query: string,
  limit = 10,
): Promise<AnswerResponse> {
  return request<AnswerResponse>(`/projects/${encodeURIComponent(projectId)}/answer`, userId, {
    method: 'POST',
    body: JSON.stringify({ query, limit }),
  })
}

// --- threads ---------------------------------------------------------------

export function listThreads(userId: string): Promise<Thread[]> {
  return request<Thread[]>('/threads', userId)
}

export function createThread(
  userId: string,
  title: string,
  projectId: string | null,
): Promise<Thread> {
  return request<Thread>('/threads', userId, {
    method: 'POST',
    body: JSON.stringify({ title, project_id: projectId }),
  })
}

export function listModels(userId: string): Promise<ModelCatalog> {
  return request<ModelCatalog>('/models', userId)
}

export function compactThread(
  userId: string,
  threadId: string,
  instructions?: string,
): Promise<CompactThreadResponse> {
  return request<CompactThreadResponse>(
    `/threads/${encodeURIComponent(threadId)}/compact`,
    userId,
    {
      method: 'POST',
      ...(instructions ? { body: JSON.stringify({ instructions }) } : {}),
    },
  )
}

export function getThread(userId: string, threadId: string): Promise<ThreadDetail> {
  return request<ThreadDetail>(`/threads/${encodeURIComponent(threadId)}`, userId)
}

export function renameThread(
  userId: string,
  threadId: string,
  title: string,
): Promise<Thread> {
  return request<Thread>(`/threads/${encodeURIComponent(threadId)}`, userId, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

export function deleteThread(userId: string, threadId: string): Promise<void> {
  return request<void>(`/threads/${encodeURIComponent(threadId)}`, userId, {
    method: 'DELETE',
  })
}

// --- fork and report -------------------------------------------------------

/**
 * Copy the conversation up to `throughPosition` into a new thread. The
 * original keeps every message it had; the branch is where the next turn
 * goes.
 */
export function forkThread(
  userId: string,
  threadId: string,
  throughPosition: number,
): Promise<Thread> {
  return request<Thread>(`/threads/${encodeURIComponent(threadId)}/fork`, userId, {
    method: 'POST',
    body: JSON.stringify({ through_position: throughPosition }),
  })
}

export interface ReportView {
  report_id: string
  created_at: string
}

/** The conversation snapshot is taken server-side, not sent from here. */
export function reportMessage(
  userId: string,
  threadId: string,
  messagePosition: number | null,
  reason: ReportReason,
  details: string | null,
): Promise<ReportView> {
  return request<ReportView>('/reports', userId, {
    method: 'POST',
    body: JSON.stringify({
      thread_id: threadId,
      message_position: messagePosition,
      reason,
      details: details !== null && details.length > 0 ? details : null,
    }),
  })
}

// --- sharing ---------------------------------------------------------------

/**
 * Create the chat's link, or return the one it already has. `refresh` extends
 * an existing snapshot to the thread as it is now; without it, reopening the
 * dialog never publishes turns written since the link was made.
 */
export function shareThread(
  userId: string,
  threadId: string,
  refresh = false,
  access?: ShareAccess,
): Promise<ShareView> {
  return request<ShareView>(`/threads/${encodeURIComponent(threadId)}/share`, userId, {
    method: 'POST',
    body: JSON.stringify(access === undefined ? { refresh } : { refresh, access }),
  })
}

/** Invite one address. The first invitation closes the public link. */
export function addShareRecipient(
  userId: string,
  threadId: string,
  email: string,
): Promise<ShareView> {
  return request<ShareView>(
    `/threads/${encodeURIComponent(threadId)}/share/recipients`,
    userId,
    { method: 'POST', body: JSON.stringify({ email }) },
  )
}

export function removeShareRecipient(
  userId: string,
  threadId: string,
  email: string,
): Promise<void> {
  return request<void>(
    `/threads/${encodeURIComponent(threadId)}/share/recipients`,
    userId,
    { method: 'DELETE', body: JSON.stringify({ email }) },
  )
}

/** Chats other people invited this account's address to. */
export function listSharedWithMe(userId: string): Promise<SharedWithMeItem[]> {
  return request<SharedWithMeItem[]>('/shared', userId)
}

/** Take an invited chat into a copy of your own, and get that thread back. */
export function forkSharedChat(userId: string, shareId: string): Promise<Thread> {
  return request<Thread>(`/share/${encodeURIComponent(shareId)}/fork`, userId, {
    method: 'POST',
  })
}

/** The chat's current link, or a 404 when it is not shared. */
export function getThreadShare(userId: string, threadId: string): Promise<ShareView> {
  return request<ShareView>(`/threads/${encodeURIComponent(threadId)}/share`, userId)
}

export function revokeThreadShare(userId: string, threadId: string): Promise<void> {
  return request<void>(`/threads/${encodeURIComponent(threadId)}/share`, userId, {
    method: 'DELETE',
  })
}

/**
 * Read a shared chat.
 *
 * Signed out it sends no identity at all: a public link must work for someone
 * with no account, and a header would only leak who is reading it. Signed in
 * it goes through `fetchWithAuth`, because an invite share decides access from
 * the verified address inside the token.
 */
export async function getSharedChat(
  shareId: string,
  userId: string | null = null,
): Promise<SharedChat> {
  const path = `/share/${encodeURIComponent(shareId)}`
  let response: Response
  try {
    response =
      userId === null
        ? await fetch(`${API_BASE}${path}`)
        : await fetchWithAuth(path, userId, {})
  } catch {
    throw new ApiError(0, 'Cannot reach the API.')
  }
  const raw = await response.text()
  let body: unknown = null
  try {
    body = JSON.parse(raw)
  } catch {
    body = null
  }
  if (!response.ok) {
    throw new ApiError(response.status, readDetail(body, 'This link is no longer valid'))
  }
  return body as SharedChat
}

// --- memory and settings ---------------------------------------------------

export function listMemories(userId: string, scope?: MemoryScope): Promise<Memory[]> {
  const query = scope === undefined ? '' : `?scope=${scope}`
  return request<Memory[]>(`/memory${query}`, userId)
}

export function createMemory(
  userId: string,
  type: MemoryType,
  key: string,
  value: string,
): Promise<Memory> {
  return request<Memory>('/memory', userId, {
    method: 'POST',
    body: JSON.stringify({ scope: 'user', type, key, value }),
  })
}

export function updateMemory(userId: string, memoryId: string, value: string): Promise<Memory> {
  return request<Memory>(`/memory/${encodeURIComponent(memoryId)}`, userId, {
    method: 'PATCH',
    body: JSON.stringify({ value }),
  })
}

export function deleteMemory(userId: string, memoryId: string): Promise<void> {
  return request<void>(`/memory/${encodeURIComponent(memoryId)}`, userId, {
    method: 'DELETE',
  })
}

export function getSettings(userId: string): Promise<UserSettings> {
  return request<UserSettings>('/settings', userId)
}

export interface LlmUsage {
  owner_user_id: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  requests: number
}

export function getUsage(userId: string): Promise<LlmUsage> {
  return request<LlmUsage>('/usage', userId)
}

export function setMemoryEnabled(userId: string, enabled: boolean): Promise<UserSettings> {
  return request<UserSettings>('/settings', userId, {
    method: 'PATCH',
    body: JSON.stringify({ memory_enabled: enabled }),
  })
}

/** The `done` payload: the stored message plus the server-computed gauge. */
export type DoneMessage = ThreadMessage & {
  context_used_percent?: number | null
  model_id?: string
}

export interface StreamHandlers {
  onUserMessage?: (message: ThreadMessage) => void
  onSources?: (sources: QueryResponse) => void
  onDelta?: (text: string) => void
  onDone?: (message: DoneMessage) => void
  onError?: (detail: string) => void
}

/**
 * Send a message and consume the SSE stream.
 *
 * The server answers 200 before generation starts, so a mid-stream failure
 * arrives as an `error` event rather than an HTTP status.
 */
export async function streamThreadMessage(
  userId: string,
  threadId: string,
  content: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
  disabledDocIds?: string[],
  modelId?: string | null,
): Promise<void> {
  const body: Record<string, unknown> = { content }
  if (disabledDocIds && disabledDocIds.length > 0) {
    body.disabled_doc_ids = disabledDocIds
  }
  if (modelId) body.model_id = modelId
  await streamRequest(
    `/threads/${encodeURIComponent(threadId)}/messages`,
    userId,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    },
    (event, payload) => {
    switch (event) {
      case 'user_message':
        handlers.onUserMessage?.(payload as unknown as ThreadMessage)
        break
      case 'sources':
        handlers.onSources?.(payload as unknown as QueryResponse)
        break
      case 'delta':
        handlers.onDelta?.(String(payload.text ?? ''))
        break
      case 'done':
        handlers.onDone?.(payload as unknown as DoneMessage)
        break
      case 'error':
        handlers.onError?.(String(payload.detail ?? 'Something went wrong.'))
        break
    }
    },
  )
}

// --- admin ---------------------------------------------------------------------

export interface AdminMe {
  email: string
  bootstrap: boolean
}

export interface AdminUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  requests: number
}

export interface AdminUser {
  uid: string
  email: string | null
  display_name: string | null
  first_login_at: string
  last_login_at: string
  disabled_at: string | null
  usage: AdminUsage
}

export interface AdminGrant {
  email: string
  granted_by: string
  created_at: string
}

export interface AdminList {
  bootstrap: string[]
  granted: AdminGrant[]
}

export interface AdminReportTurn {
  role: string
  content: string
}

export interface AdminReport {
  report_id: string
  owner_user_id: string
  thread_id: string
  message_position: number | null
  reason: string
  details: string | null
  conversation: AdminReportTurn[]
  created_at: string
  resolved_at: string | null
  resolved_by: string | null
}

export interface AdminAuditEntry {
  audit_id: string
  actor_email: string
  action: string
  target: string
  detail: string | null
  created_at: string
}

export function adminMe(userId: string): Promise<AdminMe> {
  return request(`/admin/me`, userId)
}

export function adminListUsers(userId: string): Promise<AdminUser[]> {
  return request(`/admin/users`, userId)
}

export function adminSetUserDisabled(
  userId: string,
  uid: string,
  disabled: boolean,
): Promise<AdminUser> {
  const action = disabled ? 'disable' : 'enable'
  return request(`/admin/users/${encodeURIComponent(uid)}/${action}`, userId, {
    method: 'POST',
  })
}

export function adminListReports(
  userId: string,
  includeResolved: boolean,
): Promise<AdminReport[]> {
  return request(`/admin/reports?include_resolved=${includeResolved}`, userId)
}

export function adminResolveReport(userId: string, reportId: string): Promise<AdminReport> {
  return request(`/admin/reports/${encodeURIComponent(reportId)}/resolve`, userId, {
    method: 'POST',
  })
}

export function adminListAdmins(userId: string): Promise<AdminList> {
  return request(`/admin/admins`, userId)
}

export function adminGrantAdmin(userId: string, email: string): Promise<AdminGrant> {
  return request(`/admin/admins`, userId, {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export function adminRevokeAdmin(userId: string, email: string): Promise<void> {
  return request(`/admin/admins/${encodeURIComponent(email)}`, userId, {
    method: 'DELETE',
  })
}

export function adminListAudit(userId: string): Promise<AdminAuditEntry[]> {
  return request(`/admin/audit`, userId)
}

export interface AdminStats {
  users: { total: number; active_7d: number; disabled: number }
  usage: AdminUsage
  content: {
    threads: number
    messages: number
    projects: number
    documents: number
    documents_failed: number
    memories: number
  }
  reports: { open: number; resolved: number }
  top_users: {
    email: string | null
    display_name: string | null
    total_tokens: number
    requests: number
  }[]
}

export function adminStats(userId: string): Promise<AdminStats> {
  return request(`/admin/stats`, userId)
}
