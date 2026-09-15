# 06 — Rows and recommendations

The home screen is composed, not configured. Rows are proposed by providers,
scored for relevance, and assembled per request — so the screen changes with
context, season and taste rather than being a fixed list.

## The Row hierarchy

```python
# usher/ports/rows.py
class Row(ABC):
    """A named, ordered shelf of titles, able to build itself."""

    slug: str                    # "continue-watching", "because-you-watched-dune"
    title: str                   # display name
    reason: str | None           # subtitle, written to be spoken aloud
    family: RowFamily            # the diversity key
    display_hint: DisplayHint    # a hint, never a layout
    ttl: timedelta               # how long a built result may be cached

    @abstractmethod
    async def build(self, ctx: RowContext) -> BuiltRow: ...

# usher/services/rows/base.py
class BaseRow(Row):
    async def hydrate(self, title_ids: Sequence[UUID]) -> list[RowCard]: ...
    def empty(self) -> BuiltRow: ...
```

The abstract members are **properties**, not bare annotations: a bare
annotation is a class-variable declaration, so a subclass that forgot one
inherits `None` and fails at render time rather than at instantiation.

**The ABC is a port and the shared behaviour is a base class.** `hydrate()`
reads a `TitleRepository` and a `MediaItemRepository` off the context, and a
concrete method on a port is a port with a dependency.

`build` returns a `BuiltRow`, never `BuiltRow | None`: an empty row and an
absent row are different states, and the composer's metrics count them
separately.

`RowContext` is a frozen dataclass of ports plus an injected clock — twelve
fields:

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
  `AsyncSession` is not safe for concurrent use, so a context carrying one is a
  context ten providers can `asyncio.gather` over — which *usually works*, and
  fails as an intermittent error under load. A row holding repositories has no
  session to share.
- **The clock is injected** because `SeasonalProvider` fires on a calendar
  window and `RediscoverProvider` on "watched > 2 years ago". It is on the
  context rather than each provider's constructor because providers are
  registered once.
- **`curated` is the repository, not a generation's rows.** A provider is
  constructed once at import, so per-household data cannot ride on its
  constructor, and pre-reading the rows would make every `GET /home` pay for a
  shelf the composer may not select.
- **`affinities` is awaited rather than held, so a cached screen costs
  nothing.** Held as a value it is computed while FastAPI resolves the
  dependency graph, which happens before the handler can look in the screen
  cache, so every request paid a library-wide aggregate to fill one field of
  twelve. Deferring it to `GenreAffinityProvider`'s own `await` makes a cache
  hit free and leaves the miss costing exactly what it did.
- **There is no `search` and no `taste` field**, because no provider read
  either. Every row is a *predicate over a repository* rather than a retrieval,
  and `TasteService.centroid` returns `None` on the request path because the
  centroid needs an embedder the route deliberately holds none of.
  `test_every_row_context_field_is_read_by_at_least_one_provider` scans for the
  next such field.

So rows are pure functions of context and trivially testable with fakes.

`BuiltRow` and `RowCard` are Pydantic DTOs (`usher.domain.rows`). `RowCard`
carries `title_id`, `kind`, `name`, `year`, `enrichment_state`, `owned`, the
progress triple `position_seconds` / `runtime_seconds` / `played`,
`episode_id` / `episode_label` for the two rows that are about a chapter, and
`artwork`.

**`title_id` is always the *series*, never the episode**, and the chapter rides
alongside it: every other field on the card describes the series, so a
`title_id` that sometimes meant an episode would be a second vocabulary in the
one field every provider's cards agree on. `episode_id` is what makes a Next Up
card *playable*. `episode_label` (`"S02E05"`) is composed on the server so the
zero-padding is decided once rather than by each client. Both are `None` on
every card of the other rows.

**`artwork` is one image id, chosen server-side against the row's own
`display_hint`** — a poster for `portrait`/`square`, a backdrop for
`landscape`/`wide`.

- **An id, never a URL and never a path.** A URL bakes the CDN base and the
  ladder rung into a screen a client caches; a path is provider vocabulary.
  `GET /images/{id}` is where both are decided, and the id survives a
  re-derivation.
- **One, not a list.** The poster/backdrop choice is keyed on the row's hint,
  which lives one level above the card.
