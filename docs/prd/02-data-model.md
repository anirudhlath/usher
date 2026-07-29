# 02 — Canonical data model

## Identity

**Every entity has a Usher-owned UUIDv7 primary key.** Provider identifiers
(`tmdb_id`, `imdb_id`, `tvdb_id`) are nullable, unique-indexed *attributes*.

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
| `failed` | Enrichment attempted and failed. Carries `enrichment_error`. |

Every API response exposes this so clients render deliberately rather than
guessing from null fields.

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

    genres: list[str]
    keywords: list[str]
    original_language: str | None
    spoken_languages: list[str]
    origin_countries: list[str]
    content_rating: str | None

    community_rating: float | None       # provider aggregate
    vote_count: int | None
    popularity: float | None

    collection_id: UUID | None
    enrichment_state: EnrichmentState
    enriched_at: datetime | None
    field_provenance: dict[str, str]     # field -> provider that supplied it
```

`field_provenance` exists so a second metadata provider can be added later
without ambiguity about which source won a given field.

### Season / Episode

Hierarchy under a series `Title`. Episodes are first-class — they carry watch
state and are what a source actually holds — but they are **not** independently
searchable in v1 ([05](05-search-and-similarity.md)).

```python
class Season(BaseModel):
    id: UUID; title_id: UUID
    season_number: int
    name: str | None; overview: str | None
    air_date: date | None; episode_count: int | None

class Episode(BaseModel):
    id: UUID; title_id: UUID; season_id: UUID
    season_number: int; episode_number: int
    absolute_number: int | None
    name: str | None; overview: str | None
    air_date: date | None; runtime_minutes: int | None
    tmdb_id: int | None; imdb_id: str | None
```

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
    hdr_format: str | None               # HDR10 | DV | HLG
    audio_channels: int | None
    file_size_bytes: int | None
    runtime_seconds: int | None
    added_at: datetime | None
    last_seen_at: datetime
    available: bool
```

`(source_id, external_id)` is unique. A Title with several MediaItems is the
same film available in more than one place — the append-a-source mechanism.

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
    updated_by: WatchStateOrigin         # source | api
```

Unique on `(user_id, title_id)` / `(user_id, episode_id)`. Attached to the
**canonical** title, not the MediaItem — so it survives a title becoming
available on a second source, or the first source going away.

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
| `sync_runs` | Per-source run bookkeeping: kind, cursor, status, stats |
| `jobs` | Priority work queue ([03](03-sources-and-sync.md)) |
| `raw_payloads` | JSONB cache of provider responses, so reprocessing never refetches |
| `provider_cache_meta` | Fetch timestamps — enforces TMDb's ≤6-month cache term |

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
