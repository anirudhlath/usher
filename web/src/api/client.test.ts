/**
 * The transport, exercised through the real MSW server rather than a stubbed
 * `fetch` — which would agree with whatever the client happens to do.
 *
 * Four things are asserted here that nothing else in the suite can assert:
 * that a problem document becomes a typed error carrying its `code`; that the
 * *header* and not the status is what makes a failure a problem document; that
 * a transport failure is journalled as status 0 rather than disappearing; and
 * that neither an Emby password nor a playback ticket can reach the journal.
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/test/server'
import * as devlog from './devlog'
import {
  API_BASE,
  UsherProblem,
  imageUrl,
  loadOperationTemplates,
  request,
  streamPath,
  ticketOf,
} from './client'
import { PLAYBACK_TICKET_PLACEHOLDER, redact } from './redact'
import { PROBLEMS, problemHandler, problemResponse, transportFailure } from '@/test/fixtures/problems'
import { deepLinkUrl, directTicketUrl } from '@/test/fixtures/play'
import { PLAYBACK_TICKET, TITLE_ENRICHED, TITLE_NOT_PLAYABLE } from '@/test/fixtures/ids'
import { openapiPaths } from '@/test/fixtures/meta'
import type { ProblemCode } from './problem'

beforeEach(() => {
  devlog.resetForTests()
})

const ALL_CODES: ProblemCode[] = [
  'not_found',
  'validation_failed',
  'method_not_allowed',
  'invalid_cursor',
  'source_unavailable',
  'not_playable',
  'ticket_invalid',
]

describe('API_BASE', () => {
  it('is empty, so a path is sent exactly as the OpenAPI document declares it', () => {
    expect(API_BASE).toBe('')
    expect(imageUrl('abc')).toBe('/images/abc')
    expect(streamPath('t')).toBe('/stream/t')
  })

  it('uses w, not width, on the image proxy', () => {
    // Getting this wrong fails silently: FastAPI ignores an undeclared query
    // parameter, so `?width=780` returns 200 carrying the 342 default.
    expect(imageUrl('abc', 780)).toBe('/images/abc?w=780')
    expect(imageUrl('abc', 780)).not.toContain('width=')
  })
})

describe('problem parsing', () => {
  it.each(ALL_CODES)('turns a %s document into a typed UsherProblem', async (code) => {
    const doc = PROBLEMS[code]()
    server.use(problemHandler('get', '/home', doc))

    await expect(request('/home')).rejects.toBeInstanceOf(UsherProblem)
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')

    expect(error.status).toBe(doc.status)
    expect(error.code).toBe(code)
    expect(error.knownCode).toBe(code)
    // `detail` is prose the server may reword at any release. It is carried
    // verbatim and never parsed.
    expect(error.detail).toBe(doc.detail)
    expect(error.instance).toBe(doc.instance)
    expect(error.body).toEqual(doc)
  })

  it('carries errors[] for validation_failed and nothing for the others', async () => {
    server.use(problemHandler('post', '/admin/sources', PROBLEMS.validation_failed()))
    const error = await request('/admin/sources', { method: 'POST', body: {} }).catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(Array.isArray(error.errors)).toBe(true)

    server.use(problemHandler('get', '/home', PROBLEMS.not_found()))
    const other = await request('/home').catch((e: unknown) => e)
    if (!(other instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(other.errors).toBeUndefined()
  })

  it('parses Retry-After off the response, where it lives', async () => {
    server.use(problemHandler('get', '/home', PROBLEMS.source_unavailable(), { retryAfter: 90 }))
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.retryAfter).toBe(90)
  })

  it('leaves retryAfter null when the server sent no header', async () => {
    server.use(problemHandler('get', '/home', PROBLEMS.source_unavailable()))
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.retryAfter).toBeNull()
  })

  it('keeps a code outside the seven readable while refusing to narrow it', async () => {
    server.use(
      http.get('/home', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Too Many Requests',
            status: 429,
            code: 'rate_limited',
            detail: 'Slow down.',
            instance: '/home',
          },
          { status: 429, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.code).toBe('rate_limited')
    expect(error.knownCode).toBeNull()
    // `code`, `status` and `detail` are still renderable, which is all
    // patterns.md §3 requires of a problem with no row in the table.
    expect(error.status).toBe(429)
    expect(error.detail).toBe('Slow down.')
  })

  it('survives a non-2xx that is not a document at all', async () => {
    server.use(http.get('/home', () => new HttpResponse('<html>502</html>', { status: 502 })))
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.status).toBe(502)
    expect(error.code).toBe('unknown')
    expect(error.knownCode).toBeNull()
    expect(error.message).toBe('HTTP 502')
  })
})

describe('the trace id', () => {
  // The W3C example value, and the shape Usher's middleware really sends:
  // `00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>`.
  const TRACE_ID = '0af7651916cd43dd8448eb211c80319c'
  const TRACERESPONSE = `00-${TRACE_ID}-b7ad6b7169203331-01`

  /** A 200 carrying whatever `traceresponse` a test wants — or none. */
  function ok(traceresponse?: string) {
    return http.get('/home', () =>
      HttpResponse.json({ rows: [] }, traceresponse === undefined ? {} : { headers: { traceresponse } }),
    )
  }

  it('parses it off a failure, which is what Problem renders the link from', async () => {
    server.use(
      http.get('/home', () =>
        problemResponse(PROBLEMS.source_unavailable(), { traceresponse: TRACERESPONSE }),
      ),
    )
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.traceId).toBe(TRACE_ID)
  })

  it('is null when the response carried no header', async () => {
    // Not `''`, and not the string "null". `useTraceUrl()` turns this into an
    // absent link rather than a dead one, which is the whole point of the
    // distinction — a deployment behind something that strips the header, or
    // an old backend, must read as "no trace" and not as "trace ''".
    server.use(problemHandler('get', '/home', PROBLEMS.source_unavailable()))
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.traceId).toBeNull()
  })

  it('is null for a malformed header rather than a throw', async () => {
    // A diagnostic header is the last thing that may take a screen down. The
    // parse is defensive in `client.ts`'s house style, and the assertion that
    // the request *resolved into an UsherProblem at all* is what proves the
    // constructor did not throw on the way past.
    server.use(
      http.get('/home', () =>
        problemResponse(PROBLEMS.not_found(), { traceresponse: 'ff-nonsense-not-a-trace' }),
      ),
    )
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(error.traceId).toBeNull()
    expect(error.status).toBe(404)
  })

  it('reaches the journal on a success, not only on a failure', async () => {
    // The drawer exists to make a call diagnosable and a slow 200 is a call
    // worth diagnosing. There is no problem document on this response to carry
    // the id, which is the reason it is a header.
    server.use(ok(TRACERESPONSE))
    await request('/home')
    expect(devlog.getEntries()[0]?.traceId).toBe(TRACE_ID)
    expect(devlog.getEntries()[0]?.status).toBe(200)
  })

  it('reaches the journal on a failure too, with the same value the error carries', async () => {
    // One exchange, one id: the journal and the thrown error read the same
    // header through the same parser, so a drawer entry and the screen above
    // it cannot name different traces for one request.
    server.use(
      http.get('/home', () => problemResponse(PROBLEMS.not_found(), { traceresponse: TRACERESPONSE })),
    )
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    expect(devlog.getEntries()[0]?.traceId).toBe(error.traceId)
    expect(devlog.getEntries()[0]?.traceId).toBe(TRACE_ID)
  })

  it('journals null when a response carried none, rather than omitting the field', async () => {
    server.use(ok())
    await request('/home')
    const entry = devlog.getEntries()[0]
    expect(entry?.traceId).toBeNull()
    // The key is present and null rather than absent: the drawer distinguishes
    // "this response carried no trace id" from "this deployment has no Tempo",
    // and it can only do that if the absence is a value it can read.
    expect(entry !== undefined && 'traceId' in entry).toBe(true)
  })

  it('journals null on the transport path, where there was no response at all', async () => {
    server.use(transportFailure('get', '/home'))
    await expect(request('/home')).rejects.toBeDefined()
    expect(devlog.getEntries()[0]?.traceId).toBeNull()
    expect(devlog.getEntries()[0]?.status).toBe(0)
  })
})

