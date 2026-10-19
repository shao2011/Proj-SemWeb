import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { isSameOriginSrc, Message } from './Message'

afterEach(() => {
  cleanup()
})

function renderAnswer(text: string) {
  return render(<Message message={{ id: 'a1', role: 'assistant', text }} readOnly />)
}

describe('assistant markdown images', () => {
  it.each([
    'https://evil.example/pixel.png?d=secret',
    'http://evil.example/pixel.png',
    '//evil.example/pixel.png',
    '//EVIL.com/pixel.png',
    'HTTPS://evil.example/pixel.png',
    'data:image/png;base64,iVBORw0KGgo=',
    'javascript:alert(1)',
  ])('does not load %s and shows the alt text instead', (src) => {
    const { container } = renderAnswer(`Before ![leaked alt](${src}) after`)

    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText(/leaked alt/)).toBeTruthy()
  })

  it('still renders a relative image', () => {
    const { container } = renderAnswer('![diagram](media/key.png)')

    const img = container.querySelector('img')
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toBe('media/key.png')
    expect(img!.getAttribute('alt')).toBe('diagram')
  })
})

function offOriginImages(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('img'))
    .map((img) => new URL(img.getAttribute('src') ?? '', window.location.href))
    .filter((url) => url.origin !== window.location.origin)
    .map((url) => url.href)
}

describe('images that look relative but are not', () => {
  it.each(['/\\evil.com/x.png', '\\\\evil.com/x.png', '/\\/evil.com/x.png'])(
    'never loads %s from another origin',
    (src) => {
      const { container } = renderAnswer(`![leaked alt](${src})`)

      expect(offOriginImages(container)).toEqual([])
    },
  )

  it('blocks a reference-style image pointing elsewhere', () => {
    const { container } = renderAnswer('See ![leaked alt][r]\n\n[r]: https://evil.com/x.png')

    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText(/leaked alt/)).toBeTruthy()
  })

  // The markdown parser percent-encodes backslashes before the override sees
  // the src, so the raw forms are checked against the guard directly.
  it.each(['/\\evil.com/x.png', '\\\\evil.com/x.png', '/\\/evil.com/x.png', '//EVIL.com/x.png'])(
    'the guard rejects the raw src %s',
    (src) => {
      expect(isSameOriginSrc(src)).toBe(false)
    },
  )

  it.each(['media/key.png', '/media/key.png', '/%5Cevil.com/x.png'])(
    'the guard allows the same-origin src %s',
    (src) => {
      expect(isSameOriginSrc(src)).toBe(true)
    },
  )

  it.each(['', 'data:image/png;base64,AAAA', 'http://[bad'])('the guard rejects %j', (src) => {
    expect(isSameOriginSrc(src)).toBe(false)
  })
})
