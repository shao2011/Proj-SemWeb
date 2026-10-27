import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

// jsdom has no matchMedia, and lib/theme reads it at import time.
vi.hoisted(() => {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia
})

vi.mock('firebase/auth', () => ({ signOut: vi.fn(() => Promise.resolve()) }))
vi.mock('./lib/firebase', () => ({ firebaseAuth: {} }))

import App from './App'

type Route = () => Response | Promise<Response>

const fetchMock = vi.fn()
let warn: MockInstance<typeof console.warn>

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const LEAK = 'Set COHERE_API_KEY then run docker compose up -d'

const project = {
  project_id: 'p1',
  owner_user_id: 'u',
  name: 'Quarterly Reports',
  visibility: 'private',
  embedding_profile: {
    provider: 'cohere',
    model: 'm',
    version: '1',
    dimensions: 4,
    task: 't',
    mode: 'm',
  },
  icon: null,
  color: null,
  created_at: '',
  updated_at: '',
  role: 'owner',
  member_count: 0,
}

function serve(overrides: Record<string, Route>) {
  const routes: Record<string, Route> = {
    '/api/health': () => json(200, { status: 'ok', configured_providers: ['cohere'] }),
    '/api/projects': () => json(200, [project]),
    '/api/threads': () => json(200, []),
    '/api/models': () => json(200, { default: 'auto', models: [] }),
    ...overrides,
  }
  fetchMock.mockImplementation((input: string) => {
    const path = String(input).split('?')[0] ?? ''
    const route = routes[path]
    return Promise.resolve(route ? route() : json(200, []))
  })
}

function bootstrapWarnings(): string[] {
  return warn.mock.calls.map((args) => args.map(String).join(' '))
}

beforeEach(() => {
  localStorage.clear()
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  warn.mockRestore()
  vi.unstubAllGlobals()
})

describe('App start-up', () => {
  it('still loads projects and shows no toast when /health fails', async () => {
    serve({ '/api/health': () => json(401, { detail: LEAK }) })

    render(<App loggedIn={false} onLogin={vi.fn()} />)

    expect(await screen.findByTitle('Quarterly Reports')).toBeTruthy()
    await waitFor(() => expect(warn).toHaveBeenCalled())
    expect(document.querySelector('.toast')).toBeNull()
    expect(document.body.textContent).not.toMatch(/API_KEY|docker/)
    expect(bootstrapWarnings()).toEqual(['Could not load provider status.'])
  })

  it('logs a generic warning and shows no toast when projects fail', async () => {
    serve({ '/api/projects': () => json(500, { detail: LEAK }) })

    render(<App loggedIn={false} onLogin={vi.fn()} />)

    await waitFor(() =>
      expect(bootstrapWarnings()).toContain('Could not load your workspace.'),
    )
    expect(document.querySelector('.toast')).toBeNull()
    expect(document.body.textContent).not.toMatch(/API_KEY|docker/)
    expect(bootstrapWarnings().join(' ')).not.toMatch(/API_KEY|docker/)
  })
})

describe('App login', () => {
  it('shows the account name when the anonymous settings answer after login', async () => {
    const anonId = `anon_${'a'.repeat(32)}`
    localStorage.setItem('rag.userId', anonId)
    let releaseAnon: () => void = () => {}
    const anonSettings = new Promise<Response>((resolve) => {
      releaseAnon = () => resolve(json(200, settingsFor(anonId, null)))
    })
    serve({})
    const routes = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation((input: string, init?: RequestInit) => {
      const path = String(input).split('?')[0] ?? ''
      const caller = new Headers(init?.headers).get('X-User-ID')
      if (path === '/api/settings') {
        // A cold backend answers the pre-login request last.
        return caller === anonId
          ? anonSettings
          : Promise.resolve(json(200, settingsFor('uid-1', 'Duc Tran')))
      }
      return routes(input, init)
    })

    const { rerender } = render(<App loggedIn={false} onLogin={vi.fn()} />)
    localStorage.setItem('rag.userId', 'uid-1')
    rerender(<App loggedIn onLogin={vi.fn()} />)
    expect((await screen.findAllByText('Duc Tran')).length).toBeGreaterThan(0)

    releaseAnon()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryAllByText('Duc Tran').length).toBeGreaterThan(0)
    expect(screen.queryByText('Lorem Ipsum')).toBeNull()
  })
})

