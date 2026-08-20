import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

interface DevDrawerState {
  open: boolean
  toggle: () => void
  close: () => void
}

const DevDrawerContext = createContext<DevDrawerState>({
  open: false,
  toggle: () => {},
  close: () => {},
})

/**
 * The developer drawer's open state, and the one global key binding that is not
 * a screen's business: `⌘\` / `Ctrl+\` (patterns.md §9).
 *
 * The drawer is deliberately **not** registered with the layer stack as an
 * ordinary layer even though `Esc` closes it: it sits at `--z-devdrawer` (700),
 * *above* modals at 410, because you have to be able to read the request
 * journal for the failed call that put the modal on screen. Registering it
 * would put it at the top of the `Esc` order too, which is right — so it does
 * register, but the drawer itself does that, at the point where it is open.
 */
export function DevDrawerProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const toggle = useCallback(() => setOpen((current) => !current), [])
  const close = useCallback(() => setOpen(false), [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      // `event.key` for `Ctrl+\` is `'\\'` on every layout that has the key;
      // `event.code` is `Backslash` only on ANSI. Matching the key is what
      // makes this work on a layout where the glyph has moved.
      if (event.key !== '\\' || !(event.metaKey || event.ctrlKey)) return
      event.preventDefault()
      toggle()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [toggle])

  const value = useMemo<DevDrawerState>(() => ({ open, toggle, close }), [open, toggle, close])
  return <DevDrawerContext.Provider value={value}>{children}</DevDrawerContext.Provider>
}

export function useDevDrawer(): DevDrawerState {
  return useContext(DevDrawerContext)
}
