import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('../lib/api', () => {
  class ApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    listDocuments: vi.fn(),
    getDocumentMarkdown: vi.fn(),
    getDocumentMedia: vi.fn(),
    getDocumentBlocks: vi.fn(),
    updateDocumentBlocks: vi.fn(),
  }
})

import {
  getDocumentBlocks,
  getDocumentMarkdown,
  listDocuments,
  updateDocumentBlocks,
} from '../lib/api'
import type { DocumentMarkdown } from '../lib/api'
import type { BlockEditResult, Project, ProjectFile, ProjectRole } from '../lib/types'
import { DocPanel, type DocFocus } from './DocPanel'

const listDocs = vi.mocked(listDocuments)
const getMarkdown = vi.mocked(getDocumentMarkdown)
const getBlocks = vi.mocked(getDocumentBlocks)
const updateBlocks = vi.mocked(updateDocumentBlocks)

function project(role: ProjectRole, projectId = 'p'): Project {
  return {
    project_id: projectId,
    owner_user_id: 'owner',
    name: 'Project',
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
    role,
    member_count: 0,
  }
}

function file(docId: string, filename: string): ProjectFile {
  return {
    file_id: `f-${docId}`,
    project_id: 'p',
    owner_user_id: 'owner',
    filename,
    content_hash: docId,
    doc_id: docId,
    status: 'completed',
    error: null,
    created_at: '',
    updated_at: '',
  }
}

const MARKDOWN = 'Intro paragraph\n\nCited paragraph\n\nOutro paragraph\n'
const CITED_START = MARKDOWN.indexOf('Cited')
const CITED_END = CITED_START + 'Cited paragraph'.length

let confirmMock: ReturnType<typeof vi.fn<(message?: string) => boolean>>
const originalScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

