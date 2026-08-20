import '@testing-library/jest-dom/vitest'
import { afterAll, afterEach, beforeAll, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import { server } from './server'

/**
 * `error` is the default so a handler this suite forgot to write fails the test
 * that needed it, rather than silently reaching the network and hanging.
 */
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  cleanup()
})
afterAll(() => server.close())

/**
 * jsdom implements neither, and both are load-bearing in this design:
 * `IntersectionObserver` drives keyset auto-load (patterns.md §4) and
 * `matchMedia` drives `prefers-reduced-motion` (§12).
 */
class NoopIntersectionObserver implements IntersectionObserver {
  readonly root = null
  readonly rootMargin = ''
  // TypeScript 6's lib.dom added this to the interface; without it `implements`
  // fails to compile even though nothing in the suite reads it.
  readonly scrollMargin = ''
  readonly thresholds: ReadonlyArray<number> = []
  disconnect(): void {}
  observe(): void {}
  unobserve(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}
vi.stubGlobal('IntersectionObserver', NoopIntersectionObserver)

vi.stubGlobal(
  'matchMedia',
  vi.fn((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
)

/**
 * jsdom has no `EventSource`. A test that wants live frames uses
 * `src/test/sse.ts`'s controllable fake; everything else gets this inert one so
 * mounting a shell does not throw.
 */
if (!('EventSource' in globalThis)) {
  vi.stubGlobal(
    'EventSource',
    class {
      static readonly CONNECTING = 0
      static readonly OPEN = 1
      static readonly CLOSED = 2
      readyState = 0
      close(): void {}
      addEventListener(): void {}
      removeEventListener(): void {}
    },
  )
}

// `scrollIntoView` is banned by patterns.md §4 but jsdom lacks `scrollTo` too,
// which the rail keyboard handler uses.
Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {})