describe('the content-type sniff', () => {
  it('marks a failure carrying application/problem+json', async () => {
    server.use(problemHandler('get', '/home', PROBLEMS.not_found()))
    await request('/home').catch(() => undefined)
    expect(devlog.getEntries()[0]?.problem).toBe(true)
  })

  it('does NOT mark a failure that merely sent application/json', async () => {
    // The header is the wire truth. A deployment whose spec still claims
    // `application/json` for its failures is a deployment whose failures are
    // not problem documents, whatever the document says.
    server.use(http.get('/home', () => HttpResponse.json(PROBLEMS.not_found(), { status: 404 })))
    await request('/home').catch(() => undefined)
    expect(devlog.getEntries()[0]?.problem).toBe(false)
    expect(devlog.getEntries()[0]?.status).toBe(404)
  })

  it('does not mark a 2xx, whatever it carries', async () => {
    await request('/home')
    expect(devlog.getEntries()[0]?.problem).toBe(false)
  })

  it('still parses the body of a problem sent as plain json', async () => {
    server.use(http.get('/home', () => HttpResponse.json(PROBLEMS.not_playable(), { status: 409 })))
    const error = await request('/home').catch((e: unknown) => e)
    if (!(error instanceof UsherProblem)) throw new Error('expected an UsherProblem')
    // The sniff decides how the *journal* labels it, not whether the client
    // can read it.
    expect(error.knownCode).toBe('not_playable')
  })
})

