import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  adminMe,
  isAbortError,
  compactThread,
  createMemory,
  createProject,
  createThread,
  deleteMemory,
  deleteProject,
  deleteThread,
  forkThread,
  getHealth,
  getSettings,
  getThread,
  listDocuments,
  listModels,
  listMemories,
  listProjects,
  listSharedWithMe,
  listThreads,
  renameProject,
  renameThread,
  setMemoryEnabled,
  streamThreadMessage,
  updateMemory,
  uploadDocuments,
} from './lib/api'
import {
  ANON_MESSAGE_LIMIT,
  anonMessageCount,
  bumpAnonMessageCount,
  loadUiSnapshot,
  loadUserId,
  saveUiSnapshot,
  saveUserId,
  takePendingOpenThread,
  titleFrom,
} from './lib/storage'
import type {
  ChatMessage,
  Memory,
  MemoryType,
  ModelOption,
  PendingUpload,
  Project,
  ProjectFile,
  Provider,
  QueryHit,
  SharedWithMeItem,
  Thread,
  UserSettings,
} from './lib/types'
import { Composer } from './components/Composer'
import { Message, Typing } from './components/Message'
import {
  ChatsPanel,
  ProjectPanel,
  ProjectsPanel,
  SettingsPanel,
} from './components/Panels'
import { ManageMemoryDialog } from './components/Personalization'
import { LibraryPanel, type LibraryEntry } from './components/LibraryPanel'
import { SettingsDialog } from './components/SettingsDialog'
import { NewProjectDialog } from './components/NewProjectDialog'
import { Sidebar, type View } from './components/Sidebar'
import { ThreadOutline } from './components/ThreadOutline'
import { ConfirmDialog } from './components/ConfirmDialog'
import { ShareDialog } from './components/ShareDialog'
import { ReportDialog } from './components/ReportDialog'
import { ProjectMembersDialog } from './components/ProjectMembersDialog'
import { LoginModal } from './components/LoginPage'
import { DocPanel, type DocFocus } from './components/DocPanel'
import {
  BriefcaseIcon,
  BulbIcon,
  ChevronIcon,
  CodeIcon,
  DocumentsIcon,
  PanelIcon,
  PencilIcon,
  ShareIcon,
  TrashIcon,
} from './components/icons'

const STARTERS = [
  { icon: CodeIcon, label: 'Extract data', prompt: 'Extract the key figures and tables from my documents.' },
  { icon: PencilIcon, label: 'Summarize', prompt: 'Summarize my documents in a few short paragraphs.' },
  { icon: BriefcaseIcon, label: 'Key findings', prompt: 'What are the main findings in my documents?' },
  { icon: BulbIcon, label: 'Suggest questions', prompt: 'Suggest good questions I could ask about my documents.' },
]

