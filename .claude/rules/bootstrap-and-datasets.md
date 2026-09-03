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

Rules for this subsystem; the detail is in the ADRs and module docstrings named
here. The `measure_*` scripts are not tests: each hits the network, two take a
required `--phase`, and **`measure_bulk_load.py` takes no arguments and truncates
the database between passes — scratch database only, never a real catalog.**

```bash
uv run usher bootstrap --phase all      # the six steps, in FULL_SEQUENCE's order
uv run usher bootstrap --phase imdb     # or credit-names | aliases | tmdb-ids | crosswalk | movielens
uv run usher bootstrap --phase ratings  # an alias, not a step
uv run usher bootstrap-status           # titles, genome vectors, vocabulary, checkpoints
```

## Phases: the order is load-bearing and `all` is not every member

- **Steps of a full run, in execution order:** `imdb`, `credit-names`,
  `aliases`, `tmdb-ids`, `crosswalk`, `movielens` (`domain/bootstrap.py:93-100`).
- **`all` and `ratings` are aliases rather than steps, and `--phase all`
  dispatches neither.** `ratings` re-imports `title.ratings.tsv.gz` (8.2 MiB)
  alone rather than paying `--phase imdb`'s 214.4 MiB and the rewrite of every
  name and year, which stales embeddings (ADR-0040); adding it to `FULL_SEQUENCE`
  imports the file twice. A unit case asserts `FULL_SEQUENCE` and `PHASE_ALIASES`
  partition the enum, so a member added to neither is a red rather than a phase
  `argparse` offers and `run_bootstrap` ignores. `POST /admin/bootstrap/{phase}`
  and `choices=` are the same enum.
- `credit-names`, `aliases` and `movielens` join `titles` on `imdb_id`, so all
  three follow `imdb`. **Run `credit-names` before any TMDb enrichment crawl**
  (`--help` says so): `fill_credit_names` writes only skeletons, so a title the
  crawl already enriched is deferred to TMDb permanently and re-running does not
  repair it. It stales no embedding in either order.

## Resuming and checkpoints

- **Every step is resumable and a resume finishes at the identical row count**,
  verified under `SIGKILL` for `imdb`, `aliases` and `credit-names`.
- **A dataset that groups by title checkpoints the last line of a *completed*
  title, not `position`**, so a resume replays no retained row rather than
  replaying some and destroying the rest. `credit-names` also rebuilds its whole
  `nconst -> primaryName` index before every run, resumed or not.
- **`--phase ratings` writes `--phase imdb`'s own `import_runs` row**
  (`imdb.title.ratings`), deliberately, so the two cannot disagree about what
  revision a catalog holds. The cost is that a completed run at an unchanged
  upstream revision resumes at EOF and writes nothing, which looks exactly like
  success. A rebuild therefore runs
  `DELETE FROM import_runs WHERE dataset = 'imdb.title.ratings'` first, verifying
  0 remain, then asserts on **`rows_written`**, never on the exit status. Do not
  rely on the ETag having moved: a run whose correctness depends on that reports
  success for doing nothing.
- **A phase writing through a join can poison a shared checkpoint by succeeding,
  so every such phase refuses an empty catalog.** `apply_ratings` matches nothing
  there, checkpoints `completed`, and every later bootstrap then resumes past the
  file and imports no ratings — permanently, with `bootstrap-status` green. A
  comment asserting the precondition does not run, and the guard belongs in the
  phase's own function: inline it returns from all of `run_bootstrap`.

## The download cache is keyed on the upstream token, not on local presence

**"The dumps are on disk, so nothing re-downloads" is false.**
`CachedDatasetFile.ensure_local` short-circuits on `path.exists() and
stamp.read_text() == revision`, where `revision` is what `revision()` resolved
*this run* from a `HEAD`. IMDb regenerates its dumps daily, so a cache filled
days ago re-downloads and imports a **different snapshot**; to re-run against a
fixed one, pin `revision()` to the sidecar's own value. Range-fetching only the
members an importer reads was declined: re-implementing resume, `If-Range` and
the stale-snapshot interlock is new failure surface for a one-off saving.

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
  so any order check must be on the string. A guard refusing a non-ascending dump
  was declined as stronger than the writer needs — the repo's own akas fixture is
  contiguous but not sorted.
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
  is no `community_rating` column** — `m10a`/ADR-0040 split it out, and the old
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
- **A `RepositoryConflict` reaches `import_dataset` only from `start()`, and the
  loser must touch nothing.** `_concede_to_other_owner` returns the owner's row as
  stored, with no `save` and no `commit`; re-fetching by dataset name and saving
  `FAILED` onto whatever comes back corrupts a `RUNNING` or `COMPLETED` import
  belonging to someone else. ⚠️ **Unit fakes catch neither bug** — a conflict with
  no competing row passes before and after, so seed a real winner row and assert
  it returns unchanged.
- ⚠️ **Known defect, recorded and not fixed:** `composition.run_bootstrap` opens
  `bulk_load_window()` *around* `import_dataset`, so the window is entered before
  ownership is known and its `count_titles() == 0` guard is read then. Two
  processes over an empty catalog both `DROP INDEX`; the loser concedes without
  raising and `CREATE INDEX`es both while the winner is still streaming — costing
  exactly the saving the window exists for, plus a `SHARE` lock on `titles`.
  Exposure is the *download*, not the run; both fixes change a shipped M2 path.
- **`bootstrap-status`' report scales with the catalog, not with what is on the
  screen** — three of `_GENOME_COVERAGE`'s five terms are full scans of `titles`.
  No cache was added: **that shape is an admin page's and nothing else's**, and a
  client route assembling `BootstrapReport` would pay a scan per request.
- Wikidata's crosswalk is seconds, not an hour; WDQS timeouts arrive as
  `HTTP 504 text/plain` after ~65 s with **no `Retry-After`**.

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
  **Both merge directions have a low yield (ADR-0036), so a merge costs a second
  request per person.** That is *expensive*, not *impossible* — do not restate it
  as an absolute; that is how this claim went wrong once already.
- **Price a TMDb crawl from the policy ceiling — ADR-0005's ~25 rps, never an
  observed lane rate** — over the people the catalog *holds*, not those its
  payloads mention (`mapping._CAST_LIMIT` caps stored cast at 50 a title).
- **The ≤6-month cache term applies to `raw_payloads`, not to derived columns**
  (ADR-0016), so **cache the response and a crawl recurs; store the derived id and
  it does not.** `_UPSERT_PEOPLE`'s `DO UPDATE SET` omits `imdb_id`, so `usher
  derive` cannot discard a crawl — an accident of a column list, pinned by a test.
