interface ContextGaugeProps {
  /** Server-computed 0-100. Null hides the gauge (no measured turn yet). */
  percent: number | null
  compacting: boolean
  onCompact: () => void
}

const CLICKABLE_FROM = 50
const WARN_FROM = 80
const DANGER_FROM = 90

const RADIUS = 8
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

/**
 * Circular context meter on the composer. Below 50% it is informational
 * only; from 50% it becomes click-to-compact; 80/90% recolor it as the
 * budget runs out. The percentage itself is computed server-side against
 * the model's usable budget -- 100% is the point where the backend starts
 * folding on its own.
 */
export function ContextGauge({ percent, compacting, onCompact }: ContextGaugeProps) {
  if (percent === null || percent === undefined) return null

  const clamped = Math.min(100, Math.max(0, percent))
  const clickable = clamped >= CLICKABLE_FROM && !compacting
  const tone =
    clamped >= DANGER_FROM ? 'danger' : clamped >= WARN_FROM ? 'warn' : 'ok'
  const title = clickable
    ? `${clamped}% of context used — click to compact`
    : compacting
      ? 'Compacting…'
      : `${clamped}% of context used`
  const dash = (clamped / 100) * CIRCUMFERENCE

  const ring = (
    <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
      <circle className="gauge-track" cx="10" cy="10" r={RADIUS} />
      <circle
        className="gauge-fill"
        cx="10"
        cy="10"
        r={RADIUS}
        strokeDasharray={`${dash} ${CIRCUMFERENCE - dash}`}
        transform="rotate(-90 10 10)"
      />
    </svg>
  )

  if (!clickable) {
    return (
      <span
        className={`context-gauge ${tone}${compacting ? ' compacting' : ''}`}
        title={title}
        aria-label={title}
      >
        {ring}
      </span>
    )
  }
  return (
    <button
      className={`context-gauge ${tone} clickable`}
      title={title}
      aria-label={title}
      onClick={onCompact}
    >
      {ring}
    </button>
  )
}
