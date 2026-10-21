import { useEffect, useRef, useState } from 'react'
import { signOut } from 'firebase/auth'
import { firebaseAuth } from '../lib/firebase'
import type { Project, SharedWithMeItem, Thread } from '../lib/types'
import {
  loadLanguage,
  resetIdentity,
  saveLanguage,
  setLoggedIn,
  type Language,
} from '../lib/storage'
import {
  ChatIcon,
  CheckIcon,
  ChevronIcon,
  ChevronUpDownIcon,
  DownloadIcon,
  FilterIcon,
  GlobeIcon,
  HelpIcon,
  InfoIcon,
  LibraryIcon,
  LogoutIcon,
  MoreIcon,
  PanelIcon,
  PencilIcon,
  PlansIcon,
  PlusIcon,
  ProjectsIcon,
  SearchIcon,
  SettingsIcon,
  ShareIcon,
  TrashIcon,
  UserIcon,
} from './icons'
import { SearchDialog } from './SearchDialog'
import { ConfirmDialog } from './ConfirmDialog'
import { ProjectIcon } from './projectIcons'

export type View = 'chat' | 'chats' | 'projects' | 'project' | 'library' | 'settings'

interface SidebarProps {
  collapsed: boolean
  view: View
  threads: Thread[]
  /** Chats other people invited this account to. Empty hides the section. */
  sharedWithMe: SharedWithMeItem[]
  activeThreadId: string | null
  projects: Project[]
  activeProjectId: string | null
  displayName: string | null
  projectName: string | null
  onToggle: () => void
  onNewChat: () => void
  onSelectView: (view: View) => void
  onSelectThread: (threadId: string) => void
  onDeleteThread: (threadId: string) => void
  onRenameThread: (threadId: string, title: string) => Promise<boolean>
  onSelectProject: (projectId: string) => void
  onNewProject: () => void
  onOpenSettings: () => void
  loggedIn: boolean
  onLogin: () => void
  /** Server-decided; only shows the menu entry, never grants anything. */
  isAdmin: boolean
}

/** Shown instead of the raw user id, which never appears in the UI anymore. */
const DEFAULT_NAME = 'Lorem Ipsum'

const NAV: { view: View; label: string; icon: React.ComponentType<{ size?: number }> }[] = [
  { view: 'chats', label: 'Chats', icon: ChatIcon },
  { view: 'projects', label: 'Projects', icon: ProjectsIcon },
  { view: 'library', label: 'Library', icon: LibraryIcon },
]

function initials(value: string): string {
  const trimmed = value.trim()
  if (trimmed.length === 0) return '?'
  const words = trimmed.split(/[\s-]+/).filter(Boolean)
  if (words.length >= 2) return `${words[0]![0]}${words[1]![0]}`
  return trimmed.slice(0, 2)
}

