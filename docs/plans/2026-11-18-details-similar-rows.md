# Details Page Similar Titles: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The "Similar titles" section on a title's page shows the same stored row the
home screen shows for that title, with posters, and a title with no stored row gets one
built in the background while the old list is on screen.

**Architecture:**
- **One new queued job.** `JobKind.SEED_ROW`, keyed on a title id, one at a time. Its
  handler calls the existing `SeedRowService` and publishes `title.updated`. An outage
  defers the job instead of failing it.
- **One read service.** `SimilarShelfService.read(title_id)` picks the stored row or
  `title_neighbors`, hydrates the cards through the home row's own hydration, and queues
  the build when the record is due.
- **One route body.** `GET /titles/{id}/similar` answers `{cards, source, building}`.
- **One console section.** `SimilarSection` renders home cards. `useSimilar` re-reads
  every 5 seconds for at most 60 seconds while a build is waited on.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async over asyncpg, PostgreSQL 17 with
pgvector (testcontainers), pytest with strict mypy over `tests/`. React 19, Vite,
TanStack Query, MSW, vitest, Playwright.

**Spec:** `docs/specs/2026-11-16-details-similar-rows-design.md`, approved by the owner
on 2026-11-18. Read it before any task. It is a point-in-time record, so never edit it.

## Decisions this plan makes

1. **The kind is `seed_row`, appended last in `JobKind`, with no migration.** `jobs.kind`
   is a 32-character text column with no CHECK on its values.
2. **`SeedRowService.build_if_due(title_id)` holds the handler's two checks.** It reads
   the title and the record, and returns `None` when the title is gone or nothing is due.
   *Why:* the recipe is private to the service, and the scheduled job already asks the
   same question through `due_seeds`.
3. **The event is `title.updated` with `data={"fields": ["similar"]}`, sent only when
   this job stored a record.** A job that found nothing due sends nothing. The page's own
   re-read covers that case.
4. **The section's cards are hydrated by `BecauseYouWatchedRow.build`.** One code path
   decides posters, owned marks and dropped titles for both screens. The home row's
   private `_MAX_CARDS` becomes the public `FALLBACK_CARDS`, read by both.
5. **The shelf's types live in `domain/rows.py`** (`SimilarSource`, `SimilarShelf`),
   beside `RowCard` and `BuiltRow`.
6. **The route takes the household through `RowContextDep`.** That context already
   carries the default user, the way the home route gets it, so the route names no
   separate `DefaultUserIdDep`.
7. **`get_similarity_service` and `SimilarityServiceDep` are deleted** once the route no
   longer uses them. `SimilarityService` stays, because `usher similar` uses it.
8. **Two fixture ids are renamed, values unchanged:** `TITLE_SIMILAR_NEVER` becomes
   `TITLE_SIMILAR_FINDING` and `TITLE_SIMILAR_STALE` becomes `TITLE_SIMILAR_UPDATING`.
   The old names would describe states the API no longer has.
9. **The console lands in two tasks.** Task 4 changes the shape and shows posters. Task 5
   adds the waiting behaviour. Each leaves the console gate green.
10. **Wording.** The marker reads `updating`. No cards while waiting reads
    `Finding similar titles for this one.` with `meta` `building: true`. No cards
    otherwise reads `Nothing similar to show.` with `meta` `cards: []`.

## Global Constraints

- The section shows at most **20** cards, from the stored row or from `title_neighbors`.
- A build is queued only when the record is due by the existing `is_due` (30-day rebuild,
  7-day hold after a failed attempt), the title is at or past `EnrichmentState.ENRICHED`,
  and `USHER_LLM_ENABLED` is true.
- The job is `JobPriority.DEMAND`, keyed on `str(title.id)`, concurrency **1**.
- Any `UsherPortError` out of a build becomes `JobDeferred` with `retry_after=300.0`.
- The page re-reads every **5 000 ms** for at most **60 000 ms** from the first read that
  said `building`. At 60 seconds the re-reads stop and the marker goes.
- The response is `{cards, source, building}`. `neighbors`, `score`, `computed_at` and
  `stale` are removed. An unknown title is `404`; no cards is `200`.
- `SEED_ROW` is claimed by a worker exactly when `CURATE` is: only with an `LLMClient`.
- Watched marks are read per request and never stored.
- The home screen's rows, its seed choice and `seed_rows.build` do not change.
- The PRD changes in the same commit as the behaviour it describes.
- `web/src/api/schema.d.ts` is generated, never edited by hand.
- TDD: the failing test first. Domain models are frozen; change them with `.evolve()`.
  Ports are `abc.ABC`. Use `uv` for every Python command.
