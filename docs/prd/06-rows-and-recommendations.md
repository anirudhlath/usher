# 06 — Rows and recommendations

The home screen is composed, not configured. Rows are proposed by providers,
scored for relevance, and assembled per request — so the screen changes with
context, season, and taste rather than being a fixed list.

## The Row hierarchy

```python
# usher/ports/rows.py
class Row(ABC):
    """A named, ordered shelf of titles, able to build itself."""

    # abstract *properties*, not bare annotations: a bare annotation is a
    # class-variable declaration, so a subclass that forgot one inherits
    # None and fails at render time rather than at instantiation — the
    # failure ADR-0001 chose ABCs to avoid.
    slug: str                    # "continue-watching", "because-you-watched-dune"
    title: str                   # display name
    reason: str | None           # subtitle, written to be spoken aloud
    family: RowFamily            # the diversity key
    display_hint: DisplayHint    # ADR-0006's hint, never a layout
    ttl: timedelta               # how long a built result may be cached

    @abstractmethod
    async def build(self, ctx: RowContext) -> BuiltRow: ...

# usher/services/rows/base.py
class BaseRow(Row):
    # concrete, shared:
    async def hydrate(self, title_ids: Sequence[UUID]) -> list[RowCard]: ...
    def empty(self) -> BuiltRow: ...
```

