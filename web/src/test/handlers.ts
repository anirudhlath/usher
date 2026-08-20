/**
 * The default happy path: every route Usher declares, answering with the
 * fixtures in `fixtures/`.
 *
 * Two rules this file follows so that tests stay readable:
 *
 * · **Paths are written exactly as `/openapi.json` declares them.** There is no
 *   prefix, in the app or here — the console is served by Usher's own app at
 *   `/console/` and is therefore same-origin with the API — so a handler path
 *   and the string in `hooks.ts` are the same string. A handler that needed a
 *   rewrite would be evidence of a bug rather than of configuration.
 *
 * · **Variation is selected by the request, not by a separate handler.** The
 *   skeleton title, the never-computed similarity list, the second page of a
 *   keyset walk and the two `facets.computed: false` reasons are all reachable
 *   from this one array by asking for the right id, cursor or query — so a test
 *   that wants one of them names an id instead of assembling a server.
 *
 * `setup.ts` runs with `onUnhandledRequest: 'error'`, so a route missing here
 * fails the test that needed it instead of quietly reaching the network.
 */

import { http, HttpResponse, type PathParams } from 'msw'
import {
  attribution,
  bootstrapQueued,
  bootstrapStatus,
  browsePageOne,
  browsePageTwo,
  browseUnpredicated,
  browseWithFacets,
  collection,
  collectionUnowned,
  episodePilot,
  episodeSecond,
  home,
  liveness,
  notFound,
  notPlayable,
  openapiDocument,
  person,
  personWithoutGroups,
  playEpisodeTargets,
  playTargets,
  problemResponse,
  readinessDegraded,
  readinessReady,
  regenerateQueued,
  resolved,
  rowProviders,
  searchDowngraded,
  searchFullText,
  searchFused,
  seasonEpisodesPageOne,
  seasonEpisodesPageTwo,
  seasons,
  sourceLivingRoom,
  sourceStatusHealthy,
  sourceStatusUnreachable,
  sources,
  suggestFuzzy,
  suggestPrefix,
  syncDeltaQueued,
  syncQueued,
  titleEnriched,
  titleEnrichmentFailed,
  titleSeries,
  titleSkeleton,
  unmatchedPageOne,
  unmatchedPageTwo,
} from './fixtures'
import {
  COLLECTION_TRILOGY,
  MEDIA_ITEM_UNMATCHED,
  PERSON_DIRECTOR,
  SEASON_ONE,
  SOURCE_LIVING_ROOM,
  TITLE_ENRICHED,
  TITLE_NOT_PLAYABLE,
  TITLE_SERIES,
  TITLE_SIMILAR_EMPTY,
  TITLE_SIMILAR_NEVER,
  TITLE_SIMILAR_STALE,
  TITLE_SKELETON,
} from './fixtures/ids'
import { similarComputed, similarComputedEmpty, similarNeverComputed, similarStale } from './fixtures/titles'

/** One path parameter as a string. MSW types them `string | readonly string[]`. */
function param(params: PathParams, key: string): string {
  const value = params[key]
  if (typeof value === 'string') return value
  return Array.isArray(value) ? (value[0] ?? '') : ''
}

/** 204, which `client.ts` must not try to parse a body out of. */
function noContent() {
  return new HttpResponse(null, { status: 204 })
}

/**
 * 202. Every mutating admin action answers this shape and nothing may report it
 * as "done" — `{kind, key}` is a receipt for queued work, not a result.
 */
function accepted(body: { kind: string; key: string }) {
  return HttpResponse.json(body, { status: 202 })
}