- Never undo with `git checkout <path>`, `restore`, `stash` or `reset`.
- The Python gate, before every commit that touches Python: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy src tests`, `uv run lint-imports`,
  `uv run pytest tests/unit` (the whole suite, with Docker, in Task 6).
- The console gate, from `web/`, before every commit that touches `web/`:
  `npm run verify`, `npm run e2e`, `npm run e2e:visual`.
- Commit subjects follow the repo: `<area>: <one sentence>`. No PR is opened and nothing
  is pushed by a task. Merges are the owner's.

## Review Focus

Each line names a condition the spec implies, the behaviour a person would expect, and
the task whose tests pin it.

1. **The model is down for longer than five attempts.** The job is still pending with no
   attempt spent, and it never parks. (Task 1, the six-pass worker case.)
2. **The scheduled job builds the row first.** The queued job asks for no completion and
   sends no event, and the page still swaps. (Task 1 `build_if_due`; Task 5 re-read.)
3. **No event ever reaches the page.** The section swaps on a re-read within 5 seconds of
   the build finishing. (Task 5.)
4. **The build never finishes** (the worker is off, or the model stays down). The marker
   goes at 60 seconds, the requests stop, and the list stays. (Task 5.)
5. **The page of an unenriched title is opened.** Nothing is queued and no marker shows.
   The read after enrichment queues the build. (Task 2.)

## File map

| File | Change |
|---|---|
| `src/usher/domain/jobs.py` | `JobKind.SEED_ROW` |
| `src/usher/services/jobs.py` | `KIND_CONCURRENCY` entry; a docstring's count |
| `src/usher/services/seed_rows.py` | `SeedRowService.build_if_due` |
| `src/usher/services/handlers.py` | `seed_row_handler`, `SEED_ROW_RETRY_SECONDS` |
| `src/usher/composition.py` | `worker_kinds`, `_worker_handlers`, `build_seed_row_service` |
| `src/usher/domain/rows.py` | `SimilarSource`, `SimilarShelf` |
| `src/usher/services/rows/because_you_watched.py` | `_MAX_CARDS` becomes `FALLBACK_CARDS` |
| `src/usher/services/similar_shelf.py` | new: `SimilarShelfService` |
| `src/usher/api/dto/similar.py` | `SimilarResponse` rewritten; `SimilarTitleResponse` removed |
| `src/usher/api/deps.py` | `get_similar_shelf_service`; the old similarity dependency removed |
| `src/usher/api/routers/titles.py` | the route |
| `docs/prd/01`, `05`, `06`, `07` | the behaviour, in the commits that change it |
| `web/src/api/schema.d.ts`, `hooks.ts` | regenerated types; `useSimilar` |
| `web/src/features/viewer/TitleDetail.tsx` | `SimilarSection` |
| `web/src/features/operator/Pipeline.tsx` | the tenth kind in the vocabulary table |
| `web/src/test/fixtures/{ids,titles,home}.ts`, `web/src/test/handlers.ts` | fixtures |
| `web/CONVENTIONS.md` | two departures from the handoff |

---

### Task 1: The `seed_row` job

**Files:**
- Modify: `src/usher/domain/jobs.py`, `src/usher/services/jobs.py`,
  `src/usher/services/seed_rows.py`, `src/usher/services/handlers.py`,
  `src/usher/composition.py`
- Modify: `docs/prd/01-architecture.md` (concurrency table),
  `docs/prd/07-client-api.md` (the `title.updated` row),
  `dashboards/alerts/usher.yml` (two comments: "all nine kinds", "each of the nine") and
  `dashboards/README.md` ("Depth is always nine series"). These are present-tense counts.
  The dated measurements beside them (the 2026-09-11 table, the message in
  `tests/unit/test_alerts.py`) record what was measured and stay as they are.
- Test: `tests/unit/test_services_seed_rows.py`, `tests/unit/test_services_handlers.py`,
  `tests/unit/test_services_jobs.py`, `tests/unit/test_domain_jobs.py`,
  `tests/unit/test_config.py`, `tests/unit/test_composition.py`

**Interfaces:**
- Consumes: `SeedRowService.build(seed: Title) -> SeedRow`, `is_due(row, *, recipe, now)`,
  `JobDeferred(message, *, retry_after: float)`, `_title_id(job)` in `handlers.py`,
  `ClientEvent`, `ClientEventKind.TITLE_UPDATED`, `EventPublisher` (`usher.ports.events`).
- Produces:

```python
# src/usher/domain/jobs.py, appended after BOOTSTRAP
SEED_ROW = "seed_row"

# src/usher/services/jobs.py
KIND_CONCURRENCY[JobKind.SEED_ROW] == 1

# src/usher/services/seed_rows.py
class SeedRowService:
    async def build_if_due(self, title_id: uuid.UUID) -> SeedRow | None: ...

# src/usher/services/handlers.py
SEED_ROW_RETRY_SECONDS: Final = 300.0
def seed_row_handler(service: SeedRowService, events: EventPublisher) -> Handler: ...

# src/usher/composition.py
def build_seed_row_service(
    pipeline: Pipeline, settings: Settings, client: LLMClient, provider: MetadataProvider | None
) -> SeedRowService: ...
```

- [ ] **Step 1: Write the failing `build_if_due` tests** in
  `tests/unit/test_services_seed_rows.py`, with that file's `_World`, `_built` and
  `_a_seed_with_three_lists` helpers.

```python
async def test_build_if_due_builds_a_title_that_has_no_record() -> None:
    # world + a seed with three lists + a client holding one usable body
    row = await service.build_if_due(seed.id)
    assert row is not None
    assert row.attempt_outcome is SeedRowOutcome.BUILT
    assert (await world.rows.list_for([seed.id]))[seed.id] == row
    # premise for the next case: exactly one completion was asked for here


async def test_build_if_due_asks_for_nothing_when_the_record_is_not_due() -> None:
    # the record is `_built(...)`, one day old, under this service's recipe
    assert await service.build_if_due(seed.id) is None
    # the client was asked for zero completions and the ledger holds zero rows


async def test_build_if_due_is_none_for_a_title_the_catalog_no_longer_holds() -> None:
    assert await service.build_if_due(new_id()) is None
    # zero completions


@pytest.mark.parametrize("failure", [PortUnavailable("down"), PortRateLimited("slow")])
async def test_build_if_due_lets_an_outage_out_with_no_record_saved(
    failure: UsherPortError,
) -> None:
    with pytest.raises(type(failure)):
        await service.build_if_due(seed.id)
    assert await world.rows.list_for([seed.id]) == {}
```

- [ ] **Step 2: Run them.** `uv run pytest tests/unit/test_services_seed_rows.py -k build_if_due`
  Expected: FAIL, `SeedRowService` has no attribute `build_if_due`.

- [ ] **Step 3: Implement `build_if_due`.** One `self._titles.get`, one
  `self._rows.list_for([title_id])`, `is_due(record, recipe=self._recipe, now=self._now())`,
  then `self.build(title)`. It catches nothing.

- [ ] **Step 4: Run them.** Expected: PASS.

- [ ] **Step 5: Write the failing kind, handler and wiring tests.**

`tests/unit/test_domain_jobs.py`: rename `test_the_nine_kinds_this_tree_ships` to
`test_the_ten_kinds_this_tree_ships` and add `JobKind.SEED_ROW`; add `"seed_row"` to the
stored-value set.

`tests/unit/test_config.py`, beside the `CURATE` line:

```python
assert KIND_CONCURRENCY[JobKind.SEED_ROW] == 1, "one row's completion at a time"
```

`tests/unit/test_composition.py`:

```python
def test_the_seed_row_kind_is_claimed_exactly_when_there_is_a_client() -> None:
    with_client = worker_kinds(provider=None, embedder=None, client=FakeLLMClient())
    without = worker_kinds(provider=FakeMetadataProvider(), embedder=FakeEmbedder(), client=None)
    assert JobKind.SEED_ROW in with_client
    assert JobKind.SEED_ROW not in without
