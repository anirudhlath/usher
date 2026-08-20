import { useSyncExternalStore } from 'react'

/**
 * The three designed widths, as a hook.
 *
 * **Only for the handful of places where the markup genuinely differs** — the
 * viewer's header becoming a tab bar, the operator's sidebar becoming an icon
 * rail, a table becoming cards. Everything responsive that CSS can express
 * belongs in CSS: a media query costs nothing and re-renders nothing, and a
 * JavaScript breakpoint that disagrees with a stylesheet is a bug that only
 * appears mid-resize.
 *
 * patterns.md §11 names 1440 / 834 / 390. The boundaries here are one below the
 * next designed width, so 834 itself is `tablet` and 833 is `phone`.
 */
const PHONE_MAX = 833
const TABLET_MAX = 1439

export interface Viewport {
  phone: boolean
  tablet: boolean
  desktop: boolean
}

const QUERIES = {
  phone: `(max-width: ${PHONE_MAX}px)`,
  tablet: `(min-width: ${PHONE_MAX + 1}px) and (max-width: ${TABLET_MAX}px)`,
  desktop: `(min-width: ${TABLET_MAX + 1}px)`,
} as const

/**
 * `useSyncExternalStore` rather than `useState` + an effect: the effect version
 * renders once at the wrong width and then corrects itself, which for the
 * viewer shell means mounting a desktop header on a phone and swapping it a
 * frame later. Visible, and it moves focus.
 */
function subscribe(onChange: () => void): () => void {
  if (typeof matchMedia !== 'function') return () => {}
  const lists = Object.values(QUERIES).map((query) => matchMedia(query))
  lists.forEach((list) => list.addEventListener('change', onChange))
  return () => lists.forEach((list) => list.removeEventListener('change', onChange))
}

// Cached so the snapshot is referentially stable — `useSyncExternalStore`
// compares by identity and a fresh object every call is an infinite loop.
let cached: Viewport = { phone: false, tablet: false, desktop: true }

function snapshot(): Viewport {
  if (typeof matchMedia !== 'function') return cached
  const next: Viewport = {
    phone: matchMedia(QUERIES.phone).matches,
    tablet: matchMedia(QUERIES.tablet).matches,
    desktop: matchMedia(QUERIES.desktop).matches,
  }
  if (next.phone !== cached.phone || next.tablet !== cached.tablet || next.desktop !== cached.desktop) {
    cached = next
  }
  return cached
}

/** The server snapshot is desktop; nothing here is server-rendered today. */
function serverSnapshot(): Viewport {
  return { phone: false, tablet: false, desktop: true }
}

export function useViewport(): Viewport {
  return useSyncExternalStore(subscribe, snapshot, serverSnapshot)
}