- **`null` is a real answer**, and on a catalog that has been synced but never
  derived it is the answer for every card.

The shelf read filters on `usher.ports.images.is_servable_path`, exactly as
`GET /titles/{id}`'s `images` key does — two reads of one table disagreeing
about what is servable is the drift one definition exists to prevent. The shelf
read has already chosen one image, so a title whose chosen poster is unservable
renders `artwork: null` rather than falling through to its second poster.

Two fields are **deliberately absent**:

- **rating** — `watch_states` has no rating column and neither does
  `SourceWatchState`. A `rating` on a card is a field with no source.
- **progress, as a fraction** — `runtime_seconds` is nullable, so a fraction is
  either a division by `None` or a division by a `COALESCE`d zero, and the
  latter renders every partially-watched title as finished. The card carries
  the raw pair.

`extra="forbid"` on `DomainModel` makes those absences runtime refusals rather
than naming conventions.

### Three families

`RowFamily` is the typed vocabulary the diversity constraint is stated in — a
slug cannot serve, because `because-you-watched-<seed>` is per-seed and a
slug-keyed rule would couple the composer to the catalog.

| Family | `RowFamily` | Built from | Examples | TTL |
|---|---|---|---|---|
| **`SourceRow`** | `SOURCE` | Catalog and watch state in Postgres | Continue Watching, Next Up, Recently Added, genre shelves, collections | ~60 s |
| **`SimilarityRow`** | `SIMILARITY` | Embedding / genome neighbours of a seed title | "Because you watched *Dune*", "More like this" | hours |
| **`LLMRow`** | `CURATED` | A persisted `curated_rows` record | "Slow-burn sci-fi for a rainy night" | 5 min |

`LLMRow.build()` only *hydrates* stored output. Generation happens in a
background job — never in the request path.

**The shelves of one generation hydrate together.** `CuratedProvider` returns
up to five rows from a *single* `list_for_user`, and the first shelf to build
reads the catalog and ownership for the whole family's card ids while the rest
read from that memo — at build time, so a shelf the cap discards costs nothing
and a shelf served from the row cache never reaches the memo.

**The curated TTL is a staleness bound, not the artefact's lifetime.** The
stored row is immutable until a generation replaces it, but `RowCache` holds
the built row under `(user_id, slug)` and a generation of the same row count
re-uses the same slugs — so a long TTL keeps *last night's* row on the screen.
The curation job runs under `usher work` and cannot invalidate an in-process
cache in the API.

Three surfaces reach the generation job and only one reports what it did.
`POST /admin/rows/regenerate` enqueues a `curate` job and answers 202;
`usher work` claims it, if this process built an `LLMClient` at all; and
**`usher curate` runs one generation in the foreground** and prints what it
bought — the pool it chose from, the rows kept, the drops by reason with all
five reasons and their zeros, the token counts and the cost. It writes through
the same `CurationService`, so it is a surface rather than a second
implementation.

## Dynamic composition

Rows are proposed rather than listed — a provider proposes, and the composer
decides. Diversity is a property of a *set*, and an eager builder never has
one, so its constraint's input becomes build order.

```python
class RowProvider(ABC):
    @abstractmethod
    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """Return 0..n candidate rows with relevance scores."""
```

`Sequence`, not `list`: a caller must not mutate a provider's return.

`ScoredRow` carries the `Row` itself, its `score` and a `pinned` flag. It
carries the row rather than a slug because the slug form needs a
`dict[str, Row]` on the composer — a second source of truth, and a `KeyError`
waiting for the first provider that proposes two rows under one slug.

**Scores are module constants, not configuration**, and `pinned` is how
`ContinueWatchingProvider`'s "always ranked first" is expressed. "Always first"
is a **positional** guarantee; scores are minted per provider from unrelated
signals with nothing normalising them, so a guarantee expressed as "a score
high enough to win" is one another provider's arithmetic can silently take
away.

The home service collects all proposals, sorts by `(-score, slug)`, applies
diversity constraints, builds the top N **sequentially**, drops any that build
empty, and returns them.

- **The per-family cap is applied at *selection***, because it bounds how many
  rows get built.
