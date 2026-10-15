import { ApiError } from './api'
import { isLoggedIn } from './storage'

/** Machine-readable details the server uses where the UI needs words. */
const READABLE: Record<string, string> = {
  login_required_for_projects: 'Log in to use projects.',
  anonymous_message_limit_reached: "You've used your free messages. Log in to keep chatting.",
}

function waitText(seconds: number | null): string {
  if (seconds === null) return 'Try again shortly'
  if (seconds < 60) return `Try again in ${seconds} s`
  return `Try again in ${Math.ceil(seconds / 60)} min`
}

/**
 * What to show for a failed request. Anonymity is read from the stored token
 * because that, not the UI state, is what the server limits on.
 */
export function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 429) {
    const wait = waitText(error.retryAfter ?? null)
    return isLoggedIn()
      ? `Too many requests. ${wait}.`
      : `Too many requests. ${wait}, or log in to continue.`
  }
  if (error instanceof Error) return READABLE[error.message] ?? error.message
  return fallback
}
