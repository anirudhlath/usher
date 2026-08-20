/**
 * The SSE client.
 *
 * The two assertions that carry weight here are the ones a plausible
 * implementation gets wrong:
 *
 * · **Named dispatch.** Every frame this API sends carries an `event:` name, so
 *   `onmessage` never fires. A client that only set `onmessage` shows an open
 *   connection and a permanently empty list — a failure that looks like "the
 *   server is quiet", which is also what a *healthy* stream looks like.
 * · **Idle is healthy.** A heartbeat comment arrives every 20 s and fires no
 *   handler, so a connected stream over a quiet library produces nothing.
 *   `idle` must be its own state and must not be modelled as an error.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import {
  EVENT_NAMES,
  eventStreamUrl,
  openEventStream,
  useEventStream,
  type ConnectionState,
  type UsherEvent,
} from './events'
import { FakeEventSource, installFakeEventSource } from '@/test/sse'
import { TITLE_ENRICHED, TITLE_SERIES, EPISODE_PILOT } from '@/test/fixtures/ids'

let sse: ReturnType<typeof installFakeEventSource>

beforeEach(() => {
  vi.useFakeTimers()
  sse = installFakeEventSource()
})

afterEach(() => {
  sse.restore()
  vi.useRealTimers()
})

describe('the URL', () => {
  it('is the bare route with no filter', () => {
    expect(eventStreamUrl()).toBe('/events')
    expect(eventStreamUrl([])).toBe('/events')
    expect(eventStreamUrl([' '])).toBe('/events')
  })

  it('carries ?titles= when one is given', () => {
    expect(eventStreamUrl([TITLE_ENRICHED, TITLE_SERIES])).toBe(
      `/events?titles=${encodeURIComponent(`${TITLE_ENRICHED},${TITLE_SERIES}`)}`,
    )
  })
})

describe('named-event dispatch', () => {
  it('registers a listener for every one of the six names', () => {
    const stream = openEventStream()
    const source = sse.latest()
    // Six, not one. `onmessage` is not among them and is deliberately unused:
    // a frame with an `event:` name never reaches it.
    expect(source.listenerCount()).toBe(EVENT_NAMES.length)
    expect(source.onmessage).toBeNull()
    stream.close()
  })

  it('delivers a title.updated frame with its payload typed', () => {
    const received: UsherEvent[] = []
    const stream = openEventStream({ onEvent: (e) => received.push(e) })
    const source = sse.latest()
    source.open()

    source.emit('title.updated', {
      title_id: TITLE_ENRICHED,
      episode_id: null,
      fields: ['overview', 'images'],
    })

    expect(received).toHaveLength(1)
    const event = received[0]
    if (event?.name !== 'title.updated') throw new Error('expected title.updated')
    expect(event.payload.title_id).toBe(TITLE_ENRICHED)
    expect(event.payload.episode_id).toBeNull()
    expect(event.payload.fields).toEqual(['overview', 'images'])
    expect(event.id).toBe('1755640000-1')
    stream.close()
  })

  it('delivers all six names and never confuses one for another', () => {
    const received: UsherEvent[] = []
    const stream = openEventStream({ onEvent: (e) => received.push(e) })
    const source = sse.latest()
    source.open()

    source.emit('title.updated', { title_id: TITLE_ENRICHED, fields: [] })
    source.emit('watchstate.updated', {
      title_id: TITLE_SERIES,
      episode_id: EPISODE_PILOT,
      position_seconds: 812,
      played: false,
      observed_at: '2026-08-18T21:14:02Z',
    })
    source.emit('row.invalidated', { slug: 'continue-watching' })
    source.emit('sync.progress', {
      source: 'Living Room Emby',
      kind: 'delta',
      items_seen: 412,
      items_matched: 400,
      items_unmatched: 12,
    })
    source.emit('bootstrap.progress', {
      dataset: 'imdb',
      phase: 'imdb',
      rows_seen: 418_002,
      rows_written: 411_774,
      position: 418_002,
    })
    source.emit('resync_required', { reason: 'buffer_overflow' })

    expect(received.map((e) => e.name)).toEqual([...EVENT_NAMES])
    stream.close()
  })

  it('narrows the payload off the name without a cast', () => {
    const received: UsherEvent[] = []
    const stream = openEventStream({ onEvent: (e) => received.push(e) })
    sse.latest().open()
    sse.latest().emit('bootstrap.progress', {
      dataset: 'imdb',
      phase: 'imdb',
      rows_seen: 10,
      rows_written: 9,
      position: 'tt0079944',
    })

    const event = received[0]
    if (event?.name !== 'bootstrap.progress') throw new Error('expected bootstrap.progress')
    // `position` is the resume point and is printed verbatim — it is a cursor,
    // not a percentage, and it is a string as often as a number.
    expect(event.payload.position).toBe('tt0079944')
    expect(event.payload.rows_seen).toBe(10)
    stream.close()
  })

  it('reads a field it was not given as null rather than dropping the frame', () => {
    const received: UsherEvent[] = []
    const stream = openEventStream({ onEvent: (e) => received.push(e) })
    sse.latest().open()
    // A server that adds or renames a non-identifying key must not silently
    // stop live updates — that is the failure hardest to notice.
    sse.latest().emit('sync.progress', { source: 'Living Room Emby' })

    const event = received[0]
    if (event?.name !== 'sync.progress') throw new Error('expected sync.progress')
    expect(event.payload.source).toBe('Living Room Emby')
    expect(event.payload.items_seen).toBeNull()
    stream.close()
  })

  it('drops a frame carrying no identity at all', () => {
    const received: UsherEvent[] = []
    const stream = openEventStream({ onEvent: (e) => received.push(e) })
    sse.latest().open()
    sse.latest().emit('title.updated', { fields: ['overview'] })
    sse.latest().emit('row.invalidated', {})
    expect(received).toHaveLength(0)
    stream.close()
  })

  it('drops a frame whose data is not JSON, exactly as the bus would have', () => {
    const received: UsherEvent[] = []
    const stream = openEventStream({ onEvent: (e) => received.push(e) })
    sse.latest().open()
    sse.latest().emit('row.invalidated', 'not json at all')
    sse.latest().emit('row.invalidated', '[1,2,3]')
    expect(received).toHaveLength(0)
    stream.close()
  })
})

describe('connection state', () => {
  function track() {
    const states: ConnectionState[] = []
    const stream = openEventStream({
      onStateChange: (s) => states.push(s),
      idleAfterMs: 20_000,
    })
    return { states, stream }
  }

  it('starts as reconnecting and becomes connected when the socket opens', () => {
    const { states, stream } = track()
    expect(stream.state).toBe('reconnecting')
    sse.latest().open()
    expect(states).toEqual(['connected'])
    stream.close()
  })

  /**
   * The load-bearing one. Twenty seconds of silence on an open connection is
   * what a healthy stream over a quiet library looks like, because the 20 s
   * heartbeat is an SSE *comment* and fires no handler. Drawing that as a
   * warning makes the indicator cry wolf on every deployment that is simply not
   * busy.
   */
  it('goes idle — not to an error — after a heartbeat interval of silence', () => {
    const { states, stream } = track()
    sse.latest().open()
    vi.advanceTimersByTime(20_001)

    expect(stream.state).toBe('idle')
    expect(states).toEqual(['connected', 'idle'])
    expect(states).not.toContain('reconnecting')
    expect(states).not.toContain('off')
    stream.close()
  })

  it('comes back out of idle when a frame arrives, and remembers when', () => {
    const { states, stream } = track()
    sse.latest().open()
    vi.advanceTimersByTime(20_001)
    expect(stream.state).toBe('idle')

    sse.latest().emit('row.invalidated', { slug: 'recently-added' })
    expect(stream.state).toBe('connected')
    // `lastEventAt` is what lets the indicator say "quiet since 14:22" instead
    // of implying something is wrong.
    expect(stream.lastEventAt).not.toBeNull()
    expect(states).toEqual(['connected', 'idle', 'connected'])
    stream.close()
  })

  it('never reports idle from a reconnecting state', () => {
    const { states, stream } = track()
    sse.latest().open()
    sse.latest().fail()
    // Twenty-five seconds of not-receiving, which is longer than the idle
    // window. A stream that is reconnecting is *absent*, not quiet, and the two
    // must not share an indicator — so the idle timer must have been cancelled
    // along with the socket.
    vi.advanceTimersByTime(25_000)
    expect(stream.state).toBe('reconnecting')
    expect(states).toEqual(['connected', 'reconnecting'])

    sse.latest().open()
    expect(states).toEqual(['connected', 'reconnecting', 'connected'])
    stream.close()
  })

  it('is off once closed, and stays off', () => {
    const { states, stream } = track()
    sse.latest().open()
    stream.close()
    expect(stream.state).toBe('off')
    vi.advanceTimersByTime(120_000)
    expect(states.at(-1)).toBe('off')
    stream.close()
    expect(states.filter((s) => s === 'off')).toHaveLength(1)
  })
})

