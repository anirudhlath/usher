# ADR-0014 — Absence is not zero on `SourceWatchState`

**Status:** Accepted

## Context

`SourceAdapter.watch_state()` walks a source's listing. Verified 2026-07-31
against Emby 4.9.5.0, a `GET /Users/{user}/Items` listing reports
`PlayCount: 0` and omits `LastPlayedDate` entirely, for the very item whose
`GET /Users/{user}/Items/{item}` reports `PlayCount: 2` and a real
`LastPlayedDate`. `PlaybackPositionTicks` and `Played` are correct in both.
Neither `Fields=UserDataPlayState`, `Fields=UserData`, `EnableUserData=true`,
nor restricting the listing to explicit `Ids` changes it.

`SourceWatchState.play_count` was `int` with a default of `0`, so the walk
had no way to say anything other than "zero plays". M4 is the first caller
that writes these fields. Recovering them per item costs one request each
against a library measured at 1,126,674 items.

## Decision

`play_count: int | None = None` and `last_played_at: AwareDatetime | None =
None`. `None` means "this read could not determine it"; `0` and a real
datetime are positive claims a caller must honour.

`SourceAdapter` gains `get_watch_state(external_id) -> SourceWatchState |
None` — one request, the authoritative read, `None` for a deleted item and
a raise for an unreachable source, matching `get_item` exactly.

Inside the Emby adapter the distinction is carried by
`to_watch_state(..., play_history_is_trustworthy: bool)`, which **names the
route rather than a preference**: the listing walk passes `False`,
`get_watch_state` passes `True`. It has no default, so a call site that has
not thought about which route it read from does not type-check. Trusting the
route is still not inventing a value — a payload with no `PlayCount` key
yields `None` even under `True`.

`WatchStateRepository.merge_from_source` is `COALESCE`-shaped: `None` leaves
the stored column untouched, `0` overwrites it. Recovering history is a
queued backfill over `played = true AND play_count = 0`, bounded by the
household's watched items rather than by the library.

## Consequences

**Gained:** a walk cannot zero real play history, because the value it
carries is not a number. The contract suite can state the rule
source-agnostically — a reported number must be true, but presence is never
required — so a source whose listing *is* complete (Jellyfin may well be) is
not forced to lie in either direction.

**Given up:** every read site needs a null check, and `SourceAdapter` grows
a tenth abstract method every future adapter must implement.
`watch_states.play_count` stays `int NOT NULL DEFAULT 0`, so
"played but count unknown" is spelled `played AND play_count = 0` rather
than `IS NULL` — very slightly lossy, self-healing after one background
request, and far cheaper than pushing three-valued play counts through
`WatchState`, PRD 02, every API response, and every dashboard query.

**Rejected:** leaving the port alone and making M4 simply not write the
fields. That is a rule in a docstring rather than a property of a type, and
M5's push lane re-walks `watch_state(since=…)` with nothing to stop it
inheriting the same trap.

## Evidence

`tests/contract/source_adapter_contract.py::
SourceAdapterContract::test_a_walk_never_reports_play_history_it_cannot_know`
asserts the walked value is `None` *or* the seeded value and nothing else.
It passes on the `== 7` branch against `FakeSourceAdapter` and on the `is
None` branch against `EmbyAdapter` over `FakeEmbyServer` — both branches
live across the two runs.

Mutation-tested seven ways, each killing exactly the case written for it:
against `FakeSourceAdapter`, rebuilding `get_watch_state`'s answer without
history, yielding `play_count=0` from the walk, dropping the unknown-id
guard, and dropping the readiness check; against the Emby adapter, the walk
passing `play_history_is_trustworthy=True`.

The two that matter most are the pair that could drift together — the
adapter trusting the listing, and `FakeEmbyServer` rendering a listing that
carries history:

| adapter trusts listing | fake listing carries history | result |
|---|---|---|
| no | no | green |
| **yes** | no | `test_the_walk_reports_absent_play_history` and the contract case both fail |
| no | **yes** | `test_the_listing_route_omits_the_play_history_the_item_route_carries` fails |
| **yes** | **yes** | the first and third fail |

