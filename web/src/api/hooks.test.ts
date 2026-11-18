/**
 * The hooks, against the real MSW server.
 *
 * Most of what a hook does is uninteresting to test. Three things are not:
 *
 * · **The keyset stop.** `getNextPageParam` has to turn the API's
 *   `next_cursor: null` into `undefined`, because TanStack reads `undefined` as
 *   "no next page" and `null` as a legitimate page parameter — returning the
 *   API's value straight through asks for page one forever.
 * · **`useSearch`'s default lane**, which disagreed with the search page's own
 *   default in the reference client, so which lane ran depended on whether the
 *   caller passed an argument.
 * · **`useReadiness`'s `retry: false`**, and the fact that its 503 still
 *   carries a readable readiness document.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import React from 'react'
import { http, HttpResponse } from 'msw'
import { server } from '@/test/server'
import { createTestQueryClient } from '@/test/render'
import { degradedReadiness } from '@/test/handlers'
import {
  readinessFromError,
  useBrowse,
  useHome,
  useReadiness,
  useSearch,
  useSeasonEpisodes,
  useSimilar,
  useTitle,
  useUnmatched,
} from './hooks'
import { browsePageOne, browsePageTwo } from '@/test/fixtures/browse'
import { readinessDegraded, readinessNotADocument } from '@/test/fixtures/meta'
import {
  SEASON_ONE,
  IMAGE_POSTER,
  TITLE_ENRICHED,
  TITLE_MISSING,
  TITLE_SIMILAR_EMPTY,
  TITLE_SIMILAR_FINDING,
  TITLE_SKELETON,
} from '@/test/fixtures/ids'
import { SIMILAR_REREAD_MS, SIMILAR_WAIT_MS } from './hooks'
import { similarRow, similarUpdating } from '@/test/fixtures/titles'
import type { SimilarResponse } from './hooks'
import { UsherProblem } from './client'

/**
 * One client per test, built **outside** the wrapper component.
 *
 * A wrapper that calls `createTestQueryClient()` in its own body constructs a
 * fresh client on every render, which throws the cache away between renders —
 * an infinite query then appears to accumulate no pages at all, and the failure
 * reads as a bug in `getNextPageParam` rather than in the harness.
 */
function makeWrapper() {
  const client = createTestQueryClient()
  return function Wrapper({ children }: { children: ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children)
  }
}

describe('keyset pagination', () => {
  it('reaches undefined on a null cursor, so the walk stops', async () => {
    const { result } = renderHook(() => useBrowse({}), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    // Page one has a cursor, so there is more.
    expect(result.current.data?.pages[0]?.next_cursor).toBe(browsePageOne.next_cursor)
    expect(result.current.hasNextPage).toBe(true)

    await result.current.fetchNextPage()
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2))

    // Page two's cursor is `null`. `getNextPageParam` must hand TanStack
    // `undefined`, not `null` — the latter is a legitimate page parameter and
    // would re-request page one forever.
    expect(result.current.data?.pages[1]?.next_cursor).toBeNull()
    await waitFor(() => expect(result.current.hasNextPage).toBe(false))
  })

  it('keeps the null on the page, because the UI owes the reader a sentence', async () => {
    const { result } = renderHook(() => useBrowse({}), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.hasNextPage).toBe(true))
    await result.current.fetchNextPage()
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2))

    // patterns.md §4: `next_cursor === null` MUST produce "That is everything
    // we have for this filter." A silent stop is indistinguishable from a bug,
    // so `hasNextPage: false` alone is not enough — the page keeps the fact.
    const last = result.current.data?.pages.at(-1)
    expect(last).toEqual(browsePageTwo)
    expect(last?.next_cursor).toBeNull()
  })

  it('gives no total, no count and no page number anywhere', async () => {
    const { result } = renderHook(() => useBrowse({}), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    const page = result.current.data?.pages[0]
    expect(Object.keys(page ?? {}).sort()).toEqual(['facets', 'items', 'next_cursor'])
  })

  it('stops the same way on the two other keyset routes', async () => {
    const episodes = renderHook(() => useSeasonEpisodes(SEASON_ONE), { wrapper: makeWrapper() })
    await waitFor(() => expect(episodes.result.current.isSuccess).toBe(true))
    expect(episodes.result.current.hasNextPage).toBe(true)
    await episodes.result.current.fetchNextPage()
    await waitFor(() => expect(episodes.result.current.hasNextPage).toBe(false))

    const unmatched = renderHook(() => useUnmatched(), { wrapper: makeWrapper() })
    await waitFor(() => expect(unmatched.result.current.isSuccess).toBe(true))
    expect(unmatched.result.current.hasNextPage).toBe(true)
    await unmatched.result.current.fetchNextPage()
    await waitFor(() => expect(unmatched.result.current.hasNextPage).toBe(false))
  })

  it('changing a filter is a different query key, so cursors are dropped', async () => {
    const { result, rerender } = renderHook(({ genre }: { genre: string | null }) => useBrowse({ genre }), {
      wrapper: makeWrapper(),
      initialProps: { genre: null as string | null },
    })
    await waitFor(() => expect(result.current.hasNextPage).toBe(true))
    await result.current.fetchNextPage()
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2))

    rerender({ genre: 'Drama' })
    // A cursor carries a hash of the query, so a changed filter invalidates it.
    // The accumulated pages must not survive into the new list.
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(1))
  })
})

