# Because You Watched Rows: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Because You Watched rows are built from three candidate lists (titles the LLM
names, the metadata provider's recommendations, and the closest embeddings), merged by
fixed rules. Rows are stored per seed by a scheduled job and shown with a watched mark.

**Architecture:**
- **One pure merge.** It is a function of three ordered id lists and a vote map, so its
  order is pinned by unit tests rather than by a live run.
- **One service.** `SeedRowService.build(seed)` gathers the three lists, merges them, and
  saves one record per attempt. `SeedRowJob` walks the household's recency window every
  15 minutes and builds only what is due.
- **One read-path change.** `BecauseYouWatchedProvider` reads stored cards when a seed has
  them and `title_neighbors` otherwise, then marks watched cards with one read.

**Tech Stack:** Python 3.13, httpx, SQLAlchemy async over asyncpg, PostgreSQL 17 with
pgvector (testcontainers), Alembic, loguru, OpenTelemetry, pytest with strict mypy over
`tests/`.

**Spec:** `docs/specs/2026-11-07-because-you-watched-rows-design.md`. Read it before any
task. It is a point-in-time record, so never edit it after Task 1 commits it. Spike
evidence, which is not part of the repo, is in `/var/tmp/usher-spike/`.

## Decisions this plan makes

1. **A stored record keeps the last good cards and the last attempt separately.**
   `cards`, `built_at` and `built_recipe` describe the last successful build. `attempted_at`,
   `attempt_outcome` and `attempt_recipe` describe the latest try.
   *Why:* the spec says a failed rebuild keeps the existing row, and the 7-day retry still
   needs a record of the failure. One set of fields cannot say both.
2. **Every `PortDataMalformed` from recommendations empties Recommended, and the build goes
   on.** `PortUnavailable`, `PortRateLimited` and `PortAuthFailed` stop the walk.
   *Why:* TMDb's 404 is already `PortDataMalformed` (`adapters/tmdb/client.py:160-173`), and
   Recommended is optional. An outage must not be recorded as a 7-day attempt.
3. **An LLM `PortDataMalformed` records an `UNUSABLE` attempt, and any other `UsherPortError`
   stops the walk.** This covers truncation, non-JSON output and a payload with no
   `films`. Either way the ledger row is written through `LLMLedger.settle`.
4. **`SeedRowJob.last_done()` answers when the last run of this process completed, held in
   memory, and `None` until one has.**
   *Why:* the artefact has no single completion time, and the due check is a cheap read, so
   a restart's first tick running it costs nothing.
5. **The job uses the LLM client and metadata provider the process already holds.**
   `build_scheduler` gains `client`, `metadata` and `user_id` parameters and registers
   `SeedRowJob` only when `sessions`, `client` and `user_id` are all present.
   *Why:* today `build_scheduler` holds no client, and `create_app` builds one only when
   `worker_enabled`, which is the shipped default and is set in production.
6. **No new `Settings` field and no new metric instrument.** Every number is a module
   constant copied from the spec, and the telemetry is span attributes.
   *Why:* the spec fixes the numbers. A setting means `.env.example` and the console
   catalogue, and a metric changes PRD 10's pinned count of 47.
7. **A stored row is shown even if `title_neighbors` was never built, and its reason is
   always "Because you watched X."**
   *Why:* the "Similar genres and themes" wording describes the metadata-only blend, which
   a stored row is not.
8. **Watched marks cost one `played_title_ids` read per `propose`, not one per row.**
   *Why:* the rows rule that a shelf family hydrates once per `propose`.
9. **Temperature is a per-call port argument.** `None` means the adapter's existing 0.8,
   so curation is unchanged.
10. **Name matching is a new `TitleRepository.resolve_names`, not
    `TitleMatchRepository.match_by_name_year`.**
    *Why:* that method requires one kind and drops ambiguous probes. The spec needs any
    kind, the highest vote count, and no skeletons.
11. **Close is a new exact-scan read, `TitleEmbeddingRepository.nearest_known`.**
    *Why:* `nearest_for` joins no `titles`, so it cannot apply the vote floor. A filtered
    HNSW scan returns fewer rows than asked (`search-and-embeddings.md`).

## Global Constraints

- **The spec's numbers, verbatim. These are module constants, not settings:**
  - the LLM names 25 titles, under a schema allowing up to 30 `{title, year}`, at temperature 0.3
  - Recommended is the first page only
  - Close is the top 100 by cosine among titles with `tmdb_vote_count >= 500`
  - the score is the sum of `1 / (60 + rank)`, with rank starting at 1
  - a row is up to 20 cards: odd positions from Named, even from Merged, and a Close-only title is never a card
  - a row needs at least 8 cards to be stored
  - the job runs every 15 minutes over a window of 12
  - rebuild after 30 days, retry a short or unusable attempt after 7
- **Builds run one at a time.** No `gather`, `TaskGroup` or `create_task` in the new
  modules; vLLM is shared with production Alfred.
