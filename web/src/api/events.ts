/**
 * `GET /events` — the only part of this API that is not request/response.
 *
 * **The governing rule, from patterns.md §7: the UI MUST be fully correct if
 * zero events ever arrive.** The bus is in-process and lossy by design — frames
 * are dropped when nobody is listening, on buffer overflow, and on restart —
 * so live updates are delight and never mechanism. Anything that only works
 * because a frame arrived is a bug.
 *
 * That rule is what this module's *shape* is built to enforce, and it is why
 * there is deliberately no buffer, no replay, no "frames missed" counter and no
 * `events[]` array to read. Every one of those is a thing a caller could
 * reasonably mistake for delivery, and the correct client cannot be written
 * against any of them. Frames arrive through a callback, or they do not arrive.
 *
 * Two consequences worth stating rather than discovering:
 *
 * · **A frame whose `data:` is not JSON, or whose JSON is not an object, is
 *   dropped.** This is safe for exactly the reason above — it is
 *   indistinguishable from the frame having been dropped by the bus, which is a
 *   case every surface already has to be correct for.
 *
 * · **Reconnecting loses `Last-Event-ID`.** This module owns its own backoff
 *   (see below), which means closing the `EventSource` and constructing a new
 *   one, and the DOM gives no way to set that header by hand. So a reconnect is
 *   a gap, not a resume. Again: correct by construction, because a gap is what
 *   the bus already promises.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * PRD 07's SSE vocabulary, which is versioned independently of Usher's
 * internal event kinds.
 *
 * **Every frame this API sends carries an `event:` name, so `EventSource`'s
 * `onmessage` never fires for any of them** — that handler only sees frames
 * with no name or the name `message`. Listening per name is not a nicety
 * here; a client that only set `onmessage` would show an open connection and a
 * permanently empty list.
 */
export const EVENT_NAMES = [
  'title.updated',
  'watchstate.updated',
  'row.invalidated',
  'sync.progress',
  'bootstrap.progress',
  'resync_required',
] as const

export type EventName = (typeof EVENT_NAMES)[number]

/**
 * Enrichment landing on an open skeleton. `fields` names what moved, so a
 * surface can patch in place rather than refetch — and patterns.md §7 requires
 * that patch to be opacity only: it must not move, resize or reorder, because
 * moving a row under a pointer that is about to click it is hostile.
 */
export interface TitleUpdated {
  readonly title_id: string
  /** `null` for a movie. An episode frame names both. */
  readonly episode_id: string | null
  readonly fields: readonly string[]
}

/** Fires for other devices too, which is the whole point of pushing it. */
export interface WatchStateUpdated {
  readonly title_id: string
  readonly episode_id: string | null
  readonly position_seconds: number | null
  readonly played: boolean | null
  readonly observed_at: string | null
}

/** Refetch that row only. Never delivered to a `?titles=` subscriber. */
export interface RowInvalidated {
  readonly slug: string
}

/** Operator surfaces only. */
export interface SyncProgress {
  readonly source: string | null
  readonly kind: string | null
  readonly items_seen: number | null
  readonly items_matched: number | null
  readonly items_unmatched: number | null
}

/**
 * Operator surfaces. **No percent, no denominator** — `position` is the resume
 * point and is printed verbatim in mono (patterns.md §8).
 *
 * **This is the whole run, not a cursor, and that is what makes the screen
 * drivable.** Every field is `ImportRunResponse`'s, spelled identically, so a
 * frame patches a status document with no translation. A leaner payload only
 * says *something moved*, and answering each one with
 * `GET /admin/bootstrap/status` — ~0.33 s, uncached, four scans of `titles` —
 * is strictly worse than the poll it replaces.
 *
 * **Two phases because they are two facts.** `requested_phase` is what the
 * operator pressed (`all` for a full run), so it is how a surface tells its own
 * request's frames from somebody else's. `phase` is the **step that owns the
 * dataset** — the six-member vocabulary this screen has a row for — and it is
 * `null` for a dataset the server's map does not hold. They differ on every
 * `--phase all` run.
 *
 * Every field is nullable because the wire is: a frame whose JSON is missing a
 * key, or carries the wrong type for one, is indistinguishable from a frame the
 * bus dropped, and §7 requires the surface to be correct for that anyway.
 */
export interface BootstrapProgress {
  readonly dataset: string | null
  readonly phase: string | null
  readonly requested_phase: string | null
  readonly status: string | null
  readonly revision: string | null
  readonly rows_seen: number | null
  readonly rows_written: number | null
  readonly position: string | number | null
  readonly error: string | null
  readonly started_at: string | null
  readonly heartbeat_at: string | null
  readonly finished_at: string | null
}

