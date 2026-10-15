import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('firebase/auth', () => ({ signOut: vi.fn() }))
vi.mock('./firebase', () => ({ firebaseAuth: {} }))
vi.mock('./storage', () => ({
  loadAuthToken: () => null,
  saveAuthToken: vi.fn(),
  resetIdentity: vi.fn(),
  setLoggedIn: vi.fn(),
}))

import { ApiError, getDocumentBlocks, loadProviders, updateDocumentBlocks } from './api'

const fetchMock = vi.fn()

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('getDocumentBlocks', () => {
  it('GETs the blocks route with the identity header and returns the body', async () => {
    const body = {
      doc_id: 'doc/1',
      version: 'v1',
      blocks: [{ block_id: 'b1', type: 'paragraph', text: 'Hello' }],
    }
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    const result = await getDocumentBlocks('user-1', 'proj 1', 'doc/1')

    expect(result).toEqual(body)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/projects/proj%201/documents/doc%2F1/blocks')
    expect(init.method ?? 'GET').toBe('GET')
    expect(init.body).toBeUndefined()
    expect(new Headers(init.headers).get('X-User-ID')).toBe('user-1')
  })

  it('throws ApiError with the status on 404', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: 'Document is not editable' }))

    const error = await getDocumentBlocks('u', 'p', 'd').catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(404)
    expect((error as ApiError).message).toBe('Document is not editable')
  })
})

describe('updateDocumentBlocks', () => {
  it('PATCHes base_version and edits as JSON', async () => {
    const body = { doc_id: 'd', version: 'v2', embedded: 3, deleted: 3, kept: 5 }
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    const result = await updateDocumentBlocks('user-1', 'p', 'd', 'v1', [
      { block_id: 'b2', text: 'New text' },
    ])

    expect(result).toEqual(body)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/projects/p/documents/d/blocks')
    expect(init.method).toBe('PATCH')
    const headers = new Headers(init.headers)
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('X-User-ID')).toBe('user-1')
    expect(JSON.parse(init.body as string)).toEqual({
      base_version: 'v1',
      edits: [{ block_id: 'b2', text: 'New text' }],
    })
  })

  it('surfaces a 409 conflict as ApiError with the API detail', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, { detail: 'Document changed since it was loaded; reload and retry' }),
    )

    const error = await updateDocumentBlocks('u', 'p', 'd', 'v1', [
      { block_id: 'b', text: 'x' },
    ]).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(409)
    expect((error as ApiError).message).toBe(
      'Document changed since it was loaded; reload and retry',
    )
  })
})

describe('loadProviders', () => {
  let warn: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    warn.mockRestore()
  })

  it('resolves the configured providers without logging', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { status: 'ok', configured_providers: ['cohere'] }),
    )

    await expect(loadProviders('user-1')).resolves.toEqual(['cohere'])
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/health')
    expect(warn).not.toHaveBeenCalled()
  })

  it('resolves an empty list and warns once when none are configured', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { status: 'ok', configured_providers: [] }))

    await expect(loadProviders('user-1')).resolves.toEqual([])
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['401', () => jsonResponse(401, { detail: 'Set COHERE_API_KEY secret-token' })],
    ['500', () => jsonResponse(500, { detail: 'run docker compose up -d' })],
  ])('resolves an empty list and warns generically on %s', async (_label, respond) => {
    fetchMock.mockResolvedValue(respond())

    await expect(loadProviders('user-1')).resolves.toEqual([])
    expect(warn).toHaveBeenCalledTimes(1)
    const logged = warn.mock.calls[0].map(String).join(' ')
    expect(logged).not.toMatch(/API_KEY|docker|secret-token|user-1/)
  })

  it('resolves an empty list and warns once on a network error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    await expect(loadProviders('user-1')).resolves.toEqual([])
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0].map(String).join(' ')).not.toMatch(/Failed to fetch|port 8000/)
  })
})
