/**
 * The request journal every page is verified through.
 *
 * `client.ts` writes one entry per API call and the dev drawer reads them.
 * It is a plain module-level store rather than React state on purpose: the
 * client is called from loaders, hooks and event handlers alike, and none of
 * those has a reliable React context to reach.
 */

import { useSyncExternalStore } from 'react'
import { redact, redactPath } from './redact'

export type LogEntry = {
  id: number
  /** HTTP method, uppercase. */
  method: string
  /**
   * Path as requested, including the query. There is no prefix to strip: the
   * console is same-origin with the API and sends every path exactly as
   * `/openapi.json` declares it (see `client.ts`'s `API_BASE`).
   */
  path: string
  /** The OpenAPI path template this matched, e.g. `/titles/{title_id}`. */
  template: string | null
  status: number
  /** Round-trip milliseconds, rounded. */
  ms: number
  startedAt: number
  /** Parsed JSON when the response was JSON, else a short description. */
  response: unknown
  /**
   * Request body, when there was one. Required rather than optional because
   * `unknown` already carries `undefined` and `exactOptionalPropertyTypes`
   * makes "the key is absent" and "the value is undefined" two different
   * things — a distinction with no meaning for a journal entry.
   */
  request: unknown
  /** True when the response was an RFC 9457 problem document. */
  problem: boolean
  /**
   * The trace id off the response's `traceresponse` header, or `null`.
   *
   * **Per entry, and on successes as well as failures**, which is the whole
   * reason the drawer is worth opening: the id pasted into Tempo is what turns
   * "this call failed" into "here is what it did", and a slow 200 needs it as
   * much as a 503 does. `null` is an honest absence — a transport failure that
   * never reached the server, or a deployment fronted by something that strips
   * the header — and the drawer must render it as *no link* rather than as a
   * dead one.
   *
   * Required rather than optional for the reason `request` above is:
   * `exactOptionalPropertyTypes` makes "the key is absent" a different thing
   * from "the value is null", a distinction with no meaning for a journal
   * entry, and a required field is one every caller has to answer.
   */
  traceId: string | null
}

const MAX_ENTRIES = 300

let entries: LogEntry[] = []
let nextId = 1
const listeners = new Set<() => void>()

/** Literal templates (no `{...}`), checked first. */
let literalTemplates = new Set<string>()
/** Parameterised templates, most-specific first. */
let paramTemplates: { template: string; re: RegExp }[] = []

function emit() {
  for (const l of listeners) l()
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getEntries(): LogEntry[] {
  return entries
}

/**
 * The journal as a React value.
 *
 * `useSyncExternalStore` rather than a context or a `useState` mirror: the
 * store is written from outside React (see the module docstring), and this is
 * the only subscription primitive that is safe against a write landing between
 * render and commit. `getEntries` returns the same array identity until a
 * record replaces it, which is what keeps this from re-rendering the drawer on
 * every unrelated render.
 */
export function useJournal(): LogEntry[] {
  return useSyncExternalStore(subscribe, getEntries, getEntries)
}

/**
 * Teach the log the API's path templates so `/titles/019f.../similar` is
 * recorded as `/titles/{title_id}/similar`. Without this the coverage page
 * would count every distinct id as a distinct operation.
 */
export function setTemplates(paths: string[]) {
  literalTemplates = new Set(paths.filter((p) => !p.includes('{')))

  // **Literals must win over placeholders, and sorting by length does not
  // achieve that.** `/admin/bootstrap/{phase}` is 24 characters and the
  // literal `/admin/bootstrap/status` is 23, so a longest-first ordering
  // tries the placeholder first and `[^/]+` happily matches `status`. The
  // effect is not cosmetic: that request gets journalled under an operation
  // key that does not exist, so its row on the coverage page can never turn
  // green however many times it is called -- a verification tool reporting a
  // permanent false negative about itself.
  //
  // Among *parameterised* templates length is still the right tie-break, so
  // `/titles/{id}/similar` is tried before `/titles/{id}`.
  paramTemplates = paths
    .filter((p) => p.includes('{'))
    .sort((a, b) => b.length - a.length)
    .map((template) => ({
      template,
      // `{...}` matches one segment and never a `/`, so a template cannot
      // swallow a suffix it does not declare. The literal half is escaped
      // first, or a `.` in a path would match any character.
      re: new RegExp(
        '^' +
          template
            .replace(/[.*+?^${}()|[\]\\]/g, (c) => (c === '{' || c === '}' ? c : '\\' + c))
            .replace(/\{[^}]+\}/g, '[^/]+') +
          '$',
      ),
    }))
}

export function matchTemplate(path: string): string | null {
  const clean = path.split('?')[0] ?? path
  if (literalTemplates.has(clean)) return clean
  for (const { template, re } of paramTemplates) {
    if (re.test(clean)) return template
  }
  return null
}

/**
 * Every `METHOD /template` pair this session has sent, which is what the
 * coverage page colours green.
 *
 * **Accumulated separately from `entries`, and that is the whole point.**
 * `entries` is trimmed to MAX_ENTRIES so the drawer stays readable, so
 * deriving coverage from it would make a page you verified an hour ago go
 * red once you browsed enough to push it off the end -- coverage that
 * decreases as you test more is worse than none.
 */
const seenOperations = new Set<string>()

/**
 * **Redaction happens here, at the record boundary, and not only in the
 * client.** A ticket URL that is never journalled cannot be screenshotted out
 * of the drawer, and putting the rule at the point of storage means it holds
 * for every caller rather than for the ones that remembered — the reference
 * client's player called `record` directly, and would have written a live
 * 300-second ticket into the journal on every press of play.
 *
 * `redact` is idempotent, so the client redacting a request body on the way
 * past and this redacting it again costs one pass and changes nothing.
 */
export function record(entry: Omit<LogEntry, 'id'>): LogEntry {
  const full: LogEntry = {
    ...entry,
    id: nextId++,
    path: redactPath(entry.path),
    request: redact(entry.request),
    response: redact(entry.response),
  }
  entries = [full, ...entries].slice(0, MAX_ENTRIES)
  if (full.template) seenOperations.add(`${full.method} ${full.template}`)
  emit()
  return full
}

/** Clears the drawer's visible journal. Coverage deliberately survives it. */
export function clear() {
  entries = []
  emit()
}

export function exercised(): Set<string> {
  return new Set(seenOperations)
}

/**
 * Drops the templates and the accumulated coverage as well as the entries.
 *
 * Only the test suite wants this: a module-level store outlives a test file,
 * and one test's templates leaking into the next is the kind of order
 * dependence that makes a suite pass alone and fail in a run.
 */
export function resetForTests() {
  entries = []
  nextId = 1
  literalTemplates = new Set()
  paramTemplates = []
  seenOperations.clear()
  emit()
}
