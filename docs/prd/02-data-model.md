# 02 — Canonical data model

## Identity

**Every entity has a Usher-owned UUIDv7 primary key.** Provider identifiers
(`tmdb_id`, `imdb_id`, `tvdb_id`) are nullable, unique-indexed *attributes*.

**`tmdb_id` is unique per `kind`, not globally**: a movie and a series can
carry the same one. The unique index is `(tmdb_id, kind)` and
`TitleRepository.get_by_tmdb_id` takes a `TitleKind` alongside the id. Any API
surface that exposes a `tmdb_id` exposes its `kind` beside it. `imdb_id` and
`tvdb_id` are single-column unique.

## Enrichment tiers

Every `Title` carries an explicit state; the catalog is usable before it is
complete ([03](03-sources-and-sync.md)):

| State | Meaning |
|---|---|
| `skeleton` | From a bulk dataset. Name, year, runtime, genres, ratings. No overview or artwork. |
| `stub` | Seen on a source but not yet enriched. Source's own metadata only. |
| `enriched` | Full provider metadata present. `enriched_at` set. |

Every API response exposes this state.

Whether the *last enrichment attempt* failed is tracked separately on
`Title.enrichment_error: str | None`; failure does not consume or reset a rung
on the ladder.

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

    tmdb_vote_average: float | None      # TMDb vote_average, 0-10
    tmdb_vote_count: int | None          # TMDb vote_count
    tmdb_popularity: float | None        # TMDb popularity
    imdb_average_rating: float | None    # IMDb averageRating, 0-10
    imdb_num_votes: int | None           # IMDb numVotes

    collection_id: UUID | None
    enrichment_state: EnrichmentState
    enrichment_error: str | None         # non-null => last enrichment attempt failed
    enriched_at: datetime | None
    field_provenance: dict[str, str]     # field -> provider that supplied it
```

**Each rating column names its source.** IMDb's own numbers are restored from
source by `usher bootstrap --phase ratings` ([04](04-catalog-bootstrap.md)),
never inferred by a migration data step.

**`genres` holds two importers' vocabularies**, so one concept can be spelled
twice (`Sci-Fi` and `Science Fiction`). `usher.domain.genres` holds Usher's own
31-concept vocabulary and it is applied at both ends: the readers map into it
(`/browse`'s filter and facets) and `usher genres --backfill` rewrites the
column through `canonicalise_genres`. A label naming a concept the provider has
no word for (`Biography`, `Film-Noir`, `Game-Show`, `Musical`, `Short`,
`Sport`, `Adult`) survives enrichment rather than being deleted.

`field_provenance` records which provider supplied each field. It is a `titles`
column and arbitrates nothing else: `people` and `credits` use
`credits.source` plus `CREDIT_SOURCE_PRECEDENCE`, per title and wholesale.

**`/browse` facets are opt-in and predicated** — `?facets=true` plus at least
one of `genre`/`year`/`owned` — with a `computed` flag and *absent* rather than
empty maps.

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

`seasons` and `episodes` **CASCADE** from the series `Title`;
`watch_states.episode_id` is **RESTRICT**, so deleting a series is refused if
any watch history points at one of its episodes. `media_items.episode_id` is
`SET NULL`. `episodes.imdb_id` is indexed but not unique.

### Person / Credit

People are canonical entities.

```python
class Person(BaseModel):
    id: UUID
    tmdb_id: int | None                  # TMDb's person id
    imdb_id: str | None                  # IMDb's `nconst`
    name: str; sort_name: str            # sort_name NOT NULL, name verbatim
    known_for_department: str | None
    created_at: datetime; updated_at: datetime

class Credit(BaseModel):
    id: UUID
    person_id: UUID
    title_id: UUID                       # required — no episode_id
    kind: CreditKind                     # cast | crew
    source: CreditSource                 # tmdb | imdb — required, never defaulted
    tmdb_credit_id: str | None           # TMDb's 24-char credit ObjectId
    character: str | None                # cast
    job: str | None; department: str | None   # crew
    billing_order: int | None            # the provider's own ordering
    created_at: datetime
```

Identity is a partial-unique `tmdb_id` (`WHERE tmdb_id IS NOT NULL`), never
`name`. `billing_order` is `CHECK (>= 0)` and null on crew credits. A credit
row is replaced wholesale when its payload is re-derived.

**The schema lets credits from two sources coexist**, replaced per
`(title_id, source)`, with TMDb taking precedence over IMDb. `source` is
required and never defaulted. Today only TMDb credits are written: no `people`
or `credits` row is bulk-loaded from IMDb ([03](03-sources-and-sync.md)).

A human working under both sources would be two `Person` rows, one with
`tmdb_id` and one with `imdb_id`; both id columns are nullable and each is
partially unique.

`GET /titles/{id}` renders `person_id`, `name`, `character` and `job`
([07](07-client-api.md)): `cast` and `crew` as separate keys, each capped at 20
and absent when empty. `billing_order`, `department` and `tmdb_credit_id` are
stored and not rendered.

**A title can carry `titles.credit_names` and no `credits` rows at all** — the
IMDb principals loader fills that column without deriving people — and
`GET /titles/{id}` answers such a title with neither `cast` nor `crew`.

### Collection

TMDb franchise grouping ("The Matrix Collection"). Powers franchise rows and
"you own 2 of 4" completeness signals.

```python
class Collection(BaseModel):
    id: UUID
    tmdb_id: int | None
    name: str
    created_at: datetime; updated_at: datetime
