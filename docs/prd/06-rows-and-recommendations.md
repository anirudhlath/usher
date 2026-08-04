# 06 — Rows and recommendations

The home screen is composed, not configured. Rows are proposed by providers,
scored for relevance, and assembled per request — so the screen changes with
context, season, and taste rather than being a fixed list.

## The Row hierarchy

```python
class Row(ABC):
    """A named, ordered shelf of titles."""

    slug: str                    # "continue-watching", "because-you-watched-dune"
    title: str                   # display name
    reason: str | None           # subtitle: "Because you watched Dune"
    ttl: timedelta               # how long a built result may be cached

    @abstractmethod
    async def build(self, ctx: RowContext) -> BuiltRow: ...

    # concrete, shared:
    async def hydrate(self, title_ids: Sequence[UUID]) -> list[RowCard]: ...
    def empty(self) -> BuiltRow: ...
```

`RowContext` carries the user, repositories, search index, and clock — so rows
are pure functions of context and trivially testable with fakes.

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

Rows are proposed rather than listed:

```python
class RowProvider(ABC):
    @abstractmethod
    async def propose(self, ctx: RowContext) -> list[ScoredRow]:
        """Return 0..n candidate rows with relevance scores."""
```

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
| `FranchiseProvider` | ≥ 2 owned titles in a collection | 1 row per franchise |
| `GenreAffinityProvider` | Taste centroid concentrated in a genre | 1–3 rows |
| `SeasonalProvider` | Calendar window (Halloween, holidays) | 0–1 rows |
| `PeopleProvider` | Recurring director or actor in history | 0–2 rows |
| `CuratedProvider` | Fresh LLM rows exist | 0–5 rows |
| `RediscoverProvider` | Watched > 2 years ago, rated highly | 0–1 rows |

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