- **Source names stored or reported are `named`, `recommended` and `close`.** No
  source-specific word may leave its adapter.
- **Layering:** `services/` imports only `domain/` and `ports/`. `uv run lint-imports` must
  stay at 15 kept and 0 broken.
- **TDD:** every code step follows a failing test that was seen to fail.
- **Ordering tests assert their premise** (`assert near_id < far_id` and the like), per
  CLAUDE.md's evidence rules. `FakeLLMClient` repeats its last response, so every call
  count gets its own `len(client.calls) == N`.
- **PRD changes ride the task that invalidates them.** Each task greps `docs/prd/` for the
  names it changes: `title_neighbors`, `BecauseYouWatched`, `llm_calls`, `purpose`, the
  CLI command.
- **Comments follow `guard_prose.py`'s caps**, which the hook enforces.
- **Never undo with `git checkout <path>`, `restore`, `stash` or `reset`.**
- **TMDb fixtures are shape-recorded and value-synthetic**, and every TMDb id in them is
  at least 90,000,000.
- **The gate, before every commit:** `uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly`
  During iteration, run only the task's test files.

## Review Focus

1. **The LLM writes the year inside the title ("The Social Network (2010)") or gets it off
   by one.** The title should still match; off by two should not. Pinned in Task 5
   (`parse_recall`) and Task 4 (`resolve_names`).
2. **Two catalog titles share a name and year.** Production holds two *Air (2023)*. The
   one with more votes wins, and a skeleton never does. Pinned in Task 4.
3. **The seed is a series watched only through its episodes.** It is a seed (the
   `list_recent` roll-up), and Recommended uses the TV endpoint. Pinned in Task 3 (TV path)
   and Task 7 (window).
4. **vLLM is down for hours.** That costs one failed call per run, records nothing towards
   the 7-day rule, and the walk stops. Pinned in Task 7.
5. **A stored card's title is deleted later.** The card is dropped from the screen, not
   raised. Pinned in Task 8.

## File map

| File | Task | What changes |
|---|---|---|
| `docs/specs/…-design.md`, this plan, `docs/plans/progress.md`, `docs/prd/README.md`, `tests/unit/test_docs_currency.py` | 1 | committed; status rows |
| `src/usher/ports/llm.py`, `adapters/llm/openai_compatible.py`, `domain/curation.py`, `tests/fakes/llm_client.py` | 2 | `temperature`; `LLMPurpose.BECAUSE_YOU_WATCHED` |
| `src/usher/ports/metadata.py`, `adapters/tmdb/provider.py`, `tests/fakes/metadata_provider.py`, `tests/fixtures/tmdb/recommendations_{movie,tv}.json`, `scripts/capture_tmdb_fixture.py` | 3 | `recommendations(ref)` |
| `src/usher/ports/repository/title.py`, `…/search.py`, `db/repositories/{title,search}.py`, their fakes | 4 | `resolve_names`, `nearest_known` |
| `src/usher/domain/seed_rows.py`, `services/seed_row_merge.py`, `services/seed_row_recall.py` | 5 | domain record, merge, prompt, recipe |
| `src/usher/ports/repository/seed_row.py`, `db/models/seed_row.py`, `db/repositories/seed_row.py`, migration, `db/backup_manifest.py`, fake | 6 | storage |
| `src/usher/services/seed_rows.py`, `composition.py`, `api/lanes.py`, `cli.py` (`_schedule`) | 7 | service, job, wiring |
| `src/usher/ports/rows.py`, `services/rows/because_you_watched.py`, the five `RowContext` builders | 8 | read path, watched mark |
| `src/usher/cli.py` (`similar`) | 9 | `--build-row` |
| — | 10 | live acceptance |

---

### Task 1: Commit the spec, this plan and their status rows (controller)

**Files:**
- Modify: `docs/plans/progress.md`, `docs/prd/README.md`, `tests/unit/test_docs_currency.py`
- Add: the spec and this plan, both untracked today

- [ ] **Step 1:** Add `## Because You Watched rows (from docs/specs/2026-11-07-because-you-watched-rows-design.md)`
  to `progress.md`. Give it a table with one row per task 2–10 (Task, What it delivers,
  Plan file, Status), all `📋 planned`, and a two-line note beneath it mirroring the
  every-version table's note.
- [ ] **Step 2:** Add a row to `docs/prd/README.md`'s `## Implementation plans` table,
  marked "neither a milestone nor an eval phase", that names PRD 06 and PRD 10.
- [ ] **Step 3:** In `test_docs_currency.py`, add a `_BECAUSE_YOU_WATCHED_TABLE` heading
  constant to the union of status tables and bump the table-count label from ten to
  eleven.
- [ ] **Step 4:** Run `uv run pytest tests/unit/test_docs_currency.py tests/unit/test_docs_pointers.py`.
  Expected: PASS.
- [ ] **Step 5:** Commit with `docs: the Because You Watched rows spec, its plan and their status rows`.

### Task 2: A per-call temperature, and the new LLM purpose

