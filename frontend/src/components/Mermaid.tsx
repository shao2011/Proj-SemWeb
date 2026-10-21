import { useEffect, useId, useState } from 'react'
import mermaid from 'mermaid'

mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark' })

interface MermaidProps {
  code: string
}

/**
 * Rendered only once a message has finished streaming (the caller is
 * responsible for that): a diagram fence is unreadable syntax until it is
 * closed, so rendering it mid-stream would mean parsing partial mermaid
 * source on every delta.
 */
export function Mermaid({ code }: MermaidProps) {
  const rawId = useId().replace(/[^a-zA-Z0-9]/g, '')
  const [svg, setSvg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    mermaid
      .render(`mermaid-${rawId}`, code)
      .then((result) => {
        if (!cancelled) setSvg(result.svg)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Could not render diagram')
        }
      })
    return () => {
      cancelled = true
    }
  }, [code, rawId])

  if (error !== null) {
    return (
      <div className="mermaid-error">
        <p>Could not render this diagram.</p>
        <pre className="md-pre">
          <code>{code}</code>
        </pre>
      </div>
    )
  }

  if (svg === null) {
    return <div className="mermaid-loading">Rendering diagram…</div>
  }

  // mermaid.render returns sanitized SVG markup (securityLevel: 'strict'
  // above), not raw user HTML, so this is the library's own trusted output.
  return <div className="mermaid-diagram" dangerouslySetInnerHTML={{ __html: svg }} />
}
