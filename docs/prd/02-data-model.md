# 02 — Canonical data model

## Identity

**Every entity has a Usher-owned UUIDv7 primary key.** Provider identifiers
(`tmdb_id`, `imdb_id`, `tvdb_id`) are nullable, unique-indexed *attributes*.

**`tmdb_id` is unique per `kind`, not globally.** TMDb keys movies and TV
series in two independent integer spaces that both land in `Title.tmdb_id`,
and they overlap on 26,968 ids (measured 2026-07-30). The unique index is
`(tmdb_id, kind)`, and `TitleRepository.get_by_tmdb_id` takes a `TitleKind`
alongside the id — see
[ADR-0011](decisions/0011-tmdb-id-is-namespaced-by-kind.md). `imdb_id` and
`tvdb_id` remain single-column unique; the same ADR records why. Any API
surface that exposes a `tmdb_id` must expose its `kind` beside it, because
the number alone does not identify a TMDb entity.

This is load-bearing, not ceremony:

- The identifier spaces don't align. TMDb has ~1.23M movies; IMDb lists 12.7M
  titles. Wikidata can cross-reference only ~278k with *both* IDs. Plenty of
  what a library holds has no TMDb entry at all.
- Upstream identifiers get merged, split, and re-pointed. A `tmdb_id` is a
  *claim about* a title, not the title's identity.
- Merging two Titles later (the same film ingested twice under different
  provider IDs) becomes a repointing operation rather than a primary-key
  rewrite cascading through watch state, rows, and embeddings.

UUIDv7 rather than v4: time-ordered, so index locality stays good on
insert-heavy bulk imports.

## Enrichment tiers

Every `Title` carries an explicit state, because the catalog is deliberately
usable before it is complete (see [03](03-sources-and-sync.md)):

| State | Meaning |
|---|---|
| `skeleton` | From a bulk dataset. Name, year, runtime, genres, ratings. No overview or artwork. |
| `stub` | Seen on a source but not yet enriched. Source's own metadata only. |
| `enriched` | Full provider metadata present. `enriched_at` set. |

Every API response exposes this so clients render deliberately rather than
guessing from null fields.

Whether the *last enrichment attempt* failed is tracked separately, on
`Title.enrichment_error: str | None` — a non-null value means the last
attempt failed, but the tier above is left exactly as it was; failure does
not consume or reset a rung on the ladder. `failed` was originally a fourth
tier and was split out — see [ADR-0008](decisions/0008-enrichment-tier-vs-failure.md).

Comparing tiers (e.g. "is this an improvement over what we had") must use
`usher.domain.enums.ENRICHMENT_RANK`, never the enum members' own ordering —
`EnrichmentState` is a `StrEnum`, whose ordering is lexicographic, not
ladder position. Same ADR.

## Core entities

### Title

The canonical production. One row per film; one row per series.

```python
class Title(BaseModel):
    id: UUID
    kind: TitleKind                      # movie | series

    tmdb_id: int | None
    imdb_id: str | None
    tvdb_id: int | None

    name: str
    original_name: str | None
    sort_name: str
    year: int | None
    release_date: date | None
    end_year: int | None                 # series

    overview: str | None
    tagline: str | None
    runtime_minutes: int | None
    status: ProductionStatus | None

    genres: tuple[str, ...]
    keywords: tuple[str, ...]
    original_language: str | None        # ISO 639-1
    spoken_languages: tuple[str, ...]    # ISO 639-1
    origin_countries: tuple[str, ...]    # ISO 3166-1 alpha-2
    content_rating: str | None

    community_rating: float | None       # provider aggregate, TMDb 0-10 scale
    vote_count: int | None
    popularity: float | None

    collection_id: UUID | None
    enrichment_state: EnrichmentState
    enrichment_error: str | None         # non-null => last enrichment attempt failed
    enriched_at: datetime | None
    field_provenance: dict[str, str]     # field -> provider that supplied it
```

`genres`/`keywords`/`spoken_languages`/`origin_countries` are tuples, not
lists: `Title` is frozen, and a `list` field is still mutable in place
(`title.genres.append(...)` would silently succeed on a "frozen" model). A
`dict` field has the same problem in principle but a frozen mapping isn't
worth the ergonomics cost — `field_provenance` stays a plain `dict[str,
str]`, which is why `Title` is deliberately the one domain model that isn't
hashable.

`field_provenance` exists so a second metadata provider can be added later
without ambiguity about which source won a given field.

