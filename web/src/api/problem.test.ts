/**
 * The seven-code vocabulary, and the table recovery is looked up in.
 *
 * The point of these is not that a lookup returns what it was written to
 * return. It is that the *closure* holds: the table is total over the schema's
 * enum, an eighth code cannot be silently untested, and the one distinction a
 * component would otherwise get wrong — two codes share status 404 and only one
 * of them may offer a retry — is asserted rather than assumed.
 */

import { describe, expect, it } from 'vitest'
import {
  RECOVERY,
  TRACE_HEADER,
  fieldErrors,
  isProblemCode,
  parseRetryAfter,
  parseTraceResponse,
  recoveryFor,
  type ProblemCode,
} from './problem'
import { PROBLEMS, validationFailed } from '@/test/fixtures/problems'

const ALL_CODES: ProblemCode[] = [
  'not_found',
  'validation_failed',
  'method_not_allowed',
  'invalid_cursor',
  'source_unavailable',
  'not_playable',
  'ticket_invalid',
]

describe('the closed vocabulary', () => {
  it('has exactly seven members and the table covers all of them', () => {
    // Spelled out rather than derived from `RECOVERY`, or this asserts only
    // that an object has the keys it was written with.
    expect(Object.keys(RECOVERY).sort()).toEqual([...ALL_CODES].sort())
    expect(ALL_CODES).toHaveLength(7)
  })

  it('rejects anything outside it', () => {
    expect(isProblemCode('not_found')).toBe(true)
    expect(isProblemCode('rate_limited')).toBe(false)
    expect(isProblemCode('')).toBe(false)
    expect(isProblemCode(404)).toBe(false)
    expect(isProblemCode(null)).toBe(false)
    expect(isProblemCode(undefined)).toBe(false)
  })

  it('answers null for a code the wire invented, rather than throwing', () => {
    expect(recoveryFor('teapot')).toBeNull()
    expect(recoveryFor('not_found')).toBe(RECOVERY.not_found)
  })
})

describe('patterns.md §3, one case per row of the table', () => {
  it('not_found is page scale with no retry', () => {
    const entry = RECOVERY.not_found
    expect(entry.status).toBe(404)
    expect(entry.scale).toBe('page')
    expect(entry.rendered).toBe(true)
    expect(entry.retryable).toBe(false)
    expect(entry.recovery.kind).toBe('back-and-search')
  })

  it('validation_failed is field scale and driven from errors[]', () => {
    const entry = RECOVERY.validation_failed
    expect(entry.status).toBe(422)
    expect(entry.scale).toBe('field')
    expect(entry.recovery.kind).toBe('fix-field')
  })

  it('method_not_allowed is a panel that offers nothing', () => {
    const entry = RECOVERY.method_not_allowed
    expect(entry.status).toBe(405)
    expect(entry.scale).toBe('panel')
    expect(entry.retryable).toBe(false)
    expect(entry.recovery.kind).toBe('none')
  })

  it('invalid_cursor is never rendered and restarts the list', () => {
    const entry = RECOVERY.invalid_cursor
    expect(entry.status).toBe(400)
    // The load-bearing assertion in this file: a surface that renders this code
    // has a bug in its cursor handling, not a message to show.
    expect(entry.rendered).toBe(false)
    expect(entry.scale).toBe('none')
    expect(entry.recovery.kind).toBe('restart-list')
  })

  it('source_unavailable retries and honours Retry-After', () => {
    const entry = RECOVERY.source_unavailable
    expect(entry.status).toBe(503)
    expect(entry.scale).toBe('panel')
    expect(entry.retryable).toBe(true)
    expect(entry.recovery).toEqual({ kind: 'retry', honourRetryAfter: true })
  })

  it('not_playable offers other copies and must not offer a retry', () => {
    const entry = RECOVERY.not_playable
    expect(entry.status).toBe(409)
    expect(entry.retryable).toBe(false)
    expect(entry.recovery).toEqual({ kind: 'other-copies', label: 'See other copies' })
  })

  it('ticket_invalid is an inline strip that re-requests', () => {
    const entry = RECOVERY.ticket_invalid
    expect(entry.status).toBe(404)
    expect(entry.scale).toBe('inline')
    expect(entry.retryable).toBe(true)
    expect(entry.recovery.kind).toBe('re-request')
  })

  it('separates the two 404s, which status alone cannot', () => {
    // Both are 404. One may re-request and one may not, so any surface deciding
    // "can I retry this" from `status` gets `not_found` wrong.
    expect(RECOVERY.not_found.status).toBe(RECOVERY.ticket_invalid.status)
    expect(RECOVERY.not_found.retryable).not.toBe(RECOVERY.ticket_invalid.retryable)
  })

  it('every fixture document agrees with the table about its status', () => {
    for (const code of ALL_CODES) {
      const doc = PROBLEMS[code]()
      expect(doc.code).toBe(code)
      expect(doc.status, `${code} fixture`).toBe(RECOVERY[code].status)
    }
  })
})

