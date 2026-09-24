# Stage 1A — Shared Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse three duplicated mechanisms in `src/usher` into one each — the
observable-gauge reader slot, the narrow session scope, and the non-finite float
refusal.

**Architecture:** All three are extractions, not new behaviour. Tasks 1 and 2 are
refactors whose existing tests must stay green before and after; task 3 is real
TDD, because `allow_inf_nan=False` on the base config refuses values three
domain models currently accept.

**Tech Stack:** Python 3.13, pydantic v2, OpenTelemetry SDK, SQLAlchemy 2 async,
pytest, mypy strict.

**Convention:** New prose in this plan follows the convention this milestone is
adopting — module docstrings ≤ 3 lines, function docstrings ≤ 20, comment blocks
≤ 5, and no dates, row counts or measurement narratives. Do not imitate the
surrounding style of the files you are editing.

**Source:** items 1, 32 and 34 of `docs/plans/2026-09-12-comment-convention-and-polish.md`.

---

### Task 1: One reader slot behind all five observable instrument groups

`src/usher/telemetry.py` carries five copies of the same three-part idiom: a
module-global `_x_reader`, a `register_x_gauges` that rebinds it under `global`,
and an observe function whose first act is `if _x_reader is None: return []`.
The rationale comment is pasted five times. `register_scheduler_gauges` — the
fifth copy — has no test of its own.

**Files:**
- Modify: `src/usher/telemetry.py:319-402` (queue), `:425-494` (push), `:497-536` (sse), `:539-596` (scheduler), `:645-734` (search)
- Modify: `tests/unit/test_telemetry_pipeline.py:469`
- Modify: `tests/unit/test_telemetry_push.py:304`
- Modify: `tests/unit/test_telemetry_sse.py:97`
- Modify: `tests/unit/test_telemetry_search.py:787`
- Test: `tests/unit/test_telemetry_scheduler.py` (create)

- [ ] **Step 1: Write the characterisation test for the untested fifth copy**

This is the copy with no coverage. It must pass before the refactor and after —
that is the whole point of writing it first.

Create `tests/unit/test_telemetry_scheduler.py`:

```python
"""The scheduler gauge's reader slot."""

from collections.abc import Iterable

import pytest
from opentelemetry.metrics import CallbackOptions, Observation

from usher import telemetry


def _observations(callback_result: Iterable[Observation]) -> list[tuple[float, str]]:
    return [(one.value, one.attributes["job"]) for one in callback_result]


def test_an_unregistered_scheduler_reader_observes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No reader means no observation. Zero would read as "exactly due"."""
    monkeypatch.setattr("usher.telemetry._scheduler_reader", None)

    assert list(telemetry._observe_job_due(CallbackOptions())) == []


def test_a_registered_reader_observes_one_series_per_job() -> None:
    telemetry.register_scheduler_gauges(lambda: {"retention": -42.0, "rebuild": 7.5})

    observed = _observations(telemetry._observe_job_due(CallbackOptions()))

    assert sorted(observed) == [(-42.0, "retention"), (7.5, "rebuild")]


def test_a_second_registration_replaces_the_first_reader() -> None:
    """The SDK keeps only the first instrument, so the reader must be replaceable."""
    telemetry.register_scheduler_gauges(lambda: {"first": 1.0})
    telemetry.register_scheduler_gauges(lambda: {"second": 2.0})

    observed = _observations(telemetry._observe_job_due(CallbackOptions()))

    assert observed == [(2.0, "second")]
```

- [ ] **Step 2: Run it and confirm it passes against the current code**

Run: `uv run pytest tests/unit/test_telemetry_scheduler.py -v`
Expected: 3 passed. If any fails, stop — the refactor below would then be
changing behaviour rather than preserving it, and that needs a decision.

- [ ] **Step 3: Commit the characterisation test on its own**

```bash
git add tests/unit/test_telemetry_scheduler.py
git commit -m "test(telemetry): the scheduler gauge's reader slot, which had no test"
```

- [ ] **Step 4: Add the slot type**

In `src/usher/telemetry.py`, immediately above `class QueueSnapshot` (currently
line 305), insert:

