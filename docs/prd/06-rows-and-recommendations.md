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

`RowContext` is a frozen dataclass of ports plus an injected clock — nine
fields in M7, twelve once `people`/`credits`/`collections` exist:

```python
user: User                          now: Callable[[], AwareDatetime]
titles: TitleRepository             media_items: MediaItemRepository
watch_states: WatchStateRepository  episodes: EpisodeRepository
neighbors: TitleNeighborRepository  search: SearchIndex
taste: Centroid | None
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
- **`taste` is optional** — a deployment with no embedder has no centroid
  (ADR-0022), and every reader drops the signal rather than zeroing it. Fewer
  rows, not worse rows.

So rows are pure functions of context and trivially testable with fakes.

`BuiltRow` and `RowCard` are Pydantic DTOs (`usher.domain.rows`). `RowCard`
carries `title_id`, `kind`, `name`, `year`, `enrichment_state`, `owned`, and
the progress triple `position_seconds` / `runtime_seconds` / `played`.

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
consecutive similarity rows; cap per family), builds the top N concurrently,
drops any that build empty, and returns them.

| Provider | Fires when | Emits |
|---|---|---|
| `ContinueWatchingProvider` | Anything in progress | 1 row, always ranked first |
| `NextUpProvider` | Series with an unwatched next episode | 1 row |
| `RecentlyAddedProvider` | New items in the window | 1 row |
| `BecauseYouWatchedProvider` | Recent high-engagement titles | 1 row *per seed* |
| `FranchiseProvider` | ≥ 2 owned titles in a collection — **movies only** | 1 row per franchise |
| `GenreAffinityProvider` | Taste centroid concentrated in a genre | 1–3 rows |
| `SeasonalProvider` | Calendar window (Halloween, holidays) | 0–1 rows |
| `PeopleProvider` | Recurring director or actor in history | 0–2 rows |
| `CuratedProvider` | Fresh LLM rows exist | 0–5 rows |
| `RediscoverProvider` | Watched > 2 years ago, rated highly | 0–1 rows |

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
  forever, because nothing in this document or [07](07-api-surface.md) can
  dismiss a card. The floor is left to `ContinueWatchingProvider` because it is
  a product tunable, because the percentage spelling divides by a nullable
  `runtime_seconds` and so silently empties the row on a source that reports no
  runtimes, and because Postgres uses a partial index whenever the query's
  predicate implies the index's — so a tighter caller is free and a tighter
  index predicate is a migration per adjustment.
- *`NULLS LAST` is correctness, not formatting.* `last_played_at` is nullable
  because a walk's listing frequently cannot determine it
  ([ADR-0014](decisions/0014-absent-is-not-zero.md)), and Postgres orders `DESC`
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

The **taste centroid** is the mean embedding of recently watched and highly
rated titles, computed per user and cached. It is cheap, local, and reused for:

- seeding similarity rows,
- ranking search results,
- selecting genre affinity rows,
- pre-filtering the LLM candidate pool.

Recency-weighted so it tracks changing taste rather than averaging a lifetime.

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
| Taste centroid | Invalidated on watch-state change |

Rows are recomputed lazily and served stale while refreshing, so the home screen
never blocks on a slow row.

## Alfred

Row providers are the natural surface for the voice assistant. Alfred asking
"what should I watch tonight?" resolves to a composed row set with reasons
attached — the `reason` field is already written to be spoken aloud, not just
displayed. Alfred can also register its own provider later.