```

`tmdb_id` is partial-unique. `titles.collection_id` is a foreign key with
`ON DELETE SET NULL`.

**Collections are movies-only.** `FranchiseProvider` fires on movies only, no
series grouping is invented, and `titles.collection_id` is NULL on every series
row. There is no `overview`, no `parts[]` and no collection artwork.

### Image

Artwork is referenced, never bulk-mirrored. Usher stores references and serves
them through a caching proxy that fetches a provider **rung** and stores the
bytes on first request; it does not resize.

```python
class Image(BaseModel):
    id: UUID
    title_id: UUID | None; episode_id: UUID | None; person_id: UUID | None
    kind: ImageKind                      # poster | backdrop | logo | still | profile
    provider: str; provider_path: str
    width: int | None; height: int | None
    language: str | None
    is_primary: bool
```

`DeriveService` fills the table from `raw_payloads` with no second network call
([03](03-sources-and-sync.md) stage 5).

**An image id survives a re-derivation.** A second `usher derive` returns the
id the row was first inserted with (the write is an upsert on
`(title_id, episode_id, person_id, provider, provider_path)`), so a client's
cached artwork stays valid.

Exactly one owner is set (`ck_images_exactly_one_owner`); all three foreign
keys are `ON DELETE CASCADE`. `kind`'s vocabulary is closed as `ImageKind`. The
read order is `(is_primary DESC, id)`; there is no `sort_order`.

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
same film available in more than one place.

`hdr_format` is a closed enum; a source's own vocabulary (Emby emits
`"DolbyVision"`) is translated by its adapter.

**Unmatched items are never dropped.** A `MediaItem` with `title_id IS NULL`
sits in a review queue: `GET /admin/unmatched` reads it,
`POST /admin/unmatched/{id}/resolve` writes it ([07](07-client-api.md)), and
`usher unmatched` does the same from the CLI. The queue pages by cursor.
`POST /admin/unmatched/{id}/resolve` refuses an episode belonging to a
different series.

**`added_at` carries forward**: a delta payload that omits the field cannot
erase it, but a source reporting a *different* date overwrites it. A source
that re-derives its dates therefore reprograms every row's `added_at` and
`RecentlyAddedProvider` shows the whole library until the window passes. A
source that initially could not report `added_at` can fill it in on a later
walk.

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

Exactly one of `title_id`/`episode_id` must be set, enforced by the model.
Unique on `(user_id, title_id)` / `(user_id, episode_id)`. Watch state attaches
to the **canonical** title, so it survives a title becoming available on a
second source or the first source going away.

`origin` is required and never defaulted.

### Embedding

```python
# title_embeddings. It is a row, not a domain model:
# nothing in `usher.domain` carries a vector.
title_embeddings(
    title_id            UUID PRIMARY KEY REFERENCES titles ON DELETE CASCADE,
    embedding           halfvec(1024) NULL,  -- NULL is a written refusal
    model_name          text NOT NULL,       -- "openai:BAAI/bge-m3"
    source_fingerprint  text NOT NULL,       -- md5 of the exact text embedded
    created_at, updated_at
)
```

- **`model_name` records the runtime as well as the checkpoint** —
  `openai:BAAI/bge-m3`, not `bge-m3` — so an implementation swap marks every
  vector stale. The prefix selects the `Embedder`: `fastembed:` or `openai:`.
- **`source_fingerprint`** is the `md5` of the exact assembled text. The vector
  width is the column's own type, shared with `user_taste.centroid`; changing
  it is a migration, and a startup check reports a model/width mismatch before
  any job runs.
- **`embedding` is nullable.** A NULL embedding with a current `model_name` and
  a real fingerprint means "composed, refused, re-claimed once the fingerprint
  changes", which is distinct from "no row" (never claimed).

### Derived search columns

`titles` carries two columns that no `Title` field models:
`search_document`, a generated `tsvector` maintained by Postgres, and
`credit_names text[]`, written in the same transaction as `credits` and holding
the top ten billed plus every stored crew name.

### Supporting tables

| Table | Purpose |
|---|---|
| `curated_rows` | Persisted LLM row output ([06](06-rows-and-recommendations.md)): `(id, user_id, slug, title, reason, card_title_ids uuid[], position, model_name, generation_id, generated_at)`. `reason` is nullable. `generated_at` is one instant per *generation*, and a generation replaces the previous one atomically. **Rebuildable, not restorable** — one completion regenerates it and no re-run reproduces it ([08](08-operations.md)) |
| `llm_calls` | The cost ledger ([10](10-telemetry-and-dashboards.md)): `(id, at, model, purpose, tokens_in, tokens_out, cost_usd, latency_ms, ok, error, generation_id)`. No `user_id`: spend is attributed by joining `curated_rows` on `generation_id`. Failed calls are recorded too, with `ok` false. `cost_usd` is `NUMERIC(12, 8)`. Its only reader is Grafana. **Precious**: nothing regenerates it |
| `people` | Canonical people: `(id, tmdb_id, imdb_id, name, sort_name, known_for_department, created_at, updated_at)` |
| `credits` | The `people`↔`titles` join, one row per credit |
| `collections` | TMDb franchise grouping: `(id, tmdb_id, name, created_at, updated_at)` |
| `user_taste` | One centroid per user: `(user_id PK, centroid halfvec(1024), model_name, source_watermark, title_count, computed_at)`. `centroid` and `source_watermark` are both nullable: a household below five engaged titles gets a written refusal. `(model_name, source_watermark)` is its fingerprint, and the width moves with `title_embeddings.embedding` |
| `title_neighbors` | Precomputed similarity: `(title_id, neighbor_id, score, rank, blend_fingerprint, computed_at)`. A batch artefact, rebuilt rather than repaired. `blend_fingerprint` hashes the blend's constants and **not** the embedding model, so a model swap needs the table emptied |
| `genome_scores` | One title's MovieLens tag-genome vector: `(title_id, relevance halfvec(1128), genome_revision, computed_at)`. One dense vector per title |
| `genome_tags` | What each of that vector's 1,128 lanes means: `(tag_id PK, tag, genome_revision)`, loaded by `bootstrap --phase movielens` and stamped with the same revision |
| `sync_runs` | Per-source run bookkeeping: kind, cursor, status, stats, one row per *attempt*. `error` is prose and `error_code` is the kind — a nullable `VARCHAR(32)` holding `availability_ceiling` or `gap_delta_ceiling`, null for every failure an operator has no command for |
| `jobs` | Priority work queue ([03](03-sources-and-sync.md)). A completed job's row is deleted, so the table's size is the outstanding work |
| `raw_payloads` | JSONB cache of **provider** responses, so reprocessing never refetches. `fetched_at` enforces TMDb's ≤ 6-month cache term. Source payloads are not stored here |

**The genome is one dense vector per title.** A title without one has no row,
never a zero vector. Two vectors are compared only when they carry the same
`genome_revision`; across a mismatch the pair has no genome score. A mixed table
is counted with
`SELECT genome_revision, count(*) FROM genome_scores GROUP BY 1`; the fix is a
re-import.

**The genome tag vocabulary is read in lane order** (name `i` labels
`relevance[i]`), with three outcomes:

- **Empty table:** no tags are rendered.
- **The stored vocabulary is another release's:** the job fails with
  `PortDataMalformed` naming both releases and is parked, not retried; the fix
  is `usher bootstrap --phase movielens`.
- The names, otherwise.

Loading a vocabulary whose tag ids are not exactly `1…n` is refused before
anything is written.

**`curated_rows.card_title_ids` is an ordered array**, and nothing downstream
re-sorts it. Deleting a title leaves a dangling id: the stored row still
validates, that card is dropped when the row is rendered, a shelf that empties
entirely is dropped rather than rendered as a heading with nothing under it,
and the next generation replaces the table wholesale. A NULL element is
refused. A backup never carries this table.

## Relationships

```
Collection 1─* Title
Title      1─* Season 1─* Episode
Title      *─* Person   (through Credit)
Title      1─* Image
Title      1─* MediaItem *─1 Source
Title      1─* WatchState *─1 User
Title      1─1 TitleEmbedding
Title      1─1 GenomeVector  (sparse; genome_scores)
GenomeVector ·· GenomeTag    (1,128 lanes ↔ genome_tags; positional, no FK)
User       1─1 UserTaste     (nullable centroid; user_taste)
User       1─* CuratedRow    (curated_rows; replaced per generation, CASCADE)
Title      *─* Title        (through title_neighbors, directed, precomputed)
```

`CuratedRow *─* Title` is absent: it is a `uuid[]` column, so Postgres neither
checks nor cascades it. `llm_calls` references nothing.

`images` can hang off an *episode* or a *person* as well as a title; only
title images are written and read today.

**The `Collection 1─* Title` line is movies-only.** **`Title *─* Person` runs
through `Credit.title_id`, which is `NOT NULL`**, so that edge names a title
and never an episode.

## Rules

- **Nothing source-specific on `Title`.** If a field only makes sense for Emby,
  it belongs on `MediaItem`.
- **Deleting a Source deletes its MediaItems, never its Titles.** The catalog
  outlives the servers.
- **A Title with no MediaItems is legitimate** — that is most of the catalog
  after bootstrap, and Usher can recommend titles you don't own yet.
- **Soft-delete availability, hard-delete nothing.** Items that vanish from a
  source get `available = false`; history and watch state survive.
- **A `Title` cannot be deleted out from under a `WatchState`.**
  `watch_states.title_id` is `ON DELETE RESTRICT` (`media_items.title_id` is
  `SET NULL`). Merging two Titles must repoint every `watch_states` row onto the
  winner before deleting the loser.
