/**
 * The journal, and the one bug in it that a plausible refactor puts straight
 * back.
 *
 * `setTemplates` checks literal templates before parameterised ones and sorts
 * only the parameterised half by length. Replacing that with a single
 * longest-first sort looks like a simplification and is not: it is the
 * `/admin/bootstrap/status` defect, and its symptom is a verification tool
 * reporting a permanent false negative about itself.
 */

import { beforeEach, describe, expect, it } from 'vitest'
import * as devlog from './devlog'
import { PLAYBACK_TICKET_PLACEHOLDER } from './redact'
import { openapiPaths } from '@/test/fixtures/meta'
import { PLAYBACK_TICKET, TITLE_ENRICHED } from '@/test/fixtures/ids'
import { directTicketUrl, playTargets } from '@/test/fixtures/play'

beforeEach(() => {
  devlog.resetForTests()
  devlog.setTemplates(openapiPaths)
})

function log(overrides: Partial<Omit<devlog.LogEntry, 'id'>> = {}) {
  return devlog.record({
    method: 'GET',
    path: '/home',
    template: '/home',
    status: 200,
    ms: 12,
    startedAt: Date.now(),
    response: null,
    request: undefined,
    problem: false,
    traceId: null,
    ...overrides,
  })
}

describe('the template matcher', () => {
  it('matches a literal template exactly', () => {
    expect(devlog.matchTemplate('/home')).toBe('/home')
    expect(devlog.matchTemplate('/health/ready')).toBe('/health/ready')
  })

  it('matches a parameterised template and names it', () => {
    expect(devlog.matchTemplate(`/titles/${TITLE_ENRICHED}`)).toBe('/titles/{title_id}')
    expect(devlog.matchTemplate(`/people/${TITLE_ENRICHED}`)).toBe('/people/{person_id}')
  })

  it('prefers the longer parameterised template, so a suffix is not swallowed', () => {
    expect(devlog.matchTemplate(`/titles/${TITLE_ENRICHED}/similar`)).toBe('/titles/{title_id}/similar')
    expect(devlog.matchTemplate(`/titles/${TITLE_ENRICHED}/play`)).toBe('/titles/{title_id}/play')
  })

  /**
   * The one a length sort gets wrong.
   *
   * `/admin/bootstrap/{phase}` is 24 characters; the literal
   * `/admin/bootstrap/status` is 23. Longest-first therefore tries the
   * placeholder first and `[^/]+` happily matches `status`, so that request
   * gets journalled under an operation key that does not exist and its row on
   * the coverage page can never turn green however many times it is called.
   */
  it('gives the literal /admin/bootstrap/status priority over /admin/bootstrap/{phase}', () => {
    expect(devlog.matchTemplate('/admin/bootstrap/status')).toBe('/admin/bootstrap/status')
    expect(devlog.matchTemplate('/admin/bootstrap/imdb')).toBe('/admin/bootstrap/{phase}')
    expect(devlog.matchTemplate('/admin/bootstrap/all')).toBe('/admin/bootstrap/{phase}')
  })

  it('proves the premise: the placeholder really is the longer string', () => {
    // Without this the test above passes for a build where the two happen to
    // sort the other way, and asserts nothing about the ordering rule.
    expect('/admin/bootstrap/{phase}'.length).toBeGreaterThan('/admin/bootstrap/status'.length)
  })

  it('does not let a placeholder swallow a slash', () => {
    // `{...}` matches one segment. `/titles/a/b/c` matches no declared template.
    expect(devlog.matchTemplate('/titles/a/b/c')).toBeNull()
  })

  it('escapes the literal half, so a dot is a dot', () => {
    devlog.setTemplates(['/openapi.json', '/titles/{title_id}'])
    expect(devlog.matchTemplate('/openapi.json')).toBe('/openapi.json')
    expect(devlog.matchTemplate('/openapiXjson')).toBeNull()
  })

  it('strips the query before matching', () => {
    expect(devlog.matchTemplate('/search?q=stalker&mode=fused')).toBe('/search')
  })

  it('answers null for a path no template covers', () => {
    expect(devlog.matchTemplate('/nope')).toBeNull()
  })
})

describe('the store', () => {
  it('keeps entries newest first', () => {
    log({ path: '/a', template: null })
    log({ path: '/b', template: null })
    expect(devlog.getEntries().map((e) => e.path)).toEqual(['/b', '/a'])
  })

  it('notifies subscribers and stops after unsubscribe', () => {
    let calls = 0
    const unsubscribe = devlog.subscribe(() => {
      calls += 1
    })
    log()
    expect(calls).toBe(1)
    unsubscribe()
    log()
    expect(calls).toBe(1)
  })

  it('trims to 300 entries so the drawer stays readable', () => {
    for (let i = 0; i < 310; i += 1) log({ path: `/n/${i}`, template: null })
    expect(devlog.getEntries()).toHaveLength(300)
    expect(devlog.getEntries()[0]?.path).toBe('/n/309')
  })

  /**
   * Coverage accumulates **separately** from `entries`, and that is the whole
   * point: deriving it from the trimmed list would make a page verified an hour
   * ago go red once you browsed enough to push it off the end. Coverage that
   * decreases as you test more is worse than none.
   */
  it('accumulates coverage separately, so trimming cannot lose it', () => {
    log({ method: 'POST', path: '/home', template: '/home' })
    for (let i = 0; i < 320; i += 1) log({ path: `/n/${i}`, template: null })

    expect(devlog.getEntries().some((e) => e.template === '/home')).toBe(false)
    expect(devlog.exercised()).toContain('POST /home')
  })

  it('survives clear(), which empties the drawer and not the coverage', () => {
    log({ method: 'GET', path: '/home', template: '/home' })
    devlog.clear()
    expect(devlog.getEntries()).toHaveLength(0)
    expect(devlog.exercised()).toContain('GET /home')
  })

  it('hands back a copy of the coverage set, not the set itself', () => {
    log({ template: '/home' })
    const seen = devlog.exercised()
    seen.clear()
    expect(devlog.exercised()).toContain('GET /home')
  })
})

describe('redaction at the record boundary', () => {
  /**
   * Not only in `client.ts`. The reference client's player called `record`
   * directly, so a rule enforced only on the way through the client would have
   * written a live 300-second ticket into the journal on every press of play.
   */
  it('redacts a ticket a caller journalled without going through the client', () => {
    const entry = log({
      method: 'POST',
      path: `/titles/${TITLE_ENRICHED}/play`,
      template: '/titles/{title_id}/play',
      response: playTargets,
    })
    const serialised = JSON.stringify(entry.response)
    expect(serialised).not.toContain(PLAYBACK_TICKET)
    expect(serialised).toContain(PLAYBACK_TICKET_PLACEHOLDER)
  })

  it('redacts a credential a caller journalled directly', () => {
    const entry = log({ request: { username: 'usher', password: 'hunter2' } })
    expect(JSON.stringify(entry.request)).not.toContain('hunter2')
  })

  it('redacts the ticket out of the path while keeping the route legible', () => {
    const entry = log({ path: `/stream/${PLAYBACK_TICKET}`, template: '/stream/{ticket}' })
    expect(entry.path).not.toContain(PLAYBACK_TICKET)
    expect(entry.path.startsWith('/stream/')).toBe(true)
  })

  it('leaves everything else in the entry untouched', () => {
    const entry = log({ response: { url: directTicketUrl, name: 'Stalker', year: 1979 } })
    expect(entry.response).toEqual({
      url: PLAYBACK_TICKET_PLACEHOLDER,
      name: 'Stalker',
      year: 1979,
    })
  })
})