- **"No three consecutive similarity rows" is applied to the *returned
  sequence***, after rows that built empty are dropped. Applied at selection
  instead, `[S, X, S, S]` with `X` building empty returns `[S, S, S]` — a
  violated constraint with nothing raised. A row that would be the third is
  **deferred** and re-offered at every later position rather than dropped; with
  nothing to interleave the screen stops at two similarity rows.
- **The tie is broken by the slug and never by registration order.**
- `_MAX_ROWS` (10) and `_MAX_PER_FAMILY` (4) are module constants and
  constructor defaults rather than settings.

**Rows build sequentially, on the request's own session.** `AsyncSession` is
not safe for concurrent use, so `asyncio.gather` over ten providers sharing a
request's session is a corruption that usually works, and one that fails as an
intermittent `InvalidRequestError` under load. The escapes are worse at this
scale — a session per row is ten connections for one home screen.
`usher.home.compose.duration` and the per-provider `usher.row.build.duration`
breakdown are what turn revisiting this into a number, and `usher home`
measures it.

⚠️ **Compose cost is a property of the household, not of the composer.** On a
household owning a normal library it is an order of magnitude under budget; on
one owning the whole catalog it is over budget and `genre-affinity` is almost
all of it — so the answer is to fix one provider rather than to run nine
concurrently on one session.

**Which providers compose is the registry minus what an operator switched
off.** `services/rows/__init__.py`'s `ROW_PROVIDERS` is the composition point —
registration in code is what makes a provider exist — and `row_provider_settings`
is a table of **overrides** left-joined onto it, written only by
`PUT /admin/rows/providers/{slug}` ([07](07-client-api.md)). It ships empty and
is never seeded, so **absence means enabled**. The join is one function used by
every composer — `GET /home`, `usher home`, and the background screen refresh —
because a refresh composing the unfiltered registry would write the disabled
shelf back into the cache the toggle just cleared. This is *filtering*, never
*enumeration*: no composition root names a provider.

Each provider declares a `slug_prefix`, and every row it proposes mints its
slug from that constant. It is what `usher.row.build.duration`'s `provider`
label carries: bounded at ten, where a row slug is bounded by the catalog.

| Provider | Fires when | Emits |
|---|---|---|
| `ContinueWatchingProvider` | Anything in progress | 1 row, always ranked first |
| `NextUpProvider` | Series with an unwatched next episode — **started, never merely owned** | 1 row |
| `RecentlyAddedProvider` | New items in the window | 1 row |
| `BecauseYouWatchedProvider` | Recent high-engagement titles carrying neighbours | 1 row *per seed*, capped at 3 |
| `FranchiseProvider` | ≥ 2 owned titles in a collection **and ≥ 1 of them unplayed** — movies only | 1 row per franchise, capped at 2 |
| `GenreAffinityProvider` | The household watches a genre disproportionately to its share of their library | 1–3 rows |
| `SeasonalProvider` | Calendar window (Halloween, holidays) — curated by the author, not derived | 0–1 rows |
| `PeopleProvider` | Recurring director or actor in history — 3 distinct engaged titles | 0–2 rows |
| `CuratedProvider` | A generation is in `curated_rows`; it hydrates, never generates | 0–5 rows |
| `RediscoverProvider` | Watched > 2 years ago, most-rewatched first | 0–1 rows |

**A provider that is not registered is dead code that looks exactly like a
provider with nothing to say** — the one failure a composed home screen cannot
show from the outside. The registry holds ten, asserted by name and by count,
and seven cross-provider invariants are asserted over it, so a new provider
inherits them on the day it is written: that only Continue Watching reaches the
top score, that every score is on one comparable scale, that every provider
returns nothing against an empty database, that none falls back to popular
titles on a household that has watched nothing, that every one composes with no
embedder, that its cases name the wrong implementation they rule out, and that
it reaches no port the context carries.

**`CuratedProvider` is deliberately not on the "may fire on a household that
has watched nothing" allowlist.** A curated shelf *is* a claim about the
person, so proposing one for a household with no generation would be the
popular-titles fallback arriving through the one door that costs money.

