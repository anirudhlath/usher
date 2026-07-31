# CLAUDE.md

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL.

**Status: M3 Emby adapter complete.** The project scaffold, environment
config, domain models, port ABCs, persistence (SQLAlchemy schema + Alembic
migrations + title repository), the telemetry bootstrap, a FastAPI app
with liveness/readiness endpoints, the container image + compose stack + CI
(M1), the bulk-dataset bootstrap pipeline — IMDb skeleton, TMDb ID export,
Wikidata crosswalk, all resumable and checkpointed (M2) — and the Emby
`SourceAdapter` with encrypted source credentials, a source repository, the
admin source routes, and a source-agnostic contract suite that runs against
both a pure in-memory adapter and the real one (M3) — all exist and are
verified working, M3 including against a live Emby 4.9.5.0 server. See
`docs/plans/2026-07-28-m1-foundation.md`,
`docs/plans/2026-07-30-m2-bootstrap.md` and
`docs/plans/2026-07-30-m3-emby-adapter.md` for the task breakdowns and
`docs/prd/09-roadmap.md` for what's next (M4). Do not invent commands for
tooling that does not exist yet — check the Commands section below before
assuming something runs.

## Keep the PRD current

`docs/prd/` is the authoritative, living description of what Usher is and why.
Code that contradicts it is a bug in one of them — resolve it, never let it
drift silently.

**Update the PRD in the same commit as the change that invalidates it.** Not in
a follow-up, not "later". A change that alters behaviour and leaves the PRD
stale is incomplete.

Start at `docs/prd/README.md` for the index. Detailed maintenance conventions
load automatically when working in `docs/`.

## Conventions that will bite you

- **Ports are `abc.ABC`, not `typing.Protocol`.** Deliberate — see
  [ADR-0001](docs/prd/decisions/0001-abc-over-protocol.md). Do not "modernise"
  them to Protocols.
- **Layering is enforced, not advisory.** `domain/` imports nothing from
  `adapters/`, `db/`, or `api/`; `services/` depends only on `domain/` and
  `ports/`. CI checks this with `import-linter`.
- **No source-specific concept escapes its adapter.** If something only makes
  sense for Emby, it belongs in `adapters/emby/` or on `MediaItem` — never on
  `Title`, never in an API response.
- **Identity is our UUIDv7.** `tmdb_id`/`imdb_id` are indexed attributes, never
  primary keys, never identifiers in an API contract.
- **Domain models are frozen — use `.evolve()`, never `model_copy(update=)`.**
  Every `usher.domain` model inherits `DomainModel`
  (`src/usher/domain/base.py`), so `model_copy(update=...)` is reachable on
  all of them but skips validation entirely: it can hand back an instance
  with a wrong-typed or out-of-range field that pydantic still serializes
  without complaint. `.evolve(**changes)` re-validates from scratch and is
  the only sanctioned write path.
- **Ship importers, never data.** No third-party metadata may be committed or
  included in a release artifact — IMDb and TMDb both prohibit redistribution.
  Users run importers and hold their own API keys. Attribution strings stay in
  the API surface.
- **Use `uv`** for all Python work: `uv sync`, `uv run <cmd>`, `uv add <pkg>`.
  Never pip/conda, never activate a venv.
- **TDD.** Failing test first, then implementation.
- **Secrets in `Settings` are `pydantic.SecretStr`**, never plain `str` —
  `database_url`, `secret_key`, `tmdb_api_key`. Unwrap with
  `.get_secret_value()` only at the point of use (e.g. handing a DSN to
  `create_async_engine`); never store the unwrapped value in a variable that
  outlives that call, and never let it reach a log line or an exception
  message. This is how `docs/prd/08-operations.md`'s "credentials are never
  logged" rule is enforced rather than merely asserted.

## Verified facts worth not re-deriving

**Emby push works.** Verified 2026-07-29 against the live server with a normal
non-admin token: `/embywebsocket` upgrades (101), delivers periodic `Sessions`,
and pushes `UserDataChanged` within seconds of an out-of-band state change. Two
earlier negative findings were both wrong — see
[ADR-0004](docs/prd/decisions/0004-push-over-polling.md).

Health-check caveat: a handshake against *any* path succeeds, so a successful
upgrade is not a health signal. Assert on received messages instead.

**M3's live verification found the write-back route was simply wrong, and
three other things worth not re-deriving.** Run 2026-07-31 against the live
Emby **4.9.5.0** server, driving the real `EmbyAdapter`/`EmbySession` with
`_authenticate_locked` swapped for one that installs a known token. Full
route-by-route table in the M3 plan's "Which Emby routes are guessed"
section.

- **`POST /Users/{user}/PlayingItems/{item}/Progress` answers 400** —
  `"Value cannot be null. (Parameter 'key')"` — bodyless, with an empty JSON
  body, with an `{ItemId, PositionTicks}` body, and with `MediaSourceId` and
  `IsPaused` added. So does `POST /Sessions/Playing/Progress`. Both are
  *session-scoped playback reporting*, keyed off a play session Usher never
  has. **Use `POST /Users/{user}/Items/{item}/UserData`** with a JSON body;
  it answers 204. `FakeEmbyServer` could not have caught this: it
  implemented the adapter's own guess, so 40 contract assertions passed
  against a write-back that had never worked once. This is the whole
  argument for a live run in one bug.
- **That `UserData` body must name `Played` even when it is not changing.**
  It deserialises into a DTO whose unset fields take their defaults, so a
  body carrying only `PlaybackPositionTicks` flips a played item to
  unplayed. `PlayCount` and `LastPlayedDate` survive the same omission.
- **`DELETE /Users/{user}/PlayedItems/{item}` is destructive beyond its
  name:** it resets `PlayCount` to 0, clears `LastPlayedDate`, *and* clears
  a non-zero resume position. Never use it to report an item unplayed while
  writing a position. `POST` to the same route *is* how you mark played —
  it advances `PlayCount` (to 1, idempotently, not `+1`), stamps
  `LastPlayedDate`, and clears the resume position. That last part is PRD
  03's load-bearing "position first, played last" ordering, verified for the
  first time.
- **`/Videos/{id}/stream` does not need `DeviceId`.** Measured one parameter
  at a time with a `Range` header: as built → 206 with real bytes; without
  `DeviceId` → still 206; without `api_key` → 401; without `static` → 400.
  The parameter is no longer sent (ADR-0012).

