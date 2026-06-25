import { useEffect, useState } from 'react'
import { useThemeStore, applyTheme, resolveTheme, watchSystemTheme } from '@/stores/theme'

/** Returns the *effective* theme (`light` | `dark`) given user pref + system. */
export function useTheme() {
  const pref = useThemeStore((s) => s.theme)
  const [effective, setEffective] = useState<'light' | 'dark'>(() => resolveTheme(pref))

  useEffect(() => {
    applyTheme(pref)
    setEffective(resolveTheme(pref))
    return watchSystemTheme()
  }, [pref])

  return { pref, effective }
}
