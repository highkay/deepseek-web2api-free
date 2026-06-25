import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type Theme = 'light' | 'dark' | 'system'

interface ThemeState {
  theme: Theme
  setTheme: (t: Theme) => void
}

/**
 * Persists the user theme preference. Applies the actual `dark` class
 * to <html> via the side-effect in `applyTheme()`.
 */
export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      theme: 'system',
      setTheme: (theme) => {
        set({ theme })
        applyTheme(theme)
      },
    }),
    { name: 'ds2api-theme' },
  ),
)

/** Compute the effective theme given the user's choice + system preference. */
export function resolveTheme(t: Theme): 'light' | 'dark' {
  if (t === 'system') {
    if (typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'dark'
    }
    return 'light'
  }
  return t
}

/** Apply the resolved theme to <html>. Idempotent. */
export function applyTheme(t: Theme) {
  if (typeof document === 'undefined') return
  const effective = resolveTheme(t)
  const root = document.documentElement
  root.classList.toggle('dark', effective === 'dark')
  root.style.colorScheme = effective
}

/** Re-resolve the theme when the system color scheme changes. */
export function watchSystemTheme() {
  if (typeof window === 'undefined') return () => {}
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  const handler = () => {
    const t = useThemeStore.getState().theme
    if (t === 'system') applyTheme('system')
  }
  mq.addEventListener('change', handler)
  return () => mq.removeEventListener('change', handler)
}
