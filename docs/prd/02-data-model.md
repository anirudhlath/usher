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
applied to indexes on `media_items.added_at`/`last_seen_at`/`available` and
`titles.collection_id`. Three of those five have since landed:
`ix_media_items_sweep` covers `last_seen_at`/`available` for the availability
sweep (M4), `titles.collection_id` got its index with the foreign key that
needs it (M7), and `ix_media_items_recently_added` covers `added_at` for the
row M7 built on it. The `genres` GIN index is still deferred, and M7 records
why the query it was measured for is not the query M7 runs — see
[06](06-rows-and-recommendations.md).

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
    tmdb_id: int | None
    name: str; sort_name: str            # sort_name NOT NULL, name verbatim
    known_for_department: str | None
    created_at: datetime; updated_at: datetime

class Credit(BaseModel):
    id: UUID
    person_id: UUID
    title_id: UUID                       # required — no episode_id, see below
    kind: CreditKind                     # cast | crew
    tmdb_credit_id: str | None           # TMDb's 24-char credit ObjectId
    character: str | None                # cast
    job: str | None; department: str | None   # crew
    billing_order: int | None
    created_at: datetime
```

✅ **Shipped in M7** (migration `fd7c3a5b9e12`), and three shapes in that
sketch are load-bearing rather than incidental.

**Identity is `(tmdb_id)`, partial-unique, and deliberately not `(tmdb_id,
kind)`.** `titles` needs the kind because TMDb's movie and series id spaces
overlap on 26,968 ids ([ADR-0011](decisions/0011-tmdb-id-is-namespaced-by-kind.md));
`/person/{id}` is one space, so a person carries one id. It is an index
`WHERE tmdb_id IS NOT NULL` rather than a column constraint, because a person
derived from a payload that carried no id must still be storable. **And it is
not `name`:** two people share a name often enough that a unique index on it
would refuse a real derivation, and Group B's contract suite proves the
identity is the id by writing two rows with the same name and different ids.

**`credits.kind` is a `cast`/`crew` discriminator and `billing_order` is a
rank, and neither is indexed.** `kind` has two values over a table that is
always read `WHERE title_id = …` or `WHERE person_id = …` first, so an index on
it would be a scan of half the table wearing an index's name. `billing_order`
is `CHECK (>= 0)` and nullable, because a crew credit has no billing.

**`credits` has no `updated_at` and no trigger**, unlike `people` and
`collections`. A credit row is derived from a cached payload and replaced
wholesale when that payload is re-derived; there is no update path for a
trigger to fire on.

⏳ **`imdb_id`, `birth_year`, `death_year` and `biography` are not built, and
they are not deferred pending a decision — they are a different milestone's
network budget.** None of them is on a `credits.cast[]`, `credits.crew[]` or
`created_by[]` entry; all four live on **`/person/{id}`**, which is one request
per person against an enriched tier of 2k–10k titles whose distinct-person
count is several times that. M7's boundary call 4 re-derives `Person` from
`raw_payloads` with **no second network call**, so shipping them would be four
columns no derivation can ever fill. Owner: **unassigned** — filling them is an
`append_to_response` namespace that does not exist plus a per-person crawl,
i.e. a metadata-provider change, named here rather than left implied.

⏳ **`episode_id` is not built, and `title_id` is `NOT NULL` in consequence.**
Measured against the recorded payload: `season.json`'s `episodes[].crew` and
`episodes[].guest_stars` are both `[]`, and no live run has ever seen either
populated. Building the nullable `title_id`/`episode_id` pair now would fix
this table's natural key, its CHECK and three consumers' semantics ("does an
episode credit count toward its series" — `list_for_title`, `PeopleProvider`'s
recurrence count, weight class B's `credit_names`) against a field that has
never carried a value, and a natural key over a nullable column does not
constrain at all, because NULL never collides with NULL. **Reversing it is four
DDL statements** — `ADD COLUMN episode_id uuid`, `ALTER COLUMN title_id DROP
NOT NULL`, add `CHECK (num_nonnulls(title_id, episode_id) = 1)` following
`ck_watch_states_exactly_one_target`'s precedent, and swap one partial unique
index for two — with no table rewrite and no re-crawl. Reversing the other
direction is a data migration with no `ON CONFLICT` target to lean on. The cost
until then: a guest star appearing in one episode of a series is invisible to
`PeopleProvider` and to weight class B, over a population that is currently
**zero**.

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

✅ **Shipped in M7** (migration `fd7c3a5b9e12`), with `tmdb_id` partial-unique
for the same reason `people` has it and **not** composite with `kind`, because
`belongs_to_collection` is a field of `/movie/{id}` and there is no series
counterpart to collide with. The migration also gave `titles.collection_id` the
foreign key it had waited for since M1 (`ON DELETE SET NULL`) and the partial
index that serves both `FranchiseProvider`'s read and the referencing-side
lookup that `SET NULL` performs on every collection delete.

**`belongs_to_collection` is movies-only and has no `/tv/{id}` counterpart** —
verified against the recorded payloads, where `series.json` carries no such key
and nothing plays its role. Three consequences:

- `FranchiseProvider` fires on **movies only**. On a television-only household
  PRD 06's firing condition ("≥ 2 owned titles in a collection") is
  unsatisfiable *by construction* rather than by absence of data — a different
  fact, and the one an operator debugging a missing row needs.
- **No series grouping is invented.** Grouping by name prefix, grouping by
  `networks`, and reading Emby's `TmdbCollection` provider-id key (real,
  observed in M4's key-space sweep, and a *movie* collection id attached to
  whatever Emby chose) each produce a populated, plausible, wrong row.
- `titles.collection_id` is NULL on every series row, permanently. A series row
  carrying a non-NULL one is a defect, and `CollectionRepository`'s contract
  suite kills it.

**No `overview` and no `parts[]`** — both are on `/collection/{id}`, the second
network call boundary call 4 refuses — and **no artwork**, which is M9's whole
table. `belongs_to_collection` itself is `{id, name, poster_path,
backdrop_path}`.

### Image

Artwork is referenced, never bulk-mirrored — mirroring posters for a 1.2M-title
catalog would be ~120 GB. Usher stores references and serves them through a
caching proxy that fetches a provider **rung** and stores the bytes on first
request — 🔴 it does not resize, which this line claimed until 2026-08-11, and
nothing in Usher's runtime can decode an image
([ADR-0032](decisions/0032-the-image-proxy-clamps-to-a-ladder.md)).

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

🔶 **The table exists as of `m09a`; the model above does not.** M9 ships
`images` with exactly these eleven fields, a SQLAlchemy row (`ImageRow`), and
**no `Image` domain model, no port and no repository** — those belong with
`GET /images/{id}`, and writing "Image landed" here before they exist would be
the stale "verified" fact worse than none.

Three things the table settles that this sketch leaves open. The three owner
columns are constrained by `ck_images_exactly_one_owner`
(`num_nonnulls(title_id, episode_id, person_id) = 1`) rather than by a
polymorphic `(owner_kind, owner_id)` pair, which could carry no foreign key at
all. All three foreign keys are **`ON DELETE CASCADE`**, and that is forced
rather than chosen: `SET NULL` would leave zero owners, which the CHECK
refuses, so the parent delete would fail naming a table the operator never
touched. And `kind`'s vocabulary is closed as the `ImageKind` enum above —
`poster | backdrop | logo | still | profile` — stored as `VARCHAR(16)`, since
this schema creates no native Postgres enum type anywhere.

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

**`added_at` is COALESCEd forward, which is not the same as immutable — and
the difference is what M7's Recently Added row is exposed to.** The shipped
upsert is `added_at = COALESCE(excluded.added_at, media_items.added_at)`. That
refuses to overwrite with **NULL**, so a delta payload that omits the field
cannot erase it. It does **not** refuse to overwrite with a *value*: if a
source reports a different `added_at`, the new one wins, every walk. Two
opposite consequences follow and both are real:

- A library re-imported into the same source keeps its dates, as long as the
  source keeps reporting the same ones. This is the behaviour the `COALESCE`
  is usually credited with.
- A library whose source reports *fresh* dates — files genuinely re-copied, so
  their creation time is new, or a source migration re-deriving them from the
  import — has **every row's `added_at` reprogrammed to the import instant**,
  and `RecentlyAddedProvider` then shows the entire library. The window and the
  row's limit cap how bad that looks; nothing prevents it.

M7 deliberately does not make the column insert-only, because the same clause
is what lets a source that initially could not report `added_at` fill it in on
a later walk. Making it immutable would fix the flood and make that
permanently unfixable, which is the worse trade against a nullable column on a
review-queue table. Recorded here so the next reader of "COALESCEd forward"
does not read it as "write-once".

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
# title_embeddings, as shipped in M6. It is a row, not a domain model:
# nothing in `usher.domain` carries a vector.
title_embeddings(
    title_id            UUID PRIMARY KEY REFERENCES titles ON DELETE CASCADE,
    embedding           halfvec(384) NULL,   -- NULL is a written refusal
    model_name          text NOT NULL,       -- "fastembed:BAAI/bge-small-en-v1.5"
    source_fingerprint  text NOT NULL,       -- md5 of the exact text embedded
    created_at, updated_at
)
```

