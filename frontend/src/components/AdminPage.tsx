import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  adminGrantAdmin,
  adminListAdmins,
  adminListAudit,
  adminListReports,
  adminListUsers,
  adminMe,
  adminResolveReport,
  adminRevokeAdmin,
  adminSetUserDisabled,
  adminStats,
  type AdminAuditEntry,
  type AdminList,
  type AdminMe,
  type AdminReport,
  type AdminStats,
  type AdminUser,
} from '../lib/api'
import { Burst } from './icons'

type Gate = 'loading' | 'ok' | 'forbidden' | 'error'
type Tab = 'overview' | 'users' | 'reports' | 'admins' | 'audit'

interface AdminPageProps {
  userId: string
}

const TABS: { id: Tab; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'users', label: 'Users' },
  { id: 'reports', label: 'Reports' },
  { id: 'admins', label: 'Admins' },
  { id: 'audit', label: 'Audit log' },
]

function when(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

function messageFrom(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong'
}

/**
 * The admin screen, mounted on `/admin` without the app shell: it manages the
 * whole system, not one account's chats, so the sidebar's threads and projects
 * would only mislead. Access is decided by the server on every call — this
 * component just renders the 403 politely.
 */
export function AdminPage({ userId }: AdminPageProps) {
  const [gate, setGate] = useState<Gate>('loading')
  const [me, setMe] = useState<AdminMe | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [error, setError] = useState<string | null>(null)

  const [stats, setStats] = useState<AdminStats | null>(null)
  const [users, setUsers] = useState<AdminUser[]>([])
  const [reports, setReports] = useState<AdminReport[]>([])
  const [includeResolved, setIncludeResolved] = useState(false)
  const [openReportId, setOpenReportId] = useState<string | null>(null)
  const [admins, setAdmins] = useState<AdminList | null>(null)
  const [grantDraft, setGrantDraft] = useState('')
  const [audit, setAudit] = useState<AdminAuditEntry[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    void adminMe(userId)
      .then((loaded) => {
        if (cancelled) return
        setMe(loaded)
        setGate('ok')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setGate(err instanceof ApiError && err.status === 403 ? 'forbidden' : 'error')
      })
    return () => {
      cancelled = true
    }
  }, [userId])

  const refresh = useCallback(
    async (which: Tab) => {
      setError(null)
      try {
        if (which === 'overview') setStats(await adminStats(userId))
        else if (which === 'users') setUsers(await adminListUsers(userId))
        else if (which === 'reports')
          setReports(await adminListReports(userId, includeResolved))
        else if (which === 'admins') setAdmins(await adminListAdmins(userId))
        else setAudit(await adminListAudit(userId))
      } catch (err) {
        setError(messageFrom(err))
      }
    },
    [userId, includeResolved],
  )

  useEffect(() => {
    if (gate !== 'ok') return
    void refresh(tab)
  }, [gate, tab, refresh])

  async function act(action: () => Promise<unknown>, reload: Tab) {
    setBusy(true)
    setError(null)
    try {
      await action()
      await refresh(reload)
    } catch (err) {
      setError(messageFrom(err))
    } finally {
      setBusy(false)
    }
  }

  if (gate === 'loading') {
    return (
      <div className="share-page admin-page">
        <div className="share-empty">Loading…</div>
      </div>
    )
  }

  if (gate !== 'ok') {
    return (
      <div className="share-page admin-page">
        <header className="share-head">
          <a className="share-brand" href="/">
            <Burst size={22} />
            <span>EslothAI</span>
          </a>
        </header>
        <div className="share-empty">
          <h1>{gate === 'forbidden' ? 'Not an administrator' : 'Something went wrong'}</h1>
          <p>
            {gate === 'forbidden'
              ? 'This account has no administrator access.'
              : 'The admin screen could not load. Try again in a moment.'}
          </p>
          <a className="share-cta btn" href="/">
            Back to chat
          </a>
        </div>
      </div>
    )
  }

  return (
    <div className="share-page admin-page">
      <header className="share-head">
        <a className="share-brand" href="/">
          <Burst size={22} />
          <span>EslothAI</span>
        </a>
        <span className="admin-identity">
          {me?.email}
          {me?.bootstrap && <span className="admin-badge">from config</span>}
        </span>
      </header>

      <nav className="admin-tabs">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            className={tab === entry.id ? 'admin-tab active' : 'admin-tab'}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <div className="admin-body">
        {error !== null && <p className="admin-error">{error}</p>}

        {tab === 'overview' && stats !== null && (
          <div>
            <div className="admin-refresh-row">
              <button className="admin-action" disabled={busy} onClick={() => void refresh('overview')}>
                Refresh
              </button>
            </div>
            <div className="admin-tiles">
              <div className="admin-tile">
                <span className="admin-tile-value">{stats.usage.total_tokens.toLocaleString()}</span>
                <span className="admin-tile-label">Total tokens</span>
                <span className="admin-tile-sub">
                  {stats.usage.prompt_tokens.toLocaleString()} in ·{' '}
                  {stats.usage.completion_tokens.toLocaleString()} out
                </span>
              </div>
              <div className="admin-tile">
                <span className="admin-tile-value">{stats.usage.requests.toLocaleString()}</span>
                <span className="admin-tile-label">LLM requests</span>
              </div>
              <div className="admin-tile">
                <span className="admin-tile-value">{stats.users.total.toLocaleString()}</span>
                <span className="admin-tile-label">Accounts</span>
                <span className="admin-tile-sub">
                  {stats.users.active_7d} active 7d
                  {stats.users.disabled > 0 && ` · ${stats.users.disabled} disabled`}
                </span>
              </div>
              <div className="admin-tile">
                <span className="admin-tile-value">{stats.content.threads.toLocaleString()}</span>
                <span className="admin-tile-label">Chats</span>
                <span className="admin-tile-sub">{stats.content.messages.toLocaleString()} messages</span>
              </div>
              <div className="admin-tile">
                <span className="admin-tile-value">{stats.content.documents.toLocaleString()}</span>
                <span className="admin-tile-label">Documents</span>
                <span className="admin-tile-sub">
                  {stats.content.projects} projects
                  {stats.content.documents_failed > 0 &&
                    ` · ${stats.content.documents_failed} failed`}
                </span>
              </div>
              <div className="admin-tile">
                <span className="admin-tile-value">{stats.reports.open.toLocaleString()}</span>
                <span className="admin-tile-label">Open reports</span>
                <span className="admin-tile-sub">{stats.reports.resolved} resolved</span>
              </div>
            </div>
            <h2 className="admin-section-title">Top users by tokens</h2>
            {stats.top_users.length === 0 && <p className="admin-empty">No usage recorded yet.</p>}
            {stats.top_users.length > 0 && (
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Name</th>
                      <th>Tokens</th>
                      <th>Requests</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stats.top_users.map((user, index) => (
                      <tr key={index}>
                        <td>{user.email ?? '—'}</td>
                        <td>{user.display_name ?? '—'}</td>
                        <td>{user.total_tokens.toLocaleString()}</td>
                        <td>{user.requests.toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {tab === 'users' && (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Name</th>
                  <th>Last login</th>
                  <th>Tokens</th>
                  <th>Requests</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.uid} className={user.disabled_at !== null ? 'admin-disabled-row' : undefined}>
                    <td title={user.uid}>{user.email ?? '—'}</td>
                    <td>{user.display_name ?? '—'}</td>
                    <td>{when(user.last_login_at)}</td>
                    <td>{user.usage.total_tokens.toLocaleString()}</td>
                    <td>{user.usage.requests.toLocaleString()}</td>
                    <td>{user.disabled_at !== null ? `Disabled ${when(user.disabled_at)}` : 'Active'}</td>
                    <td>
                      <button
                        className="admin-action"
                        disabled={busy}
                        onClick={() =>
                          void act(
                            () => adminSetUserDisabled(userId, user.uid, user.disabled_at === null),
                            'users',
                          )
                        }
                      >
                        {user.disabled_at === null ? 'Disable' : 'Enable'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {users.length === 0 && <p className="admin-empty">No accounts yet.</p>}
          </div>
        )}

        {tab === 'reports' && (
          <div>
            <label className="admin-toggle">
              <input
                type="checkbox"
                checked={includeResolved}
                onChange={(event) => setIncludeResolved(event.target.checked)}
              />
              Show resolved
            </label>
            {reports.length === 0 && <p className="admin-empty">Inbox is empty.</p>}
            {reports.map((report) => (
              <div key={report.report_id} className="admin-report">
                <div className="admin-report-head">
                  <div>
                    <strong>{report.reason}</strong>
                    <span className="admin-report-meta">
                      {' '}
                      · {when(report.created_at)} · from {report.owner_user_id}
                      {report.message_position !== null && ` · message #${report.message_position}`}
                    </span>
                    {report.details !== null && <p className="admin-report-details">{report.details}</p>}
                    {report.resolved_at !== null && (
                      <p className="admin-report-meta">
                        Resolved {when(report.resolved_at)} by {report.resolved_by}
                      </p>
                    )}
                  </div>
                  <div className="admin-report-actions">
                    <button
                      className="admin-action"
                      onClick={() =>
                        setOpenReportId(openReportId === report.report_id ? null : report.report_id)
                      }
                    >
                      {openReportId === report.report_id ? 'Hide chat' : 'View chat'}
                    </button>
                    {report.resolved_at === null && (
                      <button
                        className="admin-action"
                        disabled={busy}
                        onClick={() =>
                          void act(() => adminResolveReport(userId, report.report_id), 'reports')
                        }
                      >
                        Resolve
                      </button>
                    )}
                  </div>
                </div>
                {openReportId === report.report_id && (
                  <div className="admin-conversation">
                    {report.conversation.map((turn, index) => (
                      <p key={index} className={`admin-turn admin-turn-${turn.role}`}>
                        <strong>{turn.role}:</strong> {turn.content}
                      </p>
                    ))}
                    {report.conversation.length === 0 && (
                      <p className="admin-empty">Empty conversation snapshot.</p>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === 'admins' && admins !== null && (
          <div>
            <form
              className="admin-grant-row"
              onSubmit={(event) => {
                event.preventDefault()
                const email = grantDraft.trim()
                if (email.length === 0) return
                void act(async () => {
                  await adminGrantAdmin(userId, email)
                  setGrantDraft('')
                }, 'admins')
              }}
            >
              <input
                type="email"
                placeholder="email@example.com"
                value={grantDraft}
                onChange={(event) => setGrantDraft(event.target.value)}
              />
              <button className="admin-action" disabled={busy || grantDraft.trim().length === 0}>
                Grant admin
              </button>
            </form>
            <ul className="admin-list">
              {admins.bootstrap.map((email) => (
                <li key={email}>
                  <span>{email}</span>
                  <span className="admin-badge">from config</span>
                </li>
              ))}
              {admins.granted.map((grant) => (
                <li key={grant.email}>
                  <span>
                    {grant.email}
                    <span className="admin-report-meta">
                      {' '}
                      · granted by {grant.granted_by} · {when(grant.created_at)}
                    </span>
                  </span>
                  <button
                    className="admin-action admin-danger"
                    disabled={busy}
                    onClick={() => void act(() => adminRevokeAdmin(userId, grant.email), 'admins')}
                  >
                    Revoke
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {tab === 'audit' && (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((entry) => (
                  <tr key={entry.audit_id}>
                    <td>{when(entry.created_at)}</td>
                    <td>{entry.actor_email}</td>
                    <td>{entry.action}</td>
                    <td>{entry.target}</td>
                    <td>{entry.detail ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {audit.length === 0 && <p className="admin-empty">Nothing has happened yet.</p>}
          </div>
        )}
      </div>
    </div>
  )
}
