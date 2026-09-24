---
paths:
  - "src/usher/adapters/bulk/**"
  - "src/usher/services/bootstrap.py"
  - "src/usher/db/repositories/bulk.py"
  - "src/usher/db/repositories/people.py"
  - "src/usher/db/repositories/import_run.py"
  - "src/usher/domain/people.py"
  - "src/usher/domain/bootstrap.py"
  - "scripts/measure_bulk_load.py"
  - "scripts/measure_imdb_people.py"
  - "scripts/measure_people_provenance.py"
---

# IMDb, TMDb id exports, Wikidata and MovieLens

Rules for this subsystem; the detail is in the module docstrings named here. The `measure_*` scripts are not tests: each hits the network, two take a
required `--phase`, and **`measure_bulk_load.py` takes no arguments and truncates
the database between passes — scratch database only, never a real catalog.**

```bash
uv run usher bootstrap --phase all      # the six steps below, in FULL_SEQUENCE's order
uv run usher bootstrap --phase imdb     # or credit-names | aliases | tmdb-ids | crosswalk | movielens
uv run usher bootstrap --phase ratings  # an alias, not a step
uv run usher bootstrap-status           # titles, genome vectors, vocabulary, checkpoints
```

## Phases: the order is load-bearing and `all` is not every member

- **`all` and `ratings` are aliases rather than steps, and `--phase all`
  dispatches neither.** `ratings` re-imports `title.ratings.tsv.gz` alone, sparing
  `--phase imdb`'s rewrite of every name and year (which stales embeddings); in
  `FULL_SEQUENCE` it would import the file twice. A unit case asserts
  `FULL_SEQUENCE` and `PHASE_ALIASES` partition the enum, so a member in neither is
  red, not a phase `argparse` offers and `run_bootstrap` ignores.
- `credit-names`, `aliases` and `movielens` join `titles` on `imdb_id`, so all
  three follow `imdb`. **Run `credit-names` before any TMDb enrichment crawl**
  (`--help` says so): `fill_credit_names` writes only skeletons, so a title the
  crawl already enriched is deferred to TMDb permanently and re-running does not
  repair it. It stales no embedding in either order.

## Resuming and checkpoints

- **Every step is resumable, and a resume finishes at the identical row count.**
- **A dataset that groups by title checkpoints the last line of a *completed*
  title, not `position`**, so a resume replays no retained row rather than
  replaying some and destroying the rest. `credit-names` also rebuilds its whole
  `nconst -> primaryName` index before every run, resumed or not.
- **`--phase ratings` writes `--phase imdb`'s own `import_runs` row**
  (`imdb.title.ratings`), so the two agree on the revision, and a failure of that
  file resumes with `--phase ratings` whichever phase imported it. A completed run
  at an unchanged revision resumes at EOF, writes nothing and exits 0: a rebuild
  deletes that row first (verify 0 remain) and asserts on **`rows_written`**.
- **A phase writing through a join can poison a shared checkpoint by succeeding**:
  over an empty or partial catalog it checkpoints `completed`, and every later run
  resumes past what it missed, with `bootstrap-status` green. So each refuses an
  empty catalog in its own function (inline, the guard returns from all of
  `run_bootstrap`) and the refusal exits 1, and none runs while a dataset
  `composition._READS` names is unfinished — the crosswalk on the TMDb exports too.

## The download cache is keyed on the upstream token, not on local presence

**"The dumps are on disk, so nothing re-downloads" is false.**
`CachedDatasetFile.ensure_local` short-circuits on `path.exists() and
stamp.read_text() == revision`, where `revision` is what `revision()` resolved
*this run* from a `HEAD`. IMDb regenerates its dumps daily, so a cache filled
days ago re-downloads and imports a **different snapshot**; to re-run against a
fixed one, pin `revision()` to the sidecar's own value.

## Parsing the IMDb TSVs

- **They have no quoting mechanism and their title fields contain literal `"`.**
  `csv.reader`'s default `QUOTE_MINIMAL` silently rewrites those names. Parse
  with `line.split("\t")`. **Zero rows split to a wrong column count** on the
  three measured, so a wrong count is a real signal, never noise.
- **`types` and `attributes` are multi-valued inside one `title.akas` column,
  separator `\x02`** — a reader assuming a tab calls those rows malformed.