Model name is stored so a model change is a detectable, re-embeddable event
rather than silent vector-space corruption. **M6 is where that sentence
became load-bearing**, and the shipped shape differs from the sketch this
section used to carry in three ways that each carry meaning:

- **`model_name` records the runtime as well as the checkpoint** —
  `fastembed:BAAI/bge-small-en-v1.5`, not `bge-small-en-v1.5`. The
  fastembed↔sentence-transformers vector difference for this same checkpoint
  is a max pairwise-similarity delta of 1.41e-03, **6× the halfvec
  quantisation error**, so the two runtimes are not interchangeable without a
  re-embed. Recording the runtime makes an implementation swap invalidate
  every vector through the stale predicate automatically, rather than through
  a migration somebody has to remember to write
  ([ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md)).
- **`source_fingerprint`, not `source_text_hash`** — the name the code uses.
  It is the `md5` of the exact assembled text, computed by *the same assembly
  the document composer uses*, which is what makes it quotable inside a SQL
  predicate. There is no `dimension` column: the width is the column's own
  (`halfvec(384)`), and a model that changed it would be rejected by the cast.
- **`embedding` is nullable, and that is the degenerate-document rule.** A
  refusal is a *written* outcome, not a skipped one. A `NULL` embedding with
  a current `model_name` and a real fingerprint means "composed, refused, and
  it will be re-claimed exactly once when the fingerprint changes" — a state
  distinct from "no row", which means "never claimed". Two predicates,
  deliberately: the stale one and an `embedding IS NULL` diagnostic.
  [ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md).