**Its score is `0.85`, chosen against the whole table.** Continue Watching
(1.0, pinned) and Next Up (0.90) are about *intent*, and a shelf a model
proposed overnight must never outrank either; everything at 0.80 and below is a
discovery claim computed from a single signal, while this one reads the whole
recent history against a 200-title pool and is the only row that cost money.
Being outranked here is "not shown" rather than "shown lower". Every shelf in
one generation carries the same score, because `(-score, slug)` already breaks
the tie on a positional, zero-padded slug.

**`SeasonalProvider`'s calendar→signal mapping is a taste judgement with no
data source**, and the only thing of that kind in `services/`. It ships as a
module-level table of three windows curated by the author — Halloween/Horror,
December/`christmas`, Valentine's/Romance — marked as curated in the module
that holds it. The table is Gregorian, northern-hemisphere and anglophone by
construction, because adding other windows would be the same guess made less
carefully rather than a measurement. It is code rather than configuration so
that changing it is a change with a diff.

**Those three windows are 46 days, so this provider returns nothing for roughly
320 days of the year.** Two properties are asserted about the table rather than
about a built row, because both failures produce a row that is permanently
absent with no error: no window may wrap the year end, and no row TTL may
outlive the shortest window.

**`PeopleProvider` means three distinct engaged *titles* in a cast or directing
credit.** Two is a coincidence in any household that watches a studio's output.
*Distinct titles*, never credits: a person credited twice on one film is one
title's worth of evidence. The role half matters as much — a person credited on
six films as a gaffer is recurring under any counting rule and means nothing. A
billing-order bound is not part of it and cannot be, because
`list_recurring_for_user` groups by `(person_id, name, kind, job)` and the row
carries no billing rank. Rows are picked by title count, then by the most
recent title crediting the person, then by id.

**`GenreAffinityProvider`'s row is proposed on the affinity and its cards are
read when it builds**, so a genre whose owned titles the household has all
watched produces a row that builds *empty* and is dropped, rather than one that
was never proposed. Its cards are owned *and* unwatched — a "you love westerns"
shelf made of the four westerns that established the affinity is circular — and
the unwatched check rolls episode watch states up to their series.

**`BecauseYouWatchedProvider`'s seed cap is the provider's own**, not the
diversity constraint: the diversity rule bounds how many similarity rows sit
next to each other, not how many exist. Three is the most it may claim before
"things like the things you watched" *is* the home screen. A candidate seed
whose neighbour set overlaps an already-emitted row's by more than half is
skipped and the next seed promoted.

**Its `reason` changes with the signal that was available**, because the
sentence is written to be spoken. `title_neighbors` is computed with or without
an embedder, so on the shipped default the neighbours are genre and keyword
overlap alone: "Because you watched Dune" is a causal claim, and with nothing
semantic computed the row says "Similar genres and themes to Dune" instead.

**`FranchiseProvider` fires on ≥ 2 owned members *and* ≥ 1 unplayed.** A
franchise the household has finished has nothing to offer. The row still
*lists* every owned member, watched ones included, because a franchise reads in
order and hiding chapters breaks the sequence. It is **movies only**, and on a
television-only household its condition is unsatisfiable by construction rather
than by absence of data — which is what an operator debugging a missing row
needs. `CollectionRepository.attach_titles` filters `kind = 'movie'` itself
rather than trusting its caller.

**Rediscover substitutes for a rating column that does not exist**, and the
substitution is on the page rather than in the query:

- **The filter is `played AND last_played_at < cutoff`.** `played` excludes an
  abandonment, which is a rejection rather than a fondness.
- **The engagement proxy is the *ordering*, never the filter**: `play_count
  DESC`. `play_count >= 2` as a filter is the tempting version and it is wrong,
  because `played AND play_count = 0` is how "history unknown" is spelled while
  the history backfill drains — so that filter returns nothing on a
  freshly-walked deployment.

**Rediscover is film-only**, because a "rediscover" card for a series is an
invitation to re-watch sixty hours.

**Recently Added is bounded by a window, not by a row count:**

- *One row per title.* An episode's `MediaItem` carries its series' `title_id`,
  so a series that landed last night is one card reporting the **newest**
  contributing file.
- *Episode rows are **not** excluded*, even though three other reads of that
  table bound themselves with `episode_id IS NULL`. A source that reports
  episode files and never a series-level row would otherwise never show a new
  series at all.
- *No user and no source.* Availability is household-wide, so this is the one
  provider whose output is identical for every member of the household.

