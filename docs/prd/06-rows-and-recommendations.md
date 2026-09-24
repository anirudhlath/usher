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

`build` returns a `BuiltRow`, never `None`: an empty row and an absent row are
different states, and the composer's metrics count them separately.

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

It carries no `AsyncSession`, no search service and no taste centroid.
`affinities` is computed only when a provider awaits it, so a screen served
from cache does not compute it. Rows are pure functions of the context.

`BuiltRow` and `RowCard` are Pydantic DTOs (`usher.domain.rows`). `RowCard`
carries `title_id`, `kind`, `name`, `year`, `enrichment_state`, `owned`, the
progress triple `position_seconds` / `runtime_seconds` / `played`,
`episode_id` / `episode_label` for the two rows that are about a chapter, and
`artwork`.

**`title_id` is always the *series*, never the episode**, and the chapter rides
alongside it. `episode_id` is what makes a Next Up card *playable*.
`episode_label` (`"S02E05"`) is composed on the server. Both are `None` on
every card of the other rows.

**`artwork` is one image id, chosen server-side against the row's own
`display_hint`** — a poster for `portrait`/`square`, a backdrop for
`landscape`/`wide`.

- **An id, never a URL and never a path.** `GET /images/{id}` resolves it.
- **`null` is a real answer**, and on a catalog that has been synced but never
  derived it is the answer for every card.

`artwork` applies the same servability rule as `GET /titles/{id}`'s `images`
key. A title whose chosen poster is unservable renders `artwork: null` rather
than falling through to its second poster.

Two fields are **absent**:

- **rating** — Usher stores no rating.
- **progress, as a fraction** — the card carries the raw
  `position_seconds` / `runtime_seconds` pair, and `runtime_seconds` is
  nullable.

### Three families

`RowFamily` is the typed vocabulary the diversity constraint is stated in.

| Family | `RowFamily` | Built from | Examples | TTL |
|---|---|---|---|---|
| **`SourceRow`** | `SOURCE` | Catalog and watch state in Postgres | Continue Watching, Next Up, Recently Added, genre shelves, collections | ~60 s |
| **`SimilarityRow`** | `SIMILARITY` | Embedding / genome neighbours of a seed title | "Because you watched *Dune*", "More like this" | hours |
| **`LLMRow`** | `CURATED` | A persisted `curated_rows` record | "Slow-burn sci-fi for a rainy night" | 5 min |

`LLMRow.build()` only *hydrates* stored output. Generation happens in a
background job — never in the request path.

**The curated TTL bounds staleness, not the artefact's lifetime.** The stored
row is immutable until a generation replaces it, and a new generation of the
same row count re-uses the same slugs, so the previous shelf stays on screen
until its 5 min TTL runs out. The curation job runs under `usher work` and does
not invalidate the API's in-process cache.

Three surfaces reach the generation job and only one reports what it did.
`POST /admin/rows/regenerate` enqueues a `curate` job and answers 202;
`usher work` claims it only when `USHER_LLM_ENABLED=true`; and
**`usher curate` runs one generation in the foreground** and prints what it
bought — the pool it chose from, the rows kept, the drops by reason with all
five reasons and their zeros, the token counts and the cost. It writes the same
rows the job does.

## Dynamic composition

Rows are proposed rather than listed — a provider proposes, and the composer
decides.

```python
class RowProvider(ABC):
    @abstractmethod
    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """Return 0..n candidate rows with relevance scores."""
```

`ScoredRow` carries the `Row` itself, its `score` and a `pinned` flag.

**Scores are module constants, not configuration.** `pinned` places
`ContinueWatchingProvider`'s row first regardless of any other provider's
score.

The home service collects all proposals, sorts by `(-score, slug)`, applies
diversity constraints, builds the top N **sequentially**, drops any that build
empty, and returns them.

- **The per-family cap is applied at *selection***.
- **"No three consecutive similarity rows" is applied to the *returned
  sequence***, after rows that built empty are dropped. A row that would be the
  third is **deferred** and re-offered at every later position rather than
  dropped; with nothing to interleave the screen stops at two similarity rows.
- **The tie is broken by the slug and never by registration order.**
- At most **10 rows** per screen and **4 per family**; both are module
  constants, not settings.

**Rows build sequentially, on the request's own session.**
`usher.home.compose.duration` and the per-provider `usher.row.build.duration`
breakdown record compose cost; `usher home` measures it.