**Files:** `src/usher/ports/llm.py:25-43`, `src/usher/adapters/llm/openai_compatible.py:89-100`,
`src/usher/domain/curation.py:18-32`, `tests/fakes/llm_client.py`,
`tests/unit/test_adapters_llm.py`, `tests/unit/test_domain_curation.py:475`,
`tests/unit/test_ports.py:418`, `docs/prd/10-telemetry-and-dashboards.md:226`

**Interfaces:**
- Produces: `LLMClient.complete_json(self, prompt: str, schema: dict[str, Any], *, purpose: LLMPurpose, temperature: float | None = None) -> tuple[dict[str, Any], LLMUsage]`
- Produces: `LLMPurpose.BECAUSE_YOU_WATCHED = "because_you_watched"`
- Produces: `RecordedCall` gains `temperature: float | None`

- [ ] **Step 1: Write the failing tests:**
  - `test_the_temperature_a_caller_passes_is_the_one_sent`: a `MockTransport` body with
    `temperature=0.3` has `json.loads(request.content)["temperature"] == 0.3`.
  - `test_a_call_that_names_no_temperature_sends_the_default`: the same, `== 0.8`.
  - The fake records it: `client.calls[0].temperature == 0.3`.
  - The purpose set becomes `{CURATION, QUERY_EXPANSION, BECAUSE_YOU_WATCHED}` in both
    `test_domain_curation.py:475` and `test_ports.py:418`.
- [ ] **Step 2:** Run those files. Expected: FAIL, because the `temperature` keyword and the
  new member don't exist yet.
- [ ] **Step 3:** Implement the port argument, the adapter's `_DEFAULT_TEMPERATURE = 0.8`
  used when the argument is `None`, the fake's field and the enum member. Add
  `"because_you_watched"` to PRD 10's purpose comment. A new enum value needs no
  migration: `enum_column` is `native_enum=False` with no membership CHECK (`m08a`).
- [ ] **Step 4:** Run the files, plus `tests/unit/test_llm_client_contract.py`, which
  parametrizes over every purpose. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(llm): a per-call temperature, and the because_you_watched purpose`.

### Task 3: Recommendations on the metadata port

**Files:** `src/usher/ports/metadata.py:80-171`, `src/usher/adapters/tmdb/provider.py:57-59,240-268`,
`src/usher/adapters/tmdb/mapping.py:377-407` (reuse `search_candidates`),
`tests/fakes/metadata_provider.py:83`, `tests/unit/test_ports_metadata.py:236` (keep `Stale`
incomplete), `tests/unit/test_adapters_tmdb_provider.py`,
`tests/fixtures/tmdb/recommendations_movie.json`, `tests/fixtures/tmdb/recommendations_tv.json`,
`tests/fixtures/tmdb/README.md`, `scripts/capture_tmdb_fixture.py`

**Interfaces:**
- Produces: `MetadataProvider.recommendations(self, ref: ProviderRef) -> list[MetadataCandidate]`.
  It returns the provider's first page in the provider's order. A ref the provider cannot
  serve, or one it no longer serves, is `PortDataMalformed`, as `fetch` already documents.
- Produces: `FakeMetadataProvider.seed_recommendations(ref: ProviderRef, candidates: Sequence[MetadataCandidate]) -> None`
  and a `recommendation_calls: int` counter. `fail_with` applies to it.

- [ ] **Step 1: Write the failing tests:**
  - `test_recommendations_read_the_first_page_of_the_movie_endpoint`: the transport sees
    path `/movie/90000001/recommendations` with `page=1`. The result keeps the fixture's
    order, and every candidate has `kind == TitleKind.MOVIE`.
  - `test_a_series_reads_the_tv_endpoint`: path `/tv/90000002/recommendations`, with
    candidates of kind `SERIES` named from `name`/`first_air_date`.
  - `test_a_provider_with_no_entity_at_the_ref_is_malformed`: a 404 raises `PortDataMalformed`.
  - `test_a_ref_with_no_kind_is_refused_not_guessed`.
- [ ] **Step 2:** Run them. Expected: FAIL, with no `recommendations` attribute.
- [ ] **Step 3:** Implement the method on the port, the TMDb provider (`_resolve`, then
  `_DETAIL_PATH[kind]` plus `/{id}/recommendations`, then `search_candidates`) and the fake.
  Record both fixtures by shape with `--kind recommendations` added to the capture
  script, synthesise their values, and add a row for each to the fixtures README.
- [ ] **Step 4:** Run the TMDb, port and fake test files, plus
  `tests/unit/test_no_third_party_data.py`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(metadata): a title's recommendations, first page, on the port and the TMDb adapter`.

### Task 4: Two catalog reads, names and close titles

**Files:** `src/usher/ports/repository/title.py`, `src/usher/ports/repository/search.py:115`,
`src/usher/db/repositories/title.py`, `src/usher/db/repositories/search.py:407-446`,
`tests/fakes/title_repository.py`, `tests/fakes/title_embedding_repository.py`,
`tests/integration/test_title_repository.py`, `tests/integration/test_search_repository.py`