```

`tests/unit/test_services_handlers.py`, with a recording double shaped like that file's
`_RecordingCuration` (`build_if_due` records the id, then returns or raises what it was
built with) and `FakeEventPublisher`:

```python
async def test_the_seed_row_handler_builds_its_key_and_says_the_title_changed() -> None:
    await seed_row_handler(service, events)(Job(kind=JobKind.SEED_ROW, key=str(title_id)))
    assert service.asked == [title_id]
    assert events.published == [
        ClientEvent(
            kind=ClientEventKind.TITLE_UPDATED, title_id=title_id, data={"fields": ["similar"]}
        )
    ]


async def test_the_seed_row_handler_says_nothing_when_nothing_was_built() -> None:
    # the double returns None
    assert service.asked == [title_id]
    assert events.published == []


@pytest.mark.parametrize(
    "failure",
    [PortUnavailable("down"), PortRateLimited("slow"), PortAuthFailed("no"), PortDataMalformed("x")],
)
async def test_an_outage_defers_the_build_five_minutes_and_says_nothing(
    failure: UsherPortError,
) -> None:
    with pytest.raises(JobDeferred) as caught:
        await seed_row_handler(service, events)(Job(kind=JobKind.SEED_ROW, key=str(title_id)))
    assert caught.value.retry_after == 300.0
    assert caught.value.__cause__ is failure
    assert events.published == []


async def test_a_seed_row_key_that_is_not_a_uuid_parks() -> None:
    with pytest.raises(PortDataMalformed):
        await seed_row_handler(service, events)(Job(kind=JobKind.SEED_ROW, key="not-a-uuid"))
    assert service.asked == []
```

`tests/unit/test_services_jobs.py`, a `JobWorker` over `FakeJobQueue(max_attempts=5)`
built the way that file's existing cases build one, registered for `{JobKind.SEED_ROW: 1}`:

```python
async def test_an_outage_never_parks_a_seed_row_job() -> None:
    # handler = seed_row_handler(<a service whose build_if_due raises PortUnavailable>, events)
    began = datetime.now(UTC)
    for _ in range(6):                      # one more pass than max_attempts
        await worker.run_once()
        (job,) = queue.jobs_of(JobKind.SEED_ROW)
        assert job.run_after is not None and job.run_after - began >= timedelta(seconds=300)
        await queue.clear_backoff()
    (job,) = queue.jobs_of(JobKind.SEED_ROW)
    assert job.status is JobStatus.PENDING
    assert job.attempts == 0
    assert await queue.parked() == []


async def test_the_same_six_passes_park_a_job_whose_handler_fails_outright() -> None:
    # the premise arm: the handler raises PortUnavailable itself, with no deferral
    assert len(await queue.parked()) == 1
```

- [ ] **Step 6: Run them.**
  `uv run pytest tests/unit/test_domain_jobs.py tests/unit/test_config.py tests/unit/test_composition.py tests/unit/test_services_handlers.py tests/unit/test_services_jobs.py`
  Expected: FAIL on `JobKind.SEED_ROW` and `seed_row_handler`.

- [ ] **Step 7: Implement.**
  - `JobKind.SEED_ROW` and its `KIND_CONCURRENCY` entry.
  - `seed_row_handler`: `_title_id(job)` outside the `try`, so a bad key parks.
    `except UsherPortError as exc: raise JobDeferred(str(exc), retry_after=SEED_ROW_RETRY_SECONDS) from exc`.
    Publish only when `build_if_due` returned a row.
  - `build_seed_row_service` over `pipeline.titles`, `pipeline.embeddings`,
    `pipeline.watch_states`, `pipeline.seed_rows`, `pipeline.llm_calls`,
    `commit=pipeline.commit`, `model=settings.llm_model`, the wall clock.
  - `worker_kinds` adds `SEED_ROW` under `client is not None`. `_worker_handlers`
    registers `seed_row_handler(build_seed_row_service(pipeline, settings, client, provider), events)`
    under the same guard.
  - Correct the counts: `JobWorker.registered_kinds`' docstring ("Five of the ten kinds",
    naming `SEED_ROW` beside `CURATE`) and the three present-tense dashboard counts
    named under Files.

- [ ] **Step 8: Update the PRD.**
  - `docs/prd/01-architecture.md`: add the row
    `| — \`seed_row\` | 1 | one completion at a time |` under `curate`.
  - `docs/prd/07-client-api.md`, the `title.updated` row: the payload cell becomes
    "Title id + changed fields. `fields` is `["similar"]` when the title's stored
    similar-titles row was built".

- [ ] **Step 9: Run the Python gate.** `test_every_configuration_registers_exactly_the_kinds_it_claims`
  and `test_the_concurrency_table_covers_exactly_the_kinds_a_build_claims` must pass
  unchanged; they prove the kind has a handler in all eight configurations.

- [ ] **Step 10: Commit.** `jobs: a seed_row job builds one title's row and waits out an outage`

---

### Task 2: The shelf read