⚠️ **Compose cost is a property of the household, not of the composer.** On a
household owning a normal library it is an order of magnitude under budget; on
one owning the whole catalog it is over budget and `genre-affinity` is almost
all of it.

**Which providers compose is the registry minus what an operator switched
off.** A provider exists by registration in `ROW_PROVIDERS`
(`services/rows/__init__.py`), and `row_provider_settings` is a table of
**overrides** on it, written only by `PUT /admin/rows/providers/{slug}`
([07](07-client-api.md)). It ships empty and is never seeded, so **absence
means enabled**. The filter applies to every composer — `GET /home`,
`usher home`, and the background screen refresh.

Each provider declares a `slug_prefix`, and every row it proposes mints its
slug from it. `usher.row.build.duration`'s `provider` label carries the prefix,
so it has at most ten values.

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

**Every registered provider holds to these invariants:** only Continue Watching
reaches the top score; every score is on one comparable scale; every provider
returns nothing against an empty database; none falls back to popular titles
on a household that has watched nothing; and every one composes with no
embedder.

**`CuratedProvider` never fires on a household that has watched nothing.**

**`CuratedProvider` scores `0.85`**: below Continue Watching (1.0, pinned) and
Next Up (0.90), above every other provider (0.80 and below). Every shelf in one
generation carries the same score, so the generation's own order holds through
its positional, zero-padded slugs.

**`SeasonalProvider` ships a module-level table of three windows curated by
the author** — Halloween/Horror, December/`christmas`, Valentine's/Romance. The
table is Gregorian, northern-hemisphere and anglophone. It is code, not
configuration.

**Those three windows are 46 days, so this provider returns nothing for roughly
320 days of the year.** No window wraps the year end, and no row TTL outlives
the shortest window.

**`PeopleProvider` means three distinct engaged *titles* in a cast or directing
credit.** *Distinct titles*, never credits: a person credited twice on one film
counts once. Other crew roles never count, and there is no billing-order bound.
Rows are picked by title count, then by the most recent title crediting the
person, then by id.

**`GenreAffinityProvider`'s row is proposed on the affinity and its cards are
read when it builds**, so a genre whose owned titles the household has all
watched produces a row that builds *empty* and is dropped. Its cards are owned
*and* unwatched, and the unwatched check rolls episode watch states up to their
series.

**`BecauseYouWatchedProvider` emits at most three seeds**, a cap of its own
separate from the diversity constraint. A candidate seed whose neighbour set
overlaps an already-emitted row's by more than half is skipped and the next
seed promoted.

**Its `reason` changes with the signal that was available.**
`title_neighbors` is computed with or without an embedder; on the shipped
default the neighbours are genre and keyword overlap alone, and the row says
"Similar genres and themes to Dune" instead of "Because you watched Dune".

**`FranchiseProvider` fires on ≥ 2 owned members *and* ≥ 1 unplayed.** The row
*lists* every owned member, watched ones included. It is **movies only**, so on
a television-only household it never fires.

**Rediscover:**

- **The filter is `played AND last_played_at < cutoff`.** An abandoned title is
  excluded.
- **Engagement is the *ordering*, never the filter**: `play_count DESC`.
- **Rediscover is film-only.**

**Recently Added is bounded by a window, not by a row count:**

- *One row per title.* A series that landed last night is one card reporting
  the **newest** contributing file.
- *Episode files count.* A source that reports episode files and never a
  series-level row still shows a new series.
- *No user and no source.* This is the one provider whose output is identical
  for every member of the household.

**"Next" is a high-water mark, not a first gap:**

- *The mark is the greatest `(season_number, episode_number)` among played
  episodes, and the next episode is the one after it.* A skipped episode is not
  offered.
- *The mark is a position, never `ORDER BY last_played_at DESC`.* A household
  that finishes season three and rewatches the pilot is not offered S01E02.
- *A series with nothing played emits **nothing**.*
- *A finished series emits **nothing**, and never wraps to the pilot.*
- *Specials — season 0 — are excluded on both sides.*

**"Anything in progress" is `NOT played AND position_seconds > 0`, ordered
`last_played_at DESC NULLS LAST`:**

- *There is no minimum position.* A title abandoned at three seconds is in
  progress by this definition and stays there.
- *An undatable state sorts last rather than being dropped.*
- *An in-progress episode is returned as itself, never rolled up to its
  series*; the card resumes that file.

