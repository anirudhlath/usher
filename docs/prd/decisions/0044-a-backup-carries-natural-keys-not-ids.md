# ADR-0044 — A backup carries natural keys, not ids, and a raw id is a check rather than a trust

**Status:** Accepted — the identity layer M10's Group K restore
([08](../08-operations.md)) resolves its references through. Extends
[ADR-0003](0003-own-uuid-identity.md)'s "our UUID is identity, a provider id
never is" into the one place where that rule bites the other way: an id this
project owns is exactly what a *second* deployment of this project does not
agree with.

**Numbered 0044 under an explicit authorisation**, per the request-not-mint
rule. `0039`, `0040` and `0041` have each been minted twice on this project
already and the branch's own records were renumbered to `0042`/`0043` on
2026-08-21 for that reason — so this number was checked against `main` and
against all three unmerged remote branches (`feat/console`,
`followup/lane-and-provenance`, `spec/quality-evals`), none of which holds a
record above `0041`. `decisions/README.md` carries the account of why "the
next free number computed against your own tree" is the mechanism that keeps
failing.

## Context

`db/repositories/bulk.py`'s `upsert_titles` mints `new_id()` for **every row
of every batch** on the way into the staging table (`:611`), and resolves the
collision with `ON CONFLICT (imdb_id) WHERE imdb_id IS NOT NULL DO UPDATE`
(`:670`). Two consequences follow, and only the first is widely known:

- *Within* one database a re-import is idempotent, and a title keeps the id it
  was first given.
- *Across* two databases built from the same `title.basics.tsv.gz`, **every
  title gets a different id**, because the `new_id()` calls are independent.
  There is no seed, no derivation from `imdb_id`, and ADR-0003 makes it so on
  purpose: *"Every entity has a Usher-owned UUIDv7 primary key … never
  identity"*.

[ADR-0043](0043-a-bounded-column-is-a-declared-type-that-refuses.md)'s
neighbour, `usher.db.backup_manifest`, decided **which tables** an artifact
carries. Five columns in the set it calls precious name a title or an episode
**by id**, and none of those ids survives a bootstrap boundary. Read off
`pg_constraint` on the live database:

- `watch_states.title_id` → `titles`, `ON DELETE RESTRICT`
  ([ADR-0010](0010-watch-state-title-fk-restrict.md)) — a wrong id fails the insert
  and the restore is refused.
- `watch_states.episode_id` → `episodes`, `RESTRICT` — the same.
- `media_items.title_id` / `.episode_id` → `titles`/`episodes`,
  `ON DELETE SET NULL` — the delete rule differs and the insert still fails.
- `search_queries.clicked_title_id` → `titles`, `SET NULL` — the same.
- `curated_rows.card_title_ids` is a **`uuid[]` with no foreign key at all** —
  **nothing fails.** The shelf renders with dead ids and the database cannot
  tell.

That last row is the one that decides the design. `m08a` took the array
deliberately and [02](../02-data-model.md) states the trade in its own heading
— *"the missing foreign key is the price"* —
`db/repositories/curation.py` explains why `unnest` of parallel arrays cannot
express a per-element reference. So `curated_rows` is the one
precious-looking table where a naive carry is **silent**.

## Decision

### 1. Restore never creates a title id; it only ever resolves one

Every carried reference travels as a natural key and is re-resolved against
the target at restore time. `usher.db.backup_identity` holds the ladder, the
builder and the refusal; `TitleReference` and `EpisodeReference` are the
shapes, and they live in `usher.ports.repository._references` because
`pyproject.toml`'s third import contract (*"db is driven, not driving"*)
forbids a port importing `usher.db` and `resolve_natural_keys` has to be typed
in terms of something. `BrowseCursorPosition` and `EpisodeCursorPosition` are
the precedent.

- **A title** is carried as `imdb_id`, falling back to `(kind, tmdb_id)` —
  [ADR-0011](0011-tmdb-id-is-namespaced-by-kind.md)'s namespacing is why the
  kind is part of it — falling back to its own UUID.