export const handlers = [
  /* ------------------------------------------------------------- Screens */

  http.get('/home', () => HttpResponse.json(home)),

  /**
   * The keyset walk and the facet block, both driven off the query so one
   * handler covers four responses: page one, the last page, `facets=true` with
   * a filter (counts), and `facets=true` with none (`unpredicated`).
   */
  http.get('/browse', ({ request }) => {
    const url = new URL(request.url)
    if (url.searchParams.get('cursor')) return HttpResponse.json(browsePageTwo)
    if (url.searchParams.get('facets') === 'true') {
      const filtered = url.searchParams.has('genre') || url.searchParams.has('year')
      return HttpResponse.json(filtered ? browseWithFacets : browseUnpredicated)
    }
    return HttpResponse.json(browsePageOne)
  }),

  /**
   * `mode` selects the lane. The `semantic` arm answers the **downgrade**
   * fixture — `mode !== requested_mode` — because that is the response a screen
   * is most likely to get wrong, and a happy path that never produces it lets
   * that bug ship.
   */
  http.get('/search', ({ request }) => {
    const mode = new URL(request.url).searchParams.get('mode')
    if (mode === 'full_text') return HttpResponse.json(searchFullText)
    if (mode === 'semantic') return HttpResponse.json(searchDowngraded)
    return HttpResponse.json(searchFused)
  }),

  http.get('/search/suggest', ({ request }) => {
    const tier = new URL(request.url).searchParams.get('tier')
    return HttpResponse.json(tier === 'fuzzy' ? suggestFuzzy : suggestPrefix)
  }),

  /* ----------------------------------------------------------- Resources */

  http.get('/titles/:title_id', ({ params }) => {
    switch (param(params, 'title_id')) {
      case TITLE_ENRICHED:
        return HttpResponse.json(titleEnriched)
      case TITLE_SERIES:
        return HttpResponse.json(titleSeries)
      case TITLE_SKELETON:
        return HttpResponse.json(titleSkeleton)
      case TITLE_SIMILAR_EMPTY:
        return HttpResponse.json(titleEnrichmentFailed)
      default:
        return problemResponse(notFound(`/titles/${param(params, 'title_id')}`))
    }
  }),

  http.get('/titles/:title_id/similar', ({ params }) => {
    switch (param(params, 'title_id')) {
      case TITLE_SIMILAR_NEVER:
        return HttpResponse.json(similarNeverComputed)
      case TITLE_SIMILAR_EMPTY:
        return HttpResponse.json(similarComputedEmpty)
      case TITLE_SIMILAR_STALE:
        return HttpResponse.json(similarStale)
      default:
        return HttpResponse.json(similarComputed)
    }
  }),

  http.get('/series/:title_id/seasons', () => HttpResponse.json(seasons)),

  http.get('/seasons/:season_id/episodes', ({ request, params }) => {
    if (param(params, 'season_id') !== SEASON_ONE) {
      return HttpResponse.json({ items: [], next_cursor: null })
    }
    const cursor = new URL(request.url).searchParams.get('cursor')
    return HttpResponse.json(cursor ? seasonEpisodesPageTwo : seasonEpisodesPageOne)
  }),

  http.get('/episodes/:episode_id', ({ params }) => {
    const id = param(params, 'episode_id')
    if (id === episodePilot.id) return HttpResponse.json(episodePilot)
    if (id === episodeSecond.id) return HttpResponse.json(episodeSecond)
    return problemResponse(notFound(`/episodes/${id}`))
  }),

  http.get('/people/:person_id', ({ params }) => {
    const id = param(params, 'person_id')
    if (id === PERSON_DIRECTOR) return HttpResponse.json(person)
    if (id === personWithoutGroups.id) return HttpResponse.json(personWithoutGroups)
    return problemResponse(notFound(`/people/${id}`))
  }),

  http.get('/collections/:collection_id', ({ params }) => {
    const id = param(params, 'collection_id')
    if (id === COLLECTION_TRILOGY) return HttpResponse.json(collection)
    if (id === collectionUnowned.id) return HttpResponse.json(collectionUnowned)
    return problemResponse(notFound(`/collections/${id}`))
  }),

  /* ------------------------------------------------------------- Actions */

  /**
   * `TITLE_NOT_PLAYABLE` answers 409 rather than 200, because the shape of that
   * failure — panel scale, no retry button, "See other copies" — is the one a
   * play surface has to get right and is not reachable from a 200.
   */
  http.post('/titles/:title_id/play', ({ params }) => {
    const id = param(params, 'title_id')
    if (id === TITLE_NOT_PLAYABLE) return problemResponse(notPlayable(`/titles/${id}/play`))
    return HttpResponse.json(playTargets)
  }),

  http.post('/episodes/:episode_id/play', () => HttpResponse.json(playEpisodeTargets)),

  http.put('/watch/titles/:title_id', async ({ request }) => {
    const body: unknown = await request.json()
    return HttpResponse.json({
      position_seconds: readNumber(body, 'position_seconds'),
      played: readBoolean(body, 'played'),
      play_count: 1,
      last_played_at: '2026-08-18T21:14:02Z',
    })
  }),

  http.put('/watch/episodes/:episode_id', async ({ request }) => {
    const body: unknown = await request.json()
    return HttpResponse.json({
      position_seconds: readNumber(body, 'position_seconds'),
      played: readBoolean(body, 'played'),
      play_count: 1,
      last_played_at: '2026-08-18T21:14:02Z',
    })
  }),

  /** One route, two operations: `POST` marks played, `DELETE` marks unplayed. */
  http.post('/watch/titles/:title_id/played', () =>
    HttpResponse.json({
      position_seconds: 0,
      played: true,
      play_count: 2,
      last_played_at: '2026-08-18T21:14:02Z',
    }),
  ),
  http.delete('/watch/titles/:title_id/played', () =>
    HttpResponse.json({
      position_seconds: 0,
      played: false,
      play_count: 1,
      last_played_at: '2026-08-14T21:07:33Z',
    }),
  ),

  /* --------------------------------------------------------------- Admin */

  http.get('/admin/sources', () => HttpResponse.json(sources)),

  /** 201, and the response carries no credential — it never returns one. */
  http.post('/admin/sources', () => HttpResponse.json(sourceLivingRoom, { status: 201 })),

  http.delete('/admin/sources/:source_id', () => noContent()),

  http.get('/admin/sources/:source_id/status', ({ params }) =>
    HttpResponse.json(
      param(params, 'source_id') === SOURCE_LIVING_ROOM ? sourceStatusHealthy : sourceStatusUnreachable,
    ),
  ),

  http.post('/admin/sources/:source_id/sync', ({ request }) => {
    const kind = new URL(request.url).searchParams.get('kind')
    return accepted(kind === 'delta' ? syncDeltaQueued : syncQueued)
  }),

  http.get('/admin/unmatched', ({ request }) => {
    const cursor = new URL(request.url).searchParams.get('cursor')
    return HttpResponse.json(cursor ? unmatchedPageTwo : unmatchedPageOne)
  }),

  http.post('/admin/unmatched/:media_item_id/resolve', ({ params }) => {
    const id = param(params, 'media_item_id')
    if (id !== MEDIA_ITEM_UNMATCHED) {
      return problemResponse(notFound(`/admin/unmatched/${id}/resolve`))
    }
    return HttpResponse.json(resolved)
  }),

  /**
   * Registered before `/admin/bootstrap/:phase` deliberately. MSW matches in
   * array order, and this pair is the one a naive longest-first matcher gets
   * wrong — the same trap `devlog.setTemplates` documents at length: the
   * placeholder is longer than the literal and `[^/]+` will happily match
   * `status`.
   */
  http.get('/admin/bootstrap/status', () => HttpResponse.json(bootstrapStatus)),

  http.post('/admin/bootstrap/:phase', ({ params }) =>
    accepted({ kind: bootstrapQueued.kind, key: param(params, 'phase') }),
  ),

  http.get('/admin/rows/providers', () => HttpResponse.json(rowProviders)),

  http.put('/admin/rows/providers/:slug', async ({ request, params }) => {
    const body: unknown = await request.json()
    return HttpResponse.json({
      slug: param(params, 'slug'),
      enabled: readBoolean(body, 'enabled'),
    })
  }),

  http.post('/admin/rows/regenerate', () => accepted(regenerateQueued)),

  /* ---------------------------------------------------------------- Meta */

  http.get('/health', () => HttpResponse.json(liveness)),

  /**
   * 200 by default. The 503 is a *state* rather than an error and carries the
   * same `ReadinessResponse` shape, which a test reaches with
   * `server.use(degradedReadiness())`.
   */
  http.get('/health/ready', () => HttpResponse.json(readinessReady)),

  http.get('/meta/attribution', () => HttpResponse.json(attribution)),

  /** What `loadOperationTemplates` reads to name the journal's operations. */
  http.get('/openapi.json', () => HttpResponse.json(openapiDocument)),
]

function readNumber(source: unknown, key: string): number {
  if (source === null || typeof source !== 'object') return 0
  const value: unknown = Reflect.get(source, key)
  return typeof value === 'number' ? value : 0
}

function readBoolean(source: unknown, key: string): boolean {
  if (source === null || typeof source !== 'object') return false
  return Reflect.get(source, key) === true
}

/* ------------------------------------------------------------- overrides */

/**
 * The 503. Not a failure of the request — `/health/ready` is one of two routes
 * exempt from the RFC 9457 envelope precisely so the degraded body keeps the
 * same shape and names the check that failed.
 */
export function degradedReadiness() {
  return http.get('/health/ready', () => HttpResponse.json(readinessDegraded, { status: 503 }))
}

/** A `/browse` whose first page is already the last one. */
export function browseSinglePage() {
  return http.get('/browse', () => HttpResponse.json(browsePageTwo))
}