### `Title.search_document` — a derived column the domain does not model

`titles` carries a `search_document tsvector GENERATED ALWAYS AS (…) STORED`
that has **no `Title` field**, and this is the schema fact most likely to
bite the next person who adds a column to that table.

`PostgresTitleRepository` builds the domain model from
`TitleRow.__table__.columns`, and `Title` is `extra="forbid"` — so the row and
the model must agree 1:1, and a search document is not domain state. It is an
index artefact derived from domain state, and a `Title` carrying a `tsvector`
would put a PostgreSQL full-text type in `usher.domain`, which imports
nothing.

`TitleRow.DERIVED_COLUMNS` is the declared exception, and **membership in it
is the deliberate act**: an ordinary bookkeeping column added without being
named there still breaks every read, loudly, which is the property the 1:1
rule exists for. It has **three** collision sites, not one, and the second is
the one that gets missed:

| Site | What happens without the exclusion |
|---|---|
| the row → model mapping | `extra="forbid"` rejects an unknown key — **every read of every title raises**, everywhere. Loud and immediate. |
| **`update()`'s mutation loop** | it `setattr`s every column, so Postgres answers `ERROR: column "search_document" can only be updated to DEFAULT`. **This fires on writes**, so a change that only tested reading a seeded row will not see it. |
| the 1:1 assertion in the model tests | fails — and, spelled as `columns - DERIVED_COLUMNS == model_fields`, it *also* fails if someone adds a name to `DERIVED_COLUMNS` that `Title` does model. |

