/**
 * The `/admin/*` surface: sources, the review queue, bootstrap and row
 * providers.
 *
 * Three facts these fixtures are shaped to make testable:
 *
 * · **Every mutating admin action answers `202 {kind, key}` and there is no
 *   route to look that key up** (patterns.md §6, §15 item 3). "Queued", the key
 *   in mono and selectable, and a pointer at where evidence will appear.
 * · **`is_administrator: true` is a risk surface, not a success** — warn tone,
 *   "Usher holds an administrator session on this server." `device_id` is
 *   deliberately visible: it is how you revoke that session in Emby's own
 *   dashboard.
 * · **Bootstrap reports a cursor, never a percentage.** `rows_seen`,
 *   `rows_written` and `position` and no total, because there is no honest
 *   denominator to divide by.
 */

import type {
  BootstrapStatusResponse,
  BootstrapTriggerResponse,
  RowProviderResponse,
  SourceResponse,
  SourceStatusResponse,
  SyncTriggerResponse,
  UnmatchedResponse,
} from '@/api'
import type { Schemas } from '@/api'
import {
  MEDIA_ITEM_UNMATCHED,
  MEDIA_ITEM_UNMATCHED_2,
  SOURCE_LIVING_ROOM,
  SOURCE_UNREACHABLE,
  TITLE_ENRICHED,
} from './ids'

/* ----------------------------------------------------------------- sources */

/**
 * No `password` anywhere in this shape, and that is the contract: credentials
 * are write-only in the UI and the API never returns them. `device_id` is here
 * on purpose — see the file header.
 */
export const sourceLivingRoom: SourceResponse = {
  id: SOURCE_LIVING_ROOM,
  kind: 'emby',
  name: 'Living Room Emby',
  base_url: 'http://192.168.50.40:8096',
  device_id: 'usher-4f2a9c1e-living-room',
  enabled: true,
  supports_push: true,
  created_at: '2026-07-11T18:22:04Z',
}

export const sourceUnreachable: SourceResponse = {
  id: SOURCE_UNREACHABLE,
  kind: 'emby',
  name: 'Loft Emby',
  base_url: 'http://192.168.50.61:8096',
  device_id: 'usher-8b7d3e5f-loft',
  enabled: true,
  supports_push: false,
  created_at: '2026-08-02T09:15:47Z',
}

export const sources: SourceResponse[] = [sourceLivingRoom, sourceUnreachable]

/**
 * Reachable, authenticated, pushing — and holding an administrator session,
 * which the UI must warn about rather than celebrate.
 */
export const sourceStatusHealthy: SourceStatusResponse = {
  reachable: true,
  authenticated: true,
  push_available: true,
  is_administrator: true,
  server_version: '4.9.5.0',
  detail: null,
}

/**
 * Unreachable. The three booleans that depend on reaching the server are
 * `null` — never asked — rather than `false`, which would claim we asked and
 * the answer was no. `detail` is the server's own words and is shown verbatim.
 */
export const sourceStatusUnreachable: SourceStatusResponse = {
  reachable: false,
  authenticated: false,
  push_available: null,
  is_administrator: null,
  server_version: null,
  detail: 'Connection refused after 5.0 s (http://192.168.50.61:8096/System/Info)',
}

/** 202. `kind` is the queue's own vocabulary; `key` is what coalescing keys on. */
export const syncQueued: SyncTriggerResponse = {
  kind: 'sync',
  key: `sync:full:${SOURCE_LIVING_ROOM}`,
}

export const syncDeltaQueued: SyncTriggerResponse = {
  kind: 'sync',
  key: `sync:delta:${SOURCE_LIVING_ROOM}`,
}

/* -------------------------------------------------------------- unmatched */

/**
 * The review queue. This is §15 item 5's surface: `filename`, `container`,
 * `resolution`, `runtime_seconds`, `library_name` and the matcher's candidate
 * scores are all **missing from this DTO**, which is why the review screen is
 * labelled REQUIRES BACKEND WORK on screen with the missing fields printed in
 * mono. The fixture carries only what the API really sends, so nobody builds
 * against a shape that does not exist.
 */
