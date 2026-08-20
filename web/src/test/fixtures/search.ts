/**
 * `GET /search` and `GET /search/suggest`.
 *
 * Three things here are shapes a screen has to get right and nothing else in
 * the API produces:
 *
 * · **`mode !== requested_mode` is a downgrade**, and it is the server telling
 *   the client that the lane it asked for could not run — a `semantic` request
 *   against a catalog with no vectors comes back `full_text`. The UI has to say
 *   so; silently rendering the results as if the requested lane ran is the
 *   dishonest option.
 * · **`semantic_coverage` carries a denominator or it does not ship**
 *   (patterns.md §14): it is rendered as "0.98 — of 128,400 enriched titles,
 *   not of the 1,268,441-row catalog", never as a share of the library.
 * · **`expanded_query: null`** is never-computed, not empty-string.
 */

import type { SearchResponse, SuggestResponse } from '@/api'
import { TITLE_ENRICHED, TITLE_SERIES, TITLE_SIMILAR_STALE, TITLE_SKELETON } from './ids'

/** Attributes a play or a title view back to the query that produced it. */
export const SEARCH_ID = '0191f4ca-517a-73a8-9741-1e85c96ab28c'

const results: SearchResponse['results'] = [
  {
    title_id: TITLE_ENRICHED,
    kind: 'movie',
    name: 'Stalker',
    year: 1979,
    popularity: 14.812,
    owned: true,
    score: 0.0327,
  },
  {
    title_id: TITLE_SKELETON,
    kind: 'movie',
    name: 'Solaris',
    year: 1972,
    popularity: null,
    owned: false,
    score: 0.0161,
  },
  {
    title_id: TITLE_SIMILAR_STALE,
    kind: 'movie',
    name: 'The Mirror',
    year: 1975,
    popularity: 8.44,
    owned: true,
    score: 0.0159,
  },
]

/** The default lane. RRF over both, and the one the 2026-08-19 bar chose. */
export const searchFused: SearchResponse = {
  query: 'tarkovsky',
  requested_mode: 'fused',
  mode: 'fused',
  semantic_coverage: 0.98,
  expanded_query: 'tarkovsky andrei soviet science fiction contemplative',
  search_id: SEARCH_ID,
  results,
}

export const searchFullText: SearchResponse = {
  query: 'tarkovsky',
  requested_mode: 'full_text',
  mode: 'full_text',
  // The lexical lane consults no vectors, so coverage over the embedded
  // population is `0.0` rather than absent: the question was asked and the
  // answer is none.
  semantic_coverage: 0.0,
  // Query expansion is a semantic-lane feature. `null` is never-computed and
  // gets §2's dashed-hairline treatment, not an empty box.
  expanded_query: null,
  search_id: SEARCH_ID,
  results: results.slice(0, 2),
}

export const searchSemantic: SearchResponse = {
  query: 'a slow film about memory and water',
  requested_mode: 'semantic',
  mode: 'semantic',
  semantic_coverage: 1.0,
  expanded_query: 'memory water dream contemplative long take',
  search_id: SEARCH_ID,
  results,
}

/**
 * **The downgrade.** The client asked for `semantic`; the server ran
 * `full_text` because this deployment has no vectors for the query's
 * neighbourhood. `mode !== requested_mode` is the only signal, and a surface
 * that does not compare them shows semantic results that are not semantic.
 */
export const searchDowngraded: SearchResponse = {
  query: 'something nobody has embedded',
  requested_mode: 'semantic',
  mode: 'full_text',
  semantic_coverage: 0.0,
  expanded_query: null,
  search_id: SEARCH_ID,
  results: [
    {
      title_id: TITLE_SERIES,
      kind: 'series',
      name: 'Twin Peaks',
      year: 1990,
      popularity: 42.09,
      owned: true,
      score: 0.0104,
    },
  ],
}

/** A query that matched nothing. `results: []` with a real `search_id`. */
export const searchEmpty: SearchResponse = {
  query: 'zzzzzzzz',
  requested_mode: 'fused',
  mode: 'fused',
  semantic_coverage: 0.98,
  expanded_query: null,
  search_id: SEARCH_ID,
  results: [],
}

/**
 * The as-you-type tier (ADR-0031). `min_query_length` is on the wire because
 * the combobox has to know why a two-character query returned nothing — and
 * "we did not look" is a different sentence from "we looked and found none".
 */
export const suggestPrefix: SuggestResponse = {
  query: 'stal',
  tier: 'prefix',
  min_query_length: 2,
  results: [
    {
      title_id: TITLE_ENRICHED,
      kind: 'movie',
      name: 'Stalker',
      year: 1979,
      popularity: 14.812,
      owned: true,
      score: 0.94,
    },
  ],
}

/**
 * The typo-tolerant tier. The 2026-08-03 gate showed it cannot meet an
 * as-you-type latency budget on this catalog, which is why the tier is a
 * parameter the UI exposes rather than a detail it hides — and why the two
 * tiers get separate group headers rather than being presented as a fallback
 * chain (patterns.md §12).
 */
export const suggestFuzzy: SuggestResponse = {
  query: 'stlaker',
  tier: 'fuzzy',
  min_query_length: 3,
  results: [
    {
      title_id: TITLE_ENRICHED,
      kind: 'movie',
      name: 'Stalker',
      year: 1979,
      popularity: 14.812,
      owned: true,
      score: 0.71,
    },
    {
      title_id: TITLE_SKELETON,
      kind: 'movie',
      name: 'Solaris',
      year: 1972,
      popularity: null,
      owned: false,
      score: 0.33,
    },
  ],
}

/** Below `min_query_length`: nobody looked, and the field says which. */
export const suggestTooShort: SuggestResponse = {
  query: 's',
  tier: 'prefix',
  min_query_length: 2,
  results: [],
}