describe('reconnect', () => {
  it('backs off exponentially rather than hammering a restarting backend', () => {
    const stream = openEventStream()
    expect(sse.instances).toHaveLength(1)

    sse.latest().open()
    sse.latest().fail()

    // 500 ms, then 1 s, then 2 s. Nothing before each delay elapses.
    vi.advanceTimersByTime(499)
    expect(sse.instances).toHaveLength(1)
    vi.advanceTimersByTime(2)
    expect(sse.instances).toHaveLength(2)

    sse.latest().fail()
    vi.advanceTimersByTime(999)
    expect(sse.instances).toHaveLength(2)
    vi.advanceTimersByTime(2)
    expect(sse.instances).toHaveLength(3)

    sse.latest().fail()
    vi.advanceTimersByTime(1_999)
    expect(sse.instances).toHaveLength(3)
    vi.advanceTimersByTime(2)
    expect(sse.instances).toHaveLength(4)

    stream.close()
  })

  it('resets the backoff once a connection opens', () => {
    const stream = openEventStream()
    sse.latest().fail()
    vi.advanceTimersByTime(500)
    sse.latest().fail()
    vi.advanceTimersByTime(1_000)
    expect(sse.instances).toHaveLength(3)

    sse.latest().open()
    sse.latest().fail()
    // Back to the first rung, because the connection was good in between.
    vi.advanceTimersByTime(500)
    expect(sse.instances).toHaveLength(4)
    stream.close()
  })

  it('retries a refused connection, which EventSource on its own does not', () => {
    // A non-200 or a wrong content type puts `readyState` at CLOSED and the
    // browser gives up. Owning the retry is the only way the console recovers
    // from a backend that was still starting.
    const stream = openEventStream()
    sse.latest().fail(FakeEventSource.CLOSED)
    vi.advanceTimersByTime(500)
    expect(sse.instances).toHaveLength(2)
    stream.close()
  })
})