/** Discard local state, refetch, and say so in the connection indicator. */
export interface ResyncRequired {
  readonly reason: string | null
}

export interface EventPayloads {
  'title.updated': TitleUpdated
  'watchstate.updated': WatchStateUpdated
  'row.invalidated': RowInvalidated
  'sync.progress': SyncProgress
  'bootstrap.progress': BootstrapProgress
  resync_required: ResyncRequired
}

/**
 * One delivered frame, as a discriminated union on `name` so a consumer's
 * `switch` narrows `payload` without a cast.
 *
 * `id` is the SSE `id:`, which Usher spells `{epoch}-{sequence}`. It is carried
 * for display only — nothing in this module resumes from it, see the header.
 */
export type UsherEvent = {
  [N in EventName]: {
    readonly name: N
    readonly payload: EventPayloads[N]
    readonly id: string
    readonly at: number
  }
}[EventName]

/**
 * patterns.md §7's four states, and **`idle` is not a warning**.
 *
 * A heartbeat comment arrives every 20 s and an SSE comment is a line the
 * client is required to ignore, so no handler fires for it: a healthy stream
 * over a quiet library produces nothing at all. The indicator says "Live ·
 * quiet · nothing has changed since 14:22" and `lastEventAt` is what fills in
 * the time. Modelling that as an error would make the panel cry wolf on every
 * deployment that is simply not busy.
 */
export type ConnectionState = 'connected' | 'idle' | 'reconnecting' | 'off'

/* --------------------------------------------------------------- parsing */

function field(source: object, key: string): unknown {
  return key in source ? Reflect.get(source, key) : undefined
}

function str(source: object, key: string): string | null {
  const value = field(source, key)
  return typeof value === 'string' ? value : null
}

