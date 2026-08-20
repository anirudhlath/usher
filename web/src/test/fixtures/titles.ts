/**
 * `GET /titles/{id}`, its similarity list, and the series hierarchy.
 *
 * **The distinction this file exists to preserve**: on a title with no credits
 * and no artwork, `cast`, `crew` and `images` are *absent from the payload*,
 * not `[]`. `schema.d.ts` declares all three required — the route serialises
 * with `response_model_exclude_unset=True` and OpenAPI cannot say so — and
 * patterns.md §2 makes the difference a correctness rule:
 *
 *   absent  → "not applicable to this record" → em dash, one clause, no border
 *   `[]`    → "we looked and there is nothing" → solid hairline on `--bg-sunken`
 *
 * A component that renders both as a grey dash has lost a fact the API went to
 * some trouble to state. So `SkeletonTitle` below is `Omit<…>` rather than a
 * `TitleResponse` with three empty arrays, and any consumer that reads
 * `title.cast` without narrowing fails to compile.
 */

import type { EpisodeResponse, SeasonsResponse, SimilarResponse, TitleResponse } from '@/api'
import type { Schemas } from '@/api'
import {
  EPISODE_PILOT,
  EPISODE_SECOND,
  IMAGE_BACKDROP,
  IMAGE_LOGO,
  IMAGE_POSTER,
  PERSON_DIRECTOR,
  SEASON_ONE,
  SEASON_TWO,
  SOURCE_LIVING_ROOM,
  TITLE_ENRICHED,
  TITLE_SERIES,
  TITLE_SIMILAR_EMPTY,
  TITLE_SIMILAR_STALE,
  TITLE_SKELETON,
} from './ids'

/** One available copy, described by its container and codecs. */
export const availabilityLivingRoom: Schemas['AvailabilityResponse'] = {
  source_id: SOURCE_LIVING_ROOM,
  source: 'Living Room Emby',
  available: true,
  container: 'mkv',
  video_codec: 'hevc',
  hdr_format: 'HDR10',
  resolution: '3840x2160',
  runtime_seconds: 9_720,
}

/**
 * A badge for a copy that is **not** currently available — the row is present
 * whether or not the file is reachable, which is what lets a page say "you had
 * this and the drive is unmounted" rather than silently dropping the source.
 */
export const availabilityRetracted: Schemas['AvailabilityResponse'] = {
  source_id: SOURCE_LIVING_ROOM,
  source: 'Living Room Emby',
  available: false,
  container: null,
  video_codec: null,
  hdr_format: null,
  resolution: null,
  runtime_seconds: null,
}

/**
 * Fully enriched. `cast` and `crew` arrive **in billing order already spent** —
 * the list order *is* `billing_order`, which is why the field is not on the
 * wire and why a client-side re-sort is the defect the server's `ORDER BY …
 * NULLS LAST` exists to prevent.
 *
 * `character` and `job` are both nullable rather than one being absent per
 * kind: a cast entry with no character and a crew entry with no job are the
 * same stored row shape.
 */
export const titleEnriched: TitleResponse = {
  id: TITLE_ENRICHED,
  kind: 'movie',
  name: 'Stalker',
  year: 1979,
  overview: 'A guide leads two men through an area known as the Zone to find a room that grants wishes.',
  tagline: 'The Zone wants to be respected.',
  runtime_minutes: 162,
  genres: ['Drama', 'Science Fiction'],
  community_rating: 8.1,
  enrichment_state: 'enriched',
  enrichment_error: null,
  availability: [availabilityLivingRoom],
  watch_state: {
    position_seconds: 3_142,
    played: false,
    play_count: 1,
    last_played_at: '2026-08-14T21:07:33Z',
  },
  cast: [
    { person_id: PERSON_DIRECTOR, name: 'Alexander Kaidanovsky', character: 'Stalker', job: null },
    {
      person_id: '0191f4cb-628b-74b9-a850-2f96da7bc39d',
      name: 'Anatoly Solonitsyn',
      character: 'Writer',
      job: null,
    },
    {
      person_id: '0191f4cb-739c-75ca-b96f-30a7eb8cd4ae',
      name: 'Nikolai Grinko',
      // A cast entry with no character is a real stored row, not a hole.
      character: null,
      job: null,
    },
  ],
  crew: [
    { person_id: PERSON_DIRECTOR, name: 'Andrei Tarkovsky', character: null, job: 'Director' },
    {
      person_id: '0191f4cb-84ad-76db-8a7e-41b8fc9de5bf',
      name: 'Alexander Knyazhinsky',
      character: null,
      job: 'Director of Photography',
    },
    {
      person_id: '0191f4cb-95be-77ec-9b8d-52c90daef6c0',
      name: 'Boris Strugatsky',
      character: null,
      // A crew entry with no job is the same row shape as a cast entry with no
      // character, which is why both are `null` rather than one being absent.
      job: null,
    },
  ],
  // `kind` is not decoration: it is the difference between a 2:3 slot and a
  // 16:9 one, and the list order *is* `is_primary`, already spent.
  images: [
    { id: IMAGE_POSTER, kind: 'poster' },
    { id: IMAGE_BACKDROP, kind: 'backdrop' },
    { id: IMAGE_LOGO, kind: 'logo' },
  ],
}