**M7 put a second name in `DERIVED_COLUMNS`, and it is a different kind of
thing from the first.** `DERIVED_COLUMNS` is now
`{"search_document", "credit_names"}`. `search_document` is
`GENERATED ALWAYS AS (…) STORED`, so **Postgres maintains it** and no code
could write it if it tried. `titles.credit_names text[]` is **maintained by
code** — by the same call in `db/repositories/people.py` that writes `credits`, inside
one transaction, holding the top ten billed plus every stored crew name — so nothing
in the database stops a caller writing it, and its membership here is what
does: `_to_domain` filters it out (so `Title` does not carry a cast list that
is not the cast) and `_NOT_UPDATABLE` — which is
`{"id", "created_at", "updated_at"} | DERIVED_COLUMNS` — keeps `update()`'s
mutation loop off it, so a `Title` round-trip cannot blank it.

Both belong here and the shared reason is the 1:1 rule, not the mechanism:
**a column is in `DERIVED_COLUMNS` when it is derived from domain state rather
than being domain state**, whoever derives it. The distinction matters the day
someone adds a third: a generated column that is not listed fails loudly on
every read, while a code-maintained one that is not listed fails on the *write*
path only — `update()`'s mutation loop would happily set it to `None`, and
`usher_array_text` is `STRICT`, so one NULL nulls the whole search document for
that row and nothing raises. That is why `credit_names` is `NOT NULL` with a
`'{}'` server default as well as being listed here: two independent guards for
a failure that is silent under either one alone.

### Supporting tables

