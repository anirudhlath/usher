/**
 * `GET /health`, `GET /health/ready` and `GET /meta/attribution`.
 *
 * **`/health/ready` is one of exactly two routes exempt from Usher's RFC 9457
 * envelope**, and the degraded fixture below is why that matters: the 503
 * carries the *same* `ReadinessResponse` shape as the 200 and reports which
 * check failed. So a degraded deployment is a degraded *render* — the panel
 * shows which lane is down — rather than a page that disappears into an error
 * box. `readinessFromError` in `hooks.ts` is what pulls it back out.
 */

import type { AttributionResponse, LivenessResponse, ReadinessResponse } from '@/api'

/** Liveness is always 200 and says nothing about whether anything works. */
export const liveness: LivenessResponse = { status: 'ok' }

export const readinessReady: ReadinessResponse = {
  status: 'ready',
  checks: { database: true, migrations: true },
  lanes: { push: ['Living Room Emby'], worker: true },
}

/**
 * The 503. Migrations are behind and the worker lane is not running, so the
 * deployment is up and answering while being honest that it cannot do the
 * work. Both are shown; neither is inferred from the status code.
 */
export const readinessDegraded: ReadinessResponse = {
  status: 'degraded',
  checks: { database: true, migrations: false },
  lanes: { push: [], worker: false },
}

/**
 * A 503 whose body is *not* a readiness document — a proxy's own error page,
 * say. This is the case `readinessFromError` must return `null` for, so the
 * surface falls back to the error treatment rather than rendering half a panel
 * out of a shape it guessed at.
 */
export const readinessNotADocument = { error: 'upstream connect error' }

/**
 * A licensing requirement, not a credit roll. Usher ships importers and never
 * data — IMDb and TMDb both prohibit redistribution — and these strings staying
 * in the API surface is half of how that stays true.
 */
export const attribution: AttributionResponse = [
  {
    source: 'TMDb',
    text: 'This product uses the TMDb API but is not endorsed or certified by TMDb.',
  },
  {
    source: 'IMDb',
    text: 'Information courtesy of IMDb (https://www.imdb.com). Used with permission.',
  },
  {
    source: 'MovieLens',
    text: 'This product uses the MovieLens dataset from GroupLens Research at the University of Minnesota.',
  },
  {
    source: 'Wikidata',
    text: 'Contains information from Wikidata, which is made available under the Creative Commons CC0 License.',
  },
]

/**
 * A minimal but well-formed `/openapi.json`, enough for
 * `loadOperationTemplates` to teach the journal its path templates.
 *
 * It carries `/admin/bootstrap/status` **and** `/admin/bootstrap/{phase}`
 * together on purpose: that pair is the one a longest-first sort gets wrong,
 * and `devlog.test.ts` asserts against exactly these two entries.
 */
export const openapiDocument = {
  openapi: '3.1.0',
  info: { title: 'Usher', version: '0.1.0' },
  paths: {
    '/home': {},
    '/browse': {},
    '/search': {},
    '/search/suggest': {},
    '/titles/{title_id}': {},
    '/titles/{title_id}/similar': {},
    '/titles/{title_id}/play': {},
    '/episodes/{episode_id}': {},
    '/episodes/{episode_id}/play': {},
    '/series/{title_id}/seasons': {},
    '/seasons/{season_id}/episodes': {},
    '/people/{person_id}': {},
    '/collections/{collection_id}': {},
    '/images/{image_id}': {},
    '/stream/{ticket}': {},
    '/events': {},
    '/watch/titles/{title_id}': {},
    '/watch/titles/{title_id}/played': {},
    '/watch/episodes/{episode_id}': {},
    '/admin/sources': {},
    '/admin/sources/{source_id}': {},
    '/admin/sources/{source_id}/status': {},
    '/admin/sources/{source_id}/sync': {},
    '/admin/unmatched': {},
    '/admin/unmatched/{media_item_id}/resolve': {},
    '/admin/bootstrap/status': {},
    '/admin/bootstrap/{phase}': {},
    '/admin/rows/providers': {},
    '/admin/rows/providers/{slug}': {},
    '/admin/rows/regenerate': {},
    '/health': {},
    '/health/ready': {},
    '/meta/attribution': {},
  },
}

/** The path templates that document declares, in declaration order. */
export const openapiPaths: string[] = Object.keys(openapiDocument.paths)