/**
 * The absent-key case, typed so it cannot be collapsed.
 *
 * A `skeleton` title comes from a bulk dataset, so it often already carries
 * genres, ratings and a runtime — `skeleton` and `stub` differ by *provenance*
 * as much as by completeness and neither is a subset of the other. What it has
 * never had is a provider payload to derive credits or artwork from, so those
 * three keys are not on the wire at all.
 */
export type SkeletonTitle = Omit<TitleResponse, 'cast' | 'crew' | 'images'>

export const titleSkeleton: SkeletonTitle = {
  id: TITLE_SKELETON,
  kind: 'movie',
  name: 'Solaris',
  year: 1972,
  overview: null,
  tagline: null,
  runtime_minutes: 167,
  genres: ['Drama', 'Science Fiction'],
  community_rating: 8.0,
  enrichment_state: 'skeleton',
  enrichment_error: null,
  availability: [],
  // `null` here is never-computed: no watch state has ever been recorded for
  // this household. It is not "position zero".
  watch_state: null,
}

/**
 * A title whose last enrichment attempt failed. `enrichment_error` is tracked
 * separately from `enrichment_state` on purpose: a failed attempt does not
 * consume or reset a rung (ADR-0008), so this row is still `stub` and will be
 * retried.
 */
export const titleEnrichmentFailed: SkeletonTitle = {
  id: TITLE_SIMILAR_EMPTY,
  kind: 'movie',
  name: 'Andrei Rublev',
  year: 1966,
  overview: null,
  tagline: null,
  runtime_minutes: 205,
  genres: ['Drama', 'History'],
  community_rating: 8.0,
  enrichment_state: 'stub',
  enrichment_error: 'tmdb: 404 for movie/undefined (attempt 3)',
  availability: [availabilityRetracted],
  watch_state: null,
}

export const titleSeries: TitleResponse = {
  id: TITLE_SERIES,
  kind: 'series',
  name: 'Twin Peaks',
  year: 1990,
  overview: 'An FBI agent investigates the murder of a young woman in a small Washington town.',
  tagline: null,
  runtime_minutes: 47,
  genres: ['Drama', 'Mystery'],
  community_rating: 8.6,
  enrichment_state: 'enriched',
  enrichment_error: null,
  availability: [availabilityLivingRoom],
  watch_state: null,
  cast: [
    {
      person_id: '0191f4cb-a6cf-78fd-8c9c-63dae1bf07d1',
      name: 'Kyle MacLachlan',
      character: 'Dale Cooper',
      job: null,
    },
  ],
  crew: [
    {
      person_id: '0191f4cb-b7d0-790e-9dab-74ebf2c018e2',
      name: 'David Lynch',
      character: null,
      job: 'Creator',
    },
  ],
  images: [{ id: IMAGE_BACKDROP, kind: 'backdrop' }],
}

/* --------------------------------------------------------------- similar */

/**
 * Computed, with neighbours, and current. `computed_at` set + `stale: false`.
 */
export const similarComputed: SimilarResponse = {
  neighbors: [
    { id: TITLE_SKELETON, kind: 'movie', name: 'Solaris', year: 1972, score: 0.891 },
    { id: TITLE_SIMILAR_STALE, kind: 'movie', name: 'The Mirror', year: 1975, score: 0.842 },
    { id: TITLE_SIMILAR_EMPTY, kind: 'movie', name: 'Andrei Rublev', year: 1966, score: 0.817 },
  ],
  computed_at: '2026-08-16T04:12:09Z',
  stale: false,
}