function num(source: object, key: string): number | null {
  const value = field(source, key)
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function bool(source: object, key: string): boolean | null {
  const value = field(source, key)
  return typeof value === 'boolean' ? value : null
}

function strings(source: object, key: string): readonly string[] {
  const value = field(source, key)
  return Array.isArray(value) ? value.filter((v: unknown): v is string => typeof v === 'string') : []
}

/**
 * Narrows a DOM `Event` to the two members an SSE frame carries.
 *
 * `addEventListener` on a custom name is typed as a plain `Event` by lib.dom
 * — the typed overload covers only `open`, `message` and `error` — so this is
 * the narrowing the DOM types cannot express, done as an `instanceof` guard
 * rather than a cast.
 */
function frameOf(event: Event): { data: string; id: string } | null {
  if (!(event instanceof MessageEvent)) return null
  const { data, lastEventId } = event
  if (typeof data !== 'string') return null
  return { data, id: typeof lastEventId === 'string' ? lastEventId : '' }
}

/**
 * Builds one union member per arm, which is what lets a consumer's `switch`
 * narrow `payload` off `name` without a cast anywhere in the chain.
 *
 * **Every field except the identifying one is read leniently and defaults to
 * `null`.** A parser that dropped a frame for a missing field would turn "the
 * server added a key" into "live updates silently stopped", which is the
 * failure mode hardest to notice and hardest to attribute. A frame is dropped
 * only when it carries no identity at all — a `title.updated` with no
 * `title_id` names nothing and there is no surface to patch — or when its
 * `data:` is not a JSON object.
 */
export function parseFrame(name: EventName, event: Event): UsherEvent | null {
  const frame = frameOf(event)
  if (frame === null) return null

  let decoded: unknown
  try {
    decoded = JSON.parse(frame.data)
  } catch {
    // A frame whose `data:` is not JSON is dropped exactly as a frame the bus
    // dropped would be, which every surface is already required to be correct
    // for. Usher always sends JSON here.
    return null
  }
  if (decoded === null || typeof decoded !== 'object' || Array.isArray(decoded)) return null

  const data: object = decoded
  const id = frame.id
  const at = Date.now()

  switch (name) {
    case 'title.updated': {
      const titleId = str(data, 'title_id')
      if (titleId === null) return null
      return {
        name,
        id,
        at,
        payload: {
          title_id: titleId,
          episode_id: str(data, 'episode_id'),
          fields: strings(data, 'fields'),
        },
      }
    }
    case 'watchstate.updated': {
      const titleId = str(data, 'title_id')
      if (titleId === null) return null
      return {
        name,
        id,
        at,
        payload: {
          title_id: titleId,
          episode_id: str(data, 'episode_id'),
          position_seconds: num(data, 'position_seconds'),
          played: bool(data, 'played'),
          observed_at: str(data, 'observed_at'),
        },
      }
    }
    case 'row.invalidated': {
      const slug = str(data, 'slug')
      return slug === null ? null : { name, id, at, payload: { slug } }
    }
    case 'sync.progress':
      return {
        name,
        id,
        at,
        payload: {
          source: str(data, 'source'),
          kind: str(data, 'kind'),
          items_seen: num(data, 'items_seen'),
          items_matched: num(data, 'items_matched'),
          items_unmatched: num(data, 'items_unmatched'),
        },
      }
    case 'bootstrap.progress':
      return {
        name,
        id,
        at,
        payload: {
          dataset: str(data, 'dataset'),
          phase: str(data, 'phase'),
          requested_phase: str(data, 'requested_phase'),
          status: str(data, 'status'),
          revision: str(data, 'revision'),
          rows_seen: num(data, 'rows_seen'),
          rows_written: num(data, 'rows_written'),
          position: str(data, 'position') ?? num(data, 'position'),
          error: str(data, 'error'),
          started_at: str(data, 'started_at'),
          heartbeat_at: str(data, 'heartbeat_at'),
          finished_at: str(data, 'finished_at'),
        },
      }
    case 'resync_required':
      return { name, id, at, payload: { reason: str(data, 'reason') } }
  }
}

/* ---------------------------------------------------------- the connection */

/**
 * Reconnect backoff. The first retry is nearly immediate because the common
 * case is a proxy recycling a connection; the cap is 30 s because a console
 * left open overnight against a restarting backend should not be hammering it,
 * and should still be connected by the time somebody looks at it.
 *
 * This module owns the backoff rather than leaving it to `EventSource`, whose
 * built-in retry is a fixed interval and — more importantly — does not happen
 * at all when the connection *fails* rather than drops: a non-200 or a wrong
 * content type puts `readyState` at `CLOSED` and the browser gives up. Both
 * halves have to be handled and only one of them is automatic.
 */
const BACKOFF_MS = [500, 1_000, 2_000, 5_000, 10_000, 30_000] as const

/**
 * How long a connected-but-silent stream waits before it calls itself `idle`.
 *
 * One heartbeat interval. The server sends `: keepalive` every 20 s and an SSE
 * comment fires no handler, so 20 s of silence is the shortest window that
 * means anything at all, and it means "quiet" rather than "broken".
 */
const IDLE_AFTER_MS = 20_000

export interface EventStreamOptions {
  /**
   * Restrict the stream to these title ids (`?titles=`). A `row.invalidated`
   * frame is never delivered to a filtered subscriber.
   */
  titles?: readonly string[]
  /** Called once per delivered frame. There is no other delivery channel. */
  onEvent?: (event: UsherEvent) => void
  onStateChange?: (state: ConnectionState, lastEventAt: number | null) => void
  /** Overridable for tests. Defaults to one heartbeat interval. */
  idleAfterMs?: number
}

export interface EventStream {
  /** Idempotent. Cancels any pending reconnect and removes every listener. */
  close(): void
  readonly state: ConnectionState
  readonly lastEventAt: number | null
}

export function eventStreamUrl(titles?: readonly string[]): string {
  const filter = (titles ?? []).filter((t) => t.trim() !== '')
  return filter.length > 0 ? `/events?titles=${encodeURIComponent(filter.join(','))}` : '/events'
}

/**
 * Opens the stream and keeps it open. Framework-free so it can be tested
 * without a renderer; `useEventStream` below is a thin wrapper.
 */
export function openEventStream(options: EventStreamOptions = {}): EventStream {
  const idleAfter = options.idleAfterMs ?? IDLE_AFTER_MS
  const url = eventStreamUrl(options.titles)

  let source: EventSource | null = null
  let attempt = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let idleTimer: ReturnType<typeof setTimeout> | null = null
  let closed = false
  const listeners: [string, (event: Event) => void][] = []

  const stream = {
    state: 'reconnecting' as ConnectionState,
    lastEventAt: null as number | null,
    close,
  }

  function setState(next: ConnectionState) {
    if (stream.state === next) return
    stream.state = next
    options.onStateChange?.(next, stream.lastEventAt)
  }

  function armIdle() {
    if (idleTimer !== null) clearTimeout(idleTimer)
    idleTimer = setTimeout(() => {
      // Only from `connected`: a stream that is reconnecting is not quiet, it
      // is absent, and the two must not share an indicator.
      if (stream.state === 'connected') setState('idle')
    }, idleAfter)
  }

  function teardown() {
    if (source !== null) {
      for (const [name, handler] of listeners) source.removeEventListener(name, handler)
      source.onopen = null
      source.onerror = null
      source.close()
      source = null
    }
    listeners.length = 0
    if (idleTimer !== null) {
      clearTimeout(idleTimer)
      idleTimer = null
    }
  }

  function scheduleReconnect() {
    teardown()
    if (closed) return
    setState('reconnecting')
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] ?? 30_000
    attempt += 1
    reconnectTimer = setTimeout(connect, delay)
  }

  function connect() {
    reconnectTimer = null
    if (closed) return

    const es = new EventSource(url)
    source = es

    for (const name of EVENT_NAMES) {
      const handler = (event: Event) => {
        const parsed = parseFrame(name, event)
        if (parsed === null) return
        stream.lastEventAt = parsed.at
        setState('connected')
        armIdle()
        options.onEvent?.(parsed)
      }
      es.addEventListener(name, handler)
      listeners.push([name, handler])
    }

    es.onopen = () => {
      attempt = 0
      setState('connected')
      armIdle()
    }

    es.onerror = () => {
      // Both arms end up here and both are handled the same way, because the
      // difference the reference client painted separately — `CLOSED` means the
      // browser gave up, `CONNECTING` means it is retrying — is a difference
      // this module erases by owning the retry itself. What an operator needs
      // to see is "not receiving", and that is one state.
      scheduleReconnect()
    }
  }

  function close() {
    if (closed) return
    closed = true
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    teardown()
    setState('off')
  }

  connect()
  return stream
}