describe('Retry-After', () => {
  it('reads delta-seconds', () => {
    expect(parseRetryAfter('120')).toBe(120)
    expect(parseRetryAfter('  30  ')).toBe(30)
    expect(parseRetryAfter('0')).toBe(0)
  })

  it('reads an HTTP-date against a supplied now', () => {
    const now = Date.parse('2026-08-19T12:00:00Z')
    expect(parseRetryAfter('Wed, 19 Aug 2026 12:02:00 GMT', now)).toBe(120)
  })

  it('answers null rather than 0 when the server did not say', () => {
    // "wait no time" and "the server did not say" are different, and a
    // countdown rendered from a fabricated zero is an invented number.
    expect(parseRetryAfter(null)).toBeNull()
    expect(parseRetryAfter('')).toBeNull()
    expect(parseRetryAfter('soon')).toBeNull()
  })

  it('answers null for a date already in the past', () => {
    const now = Date.parse('2026-08-19T12:00:00Z')
    expect(parseRetryAfter('Wed, 19 Aug 2026 11:59:00 GMT', now)).toBeNull()
  })
})

describe('traceresponse', () => {
  // The W3C example, which is also what Usher's own middleware produces:
  // `00-<32 hex>-<16 hex>-<2 hex>`, every field lowercase.
  const TRACE_ID = '0af7651916cd43dd8448eb211c80319c'
  const SPAN_ID = 'b7ad6b7169203331'
  const HEADER = `00-${TRACE_ID}-${SPAN_ID}-01`

  it('is the header name the backend writes', () => {
    // One contract, two languages. `usher.telemetry.TRACERESPONSE_HEADER` is
    // the other half and there is no third place that names it.
    expect(TRACE_HEADER).toBe('traceresponse')
  })

  it('reads the trace id out of a well-formed header', () => {
    expect(parseTraceResponse(HEADER)).toBe(TRACE_ID)
    expect(parseTraceResponse(`  ${HEADER}  `)).toBe(TRACE_ID)
  })

  it('takes the trace id and never the span id', () => {
    // The two fields are the same alphabet and differ only in length, so a
    // parser that returned `match[3]` would still hand back a plausible hex
    // string. Tempo is queried by *trace* id; a span id opens nothing.
    const parsed = parseTraceResponse(HEADER)
    expect(parsed).toHaveLength(32)
    expect(parsed).not.toBe(SPAN_ID)
  })

  it('is null when the header is absent', () => {
    expect(parseTraceResponse(null)).toBeNull()
  })

  it.each([
    ['empty', ''],
    ['not the grammar at all', 'nope'],
    ['a bare trace id with no version', TRACE_ID],
    ['too few fields', `00-${TRACE_ID}-${SPAN_ID}`],
    // Exact at version `00`: that version is specified, so a fifth field in it
    // is a malformed header rather than a newer one.
    ['a fifth field at version 00', `00-${TRACE_ID}-${SPAN_ID}-01-extra`],
    ['a short trace id', `00-${TRACE_ID.slice(1)}-${SPAN_ID}-01`],
    ['a long trace id', `00-${TRACE_ID}f-${SPAN_ID}-01`],
    ['a short span id', `00-${TRACE_ID}-${SPAN_ID.slice(1)}-01`],
    ['non-hex characters', `00-${'z'.repeat(32)}-${SPAN_ID}-01`],
    // The spec says a reader MUST ignore the value when a field "contains
    // non-lowercase hex characters", so this is a refusal rather than
    // strictness for its own sake.
    ['uppercase hex', `00-${TRACE_ID.toUpperCase()}-${SPAN_ID}-01`],
    ['the forbidden ff version', `ff-${TRACE_ID}-${SPAN_ID}-01`],
    // Usher sends **no header** rather than a zeroed one, so a zeroed one
    // arriving means something in between invented it. `"All zeroes
    // forbidden"` is the spec's own wording, for both id fields.
    ['an all-zero trace id', `00-${'0'.repeat(32)}-${SPAN_ID}-01`],
    ['an all-zero span id', `00-${TRACE_ID}-${'0'.repeat(16)}-01`],
  ])('is null for %s, and never throws', (_label, header) => {
    expect(() => parseTraceResponse(header)).not.toThrow()
    expect(parseTraceResponse(header)).toBeNull()
  })

  it('accepts a future version, because a later one may only append fields', () => {
    // Trace Context's own forward-compatibility rule for `traceparent`, and it
    // applies here for the same reason: this reads the two fields the grammar
    // pins and a version bump that adds a fifth must not blank the link.
    expect(parseTraceResponse(`01-${TRACE_ID}-${SPAN_ID}-01`)).toBe(TRACE_ID)
    expect(parseTraceResponse(`01-${TRACE_ID}-${SPAN_ID}-01-something`)).toBe(TRACE_ID)
  })
})

describe('errors[] for the field scale', () => {
  it('joins loc into a dotted path and passes msg through verbatim', () => {
    const parsed = fieldErrors(validationFailed().errors)
    expect(parsed).toEqual([
      { field: 'body.base_url', message: 'Input should be a valid URL' },
      { field: 'body.password', message: 'String should have at least 1 character' },
    ])
  })

  it('keeps the section, so body.name and query.name stay distinct', () => {
    const parsed = fieldErrors([
      { loc: ['body', 'name'], msg: 'a' },
      { loc: ['query', 'name'], msg: 'b' },
    ])
    expect(parsed.map((e) => e.field)).toEqual(['body.name', 'query.name'])
  })

  it('is [] for anything that is not a list of objects', () => {
    expect(fieldErrors(undefined)).toEqual([])
    expect(fieldErrors(null)).toEqual([])
    expect(fieldErrors('nope')).toEqual([])
    expect(fieldErrors([null, 3, 'x'])).toEqual([])
  })
})