describe('useSearch', () => {
  it('defaults to fused, which is the measured winner', async () => {
    const { result } = renderHook(() => useSearch('tarkovsky'), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.requested_mode).toBe('fused')
    expect(result.current.data?.mode).toBe('fused')
  })

  it('surfaces a downgrade, where mode !== requested_mode', async () => {
    const { result } = renderHook(() => useSearch('x', 'semantic'), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    // The server ran a different lane from the one asked for. A surface that
    // does not compare the two shows semantic results that are not semantic.
    expect(result.current.data?.requested_mode).toBe('semantic')
    expect(result.current.data?.mode).toBe('full_text')
  })

  it('does not fire on an empty query', () => {
    const { result } = renderHook(() => useSearch('   '), { wrapper: makeWrapper() })
    expect(result.current.fetchStatus).toBe('idle')
  })
})

describe('useTitle and the absent-vs-empty rule', () => {
  it('gets cast, crew and images on an enriched title', async () => {
    const { result } = renderHook(() => useTitle(TITLE_ENRICHED), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.cast).toHaveLength(3)
    expect(result.current.data?.images).toHaveLength(3)
  })

  it('gets them ABSENT — not [] — on a skeleton title', async () => {
    const { result } = renderHook(() => useTitle(TITLE_SKELETON), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    const body = result.current.data
    // Absent means "not applicable to this record"; `[]` means "we looked and
    // there is nothing". Four absent states, four treatments — collapsing this
    // pair into one grey dash is a correctness bug, not a styling preference.
    expect(body).toBeDefined()
    expect('cast' in (body ?? {})).toBe(false)
    expect('crew' in (body ?? {})).toBe(false)
    expect('images' in (body ?? {})).toBe(false)
    // `watch_state: null` is a *present* null on the same body: we looked and
    // there is nothing. Different fact, different treatment.
    expect('watch_state' in (body ?? {})).toBe(true)
    expect(body?.watch_state).toBeNull()
  })

  it('throws a page-scale not_found for a title that is not there', async () => {
    const { result } = renderHook(() => useTitle(TITLE_MISSING), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isError).toBe(true))
    const error = result.current.error
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.knownCode).toBe('not_found')
  })

  it('does not fire without an id', () => {
    const { result } = renderHook(() => useTitle(undefined), { wrapper: makeWrapper() })
    expect(result.current.fetchStatus).toBe('idle')
  })
})

describe('useSimilar', () => {
  it('reads the stored row as home cards', async () => {
    const { result } = renderHook(() => useSimilar(TITLE_ENRICHED), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.source).toBe('row')
    expect(result.current.data?.cards.map((card) => card.name)).toEqual([
      'Solaris',
      'The Mirror',
      'Andrei Rublev',
    ])
    expect(result.current.data?.cards[0]?.artwork).toBe(IMAGE_POSTER)
  })

  it('tells no cards with a build under way from no cards at all', async () => {
    const finding = renderHook(() => useSimilar(TITLE_SIMILAR_FINDING), { wrapper: makeWrapper() })
    await waitFor(() => expect(finding.result.current.isSuccess).toBe(true))
    expect(finding.result.current.data).toEqual({ cards: [], source: 'neighbors', building: true })

    const empty = renderHook(() => useSimilar(TITLE_SIMILAR_EMPTY), { wrapper: makeWrapper() })
    await waitFor(() => expect(empty.result.current.isSuccess).toBe(true))
    expect(empty.result.current.data).toEqual({ cards: [], source: 'neighbors', building: false })
  })
})