Adding a row type is a subclass and a registration.

## Taste

The **taste centroid** is the mean embedding of recently watched, highly
engaged titles, computed per user and cached. It is cheap, local, and reused
for seeding similarity rows, ranking search results and pre-filtering the LLM
candidate pool. It is recency-weighted so it tracks changing taste rather than
averaging a lifetime.

**"Highly rated" becomes "finished, and finished twice is better."** A title
the household **rewatched** (`play_count >= 2`) weighs 1.00, one it merely
**finished** weighs 0.60.

**Abandonment is expressed by absence, never by a negative weight.**

**Recency is a 50-title rank window with a linear ramp to a 0.25 floor, not a
half-life**, ranked by the household's own viewing order rather than by
wall-clock time.

**A household below five engaged titles gets no centroid, and the refusal is
written rather than skipped.** The stored row carries a NULL centroid, so that
household is recomputed exactly once when its history moves.

**With no embedder there is no centroid at all — `None`, never a zero vector.**
The embedder is optional and off by default, so this is the shipped
configuration. Every consumer drops the signal rather than zeroing it: a
deployment without an embedder gets a home screen with **fewer rows, not worse
rows**.

### Genre affinity is not computed from the centroid

Genre affinity is **counts over `titles.genres`**, so it fires on a deployment
with no embedder. It fires when the household watches a genre
disproportionately to its share of their library:

```
share_watched(g) = weighted engaged titles carrying g / weighted engaged titles
share_library(g) = owned titles carrying g           / owned titles
lift(g)          = share_watched(g) / share_library(g)
```

**The baseline is the owned library**, so affinity is *lift over opportunity*.

Fires at `lift >= 1.5` with at least 4 supporting titles. A genre the library
does not carry yields no lift rather than an infinite one.

It reads the **same** engaged window the centroid does.

## LLM curation

**No collaborative filtering.** Usher is content-based, plus borrowed aggregate
signals where useful.

Generation runs on demand only — `POST /admin/rows/regenerate` or
`usher curate`. **Usher schedules no curation**; a nightly generation is an
operator's cron entry. Each generation:

1. **Assemble context** — the household's last 25 finished titles, most recent
   first, with a rewatch marked, plus a candidate pool of ~200 unwatched titles,
   **re-ranked** by taste-centroid proximity where a centroid exists. The pool
   spans the whole catalog, not just the library, so suggestions can include
   things to seek out.

   - **Membership is "unwatched", full stop** — `played`, rolled up to the
     series. Ownership and popularity are **ranking keys**. The order is
     `owned DESC, carries an affinity genre DESC, tmdb_vote_count DESC NULLS
     LAST, id` — and the `id` tail decides **membership** when the pool's
     limit falls inside a tie.
   - ⚠️ On a bootstrap-only catalog `tmdb_vote_count` is NULL on every row and
     that key selects nothing, so the `id` tail decides membership for a much
     larger tie ([#39](https://github.com/anirudhlath/usher/issues/39)).
   - **The re-rank permutes the embedded members among the positions they
     already occupy**, so a candidate the centroid cannot speak about keeps its
     exact index. The pool is a function of the household, not of the embedder,
     and curation runs with `USHER_EMBEDDING_ENABLED=false`.
2. **One structured call** to any OpenAI-compatible endpoint →
   `[{title, reason, item_ids ⊆ pool}]`, 3–5 rows. **`item_ids` are indices
   into the pool, never UUIDs.**
3. **Validate** — IDs not in the pool are dropped; rows below a minimum length
   are discarded **whole rather than padded**. Hallucinated identifiers never
   reach a client.

   - **An index outside the pool, negative included, is dropped.**
   - **Ids are accepted as JSON integers or strings**, whitespace stripped. A
     `bool` or a `float` is refused, never rounded, and prose is never coerced.
   - **`usher.curation.dropped` carries a `reason` label** with five members —
     `not_in_pool`, `unparseable`, `duplicate`, `row_unusable`,
     `row_too_short`. Two count **rows** and three count **cards**.
   - **Validation does not cap the number of rows**; `CuratedProvider`'s `0–5`
     is the cap. `curated_rows.slug` is zero-padded to the width of the
     generation, so shelves keep the generation's order.
4. **Persist** as `curated_rows`.

- **The prompt is code**, and the pool's length and `min_cards` are rendered
  into it from the values the validator uses.
- **The `json_schema` sent with the request is advisory, never the
  contract.** The validator checks the answer whatever the provider did.
- **Failure is non-fatal to the screen and fatal to the job.** A failed
  generation never replaces the stored rows, so previous rows persist. A
  generation that validated to **nothing** raises `PortDataMalformed`, which
  parks the job rather than retrying it.
- **A pool below `min_cards` is refused before the spend**, with a sentence
  naming how many candidates were found and what the floor is, no `llm_calls`
  row, and no completion bought.
- **`llm_calls` gets a row on every path that *attempted* a completion**,
  `ok = false` included.

Cost is one modest completion per generation — one per user per day under a
nightly cron.

### 🔴 The central product risk

**On a small local model the curated shelf can be substantively what
`GenreAffinityProvider` already gives away free** — genre-label headings, which
the prompt forbids, and headings that recur verbatim across generations.
**The prompt's grouping instruction is not self-enforcing and nothing in this
system checks it.** Curated rows are additive and home composes without them.

⚠️ **De-duplication is within a row only.** A title appearing on *two* shelves
of one generation is not counted at all; only the prompt discourages it.

⚠️ **Four of the five `DropReason` members are close to unreachable under a
provider honouring guided decoding**, so a dashboard of permanent zeros is
expected rather than a broken counter.

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

**Both caches are in-process** — per worker, emptied by a restart. The shipped
deployment runs one worker; with two replicas the screens stay within their TTL
of each other but **invalidation does not cross processes**.

**"Served stale while refreshing" is one `rows.refresh` lane draining one
bounded, deduplicating queue of stale keys, each refresh on a session of its
own, drop-on-full.** Its bound is `REFRESH_QUEUE_SIZE` = 32 keys with one
consumer ([01](01-architecture.md)).

- *The screen never waits on it.*
- *The refresh is bounded* — full means **dropped**, never blocked; an entry
  past `TTL + grace` is a hard miss and the next request rebuilds.
- *The grace window is gated on there being a refresher.* A composer handed
  none — `usher home`, whose process ends when the command does — serves
  nothing stale at all.
- *A stale serve counts as a `usher.cache.hits` point carrying
  `freshness="stale"`* ([10](10-telemetry-and-dashboards.md)).

**Row TTLs are unaffected by a screen refresh**, which re-proposes,
re-selects and re-orders while *reusing* every row whose own TTL is still
running.

**Invalidation is driven by the push lane, by demand reads and by enrichment,
never by the full source walk.** Usher does not schedule that walk — it is an
operator's `usher sync`, by hand or from cron — and its changes reach the screen
through the 30 s screen TTL.

**Enrichment invalidates on every user's screen.** It rewrites a card's `name`,
`year`, `enrichment_state` and `artwork`, so a title enriched while its rows'
TTLs (hours) are running shows the enriched card without waiting them out. The
invalidation drops only the entries whose cards name the enriched title, and a
screen is invalidated at most once per card however many titles a backfill
touches.

**Cache keys carry the user** the request is served as — in v1 always the
singleton default user ([07](07-client-api.md#authentication-seam)).

**The row half is bounded and evicts soonest-to-expire first**; expired
entries are read past rather than removed.

**Neighbour staleness has two causes and they are not the same kind of
problem:**

- *The blend changed.* That half is **a query**: `title_neighbors.blend_fingerprint`
  records which blend produced each row, `usher similar <title id>` says so per
  title, and `usher.similarity.neighbors.stale` counts the table.
- *Some other title was embedded.* This is not detected; `computed_at` stays
  beside the fingerprint and rebuilding remains an operator's job.

**`user_taste` stores the `max(watch_states.updated_at)` its centroid was
computed from**, and a demand read recomputes when the household's current max
**`IS DISTINCT FROM`** it — so a newer state, a deleted state and a cleared
history all trigger a recompute.

**Genre affinity is not stored.** Within one request on the route, or one unit
of work in the CLI and the worker, it memoises two reads:

- **the engaged window** (the 50-title window above), keyed by
  `user_id` and re-read whenever the household's
  `max(watch_states.updated_at)` has moved;
- **the library-wide genre counts**, which are the denominator of every lift
  and change only when the library does.

## Alfred

Row providers are the natural surface for the voice assistant. Alfred asking
"what should I watch tonight?" resolves to a composed row set with reasons
attached — the `reason` field is already written to be spoken aloud. Alfred can
also register its own provider later.