| Table | Purpose |
|---|---|
| `curated_rows` | ✅ Persisted LLM row output ([06](06-rows-and-recommendations.md)): `(id, user_id, slug, title, reason, card_title_ids uuid[], position, model_name, generation_id, generated_at)`. **`card_title_ids` is an ordered array on the row, not a child table** — see below. `reason` is nullable, because a model that returns an empty reason should give a row with no subtitle rather than one with an empty one. **No `created_at`**: `generated_at` is one instant per *generation*, written identically onto every row of it, which is what makes `ORDER BY generated_at DESC` select a whole generation rather than a mixture — so it also carries no `server_default`. `generation_id` is what makes a replacement atomic and a partial write legible. One index, `(user_id, generated_at DESC)`, serving the read, the delete and the `users` cascade. ⚠️ **It is the first table here whose contents no re-run reproduces** — `title_neighbors` can be diffed against a fresh computation and `search_document` has a case asserting the stored value equals a freshly computed one; this has no oracle and is not deterministic. So it is *rebuildable* (one completion) and not *restorable*, which is a distinction [08](08-operations.md)'s backup section now makes explicitly. Migration `m08a` |
| `llm_calls` | ✅ The cost ledger ([10](10-telemetry-and-dashboards.md)): `(id, at, model, purpose, tokens_in, tokens_out, cost_usd, latency_ms, ok, error, generation_id)`. **No `user_id`, deliberately** — spend is attributed to an outcome by joining `curated_rows` on `generation_id`, which is what dashboard 5's "cost per curated row" *is*. `record()` is called on the failure path too, so `ok` is the discriminator and a ledger of successes alone understates spend by exactly the failures. `cost_usd` is **`NUMERIC(12, 8)`**, never a float: `$3/Mtok × 1,200 tokens` is exactly `0.0036` and at scale 4 a `$0.02/Mtok` call stores as `0.0000` — measured. `generation_id` is nullable (query expansion produces no rows) and carries no foreign key. **No index beyond the primary key**, because every reader is an M10 dashboard; the two that will be right are written into `m08a`'s docstring. ✅ **`cost_usd` verified exact end to end on 2026-08-07**: `0.00000000` against a local model, `0.01658700` with prices configured — exactly `Decimal((4359×3 + 234×15) / 1e6)` — and `SUM()` agrees to 8 decimal places. 🔴 **And it is the one table in this project rebuildable from *nothing*** — not from the catalog, not from `curated_rows` (replaced nightly), and not from any provider, since no OpenAI-compatible endpoint offers a per-key call history. It belongs in [08](08-operations.md)'s *precious* column, where M8 put it. Migration `m08a` |
| `people` | ✅ Canonical people: `(id, tmdb_id, name, sort_name, known_for_department, created_at, updated_at)`. Identity is a **partial-unique `tmdb_id`** (`WHERE tmdb_id IS NOT NULL`), never `name` — see below |
| `credits` | ✅ The `people`↔`titles` join, one row per credit: `(id, person_id, title_id, kind, tmdb_credit_id, character, job, department, billing_order, created_at)`. `kind` is the `cast`/`crew` discriminator and `billing_order` is the cast's billing rank. **No `updated_at` and no trigger** — every write is an insert, because a credit is a fact about a payload rather than a mutable row |
| `collections` | ✅ TMDb franchise grouping: `(id, tmdb_id, name, created_at, updated_at)`, `tmdb_id` partial-unique. `titles.collection_id`'s foreign-key target, at last |
| `user_taste` | ✅ One centroid per user: `(user_id PK, centroid halfvec(384), model_name, source_watermark, title_count, computed_at)`. `centroid` and `source_watermark` are both **nullable on purpose** — a household below five engaged titles gets a written refusal rather than a skipped row, and a household with no watch state at all has no watermark to record. `(model_name, source_watermark)` together are [ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md)'s fingerprint here |
| `title_neighbors` | Precomputed similarity: `(title_id, neighbor_id, score, rank, blend_fingerprint, computed_at)`. A **batch artefact**, rebuilt rather than repaired. M6 blended the two signals it had data for; M7 makes it three of the four [05](05-search-and-similarity.md) specifies and adds **`blend_fingerprint`** (migration `ffb`), so "was this row computed under the current blend?" stopped being undecidable. `computed_at` stays beside it for the half that is still undecidable per row — *some other title was embedded since* ([ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md)) |
| `genome_scores` | ✅ One title's MovieLens tag-genome vector: `(title_id, relevance halfvec(1128), genome_revision, computed_at)`. **One dense vector per title, not a tall `(title_id, tag_id, relevance)`** — see below |
| `genome_tags` | ✅ What each of that vector's 1,128 lanes means: `(tag_id PK, tag, genome_revision)`. **1,128 rows, measured against the real `genome-tags.csv`.** Loaded by the same `bootstrap --phase movielens` that writes the vectors, from a member that phase already read for its width check, and stamped with the same revision — so `GenomeRepository.vocabulary(revision)` can refuse to name one release's lanes with another's. `tag_id` is `integer` rather than `smallint` so a too-wide vocabulary is refused by `ck_genome_tags_tag_id_in_vocabulary` rather than by asyncpg's unnamed encoder — see below. **No index beyond the primary key** and **no `computed_at`**: the only read is the whole table in lane order, and its age is `import_runs`. Migration `m08b` |
| `sync_runs` | Per-source run bookkeeping: kind, cursor, status, stats. One row per *attempt*, so the availability sweep can say which run last finished cleanly |
| `jobs` | Priority work queue ([03](03-sources-and-sync.md)). A completed job's row is deleted, so there is no `done` status and the table's size is the outstanding work, not the work ever done |
| `raw_payloads` | JSONB cache of **provider** responses, so reprocessing never refetches. Its `fetched_at` column is also what enforces TMDb's ≤6-month cache term — see [ADR-0016](decisions/0016-raw-payloads-cache-providers-not-sources.md), which is why there is no separate `provider_cache_meta` table and why source payloads are not stored here |

### `genome_scores` is one dense vector per title, and the tall shape is refused with a measurement

An earlier draft of the row above implied a tall
`(title_id, tag_id, relevance)` table. Priced on a scratch
`pgvector/pgvector:pg17` (pgvector **0.8.6**) at the real dimensions,
16,376 rows:

| Form | Rows | Total size |
|---|---|---|
| `halfvec(1128)`, one row per title | 16,376 | **45 MB** (1,096 kB heap + 43 MB TOAST + 624 kB index) |
| `real[]`, one row per title | 16,376 | 88 MB |
| `(title_id, tag_id smallint, relevance real)`, PK on the pair | **18,472,128** | **2,106 MB** |

**47×**, against a database [08](08-operations.md) budgets at 8–12 GB
*total*. `real[]` sits between and is worse than both — no operator class, so
the similarity term stops being a single `<=>` and becomes arithmetic in
Python. The genome is a genuinely dense matrix (every one of 16,376 movies
carries a value for every one of 1,128 tags, verified by counting), so the
tall form stores 16,376 copies of the tag id and the title id to express a
matrix with no holes in it.

**No HNSW index, and that is a decision rather than an omission.** The access
pattern is a *pair* lookup by `title_id` rather than a KNN, and an HNSW index
cannot help a lookup by primary key at all. Measured against a real
15,565-row load: `get_pair` is **0.062 ms** (two primary-key probes under a
`BitmapOr`); an unindexed KNN over the same table — one seed against all
15,565 — is **59.4–66.2 ms** at 93,617 buffers, dominated by one TOAST fetch
per row. Nothing asks for that today; if something ever does, this reopens on
evidence. M6 separately measured a planner-*preferred* index costing 4.3× for
byte-identical recall. The 624 kB of index inside the 45 MB is the primary
key, and `tests/integration/test_genome_repository.py` asserts the index set
so a later migration cannot quietly add one.

⚠️ **An earlier draft of this section, taken from the M7 plan, said "a full
pairwise cosine over all 16,376 vectors is 1.190 ms".** That is not
achievable and is corrected here: a full pairwise scan is 121M unordered
pairs of 1,128 lanes and measures **384 s** as a self-join. 1.190 ms is about
the cost of a single pair. The decision is unchanged — it always rested on
the access pattern, not on the scan.

**The vector is TOASTed.** 1,128 halfvec lanes is 2,256 bytes plus a header,
past Postgres's ~2 kB inline threshold, so the heap holds pointers and the
TOAST relation holds 43 MB. Every read pays a TOAST fetch — invisible at
16,376 rows, and one more reason the population here is the genome's own
16,376 rather than the catalog's 1.27M.

**`genome_revision` is [ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md)'s
shape.** The tag vocabulary can change between releases, so a vector is only
comparable to another built from the same 1,128 tags in the same order — and
two vectors from different releases are type-identical, same-width, and
otherwise indistinguishable, so a half-migrated table yields cosines that are
wrong and plausible. Each row carries the archive revision that produced it,
and `GenomeRepository.get_pair` returns `None` across a mismatch. An operator
counts a mixed table with
`SELECT genome_revision, count(*) FROM genome_scores GROUP BY 1`; the fix is a
re-import. `computed_at` and no `updated_at` and no trigger, following
`title_neighbors`: this is a batch artefact.

**Absence is the absence of the row, never a zero vector**
([ADR-0014](decisions/0014-absence-is-not-zero.md)). A zero vector
is not "no information" — it is a specific vector at cosine 0.0 from every
other vector, so a title with no genome row would score as *maximally
dissimilar* from everything. At the measured coverage that is the common case,
not the edge.

**The tag vocabulary was not stored in M7, and M8 Task 19 stored it in
`genome_tags`.** M7's argument is why it did not ship a milestone earlier:
nothing in M7 reads a tag *name*, since cosine needs the two vectors and the
guarantee that their positions mean the same thing, and `genome-tags.csv` was
read by the importer to verify contiguity and width and thrown away. The cost
landed on M8 exactly where it was predicted to — **an LLM prompt that wants to
say "atmospheric, thought-provoking" needs the words** — and it was paid as
predicted: a 1,128-row table plus a loader step in a phase that already reads
the file, and one migration (`m08b`). What made that safe rather than a
deferral-by-omission is `genome_revision`, which is what the vocabulary's own
copy of that column is compared against.