/** Advances the fake clock inside act, so the state a timer sets is flushed. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('useSimilar waiting', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /** Fake timers that still let MSW and `waitFor` settle, as Bootstrap.test does. */
  function fakeTimers() {
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
      shouldAdvanceTime: true,
    })
  }

  /** Serves `answer(n)` for the nth request (1-based) and counts them. */
  function serve(answer: (n: number) => SimilarResponse) {
    const counter = { requests: 0 }
    server.use(
      http.get('/titles/:title_id/similar', () => {
        counter.requests += 1
        return HttpResponse.json(answer(counter.requests))
      }),
    )
    return counter
  }

  async function mount() {
    const client = createTestQueryClient()
    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client }, children)
    const view = renderHook(() => useSimilar(TITLE_ENRICHED), { wrapper })
    await waitFor(() => expect(view.result.current.isSuccess).toBe(true))
    return { ...view, client }
  }

  it('exports the two bounds', () => {
    expect(SIMILAR_REREAD_MS).toBe(5_000)
    expect(SIMILAR_WAIT_MS).toBe(60_000)
  })

  it('does not re-read a section with no build under way', async () => {
    fakeTimers()
    const counter = serve(() => similarRow)
    const { result } = await mount()
    await advance(30_000)
    expect(counter.requests).toBe(1)
    expect(result.current.waiting).toBe(false)
  })

  it('re-reads every 5 seconds and swaps when a read says the row is built', async () => {
    fakeTimers()
    const counter = serve((n) => (n <= 2 ? similarUpdating : similarRow))
    const { result } = await mount()
    expect(result.current.waiting).toBe(true)
    await advance(5_000)
    expect(counter.requests).toBe(2)
    await advance(5_000)
    expect(counter.requests).toBe(3)
    expect(result.current.data?.source).toBe('row')
    expect(result.current.waiting).toBe(false)
    await advance(30_000)
    expect(counter.requests).toBe(3)
  })

  it('stops asking and stops waiting at 60 seconds, and does not wait again', async () => {
    fakeTimers()
    const counter = serve(() => similarUpdating)
    const { result, client } = await mount()
    await advance(59_000)
    expect(result.current.waiting).toBe(true)
    await advance(1_000)
    expect(result.current.waiting).toBe(false)
    const asked = counter.requests
    expect(asked).toBeGreaterThanOrEqual(12)
    await advance(60_000)
    expect(counter.requests).toBe(asked)
    expect(result.current.data?.cards.length).toBe(2)

    // A later read that still says building does not start a new wait.
    await act(async () => {
      await client.invalidateQueries({ queryKey: ['similar', TITLE_ENRICHED] })
    })
    expect(counter.requests).toBe(asked + 1)
    expect(result.current.waiting).toBe(false)
    await advance(30_000)
    expect(counter.requests).toBe(asked + 1)
  })
})

describe('useHome', () => {
  it('carries a reason sentence on some rows and null on others', async () => {
    const { result } = renderHook(() => useHome(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    const reasons = result.current.data?.rows.map((r) => r.reason) ?? []
    expect(reasons.some((r) => typeof r === 'string')).toBe(true)
    // A row with no explanation gets none invented for it.
    expect(reasons).toContain(null)
  })
})

describe('useReadiness', () => {
  it('does not retry, because a 503 is information', async () => {
    let calls = 0
    server.use(
      http.get('/health/ready', () => {
        calls += 1
        return HttpResponse.json(readinessDegraded, { status: 503 })
      }),
    )
    const { result } = renderHook(() => useReadiness(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isError).toBe(true))
    // A probe that retries into a degraded subsystem reports the last attempt
    // rather than the current state.
    expect(calls).toBe(1)
  })

  it('reads the degraded document back out of the rejection', async () => {
    server.use(degradedReadiness())
    const { result } = renderHook(() => useReadiness(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isError).toBe(true))

    const degraded = readinessFromError(result.current.error)
    // `/health/ready` is exempt from the RFC 9457 envelope, so the 503 keeps
    // the same shape and reports which check failed — a degraded render rather
    // than a page that disappears.
    expect(degraded).toEqual(readinessDegraded)
  })

  it('answers null when the 503 was not a readiness document at all', () => {
    const problem = new UsherProblem(503, readinessNotADocument)
    expect(readinessFromError(problem)).toBeNull()
  })

  it('answers null for anything that is not an UsherProblem', () => {
    expect(readinessFromError(new Error('boom'))).toBeNull()
    expect(readinessFromError(null)).toBeNull()
    expect(readinessFromError('503')).toBeNull()
  })
})
