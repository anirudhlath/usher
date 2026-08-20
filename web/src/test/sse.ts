/**
 * A controllable `EventSource`, because jsdom has none.
 *
 * `setup.ts` stubs an inert one so mounting a shell does not throw; this is the
 * one a test reaches for when it wants to *drive* the stream — open it, push a
 * named frame down it, fail it and watch the reconnect.
 *
 * It dispatches real `MessageEvent` objects rather than plain records, which
 * matters: `events.ts` narrows a listener's `Event` with `instanceof
 * MessageEvent` rather than a cast, so a fake that dispatched a duck-typed
 * object would make every frame vanish here and nowhere else — a test harness
 * that disagrees with production about the one thing the module is for.
 */

import { vi } from 'vitest'

export class FakeEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2

  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSED = 2

  /** Every instance constructed since the last `install`, in order. */
  static instances: FakeEventSource[] = []

  readonly url: string
  readonly withCredentials = false
  readyState = 0

  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null

  private readonly handlers = new Map<string, Set<(event: Event) => void>>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: Event) => void): void {
    const set = this.handlers.get(type) ?? new Set()
    set.add(listener)
    this.handlers.set(type, set)
  }

  removeEventListener(type: string, listener: (event: Event) => void): void {
    this.handlers.get(type)?.delete(listener)
  }

  dispatchEvent(event: Event): boolean {
    for (const listener of this.handlers.get(event.type) ?? []) listener(event)
    return true
  }

  close(): void {
    this.readyState = 2
  }

  /* ------------------------------------------------------ test controls */

  /** The server accepted the connection. Fires `onopen`. */
  open(): void {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  /**
   * One named frame. **Named**, always: every frame Usher sends carries an
   * `event:` name, so a fake with only an `emit()` that went through
   * `onmessage` would test a path production never takes.
   */
  emit(name: string, data: unknown, id = '1755640000-1'): void {
    this.dispatchEvent(
      new MessageEvent(name, {
        data: typeof data === 'string' ? data : JSON.stringify(data),
        lastEventId: id,
      }),
    )
  }

  /** The connection dropped or was refused. Fires `onerror`. */
  fail(state: 0 | 2 = 2): void {
    this.readyState = state
    this.onerror?.(new Event('error'))
  }

  /** How many listeners are attached, which is what a cleanup test asserts on. */
  listenerCount(): number {
    let total = 0
    for (const set of this.handlers.values()) total += set.size
    return total
  }
}

/**
 * Installs the fake for the duration of a test and hands back the list every
 * constructed instance lands in. Call `restore()` in a `finally` or an
 * `afterEach`; `vi.unstubAllGlobals()` does the same job.
 */
export function installFakeEventSource(): {
  instances: FakeEventSource[]
  latest: () => FakeEventSource
  restore: () => void
} {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  return {
    instances: FakeEventSource.instances,
    latest: () => {
      const last = FakeEventSource.instances.at(-1)
      if (last === undefined) throw new Error('no EventSource was constructed')
      return last
    },
    restore: () => {
      vi.unstubAllGlobals()
      FakeEventSource.instances = []
    },
  }
}