- **`name.basics` is sorted lexicographically by the `nconst` *string*, not
  numerically**, so the obvious in-memory index — sorted array plus `bisect` —
  answers `None` for millions of real people, each miss a title quietly losing a
  name (same family as `db-and-sql.md`'s migration-id padding trap).
- **Both dumps are contiguous by title** (zero lexicographic descents), which is
  what makes batching by title sound; the *integer* inside the id descends freely,
  so any order check must be on the string.
- **The seven dumps are not one snapshot.** A `nconst` named by
  `title.principals` and absent from `name.basics` is routine — **drop the credit,
  never raise** — and a title whose principals *all* dangle must yield no record,
  since an empty name list would blank a `credit_names` another source filled. It
  is also why `IMDbCreditNamesDataset` checkpoints a **composite** revision.
- **`replace_aliases` refuses an over-long name for the whole call, including its
  DELETE** (`ck_title_search_names_name_within_btree_bound`), so the parser drops
  names past `SEARCH_NAME_MAX_CHARS` rather than losing a whole batch to one row.

## Writing aliases and credit names

- **`replace_aliases` requires whole titles per call, and `IMDbAkaDataset`
  supplies them — `group_of` returns `row.imdb_id`.** `_ImdbDataset` otherwise
  batches on a row count, so a title straddling a boundary arrives in two calls
  and the second call's scoped `DELETE` takes the first call's rows. **Nothing
  reports it** — both calls are in scope and the report sums them — and the
  invariant to check is that *written* and *stored* match to the row. **Any new
  caller of a scoped-replace port needs that shape.**
- **`replace_aliases` is scoped by `imdb_ids` *and* `kind = 'alias'`**, so
  `person` rows survive an alias re-import — and a title whose akas IMDb withdrew
  keeps its stale ones, a streaming importer having no wider scope.
- **The writer compares and dedups under SQL `lower()`, and Postgres is
  authoritative by construction.** Python `casefold()` folds strictly more (German
  `ß`, Greek final sigma), but the test for keeping an alias is whether it reaches
  anything `ix_titles_name_lower_prefix` does not, and that index is a btree over
  the *database's* `lower(name)`. Do not repair the fake — the divergence is
  enumerated in `tests/fakes/bulk_catalog_repository.py`.
- `apply_ratings` writes **`imdb_average_rating` and `imdb_num_votes`**. **There
  is no `community_rating` column** — `m10a` split it out, and the old
  name survives on the wire only, through `domain/title.py`'s `WIRE_FIELD_NAMES`.

## MovieLens

- **`ml-latest` is forced, not preferred**: `ml-32m` dropped the genome and
  `ml-25m` may not be redistributed. Only `ml-latest` has both.
- **`links.csv`'s `tmdbId` is not unique and `imdbId` is.** Join the genome to
  `titles` through `'tt' || lpad(imdbId, 7, '0')` and **never** through `tmdbId`:
  a `tmdbId` join fans one TMDb id across several MovieLens movies and attaches
  one film's genome vector to another's title, on ids that are all real. The
  `lpad` is not decoration — widths vary, the convention is documented nowhere,
  and an unpadded row joins to nothing rather than raising.
- **`genome-scores.csv`'s physical grouping is a property of the snapshot, not a
  promise, and the importer verifies it** — contiguous `movieId` runs of exactly
  1,128 rows carrying `tagId` 1…1128 are what make single-pass streaming possible,
  and a wrong-length run, duplicate `tagId` or reopened `movieId` fails hard.
  **`tagId` ordering *within* a run is deliberately not enforced**: vectors are
  built by index and a shuffled-run case proves it.
- **Do not pass `newline=""`.** The members are CRLF-terminated and invisible only
  because `member_lines` decodes through `io.TextIOWrapper` in universal-newline
  mode; a stray `\r` lands in every stored tag name while every `"\n".join(...)`
  fixture passes. One case catches it, on carriage returns in a tag name.
- **Parse a tag line with `partition(",")`, not `split(",", 1)`** — `split` hands
  a comma-less row back as a one-element list whose `[0]` is a valid `tagId`.

## The bootstrap service

- **`PostgresImportRunRepository.save()` must roll back on a caught
  `IntegrityError`, not merely translate it.** Otherwise Postgres leaves the whole
  *session* aborted and the next statement raises `PendingRollbackError`,
  including `import_dataset`'s own except handler. Deliberately a full
  `session.rollback()`, not a SAVEPOINT (`db/repositories/import_run.py`).
- **Only the holder of a dataset writes its row.** The hold is a session-level
  advisory lock on its own connection (the session's goes back to the pool at each
  commit, lock and all); `hold()`/`start()` take it, the `finally` or a dead process
  releases it, and a test that holds releases. A failure before `start()` takes it
  to record itself, or concedes and writes nothing.
- **A phase holds what it reads *shared*** (`hold_for_reading`) from before its check
  until it ends, on one more connection (`Settings` counts two per bootstrap job): a
  refused read skips it, a hold refused by readers concedes. Being another backend, a
  read refuses its own process's hold, so `reading()` releases before the next step.
- **`touch` confirms the hold and the reads**, since an ended connection
  (`idle_session_timeout`, a proxy's cut) frees a lock silently. `_beating` commits
  one every `HEARTBEAT_SECONDS` through a fetch or wait; it or `hold()` precedes every
  save; a synchronous dump read bypassing `download.paced` stalls the beat throughout.
- ⚠️ **Known defect, recorded and not fixed:** `run_bootstrap` opens
  `bulk_load_window()` *around* `import_dataset`, so its `count_titles() == 0` guard
  is read before ownership is known. Two processes over an empty catalog both `DROP
  INDEX`; the second concedes and streams nothing, but its window closes by `CREATE
  INDEX`ing under the first, costing the saving plus a `SHARE` lock on `titles`.
- **`bootstrap-status`' report scales with the catalog** — three of
  `_GENOME_COVERAGE`'s five terms scan `titles`, so no client route assembles it.
- **WDQS times out as a `504 text/plain` (~65 s, no `Retry-After`) or a `200` cut
  off mid-document** — both `PortUnavailable`. Page with `bd:slice`, never
  `STRSTARTS`: a prefix filter still walks the whole join, slower than no filter.
- **`import_dataset` records a failure and returns it; the CLI exits 1 on any failed,
  conceded, skipped or refused one.** It retries the revision `HEAD` and a fetch,
  never a writer, and **commits before the first `HEAD`, after `start()` and before
  every wait**: a transaction held across a wait keeps an xid, hides `RUNNING`, and
  dies to `idle_in_transaction_session_timeout` unrecorded.
- **State model** (PRD 04 has the operator's). `start()` over `COMPLETED` persists only
  a cleared `error` and a heartbeat, so a retry resumes from `_FetchFailed.at`, never a
  re-read row; "failed this run" is `error is not None`, asked after the concede.

  | row | means | blocks a reader |
  |---|---|---|
  | `RUNNING` | importing, or its importer died | yes |
  | `FAILED` + error | part of a snapshot, or none ever completed | yes |
  | `COMPLETED` | finished | no |
  | `COMPLETED` + error | finished; a later attempt failed before its first batch, or `then` after it | no |
  | any, held exclusive elsewhere | another process is importing it; none starts under a reader's shared hold | yes: the read is refused |
- **`download.py`'s `HEAD` follows WDQS's ladder**: 408/5xx unavailable, 429
  rate-limited, any other 4xx or no `ETag`/`Last-Modified` malformed. The TMDb
  walk-back reads only 404/403 as "not published" (`revision_if_published`) — read
  every `PortUnavailable` that way and an outage is 35 `HEAD`s, "no export found".

## People, credits and provenance

- ⚠️ **`m09d` shipped a schema and nothing fills it.** `credits.source`,
  `people.imdb_id`, `ix_credits_source_natural_key` and `CREDIT_SOURCE_PRECEDENCE`
  exist; **no IMDb row has ever been written to `people` or `credits`, both new
  indexes are empty**, and `adapters/tmdb/mapping.py` is still the only writer of
  a `Credit`. IMDb fills `titles.credit_names` and nothing else — quoting that
  design as deployed is wrong. `db/models/people.py` argues the natural key
  `(title_id, source, billing_order)` and why `(title_id, person_id, kind)` cannot
  be UNIQUE.
- **A TMDb `cast[]`/`crew[]`/`created_by[]` entry carries no IMDb `nconst`** —
  `imdb_id`, birth/death year and biography live on `/person/{id}`, one request
  per person (`/find/{nconst}?external_source=imdb_id` works, no follow-up call).
  **Both merge directions have a low yield, so a merge costs a second
  request per person.** That is *expensive*, not *impossible* — never an absolute.
- **Price a TMDb crawl from the configured ceiling —
  `USHER_TMDB_REQUESTS_PER_SECOND`, default 30 — never an observed lane rate** —
  over the people the catalog *holds*, not those its payloads mention
  (`mapping._CAST_LIMIT` caps stored cast at 50 a title).
- **The ≤6-month cache term applies to `raw_payloads`, not to derived
  columns**, so **cache the response and a crawl recurs; store the derived id
  and it does not.** `_UPSERT_PEOPLE`'s `DO UPDATE SET` omits `imdb_id`, so `usher
  derive` cannot discard a crawl — an accident of a column list, pinned by a test.
