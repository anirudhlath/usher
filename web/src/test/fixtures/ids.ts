/**
 * Every id this suite uses, in one place, and every one of them shaped like a
 * real UUIDv7.
 *
 * Identity in Usher's contract is its own UUIDv7 (ADR-0003) — `tmdb_id` and
 * `imdb_id` are indexed attributes and never identifiers in an API response —
 * so a fixture using `"title-1"` would be testing a shape the API cannot
 * produce. These carry the version nibble (`7`) and the variant bits, and they
 * sort by their embedded timestamp, which matters for any test that asserts an
 * ordering premise rather than mere membership.
 *
 * Named constants rather than literals so a test reads "the skeleton title"
 * instead of a hex string, and so the handler and the assertion cannot drift.
 */

/* ------------------------------------------------------------------ titles */

/** Stalker (1979). Fully enriched: cast, crew, images, one available copy. */
export const TITLE_ENRICHED = '0191f4c2-8a7e-7c31-b0d9-2f6a1e4c8b55'
/**
 * Solaris (1972). `enrichment_state: "skeleton"` — and the fixture for it omits
 * `cast`, `crew` and `images` entirely rather than sending `[]`. That is the
 * distinction patterns.md §2 makes a correctness rule.
 */
export const TITLE_SKELETON = '0191f4c2-9b13-7d42-a1c8-4e7b2f9d3a61'
/** Twin Peaks (1990). A series, so it has seasons and episodes. */
export const TITLE_SERIES = '0191f4c3-0c25-7e53-92d7-6f8c3a1b5d72'
/** A title nothing on this deployment knows about — the `not_found` fixture. */
export const TITLE_MISSING = '0191f4c3-1d36-7f64-83e6-7a9d4b2c6e83'
/** Owned by nobody: `POST /play` answers `not_playable` for this one. */
export const TITLE_NOT_PLAYABLE = '0191f4c3-2e47-7075-74f5-8b0e5c3d7f94'
/** Similar titles were computed and nothing scored close enough. */
export const TITLE_SIMILAR_EMPTY = '0191f4c3-3f58-7186-a5e4-9c1f6d4e80a5'
/** Similar titles have never been computed: `computed_at: null`. */
export const TITLE_SIMILAR_NEVER = '0191f4c3-4069-7297-b6d3-0d2a7e5f91b6'
/** Similar titles are real and their inputs moved: `stale: true`. */
export const TITLE_SIMILAR_STALE = '0191f4c3-517a-73a8-87c2-1e3b8f6a02c7'

/* ---------------------------------------------------------------- episodes */

export const SEASON_ONE = '0191f4c4-628b-74b9-98b1-2f4c9a7b13d8'
export const SEASON_TWO = '0191f4c4-739c-75ca-a9a0-304da0b8c24e'
export const EPISODE_PILOT = '0191f4c4-84ad-76db-ba9f-415eb1c9d5fa'
export const EPISODE_SECOND = '0191f4c4-95be-77ec-8b8e-526fc2dae60b'

/* ------------------------------------------------------ people, collections */

export const PERSON_DIRECTOR = '0191f4c5-a6cf-78fd-9c7d-637ad3ebf71c'
export const COLLECTION_TRILOGY = '0191f4c5-b7d0-790e-ad6c-748be4fc082d'

/* ----------------------------------------------------------------- sources */

export const SOURCE_LIVING_ROOM = '0191f4c6-c8e1-7a1f-be5b-859cf50d193e'
export const SOURCE_UNREACHABLE = '0191f4c6-d9f2-7b20-8f4a-960da61e2a4f'

/* --------------------------------------------------------- unmatched items */

export const MEDIA_ITEM_UNMATCHED = '0191f4c7-ea03-7c31-9038-a71eb72f3b50'
export const MEDIA_ITEM_UNMATCHED_2 = '0191f4c7-fb14-7d42-a127-b82fc8304c61'

/* ------------------------------------------------------------------ images */

export const IMAGE_POSTER = '0191f4c8-0c25-7e53-b216-c930d9415d72'
export const IMAGE_BACKDROP = '0191f4c8-1d36-7f64-8305-da41ea526e83'
export const IMAGE_LOGO = '0191f4c8-2e47-7075-9414-eb52fb637f94'

/* ----------------------------------------------------------------- opaque */

/**
 * A keyset cursor. Opaque by contract — it encodes a position **and a hash of
 * the query**, which is why changing a filter invalidates it (ADR-0034) — so
 * the only thing a test may do with this value is send it back.
 */
export const CURSOR_PAGE_TWO = 'eyJrIjoibmFtZSIsInYiOiJTb2xhcmlzIiwiaCI6IjRmMmEifQ'

/** A cursor from a previous filter. `GET /browse` answers `invalid_cursor`. */
export const CURSOR_STALE = 'eyJrIjoieWVhciIsInYiOjE5NzksImgiOiJkZWFkIn0'

/**
 * A Fernet playback ticket, at the real length (~300 characters would be the
 * live value; this is shortened only so the file stays readable). Nothing may
 * render, copy or log it — patterns.md §13 — and `redact.ts` removes it from
 * the journal, which `client.test.ts` asserts against this exact string.
 */
export const PLAYBACK_TICKET =
  'gAAAAABo3Yk2Xq7pL0nT9vRc8sWzKfE1mJhQb4dNyU2aOgVtP6ix5CrZ3eHl_kMsD8wYaBnF7uJvQtXpR2gLzS4dK9mNbC1oPw'

/* ------------------------------------------------------------------ traces */

/**
 * The trace and span ids off a `traceresponse` header.
 *
 * Both are the worked example from `w3c/trace-context` rather than invented
 * hex, and both are lowercase: the spec says a reader MUST ignore a field that
 * "contains non-lowercase hex characters", so an uppercase fixture would be
 * testing a header `parseTraceResponse` is required to drop.
 *
 * 32 and 16 hex characters respectively, and neither is all zeroes — the two
 * shapes that make the header nameable. `Problem` renders only the first eight
 * characters of the trace id on screen, so an assertion about the *link* has to
 * look at the `href`.
 */
export const TRACE_ID = '4bf92f3577b34da6a3ce929d0e0e4736'
export const TRACE_SPAN_ID = '00f067aa0ba902b7'