describe('App abuse limits', () => {
  const thread = {
    thread_id: 't1',
    owner_user_id: 'u',
    title: 'hello',
    project_id: null,
    created_at: '',
    updated_at: '',
  }

  function serveWith(routes: Record<string, (method: string) => Response>) {
    serve({})
    const fallback = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation((input: string, init?: RequestInit) => {
      const path = String(input).split('?')[0] ?? ''
      const route = routes[path]
      if (route === undefined) return fallback(input, init)
      return Promise.resolve(route(init?.method ?? 'GET'))
    })
  }

  function limited(retryAfter: string | null): Response {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (retryAfter !== null) headers['Retry-After'] = retryAfter
    return new Response(JSON.stringify({ detail: 'rate_limited' }), { status: 429, headers })
  }

  /** The server sees a login by its token, so the test stores one. */
  function logIn() {
    localStorage.setItem('rag.authToken', 'token')
    localStorage.setItem('rag.userId', 'uid-1')
  }

  async function createProject() {
    render(<App loggedIn onLogin={vi.fn()} />)
    fireEvent.click(screen.getAllByRole('button', { name: 'New project' })[0]!)
    const create = await screen.findByRole('button', { name: 'Create project' })
    await waitFor(() => expect((create as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(create)
  }

  async function sendHello(loggedIn: boolean) {
    render(<App loggedIn={loggedIn} onLogin={vi.fn()} />)
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'hello' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  }

  it('sends an anonymous visitor to the login modal from New project', async () => {
    serveWith({})
    render(<App loggedIn={false} onLogin={vi.fn()} />)

    fireEvent.click(screen.getAllByRole('button', { name: 'New project' })[0]!)

    expect(await screen.findByRole('dialog', { name: 'Sign up or log in' })).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: 'New project' })).toBeNull()
  })

  it('sends an anonymous visitor to the login modal when they attach a file in a plain chat', async () => {
    serveWith({})
    render(<App loggedIn={false} onLogin={vi.fn()} />)

    fireEvent.change(await screen.findByTestId('file-input'), {
      target: { files: [new File(['x'], 'memo.docx')] },
    })

    expect(await screen.findByRole('dialog', { name: 'Sign up or log in' })).toBeTruthy()
    expect(screen.queryByText('Open a project before uploading a document.')).toBeNull()
  })

  it('opens the login dialog when an upload into an old anonymous project is refused', async () => {
    serveWith({
      '/api/projects/p1/documents': (method) =>
        method === 'POST'
          ? json(403, { detail: 'login_required_for_projects' })
          : json(200, []),
    })
    render(<App loggedIn={false} onLogin={vi.fn()} />)
    fireEvent.click(await screen.findByTitle('Quarterly Reports'))

    fireEvent.change(await screen.findByTestId('rail-file-input'), {
      target: { files: [new File(['x'], 'memo.docx')] },
    })

    expect(await screen.findByRole('dialog', { name: 'Sign up or log in' })).toBeTruthy()
    expect(screen.queryByText('login_required_for_projects')).toBeNull()
  })

  it('tells a logged-in user how long to wait, in seconds', async () => {
    logIn()
    serveWith({
      '/api/projects': (method) => (method === 'POST' ? limited('42') : json(200, [])),
    })

    await createProject()

    expect(await screen.findByText('Too many requests. Try again in 42 s.')).toBeTruthy()
  })

  it('shows a long wait in minutes', async () => {
    logIn()
    serveWith({
      '/api/projects': (method) => (method === 'POST' ? limited('240') : json(200, [])),
    })

    await createProject()

    expect(await screen.findByText('Too many requests. Try again in 4 min.')).toBeTruthy()
  })

  it('says shortly when the server sends no Retry-After', async () => {
    logIn()
    serveWith({
      '/api/projects': (method) => (method === 'POST' ? limited(null) : json(200, [])),
    })

    await createProject()

    expect(await screen.findByText('Too many requests. Try again shortly.')).toBeTruthy()
  })

  it('rolls back a rate-limited message and offers an anonymous visitor the login', async () => {
    serveWith({
      '/api/threads': (method) => (method === 'POST' ? json(201, thread) : json(200, [])),
      '/api/threads/t1/messages': () => limited('7'),
    })

    await sendHello(false)

    expect(
      await screen.findByText('Too many requests. Try again in 7 s, or log in to continue.'),
    ).toBeTruthy()
    // The server refused before storing it, so the bubble must not linger.
    expect(document.querySelector('.thread')?.textContent ?? '').not.toContain('hello')
  })

  it('shows a project login refusal on a message as a sentence, not a code', async () => {
    serveWith({
      '/api/threads': (method) => (method === 'POST' ? json(201, thread) : json(200, [])),
      '/api/threads/t1/messages': () => json(403, { detail: 'login_required_for_projects' }),
    })

    await sendHello(false)

    expect(await screen.findByText('Log in to use projects.')).toBeTruthy()
  })
})

function settingsFor(ownerUserId: string, displayName: string | null) {
  return {
    owner_user_id: ownerUserId,
    memory_enabled: true,
    display_name: displayName,
    email: null,
    created_at: '',
    updated_at: '',
  }
}