**Interfaces:**
- Produces: `NamedTitle(name: str, year: int)`, a frozen, slotted dataclass in `ports/repository/title.py`.
- Produces: `TitleRepository.resolve_names(self, probes: Sequence[NamedTitle]) -> dict[NamedTitle, uuid.UUID]`.
  It matches on `lower(t.name) = lower(probe.name)`, which keeps `ix_titles_name_lower_year`
  usable, and `t.year BETWEEN probe.year - 1 AND probe.year + 1`, on any kind, with
  `enrichment_state <> 'skeleton'`. It takes the highest `tmdb_vote_count` (NULLS LAST),
  then `id`. It is one statement, and empty input makes no statement.
- Produces: `TitleEmbeddingRepository.nearest_known(self, seed_id: uuid.UUID, *, min_votes: int, limit: int) -> list[uuid.UUID]`.
  It is an exact scan ordered by `embedding <=> seed`, then `title_id`. It joins `titles`
  for `coalesce(tmdb_vote_count, 0) >= :min_votes` and not skeleton, and excludes the
  seed. It takes the `SET LOCAL` exact-scan switches `nearest_for` uses, plus
  `max_parallel_workers_per_gather = 0` (`search-and-embeddings.md`).

- [ ] **Step 1: Write the failing tests.** Each runs against the Postgres arm and the fake
  arm.
  - `resolve_names`:
    - `test_a_name_matches_within_one_year_and_not_two`
    - `test_case_does_not_matter`
    - `test_of_two_titles_sharing_a_name_and_year_the_one_with_more_votes_wins` (premise: `assert low_votes < high_votes`)
    - `test_a_skeleton_never_matches`
    - `test_a_series_matches_as_well_as_a_film`
    - `test_no_probes_is_no_statement`
  - `nearest_known`:
    - `test_close_titles_come_back_nearest_first` (premise on the distances)
    - `test_a_title_under_the_vote_floor_is_left_out` (499 against 500)
    - `test_the_seed_and_skeletons_are_left_out`
    - `test_the_limit_is_met_even_when_most_nearer_titles_are_under_the_floor`: 150 nearer titles under the floor, and `limit=100` still returns 100 qualifying titles
- [ ] **Step 2:** Run them. Expected: FAIL, with the methods undefined.
- [ ] **Step 3:** Implement both methods on the ABCs, the Postgres repositories (`text()`
  with `CAST(:x AS …)` binds, never `::`) and the fakes.
- [ ] **Step 4:** Run the two integration files and the fake tests. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(catalog): names to titles within a year, and close titles over a vote floor`.

### Task 5: The domain record, the merge, the prompt and the recipe

**Files:**
- Create: `src/usher/domain/seed_rows.py`, `src/usher/services/seed_row_merge.py`, `src/usher/services/seed_row_recall.py`
- Test: `tests/unit/test_domain_seed_rows.py`, `tests/unit/test_seed_row_merge.py`, `tests/unit/test_seed_row_recall.py`

**Interfaces:**
- Produces, in `domain/seed_rows.py`:
  - `CardSource(StrEnum)`: `NAMED="named"`, `RECOMMENDED="recommended"`, `CLOSE="close"`.
  - `SeedRowOutcome(StrEnum)`: `BUILT="built"`, `TOO_FEW="too_few"`, `UNUSABLE="unusable"`.
  - `SeedRowCard(DomainModel)`: `title_id: uuid.UUID`, `sources: frozenset[CardSource]`, with at least one source.
  - `SeedRow(DomainModel)` has these fields:
    - `seed_title_id: uuid.UUID`
    - `cards: tuple[SeedRowCard, ...] = ()`
    - `built_at: AwareDatetime | None = None`
    - `built_recipe: str | None = None`
    - `attempted_at: AwareDatetime`
    - `attempt_outcome: SeedRowOutcome`
    - `attempt_recipe: str`

    Its validators, each with its own test:
    - Cards are present exactly when `built_at` and `built_recipe` are.
    - Present cards number between `MIN_SEED_ROW_CARDS = 8` and `SEED_ROW_CARDS = 20`.
    - Card ids are distinct and never the seed.
    - `attempt_outcome == BUILT` implies cards, `built_at == attempted_at` and `built_recipe == attempt_recipe`.
    - Both recipes are 32 characters.
- Produces: `merge_seed_row(*, seed_id: uuid.UUID, named: Sequence[uuid.UUID], recommended: Sequence[uuid.UUID], close: Sequence[uuid.UUID], votes: Mapping[uuid.UUID, int], limit: int = SEED_ROW_CARDS) -> tuple[SeedRowCard, ...]`.
  It is the spec's merge exactly. The caller has already removed skeletons and missing
  titles.
- Produces, in `seed_row_recall.py`:
  - `recall_prompt(*, name: str, year: int | None, overview: str | None, keywords: Sequence[str]) -> str`.
    It renders the spec's prompt verbatim. With no year the first line is `Title: {name}.`,
    no overview renders empty, and keywords are joined with `", "`.
  - `RECALL_SCHEMA: dict[str, Any]`: `films`, an array with `maxItems: 30` of
    `{title: string, year: integer}`, both required, with `additionalProperties: false` at
    both levels as `curation._schema` does. It has no `minItems`.
  - `RECALL_TEMPERATURE = 0.3`.
  - `parse_recall(payload: Mapping[str, Any]) -> list[NamedTitle]`. It strips a trailing
    `\s*\(\d{4}\)\s*$` from each title, skips an entry with an empty title, and keeps the
    first of any duplicate `(lower(name), year)`. With no `films` list it raises
    `PortDataMalformed`.
  - `seed_row_recipe(*, model: str) -> str`: the md5 hex of the sorted JSON of the
    prompt template, the schema, the temperature, 60, 20, 8, 500, 100, 25 and the model.
    Built the way `blend_fingerprint` is.

- [ ] **Step 1: Write the failing tests.** The merge's anchor case:

```python
def test_the_row_alternates_named_and_merged_and_never_shows_a_close_only_title() -> None:
    a, b, c, d, e, f, seed = _ids(7)
    cards = merge_seed_row(seed_id=seed, named=[a, b, c], recommended=[d, b], close=[e, c, f],
                           votes={a: 100, b: 100, c: 100, d: 200, e: 900, f: 900})
    # Premises: b scores 2/62, c 1/63 + 1/62, and a and d tie at 1/61, broken by votes.
    assert 2 / 62 > 1 / 63 + 1 / 62 > 1 / 61
    assert [x.title_id for x in cards] == [a, b, c, d]
    assert [x.sources for x in cards] == [
        {CardSource.NAMED}, {CardSource.NAMED, CardSource.RECOMMENDED},
        {CardSource.NAMED, CardSource.CLOSE}, {CardSource.RECOMMENDED}]
