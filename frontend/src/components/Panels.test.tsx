import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { ProjectsPanel } from './Panels'

afterEach(() => {
  cleanup()
})

describe('ProjectsPanel', () => {
  it('shows the normal empty state and no setup instructions when nothing is configured', () => {
    const { container } = render(
      <ProjectsPanel
        projects={[]}
        activeProjectId={null}
        onSelect={vi.fn()}
        onNewProject={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    expect(screen.getByText('Looking to start a project?')).toBeTruthy()
    const text = container.textContent ?? ''
    expect(text).not.toMatch(/API_KEY|docker|provider is configured/i)
  })
})
