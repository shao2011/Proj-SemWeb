import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import Root from './Root'
import { initTheme } from './lib/theme'
import './styles/global.css'

initTheme()

const container = document.getElementById('root')
if (container === null) throw new Error('Root element is missing')

createRoot(container).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
