import { useEffect, useRef, useState } from 'react'
import {
  addProjectMember,
  ApiError,
  listProjectMembers,
  removeProjectMember,
  transferProject,
} from '../lib/api'
import type { Project, ProjectMember } from '../lib/types'
import { ChevronIcon } from './icons'

type MemberRole = 'viewer' | 'editor'

interface Row {
  email: string
  role: MemberRole
  /** Marked for removal; still shown so the choice can be taken back. */
  removing: boolean
  /** Not on the server yet. */
  added: boolean
}

interface ProjectMembersDialogProps {
  userId: string
  project: Project
  onClose: () => void
  /** `transferred` says whether the caller is only an editor now. */
  onSaved: (transferred: boolean) => void
}

const ROLE_LABEL: Record<MemberRole, string> = {
  viewer: 'Viewer',
  editor: 'Editor',
}

/**
 * Who is on a project, and what they may do.
 *
 * Nothing is written until Done. Every change is a draft, including adding
 * somebody, so the dialog has one rule instead of two: an "add" that saved
 * instantly next to a role change that waited would be impossible to predict.
 *
 * Owner-only; the routes behind it refuse anyone else.
 */
export function ProjectMembersDialog({
  userId,
  project,
  onClose,
  onSaved,
}: ProjectMembersDialogProps) {
  const [initial, setInitial] = useState<ProjectMember[]>([])
  const [rows, setRows] = useState<Row[]>([])
  const [newOwner, setNewOwner] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [email, setEmail] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [menuFor, setMenuFor] = useState<string | null>(null)
  // Flipped when the drop-down would spill past the bottom of the dialog card.
  // The list is short, so the card often ends right under the last row.
  const [menuUp, setMenuUp] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])

  useEffect(() => {
    if (menuFor === null) return
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuFor(null)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [menuFor])

  useEffect(() => {
    let cancelled = false
    void listProjectMembers(userId, project.project_id)
      .then((loaded) => {
        if (cancelled) return
        setInitial(loaded)
        setRows(
          loaded.map((item) => ({
            email: item.email,
            role: item.role === 'editor' ? 'editor' : 'viewer',
            removing: false,
            added: false,
          })),
        )
      })
      .catch(() => {
        if (!cancelled) setError('Could not load the member list.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [userId, project.project_id])

  function stageAdd() {
    const address = email.trim().toLowerCase()
    if (address.length === 0) return
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address)) {
      setError('That does not look like an email address.')
      return
    }
    setError(null)
    setEmail('')
    setRows((prev) => {
      const existing = prev.find((item) => item.email === address)
      if (existing !== undefined) {
        // Re-typing somebody who is already listed just un-removes them.
        return prev.map((item) =>
          item.email === address ? { ...item, removing: false } : item,
        )
      }
      return [...prev, { email: address, role: 'viewer', removing: false, added: true }]
    })
  }

  function setRole(address: string, role: MemberRole) {
    setRows((prev) =>
      prev.map((item) =>
        item.email === address ? { ...item, role, removing: false } : item,
      ),
    )
    // Choosing a plain role also cancels a pending handover for that person.
    setNewOwner((prev) => (prev === address ? null : prev))
  }

  function makeOwner(address: string) {
    setRows((prev) =>
      prev.map((item) =>
        item.email === address ? { ...item, removing: false } : item,
      ),
    )
    // Exactly one at a time: a project cannot end up with two owners.
    setNewOwner(address)
  }

  function stageRemove(address: string) {
    setRows((prev) =>
      prev.map((item) =>
        item.email === address ? { ...item, removing: true } : item,
      ),
    )
    setNewOwner((prev) => (prev === address ? null : prev))
  }

  function labelFor(row: Row): string {
    if (newOwner === row.email) return 'Owner'
    if (row.removing) return 'Removing'
    return ROLE_LABEL[row.role]
  }

  const dirty =
    newOwner !== null ||
    rows.some((row) => row.added || row.removing) ||
    rows.some((row) => {
      const before = initial.find((item) => item.email === row.email)
      return before !== undefined && before.role !== row.role
    })

  async function save() {
    setSaving(true)
    setError(null)
    try {
      // Roles first, removals next, the handover last: after a transfer the
      // caller is only an editor and every one of these calls would 403.
      for (const row of rows) {
        if (row.removing || newOwner === row.email) continue
        const before = initial.find((item) => item.email === row.email)
        if (before === undefined || before.role !== row.role) {
          await addProjectMember(userId, project.project_id, row.email, row.role)
        }
      }
      for (const row of rows) {
        if (!row.removing) continue
        if (initial.some((item) => item.email === row.email)) {
          await removeProjectMember(userId, project.project_id, row.email)
        }
      }
      if (newOwner !== null) {
        // The new owner must be a member before they can be handed the
        // project, which matters when they were only added in this session.
        if (!initial.some((item) => item.email === newOwner)) {
          await addProjectMember(userId, project.project_id, newOwner, 'editor')
        }
        await transferProject(userId, project.project_id, newOwner)
      }
      onSaved(newOwner !== null)
    } catch (problem: unknown) {
      setError(
        problem instanceof ApiError && problem.status === 404
          ? 'Nobody has signed in with that address yet, so there is no account to hand the project to.'
          : 'Could not save the changes. Nothing after the failure was applied.',
      )
      setSaving(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        className="modal members-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Project members"
        ref={cardRef}
      >
        <h2>People in {project.name}</h2>
        <p className="confirm-text">
          Everyone here can open the project and ask its documents questions. Their
          own chats stay private to them.
        </p>

        <label className="report-label" htmlFor="member-email">
          Add by email
        </label>
        <div className="share-link-row">
          <input
            id="member-email"
            className="share-link"
            type="email"
            placeholder="name@company.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                event.preventDefault()
                stageAdd()
              }
            }}
          />
          <button
            className="secondary-btn"
            disabled={saving || email.trim().length === 0}
            onClick={stageAdd}
          >
            Add
          </button>
        </div>

        {loading ? (
          <p className="share-note">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="share-note">Nobody else yet.</p>
        ) : (
          <ul className="share-people">
            {rows.map((row) => (
              <li key={row.email} className={row.removing ? 'member-row removing' : 'member-row'}>
                <span className="share-person">{row.email}</span>
                <div className="member-role-wrap" ref={menuFor === row.email ? menuRef : undefined}>
                  <button
                    className="member-role-btn"
                    aria-haspopup="menu"
                    aria-expanded={menuFor === row.email}
                    disabled={saving}
                    onClick={(event) => {
                      const card = cardRef.current?.getBoundingClientRect()
                      const button = event.currentTarget.getBoundingClientRect()
                      // ~4 items plus padding; below that the menu would hang
                      // off the card and read as a rendering bug.
                      setMenuUp(card !== undefined && card.bottom - button.bottom < 190)
                      setMenuFor((prev) => (prev === row.email ? null : row.email))
                    }}
                  >
                    {labelFor(row)}
                    <ChevronIcon size={15} />
                  </button>
                  {menuFor === row.email && (
                    <div
                      className={
                        menuUp ? 'recent-menu member-menu up' : 'recent-menu member-menu'
                      }
                      role="menu"
                    >
                      <button
                        className="recent-menu-item"
                        role="menuitem"
                        onClick={() => {
                          setMenuFor(null)
                          makeOwner(row.email)
                        }}
                      >
                        Make Owner
                      </button>
                      <button
                        className="recent-menu-item"
                        role="menuitem"
                        onClick={() => {
                          setMenuFor(null)
                          setRole(row.email, 'editor')
                        }}
                      >
                        Editor
                      </button>
                      <button
                        className="recent-menu-item"
                        role="menuitem"
                        onClick={() => {
                          setMenuFor(null)
                          setRole(row.email, 'viewer')
                        }}
                      >
                        Viewer
                      </button>
                      <button
                        className="recent-menu-item danger"
                        role="menuitem"
                        onClick={() => {
                          setMenuFor(null)
                          stageRemove(row.email)
                        }}
                      >
                        Remove Access
                      </button>
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}

        {newOwner !== null && (
          <p className="share-note">
            {newOwner} becomes the owner when you save, and you stay on as an editor.
            Only they can hand it back.
          </p>
        )}
        {error !== null && <p className="share-error">{error}</p>}

        <div className="share-actions">
          <button className="report-cancel" disabled={saving} onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary-btn"
            disabled={saving || !dirty}
            onClick={() => void save()}
          >
            {saving ? 'Saving…' : 'Done'}
          </button>
        </div>
      </div>
    </div>
  )
}