describe('cleanup', () => {
  it('removes every listener and closes the socket', () => {
    const stream = openEventStream()
    const source = sse.latest()
    source.open()
    expect(source.listenerCount()).toBe(EVENT_NAMES.length)

    stream.close()

    expect(source.listenerCount()).toBe(0)
    expect(source.readyState).toBe(FakeEventSource.CLOSED)
    expect(source.onopen).toBeNull()
    expect(source.onerror).toBeNull()
  })

  it('cancels a pending reconnect, so a closed stream opens nothing later', () => {
    const stream = openEventStream()
    sse.latest().fail()
    stream.close()
    vi.advanceTimersByTime(120_000)
    // A stream left reconnecting across a route change is a socket per
    // navigation, and Usher's bus drops frames on overflow.
    expect(sse.instances).toHaveLength(1)
  })

  it('delivers nothing after close, even if a frame is pushed at the old socket', () => {
    const received: UsherEvent[] = []
    const stream = openEventStream({ onEvent: (e) => received.push(e) })
    const source = sse.latest()
    source.open()
    stream.close()
    source.emit('row.invalidated', { slug: 'continue-watching' })
    expect(received).toHaveLength(0)
  })

  it('is idempotent', () => {
    const stream = openEventStream()
    stream.close()
    expect(() => {
      stream.close()
    }).not.toThrow()
  })
})