**"Next" is a high-water mark, not a first gap:**

- *The mark is the greatest `(season_number, episode_number)` among played
  episodes, and the next episode is the one after it.* The alternative tells a
  household that skipped S02E05 to watch S02E05 tonight, and every night after,
  because nothing can dismiss a card. High-water costs the opposite failure and
  that one is recoverable.
- *The mark is a position, never `ORDER BY last_played_at DESC`.* A household
  that finishes season three and rewatches the pilot is not asking for S01E02,
  and `last_played_at` is NULL on nearly every walk-sourced row.
- *A series with nothing played emits **nothing**.* A series never started has
  a *first* episode, not a next one, and "S01E01 of everything unstarted" is a
  generic row wearing a personalised row's title.
- *A finished series emits **nothing**, and never wraps to the pilot.*
- *Specials — season 0 — are excluded on both sides.* A special has no defined
  position in the narrative sequence, which is what season 0 means.

**"Anything in progress" is `NOT played AND position_seconds > 0`, ordered
`last_played_at DESC NULLS LAST`:**

- *Both predicates, not one.* Without `played`, a title finished last night
  heads the row; without `position_seconds > 0`, the answer is the entire
  unwatched library.
- *A minimum position is the **provider's**, not the query's.* A title
  abandoned at three seconds is in progress by this definition and stays there
  forever. The floor is a product tunable, the percentage spelling divides by a
  nullable `runtime_seconds`, and Postgres uses a partial index whenever the
  query's predicate implies the index's — so a tighter caller is free and a
  tighter index predicate is a migration per adjustment.
- *`NULLS LAST` is correctness, not formatting.* Postgres orders `DESC` as
  NULLS FIRST, so the obvious spelling leads Continue Watching with precisely
  the rows the system knows least about. An undatable state sorts last rather
  than being dropped, because dropping it empties the row entirely on a
  walk-only deployment.
- *An in-progress episode is returned as itself, never rolled up to its
  series*, because the card resumes a file. `list_recent` rolls up instead,
  because a title-only read of watch history answers films only on a library
  that is mostly episodes.

Adding a row type is a subclass and a registration.

## Taste

The **taste centroid** is the mean embedding of recently watched, highly
engaged titles, computed per user and cached. It is cheap, local, and reused
for seeding similarity rows, ranking search results and pre-filtering the LLM
candidate pool. It is recency-weighted so it tracks changing taste rather than
averaging a lifetime.

**"Highly rated" becomes "finished, and finished twice is better."**
`watch_states` carries no `rating` and no `favorite`, and neither does
`SourceWatchState`. Two tiers over the engagement signal this schema holds: a
title the household **rewatched** (`play_count >= 2`) weighs 1.00, one it
merely **finished** weighs 0.60.

**Abandonment is expressed by absence, never by a negative weight.** A title
started and dropped twelve minutes in is evidence of nothing much, and a signal
whose sign is a guess is worse than one that is absent.

**Recency is a 50-title rank window with a linear ramp to a 0.25 floor, not a
half-life.** A half-life *is* a window, with an edge nobody wrote down and
nobody can see; and ranking by recency rather than by wall-clock normalises by
the household's own viewing pace.

**A household below five engaged titles gets no centroid, and the refusal is
written rather than skipped.** A centroid over one title *is* that title's
vector. The stored row carries a NULL centroid so that household is re-claimed
exactly once when its history moves.

**With no embedder there is no centroid at all — `None`, never a zero vector.**
The embedder is optional and off by default, so this is the shipped
configuration rather than an edge case. Every consumer drops the signal rather
than zeroing it: a deployment without an embedder gets a home screen with
**fewer rows, not worse rows**.

### Genre affinity is not computed from the centroid

Read as "taste centroid concentrated in a genre", the most broadly-useful
provider becomes the one that **never fires** on the default deployment — and
it fails in the direction hardest to notice: the screen still renders and the
row that would have said something true is simply absent.

Genre affinity is therefore **counts over `titles.genres`**, and it fires when
the household watches a genre disproportionately to its share of their library:

```
share_watched(g) = weighted engaged titles carrying g / weighted engaged titles
share_library(g) = owned titles carrying g           / owned titles
lift(g)          = share_watched(g) / share_library(g)
```

