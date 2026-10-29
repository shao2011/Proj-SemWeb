import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { expect, request, test, type APIRequestContext } from '@playwright/test'

const ORIGINAL = 'Travel requests require approval code ZX-42 from a line manager before booking.'
const EDITED = 'Travel requests require approval code QR-77 from a line manager before booking.'
const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

// The backend accepts an anonymous-shaped id through the X-User-ID fallback,
// so the specs need no Firebase login.
const userId = `anon_${randomUUID().replaceAll('-', '')}`
const projectName = `E2E doc edit ${Date.now()}`
// Voyage's free tier rate-limits back-to-back runs; pick the embedder per run.
const provider = process.env.E2E_PROVIDER ?? 'jina'

let api: APIRequestContext
let projectId: string
let docId: string

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({
    baseURL: `${baseURL}/api/`,
    extraHTTPHeaders: { 'X-User-ID': userId },
  })
  const created = await api.post('projects', { data: { name: projectName, provider } })
  expect(created.status()).toBe(201)
  projectId = (await created.json()).project_id

  const upload = await api.post(`projects/${projectId}/documents`, {
    multipart: {
      files: {
        name: 'travel-policy.docx',
        mimeType: DOCX,
        buffer: readFileSync('e2e/fixtures/travel-policy.docx'),
      },
    },
  })
  expect(upload.status()).toBe(200)
  const stream = await upload.text()
  const result = stream
    .split('\n')
    .filter((line) => line.startsWith('data: '))
    .map((line) => JSON.parse(line.slice('data: '.length)))
    .find((payload) => payload.status !== undefined)
  expect(result?.status).toBe('completed')
  docId = result.doc_id
})

test.afterAll(async () => {
  if (projectId !== undefined) await api.delete(`projects/${projectId}`)
  await api.dispose()
})

async function queryTexts(text: string): Promise<string> {
  const response = await api.post(`projects/${projectId}/query`, { data: { query: text } })
  expect(response.status()).toBe(200)
  const body = await response.json()
  return body.hits.map((hit: { source_text: string }) => hit.source_text).join('\n')
}

test('an editor fixes a sentence in the panel and the knowledge base follows', async ({ page }) => {
  expect(await queryTexts('travel approval code')).toContain('ZX-42')

  await page.addInitScript((id) => localStorage.setItem('rag.userId', id), userId)
  await page.goto('/')
  await page.getByRole('button', { name: projectName }).click()
  await page.getByRole('textbox', { name: 'Message' }).fill('What approval code do travel requests need?')
  await page.getByRole('button', { name: 'Send message' }).click()

  await page.getByRole('button', { name: 'Project documents' }).click()
  const panel = page.getByRole('complementary', { name: 'Project documents' })
  await expect(panel.getByText(ORIGINAL)).toBeVisible()

  await panel.getByRole('button', { name: 'Edit document' }).click()
  const paragraph = panel.getByRole('textbox', { name: 'Paragraph 1' })
  await expect(paragraph).toHaveValue(ORIGINAL)
  await paragraph.fill(EDITED)
  await panel.getByRole('button', { name: 'Save' }).click()

  // Back on the preview with the new text, without a page reload.
  await expect(panel.getByText(EDITED)).toBeVisible()
  await expect(panel.getByText(ORIGINAL)).toHaveCount(0)
  await expect(panel.getByRole('textbox', { name: 'Paragraph 1' })).toHaveCount(0)

  const texts = await queryTexts('travel approval code QR-77')
  expect(texts).toContain('QR-77')
  expect(texts).not.toContain('ZX-42')
})

test('a save from a stale version is refused with 409', async () => {
  const current = await api.get(`projects/${projectId}/documents/${docId}/blocks`)
  expect(current.status()).toBe(200)
  const { blocks } = await current.json()

  const stale = await api.patch(`projects/${projectId}/documents/${docId}/blocks`, {
    data: {
      base_version: '0'.repeat(32),
      edits: [{ block_id: blocks[0].block_id, text: 'Should never land.' }],
    },
  })
  expect(stale.status()).toBe(409)
  expect(await queryTexts('Should never land')).not.toContain('Should never land.')
})

test('a document id that is not in the project is not editable', async () => {
  const missing = await api.get(`projects/${projectId}/documents/doc_000000000000000000000000/blocks`)
  expect(missing.status()).toBe(404)
})
