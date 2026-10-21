import { useEffect, useState } from 'react'
import { getUsage, type LlmUsage } from '../lib/api'
import type { Memory, MemoryType, Project, Thread, UserSettings } from '../lib/types'
import { loadLanguage, setLoggedIn, type Language } from '../lib/storage'
import { loadThemePref, setThemePref, type ThemePref } from '../lib/theme'
import shareArt from '../assets/share-esloth.png'
import { ABOUT_FIELDS, AboutField } from './Personalization'
import {
  BriefcaseIcon,
  ChartIcon,
  CloseIcon,
  HeartIcon,
  SettingsIcon,
  UserIcon,
} from './icons'

type Tab = 'general' | 'account' | 'usage' | 'capabilities' | 'share'

const SHARE_MESSAGE = "I've been using EslothAI, an intelligent AI. Try it!\nhttps://eslothai.com"

// Placeholder until Google login lands and provides the real address.

/**
 * UI strings keyed by the user's chosen language. Only English is filled in
 * for now; missing languages fall back to English, so adding `vi` here later
 * localizes the whole dialog with no component changes.
 */
const STRINGS: Partial<Record<Language, Record<string, string>>> & {
  en: Record<string, string>
} = {
  en: {
    settings: 'Settings',
    general: 'General',
    account: 'Account',
    usage: 'Usage',
    capabilities: 'Capabilities',
    share: 'Share Esloth',
    profile: 'Profile',
    avatar: 'Avatar',
    appearance: 'Appearance',
    themeLight: 'Light',
    themeDark: 'Dark',
    themeAuto: 'Auto',
    userId: 'User ID',
    logOut: 'Log out',
    yourUsage: 'Your usage',
    chats: 'Chats',
    projects: 'Projects',
    tokens: 'Tokens',
    totalTokens: 'Total tokens',
    inputTokens: 'Input tokens',
    outputTokens: 'Output tokens',
    memory: 'Memory',
    memoryNotice:
      'Memory is stored and editable, but it is not injected into prompts yet, so it does not change answers.',
    referenceMemory: 'Reference saved memory',
    referenceMemorySub: 'Turn this off to stop new memories being used once injection is wired up.',
    manageMemory: 'Manage memory',
    manage: 'Manage',
    savedEntry: 'saved entry',
    savedEntries: 'saved entries',
    shareTitle: 'Spread the word',
    shareSub: 'Share a link to EslothAI with a friend...',
    copyLink: 'Copy link',
    copied: 'Copied!',
  },
}

function initials(value: string): string {
  const trimmed = value.trim()
  if (trimmed.length === 0) return '?'
  const words = trimmed.split(/[\s-]+/).filter(Boolean)
  if (words.length >= 2) return `${words[0]![0]}${words[1]![0]}`
  return trimmed.slice(0, 2)
}

interface SettingsDialogProps {
  userId: string
  threads: Thread[]
  projects: Project[]
  memories: Memory[]
  settings: UserSettings | null
  busy: boolean
  onSaveMemory: (type: MemoryType, key: string, value: string) => Promise<boolean>
  onToggleMemory: (enabled: boolean) => void
  onManageMemory: () => void
  onClose: () => void
}

