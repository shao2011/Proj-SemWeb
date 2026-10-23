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
  return { ApiError, getDocumentBlocks: vi.fn(), updateDocumentBlocks: vi.fn() }
})

import { ApiError, getDocumentBlocks, updateDocumentBlocks } from '../lib/api'
import type { BlockEditResult, DocumentBlocks } from '../lib/types'
import { DocBlockEditor } from './DocBlockEditor'

const getBlocks = vi.mocked(getDocumentBlocks)
const updateBlocks = vi.mocked(updateDocumentBlocks)

const LOADED: DocumentBlocks = {
  doc_id: 'd',
  version: 'v1',
  blocks: [
    { block_id: 'b1', type: 'paragraph', text: 'First paragraph' },
    { block_id: 'b2', type: 'list', text: '- one\n- two' },
    { block_id: 'b3', type: 'paragraph', text: 'Third paragraph' },
  ],
}

const SAVED: BlockEditResult = { doc_id: 'd', version: 'v2', embedded: 3, deleted: 3, kept: 4 }

let onDone: ReturnType<typeof vi.fn<(saved: boolean) => void>>
let onDirtyChange: ReturnType<typeof vi.fn<(dirty: boolean) => void>>
let confirmMock: ReturnType<typeof vi.fn<(message?: string) => boolean>>

function renderEditor() {
  return render(
    <DocBlockEditor
      userId="u"
      projectId="p"
      docId="d"
      onDone={onDone}
      onDirtyChange={onDirtyChange}
    />,
  )
}

async function loadedTextareas(): Promise<HTMLTextAreaElement[]> {
  return (await screen.findAllByRole('textbox')) as HTMLTextAreaElement[]
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /save|re-indexing/i }) as HTMLButtonElement
}