**A listing's `UserData` is not the same as an item's.** Verified: a
`GET /Users/{user}/Items` listing reports `PlayCount: 0` and omits
`LastPlayedDate` entirely, for the very item whose
`GET /Users/{user}/Items/{item}` reports `PlayCount: 2` and a real
`LastPlayedDate`. `PlaybackPositionTicks` and `Played` are correct in both.
Neither `Fields=UserDataPlayState`, `Fields=UserData`,
`EnableUserData=true`, nor restricting the listing to explicit `Ids`
changes it. So `watch_state()` — which walks listings — cannot carry play
history, and M4 must not write `play_count`/`last_played_at` from a walk or
it writes 0 over real history. Recovering them is one request per item
against 1,126,674 items. Making both fields optional on `SourceWatchState`
is the honest fix; it is a port change and is deliberately left to M4.

**Emby 4.9.5.0 emits neither `VideoRangeType` nor `DvProfile`.** Not once
across every video stream of 200 movies (the newest 100 4K and 100 HD of
94,438), including all 34 Dolby Vision files. What it emits is `VideoRange`
∈ {`SDR`, `DolbyVision`, `HDR 10`} — with a space — plus
`ExtendedVideoType`/`ExtendedVideoSubType` ∈ {`None`/`None`,
`Hdr10`/`Hdr10`, `DolbyVision`/`DoviProfile81`|`DoviProfile50`}. The
`Extended*` pair carries the **literal string `"None"`**, not JSON null, so
it is always truthy and any check on it must be a token lookup that falls
through. The `DOVIWith*` family the mapper also handles is Jellyfin's
vocabulary, not this server's; both are kept, since reading a field a server
omits costs nothing.

**Emby honours a secondary sort key, so `SortBy=DateCreated,SortName` is a
real request.** Shown on a tie-heavy primary key rather than hoped for:
`ProductionYear,SortName` returns the tied block in `SortName` order,
`ProductionYear` alone returns it in a different, insertion-shaped one. Tie
*instability* was **not** reproducible here — repeated pages came back
identical and overlapping `StartIndex` windows agreed exactly, with and
without the tiebreak — so the second key is a cheap guarantee rather than a
demonstrated-necessary fix. `MinDateLastSaved` and `MinDateLastSavedForUser`
are both honoured and are genuinely different filters (28,934 vs 29,005
items over the same 30-day window). An *invented* parameter name is ignored
outright and returns the full unfiltered count, which is the "degrades to a
full walk" safety property, measured.

**The library is 1,126,674 items, not 94,395.** 94,438 movies, 32,409
series, 999,827 episodes. The movie figure the adapter was designed around
was one third of the walk. At the default page size that is 5,634 pages —
**56% of `MAX_PAGES`**, so the headroom is 1.8x, not the ~21x the constant's
comment claimed.

**A token presented with a different `DeviceId` neither forks nor
invalidates its session.** `GET /Sessions` was byte-identical before and
after, and the token still worked. Emby binds a session to the token's own
authentication record, made at `AuthenticateByName` time; the header's
`DeviceId` on later requests does not register a device. So "one durable
device" comes from authenticating once with a stable id, not from repeating
it.

**Not verified, and the docs say so rather than implying coverage:** `POST
/Users/AuthenticateByName` itself (that run held a token, not a password —
it is verified separately by ADR-0004's session), silent re-authentication
on a 401 end to end, durable-device registration across restarts, and
`multi_version_movie.json`'s shape — no item with more than one
`MediaSource` exists in the newest 800 movies of this deployment, so
`primary_media_source`'s selection rule has never met a real multi-version
payload.

**`Policy.IsAdministrator` is readable**, on `GET /Users/{userId}`, with the
user's own non-admin token — a 45-key `Policy` object. (`GET /Users/Me`
answers 500 on this build.) ADR-0012 assumes a non-admin account and nothing
enforces it; this is the check that would make it observable, recorded there
as recommended-not-implemented.

**`SELECT … FOR UPDATE SKIP LOCKED` is the whole of the queue's exclusion,
and both wrong spellings *hang* rather than answer.** Verified against
`pgvector/pgvector:pg17` by deleting each in turn from
`usher.db.repositories.jobs`: a bare `FOR UPDATE` makes the second worker
block on the first's uncommitted row lock, and removing the locking clause
entirely makes both workers read the same pending row so the second's
`UPDATE` blocks on the same lock one statement later. Neither returns a wrong
answer; both wait forever. So the concurrency cases in
`tests/integration/test_job_queue.py` bound every claim with
`asyncio.wait_for` — `pytest-timeout` is deliberately not a dependency, since
the timeout belongs to the two cases that need it rather than to the runner.

**A concurrency test must assert on *observed overlap*, not on a count.**
"Exactly one of two claimers got the job" is also what a serialised pair of
claims produces — the M3 failure verbatim, where a deleted single-flight lock
let a concurrency test pass five runs in a row. `JobQueueContract`'s harness
releases N claimers through an `asyncio.Barrier` and records the wall-clock
interval each claim occupied; `overlapping()` fails unless those intervals
genuinely intersect. Measured on this host: the two windows share **76.2%** of
their union.

**Bulk loading bypasses the repository, and the SQL has three traps.**
Verified against `pgvector/pgvector:pg17` on 2026-07-30, all three of which
`usher.db.repositories.bulk` is built around:

- `ON CONFLICT` must repeat a partial index's predicate, or Postgres raises
  `InvalidColumnReferenceError: there is no unique or exclusion constraint
  matching the ON CONFLICT spec`.
- One statement may not hit the same conflict target twice —
  `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect row
  a second time`. Every staging read is `SELECT DISTINCT ON (<target>)`.
  IMDb's dumps and Wikidata's crosswalk both really contain such duplicates.
- `xmax = 0` in `RETURNING` is the only way to tell an insert from an update;
  rowcount reports their sum.

`asyncpg`'s binary `COPY` is strictly typed (a `str` into an `integer` column
raises `TypeError` client-side) and CHECK constraints fire during `COPY` into
a *constrained* table, so one bad row aborts its batch. Reach the driver with
`(await (await session.connection()).get_raw_connection()).driver_connection`.
This project's staging tables are deliberately unconstrained, which moves
that failure one statement later — see the staging note below.

**`ON CONFLICT DO UPDATE` cannot read a CTE, and that is what makes M4's
watch-state merge two statements.** Verified 2026-07-31 against
`pgvector/pgvector:pg17`. Three findings, in the order they bite:

