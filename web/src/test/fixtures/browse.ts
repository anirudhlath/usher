/**
 * `GET /browse` — a keyset page and its facet block.
 *
 * **The facet block is where the generated types are optimistic**, and it is a
 * real trap rather than a theoretical one. The route serialises with
 * `response_model_exclude_unset=True`, so:
 *
 * · `reason` is present **exactly when** `computed` is false;
 * · `genres` and `years` are present **exactly when** it is true.
 *
 * `schema.d.ts` declares all four required, because OpenAPI cannot express
 * "these two are absent together". So the fixtures below are typed with `Omit`
 * — modelling the absence in the type system is the only way a consumer that
 * reads `facets.genres` without checking `computed` fails to compile instead of
 * failing on screen.
 */

import type { BrowseFacets, BrowseItem, BrowseResponse } from '@/api'
import { CURSOR_PAGE_TWO, TITLE_ENRICHED, TITLE_SERIES, TITLE_SIMILAR_STALE, TITLE_SKELETON } from './ids'

/** `computed: true`: counts are on the wire and `reason` is not. */
export type ComputedFacets = Omit<BrowseFacets, 'reason'>
/** `computed: false`: `reason` says which of the two fixes applies. */
export type OmittedFacets = Omit<BrowseFacets, 'genres' | 'years'>

export type BrowsePage<F extends ComputedFacets | OmittedFacets> = Omit<BrowseResponse, 'facets'> & {
  facets: F
}

export const browseItems: BrowseItem[] = [
  {
    title_id: TITLE_ENRICHED,
    kind: 'movie',
    name: 'Stalker',
    year: 1979,
    popularity: 14.812,
    vote_count: 3_106,
  },
  {
    title_id: TITLE_SERIES,
    kind: 'series',
    name: 'Twin Peaks',
    year: 1990,
    popularity: 42.09,
    vote_count: 1_884,
  },
  {
    title_id: TITLE_SIMILAR_STALE,
    kind: 'movie',
    name: 'The Mirror',
    year: 1975,
    popularity: 8.44,
    vote_count: 941,
  },
  {
    title_id: TITLE_SKELETON,
    kind: 'movie',
    name: 'Solaris',
    year: 1972,
    // `popularity: null` is not zero and must not be rendered as zero. It is
    // `null` for every title TMDb's daily export has never described — 980,523
    // of the 1,272,367 rows this route was measured against — so `popularity or
    // 0.0` would render "nobody has measured this" identically to "measured,
    // and unpopular" (ADR-0014).
    popularity: null,
    vote_count: null,
  },
]

export const browseItemsPageTwo: BrowseItem[] = [
  {
    title_id: '0191f4c9-3f58-7186-b523-fc63a748906a',
    kind: 'movie',
    name: 'Nostalghia',
    year: 1983,
    popularity: 6.21,
    vote_count: 512,
  },
  {
    title_id: '0191f4c9-4069-7297-8632-0d74b859a17b',
    kind: 'movie',
    name: 'The Sacrifice',
    year: 1986,
    popularity: 5.77,
    vote_count: 468,
  },
]

/** Facets were not asked for. The fix is `facets=true`. */
export const facetsNotRequested: OmittedFacets = {
  computed: false,
  reason: 'not_requested',
}

/**
 * Facets were asked for over an unfiltered catalog. The fix is a filter, not a
 * flag — which is precisely why `FacetsOmitted` has two members and not one
 * boolean: the two have different fixes and different sentences.
 */
export const facetsUnpredicated: OmittedFacets = {
  computed: false,
  reason: 'unpredicated',
}

export const facetsComputed: ComputedFacets = {
  computed: true,
  genres: {
    Drama: 412,
    'Science Fiction': 168,
    Thriller: 96,
    Mystery: 41,
  },
  years: {
    '1972': 3,
    '1975': 2,
    '1979': 5,
    '1983': 1,
    '1986': 1,
    '1990': 4,
  },
}

/** Page one. `next_cursor` is a string, so there is more. */
export const browsePageOne: BrowsePage<OmittedFacets> = {
  items: browseItems,
  next_cursor: CURSOR_PAGE_TWO,
  facets: facetsNotRequested,
}

/**
 * The last page. `next_cursor: null` MUST produce a sentence — "That is
 * everything we have for this filter." — because a silent stop is
 * indistinguishable from a bug (patterns.md §4). There is no total, no count
 * and no page number anywhere in this response, and there is not meant to be.
 */
export const browsePageTwo: BrowsePage<OmittedFacets> = {
  items: browseItemsPageTwo,
  next_cursor: null,
  facets: facetsNotRequested,
}

/** `facets=true` with a filter set: real counts with real denominators. */
export const browseWithFacets: BrowsePage<ComputedFacets> = {
  items: browseItems,
  next_cursor: null,
  facets: facetsComputed,
}

/** `facets=true` with nothing filtered: counts are declined, with the reason. */
export const browseUnpredicated: BrowsePage<OmittedFacets> = {
  items: browseItems,
  next_cursor: CURSOR_PAGE_TWO,
  facets: facetsUnpredicated,
}

/** An empty filter result. `items: []` with a cursor of `null` is the end. */
export const browseEmpty: BrowsePage<OmittedFacets> = {
  items: [],
  next_cursor: null,
  facets: facetsNotRequested,
}
