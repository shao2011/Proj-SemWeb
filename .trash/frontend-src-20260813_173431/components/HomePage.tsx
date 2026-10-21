import { useEffect, useRef, useState } from 'react'
import { BulbIcon, CheckIcon, ChevronIcon, DocumentsIcon, PlusIcon, ProjectsIcon } from './icons'

interface HomePageProps {
  onGoToLogin: () => void
}

// Visual parity only: the real site's other nav items (Platform, Solutions,
// Pricing, Resources) have no destination in this app yet, so they render
// disabled rather than looking clickable and silently doing nothing.
const INERT_NAV = ['Platform', 'Solutions', 'Pricing', 'Resources']

// No billing exists in this app today (confirmed across the whole repo) —
// these three tiers are presentation-only, same as every other CTA on this
// page. Feature bullets are limited to real, shipped capabilities plus soft,
// non-numeric aspirations; nothing here claims a limit the backend enforces.
const PLANS = [
  {
    name: 'Starter',
    description: 'For everyday research.',
    price: '29.000₫',
    icon: DocumentsIcon,
    note: null as string | null,
    features: [
      'Chat grounded in your documents (PDF, Word, Excel, PowerPoint)',
      'Citations with every answer',
      'Organize work into projects',
    ],
  },
  {
    name: 'Pro',
    description: 'For deeper work.',
    price: '49.000₫',
    icon: ProjectsIcon,
    note: 'Everything in Starter, plus:',
    features: [
      'Choice of embedding provider per project',
      'Conversation memory across sessions',
      'Priority processing',
    ],
  },
  {
    name: 'Max',
    description: 'For power users.',
    price: '99.000₫',
    icon: BulbIcon,
    note: 'Everything in Pro, plus:',
    features: ['Unlimited projects', 'Faster support', 'Early access to new features'],
  },
]

const FAQ = [
  {
    question: 'What is EslothAI and how does it work?',
    answer:
      "EslothAI Chat answers grounded in your own uploaded documents — every answer cites the exact passages it came from, so you can verify it yourself.",
  },
  {
    question: 'What should I use EslothAI Chat for?',
    answer:
      'Research and Q&A over your own files: PDFs, Word docs, spreadsheets, and slide decks. Organize them into projects so you never have to re-upload or re-explain context.',
  },
  {
    question: 'How much does it cost to use?',
    answer: 'Three plans starting at 29.000₫/month. See Plans above for details.',
  },
]

export function HomePage({ onGoToLogin }: HomePageProps) {
  const [productsOpen, setProductsOpen] = useState(false)
  const productsRef = useRef<HTMLDivElement>(null)
  const closeTimer = useRef<number | null>(null)

  useEffect(() => {
    if (!productsOpen) return
    const close = (event: MouseEvent) => {
      if (!productsRef.current?.contains(event.target as Node)) setProductsOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setProductsOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [productsOpen])

  useEffect(() => {
    return () => {
      if (closeTimer.current) window.clearTimeout(closeTimer.current)
    }
  }, [])

  // The dropdown menu sits a few pixels below the trigger button and is
  // absolutely positioned, so it doesn't extend the wrapper's hoverable box:
  // crossing that gap fires mouseleave before the pointer reaches the menu.
  // A short close delay (cancelled on re-entry) rides through that gap.
  function openProducts() {
    if (closeTimer.current) {
      window.clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
    setProductsOpen(true)
  }

  function scheduleCloseProducts() {
    closeTimer.current = window.setTimeout(() => {
      setProductsOpen(false)
    }, 250)
  }

  return (
    <div className="home-page">
      <header className="home-nav">
        <span className="brand">
          <img className="brand-logo" src="/logo.png" alt="" />
          <span className="wordmark">EslothAI</span>
        </span>

        <nav className="home-nav-links">
          <div
            className="home-nav-dropdown"
            ref={productsRef}
            onMouseEnter={openProducts}
            onMouseLeave={scheduleCloseProducts}
          >
            <button
              className={productsOpen ? 'home-nav-item open' : 'home-nav-item'}
              aria-haspopup="menu"
              aria-expanded={productsOpen}
              onClick={() => setProductsOpen((prev) => !prev)}
            >
              Meet EslothAI
              <ChevronIcon size={14} />
            </button>
            {productsOpen && (
              <div className="model-menu home-products-menu" role="menu">
                <a
                  className="model-menu-item"
                  role="menuitem"
                  href="https://eslothai.com"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <span className="model-menu-text">
                    <span className="model-menu-name">EngCoach</span>
                    <span className="model-menu-hint">eslothai.com</span>
                  </span>
                </a>
                <button
                  className="model-menu-item"
                  role="menuitem"
                  onClick={() => {
                    setProductsOpen(false)
                    onGoToLogin()
                  }}
                >
                  <span className="model-menu-text">
                    <span className="model-menu-name">EslothAI Chat</span>
                    <span className="model-menu-hint">Chat grounded in your documents</span>
                  </span>
                </button>
              </div>
            )}
          </div>

          {INERT_NAV.map((label) => (
            <button key={label} className="home-nav-item" disabled>
              {label}
              <ChevronIcon size={14} />
            </button>
          ))}
        </nav>

        <div className="home-nav-actions">
          <button className="secondary-btn" disabled>
            Contact sales
          </button>
          <button className="secondary-btn" onClick={onGoToLogin}>
            Log in
          </button>
          <button className="primary-btn" onClick={onGoToLogin}>
            Get Started
          </button>
        </div>
      </header>

      <main className="home-hero">
        <div className="home-hero-copy">
          <h1>Question what&rsquo;s next</h1>
          <p className="home-hero-sub">Your thinking partner for big ambitions</p>

          <div className="home-hero-actions">
            <button className="primary-btn" onClick={onGoToLogin}>
              Get Started
            </button>
            <p className="home-hero-login-hint">
              Already have an account?{' '}
              <button onClick={onGoToLogin}>Log in</button>
            </p>
          </div>
        </div>

        <div className="home-hero-image" aria-hidden="true">
          Photo coming soon
        </div>
      </main>

      <section className="home-plans">
        <h2>Explore plans</h2>
        <div className="home-plan-tabs">
          <button className="home-plan-tab active" type="button">
            Individual
          </button>
          <button className="home-plan-tab" type="button" disabled>
            Team and Enterprise
          </button>
        </div>
        <div className="home-plan-grid">
          {PLANS.map((plan) => (
            <div className="home-plan-card" key={plan.name}>
              <plan.icon size={26} />
              <h3>{plan.name}</h3>
              <p className="home-plan-desc">{plan.description}</p>
              <p className="home-plan-price">
                {plan.price}
                <span>/month</span>
              </p>
              <button className="primary-btn home-plan-btn" onClick={onGoToLogin}>
                Get Started
              </button>
              <div className="home-plan-divider" />
              {plan.note && <p className="home-plan-note">{plan.note}</p>}
              <ul className="home-plan-features">
                {plan.features.map((feature) => (
                  <li key={feature}>
                    <CheckIcon size={15} />
                    {feature}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </section>

      <section className="home-faq">
        <h2>Frequently asked questions</h2>
        <div className="faq-list">
          {FAQ.map((item) => (
            <details className="faq-item" key={item.question}>
              <summary>
                {item.question}
                <PlusIcon size={16} />
              </summary>
              <p>{item.answer}</p>
            </details>
          ))}
        </div>
      </section>
    </div>
  )
}