export const unmatchedPageOne: UnmatchedResponse = {
  items: [
    {
      id: MEDIA_ITEM_UNMATCHED,
      source_id: SOURCE_LIVING_ROOM,
      external_id: '7f3a91c4e8b2',
      added_at: '2026-08-15T02:41:18Z',
      last_seen_at: '2026-08-18T03:10:02Z',
      available: true,
    },
    {
      id: MEDIA_ITEM_UNMATCHED_2,
      source_id: SOURCE_LIVING_ROOM,
      external_id: 'b18c62d9f047',
      // `added_at` is nullable: an item first seen by a delta walk has no
      // recorded arrival, and `null` says that rather than guessing one.
      added_at: null,
      last_seen_at: '2026-08-18T03:10:02Z',
      available: false,
    },
  ],
  next_cursor: 'eyJrIjoibGFzdF9zZWVuIiwidiI6IjIwMjYtMDgtMTgiLCJoIjoiYzMxYSJ9',
}

export const unmatchedPageTwo: UnmatchedResponse = {
  items: [
    {
      id: '0191f4cf-2e47-7075-a1b0-eb526d7e8f90',
      source_id: SOURCE_UNREACHABLE,
      external_id: '4c92e7a1b380',
      added_at: '2026-08-17T22:03:55Z',
      last_seen_at: '2026-08-18T03:10:02Z',
      available: true,
    },
  ],
  next_cursor: null,
}

export const unmatchedEmpty: UnmatchedResponse = { items: [], next_cursor: null }

export const resolved: Schemas['ResolvedItemResponse'] = {
  id: MEDIA_ITEM_UNMATCHED,
  title_id: TITLE_ENRICHED,
  episode_id: null,
}

/* -------------------------------------------------------------- bootstrap */

/**
 * ⚠️ **`dataset` is the wire name, never a phase name, and these fixtures said
 * otherwise for the whole of the console's life.** They spelled it `imdb`,
 * `movielens` and `crosswalk` — the six phase values — while the real server
 * sends `imdb.title.basics`, `imdb.title.ratings`, `imdb.credit_names`,
 * `imdb.title.akas`, `tmdb.ids.movie`, `tmdb.ids.series`, `wikidata.crosswalk`
 * and `movielens.genome`. Eight names, six phases, and no overlap between the
 * two sets.
 *
 * `Bootstrap.tsx` matched a phase against `run.dataset`, so on a real
 * deployment every phase read "never run" on a fully imported catalog and no
 * duration was ever "measured on this deployment". **Every test passed**,
 * because the fixture was spelled the way the screen wished the server were.
 * A fake that diverges from its real arm does not fail — it certifies.
 *
 * `phase` is the wire field that closes it (`ImportRunResponse.phase`, read
 * from `usher.domain.bootstrap.DATASET_PHASES`), and it is what the screen
 * matches on now. `npm run gen:types` against a live server is what keeps the
 * *shape* honest; only using the real names keeps the *values* honest, and
 * nothing generates those.
 */

/**
 * One run mid-flight and one finished, which is what the cursor-progress idiom
 * needs both of.
 *
 * `heartbeat_at` is on the wire beside `finished_at` because it is the only
 * field that distinguishes a `running` row whose importer is alive from one
 * whose process died — patterns.md §8 turns a `heartbeat_at` older than 120 s
 * into "Stalled?", **with the question mark**, because the API states a
 * timestamp and the inference is ours.
 */
export const importRunning: Schemas['ImportRunResponse'] = {
  dataset: 'imdb.title.basics',
  phase: 'imdb',
  status: 'running',
  revision: '2026-08-18',
  position: 418_002,
  rows_seen: 418_002,
  rows_written: 411_774,
  error: null,
  started_at: '2026-08-18T02:00:00Z',
  heartbeat_at: '2026-08-18T03:09:41Z',
  finished_at: null,
}

export const importCompleted: Schemas['ImportRunResponse'] = {
  dataset: 'movielens.genome',
  phase: 'movielens',
  status: 'completed',
  revision: 'ml-25m',
  position: 62_423,
  rows_seen: 62_423,
  rows_written: 61_990,
  error: null,
  started_at: '2026-08-17T23:04:12Z',
  heartbeat_at: '2026-08-17T23:19:38Z',
  finished_at: '2026-08-17T23:19:38Z',
}

/**
 * A `failed` run is a **normal, designed state** (patterns.md §8): the status
 * word gets bad tone, `error` is shown verbatim, the position is retained and
 * the trigger is relabelled "Resume".
 */