- **An episode** is carried as its title's key plus `(season_number,
  episode_number)`. `uq_episodes_title_season_episode` is a real unique
  constraint, so this is an identity and not a heuristic.
- **A user** is carried as `name` (`uq_users_name`).
- **A source** is carried by its own UUID, and that is correct rather than an
  exception: a source id is minted when an operator adds the source and travels
  *inside* the backup together with the `sources` row it names, so
  `source_credentials.source_id`, `media_items.source_id` and
  `sync_runs.source_id` are internally consistent within one artifact.

### 2. The one place a raw id is accepted, and it is a check rather than a trust

For a title the artifact carries the raw UUID and restore accepts it **if and
only if the target already holds a title with that exact id**. That is the
same-database case — disaster recovery into the database the backup came from,
which is the ordinary path — expressed as a lookup rather than as a mode. It
unifies the two cases: there is one code path, and *"restore into the same
database"* is simply the case where every lookup resolves.

### 3. An unresolved reference is a named refusal, never `None`

`Unresolved` carries the reference and the rungs that were tried. Four of the
five columns above are foreign keys, so a `None` written into one is a driver
exception an operator has to decode rather than the sentence *"this watch
state's title is not in the target"*.

What restore then does differs **per table**, on purpose, and
`backup_identity.UNRESOLVED_RULE` records it so K4 reads one field rather than
re-deriving the argument at each call site:

| table | unresolved reference | why |
|---|---|---|
| `watch_states` | refused, named and counted | a real loss the operator must see, and recoverable: enrich the title, restore again |
| `media_items` | refused for that row, counted | the same; the link is the operator's judgement |
| `search_queries.clicked_title_id` | set `NULL`, counted | the FK is already `SET NULL`, so `NULL` is a state every reader handles, and the analytic value is the query text and the outcome |

### 4. `curated_rows` is never carried by backup or restore

Both documents that already say so are cited rather than re-argued: PRD 08
puts it in the rebuildable column and [02](../02-data-model.md) calls it
*"rebuildable (one completion) and not restorable"*. `backup_manifest`'s own
entry is where the classification lives; this record is only the reason the
array made the choice unavoidable.

## Consequences

- **The identity layer is a port method, so it has two arms and one contract
  suite.** `TitleRepository.resolve_natural_keys` and
  `EpisodeRepository.resolve_natural_keys` are driven by
  `TitleRepositoryNaturalKeyContract` and
  `EpisodeRepositoryNaturalKeyContract`, which run against the fakes with no
  Docker and against real Postgres.
- **The Postgres arm is one statement per call.** A `VALUES` list — spelled
  `unnest(...) WITH ORDINALITY`, because two rungs of the probe are nullable
  and a join back on a nullable probe answers NULL rather than false — joined
  three ways against `titles`, with `COALESCE` making the ladder's precedence a
  property of the statement. The alternative spelling is a loop that is
  invisible to every behavioural assertion and quadratic on a real household,
  so it is pinned by a statement-count case rather than by a comment.
- **The resolver is case- and namespace-exact.** `tt99000011` and `TT99000011`
  are different keys; a `tmdb_id` without a `kind` is refused by the type
  rather than defaulted, so ADR-0011's overlap cannot be re-introduced by a
  caller.
- **An episode carries no raw id of its own.** A title with neither provider id
  is a first-class citizen and has nothing else to be named by; an episode
  always has both numbers, so its natural key is total and the same-database
  case is already covered by the series' own raw-id rung.

### Rejected: a fast path that carries ids when a catalog fingerprint matches

A hash over `(imdb_id, id)` pairs would tell the two cases apart cheaply, and
it buys nothing measurable — the precious set is small by construction
(PRD 08's *"one row per generation per household per night"*; 8 non-empty
precious rows on this deployment) — while adding a second code path that is
exercised only in the case the drill does not cover. One path, always resolve.

### Rejected: remapping `curated_rows.card_title_ids` through the same resolver

It would work mechanically and it would be restoring a *rendering* rather than
a *judgement*: [ADR-0028](0028-the-pool-is-the-contract.md) says nothing
downstream may re-sort a curated row, a card whose title did not resolve would
have to be dropped from the middle of an ordering the completion was bought
for, and the result is a shelf the model never produced. One completion
regenerates it.

## Evidence

**Coverage, measured on `usher-postgres-1` (the deployment this project
measures) on 2026-08-21**, and re-measured rather than quoted because the
figures this design was drafted against on 2026-08-13 have moved:

| | 2026-08-13 | **2026-08-21** |
|---|---|---|
| titles | 1,272,401 | **1,272,888** |
| no `imdb_id` | 13 | **72** |
| no `tmdb_id` | 980,168 | **980,176** |
| **neither** | **0** | **6** |

🔴 **The claim the design was drafted with — *"the two keys together cover 100%
of this catalog"* — is false at the second reading.** Six real titles now carry
neither provider id. That does not change the design, because the raw-id rung
was already required by ADR-0003's *"a title with no provider id is a
first-class citizen"*; it **strengthens** it. The rung is exercised by real
rows rather than being defensive, and the drift — 13 → 72 with no provider id
at all going 0 → 6 in eight days — is the measurement saying the population
moves. A design that had rested on the 100% figure would have been correct for
a week.

**`uq_users_name` is declared on the ORM as well as in the migration, contrary
to what this task's brief recorded.** `a8a0e10ff464_core_schema.py:209` emits
`sa.UniqueConstraint("name", name=op.f("uq_users_name"))`, and
`db/models/watch.py`'s `UserRow.name` is `unique=True` — which
`usher.db.base.NAMING_CONVENTION`'s `uq_%(table_name)s_%(column_0_N_name)s`
renders as exactly `uq_users_name`. Verified by introspecting
`UserRow.__table__.constraints`, which reports
`UniqueConstraint('uq_users_name', ['name'])`. So the user's natural key is a
constraint both halves of the schema declare, and
`test_migration_matches_the_orm_metadata` is what would have caught it if it
were not.

**`ix_titles_imdb_id` and `ix_titles_tmdb_id_kind` are both partial** — unique
only `WHERE <column> IS NOT NULL` — which is why many rows may share a null
provider id and why the ladder's first two rungs skip a null probe rather than
matching one. Both are index probes in the shipped statement, asserted under
`SET LOCAL enable_seqscan = off` so the case separates *not chosen* from *not
choosable*.

**Two catalogs built from one dump share no title id**, asserted directly
rather than argued: `test_two_catalogs_built_from_one_dump_share_no_title_id`
loads the same `ImdbTitle` sequence into two independent repositories and
requires the two id sets to be disjoint while both resolve to the same natural
key. It is a fact about `new_id()` this record documents rather than changes.
