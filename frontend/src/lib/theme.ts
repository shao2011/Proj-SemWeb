export type ThemePref = 'light' | 'dark' | 'auto'

const THEME_KEY = 'rag.theme'
const media = window.matchMedia('(prefers-color-scheme: light)')

export function loadThemePref(): ThemePref {
  const stored = localStorage.getItem(THEME_KEY)
  return stored === 'light' || stored === 'auto' ? stored : 'dark'
}

function resolve(pref: ThemePref): 'light' | 'dark' {
  if (pref === 'auto') return media.matches ? 'light' : 'dark'
  return pref
}

function apply(pref: ThemePref): void {
  document.documentElement.dataset.theme = resolve(pref)
}

export function setThemePref(pref: ThemePref): void {
  localStorage.setItem(THEME_KEY, pref)
  apply(pref)
}

/** Call once at startup: applies the stored preference and keeps 'auto' in
 * sync when the OS scheme changes. */
export function initTheme(): void {
  apply(loadThemePref())
  media.addEventListener('change', () => {
    if (loadThemePref() === 'auto') apply('auto')
  })
}