/**
 * **Never computed.** `computed_at: null` — dashed hairline, italic sentence,
 * and a mono `meta` naming the field that proves the claim. "We have never
 * computed similar titles for this one."
 *
 * Note `neighbors: []` here means nothing on its own; `computed_at` is the
 * field that distinguishes this from the case below, and that is exactly why
 * `meta` names it.
 */
export const similarNeverComputed: SimilarResponse = {
  neighbors: [],
  computed_at: null,
  stale: false,
}

/**
 * **Computed and empty.** A `computed_at` with `neighbors: []`: we looked, and
 * nothing scored close enough to show. Solid hairline on `--bg-sunken`,
 * "Computed 3 days ago. Nothing scored close enough to show."
 */
export const similarComputedEmpty: SimilarResponse = {
  neighbors: [],
  computed_at: '2026-08-16T04:12:09Z',
  stale: false,
}

/**
 * **Stale.** The list is real and its inputs moved — a title's neighbours go
 * stale when some *other* title gains an embedding, which no per-row predicate
 * can decide, and nothing runs the rebuild automatically. Amber hairline, and
 * the content is **shown**: suppressing it would be a bigger lie than showing
 * it. "Computed before the scoring blend changed. Shown as they were."
 */
export const similarStale: SimilarResponse = {
  neighbors: [
    { id: TITLE_ENRICHED, kind: 'movie', name: 'Stalker', year: 1979, score: 0.903 },
    { id: TITLE_SKELETON, kind: 'movie', name: 'Solaris', year: 1972, score: 0.864 },
  ],
  computed_at: '2026-07-02T11:44:51Z',
  stale: true,
}

/* ------------------------------------------------------ series hierarchy */

export const seasons: SeasonsResponse = {
  seasons: [
    {
      id: SEASON_ONE,
      title_id: TITLE_SERIES,
      season_number: 1,
      name: 'Season 1',
      overview: 'Agent Cooper arrives in Twin Peaks.',
      air_date: '1990-04-08',
      episode_count: 8,
    },
    {
      id: SEASON_TWO,
      title_id: TITLE_SERIES,
      season_number: 2,
      name: 'Season 2',
      // `null` overview and `null` episode_count are both real: the season row
      // exists and those two facts were never supplied.
      overview: null,
      air_date: '1990-09-30',
      episode_count: null,
    },
  ],
}

export const episodePilot: EpisodeResponse = {
  id: EPISODE_PILOT,
  title_id: TITLE_SERIES,
  season_id: SEASON_ONE,
  season_number: 1,
  episode_number: 1,
  absolute_number: 1,
  name: 'Pilot',
  overview: 'The body of Laura Palmer is found wrapped in plastic.',
  air_date: '1990-04-08',
  runtime_minutes: 94,
}

export const episodeSecond: EpisodeResponse = {
  id: EPISODE_SECOND,
  title_id: TITLE_SERIES,
  season_id: SEASON_ONE,
  season_number: 1,
  episode_number: 2,
  absolute_number: 2,
  name: null,
  overview: null,
  air_date: '1990-04-12',
  runtime_minutes: 47,
}

export type EpisodePage = Schemas['Page_EpisodeResponse_']

/** First page: a cursor, so there is more. */
export const seasonEpisodesPageOne: EpisodePage = {
  items: [episodePilot, episodeSecond],
  next_cursor: 'eyJrIjoiZXBpc29kZSIsInYiOjIsImgiOiI4YTFmIn0',
}

/** Last page: `next_cursor: null`, which owes the reader a sentence. */
export const seasonEpisodesPageTwo: EpisodePage = {
  items: [
    {
      id: '0191f4cc-c8e1-7a1f-8ebb-859c0d1e2934',
      title_id: TITLE_SERIES,
      season_id: SEASON_ONE,
      season_number: 1,
      episode_number: 3,
      absolute_number: 3,
      name: 'Zen, or the Skill to Catch a Killer',
      overview: null,
      air_date: '1990-04-19',
      runtime_minutes: 47,
    },
  ],
  next_cursor: null,
}