**The baseline is the owned library**, because the household's own distribution
makes every lift exactly 1.0 by construction, while the whole catalog tells a
household that owns nothing but horror that it loves horror — the library made
that choice and the person emitted no information. The owned library is the
household's actual choice set, which makes affinity *lift over opportunity*.

Fires at `lift >= 1.5` with at least 4 supporting titles. The support floor
kills a genre watched once, whose lift in a thin library is in the tens. A
genre the library does not carry yields no lift rather than an infinite one.

It reads the **same** engaged window the centroid does, so there is one
definition of what this household watches.

## LLM curation

**No collaborative filtering.** At household scale there is no co-occurrence
signal — every recommendation is a permanent cold start. Usher is
content-based, plus borrowed aggregate signals where useful.

Generation runs nightly and on demand:

1. **Assemble context** — the household's last 25 finished titles, most recent
   first, with a rewatch marked, plus a candidate pool of ~200 unwatched titles,
   **re-ranked** by taste-centroid proximity where a centroid exists. The pool
   spans the whole catalog, not just the library, so suggestions can include
   things to seek out.

   - **Membership is "unwatched", full stop** — `played`, rolled up through
     `episodes.title_id`, expressed *inside* the statement rather than
     subtracted after a `LIMIT`. Ownership and popularity are **ranking keys**,
     which is what keeps "the pool spans the whole catalog" true. The order is
     `owned DESC, carries an affinity genre DESC, tmdb_vote_count DESC NULLS
     LAST, id` — and the `id` tail decides **membership** rather than only
     order, because the `LIMIT` falls inside a tie.
   - **`vote_count`, not `popularity`**, because leading with a column that is
     `NOT NULL DEFAULT 0` in `tmdb_ids` lets a crosswalk-linked skeleton at
     `0.0` outrank an unlinked title with half a million votes. ⚠️ On a
     bootstrap-only catalog `tmdb_vote_count` is NULL on every row and this key
     selects nothing, so the `id` tail decides membership for a much larger tie
     ([#39](https://github.com/anirudhlath/usher/issues/39)).
   - **The re-rank permutes the embedded members among the positions they
     already occupy**, so a candidate the centroid cannot speak about keeps its
     exact index. The pool is a function of the household, not of the embedder.
   - **The centroid cannot be the pre-filter's spine**, because
     `USHER_EMBEDDING_ENABLED` defaults to `False` and curation would be the
     feature that never fires on a default deployment.
2. **One structured call** to any OpenAI-compatible endpoint →
   `[{title, reason, item_ids ⊆ pool}]`, 3–5 rows. **`item_ids` are indices
   into the pool, never UUIDs** — a UUID handle costs several times the prompt
   tokens, is the least accurate of the spellings measured, and an index is the
   only one that is bounds-checked.
3. **Validate** — IDs not in the pool are dropped; rows below a minimum length
   are discarded **whole rather than padded**, because a padded row is a
   fabricated recommendation wearing a model's reason string. Hallucinated
   identifiers never reach a client.

   `usher.services.curation_validate` is a module of pure functions over a
   parsed `dict` and the generation's own index → UUID map:

   - **The map is a `Mapping[int, UUID]` and the validator does no arithmetic
     on it**, so a sparse pool and a 1-based prompt need no special case and
     `pool[-1]` — legal Python, and a real film — is unreachable.
   - **The validator coerces before it compares.** `str(value).strip()` for
     `int` and `str` only: a `bool` is refused before the `int` branch, a
     `float` is refused rather than rounded, and prose is never coerced at all.
     A provider returning ids as JSON integers where the schema asked for
     strings otherwise drops every one of them while `llm_calls` records a
     successful, fully-billed call.
   - **`usher.curation.dropped` carries a `reason` label** with five members —
     `not_in_pool`, `unparseable`, `duplicate`, `row_unusable`,
     `row_too_short`. Two count **rows** and three count **cards**.
   - **Zero rows is unrepresentable as a success**, not merely checked for: the
     return type is a union whose success arm cannot be built with an empty
     `rows` and whose failure arm has no `rows` attribute.
   - **The validator does not cap the number of rows**, deliberately: a cap is
     a product bound and belongs with `CuratedProvider`'s `0–5` budget. It does
     own the *ordering* — `curated_rows.slug` is zero-padded to the width of the
     generation, because the composer breaks score ties on `slug`.
4. **Persist** as `curated_rows`.

- **The prompt is code, and the two numbers in it that have to agree with
  something else are rendered rather than written**: the pool's length and
  `min_cards`. A prompt asking for four cards under a validator demanding five
  drops every row.
- **The `json_schema` sent with the request is an optimisation and never the
  contract.** The validator checks the answer whatever the provider did.
- **Failure is non-fatal to the screen and fatal to the job.** A failed
  generation never reaches `replace_for_user`, so previous rows persist. A
  generation that validated to **nothing** raises `PortDataMalformed`, which
  parks the job: the causes are permanent properties of that request, so five
  more completions reach the same answer at five times the price.
- **A pool below `min_cards` is refused before the spend**, with a sentence
  naming how many candidates were found and what the floor is, no `llm_calls`
  row, and no completion bought.
- **`llm_calls` gets a row on every path that *attempted* a completion**,
  `ok = false` included. The one path that writes none is the one that
  attempted nothing.

Cost is one modest completion per user per day. The candidate pool being
pre-filtered locally is what keeps this affordable — the model sees 200 titles
it might plausibly recommend, not a catalog.

### 🔴 The central product risk

**On a small local model the curated shelf is substantively what
`GenreAffinityProvider` already gives away free.** Most generated headings were
genre labels, which the prompt explicitly forbids, and several recurred
verbatim across separate generations. **The prompt's grouping instruction is
not self-enforcing and nothing in this system checks it.** That is a property
of the design; the rate is a property of one model, and the way to find out
whether a frontier model obeys it is to run the measurement again rather than
to assume. Curated rows are additive, home composes without them, and a
duplicated genre shelf is a disappointment rather than a defect.

⚠️ **De-duplication is within a row only.** A title appearing on *two* shelves
of one generation is not counted at all; the prompt's instruction is the only
defence, and a prompt rule is not a guarantee.

⚠️ **Four of the five `DropReason` members are close to unreachable under a
provider honouring guided decoding**, so a dashboard of permanent zeros is
expected rather than a broken counter. The vocabulary is still right: a reason
absent from a tally is indistinguishable from a reason nobody counts.

## Caching

| Layer | Lifetime |
|---|---|
| Built rows | Per-row TTL, in-process — 60 s (Continue Watching, Next Up) to 12 h (Seasonal), each row's own. **No stale-serve grace** |
| Composed home screen | 30 s per user, in-process, **plus a 60 s stale-serve grace** (`SCREEN_STALE_GRACE`). Between 30 s and 90 s the cached screen is served and a refresh is scheduled; past 90 s it is a hard miss |
| **Neighbour tables** | ⚠️ **Not rebuilt on any event.** `usher similar --rebuild` is an operator's command, a cron entry, or the scheduler's period |
| Curated rows | 5 min per built row, in-process |
| Taste centroid | Recomputed when the household's `max(watch_states.updated_at)` moves — a fingerprint, not an event |
| Genre affinity | **Nothing stored**, memoised for the life of one request |

Rows are recomputed lazily and served stale while refreshing, so the home
screen never blocks on a slow row.

**Both caches are in-process** — a dict in the server, per worker, emptied by a
restart. The shipped deployment runs one worker; with two replicas the screens
stay within their TTL of each other but **invalidation does not cross
processes**.

**"Served stale while refreshing" is one `rows.refresh` lane draining one
bounded deduplicating queue of stale keys, each refresh on a session of its own
through `composition.unit_of_work`, drop-on-full.** The request's session is
committed and closed when the handler returns, so the queue carries a frozen
`User` and nothing request-scoped. Its bound is `REFRESH_QUEUE_SIZE` = 32 keys
with one consumer ([01](01-architecture.md)).

- *The screen never waits on it* — the handover is a **synchronous** callable,
  so there is nothing for a request to await.
- *The refresh is bounded* — full means **dropped**, never blocked, which is
  safe because an entry past `TTL + grace` is a hard miss and the next request
  rebuilds.
- *The grace window is gated on there being a refresher.* A composer handed
  none — `usher home`, whose process ends when the command does — serves
  nothing stale at all, because a stale screen with nothing to replace it is
  strictly worse than the miss it avoided and is silent.
- *A stale serve counts as a `usher.cache.hits` point carrying
  `freshness="stale"`* — a hit because the request paid no rebuild, labelled
  because a plain hit hides the one thing the feature trades away
  ([10](10-telemetry-and-dashboards.md)).

**Row TTLs are unaffected by a screen refresh**, which re-proposes,
re-selects and re-orders while *reusing* every row whose own TTL is still
running. That is why the row half has no grace of its own: the refresh unit is
a screen, and a per-row grace with no per-row refresh behind it would serve
stale rows that nothing ever replaces.

**Invalidation is driven by the push lane, by demand reads and by enrichment,
never by the nightly walk.** A walk merges up to a million states, so one
invalidation per merged row is a fan-out per row per night; a walk that
finishes at 04:00 is on the screen by 04:00:30 through the 30 s screen TTL.
`WatchStateSyncService` is handed no cache at all.

**Enrichment is the third trigger**, because it is the one write whose
staleness the TTLs cannot absorb: it rewrites a card's `name`, `year`,
`enrichment_state` and `artwork` — every field a `RowCard` carries — and lands
on rows whose TTLs are hours. Without it a title enriched inside that window
keeps rendering under its skeleton name with no artwork.

**`title.updated` does not cover it.** That frame is a statement to a *client*,
and the console's handler is colour-only by design. A cache the server reads
from has to be told separately.

**`RowCache.invalidate_titles` takes no `user_id`**, unlike `invalidate`. A
play button is one household's act; enrichment is a *catalog* write, equally
stale on every screen holding the title. It is keyed on the title rather than
on the write, so an invalidation drops only the entries whose cards name the
enriched title and a screen can be invalidated at most once per card however
many titles a backfill touches — strictly less churn than the 30 s TTL already
forces.

**Cache keys carry the user**, taken from the request's own `current_user`, so
replacing that one dependency remains the whole of adding authentication. A key
that omitted it would work today and serve one household's screen to another
the day auth lands, with no error, no log line and no metric.

**The row half is bounded and evicts soonest-to-expire first**, because
`because-you-watched-<seed>` is one slug per seed and expired entries are read
past rather than removed.

**Neighbour staleness has two causes and they are not the same kind of
problem:**

- *The blend changed.* That half is **a query**: `title_neighbors.blend_fingerprint`
  records which blend produced each row, `usher similar <title id>` says so per
  title, and `usher.similarity.neighbors.stale` counts the table.
- *Some other title was embedded.* No per-row predicate can decide it without
  recomputing the row, so `computed_at` stays beside the fingerprint and it
  remains an operator's job.

**`user_taste` stores the `max(watch_states.updated_at)` its centroid was
computed from**, and a demand read recomputes when the household's current max
**`IS DISTINCT FROM`** it. `IS DISTINCT FROM` rather than `<`: a *newer* state
raises the max; a *deleted* state lowers it, and `<` would serve a centroid
computed over a row that no longer exists forever; and a *cleared* history
makes the aggregate `NULL`, where `stored < NULL` is never true. The merge path
publishes nothing and does not know `user_taste` exists.

**Genre affinity is not stored, and sharing the centroid's row would be wrong
rather than merely wasteful.** That row is invalidated on `model_name IS
DISTINCT FROM`; genre affinity has no model, and a deployment with no embedder
would have to write a `model_name` for a model it does not have.

What it has instead is two memos inside `TasteService`, both dying with the
service — one request on the route, one unit of work in the CLI and the worker:

- **the engaged window** (`WatchStateRepository.list_recent(50)`), keyed by
  `user_id` — a memo on a per-user read is the one optimisation whose failure
  mode is a data leak — and re-read whenever a caller presents a
  `max(watch_states.updated_at)` that disagrees with the one it was filled at;
- **the library-wide genre counts**, which take no `user_id` at all, are the
  denominator of every lift, and change only when the library does.

## Alfred

Row providers are the natural surface for the voice assistant. Alfred asking
"what should I watch tonight?" resolves to a composed row set with reasons
attached — the `reason` field is already written to be spoken aloud. Alfred can
also register its own provider later.