/* ------------------------------------------------------------------- hook */

export interface UseEventStreamOptions extends EventStreamOptions {
  /** `false` puts the indicator in `off` and opens nothing. */
  enabled?: boolean
}

export interface EventStreamStatus {
  readonly state: ConnectionState
  /**
   * When the last frame arrived, or `null` if none has this connection. `null`
   * is not a failure — see `ConnectionState` — it is what a healthy stream
   * over a quiet library looks like, and the indicator says so.
   */
  readonly lastEventAt: number | null
  /** Tears the connection down and opens a new one immediately. */
  reconnect: () => void
}

/**
 * Subscribes for the lifetime of the component and cleans up completely on
 * unmount: every named listener is removed, the pending reconnect timer is
 * cancelled, and the `EventSource` is closed. A stream left open across a route
 * change is a socket per navigation, and Usher's bus drops frames on overflow.
 */
export function useEventStream(options: UseEventStreamOptions = {}): EventStreamStatus {
  const { enabled = true, titles, onEvent, onStateChange, idleAfterMs } = options

  const filter = (titles ?? []).join(',')
  const [generation, setGeneration] = useState(0)

  /**
   * Which connection the mirrored state below belongs to.
   *
   * Everything that forces a new socket is in this string, so a stale reading
   * is *discarded during render* rather than corrected by a second `setState`
   * inside the effect. Without it, bumping `generation` while `connected` would
   * leave the indicator claiming a live connection until the replacement socket
   * opened or failed.
   */
  const epoch = `${String(enabled)}|${filter}|${String(idleAfterMs)}|${generation}`

  const [mirror, setMirror] = useState<{
    epoch: string
    state: ConnectionState
    lastEventAt: number | null
  }>({ epoch, state: 'reconnecting', lastEventAt: null })

  const current =
    mirror.epoch === epoch ? mirror : { epoch, state: 'reconnecting' as ConnectionState, lastEventAt: null }

  // Held in refs so a caller passing an inline arrow — which is every caller —
  // does not tear the socket down and rebuild it on each render. Written in an
  // effect rather than during render, because a ref is not a render input.
  const onEventRef = useRef(onEvent)
  const onStateChangeRef = useRef(onStateChange)
  useEffect(() => {
    onEventRef.current = onEvent
    onStateChangeRef.current = onStateChange
  })

  useEffect(() => {
    if (!enabled) return

    // Guards every write below. Without it the `off` that `close()` announces
    // during cleanup would land on the *next* connection's mirror.
    let live = true

    const stream = openEventStream({
      ...(filter === '' ? {} : { titles: filter.split(',') }),
      ...(idleAfterMs === undefined ? {} : { idleAfterMs }),
      onEvent: (event) => {
        if (!live) return
        setMirror((previous) => ({ ...previous, epoch, lastEventAt: event.at }))
        onEventRef.current?.(event)
      },
      onStateChange: (next, at) => {
        if (!live) return
        setMirror((previous) => ({ ...previous, epoch, state: next }))
        onStateChangeRef.current?.(next, at)
      },
    })

    return () => {
      live = false
      stream.close()
    }
  }, [enabled, filter, idleAfterMs, epoch])

  const reconnect = useCallback(() => {
    setGeneration((g) => g + 1)
  }, [])

  return {
    state: enabled ? current.state : 'off',
    lastEventAt: current.lastEventAt,
    reconnect,
  }
}
