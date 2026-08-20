/**
 * RFC 9457 problem documents, one factory per member of the closed seven.
 *
 * Any test can make any route fail correctly:
 *
 * ```ts
 * server.use(problemHandler('get', '/titles/:title_id', notFound('/titles/…')))
 * server.use(transportFailure('get', '/home'))
 * ```
 *
 * **Two things every one of these carries and no test may drop.** The
 * `content-type` is `application/problem+json`, because that header is what
 * `client.ts` sniffs to decide whether a failure is a *problem document* or
 * merely a non-2xx — the header is the thing itself and the spec is a
 * description of it. And `detail` is prose: patterns.md §3 requires it shown
 * verbatim and forbids parsing it, so the strings below read like something a
 * server would say rather than like enum values in disguise.
 *
 * `type` is `about:blank`, which RFC 9457 makes the default when the problem
 * type adds nothing beyond the status — Usher's specificity lives in `code`,
 * which is what recovery is looked up on.
 */

import { http, HttpResponse, type HttpResponseResolver } from 'msw'
import { TRACE_HEADER, type ProblemCode, type ProblemDocument } from '@/api'
import { CURSOR_STALE, TITLE_MISSING, TITLE_NOT_PLAYABLE, TRACE_ID, TRACE_SPAN_ID } from './ids'

/** The `content-type` `client.ts` sniffs for. Never plain `application/json`. */
export const PROBLEM_CONTENT_TYPE = 'application/problem+json'

/**
 * A well-formed `traceresponse` value: `00-<trace>-<span>-<flags>`.
 *
 * `01` is the sampled flag, which is the only case that produces a header at
 * all — Usher's middleware sends **no header** when the span is not recording,
 * rather than an all-zero one, because a zeroed id is well-formed and names
 * nothing. That is why the "no trace" fixtures below omit the header entirely
 * instead of sending a blank one.
 */
export function traceResponse(traceId: string = TRACE_ID): string {
  return `00-${traceId}-${TRACE_SPAN_ID}-01`
}

interface ProblemOverrides {
  detail?: string
  instance?: string
  errors?: { [key: string]: unknown }[]
}

function build(
  code: ProblemCode,
  status: number,
  title: string,
  detail: string,
  instance: string,
  overrides: ProblemOverrides = {},
): ProblemDocument {
  const doc: ProblemDocument = {
    type: 'about:blank',
    title,
    status,
    code,
    detail: overrides.detail ?? detail,
    instance: overrides.instance ?? instance,
  }
  return overrides.errors ? { ...doc, errors: overrides.errors } : doc
}

/* --------------------------------------------------- the seven, in §3 order */

/** 404 · page scale · back + search, and **no retry**: the row does not exist. */
export function notFound(
  instance = `/titles/${TITLE_MISSING}`,
  overrides: ProblemOverrides = {},
): ProblemDocument {
  return build('not_found', 404, 'Not Found', `No title with id ${TITLE_MISSING}.`, instance, overrides)
}

/**
 * 422 · field scale · fix the field, from `errors[].loc` and `errors[].msg`.
 *
 * `loc`'s first element is the *section* — `body`, `query`, `path` — and the
 * remainder is the path within it, so a `query` failure and a `body` failure
 * with the same field name stay distinguishable on screen.
 */
export function validationFailed(
  instance = '/admin/sources',
  overrides: ProblemOverrides = {},
): ProblemDocument {
  return build(
    'validation_failed',
    422,
    'Unprocessable Entity',
    'The request body failed validation.',
    instance,
    {
      errors: [
        { loc: ['body', 'base_url'], msg: 'Input should be a valid URL', type: 'url_parsing' },
        {
          loc: ['body', 'password'],
          msg: 'String should have at least 1 character',
          type: 'string_too_short',
        },
      ],
      ...overrides,
    },
  )
}

/** 405 · panel scale · **no recovery**. A developer error, shown plainly. */
export function methodNotAllowed(
  instance = '/health/ready',
  overrides: ProblemOverrides = {},
): ProblemDocument {
  return build(
    'method_not_allowed',
    405,
    'Method Not Allowed',
    'POST is not allowed on this route. Allowed: GET, HEAD.',
    instance,
    overrides,
  )
}

/**
 * 400 · **never rendered** · silently restart the list from the top.
 *
 * A cursor encodes a position *and a hash of the query*, so this is what a
 * changed filter racing an in-flight page looks like. patterns.md §4 is
 * explicit that it MUST NOT reach the UI — a user who changed a filter did
 * nothing wrong and has nothing to fix.
 */