function messageFrom(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

interface TurnGroup {
  user: ChatMessage | null
  answers: ChatMessage[]
}

/**
 * Retry stores a second user/assistant pair with the identical question, but
 * the UI shows one question with the answers as switchable versions. A
 * consecutive user message repeating the previous one is therefore folded
 * into the previous turn instead of opening a new one.
 */
function groupTurns(messages: ChatMessage[]): TurnGroup[] {
  const groups: TurnGroup[] = []
  for (const message of messages) {
    const last = groups[groups.length - 1]
    if (message.role === 'user') {
      const isRetry =
        last !== undefined &&
        last.user !== null &&
        last.user.text === message.text &&
        last.answers.length > 0
      if (!isRetry) groups.push({ user: message, answers: [] })
    } else if (last === undefined) {
      groups.push({ user: null, answers: [message] })
    } else {
      last.answers.push(message)
    }
  }
  return groups
}

/**
 * The creation dialog no longer asks for a name up front; this is what fills
 * it in. Checked against the projects already loaded so a run of quick
 * creates reads as "New project 2", "New project 3", not three identical rows.
 */
function nextProjectName(existing: Project[]): string {
  const base = 'New project'
  const taken = new Set(existing.map((project) => project.name))
  if (!taken.has(base)) return base
  let suffix = 2
  while (taken.has(`${base} ${suffix}`)) suffix += 1
  return `${base} ${suffix}`
}

interface AppProps {
  loggedIn: boolean
  onLogin: () => void
  loginBusy?: boolean
  loginError?: string | null
}

export default function App({
  loggedIn,
  onLogin,
  loginBusy = false,
  loginError = null,
}: AppProps) {
  const [userId, setUserId] = useState(() => loadUserId())
  // Last session's sidebar shape, rendered instantly while the Firebase SDK
  // spends its 1.5-2.5s restoring a cold session; the real fetches replace it.
  const [snapshot] = useState(() => loadUiSnapshot(loadUserId()))
  const [collapsed, setCollapsed] = useState(false)
  const [view, setView] = useState<View>('chat')
  const [threads, setThreads] = useState<Thread[]>(() => (snapshot?.threads as Thread[]) ?? [])
  const [sharedWithMe, setSharedWithMe] = useState<SharedWithMeItem[]>([])
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState<string | null>(null)
  // Chosen answer version per turn, keyed by the turn's user message id;
  // absent means "latest".
  const [versionPick, setVersionPick] = useState<Record<string, number>>({})
  // True while a Retry attempt is streaming: the turn's previous answer hides
  // so only the loading/stream shows, and a Stop rolls the attempt back.
  const [regenerating, setRegenerating] = useState(false)
  const [headMenuOpen, setHeadMenuOpen] = useState(false)
  const [headRenaming, setHeadRenaming] = useState(false)
  const [headDraft, setHeadDraft] = useState('')
  const [headDeleteOpen, setHeadDeleteOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [reportPosition, setReportPosition] = useState<number | null>(null)
  const [reportOpen, setReportOpen] = useState(false)
  const [membersOpen, setMembersOpen] = useState(false)
  // Measured from the title pill as the rename starts, so the input opens at
  // exactly the size of the text it replaces.
  const [headRenameWidth, setHeadRenameWidth] = useState<number | null>(null)
  const headMenuRef = useRef<HTMLDivElement>(null)
  const chatPillRef = useRef<HTMLButtonElement>(null)
  const [docPanelOpen, setDocPanelOpen] = useState(false)
  const [docFocus, setDocFocus] = useState<DocFocus | null>(null)
  const [loginModalOpen, setLoginModalOpen] = useState(false)
  // The message that hit the anonymous limit, sent automatically after login.
  const [pendingLoginText, setPendingLoginText] = useState<string | null>(null)
  const [projects, setProjects] = useState<Project[]>(() => (snapshot?.projects as Project[]) ?? [])
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null)
  // Documents the user unchecked in the panel: retrieval skips them for every
  // question in this session until re-checked. Reset on project switch.
  const [disabledDocIds, setDisabledDocIds] = useState<Set<string>>(new Set())
  const [files, setFiles] = useState<ProjectFile[]>([])
  const [pendingUploads, setPendingUploads] = useState<PendingUpload[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [busy, setBusy] = useState(false)
  // The chat-model catalog and the user's pick. Null = the server's default;
  // the pick applies from the next message, never retroactively.
  const [models, setModels] = useState<ModelOption[]>([])
  const [modelId, setModelId] = useState<string | null>(null)
  // Server-computed gauge for the open thread. Updated only by measurements
  // (thread open, `done`, compact) -- deliberately not recomputed on model
  // switch, per design: the gauge reflects the last measured turn.
  const [contextPercent, setContextPercent] = useState<number | null>(null)
  const [compacting, setCompacting] = useState(false)
  const [memories, setMemories] = useState<Memory[]>([])
  const [settings, setSettings] = useState<UserSettings | null>(null)
  const [manageMemoryOpen, setManageMemoryOpen] = useState(false)
  const [settingsDialogOpen, setSettingsDialogOpen] = useState(false)
  const [isAdmin, setIsAdmin] = useState(false)
  const [libraryEntries, setLibraryEntries] = useState<LibraryEntry[]>([])
  const [libraryLoading, setLibraryLoading] = useState(false)
  const [newProjectOpen, setNewProjectOpen] = useState(false)
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  // Whether the view should follow new tokens: true while the user is at (or
  // near) the bottom, false the moment they scroll up to read something.
  const stickToBottomRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)
  // A background upload batch outlives the render it started in, so it needs
  // the current project at the moment it finishes, not the one closed over
  // when the batch began.
  const activeProjectIdRef = useRef(activeProjectId)

  const activeProject = projects.find((item) => item.project_id === activeProjectId) ?? null
  const activeThread = threads.find((item) => item.thread_id === activeThreadId) ?? null

  useEffect(() => {
    activeProjectIdRef.current = activeProjectId
  }, [activeProjectId])

  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error })
  }, [])

  useEffect(() => {
    if (toast === null) return
    const timer = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(timer)
  }, [toast])

  const refreshThreads = useCallback(
    async (id: string) => {
      try {
        setThreads(await listThreads(id))
      } catch (error) {
        notify(messageFrom(error), true)
      }
    },
    [notify],
  )

  // The library aggregates documents across every project, so it fetches on
  // entry rather than keeping a global file list in sync all session.
  useEffect(() => {
    if (view !== 'library') return
    let cancelled = false
    setLibraryLoading(true)
    Promise.all(
      projects.map(async (project) => {
        const projectFiles = await listDocuments(userId, project.project_id)
        return projectFiles.map((file) => ({ file, projectName: project.name }))
      }),
    )
      .then((groups) => {
        if (cancelled) return
        const entries = groups
          .flat()
          .sort((a, b) => b.file.created_at.localeCompare(a.file.created_at))
        setLibraryEntries(entries)
      })
      .catch((error) => {
        if (!cancelled) notify(messageFrom(error), true)
      })
      .finally(() => {
        if (!cancelled) setLibraryLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [view, projects, userId, notify])

  useEffect(() => {
    let cancelled = false
    async function bootstrap() {
      try {
        const [health, loadedProjects, loadedThreads, catalog] = await Promise.all([
          getHealth(userId),
          listProjects(userId),
          listThreads(userId),
          listModels(userId),
        ])
        if (cancelled) return
        setProviders(health.configured_providers)
        setProjects(loadedProjects)
        setActiveProjectId(loadedProjects[0]?.project_id ?? null)
        setThreads(loadedThreads)
        // The catalog's default is what "Auto" already means, so listing it
        // again would be the same choice twice.
        setModels(catalog.models.filter((item) => item.id !== catalog.default))
        // Parked by the shared-chat page when a reader continued a
        // conversation in their own copy; there is no router to carry it.
        const parked = takePendingOpenThread()
        if (parked !== null && loadedThreads.some((item) => item.thread_id === parked)) {
          void openThread(parked)
        }
      } catch (error) {
        if (!cancelled) notify(messageFrom(error), true)
      }
    }
    void bootstrap()

    // Separate from the bootstrap: "Shared with you" is keyed on a verified
    // address, so it 401s for anonymous visitors, and that must not take the
    // rest of the start-up down with it.
    if (loggedIn) {
      void listSharedWithMe(userId)
        .then((items) => {
          if (!cancelled) setSharedWithMe(items)
        })
        .catch(() => {
          if (!cancelled) setSharedWithMe([])
        })
    } else {
      setSharedWithMe([])
    }
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, loggedIn, notify])

  /**
   * Memory loads separately from the bootstrap above: the routes answer 503 when
   * the backend has no memory store, and that must not take the chat down with
   * it.
   */
  const refreshMemory = useCallback(
    async (id: string) => {
      try {
        const [loaded, loadedSettings] = await Promise.all([
          listMemories(id, 'user'),
          getSettings(id),
        ])
        setMemories(loaded)
        setSettings(loadedSettings)
      } catch (error) {
        setMemories([])
        setSettings(null)
        notify(messageFrom(error), true)
      }
    },
    [notify],
  )

  useEffect(() => {
    void refreshMemory(userId)
  }, [userId, refreshMemory])

  useEffect(() => {
    if (!loggedIn) return
    saveUiSnapshot({
      userId,
      displayName: settings?.display_name ?? null,
      threads,
      projects,
    })
  }, [userId, loggedIn, threads, projects, settings])

  // Only gates the sidebar menu entry; every admin route re-checks on the
  // server. A 403 here is the normal answer for everybody else.
  useEffect(() => {
    if (!loggedIn) {
      setIsAdmin(false)
      return
    }
    let cancelled = false
    void adminMe(userId)
      .then(() => {
        if (!cancelled) setIsAdmin(true)
      })
      .catch(() => {
        if (!cancelled) setIsAdmin(false)
      })
    return () => {
      cancelled = true
    }
  }, [userId, loggedIn])

  useEffect(() => {
    setDisabledDocIds(new Set())
    if (activeProjectId === null) {
      setFiles([])
      return
    }
    let cancelled = false
    listDocuments(userId, activeProjectId)
      .then((loaded) => {
        if (!cancelled) setFiles(loaded)
      })
      .catch((error: unknown) => {
        if (!cancelled) notify(messageFrom(error), true)
      })
    return () => {
      cancelled = true
    }
  }, [userId, activeProjectId, notify])

  useEffect(() => {
    const node = scrollRef.current
    // Scroll the container itself: scrollIntoView on an anchor stops short of
    // the true bottom by the height of the sticky composer dock, which then
    // trips the near-bottom check and silently turns following off.
    if (stickToBottomRef.current && node !== null) node.scrollTop = node.scrollHeight
  }, [messages.length, streaming, busy])

  function trackScroll() {
    const node = scrollRef.current
    if (node === null) return
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight
    stickToBottomRef.current = distance < 120
  }

  async function openThread(threadId: string) {
    setActiveThreadId(threadId)
    setView('chat')
    setMessages([])
    setVersionPick({})
    setContextPercent(null)
    try {
      const detail = await getThread(userId, threadId)
      setMessages(
        detail.messages.map((item) => ({
          id: item.message_id,
          role: item.role,
          text: item.content,
          citations: item.citations,
          position: item.position,
        })),
      )
      setContextPercent(detail.context_used_percent ?? null)
      // Reopening a thread puts the picker back on the model it last ran on,
      // when that model is still in the catalog.
      const last = detail.last_model_id ?? null
      setModelId(models.some((item) => item.id === last) ? last : null)
    } catch (error) {
      notify(messageFrom(error), true)
    }
  }

  async function compactActiveThread() {
    if (activeThreadId === null || compacting) return
    setCompacting(true)
    try {
      const outcome = await compactThread(userId, activeThreadId)
      setContextPercent(outcome.context_used_percent)
      if (outcome.changed) {
        notify(
          outcome.stalled
            ? 'Compacted some turns; more remain. Click again.'
            : 'Older turns were folded into the summary.',
        )
      } else {
        notify(
          outcome.stalled
            ? 'Compaction failed, nothing was folded. Try again.'
            : 'Nothing to compact yet.',
          outcome.stalled,
        )
      }
    } catch (error) {
      notify(messageFrom(error), true)
    } finally {
      setCompacting(false)
    }
  }

  /**
   * `projectId` only applies to the thread this call may create. Home is plain
   * chat and passes null; the project view passes its own id.
   *
   * `retry` marks a regenerate: the previous answer hides while the new one
   * streams, and stopping mid-stream rolls the whole attempt back (the
   * duplicate question is dropped and the partial text discarded) so the turn
   * reads as if Retry was never pressed.
   */
  async function send(text: string, projectId: string | null = null, retry = false) {
    if (!loggedIn) {
      // Anonymous visitors get a few messages to try the product; past the
      // limit the message is parked, the login modal opens in place, and the
      // post-login effect below sends it. History survives login because the
      // same userId keeps being used.
      if (anonMessageCount() >= ANON_MESSAGE_LIMIT) {
        setPendingLoginText(text)
        setLoginModalOpen(true)
        return
      }
      bumpAnonMessageCount()
    }
    if (busy) return
    setView('chat')
    setBusy(true)
    if (retry) setRegenerating(true)
    stickToBottomRef.current = true
    // New answers should surface as the latest version, not a stale pick.
    setVersionPick({})

    let threadId = activeThreadId
    try {
      if (threadId === null) {
        const created = await createThread(userId, titleFrom(text), projectId)
        threadId = created.thread_id
        setActiveThreadId(threadId)
        setThreads((prev) => [created, ...prev])
        setMessages([])
      }
    } catch (error) {
      setBusy(false)
      setRegenerating(false)
      notify(messageFrom(error), true)
      return
    }

    setMessages((prev) => [
      ...prev,
      { id: `pending-${Date.now()}`, role: 'user', text },
    ])
    setStreaming('')

    const controller = new AbortController()
    abortRef.current = controller

    let hits: QueryHit[] | undefined
    let accumulated = ''
    let finished = false
    try {
      await streamThreadMessage(userId, threadId, text, {
        onUserMessage: (message) => {
          setMessages((prev) => {
            const next = [...prev]
            next[next.length - 1] = {
              id: message.message_id,
              role: 'user',
              text: message.content,
              position: message.position,
            }
            return next
          })
        },
        onSources: (sources) => {
          hits = sources.hits
        },
        onDelta: (delta) => {
          accumulated += delta
          setStreaming(accumulated)
        },
        onDone: (message) => {
          finished = true
          setMessages((prev) => [
            ...prev,
            {
              id: message.message_id,
              role: 'assistant',
              text: message.content,
              citations: message.citations,
              hits,
              position: message.position,
            },
          ])
          if (message.context_used_percent !== undefined) {
            setContextPercent(message.context_used_percent)
          }
        },
        onError: (detail) => {
          finished = true
          setMessages((prev) => [
            ...prev,
            { id: `error-${Date.now()}`, role: 'assistant', text: detail, error: true },
          ])
        },
      }, controller.signal, Array.from(disabledDocIds), modelId)
    } catch (error) {
      if (isAbortError(error)) {
        if (retry) {
          // Stopping a regenerate rolls it back: drop the re-sent question so
          // the previous answer stands as if Retry was never pressed. (The
          // backend keeps the orphaned question row, but grouping folds it
          // away on reload.)
          setMessages((prev) => {
            const last = prev[prev.length - 1]
            return last !== undefined && last.role === 'user' && last.text === text
              ? prev.slice(0, -1)
              : prev
          })
        } else if (!finished && accumulated.length > 0) {
          // The user hit Stop on a normal send: keep whatever was already
          // streamed instead of dropping it or reporting a failure.
          setMessages((prev) => [
            ...prev,
            { id: `stopped-${Date.now()}`, role: 'assistant', text: accumulated, hits },
          ])
        }
      } else if (
        error instanceof ApiError &&
        error.status === 403 &&
        error.message === 'anonymous_message_limit_reached'
      ) {
        // The server-side trial limit fired (the client counter can lag or
        // be wiped): roll the unsent bubble back, park the text, and offer
        // login — same flow as the client-side gate.
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          return last !== undefined && last.role === 'user' && last.text === text
            ? prev.slice(0, -1)
            : prev
        })
        setPendingLoginText(text)
        setLoginModalOpen(true)
      } else {
        setMessages((prev) => [
          ...prev,
          { id: `error-${Date.now()}`, role: 'assistant', text: messageFrom(error), error: true },
        ])
      }
    } finally {
      abortRef.current = null
      setStreaming(null)
      setBusy(false)
      setRegenerating(false)
      void refreshThreads(userId)
    }
  }

  function stopStreaming() {
    abortRef.current?.abort()
  }

  // After an in-place login, adopt the new identity without a reload: the
  // uid replaced the anonymous UUID in storage, and switching the state here
  // re-runs every [userId] effect (threads, projects, settings, memories),
  // so the sidebar name and settings update immediately. The open thread
  // stays valid — claiming moved it to the uid.
  useEffect(() => {
    if (!loggedIn) return
    const stored = loadUserId()
    if (stored !== userId) setUserId(stored)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loggedIn])

  // After an in-place login, close the modal and send the parked message.
  useEffect(() => {
    if (!loggedIn) return
    setLoginModalOpen(false)
    if (pendingLoginText !== null) {
      const text = pendingLoginText
      setPendingLoginText(null)
      void send(text)
    }
    // send/pendingLoginText are intentionally not dependencies: this must run
    // exactly once per login flip.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loggedIn])

  useEffect(() => {
    if (!headMenuOpen) return
    const close = (event: MouseEvent) => {
      if (!headMenuRef.current?.contains(event.target as Node)) setHeadMenuOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setHeadMenuOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [headMenuOpen])

  function startHeadRename(title: string) {
    setHeadDraft(title)
    setHeadRenameWidth(chatPillRef.current?.offsetWidth ?? null)
    setHeadRenaming(true)
  }

  async function submitHeadRename() {
    if (activeThread === null) return
    const title = headDraft.trim()
    if (title.length === 0) {
      setHeadRenaming(false)
      return
    }
    if (await renameThreadHandler(activeThread.thread_id, title)) setHeadRenaming(false)
  }

  /** A citation chip was clicked: open the panel on that document and blocks. */
  function jumpToCitation(hit: QueryHit) {
    setDocPanelOpen(true)
    setDocFocus({ docId: hit.doc_id, blockIds: hit.source_block_ids, nonce: Date.now() })
  }

  function jumpToMessage(messageId: string) {
    stickToBottomRef.current = false
    document
      .getElementById(`msg-${messageId}`)
      ?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }

  /** Re-asks the latest user question as a fresh turn in the same thread. */
  function regenerate() {
    if (busy) return
    const lastUser = [...messages].reverse().find((item) => item.role === 'user')
    if (lastUser === undefined) return
    void send(lastUser.text, null, true)
  }

  /**
   * The target project is passed in rather than read from `activeProjectId`: a
   * plain chat opened while a project happens to be open must not index into
   * that project.
   */
  async function upload(file: File, project: Project | null) {
    if (project === null) {
      setView('projects')
      notify('Open a project before uploading a document.', true)
      return
    }
    setBusy(true)
    try {
      let resultMessage = ''
      let resultFailed = false
      await uploadDocuments(userId, project.project_id, [file], {
        onResult: (result) => {
          resultFailed = result.status === 'failed'
          resultMessage = resultFailed
            ? `${result.filename}: ${result.detail ?? 'Upload failed.'}`
            : `Indexed ${result.filename} into ${result.chunk_count ?? 0} chunks.`
        },
      })
      notify(resultMessage, resultFailed)
      if (project.project_id === activeProjectId) {
        setFiles(await listDocuments(userId, project.project_id))
      }
    } catch (error) {
      notify(messageFrom(error), true)
    } finally {
      setBusy(false)
    }
  }

  /**
   * Files can only be uploaded once the project exists, since the route is
   * `/projects/{id}/documents`. The project screen opens the moment the project
   * itself is created, not once every file is done: the queued files then index
   * in a bounded parallel batch, each shown in the files rail as a spinner that
   * flips to a check or an error mark as its own upload settles.
   */
  async function addProject(
    provider: Provider,
    queued: File[],
    icon: string | null = null,
    color: string | null = null,
    name = '',
  ) {
    let uploadProjectId: string | null = null
    setBusy(true)
    try {
      const created = await createProject(
        userId,
        name.length > 0 ? name : nextProjectName(projects),
        provider,
        icon,
        color,
      )
      setProjects((prev) => [...prev, created])
      setNewProjectOpen(false)
      openProject(created.project_id)

      if (queued.length === 0) {
        notify(`Project "${created.name}" is ready.`)
        return
      }

      const projectId = created.project_id
  uploadProjectId = projectId
      const keyed = queued.map((file, index) => ({
        key: `${index}:${file.name}:${file.size}`,
        file,
      }))
      setPendingUploads((prev) => [
        ...prev,
        ...keyed.map(({ key, file }) => ({
          key,
          projectId,
          name: file.name,
          status: 'uploading' as const,
        })),
      ])

      let indexed = 0
      let chunks = 0
      const failures: string[] = []
      await uploadDocuments(userId, projectId, queued, {
        onResult: (result) => {
          const queuedFile = keyed[result.index]
          if (queuedFile === undefined) return
          const { key, file } = queuedFile
          if (result.status === 'completed') {
          indexed += 1
            chunks += result.chunk_count ?? 0
          setPendingUploads((prev) =>
            prev.map((item) => (item.key === key ? { ...item, status: 'done' } : item)),
          )
          } else {
            failures.push(`${file.name}: ${result.detail ?? 'Upload failed.'}`)
          setPendingUploads((prev) =>
            prev.map((item) => (item.key === key ? { ...item, status: 'failed' } : item)),
          )
          }
        },
      })

      if (failures.length === 0) {
        notify(`"${created.name}" ready. Indexed ${indexed} file(s), ${chunks} chunks.`)
      } else {
        notify(
          `"${created.name}" created. Indexed ${indexed}/${queued.length}. ${failures[0]}`,
          true,
        )
      }
      if (activeProjectIdRef.current === projectId) {
        setFiles(await listDocuments(userId, projectId))
      }
      setPendingUploads((prev) => prev.filter((item) => item.projectId !== projectId))
    } catch (error) {
      if (uploadProjectId !== null) {
        setPendingUploads((prev) =>
          prev.filter((item) => item.projectId !== uploadProjectId),
        )
      }
      notify(messageFrom(error), true)
    } finally {
      setBusy(false)
    }
  }

  /**
   * Branch the conversation at `position` into a new chat and open it. The
   * original is untouched, so switching back to it costs nothing.
   */
  async function forkFrom(position: number) {
    if (activeThreadId === null) return
    if (!loggedIn) {
      setLoginModalOpen(true)
      return
    }
    try {
      const forked = await forkThread(userId, activeThreadId, position)
      setThreads((prev) => [forked, ...prev])
      await openThread(forked.thread_id)
    } catch (error) {
      if (error instanceof ApiError && error.message === 'login_required_to_fork') {
        setLoginModalOpen(true)
        return
      }
      notify(messageFrom(error), true)
    }
  }

  async function removeThread(threadId: string) {
    try {
      await deleteThread(userId, threadId)
      setThreads((prev) => prev.filter((item) => item.thread_id !== threadId))
      if (threadId === activeThreadId) {
        setActiveThreadId(null)
        setMessages([])
      }
    } catch (error) {
      notify(messageFrom(error), true)
    }
  }

  /**
   * Threads and project-scoped memory that pointed at this project are left as
   * they are: the backend has no cascade for either, and both already degrade
   * gracefully when the project behind them is gone (a thread's project lookup
   * falls back to null, and a send against it surfaces "Project not found" as a
   * normal error event rather than crashing).
   */
  async function removeProject(projectId: string) {
    try {
      await deleteProject(userId, projectId)
      setProjects((prev) => prev.filter((item) => item.project_id !== projectId))
      if (projectId === activeProjectId) {
        setActiveProjectId(null)
        setFiles([])
        if (view === 'project') setView('projects')
      }
    } catch (error) {
      notify(messageFrom(error), true)
    }
  }

  /** Returns whether the write landed, so the rename field only closes on a true. */
  async function renameThreadHandler(threadId: string, title: string): Promise<boolean> {
    try {
      const updated = await renameThread(userId, threadId, title)
      setThreads((prev) =>
        prev.map((item) => (item.thread_id === threadId ? updated : item)),
      )
      return true
    } catch (error) {
      notify(messageFrom(error), true)
      return false
    }
  }

  /** Returns whether the write landed, so the rename field only closes on a true. */
  async function renameProjectHandler(projectId: string, name: string): Promise<boolean> {
    try {
      const updated = await renameProject(userId, projectId, name)
      setProjects((prev) =>
        prev.map((item) => (item.project_id === projectId ? updated : item)),
      )
      return true
    } catch (error) {
      notify(messageFrom(error), true)
      return false
    }
  }

  /**
   * A key that already exists in this scope is superseded on the backend, so one
   * POST covers both create and overwrite. The list is refetched rather than
   * patched locally because the supersede drops the previous row.
   *
   * Returns whether the write landed. The caller clears its inputs on the way
   * out, and a rejected write must not look like a successful one.
   */
  async function saveMemory(
    type: MemoryType,
    key: string,
    value: string,
  ): Promise<boolean> {
    setBusy(true)
    try {
      await createMemory(userId, type, key, value)
      setMemories(await listMemories(userId, 'user'))
      notify('Saved.')
      return true
    } catch (error) {
      notify(messageFrom(error), true)
      return false
    } finally {
      setBusy(false)
    }
  }

  async function editMemory(memoryId: string, value: string): Promise<boolean> {
    setBusy(true)
    try {
      const updated = await updateMemory(userId, memoryId, value)
      setMemories((prev) =>
        prev.map((item) => (item.memory_id === memoryId ? updated : item)),
      )
      return true
    } catch (error) {
      notify(messageFrom(error), true)
      return false
    } finally {
      setBusy(false)
    }
  }

  async function removeMemory(memoryId: string) {
    try {
      await deleteMemory(userId, memoryId)
      setMemories((prev) => prev.filter((item) => item.memory_id !== memoryId))
    } catch (error) {
      notify(messageFrom(error), true)
    }
  }

  async function toggleMemory(enabled: boolean) {
    try {
      setSettings(await setMemoryEnabled(userId, enabled))
    } catch (error) {
      notify(messageFrom(error), true)
    }
  }

  function newChat() {
    setActiveThreadId(null)
    setMessages([])
    setStreaming(null)
    setVersionPick({})
    setContextPercent(null)
    setView('chat')
  }

  function openProject(projectId: string) {
    setActiveProjectId(projectId)
    setActiveThreadId(null)
    setMessages([])
    setStreaming(null)
    setView('project')
  }

  function changeUserId(next: string) {
    saveUserId(next)
    setUserId(next)
    setActiveProjectId(null)
    setProjects([])
    setThreads([])
    setActiveThreadId(null)
    setMessages([])
    setMemories([])
    setSettings(null)
    notify(`Signed in as ${next}.`)
  }

  const threadProject =
    activeThread?.project_id != null
      ? projects.find((item) => item.project_id === activeThread.project_id) ?? null
      : null
  /**
   * A chat carries a project only when the open thread was created inside one.
   * `activeProjectId` is the project being viewed, not the one being chatted
   * with, so it deliberately does not feed the chat screen.
   */
  const chatProject = activeThread !== null ? threadProject : null
  const contextProject = view === 'project' ? activeProject : chatProject

  function composerFor(project: Project | null) {
    return (
      <Composer
        disabled={false}
        busy={busy}
        onSend={(text) => void send(text, project?.project_id ?? null)}
        onAttach={(file) => void upload(file, project)}
        onStop={stopStreaming}
        models={models}
        modelId={modelId}
        onModelChange={setModelId}
        contextPercent={activeThreadId !== null ? contextPercent : null}
        compacting={compacting}
        onCompact={() => void compactActiveThread()}
      />
    )
  }

  const composer = composerFor(chatProject)
  const turnGroups = groupTurns(messages)

  return (
    <div className="shell">
      <Sidebar
        collapsed={collapsed}
        view={view}
        threads={threads}
        activeThreadId={activeThreadId}
        projects={projects}
        activeProjectId={activeProjectId}
        displayName={
          settings?.display_name ??
          memories.find((item) => item.key === 'nickname')?.value ??
          snapshot?.displayName ??
          null
        }
        loggedIn={loggedIn}
        sharedWithMe={sharedWithMe}
        onLogin={() => setLoginModalOpen(true)}
        projectName={contextProject?.name ?? null}
        onToggle={() => setCollapsed((prev) => !prev)}
        onNewChat={newChat}
        onSelectView={setView}
        onSelectThread={(threadId) => void openThread(threadId)}
        onDeleteThread={(threadId) => void removeThread(threadId)}
        onRenameThread={renameThreadHandler}
        onSelectProject={openProject}
        onNewProject={() => setNewProjectOpen(true)}
        onOpenSettings={() => setSettingsDialogOpen(true)}
        isAdmin={isAdmin}
      />

      <main className="main">
        <div className="main-head">
          {collapsed && (
            <button className="icon-btn" onClick={() => setCollapsed(false)} aria-label="Open sidebar">
              <PanelIcon size={17} />
            </button>
          )}
          {view === 'project' && activeProject !== null ? (
            <nav className="crumbs" aria-label="Breadcrumb">
              <button className="crumb" onClick={() => setView('projects')}>
                Projects
              </button>
              <span className="crumb-sep">/</span>
              <span className="crumb current">{activeProject.name}</span>
            </nav>
          ) : view === 'chat' && activeThread !== null ? (
            <nav className="crumbs head-crumb" aria-label="Breadcrumb">
              {chatProject !== null && (
                <>
                  <button
                    className="model-pill"
                    title={`Open ${chatProject.name}`}
                    onClick={() => openProject(chatProject.project_id)}
                  >
                    {chatProject.name}
                  </button>
                  <span className="crumb-sep">/</span>
                </>
              )}
              {headRenaming ? (
                <input
                  className="head-rename"
                  style={headRenameWidth !== null ? { width: headRenameWidth } : undefined}
                  value={headDraft}
                  autoFocus
                  aria-label="Rename chat"
                  onChange={(event) => setHeadDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                      void submitHeadRename()
                    } else if (event.key === 'Escape') {
                      setHeadRenaming(false)
                    }
                  }}
                  onBlur={() => setHeadRenaming(false)}
                />
              ) : (
                <button
                  ref={chatPillRef}
                  className="model-pill chat-pill"
                  title="Rename chat"
                  onClick={() => startHeadRename(activeThread.title)}
                >
                  {activeThread.title}
                </button>
              )}
              <div className="model-select" ref={headMenuRef}>
                <button
                  className="icon-btn head-more"
                  aria-label={`More option for ${activeThread.title}`}
                  title={`More option for ${activeThread.title}`}
                  aria-haspopup="menu"
                  aria-expanded={headMenuOpen}
                  onClick={() => setHeadMenuOpen((prev) => !prev)}
                >
                  <ChevronIcon size={15} />
                </button>
                {headMenuOpen && (
                  <div className="recent-menu head-menu" role="menu">
                    <button
                      className="recent-menu-item"
                      role="menuitem"
                      onClick={() => {
                        setHeadMenuOpen(false)
                        startHeadRename(activeThread.title)
                      }}
                    >
                      <PencilIcon />
                      Rename
                    </button>
                    <button
                      className="recent-menu-item danger"
                      role="menuitem"
                      onClick={() => {
                        setHeadMenuOpen(false)
                        setHeadDeleteOpen(true)
                      }}
                    >
                      <TrashIcon />
                      Delete
                    </button>
                  </div>
                )}
              </div>
            </nav>
          ) : null}
          <span className="spacer" />
          {view === 'chat' && activeThread !== null && messages.length > 0 && (
            <button
              className="share-btn"
              title="Share chat"
              onClick={() => {
                // Shown to logged-out visitors too, and it points them at the
                // login rather than hiding a feature they cannot discover.
                if (!loggedIn) setLoginModalOpen(true)
                else setShareOpen(true)
              }}
            >
              <ShareIcon size={16} />
              Share
            </button>
          )}
          {view === 'chat' && chatProject !== null && (
            <button
              className={docPanelOpen ? 'icon-btn active' : 'icon-btn'}
              aria-label="Project documents"
              title="Project documents"
              aria-pressed={docPanelOpen}
              onClick={() => setDocPanelOpen((prev) => !prev)}
            >
              <DocumentsIcon size={17} />
            </button>
          )}
        </div>

        <div className="main-split">
          <div className="main-col">
        <div className="main-body" ref={scrollRef} onScroll={trackScroll}>
          {view === 'projects' && (
            <ProjectsPanel
              projects={projects}
              activeProjectId={activeProjectId}
              providers={providers}
              onSelect={openProject}
              onNewProject={() => setNewProjectOpen(true)}
              onDelete={(projectId) => void removeProject(projectId)}
            />
          )}


          {view === 'project' &&
            (activeProject === null ? (
              <div className="panel">
                <h2>Project</h2>
                <p className="empty-hint">Pick a project from the Projects tab.</p>
              </div>
            ) : (
              <ProjectPanel
                project={activeProject}
                threads={threads.filter(
                  (item) => item.project_id === activeProject.project_id,
                )}
                files={files}
                pendingUploads={pendingUploads.filter(
                  (item) => item.projectId === activeProject.project_id,
                )}
                busy={busy}
                composer={composerFor(activeProject)}
                onOpenThread={(threadId) => void openThread(threadId)}
                onUpload={(file) => void upload(file, activeProject)}
                onRename={(name) => renameProjectHandler(activeProject.project_id, name)}
                onManageMembers={
                  activeProject.role === 'owner'
                    ? () => setMembersOpen(true)
                    : undefined
                }
              />
            ))}

          {view === 'chats' && (
            <ChatsPanel
              threads={threads}
              onSelect={(threadId) => void openThread(threadId)}
              onDelete={(threadId) => void removeThread(threadId)}
            />
          )}

          {view === 'library' && (
            <LibraryPanel entries={libraryEntries} loading={libraryLoading} />
          )}

          {view === 'settings' && (
            <SettingsPanel
              userId={userId}
              providers={providers}
              onSaveUserId={changeUserId}
            />
          )}

          {view === 'chat' &&
            (activeThreadId === null ? (
              <div className="center-stage">
                <div className="greeting">
                  <h1>What shall we think through?</h1>
                </div>
                {composer}
                <div className="chips">
                  {STARTERS.map((starter) => {
                    const Icon = starter.icon
                    return (
                      <button
                        key={starter.label}
                        className="chip"
                        onClick={() => void send(starter.prompt)}
                      >
                        <Icon />
                        {starter.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            ) : (
              <>
                <div className="thread">
                  {turnGroups.map((group, groupIndex) => {
                    const isLastGroup = groupIndex === turnGroups.length - 1
                    const pickKey = group.user?.id ?? `orphan-${groupIndex}`
                    const total = group.answers.length
                    const currentIndex = Math.min(
                      versionPick[pickKey] ?? total - 1,
                      total - 1,
                    )
                    const answer = total > 0 ? group.answers[currentIndex] : undefined
                    return (
                      <div
                        key={pickKey}
                        id={group.user !== null ? `msg-${group.user.id}` : undefined}
                      >
                        {group.user !== null && (
                          <Message
                            message={group.user}
                            onEdit={!busy ? (text) => void send(text) : undefined}
                          />
                        )}
                        {answer !== undefined && !(regenerating && isLastGroup) && (
                          <Message
                            message={answer}
                            isError={answer.error === true}
                            onRegenerate={isLastGroup && !busy ? regenerate : undefined}
                            onCitationJump={jumpToCitation}
                            // Both need the stored position, so neither is
                            // offered on a bubble that was never saved (an
                            // error, or a turn stopped mid-stream).
                            onFork={
                              answer.position !== undefined && !busy
                                ? () => void forkFrom(answer.position!)
                                : undefined
                            }
                            onReport={
                              answer.position !== undefined
                                ? () => {
                                    setReportPosition(answer.position!)
                                    setReportOpen(true)
                                  }
                                : undefined
                            }
                            version={
                              total > 1
                                ? {
                                    current: currentIndex + 1,
                                    total,
                                    onPrev: () =>
                                      setVersionPick((prev) => ({
                                        ...prev,
                                        [pickKey]: Math.max(0, currentIndex - 1),
                                      })),
                                    onNext: () =>
                                      setVersionPick((prev) => ({
                                        ...prev,
                                        [pickKey]: Math.min(total - 1, currentIndex + 1),
                                      })),
                                  }
                                : undefined
                            }
                          />
                        )}
                      </div>
                    )
                  })}
                  {streaming !== null &&
                    (streaming.length === 0 ? (
                      <Typing />
                    ) : (
                      <Message
                        message={{ id: 'streaming', role: 'assistant', text: streaming }}
                        streaming
                      />
                    ))}
                </div>
                <div className="composer-dock">{composer}</div>
              </>
            ))}
        </div>

        {view === 'chat' && activeThreadId !== null && (
          <ThreadOutline
            entries={turnGroups.flatMap((group) =>
              group.user === null
                ? []
                : [
                    {
                      id: group.user.id,
                      preview:
                        group.user.text.length > 64
                          ? `${group.user.text.slice(0, 64)}…`
                          : group.user.text,
                    },
                  ],
            )}
            onJump={jumpToMessage}
          />
        )}
          </div>

          {docPanelOpen && view === 'chat' && chatProject !== null && (
            <DocPanel userId={userId} project={chatProject} focus={docFocus} />
          )}
        </div>
      </main>

      {loginModalOpen && !loggedIn && (
        <LoginModal
          title={
            pendingLoginText !== null ? 'Enjoy EslothAI? Log in to continue' : undefined
          }
          onLogin={onLogin}
          busy={loginBusy}
          error={loginError}
          onClose={() => {
            setLoginModalOpen(false)
            setPendingLoginText(null)
          }}
        />
      )}

      {shareOpen && activeThread !== null && loggedIn && (
        <ShareDialog
          key={activeThread.thread_id}
          userId={userId}
          threadId={activeThread.thread_id}
          onClose={() => setShareOpen(false)}
        />
      )}

      {membersOpen && activeProject !== null && (
        <ProjectMembersDialog
          userId={userId}
          project={activeProject}
          onClose={() => setMembersOpen(false)}
          onSaved={(transferred) => {
            setMembersOpen(false)
            // Reloaded either way: a handover leaves the caller an editor, so
            // every role-gated control on the screen has to be re-derived.
            void listProjects(userId).then(setProjects).catch(() => {})
            notify(
              transferred
                ? 'Project handed over. You are an editor now.'
                : 'Member changes saved.',
            )
          }}
        />
      )}

      {reportOpen && activeThreadId !== null && (
        <ReportDialog
          userId={userId}
          threadId={activeThreadId}
          messagePosition={reportPosition}
          onClose={() => setReportOpen(false)}
          onSent={() => {
            setReportOpen(false)
            notify('Thanks. Your report has been sent.')
          }}
        />
      )}

      {headDeleteOpen && activeThread !== null && (
        <ConfirmDialog
          title="Delete chat"
          text="Are you sure you want to delete this chat?"
          confirmLabel="Delete"
          onCancel={() => setHeadDeleteOpen(false)}
          onConfirm={() => {
            setHeadDeleteOpen(false)
            void removeThread(activeThread.thread_id)
          }}
        />
      )}

      {settingsDialogOpen && (
        <SettingsDialog
          userId={userId}
          threads={threads}
          projects={projects}
          memories={memories}
          settings={settings}
          busy={busy}
          onSaveMemory={saveMemory}
          onToggleMemory={(enabled) => void toggleMemory(enabled)}
          onManageMemory={() => setManageMemoryOpen(true)}
          onClose={() => setSettingsDialogOpen(false)}
        />
      )}

      {manageMemoryOpen && (
        <ManageMemoryDialog
          memories={memories}
          busy={busy}
          onClose={() => setManageMemoryOpen(false)}
          onAdd={saveMemory}
          onEdit={editMemory}
          onDelete={(memoryId) => void removeMemory(memoryId)}
        />
      )}

      {newProjectOpen && (
        <NewProjectDialog
          providers={providers}
          busy={busy}
          onClose={() => setNewProjectOpen(false)}
          onCreate={(provider, queued, icon, color, name) =>
            void addProject(provider, queued, icon, color, name)
          }
        />
      )}

      {toast !== null && (
        <div className={toast.error ? 'toast error' : 'toast'} role="status">
          {toast.text}
        </div>
      )}
    </div>
  )
}