The third row is the one worth recording: with only the adapter-level tests
in place, the fake drifting back into agreement with the adapter was
**invisible** — a correct adapter discards those fields whatever the fake
supplies, so nothing objected. That is the exact shape of the M3 write-back
failure, where `FakeEmbyServer` implemented the adapter's own guess and 40
contract assertions passed against a route that had never worked. It is
closed by pinning the fake against the *measurement* directly, in
`tests/unit/test_fakes_emby_server.py`, rather than only through the
adapter. A live run remains the only thing that can catch all three files
being wrong together, which is why M4's definition of done keeps one.

### Where the rule survives contact with SQL

The port makes the wrong value unspellable; the *merge* is where it could
still be written anyway, permanently. Measured against
`pgvector/pgvector:pg17`, 2026-07-31, over a stored row holding
`play_count = 7`:

- **The natural one-statement spelling zeroes it.**
  `INSERT … SELECT … ON CONFLICT (user_id, title_id) DO UPDATE SET
  play_count = COALESCE(excluded.play_count, watch_states.play_count)`, fed
  a merge carrying `play_count = NULL`, reads back **0**. Because the column
  is `NOT NULL`, the insert path must write `COALESCE(play_count, 0)`, and
  that collapse has already happened by the time the conflict clause reads
  `excluded`.
- **The raw `NULL` is unreachable from that clause.** `ON CONFLICT DO
  UPDATE` cannot name a CTE — `missing FROM-clause entry for table "d"`,
  reproduced directly — so no one-statement form can read the value it needs.
- **`last_played_at` survives the same statement.** It is nullable, so it is
  never collapsed, `excluded.last_played_at` is genuinely `NULL`, and the
  `COALESCE` works. "The natural spelling zeroes history" is therefore true
  of exactly one of the two columns, and a suite that checked only the
  timestamp would have ratified the bug. That is why
  `test_absent_play_history_leaves_a_stored_count_alone` and
  `test_absent_last_played_at_leaves_a_stored_timestamp_alone` are separate
  cases, and it is confirmed by running the one-statement form against the
  suite: the count case fails, the timestamp case passes.

`PostgresWatchStateRepository` is therefore two statements per conflict
target — `UPDATE … FROM deduped` (where the `NULL` is still `NULL` and still
in scope), then `INSERT … ON CONFLICT DO NOTHING` — both set-based, four
statements per batch regardless of size. Fifteen mutations were run against
it, including the one-statement form itself; every one was caught, and a
plausible-but-wrong per-row implementation carrying that spelling fails 11
of the 25 shared contract cases.

### The site where `0.0` is not merely uninformative but unreachable

Added by M7's similarity work, and it is worth its own section because it is
the first application where the wrong answer is a value **the true
distribution cannot produce**.

`NeighborCandidate.tags` is the MovieLens tag-genome cosine for a *pair* of
titles, and it is `None` when **either** side has no `genome_scores` row. Every
other site in this ADR is a case where `0.0` is a plausible-but-unevidenced
reading of the same fact — "played zero times" against "we did not ask". Here
`0.0` is not even plausible:

- every component of a genome vector is a relevance in `[0.00025, 1.0]`, so a
  cosine between two of them is bounded strictly above zero by construction;
- measured over all **268,157,000** ordered off-diagonal pairs of the real
  16,376-vector release: **minimum 0.2556**, p1 0.4075, mean 0.6101.

So writing `0.0` for a half-covered pair asserts that two films share no tags,
which **no real pair can truthfully say** — the single most confident wrong
statement available in the blend.

**And its consequence is structural rather than marginal.** `_blend`
renormalises over the signals that are present, so `None` correctly drops the
term and scores the pair on what is known. `0.0` instead applies a maximally
negative genome term to every pair that straddles the coverage boundary — which
reorders every genome-bearing title's neighbour list to put every *other*
genome-bearing title above every un-genomed one. At the genome's real coverage
that is a small clique pinned to the top of the overwhelming majority of
lists, produced by a value no measurement supports, with no error and nothing
in any gauge to see it.

The clamp that keeps `tags` inside `[0, 1]` lives in the **service**, not in
SQL, for the reason `cosine`'s clamp already does: `title_neighbors.score` is
`CHECK (score >= 0 AND score <= 1)`, so the bound has to hold for every
implementation of the port rather than for the one that remembered. And
`_clamped` is a function rather than a `min`/`max` at the call site precisely
because of this ADR: `max(0.0, value or 0.0)` is the obvious repair for the
`None` arm and it silently reintroduces the collapse.