🔶 **Deferred to M9:** a GIN index on `genres` for faceted `/browse`
([07](07-client-api.md)) facet counts at catalog scale. Measured at 300k
rows: a facet count seq-scans in 78.7 ms, projecting to ~3.3 s at IMDb's
full 12.7M. Not added in M1 because `CREATE INDEX CONCURRENTLY` can add it
online with no table rewrite whenever M9 lands — there is no cost to
waiting and a real cost (write overhead through M2's bulk load, and every
write after) to adding it before anything queries by facet. The same
applies to indexes on `media_items.added_at`/`last_seen_at`/`available`
and `titles.collection_id`: none exist yet, and none are needed while
`media_items` stays in the tens of thousands of rows.

### Season / Episode

Hierarchy under a series `Title`. Episodes are first-class — they carry watch
state and are what a source actually holds — but they are **not** independently
searchable in v1 ([05](05-search-and-similarity.md)).

```python
class Season(BaseModel):
    id: UUID; title_id: UUID
    season_number: int                   # 0 is valid — TMDb numbers specials season 0
    name: str | None; overview: str | None
    air_date: date | None; episode_count: int | None
    tmdb_id: int | None

class Episode(BaseModel):
    id: UUID; title_id: UUID; season_id: UUID
    season_number: int; episode_number: int
    absolute_number: int | None
    name: str | None; overview: str | None
    air_date: date | None; runtime_minutes: int | None
    tmdb_id: int | None; imdb_id: str | None
```

Both landed in M4 (`usher.domain.episode`, `seasons`/`episodes`). `seasons`
and `episodes` **CASCADE** from the series `Title` — neither carries user
state and both are re-derivable from a cached provider payload — while
`watch_states.episode_id` is **RESTRICT**, the same asymmetry
[ADR-0010](decisions/0010-watch-state-title-fk-restrict.md) pins for
`title_id`. The two compose: deleting a series cascades into its episodes,
and that cascade is refused if any watch history points at one, so a merge
that forgot to repoint history fails at the `DELETE` rather than destroying
it. `media_items.episode_id` is `SET NULL`, mirroring its `title_id`.

`episodes.imdb_id` is indexed but **not** unique, unlike `titles.imdb_id`:
a series ingested twice under different provider ids yields two episode
trees, and two trees enriched from two TMDb entries for the same show carry
the same episode IMDb ids. Nothing looks an episode up by IMDb id, so
uniqueness would buy nothing and cost a batch-aborting `IntegrityError` on
the staged-upsert path.

### Person / Credit

People are canonical entities, so "more from this director" is a join rather
than a string match.

```python
class Person(BaseModel):
    id: UUID
    tmdb_id: int | None; imdb_id: str | None
    name: str; sort_name: str
    birth_year: int | None; death_year: int | None
    known_for_department: str | None
    biography: str | None

class Credit(BaseModel):
    id: UUID
    person_id: UUID
    title_id: UUID | None
    episode_id: UUID | None              # episode-level guest credits
    kind: CreditKind                     # cast | crew
    character: str | None                # cast
    job: str | None; department: str | None   # crew
    billing_order: int | None
```

### Collection

TMDb franchise grouping ("The Matrix Collection"). Powers franchise rows and
"you own 2 of 4" completeness signals.

### Image

Artwork is referenced, never bulk-mirrored — mirroring posters for a 1.2M-title
catalog would be ~120 GB. Usher stores references and serves them through a
caching proxy that fetches, resizes, and stores on first request.

```python
class Image(BaseModel):
    id: UUID
    title_id: UUID | None; episode_id: UUID | None; person_id: UUID | None
    kind: ImageKind                      # poster | backdrop | logo | still | profile
    provider: str; remote_url: str
    width: int | None; height: int | None
    language: str | None
    is_primary: bool
```

### Source / MediaItem

The availability layer — the only place a backend server is represented.

```python
class Source(BaseModel):
    id: UUID
    kind: SourceKind                     # emby (extensible)
    name: str
    base_url: str
    credentials_ref: str                 # indirection; never the secret itself
    device_id: str                       # stable, registers us as a durable client
    enabled: bool
    supports_push: bool

class MediaItem(BaseModel):
    id: UUID
    source_id: UUID
    title_id: UUID | None                # NULL => unmatched, in review queue
    episode_id: UUID | None
    external_id: str
    container: str | None
    video_codec: str | None; audio_codec: str | None
    width: int | None; height: int | None
    hdr_format: HdrFormat | None          # HDR10 | DV | HLG
    audio_channels: int | None
    file_size_bytes: int | None
    runtime_seconds: int | None
    added_at: datetime | None
    last_seen_at: datetime
    available: bool
```