### `genome_tags` refuses a release it cannot explain, and the refusal is an error rather than a `None`

`GenomeRepository.vocabulary(revision)` returns the tag names **in lane
order** — `result[i]` names `relevance[i]` — and has three outcomes, which is
one more than `get_pair` next to it:

- **`None` when the table is empty.** Every catalog bootstrapped before `m08b`
  is in that state, so it is a thing to do rather than a fault, and a caller
  renders no tags — [08](08-operations.md)'s "a degraded subsystem narrows
  functionality; it never fails a request local state can answer".
- **`PortDataMalformed` when a vocabulary is stored under a different
  release.** Here a wrong answer is *available* and plausible: 1,128 names of
  the right shape, in the right order, for a different release, rendered as
  prose. Retrying cannot help, so `JobWorker` parks it and the fix is an
  operator's `usher bootstrap --phase movielens` — the same fix a mixed
  `genome_scores` takes. The message names both releases.
- The names, otherwise.

**Why not `None` for both, as `get_pair` does?** Because `get_pair`'s two
outcomes call for the same response — "no genome signal", which 98.7% of pairs
already produce — and these two do not: one means *load the vocabulary* and
the other means *re-import the whole genome*. Collapsing them would hide a
corrupted table behind a state that is normal on every pre-`m08b` deployment.

**`tag_id` is `integer` and its ceiling is a batch precondition rather than a
column width.** `BulkCatalogRepository.replace_genome_tags` refuses a
vocabulary that is not exactly `1…n` before it writes — which is also the only
check that can see a *gap*, the failure that renames every later lane — so the
largest `tag_id` reaching the driver is the length of the sequence handed in.
Measured on `pgvector/pgvector:pg17`: under `smallint` a 32,768-element list
is refused by asyncpg's own encoder as an **unnamed** `DBAPIError` (SQLSTATE
`22000`), and under `integer` the same input is refused by
`ck_genome_tags_tag_id_in_vocabulary` as an `IntegrityError` (`23514`)
carrying the constraint's name. The write is a plain `INSERT` rather than the
staging `COPY` for the same reason: through `COPY` an out-of-range integer is
a bare `OverflowError` with no SQLSTATE at all.

### `curated_rows.card_title_ids` is an ordered array, and the missing foreign key is the price

Both shapes are already precedented in this schema: `titles.genres` is a
`text[]` on the row, and `title_neighbors` is a child table with an explicit
`rank` integer. A curated row's cards took the array, for three reasons.

**The ordering is the product.** A curated row *is* an ordering — it is the
only judgement the completion was bought for, and
[ADR-0028](decisions/0028-the-pool-is-the-contract.md) says nothing downstream
may re-sort it. A Postgres array is an ordered container, so the order is the
storage and there is no `ORDER BY` for a reader to forget. A child table makes
the order a `rank` column that every read has to sort by — and a UUIDv7
primary key makes a forgotten `ORDER BY rank` agree with `ORDER BY id` and
pass every test whose fixture inserted the cards in order. This project has
paid for that five times over, in M7's five untested provider orderings.
`title_neighbors` takes the other shape because its order is a *ranking* a
client may legitimately re-derive; this one is not.

**A shelf is one row, so a replacement is one statement per shelf.**
`replace_for_user` is delete-then-insert in one transaction. In the child
shape the same write also moves thirty to fifty card rows and makes a
partially inserted shelf representable — which is exactly the state
`CuratedRow`'s `min_length=1` exists to make unconstructible.

**The 1:1 row/model rule stays spellable.** `CuratedRow` has ten fields and
this table has ten columns, so `PostgresCuratedRowRepository` reads through
this project's usual shape (a `SELECT *` into an `extra="forbid"` model). A
child table leaves nine here and puts the tenth where `SELECT *` cannot see
it. `titles` is the only table in this schema carrying an exception list, and
it exists for generated columns.

