/**
 * The one place this app talks to Usher.
 *
 * Two things every call gets for free: an entry in the dev journal
 * (`devlog.ts`), and RFC 9457 problem documents turned into a typed error
 * instead of an unhelpful `TypeError` three frames later. Usher answers every
 * failure as `application/problem+json` over a closed seven-member `code`
 * vocabulary, so a client that parses it can say what actually went wrong.
 */

import type { components, paths } from './schema'
import * as devlog from './devlog'
import { TRACE_HEADER, isProblemCode, parseRetryAfter, parseTraceResponse, type ProblemCode } from './problem'
import { redact } from './redact'

export type Schemas = components['schemas']

export { REDACTED_KEYS, PLAYBACK_TICKET_PLACEHOLDER, redact } from './redact'

/**
 * One property off an unknown value, without a cast.
 *
 * A problem document arrives as `unknown` and every field of it is read
 * defensively, because the one situation this class exists for is the one
 * where the body is not what the spec says.
 */
function field(source: unknown, key: string): unknown {
  return source !== null && typeof source === 'object' ? Reflect.get(source, key) : undefined
}

export class UsherProblem extends Error {
  readonly status: number
  /** The wire's `code`, verbatim, whether or not it is one of the seven. */
  readonly code: string
  /**
   * The same value narrowed to Usher's closed vocabulary, or `null` when the
   * wire carried something else — a proxy's error page, a member of a future
   * vocabulary. `RECOVERY` is keyed on this, so the seven-way lookup is total
   * and the fallback is explicit rather than a `default:` arm.
   */
  readonly knownCode: ProblemCode | null
  readonly detail: string
  readonly instance: string | undefined
  readonly errors: unknown
  readonly body: unknown
  /**
   * `Retry-After` in seconds, or `null` when the header was absent or
   * unparseable. Parsed here because it lives on the *response* and is gone by
   * the time the error reaches a component — and patterns.md §3 requires the
   * retry affordance for `source_unavailable` to honour it.
   */
  readonly retryAfter: number | null
  /**
   * The trace id of the server span that produced this failure, or `null`.
   *
   * Read from the `traceresponse` **response header**, not from the body: the
   * problem envelope is a closed contract over six members and a seven-member
   * `code` vocabulary, `openapi-typescript` regenerates this client's types
   * from it, and a trace id is a fact about the exchange rather than about
   * what went wrong. The header is also there on a **200**, which is why
   * `devlog.ts` carries it per entry and not only here.
   *
   * Readable from JavaScript with no `Access-Control-Expose-Headers` because
   * the console is served by Usher's own app now (see `API_BASE`) — a
   * same-origin response exposes every header. That was not true of the
   * reference client behind its own nginx, where this would have been
   * invisible to `fetch`.
   *
   * `null` — never `''`, never a half-parsed id — for an absent header and for
   * a malformed one alike, on `parseRetryAfter`'s reasoning one field up:
   * "there is no trace" and "the trace is X" are different answers, and
   * `useTraceUrl()` turns the first into an absent link rather than a dead one.
   */
  readonly traceId: string | null

  constructor(status: number, body: unknown, headers?: Headers) {
    const code = field(body, 'code')
    const rawTitle = field(body, 'title')
    const rawDetail = field(body, 'detail')
    const instance = field(body, 'instance')
    const title = typeof rawTitle === 'string' ? rawTitle : `HTTP ${status}`
    const detail = typeof rawDetail === 'string' ? rawDetail : ''
    super(detail ? `${title}: ${detail}` : title)
    this.name = 'UsherProblem'
    this.status = status
    this.code = typeof code === 'string' ? code : 'unknown'
    this.knownCode = isProblemCode(code) ? code : null
    this.detail = detail
    this.instance = typeof instance === 'string' ? instance : undefined
    this.errors = field(body, 'errors')
    this.body = body
    this.retryAfter = parseRetryAfter(headers?.get('retry-after') ?? null)
    this.traceId = parseTraceResponse(headers?.get(TRACE_HEADER) ?? null)
  }
}

/**
 * No prefix. Every path is sent exactly as `/openapi.json` declares it —
 * `/titles/{id}`, `/stream/{ticket}`, `/openapi.json` itself.
 *
 * The console is served by Usher's own FastAPI app at `/console/`, so it is
 * already on the API's origin and there is nothing between the two. Vite's dev
 * server forwards each of Usher's root segments untouched for exactly that
 * reason (`vite.config.ts`), so the paths this file sends in dev are the paths
 * it sends in prod. No rewrite exists in either.
 *
 * **That absence is the fix, not a simplification.** The reference client
 * served itself from `/` and reached the API at `/api/*`, which meant a
 * rewrite in two places — nginx's `proxy_pass` and Vite's `rewrite` — and Usher
 * mints the playback ticket URL in `POST /play` from the *incoming `Host`
 * header*. A proxy that forwards `$http_host` unchanged hands back a ticket URL
 * on the proxy's own port; one that does not hands back a URL on a port the
 * browser cannot reach. There was no configuration of that pair that was
 * correct for both, and with no prefix there is no proxy in the path and no
 * header to get wrong. Usher ships no CORS middleware either; same-origin is
 * why that never comes up.
 */