export const importFailed: Schemas['ImportRunResponse'] = {
  dataset: 'wikidata.crosswalk',
  phase: 'crosswalk',
  status: 'failed',
  revision: '2026-08-16',
  position: 88_140,
  rows_seen: 88_140,
  rows_written: 87_002,
  error: 'wdqs: HTTP 429 after 3 retries (query timeout 60 s)',
  started_at: '2026-08-16T01:00:00Z',
  heartbeat_at: '2026-08-16T02:41:07Z',
  finished_at: '2026-08-16T02:41:09Z',
}

/**
 * Every field a count, none a percentage. The one ratio that matters is
 * `enriched_with_vector` over `enriched`, and the route declines to do that
 * division so a client picks its own denominator rather than inheriting one.
 */
export const bootstrapStatus: BootstrapStatusResponse = {
  titles: 1_272_869,
  runs: [importRunning, importCompleted, importFailed],
  genome: {
    with_vector: 128_400,
    titles: 1_272_869,
    movies: 604_118,
    enriched: 130_647,
    enriched_with_vector: 128_400,
    revisions: [{ revision: 'genome-2026-08-13', vectors: 128_400 }],
  },
  vocabulary: {
    state: 'named',
    tags: 1_128,
    detail: null,
  },
}

/** Nothing has ever been imported. Not an error — a fact about the database. */
export const bootstrapStatusEmpty: BootstrapStatusResponse = {
  titles: 0,
  runs: [],
  genome: {
    with_vector: 0,
    titles: 0,
    movies: 0,
    enriched: 0,
    enriched_with_vector: 0,
    revisions: [],
  },
  vocabulary: {
    state: 'no_vectors',
    tags: null,
    detail: 'No embeddings exist, so the tag vocabulary cannot be checked.',
  },
}

/** 202. `key` is the phase's own wire value, so a caller can watch for it. */
export const bootstrapQueued: BootstrapTriggerResponse = {
  kind: 'bootstrap',
  key: 'imdb',
}

export const bootstrapAllQueued: BootstrapTriggerResponse = {
  kind: 'bootstrap',
  key: 'all',
}

/* ---------------------------------------------------------- row providers */

/**
 * The ten providers `/home` composes over. The slugs are opaque, so a switch
 * rendering one needs a description in plain language beside it — patterns.md
 * §12's rule for `role="switch"`.
 *
 * ⚠️ **These are the real ten, read out of the backend rather than invented.**
 * An earlier version of this fixture carried `collections-to-complete`,
 * `unfinished-series`, `top-rated-unwatched`, `from-the-year-you-watch-most`
 * and `people-you-follow` — five slugs no provider registers. A fixture is a
 * claim about the server, and a test that passes against a vocabulary the
 * server does not have is a test that agrees with itself. Verified by
 * instantiating every `*Provider` in `usher.services.rows` and reading
 * `slug_prefix`; the order below is that same alphabetical order.
 *
 * `franchise`, `people` and `seasonal` are the three whose emitted row slugs
 * are **prefixes** rather than literals — a real row reads `franchise-<uuid>` —
 * which is why the screen explains what a prefix governs.
 */
export const rowProviders: RowProviderResponse[] = [
  { slug: 'because-you-watched', enabled: true },
  { slug: 'continue-watching', enabled: true },
  { slug: 'curated', enabled: true },
  { slug: 'franchise', enabled: true },
  { slug: 'genre-affinity', enabled: true },
  { slug: 'next-up', enabled: true },
  { slug: 'people', enabled: true },
  { slug: 'recently-added', enabled: true },
  { slug: 'rediscover', enabled: false },
  { slug: 'seasonal', enabled: false },
]

export const rowProviderDisabled: RowProviderResponse = {
  slug: 'rediscover',
  enabled: false,
}

export const rowProviderEnabled: RowProviderResponse = {
  slug: 'rediscover',
  enabled: true,
}

/**
 * A slug this console has no description for. Real in shape, absent from the
 * registry — the case that proves the screen states its own ignorance instead
 * of guessing at what an unknown provider does.
 */
export const rowProviderUnknown: RowProviderResponse = {
  slug: 'a-provider-this-console-has-never-heard-of',
  enabled: true,
}

/** 202, keyed on the household rather than on the request. */
export const regenerateQueued: Schemas['RegenerateResponse'] = {
  kind: 'curate',
  key: '0191f4d0-3f58-7186-b2cf-fc637e8f9a01',
}