```python
class _ReaderSlot[T]:
    """A replaceable reader behind an observable instrument.

    The SDK keeps only the first instrument registered under a name, so a second
    registration replaces the reader rather than the instrument. An unset reader
    observes nothing: on every series here a fabricated zero is indistinguishable
    from a real reading, and is the value an alert would act on.
    """

    def __init__(self) -> None:
        self._read: Callable[[], T] | None = None

    def set(self, read: Callable[[], T]) -> None:
        self._read = read

    def observe(self, build: Callable[[T], Iterable[Observation]]) -> Iterable[Observation]:
        return [] if self._read is None else list(build(self._read()))
```

- [ ] **Step 5: Convert the queue group**

Replace lines 321-329 (the comment block and `_queue_reader`) with:

```python
_queue: _ReaderSlot[QueueSnapshot] = _ReaderSlot()
```

Replace the body of `register_queue_gauges` (lines 365-366, the `global` and
assignment) with `_queue.set(read)`. Cut the docstring to:

```python
    """PRD 10's `usher.jobs.queued` / `usher.jobs.parked`, by kind.

    `read` is synchronous and returns the caller's most recent full re-read of
    the `jobs` table, never a query: OTel invokes the callback from the metric
    reader's background thread, where awaiting asyncpg would deadlock. Safe to
    call repeatedly.
    """
```

Replace `_observe_queued`, `_observe_parked` and `_observations` (lines 382-402)
with:

```python
def _observe_queued(options: CallbackOptions) -> Iterable[Observation]:
    return _queue.observe(lambda snapshot: _by_kind(snapshot.queued))


def _observe_parked(options: CallbackOptions) -> Iterable[Observation]:
    return _queue.observe(lambda snapshot: _by_kind(snapshot.parked))


def _by_kind(counts: Mapping[str, int]) -> Iterable[Observation]:
    return [Observation(count, {"kind": kind}) for kind, count in counts.items()]
```

- [ ] **Step 6: Run the queue tests**

Run: `uv run pytest tests/unit/test_telemetry_pipeline.py -v`
Expected: one failure — `test_...` at line 469 monkeypatches
`usher.telemetry._queue_reader`, which no longer exists, so `monkeypatch.setattr`
raises `AttributeError`.

- [ ] **Step 7: Point that test at the slot**

In `tests/unit/test_telemetry_pipeline.py:469`, change:

```python
    monkeypatch.setattr("usher.telemetry._queue_reader", None)
```

to:

```python
    monkeypatch.setattr("usher.telemetry._queue._read", None)
```

- [ ] **Step 8: Run the queue tests again**

Run: `uv run pytest tests/unit/test_telemetry_pipeline.py -v`
Expected: all pass.

- [ ] **Step 9: Convert the push group**

Replace lines 427-432 with `_push: _ReaderSlot[Mapping[str, PushSnapshot]] = _ReaderSlot()`.
Replace the `global`/assignment in `register_push_gauges` with `_push.set(read)`.
Cut its docstring to:

```python
    """PRD 10's `usher.source.push.connected` / `usher.source.push.reconnects`.

    `connected` is a gauge; `reconnects` is an asynchronous counter, because a
    cumulative total read out of a ledger is what that instrument is for.
    Reporting it as a gauge would put the wrong instrument type on the wire
    under a documented name.
    """
```

Replace `_observe_push_connected`, `_observe_push_reconnects` and
`_push_observations` (lines 471-494) with:

```python
def _observe_push_connected(options: CallbackOptions) -> Iterable[Observation]:
    return _push.observe(lambda lanes: _by_source(lanes, lambda one: 1 if one.delivering else 0))


def _observe_push_reconnects(options: CallbackOptions) -> Iterable[Observation]:
    return _push.observe(lambda lanes: _by_source(lanes, lambda one: one.reconnects))


def _by_source(
    lanes: Mapping[str, PushSnapshot], select: Callable[[PushSnapshot], int]
) -> Iterable[Observation]:
    return [Observation(select(one), {"source": source}) for source, one in lanes.items()]
```

Change `tests/unit/test_telemetry_push.py:304` to
`monkeypatch.setattr("usher.telemetry._push._read", None)`.

- [ ] **Step 10: Run the push tests**

Run: `uv run pytest tests/unit/test_telemetry_push.py -v`
Expected: all pass.

- [ ] **Step 11: Convert the sse group**

Replace lines 499-504 with `_sse: _ReaderSlot[int] = _ReaderSlot()`. Replace the
`global`/assignment in `register_sse_gauge` with `_sse.set(read)`. Cut its
docstring to:

```python
    """PRD 10's `usher.sse.connections`.

    The one live read among these: `len()` on an in-memory set is safe to call
    from the metric reader's background thread.
    """
```

Replace `_observe_sse_connections` (lines 528-536) with:

```python
def _observe_sse_connections(options: CallbackOptions) -> Iterable[Observation]:
    return _sse.observe(lambda open_connections: [Observation(open_connections)])
```

Change `tests/unit/test_telemetry_sse.py:97` to
`monkeypatch.setattr("usher.telemetry._sse._read", None)`.

- [ ] **Step 12: Run the sse tests**

Run: `uv run pytest tests/unit/test_telemetry_sse.py -v`
Expected: all pass.

- [ ] **Step 13: Convert the scheduler group**

Replace lines 541-547 with `_scheduler: _ReaderSlot[Mapping[str, float]] = _ReaderSlot()`.
Replace the `global`/assignment in `register_scheduler_gauges` with
`_scheduler.set(read)`. Cut its docstring to:

```python
    """PRD 10's `usher.scheduler.job.due`, per job.

    Seconds since a job's `last_done()` minus its period; negative means not yet
    due. A job with no reading has no entry rather than a zero, which would read
    as "exactly due".
    """
```

Replace `_observe_job_due` (lines 586-596) with:

```python
def _observe_job_due(options: CallbackOptions) -> Iterable[Observation]:
    return _scheduler.observe(
        lambda readings: [Observation(due, {"job": job}) for job, due in readings.items()]
    )
```

Change `tests/unit/test_telemetry_scheduler.py` — the file from step 1 — to
monkeypatch `usher.telemetry._scheduler._read` instead of
`usher.telemetry._scheduler_reader`.

- [ ] **Step 14: Run the scheduler tests**

Run: `uv run pytest tests/unit/test_telemetry_scheduler.py -v`
Expected: 3 passed — the same three that passed in step 2.

- [ ] **Step 15: Convert the search group**

Replace lines 647-652 with `_search: _ReaderSlot[SearchSnapshot] = _ReaderSlot()`.
Replace the `global`/assignment in `register_search_gauges` with
`_search.set(read)`. Cut its docstring to:

```python
    """PRD 10's `usher.search.embeddings.stale`, its refused companion, and
    `usher.similarity.neighbors.stale`.

    `read` returns the caller's most recent full re-read, never a query, for
    `register_queue_gauges`' reason. The third instrument takes a different
    meter because `usher index --backfill` does not drain it.
    """
```

Replace `_observe_embeddings_stale`, `_observe_embeddings_refused`,
`_observe_neighbors_stale` and `_search_observations` (lines 710-734) with:

```python
def _observe_embeddings_stale(options: CallbackOptions) -> Iterable[Observation]:
    return _search.observe(lambda snapshot: [Observation(snapshot.stale)])


def _observe_embeddings_refused(options: CallbackOptions) -> Iterable[Observation]:
    return _search.observe(lambda snapshot: [Observation(snapshot.refused)])


def _observe_neighbors_stale(options: CallbackOptions) -> Iterable[Observation]:
    return _search.observe(lambda snapshot: [Observation(snapshot.neighbors_stale)])
```

Change `tests/unit/test_telemetry_search.py:787` to
`monkeypatch.setattr("usher.telemetry._search._read", None)`.

- [ ] **Step 16: Run the whole telemetry suite**

Run: `uv run pytest tests/unit -k telemetry -v`
Expected: all pass.

- [ ] **Step 17: Run the gate**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports
```

Expected: all clean; `lint-imports` reports 12 kept, 0 broken.

- [ ] **Step 18: Commit**

```bash
git add src/usher/telemetry.py tests/unit/test_telemetry_*.py
git commit -m "refactor(telemetry): one reader slot behind five observable instrument groups