**The ABC is a port and the shared behaviour is a base class**, which is a
correction to this section's earlier sentence (*"The `Row` ABC lives in the
services layer because it has behaviour and dependencies"*). That sentence
gives a reason for the **base class** to be in `services/` and not for the
**abstraction**: `hydrate()` reads a `TitleRepository` and a
`MediaItemRepository` off the context (**two**, not the three an earlier draft
of this sentence claimed — the watch-state read is the caller's, per row), and
a concrete method on a port is a port with a dependency — `ports/` has none
today. The mechanical
half is that `test_every_port_abc_is_registered_in_all_ports` walks
`usher.ports.*` and is the only thing that checks an ABC is an ABC, so a `Row`
in `services/` would get neither of ADR-0001's two checks. The DTOs stay pure
either way.

`build` returns a `BuiltRow`, never `BuiltRow | None`: an empty row and an
absent row are different states, and the composer's metrics count them
separately.

`RowContext` is a frozen dataclass of ports plus an injected clock — eleven
fields as shipped in M7 and ✅ **twelve as of M8**:

```python
user: User                          now: Callable[[], AwareDatetime]
titles: TitleRepository             media_items: MediaItemRepository
watch_states: WatchStateRepository  episodes: EpisodeRepository
neighbors: TitleNeighborRepository  people: PersonRepository
credits: CreditRepository           collections: CollectionRepository
curated: CuratedRowRepository
affinities: Callable[[], Awaitable[Sequence[GenreAffinity]]]
```

- **No `AsyncSession`, and that is checked rather than commented.**
  `AsyncSession` is not safe for concurrent use, so a context carrying one is
  a context ten providers can `asyncio.gather` over — which *usually works*,
  and fails as an intermittent error under load. A row holding repositories
  has no session to share.
- **The clock is injected** because `SeasonalProvider` fires on a calendar
  window and `RediscoverProvider` on "watched > 2 years ago". A wall-clock
  read makes the first testable only in October; a fixture dated two years
  back stops meaning what it meant as the calendar moves. It is on the
  context rather than each provider's constructor because providers are
  registered once, and a per-request clock cannot be a singleton's
  constructor argument.
- **`curated` is the twelfth and it arrived with its reader.** M8's
  `CuratedProvider` and this field land in one change, which is the discipline
  the two deleted fields below did not have — the port and the table existed
  three tasks earlier, which is exactly the pull that put `search` here three
  groups before anything retrieved. It is the *repository*, not a generation's
  rows: a provider is constructed once at import, so per-household data cannot
  ride on its constructor, and pre-reading the rows would make every
  `GET /home` pay for a shelf the composer may not select.
- **`search: SearchIndex` and `taste: Centroid | None` were specified here and
  are not shipped.** Nine providers were built and none read either. Every row
  turned out to be a *predicate over a repository* rather than a retrieval, so
  nothing needed the index; and `taste` was worse than unread — on the request
  path `TasteService.centroid` returns `None` unconditionally, because the
  centroid needs an embedder and the route deliberately holds none, so every
  `GET /home` paid a `user_taste` read for a value that was both unused and
  unusable. A field with no consumer is what this project deletes; the
  argument is `RowCard`'s absent artwork field, one layer up.
  `test_every_row_context_field_is_read_by_at_least_one_provider` now scans for
  the next one rather than counting.
- **`affinities` replaced `taste` as the taste signal a row actually reads.**
  This section used to fire `GenreAffinityProvider` on *"taste centroid
  concentrated in a genre"*; implemented literally that makes the most
  broadly-useful provider the one that never fires, because the embedder is
  optional and off by default (ADR-0022). It fires on lift over the owned
  library instead — counts over `titles.genres`, no embedder required. Fewer
  rows, not worse rows.
- ✅ **`affinities` is awaited rather than held, so a cached screen costs
  nothing.** It shipped as the sequence, computed while FastAPI resolved the
  dependency graph — which happens *before* the handler runs, and therefore
  before `HomeService` can look in the ~30 s screen cache. So every
  `GET /home`, hit or miss, paid `list_recent(50)` + `list_by_ids(50)` + a
  library-wide `unnest(genres) GROUP BY` over 1.27M titles to fill the one
  field of twelve that a single provider reads. Deferring it to
  `GenreAffinityProvider`'s own `await` makes the hit — which is most requests
  — free, and leaves the miss costing exactly what it did. The alternative,
  looking in the cache before assembling the context, was declined: the entry
  can expire or be invalidated between the lookup and the compose, and the
  screen composed from the empty affinity that follows would then be cached
  for another 30 s. It is the only field of the twelve that is a callable
  besides the clock, and it is the only one whose value is the product of
  three statements.

So rows are pure functions of context and trivially testable with fakes.

`BuiltRow` and `RowCard` are Pydantic DTOs (`usher.domain.rows`). `RowCard`
carries `title_id`, `kind`, `name`, `year`, `enrichment_state`, `owned`, and
the progress triple `position_seconds` / `runtime_seconds` / `played`, plus ⏳
`episode_id` / `episode_label` for the two rows that are about a chapter.

⏳ **`title_id` is always the *series*, never the episode**, and the chapter
rides alongside it. Every other field on the card — `kind`, `name`, `year`,
`owned`, `enrichment_state` — describes the series, so a `title_id` that
sometimes meant an episode would be a second vocabulary in the one field every
provider's cards agree on. `episode_id` is what makes a Next Up card
*playable*: without it the card can only navigate to the series page, which is
one more click than the row exists to remove. `episode_label` (`"S02E05"`) is
composed on the server so the zero-padding is decided once rather than by each
client — ADR-0006's "the server composes", applied to a string. Both are `None`
on every card of the other seven rows.

Three fields an earlier draft of this sentence listed are **deliberately
absent**, each for a stated reason:

- **artwork refs** — M9 owns the `Image` entity, the `images` table and
  `GET /images/{id}`. There is no `poster_path` on `titles` either, so the
  choice was an always-null field or no field, and
  [ADR-0006](decisions/0006-server-composed-home.md)'s sibling call on
  `GET /titles/{id}`'s absent `images` key settles it the same way: *"an empty
  list would be indistinguishable from a film with no cast."* M9 adds the
  field additively, to a DTO that never lied about having it.
- **rating** — `watch_states` has no rating column at all, and neither does
  `SourceWatchState`. A `rating` on a card is a field with no source.
- **progress**, as a fraction — `runtime_seconds` is nullable, so a fraction
  is either a division by `None` or a division by a `COALESCE`d zero, and the
  latter renders every partially-watched title as finished. The card carries
  the raw pair: *"half an hour in, of an unknown total"* is two true facts.
  ADR-0014 at the card.

`extra="forbid"` on `DomainModel` makes those absences runtime refusals rather
than naming conventions.

### Three families

`RowFamily` (`usher.domain.rows`) is the typed vocabulary these three names
denote, and it is what the diversity constraint below is stated in — a slug
cannot serve, because `because-you-watched-<seed>` is per-seed and a
slug-keyed rule would couple the composer to the catalog.

| Family | `RowFamily` | Built from | Examples | TTL |
|---|---|---|---|---|
| **`SourceRow`** | `SOURCE` | Catalog and watch state in Postgres | Continue Watching, Next Up, Recently Added, genre shelves, collections | ~60 s |
| **`SimilarityRow`** | `SIMILARITY` | Embedding / genome neighbours of a seed title | "Because you watched *Dune*", "More like this" | hours |
| **`LLMRow`** | `CURATED` | A persisted `curated_rows` record | "Slow-burn sci-fi for a rainy night" | 5 min — see below |

`LLMRow.build()` only *hydrates* stored output. Generation happens in a
background job — never in the request path.

✅ **And the shelves of one generation hydrate together.** `CuratedProvider`
returns up to five rows from a *single* `list_for_user`, so every card id in
the family is in hand before anything builds; the composer's per-family cap
then builds four of them, and one catalog read plus one ownership read each
was **eight statements for the ~22 distinct ids one generation names**. The
first shelf to build now reads the union for all of them and the rest read from
it — at build time, not propose time, so a shelf the cap discards still costs
nothing, and a shelf served from the row cache never reaches the memo at all
because `HomeService` returns the cached row before calling `build`.

> **Three surfaces reach that job and only one of them reports what it did.**
> `POST /admin/rows/regenerate` enqueues a `curate` job and answers 202 with
> the key; `usher work` claims it, but only if this process built an
> `LLMClient` at all. ✅ **M8** adds `usher curate`, which runs one generation
> *in the foreground* against a real database and prints what it bought — the
> pool it chose from, the rows kept, the drops by reason with all five reasons
> and their zeros, the token counts and the cost. It is the only place an
> operator sees the answer in the same breath as the request, which is what a
> command that spends money owes: a 202 says nothing about what a completion
> returned, and the ledger row it leaves behind is a row somebody has to go
> and query. It writes through the same `CurationService` as the job, so it is
> a surface rather than a second implementation.

> **"Until regenerated" was this table's TTL cell and it is corrected here: it
> is the *artefact's* lifetime, not the cache's, and read as a TTL it inverts.**
> The stored row really is immutable until a generation replaces it — but
> `RowCache` holds the whole built row under `(user_id, slug)`, and a generation
> of the same row count re-uses the same slugs, so a long TTL does not keep a
> fresh row fresh: it keeps *last night's* row on the screen. Nothing
> invalidates that entry, because the cache is in-process in the API and the
> curation job runs under `usher work`; cross-process invalidation is M9's. So
> the number is a staleness bound, and `POST /admin/rows/regenerate` is what
> turns it into an operator watching a screen that has not changed. Five
> minutes, matching `RecentlyAddedProvider`'s for the sibling reason: both rows'
> content moves on an event the API process never observes.

**`RowFamily` had two members in M7 and its third arrived with its emitter.**
Boundary call 2 gave `curated_rows`, `LLMRow`, `CuratedProvider` and
`POST /admin/rows/regenerate` to M8 as one family — hydrating a table whose
generator does not exist would fix that table's shape before anything had
tried to fill it — and `CURATED` was deliberately not pre-declared, because a
"cap per family" over a family with no members is a branch nothing can reach.
It cost one line in the diff that added `LLMRow` — ✅ **M8**,
`services/rows/curated.py`, which is the only thing that emits `CURATED`.

**What the third member made reachable, since that was the question the
deferral was protecting: `_MAX_ROWS`, and not the cap.** The two cases that
exercise the cap — `tests/unit/test_services_home.py`'s
`test_no_family_exceeds_its_cap_even_when_it_proposes_the_top_scores` and
`test_a_proposal_the_cap_declined_is_selected_zero_rather_than_absent` — have
each proposed **eight `SIMILARITY`** rows since M7, and the cap has never
cared how many families exist. The screen ceiling had not been reachable at
all — see the composition section below.

## Dynamic composition

Rows are proposed rather than listed — a provider proposes, and the composer
decides. That split is contested, because the cheaper alternative (build every
provider eagerly, drop the empties) is shorter and is what the paragraph below
reads like an endorsement of; the reasons it is wrong are recorded in
[ADR-0023](decisions/0023-a-provider-proposes-it-does-not-decide.md), the first
of which is that diversity is a property of a *set* and an eager builder never
has one — so its constraint's input becomes build order, which is the order of
lines in a registry module.

```python
class RowProvider(ABC):
    @abstractmethod
    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """Return 0..n candidate rows with relevance scores."""
```

`Sequence`, not `list`: a caller must not mutate a provider's return, and a
provider that hands back its own cached list finds that out the hard way.

`ScoredRow` (`usher/ports/rows.py`) carries the `Row` itself, its `score`, and
a `pinned` flag. It carries the row rather than a slug because the slug form
needs a `dict[str, Row]` on the composer — a second source of truth, and a
`KeyError` waiting for the first provider that proposes two rows under one
slug.

**Scores are module constants, not configuration**, and `pinned` is how
`ContinueWatchingProvider`'s *"always ranked first"* is expressed. "Always
first" is a **positional** guarantee; scores are minted per provider from
unrelated signals with nothing normalising them, so a guarantee expressed as
"a score high enough to win" is one another provider's arithmetic can silently
take away. Never a slug comparison: `because-you-watched-<seed>` is per-seed,
so a slug-keyed rule couples the composer to the catalog.

A provider returns nothing when it has nothing to say. The home service collects
all proposals, sorts by score, applies diversity constraints (no three
consecutive similarity rows; cap per family), builds the top N
**sequentially**, drops any that build empty, and returns them.

> **"Concurrently" was wrong and is corrected here rather than implemented**
> ([09](09-roadmap.md)'s M7 boundary call 8). `AsyncSession` is explicitly not
> safe for concurrent use — two coroutines awaiting on one session interleave
> on one connection — so `asyncio.gather` over ten providers sharing a
> request's session is a corruption, and one that *usually works*: two short
> reads frequently complete, and the failure is an intermittent
> `InvalidRequestError` or a result set attributed to the wrong query, under
> load. The two escapes are worse at this scale — a session per row is ten
> connections for one home screen, and a semaphore has no lane to belong to.
> Every provider's query is a bounded local read;
> `usher.home.compose.duration` and the per-provider `usher.row.build.duration`
> breakdown are what turn revisiting this into a number rather than an
> argument, and `usher home` measures it.
>
> **The number, so the call is a decision rather than a preference.** Measured
> 2026-08-04 against a real 1,271,570-title catalog with a synthetic household
> on it (5,200 owned copies, 360 watch states over two years, 50 collections,
> 1,800 credits and 6,000 neighbour rows): **cold p50 23.9 ms, p95 35.9 ms**, warm
> 0.0 ms, eight rows and 115 cards. The slowest provider is
> `because-you-watched` at 4.3 ms — **34% of build time**, so no single
> provider dominates. **p95 is 11× under the 400 ms budget.** The rule for
> revisiting this was written before the run and both clauses must fire: p95
> above 400 ms *and* no single provider at ≥ 50% of build time. Neither does,
> so sequential stands on evidence rather than on the argument alone —
> [ADR-0025](decisions/0025-rows-build-sequentially.md), which also records
> what would make this the wrong answer (thirty providers, or one that calls
> out of process).
>
> ⚠️ **That figure is scoped to a 5,200-copy household and must not be read as
> a property of the composer.** Re-measured 2026-08-05 against the scale
> ceiling — a synthetic population owning all 1,277,878 items with 1,086,149
> played — compose is **p50 710.3 ms, p95 783.4 ms**, i.e. **2× over** the
> budget rather than 11× under it. The decision does not change, and the reason
> is the rule's second clause: `genre-affinity` is **98%** of build time there,
> so the answer is to fix one provider rather than to run nine concurrently on
> one session. `next-up` costs 302.9 ms to *propose* at that scale, which is
> the other number worth carrying.
>
> **Built in M7 as `services/home.py`.** The diversity constraints are two
> rules at two stages, and the split is deliberate: the **per-family cap** is
> applied at *selection*, because it bounds how many rows get built; the **no
> three consecutive similarity rows** rule is applied to the *returned
> sequence*, after rows that built empty are dropped. Applied at selection
> instead, the sequence `[S, X, S, S]` with `X` building empty returns
> `[S, S, S]` — a violated constraint with nothing raised anywhere, on a
> screen that still looks fine. A row that would be the third is **deferred**
> and re-offered at every later position rather than dropped; with nothing to
> interleave, the screen stops at two similarity rows, which is the constraint
> taking precedence over screen length.
>
> The sort key is `(-score, slug)`. **The tie is broken by the slug and never
> by registration order** — a screen ordered by the order a registry happened
> to yield is a screen whose order is a property of a tuple literal.
>
> `ContinueWatchingProvider`'s "always ranked first" is a **rule the composer
> applies**, not a score. Provider scores are not on a common scale and nothing
> normalises them, so a positional guarantee spelled as a large float is one
> that another provider's arithmetic can silently take away.
>
> `_MAX_ROWS` (10) and `_MAX_PER_FAMILY` (4) are module constants and
> constructor defaults rather than settings: the mechanism exists, but the
> reason to move either is an operator looking at a screen, which is M9's admin
> surface, and `Settings` is `extra="forbid"` so every field there owes a
> reader *and* a reason. **With two families the longest screen the composer
> could return was nine rows** — one pinned plus four per family — and the
> *registry* could only reach eight of those, because
> `BecauseYouWatchedProvider` is the only `SIMILARITY` emitter and its
> `_MAX_SEEDS` is 3. Both are under `_MAX_ROWS`, which is the point: it
> truncated nothing at any input, and the only case that reached that slice
> injected a smaller ceiling. The "one pinned" term is a property of the
> registry rather than of the composer — `_select` sets every pinned candidate
> aside *before* the cap, with no bound of its own — and
> `tests/unit/test_rows_invariants.py::test_continue_watching_is_the_only_provider_that_pins_and_it_pins_one_row`
> is what holds it. ✅ **M8's `RowFamily.CURATED` is what made it reachable**:
> thirteen candidates get past the cap and three are dropped. The case pins it
> on what was **built** rather than on what came back, because `_order` bounds
> the returned sequence by the same number — so an over-selecting composer
> returns ten rows having hydrated thirteen, which no length assertion can see.
>
> Each provider declares a `slug_prefix` (`continue-watching`,
> `because-you-watched`), and every row it proposes mints its slug from that
> constant. It is what `usher.row.build.duration`'s `provider` label and
> `usher home`'s report both carry: bounded at ten, where a row slug is
> bounded by the catalog — `because-you-watched-<seed>` per seed and
> `curated-01`, `curated-02`, … per shelf per generation.

| Provider | Fires when | Emits |
|---|---|---|
| `ContinueWatchingProvider` | Anything in progress | 1 row, always ranked first |
| `NextUpProvider` | Series with an unwatched next episode — **started, never merely owned**; see below | 1 row |
| `RecentlyAddedProvider` | New items in the window | 1 row |
| `BecauseYouWatchedProvider` | Recent high-engagement titles carrying neighbours | 1 row *per seed*, **capped at 3**; see below |
| `FranchiseProvider` | ≥ 2 owned titles in a collection **and ≥ 1 of them unplayed** — **movies only** | 1 row per franchise, capped at 2 |
| `GenreAffinityProvider` | ⏳ **The household watches a genre disproportionately to its share of their library** — *not* "taste centroid concentrated in a genre"; see the Taste section | 1–3 rows |
| `SeasonalProvider` | Calendar window (Halloween, holidays) — **curated by the author, not derived**; see below | 0–1 rows |
| `PeopleProvider` | Recurring director or actor in history — **3 distinct engaged titles, in a cast or directing credit**; see below | 0–2 rows |
| `CuratedProvider` | ✅ **M8** — a generation is in `curated_rows`; it hydrates, never generates. `0–5` is enforced *here*, because the validator deliberately caps nothing; see below | 0–5 rows |
| `RediscoverProvider` | Watched > 2 years ago, **most-rewatched first** — there is no rating column; see below | 0–1 rows |

✅ **All ten are registered as of M8.** Nine landed in M7 and boundary call 2
gave `CuratedProvider` — with `curated_rows`, `LLMRow` and
`POST /admin/rows/regenerate` — to M8 as one family, so this table was
annotated rather than silently shipped short; M8's task 15 registered the
tenth. The registry is `services/rows/__init__.py`'s `ROW_PROVIDERS`, and it is
the composition point: **a provider that is not registered is dead code, and
dead code that looks exactly like a provider with nothing to say** — which is
the one failure a composed home screen cannot show from the outside. It holds
ten, asserted by name rather than by count, and four cross-provider invariants
are parametrised over it, so the tenth provider was covered by four cases on
the day it was written: that only Continue Watching reaches the top score, that
every provider returns nothing against an empty database, that none falls back
to popular titles on a household that has watched nothing, and that every one
composes with no embedder.

**`CuratedProvider` is deliberately not on the "may fire on a household that
has watched nothing" allowlist**, which the other three library-shaped
providers are on: a curated shelf **is** a claim about the person, so proposing
one for a household with no generation would be the popular-titles fallback
arriving through the one door that costs money.

**Its score is `0.85` and it is the first in this project chosen against the
whole table rather than against one sibling.** Continue Watching (1.0, pinned)
and Next Up (0.90) are about *intent* — something the household is in the
middle of, and the next episode of something they are watching — and a shelf a
model proposed overnight must never outrank either. Everything at 0.80 and
below is a discovery claim computed from a single signal (one seed's
neighbours, one library event, one genre's lift, one recurring face, the
calendar, one collection, one crossing of the two-year line); this one reads
the household's whole recent history against a 200-title pool and is the only
row on the screen that cost money, so it sits above all seven. **Being
outranked here is "not shown" rather than "shown lower"** — the screen is ten
rows and a rich household proposes more — so a score below 0.80 would be spend
with no screen to show for it on exactly the households curation is most worth
buying for. Every shelf in one generation carries the *same* score, because
`(-score, slug)` already breaks the tie on a positional, zero-padded slug: the
model's ordering is spelled once, in the slug, and a per-row decrement would be
a second spelling of it.

**`SeasonalProvider`'s calendar→signal mapping is a taste judgement with no
data source, and it is the only thing in `services/` of that kind.** Nothing in
the catalog, in TMDb, in MovieLens or in the household's history says October
means horror. It ships as a module-level table of three windows curated by the
author — Halloween/Horror, December/`christmas`, Valentine's/Romance — marked
as curated in the module that holds it. The table is Gregorian,
northern-hemisphere and anglophone by construction: there is no Diwali window,
no Lunar New Year window and no southern-hemisphere summer, because adding them
would be the same guess made less carefully rather than a measurement. It is
code rather than configuration so that changing it is a change with a diff.

**Those three windows are 46 days, so this provider returns nothing for
roughly 320 days of the year.** That is the correct behaviour and is written
down here so an operator does not read a missing seasonal row in March as a
fault. Two properties are asserted about the table rather than about a built
row, because both failures produce a row that is permanently absent with no
error anywhere: no window may wrap the year end (`(12,27) <= today <= (1,2)`
is false for every date), and no row TTL may outlive the shortest window (a
cached Halloween row is correct when built and wrong when served in November).

**`PeopleProvider` means three distinct engaged *titles* in a cast or
directing credit, and both halves are argued.** Two is a coincidence in any
household that watches a studio's output — two films from one franchise share
dozens of crew — so a threshold of two makes "recurring" mean "appeared in a
sequel". And *distinct titles*, never credits: a person credited twice on one
film is one title's worth of evidence, and counting credits returns a real
person the household really watched, ranked first, wrongly. The role half
matters as much: a person credited on six films as a gaffer is recurring under
any counting rule and means nothing, because below the line crews repeat when
studios repeat. **A billing-order bound is not part of it and cannot be** —
`list_recurring_for_user` groups by `(person_id, name, kind, job)` and the row
it returns carries no billing rank, so "top billed" would have to be applied
before that grouping. The stored cast is already bounded at 50 per title by
the derivation. Rows are picked by title count, then by the most recent title
crediting the person, then by id: two directors at four titles each, one from
last month and one from 2019, is otherwise decided by whatever the aggregate
returned.

**`GenreAffinityProvider`'s row is proposed on the affinity and its cards are
read when it builds**, which is why a genre whose owned titles the household
has all watched produces a row that builds *empty* and is dropped, rather than
one that was never proposed. The two are different states and the metrics have
to tell them apart. Its cards are owned *and* unwatched — a "you love westerns"
shelf made of the four westerns that established the affinity is circular — and
the unwatched check rolls episode watch states up to their series, so a show
the household is partway through is not offered back as something new.

**`BecauseYouWatchedProvider`'s seed cap is the provider's own, and it is not
the diversity constraint.** The diversity rule above bounds how many similarity
rows sit *next to each other*; it does not bound how many exist to be spaced
out, and a provider emitting one row per engaged title proposes up to fifty,
every one scored near the top. Three is the most it may claim before "things
like the things you watched" *is* the home screen. A second, subtler cap goes
with it: two seeds from one franchise produce two rows with largely the same
cards, so a candidate seed whose neighbour set overlaps an already-emitted
row's by more than half is skipped and the next seed promoted.

**And its `reason` changes with the signal that was available**, because the
sentence is written to be spoken. `title_neighbors` is computed with or without
an embedder — the blend drops the absent cosine term rather than zeroing it
(ADR-0014) — so on the shipped default the neighbours are genre and keyword
overlap alone. "Because you watched Dune" is a causal claim about the
household; with nothing semantic computed the row says "Similar genres and
themes to Dune" instead.

**`FranchiseProvider` fires on ≥ 2 owned members *and* ≥ 1 unplayed.** A
franchise the household has finished has nothing to offer: every card is a
rewatch, and the row is indistinguishable from a "you have seen these" shelf
nobody asked for. The row still *lists* every owned member, watched ones
included, because a franchise reads in order and hiding chapters breaks the
sequence — it is the firing condition that requires something left, not the
card list. The unplayed check rolls an episode's watch state up to its series
(`WatchStateRepository.played_title_ids`), which matters even though
collections hold only movies: the alternative is a check whose absence is
indistinguishable from having forgotten it.

**`FranchiseProvider` is movies only, and on a television-only household its
condition is unsatisfiable *by construction* rather than by absence of data.**
`belongs_to_collection` is a field of `/movie/{id}` and TMDb has no `/tv/{id}`
counterpart — verified against the recorded payloads, where `series.json`
carries no such key and nothing plays its role. So `titles.collection_id` is
NULL on every series row, permanently. That distinction is what an operator
debugging a missing row needs, and it is why `CollectionRepository.attach_titles`
filters `kind = 'movie'` itself rather than trusting its caller: the three
available fallbacks — grouping by name prefix, by `networks`, or by Emby's
`TmdbCollection` provider-id key — each produce a populated, plausible, wrong
row. See [02](02-data-model.md)'s `Collection` section.

**Rediscover substitutes for a rating column that does not exist, and the
substitution is on the page rather than in the query.** `watch_states` has no
`rating` and no `favorite`, and `SourceWatchState` carries neither — Emby has
both and the adapter reads neither. M7 does not invent one: landing a real
rating is a source-port change plus a contract case plus a live verification,
against a field no client can set yet. What this schema can express splits in
two:

- **The filter is `played AND last_played_at < cutoff`.** `played` excludes an
  abandonment, which is a rejection rather than a fondness; the cutoff is the
  whole "> 2 years ago".
- **The engagement proxy is the *ordering*, never the filter**: `play_count
  DESC`. A rewatch is a revealed preference and it is the only thing in this
  table a household writes more than once.

`play_count >= 2` as a *filter* is the tempting version and it is wrong: `played
AND play_count = 0` is how "history unknown" is spelled while the history
backfill drains, so that filter returns **nothing** on a freshly-walked
deployment and an arbitrary subset on a half-drained one. As an ordering the
same unreliable column degrades gracefully.

**Rediscover is film-only**, and that is a scope decision rather than an
oversight: a "rediscover" card for a series is an invitation to re-watch sixty
hours, and the example this row is built around is film-shaped. It is *not* the
same call the taste centroid's read makes — there, a title-only query returns an
empty set for a TV household and the centroid is computed from nothing, which is
a correctness failure rather than a narrower row.

**Recently Added is bounded by a window, not by a row count**, which is what
"New items in the window" above already says and what makes its dedup
affordable. Three consequences worth stating, because each is a decision:

- *One row per title.* An episode's `MediaItem` carries its series' `title_id`,
  so a series that landed last night is one row per episode file — 20,000 for
  the measured pathological series, one card. The row reports the **newest**
  contributing file, because a season that just landed on a two-year-old show is
  a new arrival.
- *Episode rows are **not** excluded*, even though three other reads of that
  table bound themselves with `episode_id IS NULL`. A source that reports
  episode files and never a series-level row would otherwise never show a new
  series at all, on the one surface whose whole job is to show it.
- *No user and no source.* Availability is household-wide, so this is the one
  provider whose output is identical for every member of the household — and the
  window cutoff is passed in by the provider rather than spelled inside the
  query, which is what makes it a tunable rather than a migration.

**"Next" is a high-water mark, not a first gap, and three cases this table
left open are settled with it.** M7's `EpisodeRepository.next_up` is the read.

- *The mark is the greatest `(season_number, episode_number)` among played
  episodes, and the next episode is the one after it.* The alternative — the
  earliest unplayed episode following the earliest played one — tells a
  household that skipped S02E05 to watch S02E05 tonight, and tomorrow, and
  every night after, because nothing here or in [07](07-client-api.md) can
  dismiss a card. High-water costs the opposite failure (an episode watched
  out of order *ahead* of the household's position moves the mark and skips
  what lies between), and that one is recoverable by watching the skipped
  episodes where the gap semantic's is not recoverable at all.
- *The mark is a position, never `ORDER BY last_played_at DESC`.* A household
  that finishes season three and rewatches the pilot is not asking for S01E02
  — and `last_played_at` is NULL on nearly every walk-sourced row
  ([ADR-0014](decisions/0014-absence-is-not-zero.md)), which would make a
  recency-keyed mark arbitrary rather than merely wrong.
- *A series with nothing played emits **nothing**.* "Fires when: series with an
  unwatched **next** episode" reads as though it might mean a first episode
  too; it does not. A series never started has a *first* episode, not a next
  one, and at 32,409 series "S01E01 of everything unstarted" is a Next Up row
  holding the household's entire unwatched television library — a generic row
  wearing a personalised row's title, which is exactly the failure the rule
  above names. Never-started series belong to Recently Added and to M9's
  `/browse`.
- *A finished series emits **nothing**, and never wraps to the pilot.*
- *Specials — season 0 — are excluded on both sides.* One watched Christmas
  special must not make this say "continue" about a show nobody has started,
  and a special must never be offered as the next chapter. `(0, n) < (1, 1)`
  is an artefact of how the numbering is spelled rather than a claim about
  viewing order: a special has no defined position in the narrative sequence,
  which is what season 0 *means*. This document said nothing about specials
  before M7.

**"Anything in progress" is `NOT played AND position_seconds > 0`, ordered
`last_played_at DESC NULLS LAST`, and both halves of each are decisions this
table left open.** M7's `WatchStateRepository.list_in_progress` settles them.

- *Both predicates, not one.* Without `played`, a title finished last night is
  the most recent thing the household did and heads the row. Without
  `position_seconds > 0`, the answer is the entire unwatched library — which on
  a full walk is most of it, and which satisfies every `len(cards) > 0`
  assertion anyone will write.
- *A minimum position is the **provider's**, not the query's.* A title
  abandoned at three seconds is in progress by this definition and stays there
  forever, because nothing in this document or [07](07-client-api.md) can
  dismiss a card. The floor is left to `ContinueWatchingProvider` because it is
  a product tunable, because the percentage spelling divides by a nullable
  `runtime_seconds` and so silently empties the row on a source that reports no
  runtimes, and because Postgres uses a partial index whenever the query's
  predicate implies the index's — so a tighter caller is free and a tighter
  index predicate is a migration per adjustment.
- *`NULLS LAST` is correctness, not formatting.* `last_played_at` is nullable
  because a walk's listing frequently cannot determine it
  ([ADR-0014](decisions/0014-absence-is-not-zero.md)), and Postgres orders `DESC`
  as NULLS FIRST — so the obvious spelling leads Continue Watching with
  precisely the rows the system knows *least* about, on a row that is populated,
  plausible and wrong. An undatable state sorts **last** rather than being
  dropped, because dropping it empties the row entirely on a walk-only
  deployment.
- *An in-progress episode is returned as itself, never rolled up to its
  series*, because the card resumes a file. `list_recent` — which feeds the
  taste centroid and `BecauseYouWatchedProvider`'s seeds — rolls up instead, and
  the asymmetry is deliberate: a title-only read of watch history answers
  **films only**, on a library where 999,827 of 1,126,674 measured items are
  episodes.

Adding a row type is a subclass and a registration. Nothing else changes — and
as of M7 that is **a checked claim rather than an aspiration**. ✅ **M8 spent
it, and the claim held**: registering `CuratedProvider` was a subclass, a
tuple entry, a `BASE_SCORES` entry and one new `RowContext` field, and the
registry now holds ten providers, asserted by name *and* by count, the
composition point is one tuple in `services/rows/__init__.py`, and **five
cross-provider invariants are parametrised over that registry**, so the tenth
provider inherited five cases on the day it was written: that it returns
nothing against an empty database,
that it does not fall back to popular titles on a household that has watched
nothing, that it composes with no embedder, that its cases name the wrong
implementation they rule out, and that it reaches no port the context does not
carry. (Two further invariants are asserted over the registry's *scores* rather
than parametrised over its members — that only Continue Watching can reach the
top score, and that every score is on one comparable scale.) The half of the
sentence that was
*not* free is `RowContext`: two of its specified fields had no reader and were
deleted rather than kept, so "nothing else changes" holds for the composer and
did not hold for the context.

## Taste

The **taste centroid** is the mean embedding of recently watched, ⏳ **highly
*engaged*** titles, computed per user and cached. It is cheap, local, and
reused for:

- seeding similarity rows,
- ranking search results,
- ~~selecting genre affinity rows~~ — ⏳ **not this; see below**,
- pre-filtering the LLM candidate pool.

Recency-weighted so it tracks changing taste rather than averaging a lifetime.

⏳ **"Highly rated" is a substitution, and this is the third of three sites
where a rating this schema does not have was assumed.** The other two are
`RowCard` (no `rating` field) and `RediscoverProvider` (above). `watch_states`
carries no `rating`, no `favorite` and no `user_score`; `SourceWatchState`
carries none either, and the Emby adapter reads neither of the two fields Emby
does expose. M7 does not invent the column — landing a real rating is a
source-port change plus a contract case plus a live verification against a
field no client can set yet.

**So "highly rated" becomes "finished, and finished twice is better."** Two
tiers over the engagement signal this schema actually holds: a title the
household **rewatched** (`play_count >= 2`) weighs 1.00, one it merely
**finished** weighs 0.60. A rewatch is the only *loved* signal here and it
costs the household the entire runtime to emit — revealed preference, paid for
in hours, and a stronger endorsement than any five-star widget.

**Abandonment is expressed by absence, never by a negative weight.** A title
started and dropped twelve minutes in is not evidence of dislike strong enough
to point a vector away from it; it is evidence of nothing much, and the
household has no way to say otherwise. A signal whose sign is a guess is worse
than one that is absent ([ADR-0014](decisions/0014-absence-is-not-zero.md)).

**Recency is a 50-title rank window with a linear ramp to a 0.25 floor, not a
half-life.** A 30-day half-life gives a two-year-old watch a weight of 6e-8,
which is numerically indistinguishable from exclusion — so the half-life *is* a
window, with an edge nobody wrote down and nobody can see. A window states its
edge, and ranking by recency rather than by wall-clock normalises by the
household's **own viewing pace**, which is the variable no per-deployment
measurement exists for.

⏳ **A household below five engaged titles gets no centroid, and the refusal is
written rather than skipped.** A centroid over one title *is* that title's
vector. The stored row carries a NULL centroid so that household is re-claimed
exactly once when its history moves, rather than recomputed on every read
forever — `title_embeddings`' argument for a nullable vector, on a different
key.

⏳ **With no embedder there is no centroid at all — `None`, never a zero
vector.** The embedder is optional and off by default, so this is the shipped
configuration rather than an edge case. Every consumer drops the signal rather
than zeroing it: a deployment without an embedder gets a home screen with
**fewer rows, not worse rows**.

### Genre affinity is not computed from the centroid

⏳ **This corrects `GenreAffinityProvider`'s firing condition in the table
above.** Read literally — *"taste centroid concentrated in a genre"* — the most
broadly-useful provider becomes the one that **never fires** on the default
deployment, because the centroid needs an embedder and the embedder is
optional. Worse, it fails in the direction hardest to notice: the home screen
still renders, the other providers still fire, and the row that would have said
something true about the household is simply absent, forever, with nothing
counting its absence.

Genre affinity is therefore **counts over `titles.genres`**, and it fires when
*the household watches a genre disproportionately to its share of their
library*:

```
share_watched(g) = weighted engaged titles carrying g / weighted engaged titles
share_library(g) = owned titles carrying g           / owned titles
lift(g)          = share_watched(g) / share_library(g)
```

**The baseline is the owned library, and the two alternatives are both wrong.**
The household's own distribution makes every lift exactly 1.0 by construction,
so the provider would never fire on any household. The whole 1.27M-row catalog
tells a household that owns nothing but horror that it loves horror — but the
library made that choice and the person emitted no information, so the row's
reason string would be word-for-word false. The owned library is the
household's actual **choice set**, which makes affinity *lift over
opportunity*.

Fires at `lift >= 1.5` with at least 4 supporting titles. The support floor is
what kills a genre watched once, whose lift in a thin library is in the tens. A
genre the library does not carry at all yields no lift rather than an infinite
one.

It reads the **same** engaged window the centroid does, so the recency
weighting is shared and there is one definition of what this household watches.
Two windows would be two definitions, and on the day they disagreed the
disagreement would be invisible.

## LLM curation

**No collaborative filtering.** At household scale there is no co-occurrence
signal — every recommendation is a permanent cold start. Usher is content-based,
plus borrowed aggregate signals (TMDb similar/recommended) where useful.

Generation runs nightly and on demand:

1. **Assemble context** — recent watch history with ⏳ ~~ratings~~
   **engagement**, plus a candidate pool of ~200 unwatched titles pre-filtered
   by ⏳ popularity and genre affinity, **re-ranked** by taste-centroid
   proximity where a centroid exists. The pool spans the whole catalog, not
   just the library, so suggestions can include things to seek out.

   ✅ **Settled 2026-08-11: this sentence won a two-year-old disagreement with
   the prompt, and the prompt was corrected to match it.** `build_prompt`
   opened *"one household's **own** film and television library"*, a claim only
   an ownership filter could honour. Measured through the real Postgres
   repository: a household owning 20 unwatched titles gets a pool of 200 that
   is **10.0%** owned, one owning none gets a pool that is **0%** owned — and
   the filter would have added nothing, because `owned DESC` is the first sort
   key so the pool already contains **every** unwatched-owned title the
   household has. Its only effect is to delete the tail, and below `min_cards`
   to delete the generation. Evidence, the arm not taken and the ownership
   marker priced and declined are in
   [ADR-0028](decisions/0028-the-pool-is-the-contract.md)'s 2026-08-11
   amendment.

   ⏳ **"with ratings" is the fourth site in this document where a rating this
   schema does not have was assumed**, after `RowCard`, `RediscoverProvider`
   and the centroid — and the substitution the Taste section already writes
   down applies here unchanged: rewatched (`play_count >= 2`) weighs 1.00,
   merely finished weighs 0.60.

   ✅ **And the centroid cannot be the pre-filter's spine, because on the
   shipped configuration there is no centroid.** `USHER_EMBEDDING_ENABLED`
   defaults to `False`, so implementing that clause literally makes curation
   the feature that never fires on a default deployment — **which is exactly
   the failure this document already corrected once**, for
   `GenreAffinityProvider`, and in the same direction: *"it fails in the
   direction hardest to notice."* The pool is therefore built from signals
   that need no model, and the centroid **re-orders** it when one is
   available — which is also what finally gives `TasteService.centroid` a
   caller in `src/`, a gap M7 shipped and named.

   ✅ **Built in M8 as `CandidatePoolService` over
   `TitleRepository.list_unwatched_candidates`, and three details of the
   sentence above are sharper than it is:**

   - **Membership is "unwatched", full stop** — `played`, rolled up through
     `episodes.title_id` so a watched episode takes its series with it, and
     expressed *inside* the statement rather than subtracted after a `LIMIT`.
     Ownership and popularity are **ranking keys**, which is what keeps
     *"the pool spans the whole catalog"* true — and which M9 Task G3
     re-confirmed on 2026-08-11 rather than reversing, with the measurement
     that `owned DESC` being the *first* key makes the owned titles a prefix,
     so no ownership filter could ever add a candidate this read does not
     already return. The order is `owned DESC,
     carries an affinity genre DESC, vote_count DESC NULLS LAST, id` — and
     the `id` tail decides **membership** rather than only order, because the
     `LIMIT` falls inside a tie: losing it makes two reads of one unchanged
     household return different *sets*, so
     [ADR-0028](decisions/0028-the-pool-is-the-contract.md)'s integer handles
     stop naming the same films. The measurement behind that — how much of a
     real catalog carries no `vote_count` at all — is stated once, on
     `TitleRepository.list_unwatched_candidates`, and deliberately not
     repeated here.
   - **`vote_count`, not `popularity`.** `titles.popularity` was measured NULL
     on all 1,271,138 rows of a bootstrap-only catalog and is
     `NOT NULL DEFAULT 0` in `tmdb_ids`, so leading with it lets a
     crosswalk-linked skeleton at `0.0` outrank an unlinked title with half a
     million votes — bounded where `list_owned_by_tag` uses it (owned titles
     only) and unbounded over the whole catalog.
   - **The re-rank permutes the embedded members among the positions they
     already occupy**, so a candidate the centroid cannot speak about — no
     vector, a NULL one, or one of another model's width — keeps its exact
     index. That is stronger than "unembedded candidates are not dropped" and
     is chosen for the reason M7 quoted the genome's *candidate-pair* rate
     (1.81%, over 5,020 owned seeds — the population is part of that number)
     rather than its coverage: an artefact whose shape depends on how
     far `usher index --backfill` has drained is one that changes for reasons
     the household cannot see. The pool is a function of the household, not
     of the embedder.
2. **One structured call** to any OpenAI-compatible endpoint →
   `[{title, reason, item_ids ⊆ pool}]`, 3–5 rows. (This read *"via litellm"*
   until M8 priced that dependency at +146 MB and 29 distributions against a
   `POST` —
   [ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md).) **`item_ids`
   are indices into the pool, never UUIDs** — measured, a UUID handle costs
   3.1× the prompt tokens and is the *least* accurate of three spellings, and
   an index is the only one that is bounds-checked
   ([ADR-0028](decisions/0028-the-pool-is-the-contract.md)).
3. **Validate** — IDs not in the pool are dropped; rows below a minimum length
   are discarded **whole rather than padded**, because a padded row is a
   fabricated recommendation wearing a model's reason string. Hallucinated
   identifiers never reach a client.

   ⚠️ **This step is where the milestone's one live defect was found, and the
   sentence above does not describe it.** The obvious spelling —
   `id in set_of_pool_ids` — dropped **108 of 108** identifiers against a
   provider that returned them as JSON *integers* where the schema asked for
   strings; coerced, the same run dropped **0**. Not one id was invented. What
   that ships as is a generation that called the model, wrote an `llm_calls`
   row reading `ok = true` with real tokens and a real cost, and left the
   household with no curated rows — indistinguishable from a model that had
   nothing to say, because the degradation table below reads *"previous
   curated rows persist"*. So: the validator **coerces before it compares**,
   `usher.curation.dropped` carries a `reason` label distinguishing
   `not_in_pool` from `unparseable`, and **a generation that validates to zero
   rows is a failure rather than an empty success**.
   [ADR-0028](decisions/0028-the-pool-is-the-contract.md).

   ✅ **Built in M8 as `usher.services.curation_validate`, a module of pure
   functions over a parsed `dict` and the generation's own index → UUID map.**
   Four things about it are sharper than the paragraph above:

   - **The map is a `Mapping[int, UUID]`, and the validator does no arithmetic
     on it.** Which handles were sent is a fact the caller owns, so a sparse
     pool and a 1-based prompt (what ADR-0028 measured) need no special case,
     and `pool[-1]` — legal Python, and a real film — is unreachable.
   - **Coercion is `str(value).strip()` for `int` and `str` only.** A `bool` is
     refused before the `int` branch (`isinstance(True, int)` is `True`); a
     `float` is refused rather than rounded, because `int(11.5)` is also 11 and
     a rule that accepts `11.0` must invent an answer for the other. Prose is
     never coerced at all: a non-string `title` is a dropped row, not `str(11)`
     on a television.
   - **The reason label is five, not two** — `duplicate`, `row_unusable` and
     `row_too_short` join the original pair, each because it names a different
     *diagnosis*, not because it names a different fix: two of them share a
     lever with a member of the pair, and the load-bearing half of the
     widening is that two of the five count **rows** and three count **cards**.
     ADR-0028 carries the amendment and the argument.
   - **Zero rows is unrepresentable as a success**, not merely checked for: the
     return type is a union whose success arm cannot be built with an empty
     `rows` and whose failure arm has no `rows` attribute at all.

   ⚠️ **The validator does not cap the number of rows**, deliberately: every
   card in a hundredth row is still a title the household could watch, so a cap
   is a product bound rather than a safety one and belongs with
   `CuratedProvider`'s `0–5 rows` budget. What it does own is the *ordering* —
   `curated_rows.slug` is zero-padded to the width of the generation, because
   the composer breaks score ties on `slug` and `curated-10` sorts before
   `curated-2`.
4. **Persist** as `curated_rows`.

✅ **All four steps are `usher.services.curation.CurationService` in M8, and
five things about the assembled whole are sharper than the list above.**

- **The prompt is code, and the two numbers in it that have to agree with
  something else are rendered rather than written.** The pool's length is the
  bound the validator checks, and `min_cards` is the floor it enforces — a
  prompt asking for four cards under a validator demanding five drops every row
  and reports `row_too_short`, a generation that failed because two numbers in
  two files disagreed. Everything else in the prompt (the text, the 3–5 row
  budget, the heading width) is a constant for
  [08](08-operations.md)'s row-weights-are-code reason.
- **Step 1's other half is a real read.** The prompt carries this household's
  last 25 finished titles, most recent first, with a rewatch marked — the
  engagement substitution this section already makes for the rating column the
  schema does not have. A pool with no history behind it produces shelves about
  the catalog rather than about the household.
- **The `json_schema` sent with the request is an optimisation and never the
  contract.** It states the handle bound a second time, where a provider that
  honours guided decoding makes an out-of-pool handle harder to emit; the
  validator checks it whatever the provider did.
- **Failure is non-fatal to the screen and fatal to the job.** A failed
  generation never reaches `replace_for_user`, so *"previous curated rows
  persist"* below is a property of the control flow rather than of a
  transaction — and the exception propagates, because `JobWorker` learns "park"
  from `PortDataMalformed` and "back off" from everything else by catching it.
  A generation that validated to **nothing** raises `PortDataMalformed`: the
  three things that produce it are permanent properties of that request, so
  five more completions reach the same answer at five times the price.
- **`llm_calls` gets a row on every path that *attempted* a completion**,
  `ok = false` included — a call that never got an answer is still a row, with
  zeroed tokens and the model this deployment asked for. The one path that
  writes none is the one that attempted nothing: an empty candidate pool raises
  before the client is touched, and an empty catalog is an operator's problem
  rather than an event of the LLM subsystem.

Failure is non-fatal: previous rows stay until successfully replaced. Cost is
one modest completion per user per day.

The candidate pool being pre-filtered locally is what keeps this affordable —
the model sees 200 titles it might plausibly recommend, not a catalog.

### 🔴 What the live run found, and the limits it leaves

Measured 2026-08-07 against a local vLLM serving **`gemma-4-26b-a4b`** over a
real **1,271,138**-title catalog, bounded at 45 completions and spending 36.
⚠️ **One model, one pool, one evening.** Every rate below is scoped to that and
none is a property of "an LLM"; what transfers is the *ordering* of options and
the *shapes* of failures, never the percentages. The machinery is recorded in
[ADR-0028](decisions/0028-the-pool-is-the-contract.md); what follows is the
half that is about the **product**, and it is here rather than in a task queue
because a reader of this section is the person who needs it.

🔴 **The central product risk: on this model the curated shelf is
substantively what `GenreAffinityProvider` already gives away free.** Over 59
headings from 20 generations:

- **52 of 59 — 88% — are genre labels**, which the prompt *explicitly forbids*:
  *"Group by something a person would recognise — a mood, a period, a theme, a
  filmmaker — rather than by one genre."*
- **One heading in 59 named a filmmaker**, which is the behaviour the
  instruction was written to buy.
- *"Animated Wonders for All Ages"*, *"Epic Sci-Fi Adventures"* and
  *"Mind-Bending Sci-Fi & Thrillers"* each recur **verbatim across three
  separate generations**.

`GenreAffinityProvider` produces a genre shelf from a `SELECT`, for nothing, in
milliseconds, and needs no key. So the question this section cannot currently
answer is what the completion is *for* — and the honest statement of it is that
**the prompt's grouping instruction is not self-enforcing and nothing in this
system checks it.** That is a property of the design; the 88% is a property of
one model. A frontier model may well obey it, and the way to find out is to
run this measurement again against one rather than to assume. Not fixed here:
curated rows are additive, [08](08-operations.md)'s *"Home composes without
them"* holds, and a duplicated genre shelf is a disappointment rather than a
defect.

**Four limits the run named, recorded rather than fixed — the first and the
third have since been settled, both on 2026-08-11, and each bullet says how:**

- ✅ **The pool had no ownership *filter* and the prompt said it did — settled
  2026-08-11 by correcting the prompt.** `TitleRepository.list_unwatched_candidates`
  uses ownership as an `ORDER BY` key only — deliberately, so *"the pool spans
  the whole catalog, not just the library"* above stays true — while
  `curation_prompt.build_prompt` opened *"one household's **own** film and
  television library."* On a household whose unwatched-and-owned set is smaller
  than the pool size, the tail of the pool was titles it does not own, under a
  sentence asserting it does. Both sentences were defensible and the fork was
  filed as a product decision rather than a defect; **M9 Task G3 measured it
  and the pool won.** A pool-composition sweep through the real Postgres
  repository (1,000-title catalog, `limit = 200`) found the owned fraction
  running 0.0% → 1.5% → 2.5% → 4.0% → 10.0% → 100.0% as the household's
  unwatched-owned set grows 0 → 3 → 5 → 8 → 20 → 200 — **and found that the
  filter could add nothing**, because `owned DESC` is the first sort key so the
  pool already carries every unwatched-owned title there is. Filtering is
  purely subtractive, and at 3 owned titles it leaves a pool that cannot fill
  one row. The prompt now says some candidates are in the library and some are
  not, for **+26 prompt tokens once**; a per-candidate ownership marker was
  priced at **2.9–4.9 tokens a candidate** against a bar of 2.0 declared before
  the measurement, and **is not rendered** — `RowCard.owned` and
  [05](05-search-and-similarity.md)'s *"clearly marked"* are the client's half.
  Full evidence, the arm not taken, and what would reverse the call are in
  [ADR-0028](decisions/0028-the-pool-is-the-contract.md)'s 2026-08-11
  amendment.
- ⚠️ **De-duplication is within a row only.** `curation_validate._cards`
  collapses a repeat inside one row and counts it `duplicate`; a title
  appearing on *two* shelves of the same generation is not counted at all. The
  prompt's *"Do not use the same candidate in more than one row"* is the only
  defence, and a prompt rule is not a guarantee — the same thing this section
  says one level up about the grouping instruction.
- ✅ **`min_cards = 5` meant a small unwatched pool yielded zero rows, every
  time, at full price — settled 2026-08-11 by refusing before the spend.**
  Rows carried 5–6 cards at pool 200 and **2–3 at pool 5 and pool 8**, so every
  row was discarded as `row_too_short` and the generation was billed and
  produced nothing. That is
  [ADR-0014](decisions/0014-absence-is-not-zero.md) working — a padded row
  would be a fabricated recommendation — and it was also a household paying a
  completion a night for a permanently empty shelf, with nothing warning the
  operator before the money. **M9 Task G4 widened the guard
  `CurationService.generate` already carried for an empty pool**, from
  `len(pool) == 0` to `len(pool) < min_cards`. That a pool below the floor
  cannot produce one surviving row is arithmetic rather than a judgement —
  `_row` discards a row of fewer than `min_cards` *distinct* cards and `_cards`
  de-duplicates by title id — so the refusal sits in front of `complete_json`,
  writes no `llm_calls` row, and gives the operator a sentence naming how many
  candidates were found and what the floor is. `PortDataMalformed` parks the
  job, exactly as the empty pool has always done, and no setting was added:
  `min_cards` crosses the prompt, the schema and the validator from one
  definition. ⚠️ **Priced honestly, this is rare rather than nightly.** The pool
  is `min(catalog_unwatched, USHER_CURATION_POOL_SIZE)` and ownership is a sort
  key rather than a filter (the first bullet above), so only a catalog whose
  *whole* unwatched set is below five ever reaches the guard. Had the ownership
  filter shipped instead, the same guard would have fired for ordinary small
  libraries — and a park, which blocks every later enqueue for that household
  until a human releases it, would have been the wrong disposition for a
  condition the next sync fixes.
- ⚠️ **Four of the five `DropReason` members never fired in 20 generations**,
  and under a provider honouring `strict: true` three of them are close to
  unreachable: `unparseable` and `row_unusable` are shape failures guided
  decoding prevents, and `not_in_pool` is a range violation it also prevents.
  Only `row_too_short` fired. Worth knowing **before** an operator reads a
  dashboard of permanent zeros and concludes the counter is broken — the
  vocabulary is still right for the reason ADR-0028 gives (a reason absent from
  a tally is indistinguishable from a reason nobody counts), and its zeros are
  now expected rather than surprising.

**What the run did not reach, named rather than implied.** `media_items` was 0,
so ownership sorting and the other nine providers were never exercised against
real data; `title_embeddings` was 0, so `CandidatePoolService._reranked`'s
centroid re-rank **never executed**; end-to-end retrieval through
`PostgresSearchIndex`, `JobKind.CURATE` via `usher work`, and
`POST /admin/rows/regenerate` were all untested (only the `usher curate` path
ran); and no hosted provider was touched at all.

## Caching

| Layer | Lifetime |
|---|---|
| Built rows | ✅ Per-row TTL, in-process — 60 s (Continue Watching, Next Up) to 12 h (Seasonal), each row's own. **No stale-serve grace**, and see below for why |
| Composed home screen | ✅ 30 s per user, in-process, **plus a 60 s stale-serve grace — M9** (`SCREEN_STALE_GRACE`). Between 30 s and 90 s the cached screen is still served and a refresh is scheduled; past 90 s it is a hard miss and the request rebuilds |
| **Neighbour tables** | ⚠️ **Not rebuilt on anything.** This row was false in M6 and is false now; what changed is that half of it is finally *observable* — see below |
| Curated rows | ✅ **M8** — 5 min per built row, in-process, on `CuratedProvider`'s rows out of `curated_rows`. Not "until regenerated": the artefact is immutable until a generation replaces it, and this number is how long a household keeps seeing last night's shelf after tonight's replaced it, because the job runs in another process |
| Taste centroid | ⏳ **Recomputed when the household's `max(watch_states.updated_at)` moves** — a fingerprint, not an event |
| Genre affinity | ✅ **Nothing stored, and memoised for the life of one request** — no artefact, no fingerprint, no invalidation to get wrong; the memo is on the request-scoped `TasteService` and dies with it |

Rows are recomputed lazily and served stale while refreshing, so the home screen
never blocks on a slow row.

> **Built in M7 as `services/rows/cache.py`, with one sentence above
> corrected.** Both caches are **in-process** — a dict in the server, per
> worker, emptied by a restart. The deployment this project ships runs one
> (`compose.yml`, one uvicorn worker), so today there is exactly one cache;
> with two replicas the screens stay within their TTL of each other but
> **invalidation does not cross processes**, which is the same change as the
> cross-process `EventPublisher` and is named with it rather than built.
>
> **"Served stale while refreshing" is built in M9, in neither of the two
> shapes M7 named as wrong.** Not one task per stale key — unbounded, and in no
> concurrency table — and not `api/lanes.py`'s per-source granularity, which is
> bounded on the wrong axis. It is **one `rows.refresh` lane draining one
> bounded deduplicating queue of stale keys, each refresh on a session of its
> own through `composition.unit_of_work`, drop-on-full**. M7's reason for
> deferring stands and is what shapes it: the request's session is committed
> and closed by `get_session` when the handler returns, and sharing it with a
> task is the `AsyncSession` concurrency hazard
> [ADR-0025](decisions/0025-rows-build-sequentially.md) refuses one layer up,
> with the same "usually works" signature. The queue therefore carries a frozen
> `User` and nothing request-scoped. Its bound is `REFRESH_QUEUE_SIZE` = 32 keys
> with one consumer, quoted in [01](01-architecture.md)'s concurrency table.
>
> **Three properties, each of which is a different way to get this wrong.**
> *The screen never waits on it* — the handover is a **synchronous** callable,
> so there is nothing for a request to await and the spelling that breaks it
> does not type-check. *The refresh is bounded* — full means **dropped**, never
> blocked, which is safe because an entry past `TTL + grace` is a hard miss and
> the next request rebuilds, i.e. exactly what M7 already paid on every expiry.
> *How stale is too stale* is `SCREEN_STALE_GRACE`, 60 s, in the table above.
>
> **The grace window is gated on there being a refresher.** A composer handed
> none — `usher home`, whose process ends when the command does — serves nothing
> stale at all, because a stale screen with nothing behind it to replace it is
> strictly worse than the miss it avoided and is silent.
>
> **A stale serve counts as a `usher.cache.hits` point carrying
> `freshness="stale"`.** A hit because the request paid no rebuild; labelled
> because a plain hit hides the one thing the feature trades away. See
> [10](10-telemetry-and-dashboards.md).
>
> **Row TTLs are unaffected, and the consequence is worth stating.** A screen
> refresh re-proposes, re-selects and re-orders while *reusing* every row whose
> own TTL is still running, so a screen seconds old can carry a five-minute-old
> `recently-added` shelf. That is the second layer doing its job — rebuilding
> every row on every 30 s screen expiry is the cost it exists to avoid — and it
> is why the row half has no grace of its own: the refresh unit is a screen, and
> a per-row grace with no per-row refresh behind it would serve stale rows that
> nothing ever replaces.
>
> **Invalidation is driven by the push lane and by demand reads, never by the
> nightly walk** — the same scale argument [07](07-client-api.md) makes for
> `watchstate.updated`. A walk merges up to 1,126,789 states; one invalidation
> per merged row is a fan-out per row per night. A walk that finishes at 04:00
> is on the screen by 04:00:30, through the 30 s screen TTL.
> `WatchStateSyncService` is handed no cache at all, so adding that call means
> adding a constructor argument.
>
> **Cache keys carry the user**, taken from the request's own `current_user`,
> so replacing that one dependency remains the whole of adding authentication —
> and a key that omitted it would work today and serve one household's screen
> to another the day auth lands, with no error, no log line and no metric.
>
> **The row half is bounded and evicts soonest-to-expire first**, because
> `because-you-watched-<seed>` is one slug per seed and expired entries are read
> past rather than removed: without a ceiling the TTL reclaims nothing and the
> dict grows with the household's watch history.

⚠️ **"Neighbour tables — rebuilt on embedding change" describes a trigger that
has never existed**, and it is worth correcting loudly rather than quietly,
because it is the one row in this table an operator could act on wrongly.
Nothing rebuilds `title_neighbors` on any event: `usher similar --rebuild` is
an operator's command or a cron entry, and M6 recorded this as *"the
milestone's one honest freshness gap"*.

**M7 narrows it to one half and makes the other half countable.** Staleness has
two causes, and they are not the same kind of problem:

- *The blend changed.* M7's fourth signal re-weighted the other three, so every
  row written before it means something different from every row written after
  — and until `title_neighbors.blend_fingerprint` (migration `ffb`) nothing
  distinguished them. That half is now **a query**: `usher similar <title id>`
  says so per title and `usher.similarity.neighbors.stale` counts the table.
- *Some other title was embedded.* A title's neighbours go stale when a
  *different* title gets a vector, which no per-row predicate can decide
  without recomputing the row. That half remains undecidable, `computed_at`
  remains beside the fingerprint for it, and it stays an operator's job.

So the honest statement is: **one cause is now visible and neither is
automatic.** [ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md).

⏳ **"Invalidated on watch-state change" was an event this project has already
refused to publish.** The nightly walk merges up to **1,126,789** watch states,
so one invalidation per merged row is a million messages a night for at most
one useful recomputation per user — the exact fan-out
[07](07-client-api.md) declines for `watchstate.updated`.

So `user_taste` stores the `max(watch_states.updated_at)` its centroid was
computed from, and a demand read recomputes when the household's current max
**`IS DISTINCT FROM`** it —
[ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md)'s scheme,
per user rather than per title. `IS DISTINCT FROM` rather than `<`, and only
the first of its three reasons is obvious: a *newer* state raises the max; a
*deleted* state **lowers** it, and `<` would serve a centroid computed over a
row that no longer exists forever; and a *cleared* history makes the aggregate
`NULL`, where `stored < NULL` is `NULL` and therefore never true. The merge
path publishes nothing and does not know `user_taste` exists.

⏳ **Genre affinity is not *stored*, and sharing the centroid's row would be
wrong rather than merely wasteful.** That row is invalidated on `model_name IS
DISTINCT FROM`; genre affinity has no model. Sharing it would make an
embedding-checkpoint swap invalidate a count no model touched, and — the worse
half — would require a deployment with **no embedder** to write a `model_name`
for a model it does not have. There is no honest value for that column. A
*separate* stored cache would cost more than the answer: the affinity is a
count over ≤ 50 `text[]` values plus one library-wide aggregate, and the
validity check guarding it would itself be the `max(updated_at)` read.

✅ **What it does have is two memos inside `TasteService`, both dying with the
service** — one request on the route, one unit of work in the CLI and the
worker. Neither is an artefact and neither needs a fingerprint, which is the
whole difference from the paragraph above:

- **the engaged window** (`WatchStateRepository.list_recent(50)`), which both
  public methods open with, so one `CandidatePoolService.for_user` on a
  deployment with an embedder read the household's history twice per
  generation. Keyed by `user_id` — a memo on a per-user read is the one
  optimisation whose failure mode is a data leak — and re-read whenever a
  caller presents a `max(watch_states.updated_at)` that disagrees with the one
  the memo was filled at, which is free because `centroid` reads that
  watermark anyway;
- **the library-wide genre counts** (`unnest(genres) GROUP BY` over 1.27M
  titles), which take no `user_id` at all, are the denominator of every lift,
  and were paid once per generation *and* once per home-screen build for a
  number that changes only when the library does.

## Alfred

Row providers are the natural surface for the voice assistant. Alfred asking
"what should I watch tonight?" resolves to a composed row set with reasons
attached — the `reason` field is already written to be spoken aloud, not just
displayed. Alfred can also register its own provider later.
