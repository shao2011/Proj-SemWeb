import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from './api'
import { errorMessage } from './errors'

afterEach(() => localStorage.clear())

describe('errorMessage', () => {
  it('turns server codes into sentences', () => {
    expect(errorMessage(new ApiError(403, 'login_required_for_projects'), 'x')).toBe(
      'Log in to use projects.',
    )
    expect(errorMessage(new ApiError(403, 'anonymous_message_limit_reached'), 'x')).toBe(
      "You've used your free messages. Log in to keep chatting.",
    )
  })

  it('passes other details through and falls back for non-errors', () => {
    expect(errorMessage(new ApiError(404, 'Thread not found'), 'x')).toBe('Thread not found')
    expect(errorMessage('boom', 'Fallback.')).toBe('Fallback.')
  })

  it('rounds a wait of a minute or more up to whole minutes', () => {
    localStorage.setItem('rag.authToken', 'token')
    expect(errorMessage(new ApiError(429, 'rate_limited', 59), 'x')).toBe(
      'Too many requests. Try again in 59 s.',
    )
    expect(errorMessage(new ApiError(429, 'rate_limited', 60), 'x')).toBe(
      'Too many requests. Try again in 1 min.',
    )
    expect(errorMessage(new ApiError(429, 'rate_limited', 61), 'x')).toBe(
      'Too many requests. Try again in 2 min.',
    )
  })

  it('offers an anonymous visitor the login', () => {
    expect(errorMessage(new ApiError(429, 'rate_limited', 3600), 'x')).toBe(
      'Too many requests. Try again in 60 min, or log in to continue.',
    )
    expect(errorMessage(new ApiError(429, 'rate_limited'), 'x')).toBe(
      'Too many requests. Try again shortly, or log in to continue.',
    )
  })
})
