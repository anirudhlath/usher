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
**abstraction**: `hydrate()` needs a `TitleRepository`, a
`MediaItemRepository` and a `WatchStateRepository`, and a concrete method on a
port is a port with a dependency — `ports/` has none today. The mechanical
half is that `test_every_port_abc_is_registered_in_all_ports` walks
`usher.ports.*` and is the only thing that checks an ABC is an ABC, so a `Row`
in `services/` would get neither of ADR-0001's two checks. The DTOs stay pure
either way.

`build` returns a `BuiltRow`, never `BuiltRow | None`: an empty row and an
absent row are different states, and the composer's metrics count them
separately.

`RowContext` is a frozen dataclass of ports plus an injected clock — **eleven
fields as shipped in M7**:

```python
user: User                          now: Callable[[], AwareDatetime]
titles: TitleRepository             media_items: MediaItemRepository
watch_states: WatchStateRepository  episodes: EpisodeRepository
neighbors: TitleNeighborRepository  people: PersonRepository
credits: CreditRepository           collections: CollectionRepository
affinities: Sequence[GenreAffinity]
```

- **No `AsyncSession`, and that is checked rather than commented.**
  `AsyncSession` is not safe for concurrent use, so a context carrying one is
  a context nine providers can `asyncio.gather` over — which *usually works*,
  and fails as an intermittent error under load. A row holding repositories
  has no session to share.
- **The clock is injected** because `SeasonalProvider` fires on a calendar
  window and `RediscoverProvider` on "watched > 2 years ago". A wall-clock
  read makes the first testable only in October; a fixture dated two years
  back stops meaning what it meant as the calendar moves. It is on the
  context rather than each provider's constructor because providers are
  registered once, and a per-request clock cannot be a singleton's
  constructor argument.
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
| **`LLMRow`** | ⏳ **not built in M7 — M8 owns it** | A persisted `curated_rows` record | "Slow-burn sci-fi for a rainy night" | until regenerated |

`LLMRow.build()` only *hydrates* stored output. Generation happens in a
background job — never in the request path.

**`RowFamily` has two members in M7 and no `CURATED`.** M8 owns
`curated_rows`, `LLMRow`, `CuratedProvider` and
`POST /admin/rows/regenerate` as one family — hydrating a table whose
generator does not exist would fix that table's shape before anything had
tried to fill it. A "cap per family" over a family with no members is a branch
nothing can reach, so the member arrives in the same diff as the provider that
emits it.

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
> on one connection — so `asyncio.gather` over nine providers sharing a
> request's session is a corruption, and one that *usually works*: two short
> reads frequently complete, and the failure is an intermittent
> `InvalidRequestError` or a result set attributed to the wrong query, under
> load. The two escapes are worse at this scale — a session per row is nine
> connections for one home screen, and a semaphore has no lane to belong to.
> Every provider's query is a bounded local read;
> `usher.home.compose.duration` and the per-provider `usher.row.build.duration`
> breakdown are what turn revisiting this into a number rather than an
> argument, and `usher home` measures it.
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
> reader *and* a reason. **With two families the longest screen reachable
> today is nine rows** — one pinned plus four per family — and `_MAX_ROWS`
> becomes reachable when M8 registers `CuratedProvider` and `RowFamily` grows
> its third member.
>
> Each provider declares a `slug_prefix` (`continue-watching`,
> `because-you-watched`), and every row it proposes mints its slug from that
> constant. It is what `usher.row.build.duration`'s `provider` label and
> `usher home`'s report both carry: bounded at nine, where a row slug is
> bounded by the catalog.

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
| `CuratedProvider` | ⏳ **not built in M7 — M8 owns it**, with `curated_rows`/`LLMRow`/`POST /admin/rows/regenerate`; see below | 0–5 rows |
| `RediscoverProvider` | Watched > 2 years ago, **most-rewatched first** — there is no rating column; see below | 0–1 rows |

**Nine of these ten are registered as of M7; `CuratedProvider` is the tenth
and M8 owns it whole** (boundary call 2, and the table above is annotated
rather than silently shipped short). The registry is
`services/rows/__init__.py`'s `ROW_PROVIDERS`, and it is the composition
point: **a provider that is not registered is dead code, and dead code that
looks exactly like a provider with nothing to say** — which is the one failure
a composed home screen cannot show from the outside. It holds nine, asserted by
name rather than by count, and four cross-provider invariants are parametrised
over it, so a tenth provider is covered by four cases the day it is written:
that only Continue Watching reaches the top score, that every provider returns
nothing against an empty database, that none falls back to popular titles on a
household that has watched nothing, and that every one composes with no
embedder.

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

Adding a row type is a subclass and a registration. Nothing else changes.

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

1. **Assemble context** — recent watch history with ratings, plus a candidate
   pool of ~200 unwatched titles pre-filtered by taste-centroid proximity and
   popularity. The pool spans the whole catalog, not just the library, so
   suggestions can include things to seek out.
2. **One structured call** via litellm →
   `[{title, reason, item_ids ⊆ pool}]`, 3–5 rows.
3. **Validate** — IDs not in the pool are dropped; rows below a minimum length
   are discarded. Hallucinated identifiers never reach a client.
4. **Persist** as `curated_rows`.

Failure is non-fatal: previous rows stay until successfully replaced. Cost is
one modest completion per user per day.

The candidate pool being pre-filtered locally is what keeps this affordable —
the model sees 200 titles it might plausibly recommend, not a catalog.

## Caching

| Layer | Lifetime |
|---|---|
| Built rows | Per-row TTL, in-process |
| Composed home screen | ~30 s per user |
| Neighbour tables | Rebuilt on embedding change |
| Curated rows | Until regenerated |
| Taste centroid | ⏳ **Recomputed when the household's `max(watch_states.updated_at)` moves** — a fingerprint, not an event |
| Genre affinity | ⏳ **Not cached at all** |

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
> **"Served stale while refreshing" is not implemented in M7 and is M9's.** A
> background refresh needs a session it did not get from a request: the
> request's own is committed and closed by `get_session`, and sharing it with a
> task is the `AsyncSession` concurrency hazard [09](09-roadmap.md)'s boundary
> call 8 refuses one layer up — with the same "usually works" signature. Its
> own session is a connection outside the request pool's accounting, per stale
> key, on demand; and `api/lanes.py`'s supervisor is one lane per *source*,
> enumerable and bounded, where this would be one task per stale key. The
> payoff is one request per TTL per user, because every other request in the
> window is already a hit. It lands with `usher.cache.hits`/`.misses`, because
> a refresh path with no hit/miss metric is a mechanism nobody can see working.
> **M7 caches and expires.**
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

⏳ **Genre affinity is not cached, and sharing the centroid's row would be
wrong rather than merely wasteful.** That row is invalidated on `model_name IS
DISTINCT FROM`; genre affinity has no model. Sharing it would make an
embedding-checkpoint swap invalidate a count no model touched, and — the worse
half — would require a deployment with **no embedder** to write a `model_name`
for a model it does not have. There is no honest value for that column. A
*separate* cache would cost more than the answer: the affinity is a count over
≤ 50 `text[]` values plus one library-wide aggregate, and the validity check
guarding it would itself be the `max(updated_at)` read.

## Alfred

Row providers are the natural surface for the voice assistant. Alfred asking
"what should I watch tonight?" resolves to a composed row set with reasons
attached — the `reason` field is already written to be spoken aloud, not just
displayed. Alfred can also register its own provider later.