The module-global reader, the global rebind and the None guard were written
five times with the rationale pasted alongside each. _ReaderSlot holds the
one copy; the observation shapes stay at their call sites because they
genuinely differ."
```

---

### Task 2: One narrow session scope, with the commit as an argument

`composition.py` has two hand-rolled `@asynccontextmanager` closures that differ
only in whether they commit. The difference is stated in prose in two long
docstrings, and a third registration would write a third copy and pick its
commit semantics by reading them.

**Files:**
- Modify: `src/usher/composition.py:1029-1062` (`search_query_scope`), `:1063-1100` (`similarity_scope`)
- Test: `tests/unit/test_composition_scopes.py` (create)

- [ ] **Step 1: Write the characterisation test**

Create `tests/unit/test_composition_scopes.py`:

```python
"""A narrow scope commits, or does not, by argument rather than by shape."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _sessions(session: _Session) -> Any:
    @asynccontextmanager
    async def open() -> AsyncIterator[_Session]:
        yield session

    return open


@pytest.mark.asyncio
async def test_a_committing_scope_commits_after_the_body() -> None:
    from usher.composition import scope

    session = _Session()
    opened = scope(_sessions(session), lambda one: one, commit=True)

    async with opened() as held:
        assert session.commits == 0
        assert held is session

    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_raise_inside_the_body_leaves_it_uncommitted() -> None:
    from usher.composition import scope

    session = _Session()
    opened = scope(_sessions(session), lambda one: one, commit=True)

    with pytest.raises(RuntimeError):
        async with opened():
            raise RuntimeError("the chunk failed")

    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_non_committing_scope_never_commits() -> None:
    from usher.composition import scope

    session = _Session()
    opened = scope(_sessions(session), lambda one: one, commit=False)

    async with opened():
        pass

    assert session.commits == 0
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/unit/test_composition_scopes.py -v`
Expected: FAIL — `ImportError: cannot import name 'scope' from 'usher.composition'`.

- [ ] **Step 3: Add the helper**

In `src/usher/composition.py`, immediately above `search_query_scope`, insert:

```python
def scope[T](
    sessions: async_sessionmaker[AsyncSession],
    build: Callable[[AsyncSession], T],
    *,
    commit: bool,
) -> Callable[[], AbstractAsyncContextManager[T]]:
    """One session, one `build(session)`, opened per use.

    Returned as a callable so `usher.services` and `usher.api.lanes` reach a
    database without importing SQLAlchemy, and opened per use so a lane that
    ticks for weeks never holds a session idle in transaction.

    `commit=True` commits after the body, so a raise inside leaves the unit
    uncommitted rather than half-committed. Pass `commit=False` when the built
    object commits on its own cadence.
    """

    @asynccontextmanager
    async def open() -> AsyncIterator[T]:
        async with sessions() as session:
            yield build(session)
            if commit:
                await session.commit()

    return open
```

Add `from contextlib import AbstractAsyncContextManager` to the imports if it is
not already present.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_composition_scopes.py -v`
Expected: 3 passed.

- [ ] **Step 5: Reduce the two callers to one line each**

Replace the whole body of `search_query_scope` with:

```python
def search_query_scope(sessions: async_sessionmaker[AsyncSession]) -> SearchQueryScope:
    """One `SearchQueryRepository` per use, committed on a clean exit.

    The commit is what makes `SearchQueryRetention.run`'s "a commit per chunk"
    true: every repository here flushes and never commits, so a scope that did
    not commit would delete a year of keystrokes inside one transaction and,
    on a process that died mid-loop, delete none of them.
    """
    return scope(sessions, PostgresSearchQueryRepository, commit=True)
```

Replace the whole body of `similarity_scope` with:

```python
def similarity_scope(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> SimilarityScope:
    """One `SimilarityService` per use, committing nothing on exit.

    `SimilarityService` is handed the session's own `commit` and calls it per
    page, which is what lets an interrupted walk keep the pages it finished.
    """
    return scope(
        sessions,
        lambda session: SimilarityService(
            PostgresTitleEmbeddingRepository(session),
            PostgresTitleNeighborRepository(session),
            PostgresTitleRepository(session),
            session.commit,
            embedding_model=settings.embedding_model,
        ),
        commit=False,
    )
```

- [ ] **Step 6: Run the scope and scheduler tests**

Run: `uv run pytest tests/unit/test_composition_scopes.py tests/unit/test_services_scheduler.py -v`
Expected: all pass.

- [ ] **Step 7: Run the gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports
git add src/usher/composition.py tests/unit/test_composition_scopes.py
git commit -m "refactor(composition): the commit is an argument, not a difference between two look-alike closures"
```

---

### Task 3: `allow_inf_nan=False` on the base config

`domain/title.py:111` sets it on `tmdb_popularity` alone, and its own comment
concedes that the neighbouring fields are protected only by a `le=` ceiling that
happens to reject non-finite values. Four fields on three models have no ceiling
at all:

| model | field |
|---|---|
| `domain/search.py::SearchResult` | `popularity: float \| None` |
| `domain/search.py::SearchResult` | `score: float` |
| `domain/search.py::SimilarTitle` | `score: float` |
| `domain/taste.py::Centroid` | `vector: tuple[float, ...]` |

`Centroid.vector` is the one that matters most: a `NaN` in a stored taste
centroid does not raise anywhere, it silently makes every distance against it
`NaN`, and similarity ranking degrades to whatever order the rows arrive in.

⚠️ `domain/taste.py::GenreAffinity` is **not** affected and must not be tested
here. It is a plain frozen dataclass rather than a `DomainModel`, so pydantic's
config never reaches its `lift` and `support`.

This is the one task in this plan with real red-green.

**Files:**
- Modify: `src/usher/domain/base.py:32`
- Modify: `src/usher/domain/title.py:111`
- Test: `tests/unit/test_domain_non_finite.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_domain_non_finite.py`:

```python
"""No domain model accepts a non-finite float."""

import math
import uuid

import pytest
from pydantic import ValidationError

from usher.domain.search import SearchResult, SimilarTitle
from usher.domain.taste import Centroid
from usher.domain.title import TitleKind

NON_FINITE = [math.inf, -math.inf, math.nan]


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_search_result_refuses_a_non_finite_popularity(value: float) -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            title_id=uuid.uuid4(), kind=TitleKind.MOVIE, name="anything", popularity=value
        )


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_search_result_refuses_a_non_finite_score(value: float) -> None:
    with pytest.raises(ValidationError):
        SearchResult(title_id=uuid.uuid4(), kind=TitleKind.MOVIE, name="anything", score=value)


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_similar_title_refuses_a_non_finite_score(value: float) -> None:
    with pytest.raises(ValidationError):
        SimilarTitle(title_id=uuid.uuid4(), kind=TitleKind.MOVIE, name="anything", score=value)


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_centroid_refuses_a_non_finite_component(value: float) -> None:
    """A NaN here does not raise anywhere downstream; it makes every distance NaN."""
    with pytest.raises(ValidationError):
        Centroid(user_id=uuid.uuid4(), vector=(0.1, value, 0.3), model_name="bge-m3")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_domain_non_finite.py -v`
Expected: 12 failures — `DID NOT RAISE ValidationError`. Every one of these
models currently accepts `inf`.

- [ ] **Step 3: Set it on the base config**

In `src/usher/domain/base.py:32`, change:

```python
    model_config = ConfigDict(frozen=True, extra="forbid")
```

to:

```python
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_domain_non_finite.py -v`
Expected: 9 passed.

- [ ] **Step 5: Drop the now-redundant field-level setting**

In `src/usher/domain/title.py:111`, change:

```python
    tmdb_popularity: float | None = Field(default=None, ge=0, allow_inf_nan=False)
```

to:

```python
    tmdb_popularity: float | None = Field(default=None, ge=0)
```

Delete the comment block above it that argues for the field-level placement —
the argument no longer applies, and the base config now carries the rule.

- [ ] **Step 6: Run the full unit suite**

Run: `uv run pytest tests/unit -x -q --maxfail=5`
Expected: all pass. A failure here means some model legitimately carries a
non-finite float — stop and report it rather than reverting the base config.

- [ ] **Step 7: Run the gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports
git add src/usher/domain/base.py src/usher/domain/title.py tests/unit/test_domain_non_finite.py
git commit -m "fix(domain): no domain model accepts a non-finite float

allow_inf_nan=False was on tmdb_popularity alone; search.popularity,
taste.lift and taste.support had no ceiling and accepted inf."
```

---

### Task 4: Close out the stage

- [ ] **Step 1: Run the full suite including integration**

Run: `uv run pytest`
Expected: all pass. `tests/integration/` needs Docker.

- [ ] **Step 2: Confirm the diff is what this plan describes**

Run: `git diff --stat HEAD~4..HEAD`
Expected: `src/usher/telemetry.py`, `src/usher/composition.py`,
`src/usher/domain/base.py`, `src/usher/domain/title.py`, and five test files.
`telemetry.py` should be materially shorter.

- [ ] **Step 3: Tick the three items off the parent plan**

Mark items 1, 32 and 34 done in
`docs/plans/2026-09-12-comment-convention-and-polish.md`, and commit.