- `ON CONFLICT (kind, key) DO UPDATE SET priority = d.a`, where `d` is the
  statement's own CTE, fails with `missing FROM-clause entry for table "d"`.
  Only `excluded` and the target table are in scope.
- **The natural one-statement spelling of the watch-state merge silently
  zeroes real play history.** `watch_states.play_count` is `NOT NULL`, so
  the insert path must write `COALESCE(play_count, 0)` — and that collapse
  happens before the conflict clause runs, so `excluded.play_count` is `0`
  rather than `NULL` and
  `COALESCE(excluded.play_count, watch_states.play_count)` always picks the
  zero. Measured on a row holding `play_count = 7`, fed a merge carrying
  `NULL`: reads back **0**. This is exactly the failure ADR-0014 exists to
  prevent, arriving at the one layer where it is permanent.
- **`last_played_at` survives that same statement**, because it is nullable
  and therefore never collapsed. So "the natural spelling zeroes history" is
  true of exactly one of the two columns, and a test suite that checked only
  the timestamp would have ratified the bug. The two need separate cases.

The working shape is `UPDATE … FROM deduped` (where the `NULL` is still
`NULL` and still in scope) followed by `INSERT … ON CONFLICT DO NOTHING` —
two statements per conflict target, four per batch, all set-based.
`usher/db/repositories/watch_state.py`.

**`watch_states` has a `BEFORE UPDATE` trigger that owns `updated_at`.**
`trg_watch_states_set_updated_at` assigns `now()` unconditionally (the core
schema creates it alongside `sources` and `titles`; `media_items` has none
deliberately). So a merge's own `updated_at = observed_at` lands on the
*insert* path only, and a merged row's stored `updated_at` is its write
instant. Benign for the "latest `updated_at` wins" conflict rule — if
anything the more honest reading — but it means that assignment is not
observable on the update path, and `FakeWatchStateRepository` stores
`observed_at` on both paths, so the two diverge there. Pinned by
`tests/integration/test_watch_state_repository.py::test_the_update_trigger_owns_updated_at`.

**`:param::type` does not work in a SQLAlchemy `text()` statement.** Its
bind-parameter regex treats a name immediately followed by `::` as a
Postgres cast and skips the bind entirely, so `:source_id::uuid` reaches the
driver as that literal string and asyncpg answers
`PostgresSyntaxError: syntax error at or near ":"`. Verified by compiling
both spellings against the asyncpg dialect. Use `CAST(:source_id AS uuid)`.

**That same regex scans SQL *comments*, so `:name` inside a `--` line
declares a real bind parameter.** Same family as the trap above, opposite
direction: there the bind is silently skipped, here one is silently created.
A comment reading `-- lower(t.name), not lower(:name) against t.name` made
every single call to that statement raise
`sqlalchemy.exc.InvalidRequestError: A value is required for bind parameter
'name'` — with the offending token visible only in the echoed SQL, inside a
comment nobody reads when debugging a bind error. Found by running it
(M4 group C2, `usher/db/repositories/matching.py`). Write a placeholder that
is not colon-prefixed when a comment needs to quote a parameter spelling.

**`now()` is `transaction_timestamp()` and is frozen for the life of a
transaction; `clock_timestamp()` is the instant the statement runs.** Both
appear in this schema and the difference is load-bearing in two places:

- `usher.db.repositories.jobs` uses `clock_timestamp()` in all four of its
  statements. `requeue_running`'s `updated_at <= clock_timestamp() -
  interval` cannot match a claim made in the same transaction if both sides
  read the same frozen `now()`, and a job that failed twenty minutes into a
  long transaction must back off from *now* rather than from when that
  transaction opened. The mutation back to `now()` fails three cases.
- The `set_updated_at()` trigger the core schema installs assigns `now()`,
  so **two updates to the same row inside one transaction read back the
  identical `updated_at`**. `tests/integration/`'s per-test fixture is one
  long transaction, which makes "the second write is later than the first"
  unobservable there — `tests/integration/test_episode_repository.py::
  test_the_update_trigger_owns_updated_at` backdates the row with a raw
  `INSERT` (the trigger is `BEFORE UPDATE`, so an `INSERT` dodges it; a plain
  `UPDATE` does not) to give the stamp something to move away from.

**`UPDATE … RETURNING` promises no row order, and at real queue depth it is
not the order you selected.** `PostgresJobQueue`'s claim is a locking,
`LIMIT`ed `SELECT` in a CTE plus an `UPDATE … FROM` it. Measured on
`pgvector/pgvector:pg17` at 2,000 / 50,000 / 300,000 pending rows: the
selection stage is `Index Scan using ix_jobs_claim` at every size, while the
*update* stage moves from `Hash Join` over a `Seq Scan` (2,000 rows, where a
seq scan really is cheaper — cost 45) to `Nested Loop` + `Index Scan using
pk_jobs` from 50,000 up. So `RETURNING` hands rows back in heap order on a
small table, and an outer `ORDER BY` over the data-modifying CTE is what makes
a documented claim ordering true rather than incidental. It also means an
unscoped "no `Seq Scan` anywhere" plan assertion fails on a small fixture for
a plan that is correct at scale — scope it to the stage that has an ordering
to serve.

**A second `ORDER BY` key that the chosen index already carries is
unobservable.** `ix_jobs_claim` is `(priority DESC, created_at) WHERE status =
'pending'`, so deleting `created_at` from the claim's own `ORDER BY` survives
every ordinary test: the index supplies it. Forcing `SET LOCAL
enable_indexscan = off` is what makes it observable, and only in combination
with two other things — a row re-written by an `UPDATE` (so heap order and
`created_at` order disagree at all) and a `LIMIT` smaller than the candidate
set (so the key decides *which* rows are kept, not just how they are
returned). Worth knowing before writing a plan-independent ordering test.

**A test that commits through `usher.db.staging` leaves its staging table
behind.** `stage_records` creates the table with DDL, Postgres DDL is
transactional, and the integration suite's usual isolation is a rolled-back
transaction — so only a test that *commits* (the job queue's concurrency
harness, which needs two real backends) leaks one. It surfaces as
`test_migration_matches_the_orm_metadata` reporting schema drift in a *later*
file, so the queue suite passes alone and takes the migration test down in
combination. Such a fixture must `DROP TABLE IF EXISTS stg_*` in its cleanup.

**A staged `COPY` does not fire the destination's CHECK constraints**, on
this project's path, because `usher.db.staging`'s staging tables are
declared without constraints. The violation surfaces one statement later, at
the `INSERT … SELECT`, which goes through SQLAlchemy and is therefore a
`sqlalchemy.exc.IntegrityError` a repository can translate. Had the
constraint been on the staging table, `copy_records_to_table` runs on the
raw asyncpg connection, outside SQLAlchemy's error translation, and would
raise `asyncpg.exceptions.CheckViolationError` straight past any
`except IntegrityError`. Do not add constraints to a staging DDL without
giving its caller a second `except`.

**`tmdb_id` is unique per `kind`.** TMDb's movie and series id spaces overlap
on 26,968 ids (measured against Wikidata, 2026-07-30 — 47.3% of all series
ids it knows). `ix_titles_tmdb_id_kind`, and `get_by_tmdb_id` takes a
`TitleKind`. [ADR-0011](docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).

**IMDb TSVs have no quoting mechanism** and their title fields contain
literal `"` (21 in the first 553,395 rows of `title.basics.tsv.gz`).
`csv.reader`'s default `QUOTE_MINIMAL` silently strips them — verified. Parse
with `line.split("\t")`.

