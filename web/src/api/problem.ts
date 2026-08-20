/**
 * Usher's error model, as something a component can switch on.
 *
 * The API's failure set is **closed at seven codes** (ADR-0030), and that is
 * the fact the whole `Problem` component rests on: recovery is a *lookup*, not
 * a judgement, and never an inference from `detail`. `detail` is prose the
 * server may reword at any release — patterns.md §3 requires it shown verbatim
 * and forbids parsing it — so everything a UI decides is decided from `code`.
 *
 * Nothing in this file imports the transport. `client.ts` depends on it, not
 * the other way round, so the seven-way table is reachable from a component
 * test that never opens a socket.
 */

import type { components } from './schema'

/**
 * Usher's `code` vocabulary. Closed at seven members by ADR-0030, and read off
 * the generated schema rather than restated — an eighth member added to the API
 * becomes a compile error in `RECOVERY` below instead of an unhandled case at
 * runtime.
 */
export type ProblemCode = components['schemas']['ProblemCode']

/** The wire shape of an RFC 9457 document as Usher sends it. */
export type ProblemDocument = components['schemas']['ProblemResponse']

/**
 * How much of the screen a failure owns (patterns.md §3).
 *
 * `none` is `invalid_cursor` and is not a degenerate case: that code is
 * **never rendered**. A cursor encodes a hash of the query, so an invalidated
 * one means the filters moved under an in-flight page — the list restarts from
 * the top and the user is told nothing, because nothing went wrong for them.
 */
export type ProblemScale = 'page' | 'field' | 'panel' | 'inline' | 'none'

/**
 * What the user can do about it. A discriminated union rather than a string so
 * that a surface offering the wrong affordance — a retry button on
 * `not_playable`, which will fail identically every time — cannot typecheck.
 */
export type ProblemRecovery =
  /** Back, plus search. Explicitly **no retry**: the row does not exist. */
  | { readonly kind: 'back-and-search' }
  /** Per-field, driven from `errors[].loc` and `errors[].msg`. */
  | { readonly kind: 'fix-field' }
  /** None. A developer error; show it plainly and offer nothing. */
  | { readonly kind: 'none' }
  /** Silently discard accumulated items and re-request from the top. */
  | { readonly kind: 'restart-list' }
  /** Retry, honouring `Retry-After`, disabled while a retry is in flight. */
  | { readonly kind: 'retry'; readonly honourRetryAfter: true }
  /** No retry button. The copy patterns.md §3 fixes for this case. */
  | { readonly kind: 'other-copies'; readonly label: 'See other copies' }
  /** One tap re-requests the ticket and plays. A 300 s ticket simply expired. */
  | { readonly kind: 're-request' }

export interface RecoveryEntry {
  /** The HTTP status this code is answered with. */
  readonly status: number
  readonly scale: ProblemScale
  /**
   * False only for `invalid_cursor`. A surface that renders it has a bug in its
   * cursor handling, not a message to show.
   */
  readonly rendered: boolean
  /**
   * Whether repeating the identical request could plausibly succeed. Two codes
   * are 404 and only one of them is retryable, which is the reason this is a
   * field and not a function of `status`.
   */
  readonly retryable: boolean
  readonly recovery: ProblemRecovery
}

/**
 * patterns.md §3's table, transcribed.
 *
 * The mapped type over `ProblemCode` is the load-bearing part: this object is
 * required to have exactly the members the generated schema declares, so the
 * table cannot fall behind the API and a component switching over it can be
 * exhaustive without a `default` arm that quietly swallows a new code.
 */
export const RECOVERY: { readonly [C in ProblemCode]: RecoveryEntry } = {
  not_found: {
    status: 404,
    scale: 'page',
    rendered: true,
    retryable: false,
    recovery: { kind: 'back-and-search' },
  },
  validation_failed: {
    status: 422,
    scale: 'field',
    rendered: true,
    retryable: false,
    recovery: { kind: 'fix-field' },
  },
  method_not_allowed: {
    status: 405,
    scale: 'panel',
    rendered: true,
    retryable: false,
    recovery: { kind: 'none' },
  },
  invalid_cursor: {
    status: 400,
    scale: 'none',
    rendered: false,
    retryable: false,
    recovery: { kind: 'restart-list' },
  },
  source_unavailable: {
    status: 503,
    scale: 'panel',
    rendered: true,
    retryable: true,
    recovery: { kind: 'retry', honourRetryAfter: true },
  },
  not_playable: {
    status: 409,
    scale: 'panel',
    rendered: true,
    retryable: false,
    recovery: { kind: 'other-copies', label: 'See other copies' },
  },
  ticket_invalid: {
    status: 404,
    scale: 'inline',
    rendered: true,
    retryable: true,
    recovery: { kind: 're-request' },
  },
}

const PROBLEM_CODES = Object.keys(RECOVERY)

/**
 * Whether a wire value is one of the seven.
 *
 * The wire is allowed to carry something else — a proxy's own error page, a
 * future member of a vocabulary this build predates — and that has to be
 * *representable* rather than crashed on. `UsherProblem.code` keeps the raw
 * string for display; only `knownCode` narrows, and a surface with no entry in
 * the table falls back to showing `code`, `status` and `detail`, which
 * patterns.md §3 requires of every rendered problem anyway.
 */
export function isProblemCode(value: unknown): value is ProblemCode {
  return typeof value === 'string' && PROBLEM_CODES.includes(value)
}

/** The row of the table for a code, or `null` when the wire said something else. */
export function recoveryFor(code: string): RecoveryEntry | null {
  return isProblemCode(code) ? RECOVERY[code] : null
}

