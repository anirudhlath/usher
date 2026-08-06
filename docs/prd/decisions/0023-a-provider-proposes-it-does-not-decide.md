# ADR-0023 — A provider proposes; it does not decide

**Status:** Accepted. Implemented in M7 — settles PRD 06's composition
sketch and corrects one clause of it.
**Date:** 2026-08-03

## Context

[PRD 06](../06-rows-and-recommendations.md) describes composition in one
sentence: the home service *"collects all proposals, sorts by score, applies
diversity constraints (no three consecutive similarity rows; cap per family),
builds the top N concurrently, drops any that build empty, and returns them."*
That is two phases — every provider is asked `propose(ctx) -> Sequence[ScoredRow]`,
and only the survivors are asked to `build(ctx)`.

**The cheaper alternative is not a strawman.** It is shorter, it is what PRD
06's own last clause already implies, and a competent reviewer proposes it in
the first five minutes: build every provider eagerly, drop the empties, done.
The drop step exists either way, so the split looks like a phase bought for
nothing.

**And proposing genuinely costs a query.** `ContinueWatchingProvider.propose`
has to know whether anything is in progress, and the honest way to know is
`list_in_progress(user_id, limit=1)` — the same index scan `build` will run a
moment later with a larger limit. For every provider that ends up on the
screen, two-phase composition is *two* round-trips where one would have done.
That cost is real and is not argued away below; it is priced in Consequences.

## Decision

**A provider proposes. The composer decides.** `propose` returns 0..n
`ScoredRow`s; the composer sorts the union, applies the diversity constraints,
caps at N, and only then calls `build` on the survivors.

Three arguments, in the order they decide it.

**1. Diversity is a property of a set, and eager building never has one.** A
constraint over a set cannot be applied while the set is still being produced.
An eager builder can only apply it to what it has built *so far*, which makes
the constraint's input the build order — and the build order is registration
order, which is the order of lines in `services/rows/__init__.py`. **The home
screen would be ordered by a source file.** That is this milestone's headline
failure exactly: it renders identically to a working one, forever, with
nothing raised and nothing logged.

**2. The costs are not symmetric, and the asymmetry runs against eager
building.** `propose` is a bounded existence check. `build` is a hydration —
titles, media items and watch states for up to twenty cards. And PRD 06's own
table says the fan-out is uneven: `FranchiseProvider` emits *"1 row per
franchise"*, `BecauseYouWatchedProvider` *"1 row per seed"*,
`GenreAffinityProvider` *"1–3 rows"*. On a real library that is tens of
proposals of which a handful survive. The split trades **one cheap query per
kept row** for **one full hydration per dropped row**, and there are more
dropped than kept.

**3. An eager builder cannot be capped.** The build is sequential, because
`AsyncSession` is not safe for concurrent use. Sequential over an uncapped set
is unbounded latency in the request path — and there is no top-N to stop at,
because the top-N is decided by scores you only possess after proposing. The
two calls hold each other up: the sequential build is affordable *because* its
input is bounded by the cap.

## Consequences

- **N extra round-trips, one per kept row.** Real, unpriced today, and priced
  by `usher.home.compose.duration` plus [PRD 10](../10-telemetry-and-dashboards.md)'s
  per-provider breakdown. Stated as a cost rather than argued away.
- **A provider can still lie, so the drop step stays.** `propose` returning a
  `ScoredRow` is not a promise that `build` returns cards: a seed can vanish
  between the two, and a provider can simply be wrong. The composer must
  *still* drop empties. The split does not eliminate the empty-row case; it
  makes it rare. **This paragraph exists to prevent a future simplification** —
  a reader who expects the split to remove the drop step will remove it, and
  the first empty row on a home screen after that is invisible until a client
  renders an empty shelf.
- **Two methods to get right per provider, and they can disagree in both
  directions.** A `propose` stricter than its `build` silently suppresses rows
  that would have been fine; a looser one is the empty-row case above. Every
  per-provider test must exercise both directions.
- **"A provider returns nothing when it has nothing to say" is the same
  decision seen from the other end.** `propose` returning `0..n` is what makes
  "nothing" a legal answer rather than a degraded one, which is why the port
  carries no `limit`, `min_results` or `fallback` parameter: a signature
  asking for a minimum has already decided the popular-titles fallback exists.
  The genuinely dangerous form of that failure — a provider that, finding no
  signal, returns "popular titles" and produces a screen that looks
  personalised on a household that has watched nothing — is a *review* failure
  rather than a decision failure, and the artefact that catches it is nine
  per-provider cases with seeded distractors, not this document.
- **"Always ranked first" becomes arithmetic the composer can perform**, because
  there is a scored set in which to pin a maximum. It is spelled as a `pinned`
  flag on `ScoredRow` rather than as a high score: scores are minted per
  provider from unrelated signals and nothing normalises them, so a guarantee
  expressed as "a score high enough to win" is one another provider's
  arithmetic can silently take away.

## Rejected

**Build eagerly and drop empties.** The three arguments above. The first alone
decides it: the screen would be ordered by a registry module and would look
correct.

**One phase with a `dry_run: bool` flag.** A method whose behaviour forks on a
bool is two methods sharing a body, and the fork is where a provider forgets
to honour it. The in-repo precedent for refusing a bool that cannot express a
third state is `SearchMode` replacing `semantic: bool`, which *"could not
express `FUSED` at all"*.

**Providers return `BuiltRow | None` and the composer sorts on `len(cards)`.**
Makes row length the relevance signal, so a three-card "Because you watched
Dune" always loses to a forty-card "Recently Added" — a ranking rule nobody
chose, arriving as a consequence of a data shape.

## Evidence

PRD 06's provider table restated as costs, which *is* the argument: the
providers that are cheapest to propose are the ones most expensive to build,
and they are the ones that fan out.

| Provider | Proposals | `propose` reads | `build` reads |
|---|---|---|---|
| ContinueWatching | 1 | in-progress exists | in-progress + hydrate ≤ 20 |
| NextUp | 1 | any series with a next episode | next-up per series + hydrate |
| RecentlyAdded | 1 | any item in the window | window + hydrate |
| BecauseYouWatched | **1 per seed** | seed list | neighbours per seed + hydrate |
| Franchise | **1 per franchise** | collections with ≥ 2 owned | members per collection + hydrate |
| GenreAffinity | **1–3** | centroid present, genre concentrated | per-genre candidates + hydrate |
| Seasonal | 0–1 | calendar window | keyword match + hydrate |
| People | 0–2 | recurring person in history | credits per person + hydrate |
| Rediscover | 0–1 | any old high-engagement title | candidates + hydrate |

## Uncertainty

**The fan-out numbers in that table are PRD 06's specification, not a
measurement.** No provider exists at the time this is written; M7's later
groups build them, and its verification task measures the compose duration and
its per-provider breakdown.

So the decision rests on the *structure* of the two phases, not on a
benchmark — exactly as [ADR-0021](0021-the-suggest-path-is-its-own-port.md)
chose its port split *"on the structure of the two implementations, not on the
gate's result, which is the order that keeps it an architecture decision
rather than a reaction."*

If that measurement finds proposal queries dominating the compose duration,
the response is a shared per-request read that several providers consult — not
a return to eager building, because the diversity argument is independent of
the numbers.
