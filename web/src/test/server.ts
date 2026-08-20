import { setupServer } from 'msw/node'
import { handlers } from './handlers'

/**
 * One MSW server for the whole suite, so tests exercise the real `client.ts` —
 * its RFC 9457 parsing, its `problem+json` content-type sniff, its status-0
 * transport-failure path — instead of a hand-stubbed `fetch` that agrees with
 * whatever the client happens to do.
 */
export const server = setupServer(...handlers)