beforeEach(() => {
  getBlocks.mockReset()
  updateBlocks.mockReset()
  onDone = vi.fn<(saved: boolean) => void>()
  onDirtyChange = vi.fn<(dirty: boolean) => void>()
  confirmMock = vi.fn<(message?: string) => boolean>()
  vi.stubGlobal('confirm', confirmMock)
  getBlocks.mockResolvedValue(structuredClone(LOADED))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('DocBlockEditor', () => {
  it('loads the blocks and shows one prefilled textarea per block, in order', async () => {
    renderEditor()

    const areas = await loadedTextareas()

    expect(getBlocks).toHaveBeenCalledWith('u', 'p', 'd')
    expect(areas.map((area) => area.value)).toEqual([
      'First paragraph',
      '- one\n- two',
      'Third paragraph',
    ])
  })

  it('shows a load error in the panel with a way back', async () => {
    getBlocks.mockRejectedValue(new ApiError(503, 'Document could not be loaded'))
    renderEditor()

    expect(await screen.findByText('Document could not be loaded')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onDone).toHaveBeenCalledWith(false)
  })

  it('explains a legacy document (404) and offers Back', async () => {
    getBlocks.mockRejectedValue(new ApiError(404, 'Document is not editable'))
    renderEditor()

    expect(
      await screen.findByText('Uploaded before editing was available. Re-upload to edit.'),
    ).toBeTruthy()
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onDone).toHaveBeenCalledWith(false)
  })

  it('shows the API detail for a 404 that is not the legacy case', async () => {
    getBlocks.mockRejectedValue(new ApiError(404, 'Project not found'))
    renderEditor()

    expect(await screen.findByText('Project not found')).toBeTruthy()
    expect(screen.queryByText(/Uploaded before editing/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onDone).toHaveBeenCalledWith(false)
  })

  it('labels each textarea with its type and position', async () => {
    renderEditor()
    await loadedTextareas()

    expect(screen.getByRole('textbox', { name: 'Paragraph 1' })).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'List 2' })).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Paragraph 3' })).toBeTruthy()
  })

  it('fires no callbacks once unmounted mid-save', async () => {
    let finish: (value: BlockEditResult) => void = () => {}
    updateBlocks.mockReturnValue(
      new Promise<BlockEditResult>((resolve) => {
        finish = resolve
      }),
    )
    const view = renderEditor()
    const areas = await loadedTextareas()
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })
    fireEvent.click(saveButton())
    view.unmount()
    onDirtyChange.mockClear()

    await act(async () => {
      finish(SAVED)
    })

    expect(onDone).not.toHaveBeenCalled()
    expect(onDirtyChange).not.toHaveBeenCalled()
  })

  it('sends only the changed blocks with the loaded version, then reports a save', async () => {
    let finish: (value: BlockEditResult) => void = () => {}
    updateBlocks.mockReturnValue(
      new Promise<BlockEditResult>((resolve) => {
        finish = resolve
      }),
    )
    renderEditor()
    const areas = await loadedTextareas()

    fireEvent.change(areas[1]!, { target: { value: '- one\n- two\n- three' } })
    fireEvent.click(saveButton())

    expect(updateBlocks).toHaveBeenCalledTimes(1)
    expect(updateBlocks).toHaveBeenCalledWith('u', 'p', 'd', 'v1', [
      { block_id: 'b2', text: '- one\n- two\n- three' },
    ])
    expect(saveButton().textContent).toContain('Re-indexing…')
    expect(saveButton().disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveProperty('disabled', true)
    for (const area of screen.getAllByRole('textbox')) {
      expect(area).toHaveProperty('disabled', true)
    }
    expect(onDone).not.toHaveBeenCalled()

    await act(async () => {
      finish(SAVED)
    })
    expect(onDone).toHaveBeenCalledWith(true)
  })

  it('exits without a request when nothing changed', async () => {
    renderEditor()
    await loadedTextareas()

    fireEvent.click(saveButton())

    expect(updateBlocks).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledWith(false)
  })

  it('disables Save and hints under a block whose text is blank', async () => {
    renderEditor()
    const areas = await loadedTextareas()

    fireEvent.change(areas[0]!, { target: { value: 'Edited' } })
    fireEvent.change(areas[2]!, { target: { value: '   \n ' } })

    expect(saveButton().disabled).toBe(true)
    const hints = screen.getAllByText(/can't be blank/i)
    expect(hints).toHaveLength(1)
    expect(areas[2]!.closest('.doc-edit-block')?.contains(hints[0]!)).toBe(true)
    expect(hints[0]!.id).not.toBe('')
    expect(areas[2]!.getAttribute('aria-describedby')).toBe(hints[0]!.id)
    expect(areas[0]!.getAttribute('aria-describedby')).toBeNull()

    fireEvent.click(saveButton())
    expect(updateBlocks).not.toHaveBeenCalled()
  })

  it('keeps drafts on a 409, shows the detail and reloads only after confirm', async () => {
    updateBlocks.mockRejectedValue(
      new ApiError(409, 'Document changed since it was loaded; reload and retry'),
    )
    renderEditor()
    const areas = await loadedTextareas()
    fireEvent.change(areas[0]!, { target: { value: 'My draft' } })

    fireEvent.click(saveButton())

    expect(
      await screen.findByText('Document changed since it was loaded; reload and retry'),
    ).toBeTruthy()
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('My draft')
    expect(updateBlocks).toHaveBeenCalledTimes(1)
    expect(onDone).not.toHaveBeenCalled()
    expect(saveButton().disabled).toBe(true)
    fireEvent.click(saveButton())
    expect(updateBlocks).toHaveBeenCalledTimes(1)

    confirmMock.mockReturnValue(false)
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(getBlocks).toHaveBeenCalledTimes(1)
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('My draft')

    getBlocks.mockResolvedValue({
      doc_id: 'd',
      version: 'v9',
      blocks: [{ block_id: 'b1', type: 'paragraph', text: 'Someone else saved this' }],
    })
    confirmMock.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))

    await waitFor(() => {
      expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe(
        'Someone else saved this',
      )
    })
    expect(getBlocks).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('button', { name: 'Reload' })).toBeNull()
    expect(updateBlocks).toHaveBeenCalledTimes(1)
    expect(saveButton().disabled).toBe(false)

    updateBlocks.mockResolvedValue(SAVED)
    fireEvent.change(screen.getAllByRole('textbox')[0]!, { target: { value: 'Again' } })
    fireEvent.click(saveButton())
    expect(updateBlocks).toHaveBeenLastCalledWith('u', 'p', 'd', 'v9', [
      { block_id: 'b1', text: 'Again' },
    ])
  })

  it.each([
    [422, 'Unknown block b1'],
    [503, 'Index unavailable'],
    [0, 'Cannot reach the API. Is the backend running on port 8000?'],
  ])('keeps drafts and re-enables Save after a %i failure', async (status, detail) => {
    updateBlocks.mockRejectedValue(new ApiError(status, detail))
    renderEditor()
    const areas = await loadedTextareas()
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })

    fireEvent.click(saveButton())

    expect(await screen.findByText(detail)).toBeTruthy()
    expect(saveButton().disabled).toBe(false)
    expect(saveButton().textContent).toBe('Save')
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('Draft')
    expect(screen.queryByRole('button', { name: 'Reload' })).toBeNull()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('explains a login refusal instead of showing its code', async () => {
    updateBlocks.mockRejectedValue(new ApiError(403, 'login_required_for_projects'))
    renderEditor()
    const areas = await loadedTextareas()
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })

    fireEvent.click(saveButton())

    expect(await screen.findByText('Log in to use projects.')).toBeTruthy()
    expect(screen.queryByText('login_required_for_projects')).toBeNull()
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('Draft')
  })

  it('asks before discarding drafts on Cancel and stays when declined', async () => {
    renderEditor()
    const areas = await loadedTextareas()
    fireEvent.change(areas[0]!, { target: { value: 'Draft' } })

    confirmMock.mockReturnValue(false)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onDone).not.toHaveBeenCalled()

    confirmMock.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onDone).toHaveBeenCalledWith(false)
  })

  it('cancels without asking when nothing changed', async () => {
    renderEditor()
    await loadedTextareas()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(confirmMock).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledWith(false)
  })

  it('reports dirtiness as drafts diverge and return', async () => {
    renderEditor()
    const areas = await loadedTextareas()

    fireEvent.change(areas[0]!, { target: { value: 'Changed' } })
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)

    fireEvent.change(areas[0]!, { target: { value: 'First paragraph' } })
    expect(onDirtyChange).toHaveBeenLastCalledWith(false)
  })

  it('says so when the document has no editable blocks', async () => {
    getBlocks.mockResolvedValue({ doc_id: 'd', version: 'v1', blocks: [] })
    renderEditor()

    expect(await screen.findByText('This document has no editable text.')).toBeTruthy()
    fireEvent.click(saveButton())
    expect(updateBlocks).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledWith(false)
  })
})