`(source_id, external_id)` is unique. A Title with several MediaItems is the
same film available in more than one place — the append-a-source mechanism.

`hdr_format` is a closed enum, not a free string: a source's own vocabulary
(Emby, for instance, emits `"DolbyVision"`) is translated into `HdrFormat`
by its adapter. No source-specific concept escapes onto this canonical
field — the same "only place a backend server is represented" rule this
section opens with, and that `CLAUDE.md` states project-wide.

**Unmatched items are never dropped.** A `MediaItem` with `title_id IS NULL`
sits in a review queue exposed over the admin API for manual resolution.

### User / WatchState

```python
class WatchState(BaseModel):
    id: UUID
    user_id: UUID
    title_id: UUID | None
    episode_id: UUID | None
    position_seconds: int
    runtime_seconds: int | None
    played: bool
    play_count: int
    last_played_at: datetime | None
    updated_at: datetime
    origin: WatchStateOrigin             # source | api
```

Exactly one of `title_id`/`episode_id` must be set — enforced by the model,
not just convention. `MediaItem.title_id` is deliberately the opposite:
permissive, because NULL there means "unmatched, in the review queue", a
legitimate and common state. An unattached `WatchState` has no equivalent
legitimate reading, so it is rejected instead.

Unique on `(user_id, title_id)` / `(user_id, episode_id)`. Attached to the
**canonical** title, not the MediaItem — so it survives a title becoming
available on a second source, or the first source going away.

Named `origin`, not `updated_by`: in nearly every schema `updated_by` is a
user FK, and this model has `user_id` sitting right next to it — the
misreading was close to guaranteed. `origin` never defaults; a write path
that forgets to set it fails instead of silently mislabeling source-pushed
state as user-originated.

### Embedding

```python
class TitleEmbedding(BaseModel):
    title_id: UUID
    model: str                           # e.g. "bge-small-en-v1.5"
    dimension: int
    vector: list[float]                  # halfvec(384) in Postgres
    source_text_hash: str                # skip re-embedding unchanged text
    created_at: datetime
```

Model name is stored so a model change is a detectable, re-embeddable event
rather than silent vector-space corruption.

### Supporting tables

| Table | Purpose |
|---|---|
| `curated_rows` | Persisted LLM row output ([06](06-rows-and-recommendations.md)) |
| `genome_scores` | MovieLens tag-genome relevance vectors, where available |
| `sync_runs` | Per-source run bookkeeping: kind, cursor, status, stats. One row per *attempt*, so the availability sweep can say which run last finished cleanly |
| `jobs` | Priority work queue ([03](03-sources-and-sync.md)). A completed job's row is deleted, so there is no `done` status and the table's size is the outstanding work, not the work ever done |
| `raw_payloads` | JSONB cache of **provider** responses, so reprocessing never refetches. Its `fetched_at` column is also what enforces TMDb's ≤6-month cache term — see [ADR-0016](decisions/0016-raw-payloads-cache-providers-not-sources.md), which is why there is no separate `provider_cache_meta` table and why source payloads are not stored here |

## Relationships

```
Collection 1─* Title
Title      1─* Season 1─* Episode
Title      *─* Person   (through Credit)
Title      1─* Image
Title      1─* MediaItem *─1 Source
Title      1─* WatchState *─1 User
Title      1─1 TitleEmbedding
```

## Rules

- **Nothing source-specific on `Title`.** If a field only makes sense for Emby,
  it belongs on `MediaItem`.
- **Deleting a Source deletes its MediaItems, never its Titles.** The catalog
  outlives the servers.
- **A Title with no MediaItems is legitimate** — that is most of the catalog
  after bootstrap, and it is what makes "recommend something you don't own yet"
  possible.
- **Soft-delete availability, hard-delete nothing.** Items that vanish from a
  source get `available = false`; history and watch state survive.
- **A `Title` cannot be deleted out from under a `WatchState`.**
  `watch_states.title_id` is `ON DELETE RESTRICT`, the deliberate opposite of
  `media_items.title_id`'s `SET NULL` two rules up — an unmatched `MediaItem`
  is worth keeping (review queue), but a `WatchState` *is* the thing worth
  keeping. Merging two Titles (the repointing operation the Identity section
  above describes) must explicitly repoint every `watch_states` row onto the
  winner before deleting the loser; `RESTRICT` makes skipping that step fail
  loudly instead of silently discarding watch history. See
  [ADR-0010](decisions/0010-watch-state-title-fk-restrict.md).