describe('the status-0 transport path', () => {
  it('journals a failed request rather than losing it', async () => {
    server.use(transportFailure('get', '/home'))
    await expect(request('/home')).rejects.toBeDefined()

    const entry = devlog.getEntries()[0]
    // "The request never left" and "the server said nothing" look identical to
    // a user staring at a spinner. Status 0 is how the drawer tells them apart.
    expect(entry?.status).toBe(0)
    expect(entry?.method).toBe('GET')
    expect(entry?.path).toBe('/home')
    expect(entry?.problem).toBe(false)
    expect(typeof entry?.response).toBe('string')
  })

  it('rejects with something that is NOT an UsherProblem', async () => {
    server.use(transportFailure('get', '/home'))
    const error = await request('/home').catch((e: unknown) => e)
    // A surface that only handles `UsherProblem` renders nothing here, which is
    // exactly the case this assertion exists to keep visible.
    expect(error).not.toBeInstanceOf(UsherProblem)
    expect(error).toBeInstanceOf(Error)
  })

  it('still redacts the request body it never managed to send', async () => {
    server.use(transportFailure('post', '/admin/sources'))
    await request('/admin/sources', {
      method: 'POST',
      body: { name: 'Loft', password: 'hunter2' },
    }).catch(() => undefined)
    expect(JSON.stringify(devlog.getEntries()[0]?.request)).not.toContain('hunter2')
  })
})