**Files:**
- Modify: `src/usher/domain/rows.py`, `src/usher/services/rows/because_you_watched.py`
- Create: `src/usher/services/similar_shelf.py`
- Modify: `tests/unit/rows.py` (`Library.title` gains a keyword
  `enrichment_state: EnrichmentState | None = None`; `None` keeps today's behaviour)
- Test: `tests/unit/test_services_similar_shelf.py` (new)

**Interfaces:**
- Consumes: `JobKind.SEED_ROW` (Task 1); `is_due` (`usher.services.seed_rows`);
  `seed_row_recipe(*, model: str) -> str`; `BecauseYouWatchedRow(seed_id, seed_name,
  neighbours, *, semantic, played)` and its `build(ctx) -> BuiltRow`; `RowContext`;
  `JobQueue.enqueue`, `JobRequest` (`usher.ports.jobs`); `ENRICHMENT_RANK`;
  `current_traceparent()` (`usher.telemetry`).
- Produces:

```python
# src/usher/domain/rows.py
class SimilarSource(StrEnum):
    ROW = "row"
    NEIGHBORS = "neighbors"

class SimilarShelf(DomainModel):
    cards: tuple[RowCard, ...] = ()
    source: SimilarSource
    building: bool

# src/usher/services/rows/because_you_watched.py
FALLBACK_CARDS = 20          # was _MAX_CARDS; in __all__

# src/usher/services/similar_shelf.py
class SimilarShelfService:
    def __init__(self, *, ctx: RowContext, queue: JobQueue, recipe: str | None) -> None: ...
    async def read(self, title_id: uuid.UUID) -> SimilarShelf | None: ...
```

`recipe` is `None` for a deployment with no model. `read` answers `None` for an unknown
title.

- [ ] **Step 1: Write the failing tests**, over `Library` from `tests/unit/rows.py`,
  `FakeJobQueue`, and the `_store` and `_neighbours` helpers' shapes from
  `tests/unit/test_rows_because_you_watched.py`. `RECIPE = seed_row_recipe(model="m")`.

```python
async def test_a_stored_row_is_served_in_stored_order_with_poster_owned_and_watched() -> None:
    # 8 card titles; one has library.artwork(...); one is library.finished(...);
    # one is title(owned=False). Stored in REVERSE creation order.
    # The seed also has title_neighbors naming two other titles.
    assert stored != sorted(stored)                       # premise: not insertion order
    shelf = await service.read(seed)
    assert shelf.source is SimilarSource.ROW
    assert [card.title_id for card in shelf.cards] == stored
    assert {c.title_id: c.artwork for c in shelf.cards} == {<poster card>: image_id, **others None}
    assert {c.title_id for c in shelf.cards if c.played} == {watched}
    assert {c.title_id for c in shelf.cards if not c.owned} == {unowned}


async def test_with_no_stored_row_the_neighbours_are_served_in_rank_order_up_to_twenty() -> None:
    # 21 neighbours, ranked in reverse creation order
    assert len(await library.neighbors.list_for(seed, limit=50)) == 21      # premise
    shelf = await service.read(seed)
    assert shelf.source is SimilarSource.NEIGHBORS
    assert [card.title_id for card in shelf.cards] == ranked[:20]


async def test_a_record_holding_only_a_failed_attempt_serves_the_neighbours() -> None:
    assert shelf.source is SimilarSource.NEIGHBORS and len(shelf.cards) == 2


async def test_a_stored_card_the_catalog_no_longer_holds_is_dropped_in_place() -> None:
    # 8 stored ids, the third never saved as a title
    assert [card.title_id for card in shelf.cards] == stored[:2] + stored[3:]


async def test_an_unknown_title_is_none_and_queues_nothing() -> None:
    assert await service.read(new_id()) is None
    assert queue.jobs_of(JobKind.SEED_ROW) == []
```

The queue rule, one parametrised case. Each row builds its own title and record:

| record | title state | `recipe` | `building` | jobs |
|---|---|---|---|---|
| none | `ENRICHED` | `RECIPE` | True | 1 |
| none | `ENRICHED` | `None` | False | 0 |
| none | the state just below `ENRICHED` | `RECIPE` | False | 0 |
| built 1 day ago under `RECIPE` | `ENRICHED` | `RECIPE` | False | 0 |
| built 31 days ago under `RECIPE` | `ENRICHED` | `RECIPE` | True | 1 |
| built 1 day ago under another recipe | `ENRICHED` | `RECIPE` | True | 1 |
| `TOO_FEW` attempt 1 day ago, no cards | `ENRICHED` | `RECIPE` | False | 0 |
| `TOO_FEW` attempt 8 days ago, no cards | `ENRICHED` | `RECIPE` | True | 1 |

```python
async def test_a_build_is_queued_exactly_when_due_enriched_and_a_model_is_configured(...) -> None:
    shelf = await service.read(title_id)
    assert shelf.building is building
    queued = queue.jobs_of(JobKind.SEED_ROW)
    assert len(queued) == jobs
    if jobs:
        assert queued[0].key == str(title_id)
        assert queued[0].priority == JobPriority.DEMAND


async def test_a_31_day_old_row_is_still_served_while_its_rebuild_is_queued() -> None:
    assert shelf.source is SimilarSource.ROW and shelf.building is True


async def test_a_second_read_while_the_job_waits_still_says_building_and_queues_no_more() -> None:
    first, second = await service.read(seed), await service.read(seed)
    assert first.building is True and second.building is True
    assert len(queue.jobs_of(JobKind.SEED_ROW)) == 1


async def test_an_unenriched_title_queues_on_the_read_after_it_is_enriched() -> None:
    assert (await service.read(seed)).building is False
    assert queue.jobs_of(JobKind.SEED_ROW) == []
    # save the same title evolved to EnrichmentState.ENRICHED
    assert (await service.read(seed)).building is True
    assert len(queue.jobs_of(JobKind.SEED_ROW)) == 1


async def test_the_cards_cost_one_catalog_read_one_ownership_read_and_one_artwork_read() -> None:
    # `_CountingTitles` and `_CountingCopies` as tests/unit/test_rows_curated.py defines
    # them, through Library(titles=..., media_items=...). A third subclass of
    # FakeImageRepository tallies `primary_for_titles`; assign it to `library.images`
    # straight after constructing the Library, before any `library.artwork(...)`.
    # 8 stored cards, one with a poster.
    assert len(shelf.cards) == 8                           # premise: the reads happened
    assert catalog.catalog_reads == 1
    assert copies.ownership_reads == 1
    assert images.artwork_reads == 1
```

- [ ] **Step 2: Run them.** `uv run pytest tests/unit/test_services_similar_shelf.py`
  Expected: FAIL, no module `usher.services.similar_shelf`.

- [ ] **Step 3: Implement.**
  - The two domain types, exported in `domain/rows.py`'s `__all__`.
  - Rename `_MAX_CARDS` to `FALLBACK_CARDS` and export it.
  - `read`: `ctx.titles.get`; `ctx.seed_rows.list_for([title_id])`; the record's cards,
    else `ctx.neighbors.list_for(title_id, limit=FALLBACK_CARDS)`; one
    `ctx.watch_states.played_title_ids(ctx.user.id, ids)` when there are ids; then
    `(await BecauseYouWatchedRow(title.id, title.name, ids, semantic=True, played=...).build(ctx)).cards`.
  - `building = recipe is not None and enriched and is_due(record, recipe=recipe, now=ctx.now())`,
    with `enriched` the `ENRICHMENT_RANK` comparison `TitleReadService._promote` makes.
  - When `building`, enqueue one `JobRequest(kind=JobKind.SEED_ROW, key=str(title.id),
    priority=JobPriority.DEMAND, traceparent=current_traceparent())`. The service never
    commits; the request's session does.

- [ ] **Step 4: Run them, then the Python gate.** Expected: PASS. `uv run lint-imports`
  must keep every contract.

- [ ] **Step 5: Commit.** `similar: one read picks a title's stored row or its neighbours and queues the build`

---

### Task 3: The route answers cards

**Files:**
- Modify: `src/usher/api/dto/similar.py`, `src/usher/api/deps.py`,
  `src/usher/api/routers/titles.py`
- Modify: `docs/prd/05-search-and-similarity.md`, `docs/prd/06-rows-and-recommendations.md`,
  `docs/prd/07-client-api.md`
- Test: `tests/unit/test_api_similar.py` (rewritten),
  `tests/integration/test_similar_route.py` (restated),
  `tests/integration/test_pipeline_deps.py` (the provider map names the old dependency)

**Interfaces:**
- Consumes: `SimilarShelfService`, `SimilarShelf`, `SimilarSource` (Task 2);
  `RowCardResponse.of(card)`; `RowContextDep`, `JobQueueDep`, `SettingsDep`.
- Produces:

```python
# src/usher/api/dto/similar.py
class SimilarResponse(BaseModel):
    cards: list[RowCardResponse]
    source: SimilarSource
    building: bool

    @classmethod
    def of(cls, shelf: SimilarShelf) -> Self: ...

# src/usher/api/deps.py
def get_similar_shelf_service(
    ctx: RowContextDep, queue: JobQueueDep, settings: SettingsDep
) -> SimilarShelfService: ...
SimilarShelfServiceDep = Annotated[SimilarShelfService, Depends(get_similar_shelf_service)]

# src/usher/api/routers/titles.py
async def get_similar_titles(title_id: uuid.UUID, shelf: SimilarShelfServiceDep) -> SimilarResponse
```

`get_similar_shelf_service` passes `recipe=seed_row_recipe(model=settings.llm_model)`
when `settings.llm_enabled`, else `None`.

- [ ] **Step 1: Rewrite `tests/unit/test_api_similar.py`.** The app is built as the file
  builds it today. It overrides `get_similar_shelf_service` with a real
  `SimilarShelfService` over `Library().context()` and a `FakeJobQueue`.

```python
async def test_the_body_is_cards_source_and_building_and_nothing_else(...) -> None:
    assert set(body) == {"cards", "source", "building"}
    assert all(set(card) == set(RowCardResponse.model_fields) for card in body["cards"])


async def test_a_stored_row_arrives_in_stored_order_with_its_poster_and_watched_mark(...) -> None:
    assert body["source"] == "row"
    assert [card["title_id"] for card in body["cards"]] == [str(one) for one in stored]
    assert by_id[str(poster_card)]["artwork"] == str(image_id)
    assert by_id[str(watched)]["played"] is True


async def test_a_due_enriched_title_reads_building_over_its_neighbours(...) -> None:
    assert body["source"] == "neighbors" and body["building"] is True


async def test_a_title_with_nothing_similar_is_200_with_no_cards(...) -> None:
    assert response.status_code == 200
    assert body == {"cards": [], "source": "neighbors", "building": False}
```

Keep `test_an_unknown_title_is_a_404_in_prd_07s_envelope`, pointed at the new override.
Keep the AST guard as `test_the_route_holds_no_model_and_no_source_adapter`, with
`LLMClient` added to the names it refuses. Delete the stale, rank, and `computed_at`
cases; Task 2's tests own order now.

- [ ] **Step 2: Restate `tests/integration/test_similar_route.py`** through the real
  wiring, with `worker_enabled=False`.

```python
async def test_the_route_serves_a_stored_row_with_a_poster_through_the_real_wiring(...) -> None:
    # real rows: a seed, 8 card titles, a seed_rows record saved through
    # PostgresSeedRowRepository in reverse creation order, one servable poster image
    assert body["source"] == "row"
    assert [card["title_id"] for card in body["cards"]] == [str(one) for one in stored]
    assert by_id[str(poster_card)]["artwork"] is not None


async def test_no_cards_is_200_and_an_unknown_title_is_404(...) -> None: ...


async def test_without_a_model_the_route_writes_nothing(...) -> None:
    # llm_enabled=False; an enriched title with no record
    assert writes == 0 and body["building"] is False


async def test_with_a_model_a_due_title_queues_exactly_one_seed_row_job(...) -> None:
    # llm_enabled=True (Settings requires nothing else for it); the same title shape.
    # worker_enabled=False, so nothing claims the job and no completion is made.
    assert body["building"] is True
    assert writes == 1
    assert <one jobs row: kind 'seed_row', key str(title.id), priority 100, status 'pending'>
    # the route commits for real: delete that jobs row in the test's own cleanup
```

- [ ] **Step 3: Run both files.** Expected: FAIL on the missing dependency and fields.

- [ ] **Step 4: Implement.** The DTO, the dependency, and the route: `await shelf.read(title_id)`,
  the existing `ProblemException` 404 when it is `None`, else `SimilarResponse.of(...)`.
  Rewrite the route's docstring to say what it reads and that it may queue one job.
  Delete `get_similarity_service` and `SimilarityServiceDep` if `grep` finds no other
  user in `src/`. In `tests/integration/test_pipeline_deps.py`, replace the
  `"similarity_service": get_similarity_service` entry of `_PROVIDERS` with
  `"similar_shelf_service": get_similar_shelf_service` and rewrite its comment. The
  built class is `SimilarShelfService`, so the name check's `"Similarity"` prefix becomes
  `"Similar"`. Run that file with Docker up.

- [ ] **Step 5: Update the PRD.**
  - `05`, "Similarity": the first paragraph becomes two facts. `GET /titles/{id}/similar`
    answers the title's stored row from `seed_rows` when it holds cards, the same record
    and order as the home screen's row ([06]), and the title's `title_neighbors`
    otherwise, up to 20 cards. `title_neighbors` blends embedding cosine with Jaccard over
    keywords and genres, precomputed offline. The weight table stays.
  - `06`, after the paragraph that introduces `seed_rows.build`: reading
    `GET /titles/{id}/similar` for an enriched title whose record is due also queues one
    `seed_row` job for that title when `USHER_LLM_ENABLED` is true, at `DEMAND` priority,
    one at a time. It writes the same record under the same rules. A model or provider
    outage defers it five minutes at a time and spends no attempt.
  - `07`, the route's row: "Up to 20 cards in the home screen's card shape, from the
    title's stored row (`source: row`) or `title_neighbors` (`source: neighbors`).
    `building` is true while a build for the title is queued".
  - `grep -rn similar docs/guide` and correct any sentence this makes false.

- [ ] **Step 6: Run the Python gate, with `uv run pytest tests/integration/test_similar_route.py`,
  and `uv run pytest tests/unit/test_docs_pointers.py tests/unit/test_docs_currency.py`.**

- [ ] **Step 7: Commit.** `api: a title's similar section is its stored row, as home cards`

---

### Task 4: The console shows the cards

**Files:**
- Regenerate: `web/src/api/schema.d.ts`
- Modify: `web/src/api/hooks.ts` (the comment on `useSimilar`),
  `web/src/features/viewer/TitleDetail.tsx`, `web/src/features/operator/Pipeline.tsx`,
  `web/src/test/fixtures/ids.ts`, `titles.ts`, `home.ts` (header comment),
  `web/src/test/handlers.ts`, and every file that imports the two renamed ids
- Modify: `web/CONVENTIONS.md`
- Test: `web/src/api/hooks.test.ts`, `web/src/features/viewer/TitleDetail.test.tsx`,
  `web/src/features/operator/Pipeline.test.tsx`; visual baselines under `web/e2e/`

**Interfaces:**
- Consumes: the route's body (Task 3); `PosterCard`'s `card: RowCard` prop.
- Produces: `SimilarResponse` is `{ cards: RowCard[]; source: 'row' | 'neighbors';
  building: boolean }`. Fixtures `similarRow`, `similarUpdating`, `similarFinding`,
  `similarEmpty`. Ids `TITLE_SIMILAR_FINDING`, `TITLE_SIMILAR_UPDATING`,
  `TITLE_SIMILAR_EMPTY`.

- [ ] **Step 1: Regenerate the types** from this branch's own document. No server is
  needed:

```bash
uv run python -c "import json, pathlib; from usher.api.app import create_app; from usher.config import Settings; pathlib.Path('/var/tmp/usher-openapi.json').write_text(json.dumps(create_app(Settings(database_url='postgresql+asyncpg://u:p@localhost/x', secret_key='x'*32, push_enabled=False, worker_enabled=False)).openapi()))"
cd web && npx -y --package=typescript@5.9 --package=openapi-typescript@7 openapi-typescript /var/tmp/usher-openapi.json -o src/api/schema.d.ts
git diff --stat src/api/schema.d.ts
```

The script writes the file itself because the process logs to stdout. Run on the tree
before Task 1, these two commands reproduce the committed `schema.d.ts` byte for byte.

Expected: the diff touches only `SimilarResponse`, the removed `SimilarTitleResponse`,
the new `SimilarSource`, and `JobKind` gaining `seed_row`. Any other hunk means the
document differs from the one the file was last generated from: stop, and regenerate
against a running server as `web/README.md` says.

- [ ] **Step 2: Fixtures.**
  - `ids.ts`: rename the two ids (values unchanged) across `web/src` and rewrite the
    three doc comments: `TITLE_SIMILAR_FINDING` has no cards and a build under way,
    `TITLE_SIMILAR_UPDATING` shows the old list with a build under way,
    `TITLE_SIMILAR_EMPTY` has no cards and no build.
  - `titles.ts`: `similarRow` (`source: 'row'`, `building: false`, the same three titles
    in the same order as today's `similarComputed`, as full `RowCard`s: Solaris with
    `artwork: IMAGE_POSTER`, The Mirror `played: true`, Andrei Rublev `artwork: null`);
    `similarUpdating` (`source: 'neighbors'`, `building: true`, Stalker and Solaris);
    `similarFinding` (`cards: []`, `source: 'neighbors'`, `building: true`);
    `similarEmpty` (`cards: []`, `source: 'neighbors'`, `building: false`).
  - `handlers.ts`: the switch answers those four.
  - `home.ts`: the header comment no longer lists similarity among the DTOs with no
    artwork.

- [ ] **Step 3: Write the failing tests.**

`hooks.test.ts`, replacing "useSimilar and its three absent states":

```ts
describe('useSimilar', () => {
  it('reads the stored row as home cards', async () => {
    expect(result.current.data?.source).toBe('row')
    expect(result.current.data?.cards.map((card) => card.name)).toEqual([
      'Solaris', 'The Mirror', 'Andrei Rublev',
    ])
    expect(result.current.data?.cards[0]?.artwork).toBe(IMAGE_POSTER)
  })
  it('tells no cards with a build under way from no cards at all', async () => {
    expect(finding.result.current.data).toEqual({ cards: [], source: 'neighbors', building: true })
    expect(empty.result.current.data).toEqual({ cards: [], source: 'neighbors', building: false })
  })
})
```

`TitleDetail.test.tsx`: delete the three stale and never-computed cases. Keep "offers no
similarity reason". Add:

```ts
it('renders the stored row as poster cards in the order the API gave', async () => {
  const similar = await screen.findByRole('region', { name: 'Similar titles' })
  expect(within(similar).getAllByRole('button').map((b) => b.getAttribute('aria-label') ?? b.textContent))
    .toEqual([expect.stringMatching(/Solaris, 1972/), expect.stringMatching(/The Mirror, 1975/),
              expect.stringMatching(/Andrei Rublev, 1966/)])
  // Solaris's poster and The Mirror's watched mark, asserted the way Home.test.tsx
  // asserts a card's poster and its played state
  expect(within(similar).queryByText('stale')).not.toBeInTheDocument()
})

it('says there is nothing similar when there are no cards', async () => {
  renderTitle(TITLE_SIMILAR_EMPTY)
  expect(await screen.findByText('Nothing similar to show.')).toBeInTheDocument()
  expect(screen.getByText('cards: []')).toBeInTheDocument()
})
```

`Pipeline.test.tsx`: the case becomes "shows the ten job kinds…" and its list gains
`'seed_row'` last.

- [ ] **Step 4: Run them.** `npm test -- hooks TitleDetail Pipeline` from `web/`.
  Expected: FAIL.

- [ ] **Step 5: Implement.**
  - `SimilarSection`: `query.data.cards.map((card) => <PosterCard key={card.title_id}
    card={card} onOpen={() => onOpenTitle(card.title_id)} />)`. No cards renders
    `<StateBlock kind="empty" meta="cards: []">Nothing similar to show.</StateBlock>`.
    Remove the stale badge, both notes and the `computed_at` states. Rewrite the
    section's doc comment and `useSimilar`'s to describe the new body. `building` is not
    rendered until Task 5.
  - `Pipeline.tsx`: `{ kind: 'seed_row', does: 'Build one title’s “Because you watched” row.' }`
    last in `JOB_KINDS`; the caption and both comments say ten.
  - `web/CONVENTIONS.md`, under "Departures from the handoff": similar titles carry
    posters, watched marks and owned marks, because the route now answers home cards;
    and the section has no never-computed or stale state, because its list is the stored
    row or the old list and the API reports neither age.

- [ ] **Step 6: Run the console gate from `web/`.** `npm run verify`, `npm run e2e`,
  `npm run e2e:visual`. A visual difference is expected only inside the "Similar titles"
  rail of the title screens. Open one reported diff image and confirm that, then
  `npm run e2e:visual:update` and re-run `npm run e2e:visual` to green. A difference
  anywhere else is a defect.

- [ ] **Step 7: Commit.** `console: similar titles are home cards with posters`

---

### Task 5: The console waits for a build

**Files:**
- Modify: `web/src/api/hooks.ts`, `web/src/features/viewer/TitleDetail.tsx`,
  `web/CONVENTIONS.md`
- Test: `web/src/api/hooks.test.ts`, `web/src/features/viewer/TitleDetail.test.tsx`

**Interfaces:**
- Consumes: `SimilarResponse.building`; the fixtures and ids from Task 4; the existing
  `title.updated` handler in `TitleDetail.tsx`, which already invalidates
  `['similar', titleId]` and is not changed.
- Produces:

```ts
// web/src/api/hooks.ts
export const SIMILAR_REREAD_MS = 5_000
export const SIMILAR_WAIT_MS = 60_000
/** The query, plus `waiting`: a build is under way and the 60 seconds are not up. */
export function useSimilar(id: string | undefined): UseQueryResult<SimilarResponse> & { waiting: boolean }
```

The rule `useSimilar` implements: the wait starts at the first read for this id that says
`building: true`. While waiting, the query re-reads every `SIMILAR_REREAD_MS`. A read
that says `building: false` ends the wait. `SIMILAR_WAIT_MS` after the wait started,
`waiting` turns false on a timer, without a read, and stays false for this id until the
hook is mounted again, whatever later reads say.

- [ ] **Step 1: Write the failing hook tests**, with a request counter on an MSW
  handler and fake timers set the way `Bootstrap.test.tsx`'s "keeps polling for 20 min"
  case sets them: `vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout',
  'setInterval', 'clearInterval', 'Date'], shouldAdvanceTime: true })`, so MSW and
  `findBy*` still settle. Restore real timers in `afterEach`.

```ts
it('does not re-read a section with no build under way', async () => {
  await vi.advanceTimersByTimeAsync(30_000)
  expect(requests).toBe(1)
  expect(result.current.waiting).toBe(false)
})

it('re-reads every 5 seconds and swaps when a read says the row is built', async () => {
  // the handler answers similarUpdating twice, then similarRow
  expect(result.current.waiting).toBe(true)
  await vi.advanceTimersByTimeAsync(5_000)
  expect(requests).toBe(2)
  await vi.advanceTimersByTimeAsync(5_000)
  expect(requests).toBe(3)
  expect(result.current.data?.source).toBe('row')
  expect(result.current.waiting).toBe(false)
  await vi.advanceTimersByTimeAsync(30_000)
  expect(requests).toBe(3)
})

it('stops asking and stops waiting at 60 seconds', async () => {
  // the handler always answers similarUpdating
  await vi.advanceTimersByTimeAsync(59_000)
  expect(result.current.waiting).toBe(true)
  await vi.advanceTimersByTimeAsync(1_000)
  expect(result.current.waiting).toBe(false)
  const asked = requests
  expect(asked).toBeGreaterThanOrEqual(12)          // premise: it did re-read
  await vi.advanceTimersByTimeAsync(60_000)
  expect(requests).toBe(asked)
  expect(result.current.data?.cards.length).toBe(2) // the list it had stays
})

it('does not start waiting again when a later read still says building', async () => {
  // after the 60 seconds: invalidate ['similar', id] once
  expect(requests).toBe(asked + 1)
  expect(result.current.waiting).toBe(false)
  await vi.advanceTimersByTimeAsync(30_000)
  expect(requests).toBe(asked + 1)
})
```

- [ ] **Step 2: Write the failing screen tests** in `TitleDetail.test.tsx`, under
  "similar titles", with the fake `EventSource` the file already installs.

```ts
it('shows the old list with an updating marker while a build is waited on', async () => {
  // TITLE_SIMILAR_UPDATING
  expect(within(similar).getByRole('button', { name: /Solaris, 1972/ })).toBeInTheDocument()
  expect(within(similar).getByText('updating')).toBeInTheDocument()
})

it('says it is finding similar titles when there are none yet', async () => {
  // TITLE_SIMILAR_FINDING
  expect(screen.getByText('Finding similar titles for this one.')).toBeInTheDocument()
  expect(screen.getByText('building: true')).toBeInTheDocument()
  expect(screen.queryByText('Nothing similar to show.')).not.toBeInTheDocument()
})

it('swaps to the built row on a re-read when no event ever arrives', async () => {
  // the handler answers similarUpdating once, then similarRow; no frame is emitted
  await vi.advanceTimersByTimeAsync(5_000)
  expect(await within(similar).findByRole('button', { name: /Andrei Rublev, 1966/ })).toBeInTheDocument()
  expect(within(similar).queryByText('updating')).not.toBeInTheDocument()
})

it('swaps on title.updated without waiting for the re-read', async () => {
  // emit a title.updated frame for this title at 1 000 ms; never reach 5 000 ms
  expect(await within(similar).findByRole('button', { name: /Andrei Rublev, 1966/ })).toBeInTheDocument()
})

it('drops the marker at 60 seconds and keeps the list', async () => {
  await vi.advanceTimersByTimeAsync(60_000)
  expect(within(similar).queryByText('updating')).not.toBeInTheDocument()
  expect(within(similar).getByRole('button', { name: /Solaris, 1972/ })).toBeInTheDocument()
})

it('says nothing similar once 60 seconds pass with no cards', async () => {
  // TITLE_SIMILAR_FINDING
  await vi.advanceTimersByTimeAsync(60_000)
  expect(screen.getByText('Nothing similar to show.')).toBeInTheDocument()
  expect(screen.queryByText('Finding similar titles for this one.')).not.toBeInTheDocument()
})

it('has no axe violations while updating', async () => { await expectNoViolations(container) })
```

- [ ] **Step 3: Run them.** Expected: FAIL.

- [ ] **Step 4: Implement.**
  - `useSimilar` as specified, with `refetchInterval` set only while `waiting`. The
    shape is the watch window `Bootstrap.tsx` already has: a piece of state, a
    `setTimeout` that ends it, and `refetchInterval` given as a function.
  - `SimilarSection` takes `waiting: boolean`. With cards and `waiting`, the head shows
    `<Badge tone="neutral" icon={<Icon name="history" />}>updating</Badge>` in the slot
    the stale badge had. With no cards and `waiting`:
    `<StateBlock kind="never" meta="building: true">Finding similar titles for this one.</StateBlock>`.
    With no cards otherwise: Task 4's empty block.
  - `web/CONVENTIONS.md`, under "Departures from the handoff": while a build is under
    way the section re-reads every 5 seconds for at most 60 seconds, so the swap never
    depends on an event; the window is bounded and the marker goes with it.

- [ ] **Step 5: Run the console gate from `web/`**, with the visual rule from Task 4,
  Step 6.

- [ ] **Step 6: Commit.** `console: similar titles wait for a build, for at most a minute`

---

### Task 6: The whole gate and the status rows (controller)

**Files:**
- Modify: `docs/plans/progress.md`, `docs/prd/README.md`

- [ ] **Step 1: Run the Python gate whole**, Docker up: `uv sync --extra eval`, then all
  six commands in `CLAUDE.md`, with `uv run pytest` unscoped.
- [ ] **Step 2: Run the console gate whole** from `web/`.
- [ ] **Step 3: Mark Tasks 1 to 6 `🔨 in review`** in this plan's table in
  `docs/plans/progress.md`, and this plan's row in `docs/prd/README.md`.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_docs_pointers.py tests/unit/test_docs_currency.py`.
- [ ] **Step 5: Commit.** `docs: the details page's similar titles are in review`

---

### Task 7: Live acceptance (controller, owner-gated)

Runs on production after the owner merges and the deploy is healthy, with the model up.
The driving script lives outside the tree and writes no host, user id, token or
credential into the repo. Results go on the PR as comments.

- [ ] **Step 1: Same row in both places.** For BlackBerry, War Dogs and Spider-Man:
  Homecoming, `GET /titles/{id}/similar` answers `source: row` and its card ids equal the
  title's `seed_rows` cards in stored order.
- [ ] **Step 2: Posters.** On a stored row and on a title served from `neighbors`, every
  card whose title has a servable poster has a non-null `artwork`, and `GET /images/{id}`
  answers 200 for each.
- [ ] **Step 3: First view of an unbuilt title.** Pick an enriched film with no
  `seed_rows` record (a join of `titles` and `seed_rows`; watch history is not read).
  Open its page in the console: the old list with posters and the `updating` marker at
  once, then the swap with no reload. Record the seconds from open to swap.
- [ ] **Step 4: Second view.** Reopen it: the stored row at once, no marker, and the
  count of `llm_calls` rows for the `because_you_watched` purpose unchanged across the
  reopen.
- [ ] **Step 5: Too few cards.** Find or produce a title whose build recorded `too_few`.
  Reopened, it shows the old list, no marker, and no new `seed_row` job.
- [ ] **Step 6: The owner reads** two or three details pages of their choosing and says
  whether the section is right.
- [ ] **Step 7:** Mark every task `✅ landed` in both status tables, in a follow-up commit
  on a fresh branch.

---

## Spec premises, checked against the tree

- `jobs.kind` has no CHECK on its values (`db/models/jobs.py`), so a new kind needs no
  migration.
- A finished job's row is deleted, so asking again after a build re-queues. `enqueue`
  never un-parks a parked job, which is why the handler defers.
- `get_session` commits after the handler, so the route's enqueue is committed by the
  request.
- `SeedRowService.build` settles a ledger row and re-raises on an outage, with no record
  saved, so the record stays due.
- `TitleDetail.tsx` already invalidates `['similar', titleId]` on `title.updated`.
- `PosterCard` takes a `RowCard` and already draws its poster, watched bar and progress.
- `get_row_context` carries the default user and every repository the read needs.
- `Library.context()` reads `library.images` when it is called and nothing else is wired
  to that fake, so a case can swap in a counting subclass.
- `create_app(...).openapi()` needs no database, and the pinned `openapi-typescript`
  turns that document into the committed `schema.d.ts` with no difference.

## Residuals

- **A long outage writes ledger rows, and a TMDb outage spends money.** Each deferred
  attempt settles one `llm_calls` row: 12 an hour for each title whose build is
  waiting. When the model is unreachable the row is a failed call. When TMDb fails,
  `build` has already spent a completion that succeeded, so every retry pays for
  another, with no end for a refused TMDb key. A deferral reschedules the job and parks
  nothing, so neither queue alert sees it. Not guarded; a back-off is a follow-up.
- **The scheduled job and a queued job can build one seed at the same moment.** Both
  build and the later save wins. One extra completion, as the spec accepts.
- **A deployment with a model but no running worker** reads `building: true` on every
  visit to a due title, so the marker shows for 60 seconds each time. The same holds
  for a `seed_row` job that parked or crashed on a defect: the read cannot tell it from
  one that is waiting.
- **`SimilarityService.neighbors_of`, `computed_at` and `stale_neighbors` stay** for
  `usher similar`. Nothing in the API calls them after Task 3.
