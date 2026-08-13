# ADR-0031 — The two-tier suggest: one route, two indexes, and a minimum prefix length

**Status:** Accepted. **Amends [ADR-0002](0002-postgres-first-search.md)** and
discharges the follow-up its failed gate opened —
[09](../09-roadmap.md) assigned *"the two-tier suggest"* to M9 and this is what
M9 built. Corrects [05](../05-search-and-similarity.md)'s autocomplete section
and [07](../07-client-api.md)'s `GET /search/suggest` row. Implemented in M9
(B2 built tier 1, B3 measured it, B5 put both on the wire).

**Written honestly, because the numbers underneath it are mixed.** Of the four
bars B3 committed before its run, **two passed and two failed**; both failures
are attributed away from the shipped code by measurement rather than by
argument; and the bar that passed does **not** cover the defect this ADR is
mostly about. The short version is at the top of the Evidence section and
nothing below softens it.

## Context

[ADR-0002](0002-postgres-first-search.md) gated Meilisearch on a measurable
typo-tolerance failure. The gate ran on 2026-08-03 against a real
1,271,138-title catalog and **failed both halves of a bar written down before
the numbers were known**: the shipped trigram + `levenshtein_less_equal`
type-ahead finds the right title **27.8%** of the time for a 2–4-character name
against a bar of 0.75, **68.3%** for 5–7 against 0.85, **0.0%** for a
transposition on a short name, and no configuration measured under any
threshold, cap or index type comes within **6×** of a 50 ms as-you-type budget.

ADR-0002's own conclusion was not "add an engine" — boundary call 7 declined
that — but **two tiers**: *"btree prefix on every keystroke, the trigram path
debounced behind it."* That sentence is a statement about a **request
boundary**: which tier runs, how often, and who decides. M6 added no route, so
there was nowhere to make it. This ADR is that place.

What existed before this decision: `PostgresPrefixSuggestIndex`
(`adapters/search/prefix.py`), a second implementation of the `SuggestIndex`
port ([ADR-0021](0021-the-suggest-path-is-its-own-port.md)) reading the two
`lower(name) text_pattern_ops` btrees `m09a` ships, over `titles` **and**
`title_search_names` as one `UNION`; and B3's measurement of it at catalog
scale.

## Decision

**1. One route with `?tier=`, not two routes.** `GET /search/suggest?q=&tier=`,
where `tier` is a `SuggestTier` enum (`prefix` | `fuzzy`) reaching
`/openapi.json` as an enum, **defaulting to `prefix`**. One resource, two ways
of answering it; a client that names a tier it cannot spell gets a 422 from the
enum rather than a plausible answer from whichever branch an `else` fell
through to.

**2. Both indexes are required collaborators of `SearchService`, neither
optional.** `m09a` creates both indexes unconditionally, so "built or not
built" has no state left to express — which is the argument
`SearchService.__init__` makes about `embedder` and `expander` one parameter
over, arriving at the opposite answer for a different reason. They are named
`prefix_suggestions`/`fuzzy_suggestions` rather than positioned, because two
adjacent parameters of one type are a swap nothing catches: swapped, the
keystroke tier becomes the 33.6 ms one, both answer plausibly, and only a case
asserting that a typo is **absent** from tier 1 can tell.

**3. Neither tier is re-ranked, and the hydration is written once.** Each index
already ordered its own answer inside its own capped set; applying the search
blend on top would count popularity twice and would make the two tiers
*disagree* about a row they both matched — which is the one thing a client
painting tier 1 and replacing it with tier 2 cannot absorb. Hydration is
`list_by_ids` + `owned_title_ids`, **two reads regardless of hit count and
regardless of tier**.

**4. The server does not debounce; the client does.** Nothing in this route
holds a timer, coalesces a request or drops one. A debounce is a decision about
a keyboard, the server has never seen one, and a server-side one would add
latency to the tier whose entire purpose is not to have any.

**5. Tier 1 does not run below a four-character prefix.** Below it the route
answers **200 with no results and issues no query at all**, and every response
— refused or served — carries `min_query_length` for the tier that answered.

- **Four is derived from B3's curve, not chosen.** The rule is *the shortest
  prefix at which tier 1's measured p95 is below tier 2's*, because the one
  thing the two-tier split rests on is that tier 1 is the cheap tier, and
  wherever that is false the split is upside down. Tier 2's shipped p95 is
  211 ms; tier 1's union p95 is **303 ms at three characters and 112 ms at
  four**.