export const API_BASE = ''

/**
 * The proxy's width parameter is `w`, not `width`.
 *
 * This is worth a comment because getting it wrong fails *silently*: FastAPI
 * ignores an undeclared query parameter, so `?width=780` returns 200 carrying
 * the 342 default. A wrong name here costs image quality with no error
 * anywhere to notice it by.
 */
export function imageUrl(imageId: string, w?: number): string {
  return `${API_BASE}/images/${imageId}${w ? `?w=${w}` : ''}`
}

/** ADR-0032's ladder. The proxy clamps to it; asking for 800 gets you 780. */
export const IMAGE_WIDTHS = [154, 342, 780, 1280] as const

const STREAM_SEGMENT = /\/stream\/([^/?#]+)/

/**
 * The ticket in a play target's `url`, or `null` when there is none.
 *
 * `PlayTargetResponse.url` is **already a ticket URL on Usher's own origin**
 * (`http://<usher>/stream/<fernet ticket>`), and the `deep_link` arm is that
 * same stream URL percent-encoded into an `infuse://x-callback-url/play?url=…`.
 * Neither arm carries a session token: the token appears only in the `302
 * Location` that `/stream/{ticket}` answers with, which the browser follows and
 * the page never sees.
 *
 * So the reason not to hand `target.url` to a `<video>` verbatim is not
 * leakage. It is that the absolute URL names a host — the one Usher read off
 * the request's `Host` header — and the only host this document is allowed to
 * assume is its own. Lifting the ticket out and re-issuing it at
 * `/stream/{ticket}` is what makes that assumption unnecessary.
 *
 * This refuses to guess: a target whose `url` has no `/stream/{…}` segment is
 * reported rather than played, because the only other thing it could be is a
 * source URL carrying somebody's session token.
 */
export function ticketOf(rawUrl: string): string | null {
  const direct = STREAM_SEGMENT.exec(rawUrl)
  if (direct?.[1] !== undefined) return decodeURIComponent(direct[1])
  try {
    const wrapped = new URL(rawUrl).searchParams.get('url')
    if (wrapped !== null) {
      const inner = STREAM_SEGMENT.exec(wrapped)
      if (inner?.[1] !== undefined) return decodeURIComponent(inner[1])
    }
  } catch {
    // Not parseable as a URL, which is itself an answer: no ticket.
  }
  return null
}

/**
 * Same-origin stream path. Never a host — see `API_BASE`.
 *
 * The player is pointed at this and at nothing else. A caller that has this
 * string must not render it, copy it or log it: patterns.md §13 makes the
 * ticket URL a secret, which is why `redact.ts` removes it from the journal
 * and why there is no `shortTicket`-style formatter anywhere in this codebase.
 *
 * Worth knowing before debugging a failure to play: most of this library is
 * mkv, and a browser that demuxes Matroska will still refuse HEVC. A `206`
 * carrying `video/x-matroska` alongside `MEDIA_ERR_SRC_NOT_SUPPORTED` means the
 * byte path works and the decoder is missing — a different fact from "playback
 * is broken", and one a player surface has to state separately. (`content-range`
 * itself is unreadable from JS: the Emby upstream sends no
 * `access-control-expose-headers`, so only safelisted headers survive.)
 */
export function streamPath(ticket: string): string {
  return `${API_BASE}/stream/${encodeURIComponent(ticket)}`
}

export type QueryValue = string | number | boolean | undefined | null

export type RequestOptions = {
  method?: string
  body?: unknown
  signal?: AbortSignal
  /** Query parameters. `undefined` and `null` values are dropped, not sent. */
  query?: Record<string, QueryValue>
}

function buildPath(path: string, query?: RequestOptions['query']): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue
    params.set(k, String(v))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = (opts.method ?? 'GET').toUpperCase()
  const withQuery = buildPath(path, opts.query)
  const started = performance.now()
  const startedAt = Date.now()

  // Built up rather than written as one literal: `exactOptionalPropertyTypes`
  // makes `{ signal: undefined }` a different thing from an absent `signal`,
  // and `RequestInit` declares neither as accepting `undefined`.
  const init: RequestInit = { method }
  if (opts.signal) init.signal = opts.signal
  if (opts.body !== undefined) {
    init.headers = { 'content-type': 'application/json' }
    init.body = JSON.stringify(opts.body)
  }

  let res: Response
  try {
    res = await fetch(`${API_BASE}${withQuery}`, init)
  } catch (err) {
    // A transport failure has no status and no body, and recording it as
    // status 0 keeps it visible in the drawer rather than silently absent --
    // "the request never left" and "the server said nothing" look identical
    // to a user staring at a spinner.
    devlog.record({
      method,
      path: withQuery,
      template: devlog.matchTemplate(withQuery),
      status: 0,
      ms: Math.round(performance.now() - started),
      startedAt,
      response: String(err),
      request: redact(opts.body),
      problem: false,
      // There is no response, so there is no header and no server span this
      // call ever reached. `null` rather than an empty string: the drawer
      // renders the absence as "this response carried no trace id", which is
      // exactly true here.
      traceId: null,
    })
    throw err
  }

  const ms = Math.round(performance.now() - started)
  const contentType = res.headers.get('content-type') ?? ''
  const isJson = contentType.includes('json')

  let body: unknown = null
  if (res.status !== 204) {
    body = isJson ? await res.json().catch(() => null) : await res.text().catch(() => null)
  }

  devlog.record({
    method,
    path: withQuery,
    template: devlog.matchTemplate(withQuery),
    status: res.status,
    ms,
    startedAt,
    // Redacted again inside `record`, which is what catches the response side:
    // a 200 from `POST /play` carries a live 300-second ticket in `targets[].url`
    // and would otherwise be journalled in full. The caller below still receives
    // the real body — redaction is what the *journal* holds, not what the app
    // gets, or the player would have nothing to play.
    response: body,
    request: redact(opts.body),
    // `application/problem+json` is the wire truth, so this reads the response
    // header rather than the API document. The document now agrees -- all 56
    // problem responses in `/openapi.json` declare `application/problem+json`
    // since the M10 sweep, where they used to say `application/json` and were
    // known-wrong about it -- but that is a reason to stop distrusting the
    // spec, not a reason to start trusting it over the header. The header is
    // the thing itself; the spec is a description of it, and only one of the
    // two can be stale on a deployment.
    problem: !res.ok && contentType.includes('problem+json'),
    // **Every response, not only the failures.** The drawer's whole job is to
    // make a call diagnosable, and "why was this 200 four seconds slow" is a
    // Tempo question with no problem document to carry the answer. Read off
    // the same header `UsherProblem` reads, through the same parser, so the
    // journal and the thrown error cannot disagree about one exchange.
    traceId: parseTraceResponse(res.headers.get(TRACE_HEADER)),
  })

  if (!res.ok) throw new UsherProblem(res.status, body, res.headers)
  // The one unchecked assertion in the client, and it is the generic's whole
  // purpose: this is where untyped wire bytes become the schema's types. Every
  // caller names its `T` from `Ok<'/route'>` below rather than hand-writing an
  // interface, so a DTO rename in Usher fails to compile here instead of
  // arriving as `undefined` on a screen.
  return body as T
}

/* -------------------------------------------------------------------------
 * Response types, read straight off the generated schema.
 *
 * `Ok<'/home'>` is whatever `GET /home` actually answers, so a rename in
 * Usher's DTOs becomes a compile error here instead of `undefined` on screen.
 * ---------------------------------------------------------------------- */

/**
 * Only ever applied to a success response, which is why the spec's move of
 * every failure body from `application/json` to `application/problem+json`
 * changed nothing here: a 2xx still carries `application/json`, and the error
 * arms were never extracted by these types in the first place -- they arrive
 * as a thrown `UsherProblem` instead.
 */
type JsonOf<T> = T extends { content: { 'application/json': infer R } } ? R : never

export type Ok<P extends keyof paths> = paths[P] extends {
  get: { responses: { 200: infer R } }
}
  ? JsonOf<R>
  : never

/**
 * A POST's success body. Three codes because Usher uses all three and means
 * different things by them: `200` for a synchronous answer (the playback
 * ticket), `201` for a created resource (a source), `202` for work that has
 * been *queued* (a sync, a bootstrap phase, a row regeneration) -- and the
 * last one is why a UI must not report "done" on a 2xx from those routes.
 */
export type OkPost<P extends keyof paths> = paths[P] extends {
  post: { responses: { 200: infer R } }
}
  ? JsonOf<R>
  : paths[P] extends { post: { responses: { 201: infer R } } }
    ? JsonOf<R>
    : paths[P] extends { post: { responses: { 202: infer R } } }
      ? JsonOf<R>
      : never

export type OkPut<P extends keyof paths> = paths[P] extends {
  put: { responses: { 200: infer R } }
}
  ? JsonOf<R>
  : never

/** Loads the API's path templates so the journal can name each operation. */
export async function loadOperationTemplates(): Promise<void> {
  try {
    const doc: unknown = await fetch(`${API_BASE}/openapi.json`).then((r) => r.json())
    const declared = field(doc, 'paths')
    devlog.setTemplates(declared !== null && typeof declared === 'object' ? Object.keys(declared) : [])
  } catch {
    // A missing document costs the drawer its operation names and nothing
    // else, so this must not take the app down with it.
  }
}