```

  The rest of the cases:
  - `test_equal_scores_and_votes_break_by_id` (premise: `assert first < second`)
  - `test_a_row_stops_at_twenty_cards`
  - `test_when_named_runs_out_merged_fills_the_rest_and_the_reverse`
  - `test_the_seed_is_never_a_card`
  - `test_a_title_named_twice_is_one_card`
  - `test_a_year_inside_the_title_is_stripped`
  - `test_a_payload_without_films_is_malformed`
  - `test_the_prompt_is_the_specs_text` (compares against the spec's block, with values filled in)
  - `test_the_recipe_changes_with_the_model_and_with_any_merge_constant`
  - one test per `SeedRow` validator
- [ ] **Step 2:** Run the three files. Expected: FAIL, because the modules don't exist.
- [ ] **Step 3:** Implement the three modules.
- [ ] **Step 4:** Run the three files. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(rows): the seed-row record, its merge and its recall prompt`.

### Task 6: Storage for seed rows

**Files:**
- Create: `src/usher/ports/repository/seed_row.py`, `src/usher/db/models/seed_row.py`
  (registered in `db/models/__init__.py`), `src/usher/db/repositories/seed_row.py`, the
  migration `src/usher/db/migrations/versions/m10i_seed_rows.py` (the next free id after
  `origin/main`'s head at implementation time, which is `m10h` today),
  `tests/fakes/seed_row_repository.py`, `tests/contract/seed_row_repository_contract.py`,
  `tests/unit/test_fakes_seed_row_repository.py` and
  `tests/integration/test_seed_row_repository.py`
- Modify:
  - `src/usher/db/backup_manifest.py`: add `"seed_rows": _rebuildable(…, "schedule --once")`
  - `tests/unit/test_backup_manifest.py`: `REBUILDABLE` goes from 21 to 22
  - `tests/integration/test_migrations.py:528`: re-point the `-1` block, one assertion for `seed_rows`
  - `tests/unit/test_db_migration_status.py`: head, chain and cardinals
  - `.claude/rules/db-and-sql.md`: the landing count
  - PRD 02, if it lists tables (grep for `title_neighbors`)

**Interfaces:**
- Produces: `SeedRowRepository(ABC)`, which flushes and never commits:
  - `list_for(self, seed_title_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SeedRow]`. Empty input makes no statement.
  - `save(self, row: SeedRow) -> None`. It replaces the seed's record.
- Produces the table `seed_rows`:
  - Columns:
    - `seed_title_id` uuid PK, FK `titles.id` ON DELETE CASCADE
    - `card_title_ids uuid[]` and `card_sources text[]`, both NOT NULL with default `{}`. Each source is the sorted names joined by `+`, such as `close+named`.
    - `built_at` and `built_recipe`, both nullable
    - `attempted_at`, `attempt_outcome` (`enum_column`, length 16) and `attempt_recipe`, all NOT NULL
  - Hand-written CHECKs: `ck_seed_rows_sources_match_cards`, `ck_seed_rows_built_iff_cards`,
    `ck_seed_rows_card_count` (0, or 8 to 20) and `ck_seed_rows_recipe_length`.

- [ ] **Step 1: Write the failing contract tests**, run against the fake and Postgres arms:
  - `test_a_saved_row_reads_back_whole_with_its_sources_in_card_order`
  - `test_saving_again_replaces_the_record`
  - `test_a_seed_with_no_record_is_absent_from_the_answer`
  - `test_an_attempt_with_no_cards_round_trips`
  - `test_no_seeds_is_no_statement`

  These run against Postgres only:
  - `test_deleting_the_seed_title_deletes_its_record`
  - the existing `test_every_check_constraint_in_the_models_exists_in_the_database`
  - `test_migration_matches_the_orm_metadata`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement the port, model, migration, repository (a `DO UPDATE` guarded by
  `IS DISTINCT FROM`, with refusals mapped through `refusals_as_conflict`), fake and manifest
  entry.
- [ ] **Step 4:** Run the contract files, `tests/integration/test_migrations.py`,
  `tests/unit/test_db_migration_status.py`, both backup-manifest tests and
  `test_backup_manifest_covers_the_live_schema.py`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(db): seed_rows, one stored Because You Watched row per seed`.

### Task 7: The builder, the job and the wiring

**Files:**
- Create: `src/usher/services/seed_rows.py`, `tests/unit/test_services_seed_rows.py`
- Modify:
  - `src/usher/composition.py:798-822`: `build_scheduler`, plus a new `seed_row_scope`
  - `src/usher/api/lanes.py:147`: pass `client`, `metadata` and `user_id`
  - `src/usher/cli.py:625-646`: `_schedule` builds the LLM client, the metadata provider and `DefaultUserId`, and closes the clients in `finally`
  - `src/usher/services/rows/because_you_watched.py`: rename `_SEED_WINDOW` to the public `SEED_WINDOW = 12`
  - `tests/unit/test_services_scheduler.py`: the registry test
  - `tests/unit/test_services_llm_ledger.py:222-246`: add `seed_rows` to the ledger AST guard's module tuple
  - `docs/prd/06-rows-and-recommendations.md`: the caching table's neighbour row, plus a seed-rows row

**Interfaces:**
- Consumes: Tasks 2 to 6.
- Produces:
  - `SeedRowService.__init__(self, *, titles: TitleRepository, embeddings: TitleEmbeddingRepository, watch_states: WatchStateRepository, rows: SeedRowRepository, calls: LLMCallRepository, client: LLMClient, metadata: MetadataProvider | None, commit: Callable[[], Awaitable[None]], model: str, now: Callable[[], AwareDatetime]) -> None`
  - `SeedRowService.due_seeds(self, user_id: uuid.UUID) -> list[Title]`. These are
    `list_recent(user_id, limit=SEED_WINDOW)` in recency order, filtered by `is_due`, with
    seeds the catalog no longer holds dropped.
  - `SeedRowService.build(self, seed: Title) -> SeedRow`. Unless it raises, it saves exactly
    one record and settles exactly one ledger row, with the domain write before
    `settle`, which commits both, as `CurationService.generate` does.
  - `is_due(row: SeedRow | None, *, recipe: str, now: AwareDatetime) -> bool`, which is pure.
    These are due: no record; a `TOO_FEW` or `UNUSABLE` attempt over 7 days old, or one
    under a different recipe; and, when no failed attempt holds it back, no cards, a
    `built_recipe` other than `recipe`, or a `built_at` over 30 days old.
  - `SEED_ROWS_JOB_NAME = "seed_rows.build"`, `SEED_ROWS_PERIOD = timedelta(minutes=15)`,
    and `SeedRowScope = Callable[[], AbstractAsyncContextManager[SeedRowService]]`.
  - `SeedRowJob(ScheduledJob)`: `__init__(self, scope: SeedRowScope, *, user_id: Callable[[], Awaitable[uuid.UUID]], now: Callable[[], AwareDatetime])`.
    `run()` builds every due seed in one scope, one at a time, and answers `DONE`.
    `last_done()` follows Decision 4.
  - `build_scheduler(settings, *, sessions, client: LLMClient | None = None, metadata: MetadataProvider | None = None, user_id: Callable[[], Awaitable[uuid.UUID]] | None = None) -> Scheduler`.
  - `seed_row_service(session: AsyncSession, settings: Settings, *, client: LLMClient, metadata: MetadataProvider | None) -> SeedRowService` in `composition.py`. It wires the Postgres repositories over one session, `session.commit` and `settings.llm_model`, and `seed_row_scope` is built on it.

`build` gathers the lists in this order, which the tests pin with call counts:
1. one LLM call, with `purpose=BECAUSE_YOU_WATCHED` and `temperature=RECALL_TEMPERATURE`
2. `resolve_names`
3. `recommendations`, only with a provider and a `tmdb_id`
4. `resolve_tmdb_ids(seed.kind, …)`
5. `nearest_known(seed.id, min_votes=500, limit=100)`
6. one `list_by_ids` over the union, to drop skeletons and read votes
7. `merge_seed_row`

Errors map as Decisions 2 and 3 say.

- [ ] **Step 1: Write the failing tests**, with the fakes and a fixed `now`:
  - `is_due`, one case per clause, each with its boundary: 29 and 31 days, 6 and 8 days.
    This includes `test_a_failed_attempt_under_the_current_recipe_holds_back_a_stale_row`,
    which stops a recipe change from re-calling the LLM every 15 minutes.
  - `test_a_build_stores_the_merged_row_and_settles_one_ledger_row`: `len(client.calls) == 1`,
    `calls[0].temperature == 0.3`, the purpose is `BECAUSE_YOU_WATCHED`, and there is one ok
    ledger row.
  - `test_too_few_cards_records_the_attempt_and_keeps_the_earlier_row`
  - `test_an_unusable_completion_records_the_attempt_and_keeps_the_earlier_row`
  - `test_an_unreachable_llm_raises_and_saves_no_record`: one failed ledger row and no `seed_rows` record.
  - `test_a_provider_with_no_record_of_the_seed_leaves_recommended_empty`, using `fail_with(PortDataMalformed(...))`.
  - `test_an_unreachable_provider_raises_and_saves_no_record`
  - `test_a_seed_without_a_provider_id_never_asks_the_provider`: `recommendation_calls == 0`.
  - `test_skeleton_titles_never_become_cards`
  - `test_nothing_due_makes_no_llm_or_provider_call`: `len(client.calls) == 0` and `recommendation_calls == 0`.
  - `test_the_job_builds_due_seeds_in_recency_order_including_a_series_seen_through_episodes`
  - `test_an_outage_stops_the_walk_at_the_failing_seed`: the next seed is never attempted, and `run()` raises.
  - `test_an_unusable_seed_does_not_stop_the_walk`
  - `test_last_done_is_none_until_a_run_completes`
  - `test_the_builder_starts_no_concurrent_work`: an AST scan of `services/seed_rows.py` for
    `gather`, `TaskGroup`, `create_task` and `wait`, checking `ast.Import` and `ast.ImportFrom`.
  - The registry test: with a client, `["search_queries.retention", "similar.rebuild", "seed_rows.build"]`; without one, the existing two.
- [ ] **Step 2:** Run `tests/unit/test_services_seed_rows.py` and `tests/unit/test_services_scheduler.py`.
  Expected: FAIL.
- [ ] **Step 3:** Implement the service, the job, `seed_row_service`, `seed_row_scope` and the
  three call sites.
  Update PRD 06's caching table and its BecauseYouWatched paragraph's statement of where
  neighbours come from.
- [ ] **Step 4:** Run those files, plus `tests/unit/test_cli_schedule.py` and
  `tests/integration/test_scheduler_lane.py`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(rows): a scheduled job builds Because You Watched rows from three lists`.

### Task 8: The row reads stored cards and marks watched ones

**Files:**
- Modify:
  - `src/usher/ports/rows.py:27-52`: add a `seed_rows: SeedRowRepository` field
  - `src/usher/services/rows/because_you_watched.py`
  - the `RowContext` builders: `composition.py:1174`, `cli.py:1049`, `api/deps.py:680`, `tests/unit/rows.py:475` (a `FakeSeedRowRepository` on `Library`) and `tests/unit/test_ports_rows.py:40`
  - `tests/unit/test_rows_because_you_watched.py`
  - PRD 06 at `:164` and `:206-214`
  - PRD 10, if it lists home span attributes

**Interfaces:**
- Consumes: `SeedRowRepository.list_for` and `WatchStateRepository.played_title_ids(user_id, title_ids) -> set[uuid.UUID]`.
- Produces: `BecauseYouWatchedRow(seed_id, seed_name, neighbours, *, semantic: bool, played: frozenset[uuid.UUID] = frozenset())`
  and a `_progress` override that returns `Progress(played=True)` for each id in `played`.
  A row built from stored cards passes `semantic=True` (Decision 7).
  Grep `services/rows/` for `_played` before adding the attribute.
- Produces the span attributes `usher.home.because_you_watched.stored` and
  `usher.home.because_you_watched.fallback`, both row counts, on the current `propose`
  span, as `curated.py:185` does.

`propose` makes these reads:
1. `list_recent`
2. `list_by_ids`
3. one `seed_rows.list_for` over the window
4. for each seed, the stored cards when its record has any, otherwise `neighbors.list_for` (only when `computed_at()` is not `None`)
5. after the overlap rule picks the rows, one `played_title_ids` over every chosen card

- [ ] **Step 1: Write the failing tests:**
  - `test_a_seed_with_a_stored_row_uses_its_cards_in_stored_order` (premise: the stored order differs from id order)
  - `test_a_seed_with_no_stored_row_falls_back_to_title_neighbors`
  - `test_a_seed_whose_record_is_only_a_failed_attempt_falls_back`
  - `test_stored_rows_show_even_when_title_neighbors_was_never_built`
  - `test_a_stored_row_says_because_you_watched`
  - `test_the_overlap_rule_applies_to_stored_rows`
  - `test_a_stored_card_whose_title_was_deleted_is_dropped`
  - `test_watched_cards_are_marked_and_unwatched_ones_are_not`
  - `test_the_watched_mark_costs_one_read_per_propose`: the read count equals 1 for a screen of three rows.
  - `test_the_span_counts_stored_and_fallback_rows`

  The existing tests in the file must still pass unchanged.
- [ ] **Step 2:** Run `tests/unit/test_rows_because_you_watched.py`. Expected: FAIL.
- [ ] **Step 3:** Implement the change. Then run `tests/unit/test_ports_rows.py` (including
  `test_every_row_context_field_is_read_by_at_least_one_provider`) and
  `tests/unit/test_api_home.py`.
- [ ] **Step 4:** Run those three files. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(rows): Because You Watched reads stored rows and marks what was watched`.

### Task 9: `usher similar <id> --build-row`

**Files:** `src/usher/cli.py:971` (`_similar`), `:1836-1860` (argparse), `:2014-2030`
(`parse_args`), `:2167` (dispatch); `tests/unit/test_cli.py`, `tests/unit/test_cli_errors.py:73`,
`tests/integration/test_cli_pipeline.py`; `docs/guide/`, wherever it shows `usher similar`.

**Interfaces:**
- Consumes: `seed_row_service` (Task 7), `SeedRowService.build` and `SeedRowRepository.list_for`.
- Produces:
  - `--build-row` needs a title id and refuses `--rebuild`, `--resume` and `--max-seeds`, each through `parser.error`.
  - It builds that seed's row now, due or not, then prints the outcome and one line per
    card: `{n:>2}. {name} ({year}) [{sources joined by "+"}]`.
  - With `USHER_LLM_ENABLED=false` it raises `SystemExit`, with a message naming the setting.
  - Bare `usher similar <id>` also prints the seed's stored record when it has one: the
    outcome, `built_at`, and the cards in the same format.
  - It catches `PortDataMalformed` itself, as `_curate` does.

- [ ] **Step 1: Write the failing tests:**
  - `test_build_row_needs_a_title_id`
  - `test_build_row_refuses_the_rebuild_flags`
  - `test_build_row_without_an_llm_names_the_setting`
  - `test_build_row_prints_each_card_with_its_sources`, which monkeypatches the factories the way `test_cli_schedule.py` does
  - `test_similar_prints_a_stored_row_beside_the_neighbours`, in integration
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement the flag and update the guide.
- [ ] **Step 4:** Run the three CLI test files. Expected: PASS. Then run the full gate.
- [ ] **Step 5:** Commit with `feat(cli): usher similar <id> --build-row builds and prints one seed's row`,
  then set the status rows to `🔨 in review`.

### Task 10: Live acceptance (controller, owner-gated)

This runs after the owner merges and the deploy reports healthy, never before, and never
while a deploy is running. A deploy kills `docker compose exec` jobs.

- [ ] **Step 1:** Write the spec's bar table to `/var/tmp/usher-byw-acceptance/bars.md` and
  record its `sha256` before any build.
- [ ] **Step 2:** Find the three seeds' ids with `usher search`. Then, from a throwaway
  script outside the tree that writes no id or host into the repo, run
  `usher similar <id> --build-row` twice for BlackBerry, War Dogs and Spider-Man:
  Homecoming. Save each output to `/var/tmp/usher-byw-acceptance/`.
- [ ] **Step 3:** Check each bar against both runs and post the actual rows, not a summary.
  A failed bar stops the work and goes to the owner.
- [ ] **Step 4:** Confirm the scheduled job built the household's own window. Run
  `usher similar <id>` for each window seed, and check `llm_calls` for one
  `because_you_watched` row per build.
- [ ] **Step 5:** Ask the owner to read the home screen. Their verdict is the last bar.

## Spec premises, checked against the tree

| Premise | Found |
|---|---|
| `complete_json` hard-codes 0.8 | ✅ `openai_compatible.py:93`; no test pins it |
| A new `LLMPurpose` needs PRD 10 | ✅ `:226`; no DB CHECK, so no migration |
| `nearest_for` cannot be reused | ✅ no `titles` join, no vote filter |
| This row has no watched mark | ✅ there is no `_progress` override; `played_title_ids` exists (`watch_state.py:108`) |
| The provider's not-found is distinguishable | ❌ a 404 is plain `PortDataMalformed`, hence Decision 2 |
| The scheduler can reach the LLM | ❌ `build_scheduler` holds no client, hence Decision 5 |

## Residuals

- Two processes running the scheduler (the API and `usher schedule`) can build the same
  seed twice. The last write wins, at the cost of one extra call. This is accepted.
- Blue Beetle, the back of the row, and the title page's "More like this" are out of
  scope, as the spec says.