export function invalidCursor(instance = '/browse', overrides: ProblemOverrides = {}): ProblemDocument {
  return build(
    'invalid_cursor',
    400,
    'Bad Request',
    `The cursor ${CURSOR_STALE.slice(0, 12)}… does not match this query.`,
    instance,
    overrides,
  )
}

/** 503 · panel scale · retry, honouring `Retry-After`. */
export function sourceUnavailable(
  instance = '/admin/sources/status',
  overrides: ProblemOverrides = {},
): ProblemDocument {
  return build(
    'source_unavailable',
    503,
    'Service Unavailable',
    'Living Room Emby did not answer within 5.0 s.',
    instance,
    overrides,
  )
}

/**
 * 409 · panel scale · **no retry button**. Offer "See other copies".
 *
 * Retrying is guaranteed to fail identically, so a retry affordance here is a
 * button whose only function is to waste the user's attention.
 */
export function notPlayable(
  instance = `/titles/${TITLE_NOT_PLAYABLE}/play`,
  overrides: ProblemOverrides = {},
): ProblemDocument {
  return build(
    'not_playable',
    409,
    'Conflict',
    'No available copy of this title on any configured source.',
    instance,
    overrides,
  )
}

/** 404 · inline strip · one tap re-requests and plays. A 300 s ticket expired. */
export function ticketInvalid(instance = '/stream/…', overrides: ProblemOverrides = {}): ProblemDocument {
  return build(
    'ticket_invalid',
    404,
    'Not Found',
    'This playback ticket has expired or was already redeemed.',
    instance,
    overrides,
  )
}

/**
 * All seven, keyed by code, for a test that wants to sweep the vocabulary
 * rather than name one member. Typed as a total map so an eighth code added to
 * the API breaks this file rather than being silently untested.
 */
export const PROBLEMS: { readonly [C in ProblemCode]: () => ProblemDocument } = {
  not_found: () => notFound(),
  validation_failed: () => validationFailed(),
  method_not_allowed: () => methodNotAllowed(),
  invalid_cursor: () => invalidCursor(),
  source_unavailable: () => sourceUnavailable(),
  not_playable: () => notPlayable(),
  ticket_invalid: () => ticketInvalid(),
}

/* --------------------------------------------------------------- handlers */

export type Method = 'get' | 'post' | 'put' | 'delete'

/** A problem response with the right status and the right content type. */
export function problemResponse(doc: ProblemDocument, headers: Record<string, string> = {}) {
  return HttpResponse.json(doc, {
    status: doc.status,
    headers: { 'content-type': PROBLEM_CONTENT_TYPE, ...headers },
  })
}

/**
 * Makes one route fail with one problem document.
 *
 * `retryAfter` is the seconds value for `source_unavailable`'s `Retry-After`
 * header — the one code whose recovery is a retry, and the one patterns.md §3
 * requires that retry to honour.
 *
 * `traceId` puts a `traceresponse` header on the response, which is where the
 * trace id lives — it is **not** a member of the problem document and there is
 * no plan for it to become one. Omitting it is the honest "this response
 * carried no trace" case, and the two are the whole test matrix for §3's
 * "Open trace" link.
 */
export function problemHandler(
  method: Method,
  path: string,
  doc: ProblemDocument,
  options: { retryAfter?: number | string; traceId?: string } = {},
) {
  const headers: Record<string, string> = {}
  if (options.retryAfter !== undefined) headers['retry-after'] = String(options.retryAfter)
  if (options.traceId !== undefined) headers[TRACE_HEADER] = traceResponse(options.traceId)
  const resolver: HttpResponseResolver = () => problemResponse(doc, headers)
  return http[method](path, resolver)
}

/**
 * Makes one route fail at the **transport** layer — no status, no body, no
 * headers.
 *
 * This is the `status: 0` path in `client.ts`, and it is a distinct failure
 * from any HTTP status: "the request never left" and "the server said nothing"
 * look identical to a user staring at a spinner, so the journal records it as
 * status 0 rather than omitting it. `fetch` rejects, so the caller gets a
 * `TypeError` and **not** an `UsherProblem` — a surface that only handles
 * `UsherProblem` renders nothing for this case, which is exactly what these
 * fixtures exist to catch.
 */
export function transportFailure(method: Method, path: string) {
  const resolver: HttpResponseResolver = () => HttpResponse.error()
  return http[method](path, resolver)
}