/**
 * `Retry-After`, in seconds, from either spelling RFC 9110 permits.
 *
 * `source_unavailable` is the one code whose recovery is a retry, and
 * patterns.md §3 requires that retry to honour the header. A server may send
 * delta-seconds (`120`) or an HTTP-date (`Wed, 19 Aug 2026 12:00:00 GMT`); the
 * date form is converted against `now` so a caller only ever deals in seconds.
 *
 * A header that is absent, unparseable, or in the past yields `null` rather
 * than `0`: "wait no time" and "the server did not say" are different, and a
 * countdown rendered from a fabricated zero is the kind of invented number
 * patterns.md §14 exists to forbid.
 */
export function parseRetryAfter(header: string | null, now: number = Date.now()): number | null {
  if (header === null) return null
  const trimmed = header.trim()
  if (trimmed === '') return null

  if (/^\d+$/.test(trimmed)) {
    const seconds = Number(trimmed)
    return Number.isFinite(seconds) ? seconds : null
  }

  const at = Date.parse(trimmed)
  if (Number.isNaN(at)) return null
  const seconds = Math.round((at - now) / 1000)
  return seconds > 0 ? seconds : null
}

/**
 * The response header Usher puts the server span in.
 *
 * Lowercase because `Headers.get` is case-insensitive and this is the spelling
 * the backend writes (`usher.telemetry.TRACERESPONSE_HEADER`). The two are one
 * contract; there is no third place that names it.
 */
export const TRACE_HEADER = 'traceresponse'

/**
 * `00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>`, per field.
 *
 * Transcribed from `w3c/trace-context`'s `spec/21-http_response_header_format.md`
 * rather than from memory, which is worth saying because the header **moved**:
 * the published Trace Context Level 2 Recommendation defines no response header
 * at all, and the current draft carries this exact grammar on a `Server-Timing`
 * metric named `trace` instead. The *value* is the same either way, and this is
 * the value.
 *
 * Lowercase only, deliberately: the spec says a reader MUST ignore the metric
 * when a field "contains non-lowercase hex characters", so widening this to
 * `[0-9a-fA-F]` would accept exactly the headers a conformant reader drops.
 */
const HEX_2 = /^[0-9a-f]{2}$/
const HEX_16 = /^[0-9a-f]{16}$/
const HEX_32 = /^[0-9a-f]{32}$/

const ALL_ZERO = /^0+$/

/**
 * The trace id out of a `traceresponse` header, or `null`.
 *
 * `null`, never a throw and never a partial id, for every one of: an absent
 * header, a header that does not match the grammar, version `ff` (forbidden by
 * the spec), and an all-zero trace or span id (*"All zeroes forbidden"*, both
 * fields — Usher's own middleware sends no header at all rather than a zeroed
 * one, so a zeroed one arriving means something in between invented it).
 *
 * Defensive in `client.ts`'s house style: the one situation this exists for is
 * the one where the wire is not what the spec says, and a console that throws
 * while reading a diagnostic header is worse than one with no link.
 *
 * A version other than `00` is **accepted, including with extra fields after
 * the flags**, which is Trace Context's own forward-compatibility rule for
 * `traceparent` and applies here for the same reason: a later version may
 * append fields, this reads only the two that are pinned, and a console that
 * blanked the trace link on a version bump would be broken by an upgrade that
 * changed nothing it uses. At version `00` the field count is exact, because
 * `00` is specified and a fifth field in it is a malformed header rather than
 * a newer one.
 */
export function parseTraceResponse(header: string | null): string | null {
  if (header === null) return null
  const parts = header.trim().split('-')
  const [version, traceId, spanId, flags] = parts
  if (version === undefined || traceId === undefined) return null
  if (spanId === undefined || flags === undefined) return null
  if (!HEX_2.test(version) || version === 'ff') return null
  if (version === '00' && parts.length !== 4) return null
  if (!HEX_32.test(traceId) || !HEX_16.test(spanId) || !HEX_2.test(flags)) return null
  // `"All zeroes forbidden"`, the spec's own wording, for both id fields.
  if (ALL_ZERO.test(traceId) || ALL_ZERO.test(spanId)) return null
  return traceId
}

/**
 * `errors[]` as the field-scale renderer wants it: a JSON-pointer-ish location
 * and the server's own message.
 *
 * FastAPI's `loc` is an array whose first element is the *section* (`body`,
 * `query`, `path`) and whose remainder is the path within it. The section is
 * kept — a `query` failure and a `body` failure with the same field name are
 * different fields on screen — and the join is the only transformation applied.
 * `msg` is passed through untouched for the same reason `detail` is.
 */
export interface FieldError {
  /** Dotted path, e.g. `body.password`. Empty when the server sent no `loc`. */
  readonly field: string
  /** The server's message, verbatim. */
  readonly message: string
}

function readField(source: object, key: string): unknown {
  return key in source ? Reflect.get(source, key) : undefined
}

function joinLoc(loc: unknown): string {
  if (!Array.isArray(loc)) return ''
  return loc
    .filter((part: unknown): part is string | number => typeof part === 'string' || typeof part === 'number')
    .join('.')
}

/**
 * Reads `errors[]` off a problem document. Returns `[]` for every code except
 * `validation_failed`, which is the only one that carries them — and `[]` here
 * means "the server sent none", which a field-scale renderer must treat as a
 * reason to fall back to `detail` rather than to render an empty list.
 */
export function fieldErrors(errors: unknown): FieldError[] {
  if (!Array.isArray(errors)) return []
  const items: unknown[] = errors
  const out: FieldError[] = []
  for (const raw of items) {
    if (raw === null || typeof raw !== 'object') continue
    const message = readField(raw, 'msg')
    out.push({
      field: joinLoc(readField(raw, 'loc')),
      message: typeof message === 'string' ? message : '',
    })
  }
  return out
}