describe('redaction', () => {
  it('keeps an Emby password out of the journal', async () => {
    await request('/admin/sources', {
      method: 'POST',
      body: {
        kind: 'emby',
        name: 'Loft Emby',
        base_url: 'http://192.168.50.61:8096',
        username: 'usher',
        password: 'correct-horse-battery-staple',
      },
    })

    const entry = devlog.getEntries()[0]
    const serialised = JSON.stringify(entry?.request)
    expect(serialised).not.toContain('correct-horse-battery-staple')
    expect(serialised).toContain('<redacted>')
    // The fields around it survive: a journal that redacted the whole body
    // would be no more useful than no journal.
    expect(serialised).toContain('Loft Emby')
    expect(serialised).toContain('192.168.50.61')
  })

  it('keeps a playback ticket out of the journal, in both target arms', async () => {
    await request(`/titles/${TITLE_ENRICHED}/play`, { method: 'POST' })

    const serialised = JSON.stringify(devlog.getEntries()[0]?.response)
    expect(serialised).not.toContain(PLAYBACK_TICKET)
    expect(serialised).toContain(PLAYBACK_TICKET_PLACEHOLDER)
    // Two arms, two spellings. The deep link percent-encodes the separators, so
    // a `.includes('/stream/')` check catches the direct target and silently
    // misses this one.
    expect(serialised).not.toContain('infuse://')
    expect(serialised.match(/300 s playback ticket/g)).toHaveLength(2)
  })

  it('hands the caller the real ticket even though the journal never saw it', async () => {
    const play = await request<{ targets: { url: string }[] }>(`/titles/${TITLE_ENRICHED}/play`, {
      method: 'POST',
    })
    // Redaction is what the journal holds, not what the app gets, or the player
    // would have nothing to play.
    expect(play.targets[0]?.url).toBe(directTicketUrl)
  })

  it('redacts the ticket out of a request path without losing the route', async () => {
    server.use(http.get('/stream/:ticket', () => new HttpResponse(null, { status: 204 })))
    await request(`/stream/${PLAYBACK_TICKET}`)

    const path = devlog.getEntries()[0]?.path ?? ''
    expect(path).not.toContain(PLAYBACK_TICKET)
    expect(path).toContain('/stream/')
    expect(path).toContain(PLAYBACK_TICKET_PLACEHOLDER)
  })

  it('is idempotent, so redacting twice changes nothing', () => {
    const once = redact({ password: 'x', url: directTicketUrl })
    expect(redact(once)).toEqual(once)
  })

  it('leaves a source base_url alone — it is not a ticket', () => {
    const kept = redact({ base_url: 'http://192.168.50.40:8096', device_id: 'usher-4f2a' })
    expect(kept).toEqual({ base_url: 'http://192.168.50.40:8096', device_id: 'usher-4f2a' })
  })

  it('recurses through arrays and nested objects', () => {
    expect(redact({ targets: [{ url: deepLinkUrl }, { nested: { token: 'abc' } }] })).toEqual({
      targets: [{ url: PLAYBACK_TICKET_PLACEHOLDER }, { nested: { token: '<redacted>' } }],
    })
  })
})

describe('ticketOf', () => {
  it('lifts the ticket out of a direct target', () => {
    expect(ticketOf(directTicketUrl)).toBe(PLAYBACK_TICKET)
  })

  it('lifts the same ticket out of the deep link that wraps it', () => {
    expect(ticketOf(deepLinkUrl)).toBe(PLAYBACK_TICKET)
  })

  it('refuses to guess when there is no /stream/ segment', () => {
    // The only other thing it could be is a source URL carrying somebody's
    // session token, so this reports rather than plays.
    expect(ticketOf('http://192.168.50.40:8096/Videos/42/stream.mkv?api_key=secret')).toBeNull()
    expect(ticketOf('not a url at all')).toBeNull()
  })
})

describe('204 and the body', () => {
  it('does not try to parse a body out of a 204', async () => {
    server.use(http.delete('/admin/sources/:id', () => new HttpResponse(null, { status: 204 })))
    await expect(request('/admin/sources/x', { method: 'DELETE' })).resolves.toBeNull()
    expect(devlog.getEntries()[0]?.status).toBe(204)
  })
})

describe('loadOperationTemplates', () => {
  it('teaches the journal every template the document declares', async () => {
    await loadOperationTemplates()
    expect(devlog.matchTemplate(`/titles/${TITLE_ENRICHED}/similar`)).toBe('/titles/{title_id}/similar')
    expect(openapiPaths).toContain('/admin/bootstrap/status')
  })

  it('survives a missing document without taking the app down', async () => {
    server.use(transportFailure('get', '/openapi.json'))
    await expect(loadOperationTemplates()).resolves.toBeUndefined()
  })

  it('names the operation on a real failing call', async () => {
    await loadOperationTemplates()
    await request(`/titles/${TITLE_NOT_PLAYABLE}/play`, { method: 'POST' }).catch(() => undefined)
    expect(devlog.getEntries()[0]?.template).toBe('/titles/{title_id}/play')
    expect(devlog.exercised()).toContain('POST /titles/{title_id}/play')
  })
})