- **Not the 10 ms keystroke bar**, which is met only from seven characters up.
  A minimum of seven leaves the tier that exists to answer every keystroke
  answering nothing for most of a typed word, and takes tier 1 away from
  exactly the short one-word names (`Up`, `Her`, `Dune`) that made ADR-0002's
  gate fail.
- **Measured on the stripped string.** Leading whitespace contributes no
  selectivity to `LIKE 'q%'`, so it must not count toward a length that stands
  in for selectivity.
- **Tier 2 is bounded at one character and not at four.** Nobody has measured
  the trigram statement per prefix length — B3's tier-2 figures are over whole
  mutated names — and a bound with no measurement under it is the shape this
  project has already been bitten by (`ports-and-error-taxonomy.md`: *"a
  refusal justified by 'this cannot happen' is one measurement away from firing
  constantly"*). Tier 2's defence is the client's debounce, which is the
  design.
- **Not a `Settings` field.** The number is a property of catalog size, not of
  an operator's preference — at the 10,000-title enriched tier the same
  one-character probe is 489 ms and four characters is 5.5 ms — and a knob here
  is a latency budget expressed as a character count, which nobody can set
  correctly without re-running B3.
- **The bound is at the route and not in `SearchService`**, so `usher suggest`
  keeps unbounded access. A command is typed once; refusing it a
  three-character prefix would take a capability away from the one caller that
  can afford it, and diagnosing tier 1 at one character is exactly what an
  operator opens that command to do.

**6. The response echoes the tier that answered**, on `requested_mode`'s
argument and minus its second field. There is one field rather than two because
a tier request is always *served* by the tier it named — both indexes exist on
every deployment — so a `requested_tier` could never differ. The echo is still
owed: `?tier=` has a default, so a client that named no tier is reading an
answer from a tier it did not choose, the two tiers give **different answers to
the same `q`** by design, and this ADR records changing the default as a live
possibility.

**7. `usher suggest --tier` defaults to `fuzzy` where the route defaults to
`prefix`, and `SearchService.suggest` takes `tier` as a required keyword with
no default at all.** The two boundaries want opposite answers and each states
its own; a default on the service would be one of them silently serving the
other. `usher suggest` has been the typo-tolerant one since M6 and CLAUDE.md
documents it as such.

## Consequences

**Gained.** The gate's own prescription, on the wire, with both halves
separately askable and the answer self-describing. A keystroke path that is a
keystroke path from four characters up, and an explicit, legible refusal below
it instead of a 2.7-second one. No new engine, no dual write, no
`search_queries` write, no LLM on the keystroke path — structurally, since
`QueryExpansionService.expand` is called from one line in front of the semantic
embed and `suggest` has no embed at all.

**Given up.** **Tier 1 answers nothing for the first three characters of every
query**, which is a real product cost and is the price of the paragraph above.
On a small catalog it is a cost with no benefit — the curve is steep in
coverage, and at 10,000 covered titles a one-character probe is 489 ms rather
than 2,707 — and a deployment that could afford three-character prefixes cannot
say so. That is the trade a fixed constant makes and it is recorded rather than
hidden.

**Not closed.** Tier 1 misses a 10 ms keystroke budget on **both** arms at every
prefix shorter than seven characters, and this ADR does not fix that — it
declines to serve the worst of it. The mechanism is known and named below; the
fix is a change to the statement, which needs a measurement this task did not
take.

**A `score` that means different things on the two tiers.** Tier 1 answers 1.0
for every row, honestly — every row is an exact prefix match, so the distance
tier 2 varies its score with is zero for all of them. A client must render the
order, not the number.

## Evidence

**B3's run, 2026-08-12**, on the gate's own 1,271,138-title `--phase imdb`
catalog with a `title_search_names` **person** arm of 10,896,525 rows over
1,191,768 titles (the `alias` arm was empty — T7's), on a box the harness
verified quiet (`quiet_enough: True`, idle-sampled CPU drift **+0.0025**),
against a bar committed before the first number.

| | bar | measured | |
|---|---|---|---|
| (1) tier-1 p95, `titles` only | ≤ 10 ms | **0.947 ms** | **PASS** |
| (2) tier-1 p95, union at 10.9M rows | ≤ 10 ms | **1.465 ms** | **PASS** |
| (3) tier-2 p50 | 33.6 ms ±10% | **39.59 ms** | **FAIL** |
| (4) tier-1 recall@5 | 1.9% (1.6–2.2) | **2.67%** | **FAIL** |

**Both failures are attributed away from the shipped code by measurement.**
Bar (3): a within-run A/B over the identical 2,993 cases, with both `m09a`
prefix indexes present and then both dropped, gives **39.593 ms against 39.571
ms, ratio 1.001, byte-identical recall** — so the GIN/GiST lesson that an added
index can silently tax the shipped path **does not generalise to a btree**, and
the 6 ms against 2026-08-03 is run-to-run. Bar (4): 2.67% against a 1.6–2.2%
window is **23 cases out of 2,993**, and tier 1 finds a typo'd name essentially
only when the edit lands on the last character and leaves a true prefix — so
recall is a function of the sampled names' lengths, and the draw order the gate
never recorded is the one input known to differ.

**Bar (2) passed, so B2's union arm ships and the narrowing is not made.** The
plan's pre-recorded failure consequence — narrow tier 1 to `titles` and reach
`title_search_names` from tier 2 alone — did not fire, and B3 declined to fire
it on a workload other than the one the bar named.

🔴 **The defect bar (2) does not cover, and it is the reason this ADR exists in
the shape it does.** Bars (1) and (2) are scored on the gate's 2,993 typo
strings, which are whole mutated names: long and selective. **A keystroke is
not that.** p95 by prefix length, at `--reps 5`:

| characters typed | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| tier 1, `titles` only | **291 ms** | 51 ms | 15 ms | 5 ms | 19 ms | 14 ms | 2.0 ms | 2.3 ms |
| tier 1, union at 10.9M | **2,707 ms** | 809 ms | 303 ms | 112 ms | 100 ms | 86 ms | 2.3 ms | 2.6 ms |

**Tier 1 is a keystroke path from seven characters up and nowhere below it, on
both arms.** Worst single probe measured: `'m'` at **2,744 ms**, 78,203 `titles`
rows and 1,069,834 `title_search_names` rows. Two things follow and both are
load-bearing here. Narrowing the union was never the fix — it would have moved
a 291 ms first keystroke to where a 2,707 ms one had been. And **below seven
characters tier 1 is the slower of the two tiers**, so at the short end the
split's cost and its benefit are the other way round from the summary sentence
everybody reaches for; `api/routers/search.py`'s module docstring carries the
curve rather than a single p50 for exactly that reason.

**The mechanism, which G7 got wrong.** B2 declined an inner per-arm cap partly
because *"an ordered one costs the same sort, so it buys nothing"*. In the plan
for the worst probe the sort is a **top-N heapsort in 26 kB**, costing
microseconds. The cost is the `UNION`'s de-duplication — a `HashAggregate` at
**17 batches spilling 47 MB to disk per worker** — plus a `title_search_names`
bitmap heap scan that **goes lossy** (5,664,971 rows removed by filter to keep
1,069,834), plus a hash join back to `titles` at 16 batches. The index probes
themselves are 6 ms and 40 ms.

**Parallelism is not the lever and coverage is.** For the four largest probes,
serial against parallel is **0.997–1.029** — the work is disk-spill and heap
recheck, not CPU — so the 2,744 ms does not degrade further on a busy box. At
the 10,000-title enriched tier the union's one-character p95 is **489 ms** and
by four characters **5.5 ms**, with the titles-only column identical across both
runs (291.23 against 290.24 ms at length one), which is the control that says
table size was the only variable.

**The union costs tier-1 recall slightly**: 2.34% union against 2.67%
titles-only, entirely in the two short bands, with 8 characters and up
identical. Person-name rows crowd the true title out of a five-row box.

**Why tier 1 is not typo-tolerant, by design rather than by shortfall.** A btree
`text_pattern_ops` index answers a range condition on a prefix; there is no
edit distance in it and no way to add one without changing the index type, at
which point it is tier 2. Measured typo recall is **1.9%** in the gate's run and
**2.67%** in B3's — the difference is 23 cases and a different draw of names,
and neither number is a target to improve. The tier is the only configuration
measured that fits inside a keystroke at all, and it is worth having precisely
because it is that narrow. Tolerance is tier 2's job and tier 2 is 200–330×
slower; a single index doing both is what ADR-0002's gate spent 2,993 cases
proving does not exist. `SuggestIndex`'s port docstring stopped promising
tolerance in M9 for this reason.

**Index build and size**, for completeness: `ix_titles_name_lower_prefix`
**0.666 s / 44.2 MB** over 1,271,138 rows (the gate measured 0.559 s / 44 MB —
size reproduces exactly, build time is run-to-run);
`ix_title_search_names_name_lower_prefix` **4.527 s / 155.4 MB** over 10,896,525
rows, which has no prior number because the table did not exist when the gate
ran.

Full tables, the regeneration procedure and the harness findings are in
`.claude/rules/search-and-embeddings.md`.

## Alternatives considered

**Two routes with different cache TTLs, instead of one route with `?tier=`.**
Genuinely arguable and recorded rather than dismissed: the two tiers have very
different cost and very different staleness tolerance, and
[A4](../07-client-api.md)'s `conditional_response` would let a prefix route
carry a long `max-age` while the fuzzy one carried none. It is not taken **now**
because deciding it after the cache work landed would be a wire change to a
route clients may already have written against, and because the two tiers are
one resource asked two ways rather than two resources. **Neither route is
cached at all today**, which is what keeps the option open: adding
`Cache-Control` to one tier of one route is additive, and splitting the route
later is not.

**Narrowing tier 1 to `titles` and reaching `title_search_names` from tier 2.**
Pre-registered by B3 as the consequence of bar (2) failing. Bar (2) passed, and
the per-length curve says the narrowing would not have bought a keystroke path
anyway.

**An ordered inner per-arm cap.** Now known to be **far cheaper than B2 priced
it** — it would bound the `HashAggregate`'s input rather than pay a sort that is
already free — and it is the most promising unexplored fix for the sub-seven-
character range. **Not made here**, for two reasons and the second is the one
that decides it: B5 changes no SQL (this task ships a route, a DTO, a service
parameter and this document), and B3's per-length curve is a measurement *of the
shipped statement* — changing that statement without re-running the curve would
leave this ADR describing a query that no longer exists. It is the first thing
a follow-up should measure.

**A minimum of seven characters, i.e. the 10 ms bar.** Rejected under Decision
5: it would make tier 1 answer nothing for most of a typed word and would
abandon the short-name band the whole two-tier design exists to serve.

**A server-side debounce.** Rejected under Decision 4. It is latency added to
the tier whose purpose is not to have any, on behalf of a keyboard the server
cannot see, and it would make two clients with different typing speeds share one
coalescing window.

**Tier 1 as a fallback for tier 2 (or the reverse), rather than as a peer.**
Rejected in the vocabulary itself: `SuggestTier` is an enum rather than a
`typo_tolerant: bool` precisely because a bool invites reading one tier as a
degraded form of the other. They are complements — one has no tolerance, the
other cannot meet a keystroke budget at any setting — and a route that silently
fell back would hide which one answered on exactly the requests where that
matters.

## Uncertainty

Named rather than implied, and none of it is settled by this decision.

- **Real typed queries.** Every latency figure above is over synthetic
  prefixes of, or single-edit mutations of, real catalog names. What people
  actually type is [10](../10-telemetry-and-dashboards.md)'s `search_queries`
  table, whose write is group F's and which has no rows until after M9 ships.
  The four-character minimum is the first thing that table should be pointed at.
- **Tier 2 per prefix length**, which is why its bound is one character. Its
  33.6 ms p50 / 211 ms p95 / 730 ms max are whole-name figures.
- **The alias arm.** B3 measured a union whose `title_search_names` held
  10,896,525 **person** rows and no aliases; T7's writer landed afterwards, so
  the shipped table is larger than the one the curve was taken over and the
  curve is therefore optimistic in the direction that matters.
- **An enriched catalog.** Every number here is from a bootstrap-only catalog
  with `popularity` NULL throughout, so tier 1's ordering degenerates to
  `vote_count DESC, id ASC` on the measured deployment.
- **Non-Latin scripts**, where `lower()` and Python's `casefold()` diverge and
  no case in this repository tests one.
