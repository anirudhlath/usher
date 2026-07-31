# ADR-0017 — The metadata port returns an aggregate and a cursor, and carries a `ProviderRef`

**Status:** Accepted — settles three provisional markers
**Date:** 2026-07-31

## Context

`usher.ports.metadata.MetadataProvider` carried three 🔶 markers from M1, all
three naming **M4** and all three blocked on the same thing: the models the
enrich stage populates did not exist yet.

1. `to_title(payload, title_id) -> Title` returns one entity, while
   [03](../03-sources-and-sync.md)'s enrich stage populates `Season`,
   `Episode`, `Person`, `Credit`, `Collection` and `Image` from the same
   response.
2. `fetch(provider_id: int, kind)` bakes in TMDb's integer id scheme. IMDb's
   `tt1160419` does not fit it, and [01](../01-architecture.md) lists
   additional metadata providers as an open extension seam.
3. `changed_since(days: int) -> list[int]` cannot express a resumable
   position through TMDb's paginated, 14-day-capped changes feed, so a
   partial run restarts the window.

`Season`/`Episode` now exist (M4), and so does `ProviderRef`
(`usher.ports.ingest`), so all three can be settled rather than deferred
again. The port is an ABC ([ADR-0001](0001-abc-over-protocol.md)); reshaping
it is cheap now and expensive once a second provider exists.

## Decision

**1. `to_title` becomes `to_result(payload, title_id) -> EnrichmentResult`**,
a frozen dataclass carrying `title`, `seasons`, `episodes`, and the
provider's verbatim `payload`. `people`, `credits`, `images` and `collection`
are **not** fields.

**2. `fetch(ref: ProviderRef) -> dict[str, Any]`.** One argument, carrying a
string value and a `TitleKind | None`.

**3. `changed_since(since: AwareDatetime, cursor: str | None = None) ->
ChangedPage`**, where `ChangedPage` carries `refs: tuple[ProviderRef, ...]`
and `next_cursor: str | None`. **A provider may answer a narrower window than
it was asked for**, and clamps rather than raising.

**4. Not marked, and changed anyway: `search` gains an optional
`kind: TitleKind | None = None`.**

**5. Marked-adjacent, and deliberately *not* changed:
`MetadataCandidate.provider_id` stays an `int`.**

## Consequences

- **Deferring `Person`/`Credit`/`Collection`/`Image` is honest rather than
  lossy only because `payload` travels with the result.** Nothing in M4
  stores those four: `Person`/`Credit` are first read by M7's "more from this
  director" join, `Collection` by M7's franchise completeness, `Image` by
  M9's image proxy. The response that would have produced them is cached in
  `raw_payloads` ([ADR-0016](0016-raw-payloads-cache-providers-not-sources.md)),
  so each lands with the milestone that reads it, re-derived with **no second
  network call**. Adding `people: tuple[Person, ...]` later is an added
  field, not a signature change. A reasonable person would add the four
  fields now and populate them with empty tuples; that is the call this ADR
  argues against, because a `tuple()` on a result reads as "this provider has
  no cast" and a table nobody queries is worse than an absent one.
- **`seasons`/`episodes` are on the result because M4 stores them.** 999,827
  of the one measured source's 1,126,674 items are episodes. A result that
  could not carry the hierarchy would leave the pipeline unable to enrich 89%
  of what this library holds, which is not a deferral anyone could defend.
- **`fetch` taking a ref means a second provider is an implementation, not a
  signature change.** It also means the kind travels with the id, which
  [ADR-0011](0011-tmdb-id-is-namespaced-by-kind.md) makes non-optional: TMDb's
  movie and series id spaces overlap on 26,968 measured ids, so a bare
  integer names two different things. `ChangedPage.refs` carries refs for the
  same reason — a page of integers is a page whose kind the caller has to
  guess.
- **A clamped change window is a silent narrowing, and the port says so.** A
  caller may not read an exhausted feed as proof that nothing older changed.
  The alternative — raising when `since` is more than 14 days back — turns
  the one call a re-enrichment sweep makes after a fortnight's downtime into
  no answer at all, when a partial answer is strictly better and the full
  recovery path (a re-enrichment sweep over `titles`) exists anyway. PRD 04's
  Phase 5 runs this daily, so the clamp is unreachable in steady state.
- **`search`'s `kind` costs a provider nothing and saves the match ladder
  half its requests.** TMDb searches movies and series through separate
  endpoints (`/search/movie`, `/search/tv`), and `/search/multi` supports
  neither a year filter nor those endpoints' `primary_release_year` /
  `first_air_date_year`. Without a `kind`, a provider must issue both
  requests and the caller discards half the answers — on the tier PRD 03
  already calls "a last resort" precisely because it is rate-limited. The
  match stage always knows the kind (`SourceItem.kind`). It is optional, so a
  provider with one search space ignores it, and a caller that genuinely does
  not know passes `None` and filters on `MetadataCandidate.kind`.
- **`MetadataCandidate` staying integer-keyed is an asymmetry, and a
  bounded one.** `provider_id` + `kind` + the provider's own `name` is
  losslessly a `ProviderRef`, and `MatchService` builds one at the single
  point a candidate crosses into the matcher. The M1 test that pins this
  shape exists to record a real bug — `search()` returning
  `list[dict[str, Any]]` made the match stage index into TMDb's own keys —
  and a ref preserves that fix as well as an int does. The trigger for
  revisiting is named rather than left to taste: **a provider whose search
  results are not integer-keyed.** At that point the field becomes a
  `ProviderRef`, `kind` folds into it, and nothing else moves.
- **`to_result` is synchronous and never sets `enrichment_state`.** It is a
  pure function of a payload the caller already holds, and the tier is only
  ever raised through `ENRICHMENT_RANK`
  ([ADR-0008](0008-enrichment-tier-vs-failure.md)) — a provider that stamped
  `ENRICHED` on a partial payload would promote a title its own answer did
  not earn.
- **`fetch` reports "this entity is gone" as `PortDataMalformed`, not
  `PortUnavailable`.** TMDb answers 404 for an id it has merged away, and the
  catalog holds 291,737 TMDb ids from a bulk export that ages. Retrying does
  not help, so `JobWorker` parks it immediately rather than spending five
  rate-limited attempts first.

## Evidence

- 26,968 TMDb ids live in both the movie and series spaces, 47.3% of all
  series ids Wikidata knows — measured 2026-07-30,
  [ADR-0011](0011-tmdb-id-is-namespaced-by-kind.md).
- 1,126,674 items on the measured source, 999,827 of them episodes; 1,271,138
  catalog titles of which 291,737 carry a `tmdb_id` — `CLAUDE.md`.
- TMDb's `/movie/changes` 14-day window cap and the `append_to_response`
  vocabulary — [03](../03-sources-and-sync.md),
  [04](../04-catalog-bootstrap.md).
- `EnrichmentState.ENRICHED > EnrichmentState.SKELETON` is `False` —
  [ADR-0008](0008-enrichment-tier-vs-failure.md).