⚠️ **The price is that this column cannot have referential integrity, and it
is a real consequence rather than a footnote.** PostgreSQL has no foreign key
over array elements, so deleting a title leaves a dangling id in every curated
row that mentioned it. Three things follow, in the order they arrive: the
stored row still validates, because the ids are all still there and the model
never claims they resolve; `LLMRow`'s hydration loses a card, which is
[ADR-0014](decisions/0014-absence-is-not-zero.md)'s shape and the same
degradation the validator already produces, with a shelf that empties entirely
dropped rather than rendered as a heading with nothing under it; and it
self-heals at the next generation, because this table holds one generation per
user and the nightly run replaces it wholesale, so the window is one day.

**The child table would not have bought integrity — it would have bought a
choice between two worse outcomes.** `ON DELETE CASCADE` on a card's
`title_id` can empty a curated row *inside the database*, producing the
heading-with-no-shelf that `min_length=1` refuses, silently and where nothing
is looking. `ON DELETE RESTRICT` makes a title undeletable because a model
mentioned it last night, for an artefact that is fully re-derivable — the
delete that can essentially never succeed, which `title_neighbors` refuses
RESTRICT for by name.

**One liability the array really does introduce is closed with a CHECK.** A
`uuid[]` admits a NULL *element*, which a child table's `NOT NULL` column
could not, and a NULL element reads back as a card that denotes nothing while
still satisfying "the array is non-empty". `array_position` is `IMMUTABLE` on
PostgreSQL 17 and does find a NULL element (both verified directly), so
`ck_curated_rows_cards_have_no_nulls` is what that `NOT NULL` would have been.

## Relationships

```
Collection 1─* Title
Title      1─* Season 1─* Episode
Title      *─* Person   (through Credit)
Title      1─* Image
Title      1─* MediaItem *─1 Source
Title      1─* WatchState *─1 User
Title      1─1 TitleEmbedding
Title      1─1 GenomeVector  (sparse — 15,565 of 1,271,570; genome_scores)
GenomeVector ·· GenomeTag    (1,128 lanes ↔ genome_tags; positional, no FK)
User       1─1 UserTaste     (nullable centroid; user_taste)
User       1─* CuratedRow    (curated_rows; replaced per generation, CASCADE)
Title      *─* Title        (through title_neighbors, directed, precomputed)
```

⚠️ **`CuratedRow *─* Title` is deliberately absent from that list**, and its
absence is the shape decision above rather than an omission. The relationship
exists — a curated row names three to eight titles, in order — but it is a
`uuid[]` column rather than a join table, so Postgres does not know about it
and will neither check nor cascade it. `llm_calls` appears on no line at all:
it references nothing, by the same argument.

🔶 **`Title 1─* Image` is a third of the way real.** `m09a` gives `Image` a
table and a SQLAlchemy row; it still has **no domain model and no port
anywhere in `src/`**, and nothing writes it. The rows are re-derived from
`raw_payloads` with no second network call ([09](09-roadmap.md)'s M4 boundary
call 2) by the M9 task that serves them.

The diagram line understates the table by one edge, deliberately: `images` can
hang off an *episode* or a *person* as well as a title (`still` and `profile`
are two of the five `ImageKind` members), which is three foreign keys and one
CHECK rather than the single parent the `1─*` notation can draw.

`Collection`, `Person` and `Credit` **landed in M7** (`fd7c3a5b9e12`), which
also gave `titles.collection_id` — a bare nullable UUID with no foreign key
since M1, which nothing in `src/` ever wrote — its foreign key to
`collections` (`ON DELETE SET NULL`) and the partial index PRD 02 had deferred
to M9 alongside `media_items`' three columns. That deferral is retracted here
with its reason: the index is the whole of `FranchiseProvider`'s read *and*
the referencing-side lookup `SET NULL` performs on every collection delete.

**The `Collection 1─* Title` line is movies-only**, and by construction rather
than by absence of data — see the `Collection` section above.

**`Title *─* Person` runs through `Credit.title_id`, which is `NOT NULL`**, so
that edge names a title and never an episode. The ⏳ under `Credit` above
carries the measurement and the four DDL statements that would reverse it.

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