**Wikidata's crosswalk is seconds, not an hour.** The three property joins
measured 14.5 s / 2.1 s / 1.1 s unchunked. WDQS's timeout surfaces as
`HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
`Retry-After`. A live end-to-end run stored 336,200 pairs.

**Suspending `ix_titles_sort_name`/`ix_titles_name_lower_year` during Phase 0
is a real, if modest, win — kept, not emptied.** Measured 2026-07-30 against
the live `title.basics.tsv.gz` (1,271,138 retained titles): 35.8 s suspended
vs 40.2 s kept (11.0% faster), and the rebuilt pair is ~24% smaller (97 MB
vs 127 MB) than building them incrementally across the same load. Only
applies to a first bootstrap (`bulk_load_window` declines on a non-empty
`titles`), so the saving costs nothing when it doesn't apply. See PRD 04's
Phase 0 section for the full numbers.

**`PostgresImportRunRepository.save()` must roll back on a caught
`IntegrityError`, not just translate it.** Without the rollback, Postgres
leaves the *session* — not just the failed call — with an aborted
transaction, so the very next statement on it raises `sqlalchemy.exc.
PendingRollbackError` instead of running. `BootstrapService.import_dataset`'s
except handler is exactly such a next statement, so the missing rollback
broke its documented "does not re-raise" contract for real, verified against
real Postgres with two engine-bound sessions racing to bootstrap the same
dataset (`tests/integration/test_import_run_repository.py`). Deliberately a
full `session.rollback()`, not a `PostgresTitleRepository`-style SAVEPOINT —
see `usher/db/repositories/import_run.py`'s module docstring for why this
repository's one caller never has independent pending work on the session
worth a SAVEPOINT protecting.

**Fixing that session-poisoning bug surfaced a second one, one layer up, in
`BootstrapService.import_dataset` itself: the loser's failure handler
overwrote the winner's checkpoint.** Once `self._runs.get(dataset.name)`
after a caught `RepositoryConflict` stopped raising and started actually
returning a row, it returns the *other*, winning process's row — the loser
never got one of its own (`start()` never returned it one). The except
handler used to re-fetch by dataset name unconditionally and evolve+save
`FAILED` onto whatever it found, which is correct when that row is the
caller's own (a `_drain` failure, after `start()` succeeded) but silently
corrupts a legitimately `RUNNING` or already-`COMPLETED` import when it
belongs to someone else (a `start()` conflict) — worse than the crash it
replaced, because the crash was loud and this would not have been: a
subsequent resume reads exactly that corrupted record. `RepositoryConflict`
can only ever reach `import_dataset` from `start()` itself — once any row
exists for a dataset, every later `start()`/`save()` call updates that same
row rather than competing for a new one, so `_drain`'s own `save()` calls
(which always update the id `start()` already returned) cannot trigger it.
That made the fix a clean split: a `RepositoryConflict` from `start()`
specifically now goes to `_concede_to_other_owner`, which touches nothing
(no `save`, no `commit`) and returns the current owner's row exactly as
stored; every other `UsherPortError` path is unchanged. Verified against
real Postgres with a forced two-session race
(`tests/integration/test_bootstrap_concurrency.py`) — reproduced the
overwrite on the pre-fix code first (the winner's row read back `FAILED`
with the loser's unrelated conflict message), then confirmed the fix
leaves it untouched. The unit-level fakes needed a matching fix to even be
capable of catching this: the original conflict test double raised
`RepositoryConflict` with no competing row present at all, so asserting
only "the caller didn't crash" passed both before and after either bug —
it needs a real winner row seeded first, and an assertion that it comes
back byte-for-byte unchanged.

**An episode must never walk the match ladder, and the reason is in the
payload.** A live Emby episode carries the *episode's* own provider ids —
`{"Imdb": "tt2178782", "Tvdb": "4517466"}` on `tests/fixtures/emby/
episode_item.json` — not its series'. Two consequences, both catastrophic at
999,827 episodes. TVDb numbers episodes and series in different, numerically
overlapping namespaces and `usher.db.repositories.matching`'s TVDb statement
deliberately does not filter on kind, so an episode run through the provider
tiers resolves to whichever unrelated series holds that integer. And no
episode's IMDb id is in the catalog at all (`tvEpisode` is excluded from M2's
bootstrap by design), so the stub tier mints one junk `Title` per episode —
a catalog of rubbish roughly the size of the real one. `MatchService` returns
`UNMATCHED` for an episode with no lookups and **no remote-search job** (one
per episode is a queue the size of the library, and a TMDb title search for
an episode name is not a resolution path); `IngestService` attaches it to its
series' `Title`, labelled `MatchMethod.SERIES_PARENT`.

**Nothing a source can put in a payload may abort a walk.** `Title.imdb_id`
is pattern-validated (`^tt\d{7,8}$`) and `year` is `ge=0`, and a pydantic
`ValidationError` is **not** a `UsherPortError` — so `ReconcileService`, which
re-raises anything that is not one, would let a single stray
`ProviderIds.Imdb` in 1,126,674 items abort that source's sync permanently.
Filter every value to the shape the model accepts *before* the constructor;
`usher.services.matching._usable_ids` is where.

**`sorted()` over a set of `ProviderRef`/`NameYearProbe` raises.** Both are
`@dataclass(frozen=True, slots=True)` without `order=True`, so there is no
`__lt__` — `TypeError: '<' not supported`. `dict.fromkeys` is the idiom used
throughout: it deduplicates *and* keeps the batch's own order, which is what
makes a failure read in the order the page arrived.

**A service that saves a frozen checkpoint per batch must not evolve its own
stale copy in the failure handler.** `ReconcileService._flush` saves an
evolved `SyncRun` after each batch, so when the walk raises, `reconcile`'s
binding is the pre-walk value — and `run.evolve(status=FAILED)` on it writes
`items_seen = 0` over a checkpoint that recorded eight. Same trap
`BootstrapService.import_dataset` documents; here there is no re-fetch to
recover from (`SyncRunRepository` is a history, not a per-source checkpoint),
so a small mutable holder carries the latest run across the `try`.

**Moving the availability sweep into a `finally:` really does retract a
healthy library, and the obvious test shape hides why.** Measured. Seed seven
items, fail the walk immediately, one batch: nothing is written before the
failure, so the sweep would retract 7 of 7 — 100%, refused by ADR-0015's
ceiling, and `AvailabilitySweepRefused` then escapes the `finally:` and
propagates out of `reconcile`. The case fails, but on an uncaught exception
rather than on its own assertion, and it never exercises a sweep that
*succeeds* after a failed walk. The shape that does is a walk that commits
eight of ten items and then raises: two stale rows, 20%, under the ceiling,
no refusal, two available items silently retracted. **The ceiling is not a
second line of defence for the success-path gate** — it fires on a fraction,
so it catches the catastrophe and misses the quiet one. Reproduced against
real Postgres as well as the fakes.

**`observed_at=now()` instead of the run's start instant is a *semantic*
break, not a race.** A per-row write instant is always later than
`run.started_at`, so the sweep's `last_seen_at < seen_since` still spares
everything the run saw and no retraction test fails. What breaks is the
meaning of the column. Assert `stored.last_seen_at == run.started_at`
directly; no frozen clock is needed.

**`FakeTitleRepository` and `FakeTitleMatchRepository` are one table and are
now wired together.** `TitleRepository.add` flushes, so a stub the match
stage just wrote is visible to the very next `TitleMatchRepository` read.
Keeping two independent dicts made a *correct* service fail rather than a
wrong one pass: `IngestService`'s second walk of a series it had itself
stubbed missed the ladder, re-created the stub, conflicted on
`ix_titles_tvdb_id`, and had nothing left to look the winner up with. Pass a
`FakeTitleRepository` to the constructor; leaving it out is still meaningful
and models a read that missed another worker's committed write, which is the
only deterministic way to produce the race `MatchService`'s conflict handler
exists for.

**Two `IngestService` defects are invisible to every port fake and only real
Postgres catches them.** Skipping `resolve_seasons` or `resolve_episodes` and
trusting the freshly-minted UUIDv7 leaves all 24 unit cases green — a dict has
no foreign keys — and fails on `fk_episodes_season_id_seasons` /
`fk_media_items_episode_id_episodes` on the *second* walk, when that id names
no row. `tests/integration/test_services_ingest.py` and
`tests/integration/test_services_reconcile.py` are the paired runs; the latter
also pins "a refused sweep leaves the session usable for the `FAILED` row that
explains it", which no fake can express (the guard is evaluated in Python
after a successful `SELECT`, so Postgres never aborts the transaction).

## Commands

Verified working as of Group A (scaffold + config):

```bash
uv sync                          # install dependencies
uv run pytest                    # run the test suite (now needs Docker — see Group E below)
uv run pytest tests/unit         # fast unit tests only, no Docker required
uv run ruff check .              # lint — clean
uv run ruff format .             # format — clean
uv run mypy                      # type check, strict mode — clean
uv run lint-imports              # enforce architecture contracts — 6 kept, 0 broken
```

`[tool.ruff] extend-exclude = ["docs"]` keeps ruff off `docs/plans/*.md` and
`docs/prd/*.md` — ruff 0.16+ formats/lints Python code fences embedded in
Markdown by default, and those two directories hold planning and PRD prose
with embedded code fences that must stay byte-identical for other groups to
transcribe. Without the exclude, an unscoped `ruff format .` silently
rewrites that prose.

Verified working as of Group D (db engine, models, migrations) — requires a
live Postgres (e.g. `docker run -d -e POSTGRES_USER=usher -e
POSTGRES_PASSWORD=usher -e POSTGRES_DB=usher -p 5432:5432
pgvector/pgvector:pg17`), so not part of the default `uv run pytest` run:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head                       # apply migrations
uv run alembic downgrade base                     # reverse them (0001 is fully reversible)
uv run alembic revision --autogenerate -m "..."    # generate a migration from model changes
```

**`--autogenerate` is blind to two categories of change — verify by eye, not
just by running it:**
- **CHECK constraint bodies.** Changing a bound (e.g. loosening
  `ck_titles_year_non_negative`'s `>= 0`) and running `--autogenerate`
  produces an empty `pass` migration with no warning — verified directly.
  This schema deliberately mirrors every Pydantic field constraint as a
  CHECK, so this will eventually bite: tightening or loosening one in a
  model file does not, by itself, get picked up.
- **Triggers and functions** (the three `set_updated_at()` triggers from
  the first migration). These aren't SQLAlchemy `Table` metadata at all, so
  autogenerate never sees them, in either direction — adding, dropping, or
  changing one is always a hand-written `op.execute(...)` migration.

Verified working as of Group E (title repository, first integration tests) —
`tests/integration/` runs against a real PostgreSQL, started and torn down
per test run by `testcontainers` (`pgvector/pgvector:pg17`; first run pulls
the image, ~625 MB). Docker must be running; nothing else to set up. Its
schema comes from running the real Alembic migration once per test session
(`postgres_url`, `tests/integration/conftest.py`), not `Base.metadata.
create_all` — CHECK constraint bodies and the three `set_updated_at`
triggers are invisible to `create_all` the same way they're invisible to
`--autogenerate` (above), so a suite that never runs the migration can't
catch either drifting from the models. Each test still gets a fully
isolated database via a connection-bound transaction rolled back
afterward, not a schema recreate — cheaper than the 23-tests-worth of
`create_all`/`drop_all` cycles that used to cost, and `tests/integration/
test_migrations.py` is the ongoing regression check (trigger existence,
plus an autogenerate diff against the migrated database asserting no
drift):

```bash
uv run pytest                        # full suite — 235 tests, needs Docker for the 44 under tests/integration/
uv run pytest tests/unit             # 191 tests, no Docker
uv run pytest tests/integration      # 44 tests, needs Docker
uv run pytest -m "not integration"   # marker equivalent of tests/unit
uv run pytest -m integration         # marker equivalent of tests/integration
```

Two ways to select the same split — pick whichever fits: directory (what
Task 10 itself was written and verified against) or the `integration`
marker (registered in `pyproject.toml`, auto-applied to everything under
`tests/integration/` by that directory's `conftest.py`). Both are kept in
sync deliberately, so Group G's CI can use either without the two
diverging. Not wired into `addopts` as a default `-m "not integration"` —
that would make `pytest tests/integration/...` silently collect zero tests
instead of running them.

`tests/contract/title_repository_contract.py` holds the behavioural
assertions every `TitleRepository` implementation must satisfy — the same
suite runs against `FakeTitleRepository` (`tests/unit/`, no Docker) and
`PostgresTitleRepository` (`tests/integration/`, real Postgres), so the two
are verified to actually agree instead of merely looking alike. This is the
pattern PRD 08 calls the "contract suite" for `SourceAdapter`; M3 is
expected to reuse it.

Verified working as of Group F (telemetry bootstrap, FastAPI app with health
endpoints, then hardened in a follow-up review pass) — the app is now a
runnable service:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head
uv run uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000
curl http://localhost:8000/health          # liveness  -- {"status":"ok"}, HTTP 200 always
curl http://localhost:8000/health/ready    # readiness -- {"status":"ready","checks":{"database":true,"migrations":true}}, HTTP 200 or 503
```

`/health` and `/health/ready` are deliberately different: liveness must never
depend on Postgres (a database outage is not a reason to kill and restart
the process — restarting doesn't fix Postgres), so only readiness executes
`SELECT 1` (and, only if that succeeds, compares the live `alembic_version`
table against `usher.db.migrations.status.code_head_revision()` — PRD 08:
"the app refuses to serve on a schema mismatch rather than guessing").
Readiness returns HTTP 503 (not 200) when any check fails: no PRD text pins
a status code, but a readiness probe's real consumers — Kubernetes, Docker
`healthcheck`, load balancers — gate on the code and never parse the body.
Verified directly against a real container: stopping Postgres mid-session
leaves `/health` returning `{"status":"ok"}`/200 unchanged while
`/health/ready` switches to `{"status":"degraded","checks":{"database":
false,"migrations":false}}`/503 — same running process, no restart.
Readiness self-heals once Postgres comes back, still without restarting
Usher. Corrupting `alembic_version` on an otherwise-healthy database
produces the same degraded/503 shape with `database: true, migrations:
false` — a live demonstration is in the "readiness reports migration
state" commit.

Every request gets a real server span (`FastAPIInstrumentor`, wired in
`create_app`) with SQLAlchemy queries and outbound httpx calls nested under
it (`SQLAlchemyInstrumentor`/`HTTPXClientInstrumentor`, wired in
`configure_tracing`) — without this, nothing ever called
`tracer.start_as_current_span()` during request handling, so
`inject_trace_context` never fired in the running service, only in tests
that built their own span. `configure_tracing`/`configure_metrics` install a
real `TracerProvider`/`MeterProvider` *unconditionally* (a bare provider
with zero processors still assigns valid ids/records instruments, verified
directly) — only the actual OTLP *export* is conditional on
`settings.telemetry_enabled`. Both are `isinstance`-guarded against being
reconfigured on a second `create_app()` call in the same process (verified
directly: without the guard, 5 calls with telemetry enabled leaked 5
background export threads; with it, flat at the 2 the first call installs).
With no `OTEL_EXPORTER_OTLP_ENDPOINT` set, the default (unset) config still
carries zero *export*-related risk — nothing gRPC-related is ever
constructed. If an endpoint *is* set but nothing is listening there, the
OTel SDK's own retry loop logs a warning rather than raising or hanging the
app — graceful, but not literally silent in that specific case.

Stdlib `logging` (uvicorn's access/error logs, SQLAlchemy warnings, the OTel
exporter's own retry messages) is bridged into loguru via `_InterceptHandler`
(loguru's own documented recipe) — without it, confirmed on a live run, only
`usher`'s own logger calls were structured JSON; everything else printed as
plain text, ignored `log_level`/`log_json`, and never got
`trace_id`/`span_id` patched in.

`get_session` (`api/deps.py`) is the request's commit/rollback boundary:
commits once the handler completes without raising, rolls back and
re-raises otherwise. Previously nothing in `src/` ever called `commit()` —
`ports/repository.py`'s "the caller owns the session and the transaction"
had no concrete caller yet, so a future write endpoint that forgot to
commit would have lost data silently.

`/health` and `/health/ready` responses are typed (`api/dto/health.py`,
`LivenessResponse`/`ReadinessResponse`/`ReadinessChecks`), so
`/openapi.json` describes real shapes instead of `{"type": "object"}`.

`tests/integration/test_health.py`'s async `client` fixture needs
`asgi_lifespan.LifespanManager` (new dev dependency) wrapping the app:
`httpx.ASGITransport` only implements the ASGI "http" protocol, not
"lifespan" (confirmed against its source and FastAPI's own docs), so a bare
`AsyncClient(transport=ASGITransport(app=app))` never runs `create_app`'s
lifespan and `app.state.session_factory` is never set. Reproduced directly:
without the fix, `/health/ready` raises `AttributeError` while the other two
tests in the file still pass. `deps.py`'s `get_session_factory` now raises a
diagnosable `RuntimeError` for this exact case instead of Starlette's
generic `AttributeError`.

Verified working as of Group G (container image, compose stack, CI) — M1
is now deployable, not just runnable from a dev shell:

```bash
docker build -t usher .                       # multi-stage, ~332MB, non-root
echo "USHER_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build                  # postgres + usher, both healthchecked
curl -sf http://localhost:8100/health         # {"status":"ok"}
curl -sf http://localhost:8100/health/ready   # {"status":"ready","checks":{"database":true,"migrations":true}}
docker compose down                           # data/ bind mounts survive -- not removed by down, -v or not
```

`USHER_HOST_PORT` (`.env`, defaults to `8100`) is the *host*-side publish
port for `usher`'s container port `8000` — deliberately not a bare
`"8000:8000"`, since this host already publishes an unrelated container's
app on host port 8000. Postgres's own port is never published to the host
at all, only reachable from `usher` over the compose network as
`postgres:5432`, matching PRD 08's deployment shape.

The image is genuinely multi-stage: a `builder` stage has `uv` and builds
the venv, a `runtime` stage copies only `.venv/` and `src/` across. No
dependency in `uv.lock` needed a compiler to install (verified: `python:
3.13-slim` has none, and the build never installed one) — every one
resolved to a prebuilt `cp313` wheel. Verified directly against the built
image: runs as `uid=1000(usher)` (`touch /root/nope` → `Permission
denied`), has neither `uv` nor `gcc`/`cc` on `PATH`. `pyproject.toml`
declares `readme = "README.md"`; hatchling (the build backend) reads that
file while building `usher`'s own wheel, so `README.md` has to be `COPY`'d
into the builder stage before the second `uv sync` (the one that installs
the project itself, not just its dependencies) — omitted, that step fails.

**The Postgres healthcheck forces TCP
(`pg_isready -h 127.0.0.1 -U usher -d usher`), not the more obvious
`pg_isready -U usher -d usher`.** `pgvector/pgvector:pg17` runs a
*temporary* bootstrap server during `initdb` on a fresh volume — started
with `listen_addresses=''` (Unix socket only, confirmed against the
running container's own log line: `LOG: listening on Unix socket
"/var/run/postgresql/.s.PGSQL.5432"`, no TCP line) — to run init scripts
before the real server starts. `pg_isready` with no `-h` defaults to the
Unix socket, so an unqualified healthcheck reaches that temporary server.
Verified directly, twice: once with a standalone `docker run` polled every
~0.1s, once against the literal container `docker compose up` creates for
this project (same tight poll, racing the container's own creation from a
background process started before `docker compose up`). Both runs show
the same shape — the Unix-socket form reports "accepting connections"
while the bootstrap server is up, then "rejecting connections" for
roughly a second while it shuts down and the real server starts, then
"accepting" again once the real server is listening (standalone:
accepting at t+1.8s, rejecting t+2.0s–2.9s, accepting again from t+3.0s;
against the compose-managed container: same shape, ~1.1s-wide window). The
TCP-forced form (`-h 127.0.0.1`) never once false-positived in either run:
"no response" solidly until the exact moment the real server started
accepting TCP connections, because the bootstrap server never listens on
TCP at all. `depends_on: condition: service_healthy` gates on the first
successful check, not N consecutive ones, and `start_period` only exempts
early *failures* from counting — it does not delay a false-positive
*success* from being believed — so the Unix-socket form is a real,
reproducible way for `usher` to start against a Postgres that is about to
be torn down and restarted. Docker's own 2s-interval healthcheck did not
happen to land inside the ~1.1s window in the compose runs observed here —
that's host-load luck, not a guarantee, which is why this was verified by
tight-polling the mechanism directly rather than trusting a handful of
`docker compose up` runs to have been unlucky in the right way.

**`usher`'s own healthcheck targets `/health/ready`, not `/health`.**
Plain `docker compose` (no Swarm) never restarts a container because its
healthcheck failed — verified against Docker's documented behaviour, an
unhealthy status only ever changes what `docker compose ps` reports and
what `depends_on: condition: service_healthy` gates on; `restart:
unless-stopped` triggers on the container's *process* exiting, a
condition a failing healthcheck alone does not cause. With no restart-loop
risk in this deployment shape, `/health/ready` (database + migration
state) is strictly more informative for what a compose healthcheck
actually gates than `/health` (always 200, checks nothing) would be.
Compose has no separate liveness/readiness probe pair the way Kubernetes
does, so one healthcheck necessarily conflates the two; readiness is the
more useful of the two to conflate it into. No `curl`/`wget` in
`python:3.13-slim` (and adding either would cut against a small image), so
both the `usher` healthcheck and the CI verification below use Python's
own `urllib.request` — `urlopen` already raises on any non-2xx status or
connection failure, which is already a nonzero exit, so no explicit
try/except is needed for a check where any exception already means
"unhealthy".

`Settings.host`/`Settings.port` validated but were previously read by
nothing — the only way to start the server was the `uvicorn` CLI with
hardcoded `--host 0.0.0.0 --port 8000`. `src/usher/__main__.py`
(`python -m usher`, what the container's `CMD` now runs after `alembic
upgrade head`) fixes this: `uvicorn.run("usher.api.app:create_app",
factory=True, host=settings.host, port=settings.port)`, the same code
path the CLI form uses internally. Local dev is unaffected — `uv run
uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000`
still works exactly as documented above.

Migrations run on container start (`alembic upgrade head && exec python -m
usher`, `exec` so `docker stop`'s SIGTERM reaches uvicorn directly instead
of being swallowed by the wrapping shell) — verified end to end against a
clean volume: `docker exec ... psql -c '\dt'` shows all five core tables
(`titles`, `sources`, `media_items`, `users`, `watch_states`) plus
`alembic_version` at `a8a0e10ff464`, and `SELECT tgname FROM pg_trigger
WHERE NOT tgisinternal` shows all three `set_updated_at` triggers — the
migration ran for real, not `create_all`. **This has no distributed lock**
— fine at M1's one-replica scale, a real problem the moment `usher` is
ever scaled past one replica, at which point migrations belong in a
separate one-shot step instead of every replica's own startup;
`/health/ready`'s migration-mismatch check would surface a lost race as a
503 rather than prevent it. Noted in the Dockerfile's own `CMD` comment,
not solved — nothing in M1 runs more than one replica.

Test count grew from 235 to 237 (`src/usher/__main__.py`'s two new unit
tests). Full suite with coverage, exactly as CI runs it: `uv run pytest
--cov=usher --cov-report=term-missing` → 237 passed, 98% coverage.

CI (`.github/workflows/ci.yml`) pins `actions/checkout@v7` and
`astral-sh/setup-uv@v9` — the plan's `@v4`/`@v5` were several majors
stale by the time this ran (checked against each action's own GitHub
releases). A new `.python-version` file (`3.13`) at the repo root exists
because of a real gap found by running the install step, not by
inspection: `pyproject.toml`'s `requires-python = ">=3.13"` has no upper
bound, and a bare `uv sync --frozen` on a machine with no Python
preinstalled (verified on a stock `ubuntu:24.04` container with a
freshly-installed `uv`, standing in for a fresh runner) resolved **Python
3.14.6** — newer than the 3.13.14 every group has actually developed and
had mypy strict/pytest/ruff verified against. With `.python-version`
present, the identical command resolves `3.13.14` instead. `act` is not
installed on this host and was not added to check this workflow (a
GitHub-Actions emulator whose own correctness is itself unverified doesn't
add much confidence over not having it) — instead, every `run:` step's
literal command was run locally exactly as written, in order, and all
passed: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format
--check .`, `uv run mypy` (`Success: no issues found in 67 source files`
— the mypy-override contingency for `usher.db.migrations.*` was never
needed), `uv run lint-imports` (4 contracts kept), `uv run pytest --cov=
usher --cov-report=term-missing`. Not reproduced byte-for-byte: the
`setup-uv` action's own code (its net effect — a working `uv` on `PATH`
that obeys `.python-version` — was verified by installing `uv` the same
way, astral's own install script, on a bare `ubuntu:24.04` container,
which is a reasonable proxy for a fresh runner but not the literal
`ubuntu-latest` GitHub-hosted image), and Docker-in-CI for
`tests/integration/`'s testcontainers (GitHub's own docs state
`ubuntu-latest` ships Docker running by default, and this project's `uv
run pytest` already depends on exactly that locally, but no run happened
on an actual GitHub-hosted runner).

Verified working as of M2's final group (end-to-end integration, the index
measurement, and documentation) — the bulk-dataset bootstrap pipeline is
runnable for real, not just under test:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run python -m usher bootstrap --phase all       # import IMDb + TMDb ids + crosswalk
uv run python -m usher bootstrap --phase imdb      # one phase at a time
uv run python -m usher bootstrap-status            # progress and catalog size
uv run python scripts/measure_bulk_load.py         # NOT a test -- downloads the real dump
```

Verified directly against a scratch `pgvector/pgvector:pg17`, 2026-07-30,
downloading the real IMDb/TMDb dumps and querying live Wikidata — nothing
mocked. `bootstrap --phase imdb` killed mid-run at 700,000/1,271,138 titles
committed; re-run logged `resuming imdb.title.basics from position 6033908
(700000 rows already seen)` and finished at the identical 1,271,138 titles
an uninterrupted run reaches. A full `bootstrap --phase all` then ran end to
end: 1,271,138 titles (899,828 movies / 371,310 series), 538,937 with a
community rating, 291,737 linked to a `tmdb_id` (236,712 movies / 55,025
series, zero `(tmdb_id, kind)` duplicates — ADR-0011 holds under real data),
50,793 linked to a `tvdb_id`. Two known titles spot-checked correct end to
end: `tt0111161` (The Shawshank Redemption) landed with `tmdb_id=278`,
`community_rating=9.3`; `tt0944947` (Game of Thrones) landed with
`tmdb_id=1399`, `tvdb_id=121361`, `community_rating=9.2`. `bootstrap-status`'s
final report:

```text
titles in catalog: 1271138
wikidata.crosswalk       completed  position=30 seen=386364 written=385805
tmdb.ids.series          completed  position=228100 seen=228100 written=228100
tmdb.ids.movie           completed  position=1226544 seen=1226544 written=1226544
imdb.title.ratings       completed  position=1700616 seen=1700615 written=538937
imdb.title.basics        completed  position=12678891 seen=1271138 written=1271138
```

**Gotcha found running this: `kill -9 "$(cat pidfile)"` on a backgrounded
`uv run <command> &` does not stop the work.** `uv run` forks a child
process (the real interpreter) rather than exec-replacing itself — verified
directly with `ps --forest`, which showed two live PIDs, the `uv` wrapper
and its `python3` child. Killing only the wrapper PID left the child
running, orphaned, still committing to the database — the first kill/resume
attempt against this exact pipeline was contaminated by exactly this before
it was caught (a `bootstrap-status` read raced an orphaned child still
writing). A real deployment is unaffected: systemd's `KillMode=control-group`,
Docker's container-wide signal delivery, and an interactive terminal's
Ctrl-C all reach the whole process group, not just one PID in it. A
hand-rolled `nohup ... & echo $!` script does not — kill the child
(`pgrep -P "$wrapper_pid"`) or the whole process group, never just the
captured `$!`.

Verified working as of M3 (the Emby adapter) — a source can be registered
and interrogated over HTTP, and the suite is 865 tests (733 unit / 132
integration), mypy strict clean over `src` and `tests`, 6 import contracts:

```bash
uv run pytest                                    # 1434 tests as of M4 group D1 (1063 unit / 371 integration)
uv run pytest tests/unit                         # 1063 tests, no Docker and no network
uv run pytest tests/unit/test_adapters_emby_contract.py  # the contract suite against the real adapter
uv run mypy src tests                            # strict, including tests/
uv run ruff check --no-cache . && uv run ruff format --check .
uv run lint-imports                              # 6 kept, 0 broken

# Register a source and read its health, against a running app:
curl -sS -X POST http://localhost:8000/admin/sources \
  -H 'content-type: application/json' \
  -d '{"kind":"emby","name":"Living Room Emby","base_url":"https://emby.example","username":"...","password":"..."}'
curl -sS http://localhost:8000/admin/sources/<id>/status

# Diff a live server's *shape* against the committed fixtures. NOT a test,
# and its output is deliberately never committed -- see the module docstring.
export USHER_EMBY_URL=... USHER_EMBY_USER=... USHER_EMBY_PASSWORD=...
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
```

**Fixtures under `tests/fixtures/emby/` are shape-recorded and
value-synthetic, and that is a licensing constraint, not a style.** A real
Emby response embeds TMDb-sourced metadata, which TMDb's terms forbid
redistributing and which "ship importers, never data" above already
forbids committing; it also identifies a real library and carries real
server and user ids. Regenerate a scrubbed *shape* with the script above
and diff that; never paste a capture in.

**Live-verification runs must not write a credential, a token, a user id or
a host into the repo.** M3's run was driven from a throwaway script outside
the working tree, reading the operator's own secrets file, redacting every
one of those four values from anything it printed. Its one write to a real
account recorded the item's complete `UserData` first and restored it
exactly afterwards, confirmed by reading it back.
