/**
 * Fixtures for the three viewer screens that read a single record —
 * `TitleDetail`, `Series` and `Episode` — covering the responses the shared
 * happy path in `handlers.ts` deliberately does not produce.
 *
 * Every one of these exists to make a *distinction* reachable from a test, and
 * each distinction is one patterns.md calls a correctness rule rather than a
 * preference:
 *
 * · `titleCreditsEmpty` is the counterpart to `titleSkeleton`. The skeleton
 *   omits `cast`, `crew` and `images` from the payload; this one **sends all
 *   three as `[]`**. Absent means "not applicable to this record", `[]` means
 *   "we looked and there is nothing", and a screen that draws the same grey
 *   dash for both has lost a fact the API went to some trouble to state.
 *
 * · `titleSkeletonEnriched` is the *same row* as `titleSkeleton` after
 *   enrichment landed — same id, same name, with the three keys now on the
 *   wire. It is what a `title.updated` frame means, and it is how a test can
 *   watch enrichment arrive on an open skeleton title without inventing a
 *   second title.
 *
 * · `seasonsWithSpecials` adds **season 0**. Specials are a real season with
 *   real episodes, and a switcher that drops them because the number is falsy
 *   drops episodes a household owns.
 */

import type { SeasonsResponse, TitleResponse } from '@/api'
import {
  IMAGE_BACKDROP,
  IMAGE_POSTER,
  PERSON_DIRECTOR,
  SEASON_ONE,
  SEASON_TWO,
  SOURCE_LIVING_ROOM,
  TITLE_ENRICHED,
  TITLE_SERIES,
  TITLE_SKELETON,
} from './ids'

/** Season 0. Its own id, because it is its own season row. */
export const SEASON_SPECIALS = '0191f4c4-517a-73a8-b7c2-1e3b8f6a02c7'

/**
 * `cast: []`, `crew: []`, `images: []` — **present and empty**, which is the
 * fact `titleSkeleton` does not carry. Enrichment ran against this row and the
 * provider returned no people and no artwork.
 */
export const titleCreditsEmpty: TitleResponse = {
  id: TITLE_ENRICHED,
  kind: 'movie',
  name: 'Stalker',
  year: 1979,
  overview: 'A guide leads two men through an area known as the Zone to find a room that grants wishes.',
  tagline: null,
  runtime_minutes: 162,
  genres: ['Drama', 'Science Fiction'],
  community_rating: 8.1,
  enrichment_state: 'enriched',
  enrichment_error: null,
  availability: [
    {
      source_id: SOURCE_LIVING_ROOM,
      source: 'Living Room Emby',
      available: true,
      container: 'mkv',
      video_codec: 'hevc',
      hdr_format: 'HDR10',
      resolution: '3840x2160',
      runtime_seconds: 9_720,
    },
  ],
  watch_state: null,
  cast: [],
  crew: [],
  images: [],
}

/**
 * The skeleton title after `title.updated`: the same `TITLE_SKELETON` row,
 * enriched. `cast`, `crew` and `images` are on the wire for the first time.
 */
export const titleSkeletonEnriched: TitleResponse = {
  id: TITLE_SKELETON,
  kind: 'movie',
  name: 'Solaris',
  year: 1972,
  overview:
    'A psychologist is sent to a station orbiting a distant planet to discover what has caused the crew to go insane.',
  tagline: 'Who are we to say what is real?',
  runtime_minutes: 167,
  genres: ['Drama', 'Science Fiction'],
  community_rating: 8.0,
  enrichment_state: 'enriched',
  enrichment_error: null,
  availability: [],
  watch_state: null,
  cast: [{ person_id: PERSON_DIRECTOR, name: 'Donatas Banionis', character: 'Kris Kelvin', job: null }],
  crew: [
    {
      person_id: '0191f4cb-b7d0-790e-9dab-74ebf2c018e2',
      name: 'Andrei Tarkovsky',
      character: null,
      job: 'Director',
    },
  ],
  images: [
    { id: IMAGE_POSTER, kind: 'poster' },
    { id: IMAGE_BACKDROP, kind: 'backdrop' },
  ],
}

/**
 * The season list with **Specials (season 0)** in it, and it sorts first — the
 * provider's own ordering puts it there and the switcher must not hide it.
 * `episode_count: 6` against a list that comes back empty is the disagreement
 * this screen is required to state rather than smooth over.
 */
export const seasonsWithSpecials: SeasonsResponse = {
  seasons: [
    {
      id: SEASON_SPECIALS,
      title_id: TITLE_SERIES,
      season_number: 0,
      name: 'Specials',
      overview: null,
      air_date: null,
      episode_count: 6,
    },
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
      overview: null,
      air_date: '1990-09-30',
      episode_count: null,
    },
  ],
}