export function SettingsDialog({
  userId,
  threads,
  projects,
  memories,
  settings,
  busy,
  onSaveMemory,
  onToggleMemory,
  onManageMemory,
  onClose,
}: SettingsDialogProps) {
  const [tab, setTab] = useState<Tab>('general')
  const [theme, setTheme] = useState<ThemePref>(() => loadThemePref())
  const [copied, setCopied] = useState(false)
  const [usage, setUsage] = useState<LlmUsage | null>(null)
  const t = { ...STRINGS.en, ...STRINGS[loadLanguage()] }
  const stored = new Map(memories.map((item) => [item.key, item.value]))
  const displayName =
    settings?.display_name ?? stored.get('nickname') ?? 'Lorem Ipsum'
  const email = settings?.email ?? 'Not signed in'

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])

  useEffect(() => {
    if (tab !== 'usage') return
    let cancelled = false
    getUsage(userId)
      .then((data) => {
        if (!cancelled) setUsage(data)
      })
      .catch(() => {
        // The tab still shows chats/projects; token rows just stay at "–".
      })
    return () => {
      cancelled = true
    }
  }, [tab, userId])

  async function copyShareLink() {
    await navigator.clipboard.writeText(SHARE_MESSAGE)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const TABS: { id: Tab; label: string; icon: React.ComponentType<{ size?: number }> }[] = [
    { id: 'general', label: t.general!, icon: SettingsIcon },
    { id: 'account', label: t.account!, icon: UserIcon },
    { id: 'usage', label: t.usage!, icon: ChartIcon },
    { id: 'capabilities', label: t.capabilities!, icon: BriefcaseIcon },
    { id: 'share', label: t.share!, icon: HeartIcon },
  ]

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="settings-modal" role="dialog" aria-modal="true" aria-label={t.settings}>
        <div className="settings-rail">
          <div className="settings-rail-user">
            <p className="settings-rail-name" title={displayName}>
              {displayName}
            </p>
            <p className="settings-rail-email" title={email}>
              {email}
            </p>
          </div>
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={tab === id ? 'nav-item active' : 'nav-item'}
              onClick={() => setTab(id)}
              aria-current={tab === id ? 'page' : undefined}
            >
              <Icon size={16} />
              <span className="nav-label">{label}</span>
            </button>
          ))}
        </div>

        <div className="settings-body">
          <button className="icon-btn settings-close" onClick={onClose} aria-label="Close settings">
            <CloseIcon size={17} />
          </button>

          {tab === 'general' && (
            <>
              <h2 className="settings-heading-lg">{t.profile}</h2>
              <div className="settings-row">
                <span className="settings-row-label">{t.avatar}</span>
                <span className="avatar settings-avatar">{initials(displayName)}</span>
              </div>
              {ABOUT_FIELDS.map((field) => (
                <AboutField
                  key={field.key}
                  label={field.label}
                  placeholder={field.placeholder}
                  stored={stored.get(field.key) ?? ''}
                  busy={busy}
                  onSave={(value) => onSaveMemory(field.type, field.key, value)}
                />
              ))}

              <h2 className="settings-heading-lg settings-appearance-heading">{t.appearance}</h2>
              <div className="appearance-row" role="radiogroup" aria-label={t.appearance}>
                {(
                  [
                    { value: 'light', label: t.themeLight! },
                    { value: 'dark', label: t.themeDark! },
                    { value: 'auto', label: t.themeAuto! },
                  ] as const
                ).map(({ value, label }) => (
                  <button
                    key={value}
                    className={theme === value ? 'appearance-card selected' : 'appearance-card'}
                    role="radio"
                    aria-checked={theme === value}
                    onClick={() => {
                      setTheme(value)
                      setThemePref(value)
                    }}
                  >
                    <span className={`appearance-preview ${value}`} aria-hidden="true">
                      <span className="ap-chip" />
                      <span className="ap-line w-80" />
                      <span className="ap-line w-60" />
                      <span className="ap-line w-70" />
                      <span className="ap-pill">
                        <span className="ap-dot" />
                      </span>
                    </span>
                    <span className="appearance-label">{label}</span>
                  </button>
                ))}
              </div>
            </>
          )}

          {tab === 'account' && (
            <>
              <h2 className="settings-heading-lg">{t.account}</h2>
              <div className="settings-row">
                <span className="settings-row-label">{t.userId}</span>
                <code className="settings-userid" title={userId}>
                  {userId}
                </code>
              </div>
              <div className="settings-row">
                <span className="settings-row-label" />
                <button
                  className="danger-btn"
                  onClick={() => {
                    setLoggedIn(false)
                    window.location.href = '/'
                  }}
                >
                  {t.logOut}
                </button>
              </div>
            </>
          )}

          {tab === 'usage' && (
            <>
              <h2 className="settings-heading-lg">{t.yourUsage}</h2>
              <div className="settings-row">
                <span className="settings-row-label">{t.chats}</span>
                <span className="settings-stat">{threads.length}</span>
              </div>
              <div className="settings-row">
                <span className="settings-row-label">{t.projects}</span>
                <span className="settings-stat">{projects.length}</span>
              </div>

              <h2 className="settings-heading-lg settings-appearance-heading">{t.tokens}</h2>
              <div className="settings-row">
                <span className="settings-row-label">{t.totalTokens}</span>
                <span className="settings-stat">
                  {usage === null ? '–' : usage.total_tokens.toLocaleString()}
                </span>
              </div>
              <div className="settings-row">
                <span className="settings-row-label">{t.inputTokens}</span>
                <span className="settings-stat">
                  {usage === null ? '–' : usage.prompt_tokens.toLocaleString()}
                </span>
              </div>
              <div className="settings-row">
                <span className="settings-row-label">{t.outputTokens}</span>
                <span className="settings-stat">
                  {usage === null ? '–' : usage.completion_tokens.toLocaleString()}
                </span>
              </div>
            </>
          )}

          {tab === 'capabilities' && (
            <>
              <h2 className="settings-heading-lg">{t.memory}</h2>
              <div className="notice">{t.memoryNotice}</div>
              <div className="toggle-row">
                <span className="toggle-text">
                  <span className="toggle-title">{t.referenceMemory}</span>
                  <span className="toggle-sub">{t.referenceMemorySub}</span>
                </span>
                <button
                  className={settings?.memory_enabled === false ? 'switch' : 'switch on'}
                  role="switch"
                  aria-checked={settings?.memory_enabled !== false}
                  aria-label={t.referenceMemory}
                  disabled={settings === null || busy}
                  onClick={() => onToggleMemory(settings?.memory_enabled === false)}
                >
                  <span className="switch-knob" />
                </button>
              </div>
              <div className="toggle-row">
                <span className="toggle-text">
                  <span className="toggle-title">{t.manageMemory}</span>
                  <span className="toggle-sub">
                    {memories.length} {memories.length === 1 ? t.savedEntry : t.savedEntries}
                  </span>
                </span>
                <button className="secondary-btn" onClick={onManageMemory}>
                  {t.manage}
                </button>
              </div>
            </>
          )}

          {tab === 'share' && (
            <div className="share-pane">
              <img className="share-art" src={shareArt} alt="" />
              <h2 className="share-title">{t.shareTitle}</h2>
              <p className="share-sub">{t.shareSub}</p>
              <button className="share-copy-btn" onClick={() => void copyShareLink()}>
                {copied ? t.copied : t.copyLink}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