export function Sidebar({
  collapsed,
  view,
  threads,
  sharedWithMe,
  activeThreadId,
  projects,
  activeProjectId,
  displayName,
  projectName,
  onToggle,
  onNewChat,
  onSelectView,
  onSelectThread,
  onDeleteThread,
  onRenameThread,
  onSelectProject,
  onNewProject,
  onOpenSettings,
  loggedIn,
  onLogin,
  isAdmin,
}: SidebarProps) {
  const [menuFor, setMenuFor] = useState<string | null>(null)
  // True when the row sits too close to the bottom of the scroll viewport for
  // the menu to fit below it, so it opens upward instead of being clipped.
  const [menuUp, setMenuUp] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Thread | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [projectsOpen, setProjectsOpen] = useState(true)
  const [recentsOpen, setRecentsOpen] = useState(true)
  const [sharedOpen, setSharedOpen] = useState(true)
  const [searchOpen, setSearchOpen] = useState(false)
  const [filterOpen, setFilterOpen] = useState(false)
  // Viewport coordinates for the filter menu: it renders position:fixed so its
  // side flyouts escape .sidebar-scroll's overflow-x clipping.
  const [filterPos, setFilterPos] = useState<{ top: number; left: number } | null>(null)
  const [hoverFilter, setHoverFilter] = useState<'type' | 'activity' | null>(null)
  const [typeFilter, setTypeFilter] = useState<'all' | 'chat' | 'project'>('all')
  // Days back to keep a thread, by updated_at; null = no limit.
  const [activityFilter, setActivityFilter] = useState<number | null>(null)
  const filterRef = useRef<HTMLSpanElement>(null)
  const [profileOpen, setProfileOpen] = useState(false)
  const [hoverMenu, setHoverMenu] = useState<'lang' | 'more' | null>(null)
  const [language, setLanguage] = useState<Language>(() => loadLanguage())
  const menuRef = useRef<HTMLDivElement>(null)
  const profileRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!profileOpen) return
    const close = (event: MouseEvent) => {
      if (!profileRef.current?.contains(event.target as Node)) {
        setProfileOpen(false)
        setHoverMenu(null)
      }
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setProfileOpen(false)
        setHoverMenu(null)
      }
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [profileOpen])

  useEffect(() => {
    if (!filterOpen) return
    const close = (event: MouseEvent) => {
      if (!filterRef.current?.contains(event.target as Node)) {
        setFilterOpen(false)
        setHoverFilter(null)
      }
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setFilterOpen(false)
        setHoverFilter(null)
      }
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [filterOpen])

  function closeProfileMenu() {
    setProfileOpen(false)
    setHoverMenu(null)
  }

  function pickLanguage(next: Language) {
    setLanguage(next)
    saveLanguage(next)
    closeProfileMenu()
  }

  function logout() {
    setLoggedIn(false)
    // A fresh anonymous identity: the uid must not linger as the browse-mode
    // identity on this machine, and the next login must not claim it.
    resetIdentity()
    // The Firebase SDK keeps its own session in IndexedDB; without this
    // sign-out, the next "Continue with Google" on a shared machine can
    // silently reopen the previous person's account. The reload waits for it.
    // Full reload rather than lifted state: Root reads login state once on
    // mount, and a reload also drops every per-session cache in App.
    void signOut(firebaseAuth).finally(() => {
      window.location.href = '/'
    })
  }

  useEffect(() => {
    if (menuFor === null) return
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuFor(null)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuFor(null)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [menuFor])

  async function submitRename(threadId: string) {
    const title = renameDraft.trim()
    if (title.length === 0) {
      setRenamingId(null)
      return
    }
    if (await onRenameThread(threadId, title)) setRenamingId(null)
  }

  if (collapsed) return null

  const projectById = new Map(projects.map((project) => [project.project_id, project]))
  const cutoff = activityFilter === null ? null : Date.now() - activityFilter * 86_400_000
  const visibleThreads = threads.filter((thread) => {
    if (typeFilter === 'chat' && thread.project_id !== null) return false
    if (typeFilter === 'project' && thread.project_id === null) return false
    if (cutoff !== null && Date.parse(thread.updated_at) < cutoff) return false
    return true
  })
  const filtersActive = typeFilter !== 'all' || activityFilter !== null

  const TYPE_OPTIONS: { value: 'all' | 'chat' | 'project'; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'chat', label: 'Chat' },
    { value: 'project', label: 'Project' },
  ]
  const ACTIVITY_OPTIONS: { value: number | null; label: string }[] = [
    { value: 1, label: '1d' },
    { value: 3, label: '3d' },
    { value: 7, label: '7d' },
    { value: 30, label: '30d' },
    { value: null, label: 'All' },
  ]

  return (
    <nav className="sidebar" aria-label="Sidebar">
      <div className="sidebar-head">
        <span className="brand">
          <img className="brand-logo" src="/logo.png" alt="" />
          <span className="wordmark">EslothAI</span>
        </span>
        <div className="head-actions">
          <button
            className="icon-btn"
            onClick={() => setSearchOpen(true)}
            aria-label="Search chats and projects"
          >
            <SearchIcon size={17} />
          </button>
          <button className="icon-btn" onClick={onToggle} aria-label="Collapse sidebar">
            <PanelIcon size={17} />
          </button>
        </div>
      </div>

      <div className="sidebar-scroll">
        <button className="nav-item new-chat" onClick={onNewChat}>
          <PlusIcon size={17} />
          <span className="nav-label">New chat</span>
        </button>

        {NAV.map((item) => {
          const Icon = item.icon
          return (
            <button
              key={item.view}
              className={view === item.view ? 'nav-item active' : 'nav-item'}
              onClick={() => onSelectView(item.view)}
              aria-current={view === item.view ? 'page' : undefined}
            >
              <Icon />
              <span className="nav-label">{item.label}</span>
            </button>
          )
        })}

        <div className="section-label">
          <button
            className="section-toggle"
            onClick={() => setProjectsOpen((prev) => !prev)}
            aria-expanded={projectsOpen}
          >
            <span className={projectsOpen ? 'section-chevron' : 'section-chevron collapsed'}>
              <ChevronIcon size={14} />
            </span>
            <span>Projects</span>
          </button>
          <button className="icon-btn" onClick={onNewProject} aria-label="New project">
            <PlusIcon size={15} />
          </button>
        </div>

        {projectsOpen &&
          (projects.length === 0 ? (
            <p className="recent-empty">No projects yet</p>
          ) : (
            projects.map((project) => (
              <button
                key={project.project_id}
                className={
                  project.project_id === activeProjectId && view === 'project'
                    ? 'nav-item content-row active'
                    : 'nav-item content-row'
                }
                onClick={() => onSelectProject(project.project_id)}
                title={project.name}
                aria-current={
                  project.project_id === activeProjectId && view === 'project' ? 'page' : undefined
                }
              >
                <ProjectIcon icon={project.icon} color={project.color} size={15} />
                <span className="nav-label">{project.name}</span>
              </button>
            ))
          ))}

        <div className="section-label">
          <button
            className="section-toggle"
            onClick={() => setRecentsOpen((prev) => !prev)}
            aria-expanded={recentsOpen}
          >
            <span className={recentsOpen ? 'section-chevron' : 'section-chevron collapsed'}>
              <ChevronIcon size={14} />
            </span>
            <span>Recents</span>
          </button>
          <span className="recents-filter" ref={filterRef}>
            <button
              className={filterOpen || filtersActive ? 'icon-btn active' : 'icon-btn'}
              aria-label="Filter conversations"
              aria-haspopup="menu"
              aria-expanded={filterOpen}
              onClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect()
                setFilterPos({ top: rect.bottom + 4, left: rect.left })
                setFilterOpen((prev) => !prev)
                setHoverFilter(null)
              }}
            >
              <FilterIcon />
            </button>
            {filterOpen && filterPos !== null && (
              <div
                className="recent-menu filter-menu"
                role="menu"
                style={{ top: filterPos.top, left: filterPos.left }}
              >
                <div
                  className="filter-flyout"
                  onMouseEnter={() => setHoverFilter('type')}
                  onMouseLeave={() =>
                    setHoverFilter((prev) => (prev === 'type' ? null : prev))
                  }
                >
                  <button
                    className={
                      hoverFilter === 'type' ? 'recent-menu-item open' : 'recent-menu-item'
                    }
                    role="menuitem"
                    aria-haspopup="menu"
                    aria-expanded={hoverFilter === 'type'}
                    onClick={() =>
                      setHoverFilter((prev) => (prev === 'type' ? null : 'type'))
                    }
                  >
                    <span className="menu-label">Type</span>
                    <span className="menu-value">
                      {TYPE_OPTIONS.find(({ value }) => value === typeFilter)!.label}
                    </span>
                    <span className="menu-side-chevron">
                      <ChevronIcon size={14} />
                    </span>
                  </button>
                  {hoverFilter === 'type' && (
                    <div className="recent-menu filter-submenu" role="menu">
                      {TYPE_OPTIONS.map(({ value, label }) => (
                        <button
                          key={value}
                          className="recent-menu-item"
                          role="menuitemradio"
                          aria-checked={typeFilter === value}
                          onClick={() => setTypeFilter(value)}
                        >
                          <span className="menu-label">{label}</span>
                          {typeFilter === value && <CheckIcon size={14} />}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <div
                  className="filter-flyout"
                  onMouseEnter={() => setHoverFilter('activity')}
                  onMouseLeave={() =>
                    setHoverFilter((prev) => (prev === 'activity' ? null : prev))
                  }
                >
                  <button
                    className={
                      hoverFilter === 'activity' ? 'recent-menu-item open' : 'recent-menu-item'
                    }
                    role="menuitem"
                    aria-haspopup="menu"
                    aria-expanded={hoverFilter === 'activity'}
                    onClick={() =>
                      setHoverFilter((prev) => (prev === 'activity' ? null : 'activity'))
                    }
                  >
                    <span className="menu-label">Last activity</span>
                    <span className="menu-value">
                      {ACTIVITY_OPTIONS.find(({ value }) => value === activityFilter)!.label}
                    </span>
                    <span className="menu-side-chevron">
                      <ChevronIcon size={14} />
                    </span>
                  </button>
                  {hoverFilter === 'activity' && (
                    <div className="recent-menu filter-submenu" role="menu">
                      {ACTIVITY_OPTIONS.map(({ value, label }) => (
                        <button
                          key={label}
                          className="recent-menu-item"
                          role="menuitemradio"
                          aria-checked={activityFilter === value}
                          onClick={() => setActivityFilter(value)}
                        >
                          <span className="menu-label">{label}</span>
                          {activityFilter === value && <CheckIcon size={14} />}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </span>
        </div>

        {recentsOpen &&
          (threads.length === 0 ? (
            <p className="recent-empty">No conversations yet</p>
          ) : visibleThreads.length === 0 ? (
            <p className="recent-empty">No conversations match the filter</p>
          ) : (
            visibleThreads.map((thread) => {
              const isActive = thread.thread_id === activeThreadId && view === 'chat'
              const isOpen = menuFor === thread.thread_id
              const threadProject =
                thread.project_id === null ? null : projectById.get(thread.project_id) ?? null
              return (
                <div
                  key={thread.thread_id}
                  className={
                    isActive || isOpen ? 'recent-row active' : 'recent-row'
                  }
                >
                  {renamingId === thread.thread_id ? (
                    <input
                      className="recent-rename"
                      value={renameDraft}
                      autoFocus
                      aria-label="Rename conversation"
                      onChange={(event) => setRenameDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                          void submitRename(thread.thread_id)
                        } else if (event.key === 'Escape') {
                          setRenamingId(null)
                        }
                      }}
                      onBlur={() => setRenamingId(null)}
                    />
                  ) : (
                    <button
                      className="recent-item"
                      onClick={() => onSelectThread(thread.thread_id)}
                      title={thread.title}
                    >
                      <span className="recent-icon">
                        {threadProject === null ? (
                          <ChatIcon size={15} />
                        ) : (
                          <ProjectIcon
                            icon={threadProject.icon}
                            color={threadProject.color}
                            size={15}
                          />
                        )}
                      </span>
                      <span className="recent-title">{thread.title}</span>
                    </button>
                  )}
                  <button
                    className={isOpen ? 'recent-more open' : 'recent-more'}
                    aria-label={`Options for ${thread.title}`}
                    aria-expanded={isOpen}
                    onClick={(event) => {
                      if (isOpen) {
                        setMenuFor(null)
                        return
                      }
                      const rect = event.currentTarget.getBoundingClientRect()
                      const scroller = event.currentTarget.closest('.sidebar-scroll')
                      const limit =
                        scroller?.getBoundingClientRect().bottom ?? window.innerHeight
                      setMenuUp(limit - rect.bottom < 100)
                      setMenuFor(thread.thread_id)
                    }}
                  >
                    <MoreIcon />
                  </button>
                  {isOpen && (
                    <div
                      className={menuUp ? 'recent-menu up' : 'recent-menu'}
                      ref={menuRef}
                      role="menu"
                    >
                      <button
                        className="recent-menu-item"
                        role="menuitem"
                        onClick={() => {
                          setMenuFor(null)
                          setRenameDraft(thread.title)
                          setRenamingId(thread.thread_id)
                        }}
                      >
                        <PencilIcon />
                        Rename
                      </button>
                      <button
                        className="recent-menu-item danger"
                        role="menuitem"
                        onClick={() => {
                          setMenuFor(null)
                          setDeleteTarget(thread)
                        }}
                      >
                        <TrashIcon />
                        Delete
                      </button>
                    </div>
                  )}
                </div>
              )
            })
          ))}

        {sharedWithMe.length > 0 && (
          <>
            <div className="section-label">
              <button
                className="section-toggle"
                onClick={() => setSharedOpen((prev) => !prev)}
                aria-expanded={sharedOpen}
              >
                <span className={sharedOpen ? 'section-chevron' : 'section-chevron collapsed'}>
                  <ChevronIcon size={14} />
                </span>
                <span>Shared with you</span>
              </button>
            </div>
            {sharedOpen &&
              sharedWithMe.map((item) => (
                <a
                  key={item.share_id}
                  className="recent-row shared-row"
                  href={`/share/${item.share_id}`}
                  title={item.title}
                >
                  <span className="recent-item">
                    <span className="recent-icon">
                      <ShareIcon size={15} />
                    </span>
                    <span className="recent-title">{item.title}</span>
                  </span>
                </a>
              ))}
          </>
        )}
      </div>

      <div className="sidebar-foot" ref={profileRef}>
        {!loggedIn ? (
          <button className="login-cta" onClick={onLogin}>
            Log in
          </button>
        ) : (
        <button
          className="profile"
          aria-haspopup="menu"
          aria-expanded={profileOpen}
          onClick={() => {
            setProfileOpen((prev) => !prev)
            setHoverMenu(null)
          }}
        >
          <span className="avatar">{initials(displayName ?? DEFAULT_NAME)}</span>
          <span className="profile-text">
            <span className="profile-name">{displayName ?? DEFAULT_NAME}</span>
            <span className="profile-sub">{projectName ?? 'No project selected'}</span>
          </span>
          <DownloadIcon />
          <ChevronUpDownIcon />
        </button>
        )}

        {loggedIn && profileOpen && (
          <div className="profile-menu" role="menu">
            <p className="profile-menu-head" title={displayName ?? DEFAULT_NAME}>
              {displayName ?? DEFAULT_NAME}
            </p>

            <button
              className="recent-menu-item"
              role="menuitem"
              onClick={() => {
                closeProfileMenu()
                onOpenSettings()
              }}
            >
              <SettingsIcon />
              <span className="menu-label">Settings</span>
            </button>
            {isAdmin && (
              <button
                className="recent-menu-item"
                role="menuitem"
                onClick={() => {
                  closeProfileMenu()
                  window.location.href = '/admin'
                }}
              >
                <UserIcon />
                <span className="menu-label">Admin</span>
              </button>
            )}
            <div
              className="profile-flyout"
              onMouseEnter={() => setHoverMenu('lang')}
              onMouseLeave={() => setHoverMenu((prev) => (prev === 'lang' ? null : prev))}
            >
              <button
                className={hoverMenu === 'lang' ? 'recent-menu-item open' : 'recent-menu-item'}
                role="menuitem"
                aria-haspopup="menu"
                aria-expanded={hoverMenu === 'lang'}
                onClick={() => setHoverMenu((prev) => (prev === 'lang' ? null : 'lang'))}
              >
                <GlobeIcon />
                <span className="menu-label">Language</span>
                <span className="menu-side-chevron">
                  <ChevronIcon size={14} />
                </span>
              </button>
              {hoverMenu === 'lang' && (
                <div className="profile-submenu" role="menu">
                  <button
                    className="recent-menu-item"
                    role="menuitemradio"
                    aria-checked={language === 'en'}
                    onClick={() => pickLanguage('en')}
                  >
                    <span className="menu-flag">🇺🇸</span>
                    <span className="menu-label">English</span>
                    {language === 'en' && <CheckIcon size={15} />}
                  </button>
                  <button
                    className="recent-menu-item"
                    role="menuitemradio"
                    aria-checked={language === 'vi'}
                    onClick={() => pickLanguage('vi')}
                  >
                    <span className="menu-flag">🇻🇳</span>
                    <span className="menu-label">Tiếng Việt</span>
                    {language === 'vi' && <CheckIcon size={15} />}
                  </button>
                </div>
              )}
            </div>
            <button className="recent-menu-item" role="menuitem" onClick={closeProfileMenu}>
              <HelpIcon />
              <span className="menu-label">Get help</span>
            </button>

            <div className="menu-sep" role="separator" />

            <button className="recent-menu-item" role="menuitem" onClick={closeProfileMenu}>
              <PlansIcon />
              <span className="menu-label">View all plans</span>
            </button>
            <button className="recent-menu-item" role="menuitem" onClick={closeProfileMenu}>
              <DownloadIcon />
              <span className="menu-label">Get apps and extensions</span>
            </button>
            <div
              className="profile-flyout"
              onMouseEnter={() => setHoverMenu('more')}
              onMouseLeave={() => setHoverMenu((prev) => (prev === 'more' ? null : prev))}
            >
              <button
                className={hoverMenu === 'more' ? 'recent-menu-item open' : 'recent-menu-item'}
                role="menuitem"
                aria-haspopup="menu"
                aria-expanded={hoverMenu === 'more'}
                onClick={() => setHoverMenu((prev) => (prev === 'more' ? null : 'more'))}
              >
                <InfoIcon />
                <span className="menu-label">Learn more</span>
                <span className="menu-side-chevron">
                  <ChevronIcon size={14} />
                </span>
              </button>
              {hoverMenu === 'more' && (
                <div className="profile-submenu" role="menu">
                  <button className="recent-menu-item" role="menuitem" onClick={closeProfileMenu}>
                    <span className="menu-label">About EslothAI</span>
                  </button>
                  <div className="menu-sep" role="separator" />
                  <button className="recent-menu-item" role="menuitem" onClick={closeProfileMenu}>
                    <span className="menu-label">Usage policy</span>
                  </button>
                  <button className="recent-menu-item" role="menuitem" onClick={closeProfileMenu}>
                    <span className="menu-label">Privacy policy</span>
                  </button>
                  <div className="menu-sep" role="separator" />
                  <button className="recent-menu-item" role="menuitem" onClick={closeProfileMenu}>
                    <span className="menu-label">Keyboard shortcuts</span>
                  </button>
                </div>
              )}
            </div>

            <div className="menu-sep" role="separator" />

            <button className="recent-menu-item" role="menuitem" onClick={logout}>
              <LogoutIcon />
              <span className="menu-label">Log out</span>
            </button>
          </div>
        )}
      </div>

      {searchOpen && (
        <SearchDialog
          threads={threads}
          projects={projects}
          onSelectThread={onSelectThread}
          onSelectProject={onSelectProject}
          onClose={() => setSearchOpen(false)}
        />
      )}

      {deleteTarget !== null && (
        <ConfirmDialog
          title="Delete chat"
          text="Are you sure you want to delete this chat?"
          confirmLabel="Delete"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            onDeleteThread(deleteTarget.thread_id)
            setDeleteTarget(null)
          }}
        />
      )}
    </nav>
  )
}
