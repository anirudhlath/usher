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

Added by M7's similarity work — **two sites, one at the port and one in the
blend** — and it is worth its own section because it is the first application
where the wrong answer is a value **the true distribution cannot produce**.

**At the port, `GenomeRepository.get_pair` returns `None` rather than a zero
vector**, and it does so for two different reasons that must not be collapsed:
a title with no `genome_scores` row (the common case at 1.22% coverage), and a
pair whose rows carry *different* `genome_revision`s. The second is the subtler
one — two vectors from different MovieLens releases are type-identical,
same-width and otherwise indistinguishable, so a half-migrated table would
yield cosines that are wrong *and plausible*, which is worse than a missing
signal in exactly the way this ADR keeps finding.
[ADR-0024](0024-the-genome-is-one-dense-vector-per-title.md).

**In the measurement, `NeighborCandidate.tags` is `None` for a half-covered
pair.**

⚠️ **This site said "in the blend" until 2026-08-12, and the blend is where it
no longer is.** M9's S7 removed the genome term from `SimilarityService`
([ADR-0024](0024-the-genome-is-one-dense-vector-per-title.md)'s amendment: a
2.4746% candidate-pair rate against the 10% floor the 0.25 weight assumed). The
value is still read, still carried on the port DTO, and still counted — so the
rule below **still binds and now has exactly one consumer**,
`NeighborRebuild.pairs_with_tags`, which counts `tags is not None` and is the
number a later milestone would re-open that decision on. The argument's
*conclusion* is unchanged; the paragraph on structural consequence at the end
of this section is the part that describes a code path that no longer exists,
and it is marked there rather than deleted.

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

**And its consequence *was* structural rather than marginal — M7 to M9's S7.**
`_blend` renormalises over the signals that are present, so `None` correctly
dropped the term and scored the pair on what was known. `0.0` instead applied a
maximally negative genome term to every pair that straddled the coverage
boundary — which reordered every genome-bearing title's neighbour list to put
every *other* genome-bearing title above every un-genomed one. At the genome's
real coverage that is a small clique pinned to the top of the overwhelming
majority of lists, produced by a value no measurement supports, with no error
and nothing in any gauge to see it. **`SimilarityService` no longer blends this
value, so that specific damage is unreachable today** — kept in past tense
rather than deleted, because it is the argument that would apply again the day
any signal of this shape becomes a term.

**The live consequence is now the measurement, and it runs the other way.**
`0.0` is not `None`, so a port that answered `0.0` for a half-covered pair
would make `pairs_with_tags` count it — reporting a catalog the genome barely
touches as one it fully covers. That is a **dead signal looking live**, in the
one number an operator would use to decide whether to bring the term back.
Pinned on both arms:
`test_a_pair_carries_a_genome_cosine_only_when_both_sides_have_one` against the
real join, `test_a_half_covered_pair_is_not_counted_as_a_genome_pair` against
the counter.

⚠️ **`_clamped` is gone with the term, and that is worth saying because its
argument was good.** It kept `tags` inside `[0, 1]` in the **service** rather
than in SQL, for the reason `cosine`'s clamp still does:
`title_neighbors.score` is `CHECK (score >= 0 AND score <= 1)`, so the bound
has to hold for every implementation of the port rather than for the one that
remembered. And it was a function rather than a `min`/`max` at the call site
precisely because of this ADR — `max(0.0, value or 0.0)` is the obvious repair
for the `None` arm and it silently reintroduces the collapse. **Both arguments
transfer verbatim to whatever `[0, 1]`-valued optional signal becomes a term
next; neither survives as code, because a value that reaches no scorer needs no
clamp.**
`test_a_genome_cosine_a_port_put_outside_the_unit_interval_cannot_reach_a_score`
is what would notice `candidate.tags` being re-passed without one.