beforeEach(() => {
  listDocs.mockReset()
  getMarkdown.mockReset()
  getBlocks.mockReset()
  updateBlocks.mockReset()
  confirmMock = vi.fn<(message?: string) => boolean>()
  vi.stubGlobal('confirm', confirmMock)
  Element.prototype.scrollIntoView = vi.fn()

  listDocs.mockResolvedValue([file('d1', 'one.docx'), file('d2', 'two.docx')])
  getMarkdown.mockImplementation(async (_user, _project, docId): Promise<DocumentMarkdown> =>
    docId === 'd1'
      ? { doc_id: 'd1', markdown: MARKDOWN, anchors: { cited: [CITED_START, CITED_END] } }
      : { doc_id: 'd2', markdown: 'Second document body\n', anchors: {} },
  )
  getBlocks.mockResolvedValue({
    doc_id: 'd1',
    version: 'v1',
    blocks: [
      { block_id: 'intro', type: 'paragraph', text: 'Intro paragraph' },
      { block_id: 'cited', type: 'paragraph', text: 'Cited paragraph' },
    ],
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  if (originalScrollIntoView === undefined) {
    delete (Element.prototype as Partial<Element>).scrollIntoView
  } else {
    Object.defineProperty(Element.prototype, 'scrollIntoView', originalScrollIntoView)
  }
})

describe('DocPanel editing', () => {
  it('hides Edit from viewers', async () => {
    render(<DocPanel userId="u" project={project('viewer')} />)

    expect(await screen.findByText('Intro paragraph')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Edit document' })).toBeNull()
  })

  it.each(['editor', 'owner'] as const)('shows Edit to an %s', async (role) => {
    render(<DocPanel userId="u" project={project(role)} />)

    expect(await screen.findByRole('button', { name: 'Edit document' })).toBeTruthy()
  })

  it('hides Edit from a role it does not know', async () => {
    render(<DocPanel userId="u" project={project('commenter' as ProjectRole)} />)

    expect(await screen.findByText('Intro paragraph')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Edit document' })).toBeNull()
  })

  it('says it is loading, not that the project is empty, while the file list loads', async () => {
    let release: (files: ProjectFile[]) => void = () => {}
    listDocs.mockReturnValue(new Promise<ProjectFile[]>((resolve) => { release = resolve }))
    render(<DocPanel userId="u" project={project('owner')} />)

    expect(screen.getByText('Loading…')).toBeTruthy()
    expect(screen.queryByText('No indexed documents in this project yet.')).toBeNull()

    release([])
    expect(await screen.findByText('No indexed documents in this project yet.')).toBeTruthy()
  })

  it.each(['editor', 'owner'] as const)('hides Edit from an %s when there is no document', async (role) => {
    listDocs.mockResolvedValue([])
    render(<DocPanel userId="u" project={project(role)} />)

    expect(await screen.findByText('No indexed documents in this project yet.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Edit document' })).toBeNull()
  })

  it('returns to the same preview and highlight after a cancel without changes', async () => {
    const focus: DocFocus = { docId: 'd1', blockIds: ['cited'], nonce: 1 }
    const { container } = render(<DocPanel userId="u" project={project('editor')} focus={focus} />)
    await screen.findByText('Cited paragraph')
    const before = container.querySelector('.doc-body')?.innerHTML

    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    await screen.findAllByRole('textbox')
    expect(container.querySelector('.doc-highlight')).toBeNull()
    const scroll = vi.mocked(Element.prototype.scrollIntoView)
    scroll.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    const highlight = container.querySelector('.doc-highlight')
    expect(highlight?.textContent).toBe('Cited paragraph')
    expect(scroll).toHaveBeenCalled()
    expect(scroll.mock.contexts).toContain(highlight)
    expect(container.querySelector('.doc-body')?.innerHTML).toBe(before)
    expect(screen.getByRole('tab', { name: 'Preview' }).getAttribute('aria-selected')).toBe('true')
    expect(getMarkdown).toHaveBeenCalledTimes(1)
  })

  it('shows the new text in the preview after a save', async () => {
    updateBlocks.mockResolvedValue({ doc_id: 'd1', version: 'v2', embedded: 1, deleted: 1, kept: 2 })
    const { container } = render(<DocPanel userId="u" project={project('owner')} />)
    await screen.findByText('Intro paragraph')

    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    const areas = await screen.findAllByRole('textbox')
    fireEvent.change(areas[0]!, { target: { value: 'Rewritten intro' } })
    getMarkdown.mockResolvedValue({
      doc_id: 'd1',
      markdown: 'Rewritten intro\n\nCited paragraph\n',
      anchors: {},
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // The textarea also holds this text, so look for it in the preview only.
    await waitFor(() => {
      expect(container.querySelector('.doc-preview')?.textContent).toContain('Rewritten intro')
    })
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(getMarkdown).toHaveBeenCalledTimes(2)
  })

  it('asks before switching files with unsaved drafts', async () => {
    render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    const areas = await screen.findAllByRole('textbox')
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Project files' }))

    confirmMock.mockReturnValue(false)
    fireEvent.click(screen.getByRole('button', { name: 'two.docx' }))
    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('Draft')

    confirmMock.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'two.docx' }))
    expect(await screen.findByText('Second document body')).toBeTruthy()
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
  })

  it('switches files without asking when the editor is clean', async () => {
    render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    await screen.findAllByRole('textbox')
    fireEvent.click(screen.getByRole('button', { name: 'Project files' }))

    fireEvent.click(screen.getByRole('button', { name: 'two.docx' }))

    expect(confirmMock).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryAllByRole('textbox')).toHaveLength(0))
    expect(await screen.findByText('Second document body')).toBeTruthy()
  })

  it('asks before a citation jump discards drafts, then shows the highlight', async () => {
    const view = render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    const areas = await screen.findAllByRole('textbox')
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })

    confirmMock.mockReturnValue(false)
    view.rerender(
      <DocPanel
        userId="u"
        project={project('editor')}
        focus={{ docId: 'd1', blockIds: ['cited'], nonce: 1 }}
      />,
    )
    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('Draft')

    confirmMock.mockReturnValue(true)
    view.rerender(
      <DocPanel
        userId="u"
        project={project('editor')}
        focus={{ docId: 'd1', blockIds: ['cited'], nonce: 2 }}
      />,
    )
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(view.container.querySelector('.doc-highlight')?.textContent).toBe('Cited paragraph')
  })

  it('keeps editing when the already open file is clicked', async () => {
    render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    const areas = await screen.findAllByRole('textbox')
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Project files' }))

    fireEvent.click(screen.getByRole('button', { name: 'one.docx' }))

    expect(confirmMock).not.toHaveBeenCalled()
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('Draft')
  })

  it('closes a clean editor on a citation jump without asking and shows the highlight', async () => {
    const view = render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    await screen.findAllByRole('textbox')

    view.rerender(
      <DocPanel
        userId="u"
        project={project('editor')}
        focus={{ docId: 'd1', blockIds: ['cited'], nonce: 1 }}
      />,
    )

    expect(confirmMock).not.toHaveBeenCalled()
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(view.container.querySelector('.doc-highlight')?.textContent).toBe('Cited paragraph')
  })

  it('leaves edit mode when the project changes and does not reopen it on return', async () => {
    listDocs.mockImplementation(async (_user, projectId) =>
      projectId === 'p' ? [file('d1', 'one.docx')] : [file('d3', 'three.docx')],
    )
    getMarkdown.mockImplementation(async (_user, _project, docId): Promise<DocumentMarkdown> =>
      docId === 'd1'
        ? { doc_id: 'd1', markdown: MARKDOWN, anchors: {} }
        : { doc_id: 'd3', markdown: 'Third document body\n', anchors: {} },
    )
    const view = render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    const areas = await screen.findAllByRole('textbox')
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })

    view.rerender(<DocPanel userId="u" project={project('viewer', 'p2')} />)

    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(screen.getByRole('tab', { name: 'Preview' })).toBeTruthy()
    expect(await screen.findByText('Third document body')).toBeTruthy()
    expect(getBlocks).toHaveBeenCalledTimes(1)

    view.rerender(<DocPanel userId="u" project={project('editor')} />)
    expect(await screen.findByText('Intro paragraph')).toBeTruthy()
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(getBlocks).toHaveBeenCalledTimes(1)
  })

  it('shows tabs and no editor when the new project has no documents', async () => {
    listDocs.mockImplementation(async (_user, projectId) =>
      projectId === 'p' ? [file('d1', 'one.docx')] : [],
    )
    const view = render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    await screen.findAllByRole('textbox')

    view.rerender(<DocPanel userId="u" project={project('editor', 'p3')} />)

    expect(await screen.findByText('No indexed documents in this project yet.')).toBeTruthy()
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(screen.getByRole('tab', { name: 'Preview' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Edit document' })).toBeNull()
  })

  it('ignores a save that finishes after the user moved to another file', async () => {
    let finish: (value: BlockEditResult) => void = () => {}
    updateBlocks.mockReturnValue(
      new Promise<BlockEditResult>((resolve) => {
        finish = resolve
      }),
    )
    const { container } = render(<DocPanel userId="u" project={project('editor')} />)
    await screen.findByText('Intro paragraph')
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    const areas = await screen.findAllByRole('textbox')
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(screen.getByRole('button', { name: 'Project files' }))
    confirmMock.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'two.docx' }))
    expect(await screen.findByText('Second document body')).toBeTruthy()
    const markdownCalls = getMarkdown.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }))
    await screen.findAllByRole('textbox')
    await act(async () => {
      finish({ doc_id: 'd1', version: 'v2', embedded: 1, deleted: 1, kept: 2 })
    })

    expect(screen.queryAllByRole('textbox').length).toBeGreaterThan(0)
    expect(getMarkdown.mock.calls.length).toBe(markdownCalls)
    expect(container.querySelector('.doc-edit')).not.toBeNull()
  })
})