describe('useEventStream', () => {
  it('opens on mount and tears everything down on unmount', () => {
    const { unmount } = renderHook(() => useEventStream())
    const source = sse.latest()
    act(() => {
      source.open()
    })
    expect(source.listenerCount()).toBe(EVENT_NAMES.length)

    unmount()

    // A stream left open across a route change is a socket per navigation.
    expect(source.listenerCount()).toBe(0)
    expect(source.readyState).toBe(FakeEventSource.CLOSED)
  })

  it('mirrors the connection state, including idle', () => {
    const { result } = renderHook(() => useEventStream({ idleAfterMs: 20_000 }))
    expect(result.current.state).toBe('reconnecting')

    act(() => {
      sse.latest().open()
    })
    expect(result.current.state).toBe('connected')
    expect(result.current.lastEventAt).toBeNull()

    act(() => {
      vi.advanceTimersByTime(20_001)
    })
    // Quiet, and healthy. `lastEventAt` stays `null` because nothing has ever
    // arrived — which is what a stream over a library nobody is touching does.
    expect(result.current.state).toBe('idle')
    expect(result.current.lastEventAt).toBeNull()
  })

  it('records lastEventAt when a frame lands', () => {
    const { result } = renderHook(() => useEventStream())
    act(() => {
      sse.latest().open()
      sse.latest().emit('row.invalidated', { slug: 'recently-added' })
    })
    expect(result.current.state).toBe('connected')
    expect(result.current.lastEventAt).not.toBeNull()
  })

  it('opens nothing at all when disabled', () => {
    const { result } = renderHook(() => useEventStream({ enabled: false }))
    expect(result.current.state).toBe('off')
    expect(sse.instances).toHaveLength(0)
  })

  it('does not rebuild the socket when the callback identity changes', () => {
    const { rerender } = renderHook(({ tag }: { tag: string }) => useEventStream({ onEvent: () => tag }), {
      initialProps: { tag: 'a' },
    })
    expect(sse.instances).toHaveLength(1)
    rerender({ tag: 'b' })
    // Every caller passes an inline arrow; a socket per render would be a
    // reconnect storm on a page that re-renders for any other reason.
    expect(sse.instances).toHaveLength(1)
  })

  it('reconnect() replaces the socket and resets the indicator', () => {
    const { result } = renderHook(() => useEventStream())
    act(() => {
      sse.latest().open()
    })
    expect(result.current.state).toBe('connected')

    act(() => {
      result.current.reconnect()
    })
    expect(sse.instances).toHaveLength(2)
    // The old reading belongs to the old socket and is discarded during render
    // rather than left claiming a live connection.
    expect(result.current.state).toBe('reconnecting')
  })
})

describe('what the module deliberately does not expose', () => {
  it('offers no buffer, replay or delivery count a caller could rely on', () => {
    const stream = openEventStream()
    // The bus is in-process and lossy by design: frames are dropped when
    // nobody is listening, on overflow, and on restart. Anything here that
    // looked like a delivery guarantee would invite a screen that only works
    // when a frame arrives — which patterns.md §7 forbids outright.
    expect(Object.keys(stream).sort()).toEqual(['close', 'lastEventAt', 'state'])
    stream.close()
  })
})
