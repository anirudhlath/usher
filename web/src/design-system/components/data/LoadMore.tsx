import { useEffect, useRef } from 'react'
import { Button } from '../actions'

/**
 * Keyset "load more". The only pagination idiom in this product: no page numbers, no totals, no
 * jump-to-page, no result counts. `next_cursor === null` means the last page — say so in words.
 *
 * Changing a filter invalidates outstanding cursors (400 invalid_cursor). The list must silently
 * restart from the top; a viewer never sees that error.
 */
export interface LoadMoreProps {
  /** The opaque cursor from the response. null = end of list. */
  nextCursor?: string | null
  loading?: boolean
  onLoad?: () => void
  /** Honest progress without a denominator: "72 loaded so far". Never "72 of 400". */
  loadedLabel?: string
  endMessage?: string
  /** Auto-load on approach (600 px). Use for viewer grids; keep the button for operator tables. */
  autoLoad?: boolean
}

/** A silent stop is indistinguishable from a bug, so the end of a list is a sentence. */
export const LOAD_MORE_END_MESSAGE = 'That is everything we have for this filter.'

/** patterns.md §4: viewer grids auto-load at 600 px of approach. */
export const LOAD_MORE_ROOT_MARGIN = '600px'

/**
 * Keyset pagination footer. There is no total, no page number, and no count — only "there might
 * be more". `nextCursor === null` is the end of the list.
 *
 * The button is rendered in **both** modes on purpose: the sentinel is a decorative node that sits
 * ahead of the footer's own control and never *is* the last row, so the final page stays reachable
 * from the keyboard even when the observer never fires (§4).
 */
export function LoadMore({
  nextCursor,
  loading = false,
  onLoad,
  loadedLabel,
  endMessage,
  autoLoad = false,
}: LoadMoreProps) {
  const sentinelRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    const sentinel = sentinelRef.current
    if (!autoLoad || nextCursor == null || loading || sentinel === null) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) onLoad?.()
      },
      { rootMargin: LOAD_MORE_ROOT_MARGIN },
    )
    observer.observe(sentinel)
    return () => {
      observer.disconnect()
    }
  }, [autoLoad, nextCursor, loading, onLoad])

  if (nextCursor == null) {
    return (
      <div className="u-more">
        <span className="u-more__end">{endMessage ?? LOAD_MORE_END_MESSAGE}</span>
      </div>
    )
  }

  return (
    <div className="u-more">
      {autoLoad ? <span className="u-more__sentinel" aria-hidden="true" ref={sentinelRef} /> : null}
      <Button
        type="button"
        variant="secondary"
        loading={loading}
        loadingLabel="Loading…"
        onClick={() => {
          onLoad?.()
        }}
      >
        Load more
      </Button>
      {loadedLabel ? <span className="u-more__note">{loadedLabel}</span> : null}
    </div>
  )
}
