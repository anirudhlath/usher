import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { useLocation } from 'react-router-dom'

/**
 * Focus on route change (patterns.md §9).
 *
 * **Focus MUST NOT be left on the link that was clicked.** After a client-side
 * navigation the DOM has changed under a focus ring that is still sitting on a
 * nav item, so the next `Tab` continues from the header rather than from the
 * new page — and a screen reader announces nothing at all, because no document
 * load happened. Focus moves to the new `<main>`'s heading, which is
 * `tabIndex={-1}` so it can receive focus without joining the tab order.
 *
 * `preventScroll` matters: without it the browser scrolls the heading into
 * view, which fights the router's own scroll-to-top and reads as a jump.
 */
export function useFocusOnRouteChange(heading: RefObject<HTMLElement | null>): void {
  const { pathname } = useLocation()
  const first = useRef(true)

  useEffect(() => {
    // Not on first paint: the user has not navigated, and stealing focus from
    // the document on load moves a screen reader past the skip link.
    if (first.current) {
      first.current = false
      return
    }
    heading.current?.focus({ preventScroll: true })
  }, [pathname, heading])
}

/**
 * The polite live region that says where you are.
 *
 * One per app, `aria-live="polite"` — patterns.md §12: nothing in this product
 * is `assertive`. The announcement is deferred a tick because a region that is
 * populated in the same frame it mounts is frequently not announced at all;
 * assistive tech has to observe the *change*.
 */
export function RouteAnnouncer({ label }: { label: string }) {
  const [announced, setAnnounced] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => setAnnounced(label), 100)
    return () => clearTimeout(timer)
  }, [label])
  return (
    <p className="u-visually-hidden" aria-live="polite" aria-atomic="true">
      {announced}
    </p>
  )
}

/** patterns.md §9: every screen starts with one, to `#main`. */
export function SkipLink({ children = 'Skip to content' }: { children?: ReactNode }) {
  return (
    <a className="u-skip-link" href="#main">
      {children}
    </a>
  )
}

/**
 * Scroll restoration for a keyset list (patterns.md §4).
 *
 * **Cursors are not durable**, so this cannot be the usual "remember the offset
 * and set it back": on return there is one page of items and the remembered
 * offset is past the end of it. The specified behaviour is to re-request from
 * the top, keep fetching until either the offset is reachable or three pages
 * have loaded, and then restore — falling back to the top rather than jumping
 * somewhere arbitrary. `scrollIntoView` is never used; it scrolls ancestors
 * too and lands somewhere nobody chose.
 */
const MAX_PAGES_TO_CHASE = 3

export interface ScrollMemory {
  offset: number
  pages: number
}

const memory = new Map<string, ScrollMemory>()

export function rememberScroll(key: string, value: ScrollMemory): void {
  memory.set(key, value)
}

export function useRestoreScroll(
  key: string,
  loadedPages: number,
  fetchNextPage: () => void,
  hasNextPage: boolean,
): void {
  const done = useRef(false)
  useEffect(() => {
    const remembered = memory.get(key)
    if (!remembered || done.current) return
    if (window.scrollY >= remembered.offset) {
      done.current = true
      return
    }
    const reachable = document.documentElement.scrollHeight - window.innerHeight
    if (reachable >= remembered.offset) {
      window.scrollTo({ top: remembered.offset, behavior: 'instant' })
      done.current = true
      return
    }
    if (loadedPages >= Math.min(remembered.pages, MAX_PAGES_TO_CHASE) || !hasNextPage) {
      // Unreachable. The top is a place the user recognises; halfway down a
      // shorter list is not.
      done.current = true
      return
    }
    fetchNextPage()
  }, [key, loadedPages, fetchNextPage, hasNextPage])
}
