import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ModelOption } from '../lib/types'
import { Composer } from './Composer'

const MODELS: ModelOption[] = [
  { id: 'z-ai/glm-5.2:free', label: 'GLM 5.2', display_name: 'z-ai/glm-5.2' },
  { id: 'openai/gpt-5.5', label: 'GPT-5.5', display_name: 'openai/gpt-5.5' },
  { id: 'google/gemma-4-31b-it:free', label: 'Gemma', display_name: 'google/gemma-4-31b-it' },
  {
    id: 'deepseek/deepseek-v4-flash-free',
    label: 'DeepSeek V4 Flash',
    display_name: 'deepseek/deepseek-v4-flash',
  },
]

function renderComposer(onModelChange?: (modelId: string | null) => void) {
  render(
    <Composer
      disabled={false}
      busy={false}
      onSend={() => {}}
      onAttach={() => {}}
      models={MODELS}
      onModelChange={onModelChange}
    />,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Choose model' }))
}

afterEach(cleanup)

describe('Composer model menu', () => {
  it('shows display_name as the hint, not the raw free-suffixed id', () => {
    renderComposer()

    const glm = screen.getByRole('menuitemradio', { name: /GLM 5\.2/ })
    expect(glm.textContent).toContain('z-ai/glm-5.2')
    expect(glm.textContent).not.toContain(':free')
  })

  it('uses display_name for models behind the "Other models" fold', () => {
    renderComposer()
    fireEvent.click(screen.getByRole('button', { name: 'Other models' }))

    const flash = screen.getByRole('menuitemradio', { name: /DeepSeek V4 Flash/ })
    expect(flash.textContent).toContain('deepseek/deepseek-v4-flash')
    expect(flash.textContent).not.toMatch(/free/i)
  })

  it('still sends the raw id when a row is picked', () => {
    const onModelChange = vi.fn()
    renderComposer(onModelChange)

    fireEvent.click(screen.getByRole('menuitemradio', { name: /GLM 5\.2/ }))

    expect(onModelChange).toHaveBeenCalledWith('z-ai/glm-5.2:free')
  })
})
