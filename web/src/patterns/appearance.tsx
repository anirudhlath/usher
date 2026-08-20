import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

/**
 * Theme and density are attributes on `<html>`, and **no component reads them**
 * (patterns.md §10). Only `--density-*` tokens change; nothing else branches on
 * density, which is what stops "compact" becoming a second design system.
 *
 * The two halves of the product have different defaults and one of them is not
 * a preference: **the viewer is pinned dark**, because it lives behind film
 * artwork and the warm neutral ramp was chosen for that. The operator half
 * defaults to light and compact and may be switched.
 */
export type Theme = 'dark' | 'light'
export type Density = 'comfortable' | 'compact'

export interface Appearance {
  theme: Theme
  density: Density
  /** Null while a surface pins the theme — the viewer does. */
  setTheme: ((theme: Theme) => void) | null
}

const AppearanceContext = createContext<Appearance>({
  theme: 'dark',
  density: 'comfortable',
  setTheme: null,
})

const STORAGE_KEY = 'usher.console.operator-theme'

function storedOperatorTheme(): Theme {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    // Private-mode Safari throws on `localStorage` access rather than
    // returning null. A theme preference is not worth an error boundary.
    return 'light'
  }
}

/**
 * Applies the two attributes to `<html>` for as long as it is mounted, and puts
 * them back on unmount so moving between the viewer and the operator halves
 * does not leave the previous surface's density behind.
 */
export function AppearanceProvider({
  children,
  pinnedTheme,
  density,
}: {
  children: ReactNode
  /** The viewer passes `'dark'`. The operator passes nothing and gets a switch. */
  pinnedTheme?: Theme
  density: Density
}) {
  const [operatorTheme, setOperatorTheme] = useState<Theme>(storedOperatorTheme)
  const theme = pinnedTheme ?? operatorTheme

  const setTheme = useCallback((next: Theme) => {
    setOperatorTheme(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* see storedOperatorTheme */
    }
  }, [])

  useEffect(() => {
    const root = document.documentElement
    const previousTheme = root.getAttribute('data-theme')
    const previousDensity = root.getAttribute('data-density')
    root.setAttribute('data-theme', theme)
    // The attribute is only ever set for `compact`. `[data-density="compact"]`
    // is the selector the token layer ships; there is no comfortable rule to
    // apply, because comfortable is what the tokens already are.
    if (density === 'compact') root.setAttribute('data-density', 'compact')
    else root.removeAttribute('data-density')
    return () => {
      if (previousTheme) root.setAttribute('data-theme', previousTheme)
      if (previousDensity) root.setAttribute('data-density', previousDensity)
      else root.removeAttribute('data-density')
    }
  }, [theme, density])

  const value = useMemo<Appearance>(
    () => ({ theme, density, setTheme: pinnedTheme ? null : setTheme }),
    [theme, density, pinnedTheme, setTheme],
  )
  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>
}

/**
 * For the operator's theme switch and for nothing else. A component reaching
 * for this to change its own rendering is the thing §10 forbids — the tokens
 * already did it.
 */
export function useAppearance(): Appearance {
  return useContext(AppearanceContext)
}

/**
 * patterns.md §12: `prefers-reduced-motion: reduce` collapses every duration to
 * 1 ms, removes card lift and press scale, and stops the skeleton sweep. All of
 * that is CSS, in `tokens/base.css` and the group stylesheets — this hook is
 * only for the handful of behaviours JavaScript owns, chiefly not scheduling a
 * 1000 ms patch highlight that will never be seen.
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    if (typeof matchMedia !== 'function') return false
    return matchMedia('(prefers-reduced-motion: reduce)').matches
  })
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const query = matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])
  return reduced
}
