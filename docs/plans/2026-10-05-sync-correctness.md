# Sync correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every sync lane leaves the catalog and its run rows correct when a listing loses items mid-walk or between attempts, when a sync is stopped or its process dies, and when two watch runs overlap.

**Architecture:** Four rulings (D1–D4 below), each a self-contained change. A cancelled walk closes its own run (`services/run_closing.py`). Emby's paging judges every page by ids and by `DateCreated` and asks again earlier instead of skipping (`adapters/emby/paging.py`), carries a checkpoint across a resume (`sync_run_units.checkpoint`) and bounds episode chunks by key. Every run whose process died is closed by the next run of its lane (`SyncRunRepository.close_abandoned`, `sync_runs.planned`), and the watch lane never resumes. The `watch_states` trigger keeps a source merge's own instant. One migration, `m10h`.

**Tech Stack:** Python 3.13, asyncio, SQLAlchemy 2 (async, asyncpg), Alembic, PostgreSQL 17, pytest with testcontainers, the fake Emby server in `tests/fakes/`.

**Spec:** none. The authority is this plan's **Decisions** section: the owner's rulings, on 2026-10-05, on the concerns fast first sync's review raised against PR #95 — *"the data and state should stay correct. So make the decisions accordingly. I want the rerun to start at once unless it has some drawbacks. No need for unpark button."* Background for the paging and resume code: `docs/specs/2026-10-01-fast-first-sync-design.md` and `docs/plans/2026-10-01-fast-first-sync.md` (historical — never edit either).

## Decisions

- **D1 — a cancelled walk closes its run (the owner's item 35).** A walk whose task is cancelled (Ctrl-C on `usher sync`, a cancelled job) rolls back, then saves its run `failed` with `CANCELLED_ERROR = "cancelled: the walk was stopped before it finished"`, as last committed, and re-raises the cancellation. A re-run starts at once; a whole-library walk closed this way is resumed (it is `failed` with units). **Drawbacks, stated in PRD 03 and the PR:** a process killed outright (SIGKILL, OOM, a deploy that stops the container under a `docker compose exec`) cannot close anything, so its run still counts as live for up to 10 minutes; a second Ctrl-C abandons the close; a close that cannot finish within `CLOSE_SECONDS = 10.0` is given up with a WARNING.
- **D2 — paging never skips (concern 1, P3/P7).** A page that shares no item with the page before, and does not start before the `DateCreated` of the last item read, has moved: it is not read, and the walk asks again `BACKUP` (100) items earlier, doubling while pages keep moving, never before 0 (Task 2). A resumed unit judges its first page against the checkpoint it committed, `m10h`'s `sync_run_units.checkpoint` (Task 3); a unit resumed without one restarts at its floor. Episode chunks are bounded by the `DateCreated` key at each boundary, read when the walk is planned (Task 4); a chunk planned in the old format walks to the end of its library. **Costs:** re-reads (duplicates are idempotent, though `items_seen` counts them) and extra requests where `DateCreated` ties across a page boundary.
- **D3 — abandoned rows are closed (concern 2, C4-2).** Every item walk beats, single walks included. Each run first closes its lanes' `running` rows that are not live with `ABANDONED_ERROR = "abandoned: its process stopped before it finished"` (Tasks 5–6). A new column `sync_runs.planned` marks a whole-library walk, replacing "has a heartbeat" as that test. **The watch lane never resumes:** this withdraws #41's resume, because a resumed `StartIndex` into a listing that lost items skips them; a failed watch delta now re-reads from its cursor. The superseded errors and the superseding code go. `usher sync`'s exit treats the watch run after a walk as the last word for that source's watch lane.
- **D4 — a watch merge keeps its read instant (concern 3, C4-1).** The `watch_states` trigger keeps a source write's own `updated_at` (the instant its walk began); a client write still gets `now()`, and the restore upsert stamps `updated_at = now()` itself (Task 7).
- **The owner's item 29:** no un-park command. Nothing in this plan adds one.

## Global Constraints

- `PAGE_OVERLAP = 50`; `BACKUP = 2 * PAGE_OVERLAP` (100), doubling per consecutive moved page, floored at `StartIndex` 0.
- `STALE_AFTER` stays 10 minutes; every walk beats on every commit and every `heartbeat_seconds` (60 s) between commits.
- `CLOSE_SECONDS = 10.0`.
- Exact strings (tests assert them verbatim):
  - `CANCELLED_ERROR = "cancelled: the walk was stopped before it finished"`
  - `ABANDONED_ERROR = "abandoned: its process stopped before it finished"`
  - Shift WARNING: `"{source}'s listing moved past the overlap before StartIndex={start}; reading again from StartIndex={again}"`
  - Unsorted WARNING, once per listing: `"{source}'s listing is not in DateCreated order, so its pages are judged by ids alone"`
  - Close-failure WARNING: `"a cancelled sync of {source} could not close its run {run_id} ({error}); it counts as live until its heartbeat is 10 minutes old"`, with `error = str(exc) or type(exc).__name__`
  - Item-lane WARNING: `"closed {count} item walk(s) of {source} whose process had stopped"`
  - Watch-lane WARNING: `"closed {count} watch-state run(s) of {source} whose process had stopped"`
- One migration, `m10h` (`down_revision = "m10g"`): created in Task 3, extended in Tasks 6 and 7. It is the eighteenth landing of `test_migrations.py`'s `-1` block.
- Unit key format: `episodes:<view>:<lower>[@<key>]:<upper>[@<key>]`, keys in microseconds since the epoch, possibly negative. A checkpoint is the decimal microsecond string of the last `DateCreated` read.
- No run against any real server, and no `docker` command against production (`usher-prod-*`). No merges and no pushes from implementers.
- Repository prose caps (enforced by the `guard-prose.sh` hook): module docstring ≤ 3 lines, function docstring ≤ 20 lines, comment block ≤ 5 lines; no dates, shas or "measured" in code prose.
- The PRD and `CHANGELOG.md` (`[Unreleased]`) change in the same commit as the behaviour they describe.
- Commit subjects use a lowercase area prefix (`sync: …`, `emby: …`, `watch: …`, `db: …`); no trailers, no model names.
- Test IMDb ids sit in the `tt99` band.
- Plant-and-verify uses `cp` backups in `/var/tmp`; never `git checkout`/`restore`/`stash`/`reset` to undo.
- Do not edit historical plans or specs, and leave `SyncRunResponse`'s docstring alone (it feeds the OpenAPI document).
- Iterate on the narrow test file; run the full gate once per task, before its commit:
  `uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly`

## Review Focus

1. **More than `PAGE_OVERLAP` items leave a listing** — between two pages (Task 2), between two attempts at a unit (Task 3), across a chunk boundary between plan and walk (Task 4). Expected: every surviving item is read; one WARNING per moved page.
2. **A Ctrl-C at any await of a walk.** Expected: the run is closed `failed` with `CANCELLED_ERROR` as last committed, and the task still ends cancelled (Task 1).
3. **Overlapping watch runs.** Expected: a live run is left alone (Task 5), and of two merges the newer read wins whichever commits first (Task 7).
4. **A killed process.** Expected: the next run of its lane closes it as abandoned, never a live one, with the boundary exactly at `STALE_AFTER` (Tasks 5 and 6).
5. **`DateCreated` ties at a page boundary, a checkpoint or a chunk boundary.** Expected: no endless backing up — the walk ends at the tie group's start or at `StartIndex` 0 (Tasks 2 and 3).

## Residuals (accepted; Task 2–4 PRD text states them where a user meets them)

- An item whose `DateCreated` moves behind the cursor mid-walk is missed until the next full walk.
- A server that serves one item per page walks unjudged (a page of one reaches back nothing).
- A process killed outright leaves its run live for up to 10 minutes (D1).
- A second Ctrl-C abandons the close (D1).
- A chunk from an old-format plan whose predecessor had already completed is judged by index at its start.
- On a listing out of `DateCreated` order, a resumed unit's first page is accepted unjudged.

## File map

| File | Tasks | What changes |
|---|---|---|
| `src/usher/services/run_closing.py` (new) | 1 | `close_cancelled`, `CLOSE_SECONDS` |
| `src/usher/domain/sync.py` | 1, 3, 5, 6 | `CANCELLED_ERROR`; `SyncRunUnit.checkpoint`; `ABANDONED_ERROR`; `SyncRun.planned`, docstrings |
| `src/usher/services/reconcile.py` | 1, 3, 6 | `rollback`, cancel-close; checkpoint through `_fetch`/`_commit_unit`; single walk beats, `close_abandoned`, `_claim(now)` |
| `src/usher/services/watch_sync.py` | 1, 5 | `rollback`, cancel-close; never resumes, `close_abandoned` |
| `src/usher/composition.py`, `src/usher/api/deps.py` | 1 | `rollback=session.rollback` |
| `src/usher/adapters/emby/paging.py` | 2, 4 | `key_of`, `BACKUP`, moved pages, `after`; `until`, no `stop` |
| `src/usher/adapters/emby/adapter.py` | 2, 3, 4 | `_pages` triples and WARNINGs; `list_unit(checkpoint=)`, `_resume`; `_boundary_keys`, keyed chunks |
| `src/usher/adapters/emby/planning.py` | 4 | `@key` bounds, `keyed_chunks` |
| `src/usher/ports/source.py` | 3 | `UnitPage.checkpoint`, `list_unit(checkpoint=)` |
| `src/usher/ports/repository/sync.py` | 5, 6 | `close_abandoned`; docstrings |
| `src/usher/db/models/sync.py`, `src/usher/db/repositories/sync.py` | 3, 5, 6 | `checkpoint`; `close_abandoned`; `planned` |
| `src/usher/db/migrations/versions/m10h_sync_correctness.py` (new) | 3, 6, 7 | the three changes |
| `src/usher/db/repositories/backup.py` | 7 | restore stamps `updated_at = now()` |
| `src/usher/cli.py` | 5 | watch failures counted per source |
| `tests/fakes/{source_adapter,sync_run_repository}.py`, `tests/contract/{source_adapter,sync_run_repository}_contract.py` | 3, 5, 6 | the port changes |
| `docs/prd/03-sources-and-sync.md`, `docs/prd/02-data-model.md`, `CHANGELOG.md`, `.claude/rules/{emby-push-and-ingest,db-and-sql}.md` | 1–7 | with each behaviour |
| `docs/plans/progress.md`, `docs/prd/README.md` | 8 | status rows |

---

### Task 1: A cancelled walk closes its run

**Files:**
- Create: `src/usher/services/run_closing.py`, `tests/unit/test_services_run_closing.py`
- Modify: `src/usher/domain/sync.py` (add `CANCELLED_ERROR` beside `STALE_AFTER`)
- Modify: `src/usher/services/reconcile.py:170-339`, `src/usher/services/watch_sync.py:161-309`
- Modify: `src/usher/composition.py:387-410`, `src/usher/api/deps.py:475-505`, and every other `ReconcileService(` / `WatchStateSyncService(` call (`grep -rn 'ReconcileService(\|WatchStateSyncService(' src tests` — about fifteen sites across `tests/integration/test_ingest_end_to_end.py`, `test_push_lane_end_to_end.py`, `test_services_push.py`, `test_services_reconcile.py`, `test_services_watch_sync.py` and `tests/unit/test_services_handlers.py`, `test_services_push.py`, `test_services_reconcile.py`, `test_services_watch_sync.py`, `test_telemetry_push.py`)
- Test: `tests/unit/test_services_run_closing.py`, `tests/unit/test_services_reconcile.py`, `tests/unit/test_services_watch_sync.py`, `tests/integration/test_services_reconcile.py`
- Docs: `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`

**Interfaces:**
- Produces: `usher.domain.sync.CANCELLED_ERROR: str`; `usher.services.run_closing.CLOSE_SECONDS: float`; `async def close_cancelled(runs: SyncRunRepository, run_id: uuid.UUID, *, rollback: Callable[[], Awaitable[None]], commit: Callable[[], Awaitable[None]], source: str) -> None`.
- Produces: both services take a **required** `rollback: Callable[[], Awaitable[None]]` positional-or-keyword parameter right after `commit`. Every call site passes `rollback=` by keyword (`session.rollback` in the composition roots; a no-op or a counter in unit fixtures).
- Integration sites whose `commit` is `session.flush` (the rolled-back `session` fixture) pass `rollback=session.rollback` only if they never cancel — a real `session.rollback()` would roll back the fixture's outer transaction; a test that cancels uses the `sessions` factory instead.

- [ ] **Step 1: Write the failing `close_cancelled` tests**

```python
"""`close_cancelled`: a cancelled walk's run closed as last committed."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from loguru import logger

from tests.fakes.sync_run_repository import FakeSyncRunRepository
from usher.domain.ids import new_id
from usher.domain.sync import CANCELLED_ERROR, SyncRun, SyncRunKind, SyncRunStatus
from usher.services import run_closing
from usher.services.run_closing import close_cancelled

T0 = datetime(2026, 7, 1, tzinfo=UTC)
SOURCE = "Living Room Emby"


class _Session:
    """A unit of work over the fake: `commit` keeps what is saved, `rollback` restores it."""

    def __init__(self, runs: FakeSyncRunRepository, run_id: uuid.UUID) -> None:
        self.runs = runs
        self.run_id = run_id
        self.events: list[str] = []
        self.committed: SyncRun | None = None

    async def commit(self) -> None:
        self.events.append("commit")
        self.committed = await self.runs.get(self.run_id)

    async def rollback(self) -> None:
        self.events.append("rollback")
        if self.committed is not None:
            await self.runs.save(self.committed)


async def _running() -> tuple[FakeSyncRunRepository, SyncRun, _Session]:
    runs = FakeSyncRunRepository()
    run = SyncRun(source_id=new_id(), kind=SyncRunKind.FULL, started_at=T0, heartbeat_at=T0)
    await runs.add(run)
    return runs, run, _Session(runs, run.id)


def _warnings() -> tuple[list[str], int]:
    lines: list[str] = []
    return lines, logger.add(lines.append, level="WARNING", format="{level.name}|{message}")


async def test_a_cancelled_walk_is_closed_as_last_committed() -> None:
    runs, run, session = await _running()
    await runs.save(run.evolve(items_seen=2))
    await session.commit()
    await runs.save(run.evolve(items_seen=5))  # in flight when the walk was cancelled
    session.events.clear()

    await close_cancelled(
        runs, run.id, rollback=session.rollback, commit=session.commit, source=SOURCE
    )

    stored = await runs.get(run.id)
    assert stored is not None
    assert (stored.status, stored.error, stored.error_code, stored.items_seen) == (
        SyncRunStatus.FAILED,
        CANCELLED_ERROR,
        None,
        2,
    )
    assert stored.finished_at is not None
    assert session.events == ["rollback", "commit"]


@pytest.mark.parametrize("status", [SyncRunStatus.COMPLETED, SyncRunStatus.FAILED])
async def test_a_run_that_is_not_running_is_left_alone(status: SyncRunStatus) -> None:
    runs, run, session = await _running()
    await runs.save(run.evolve(status=status, error="its own"))
    await session.commit()
    session.events.clear()

    await close_cancelled(
        runs, run.id, rollback=session.rollback, commit=session.commit, source=SOURCE
    )

    stored = await runs.get(run.id)
    assert stored is not None
    assert (stored.status, stored.error) == (status, "its own")
    assert session.events == ["rollback"]


async def test_a_close_that_fails_logs_and_raises_nothing() -> None:
    runs, run, session = await _running()

    async def reset() -> None:
        raise RuntimeError("connection reset")

    lines, sink = _warnings()
    try:
        await close_cancelled(runs, run.id, rollback=reset, commit=session.commit, source=SOURCE)
    finally:
        logger.remove(sink)
    assert [line.rstrip("\n") for line in lines] == [
        f"WARNING|a cancelled sync of {SOURCE} could not close its run {run.id} "
        "(connection reset); it counts as live until its heartbeat is 10 minutes old"
    ]


async def test_a_close_that_hangs_is_given_up(monkeypatch: pytest.MonkeyPatch) -> None:
    runs, run, session = await _running()
    monkeypatch.setattr(run_closing, "CLOSE_SECONDS", 0.01)

    async def hang() -> None:
        await asyncio.Event().wait()

    lines, sink = _warnings()
    try:
        await close_cancelled(runs, run.id, rollback=hang, commit=session.commit, source=SOURCE)
    finally:
        logger.remove(sink)
    assert [line.rstrip("\n") for line in lines] == [
        f"WARNING|a cancelled sync of {SOURCE} could not close its run {run.id} "
        "(TimeoutError); it counts as live until its heartbeat is 10 minutes old"
    ]


async def _cancelled_walk(close: object) -> asyncio.Task[None]:
    started = asyncio.Event()

    async def walk() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await close  # type: ignore[misc]
            raise

    task = asyncio.create_task(walk())
    await started.wait()
    task.cancel()
    return task


async def test_a_close_inside_a_cancelled_task_runs_and_the_task_stays_cancelled() -> None:
    runs, run, session = await _running()
    task = await _cancelled_walk(
        close_cancelled(
            runs, run.id, rollback=session.rollback, commit=session.commit, source=SOURCE
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await runs.get(run.id)
    assert stored is not None
    assert (stored.status, stored.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)


async def test_a_hanging_close_inside_a_cancelled_task_gives_up_and_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.timeout` inside a task already cancelling once still times out."""
    runs, run, session = await _running()
    monkeypatch.setattr(run_closing, "CLOSE_SECONDS", 0.01)

    async def hang() -> None:
        await asyncio.Event().wait()

    task = await _cancelled_walk(
        close_cancelled(runs, run.id, rollback=hang, commit=session.commit, source=SOURCE)
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5.0)
```

(`_cancelled_walk` takes the un-awaited coroutine so each case states its own close; tidy the `object`/`type: ignore` into a `Coroutine[object, object, None]` annotation if mypy prefers.)

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_services_run_closing.py -p no:randomly -q`
Expected: collection error, `usher.services.run_closing` does not exist.

- [ ] **Step 3: Write `run_closing.py` and the constant**

In `src/usher/domain/sync.py`, beside `STALE_AFTER`:

```python
#: What a walk closed by its own cancellation says.
CANCELLED_ERROR = "cancelled: the walk was stopped before it finished"
```

`src/usher/services/run_closing.py`:

```python
"""Closing a walk's run when the task walking it is cancelled."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from loguru import logger

from usher.domain.sync import CANCELLED_ERROR, SyncRunStatus
from usher.ports.repository import SyncRunRepository

#: How long a close may take before it is given up; module-level so a case can shorten it.
CLOSE_SECONDS = 10.0


async def close_cancelled(
    runs: SyncRunRepository,
    run_id: uuid.UUID,
    *,
    rollback: Callable[[], Awaitable[None]],
    commit: Callable[[], Awaitable[None]],
    source: str,
) -> None:
    """Close a cancelled walk's run `failed` with `CANCELLED_ERROR`, as last committed.

    Rolls back first, so nothing the walk had not committed rides along. A run no
    longer `running` is left as it is. A close that fails or takes longer than
    `CLOSE_SECONDS` logs a WARNING and returns: the run then counts as live until
    its heartbeat is stale. Never raises an `Exception`; a second cancellation
    still ends it.
    """
    try:
        async with asyncio.timeout(CLOSE_SECONDS):
            await rollback()
            stored = await runs.get(run_id)
            if stored is None or stored.status is not SyncRunStatus.RUNNING:
                return
            await runs.save(
                stored.evolve(
                    status=SyncRunStatus.FAILED,
                    error=CANCELLED_ERROR,
                    error_code=None,
                    finished_at=datetime.now(UTC),
                )
            )
            await commit()
    except Exception as exc:
        logger.warning(
            "a cancelled sync of {source} could not close its run {run_id} ({error}); "
            "it counts as live until its heartbeat is 10 minutes old",
            source=source,
            run_id=run_id,
            error=str(exc) or type(exc).__name__,
        )
```

(If ruff flags the blind `except`, add `# noqa: BLE001` with a reason; `ReconcileService._fetch` already catches `Exception` the same way.)

- [ ] **Step 4: Run the `close_cancelled` tests to see them pass**

Run: `uv run pytest tests/unit/test_services_run_closing.py -p no:randomly -q` — Expected: 7 passed.

- [ ] **Step 5: Write the failing service tests**

In `tests/unit/test_services_reconcile.py`: give `_Fixture` `self.rollbacks = 0` and an `async def _rollback(self) -> None: self.rollbacks += 1`, passed as `rollback=self._rollback`. Add a module helper and three cases:

```python
async def _until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until `condition` holds, failing rather than hanging."""
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


async def test_a_cancelled_single_walk_closes_its_run_and_stays_cancelled(
    fixture: _Fixture,
) -> None:
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    listing = asyncio.Event()

    async def stalled(since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        listing.set()
        await asyncio.Event().wait()
        yield _item("m1")

    fixture.adapter.list_items = stalled  # type: ignore[method-assign]
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    )
    await listing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.DELTA)
    assert stored is not None
    assert (stored.status, stored.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)
    assert fixture.rollbacks == 1


async def test_a_cancelled_planned_walk_is_resumed_at_once(fixture: _Fixture) -> None:
    _shelve(fixture, "Films", range(3))
    release = fixture.adapter.hold("Films")
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    await _until(lambda: bool(fixture.adapter.unit_starts))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.FULL)
    assert cancelled is not None
    assert (cancelled.status, cancelled.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)

    release.set()
    resumed = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert (resumed.id, resumed.status) == (cancelled.id, SyncRunStatus.COMPLETED)


async def test_a_walk_whose_close_fails_still_ends_cancelled(fixture: _Fixture) -> None:
    async def reset() -> None:
        raise RuntimeError("connection reset")

    fixture.service._rollback = reset  # the close's own failure, not the walk's
    _shelve(fixture, "Films", range(3))
    fixture.adapter.hold("Films")
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    await _until(lambda: bool(fixture.adapter.unit_starts))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.FULL)
    assert stored is not None
    assert stored.status is SyncRunStatus.RUNNING, "the premise: nothing closed it"
```

(Replace the private-attribute poke with a `_Fixture(rollback=...)` parameter if you prefer; either way the case must reach `close_cancelled` with a rollback that raises.)

In `tests/unit/test_services_watch_sync.py`: the `_Fixture` gains the same `rollbacks` counter. Add:

```python
async def test_a_cancelled_watch_walk_closes_its_run_and_stays_cancelled() -> None:
    fixture = _Fixture()
    adapter = fixture.adapter = _StallingSourceAdapter(fixture.source, stall_after=0)
    await fixture.given_completed_walk()
    await fixture.given_matched("movie-0")
    task = asyncio.create_task(
        fixture.service.sync(fixture.source, adapter, user_id=fixture.user_id)
    )
    await adapter.stalled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.WATCH_STATE)
    assert stored is not None
    assert (stored.status, stored.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)
    assert fixture.rollbacks == 1
```

In `tests/integration/test_services_reconcile.py`, `_service` gains a keyword `commit: Callable[[], Awaitable[None]] | None = None` (`commit=commit or session.flush`) and always passes `rollback=session.rollback` — which no existing case reaches, since none cancels. Then a case on the real database, with the `sessions` factory, because it must commit and roll back for real:

```python
MARK = "Cancelled Walk Case"


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


async def test_a_cancelled_walk_closes_its_run_as_last_committed_against_real_sql(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The close rolls back the write in flight and commits the run `failed`."""
    try:
        async with sessions() as session:
            source = Source(
                kind=SourceKind.EMBY,
                name=MARK,
                base_url="https://emby.invalid",
                credentials_ref=f"ref-{new_id()}",
                device_id=str(new_id()),
            )
            await PostgresSourceRepository(session).add(source)
            await session.commit()
            adapter = FakeSourceAdapter(source)
            adapter.seed(_item("m1"), datetime(2026, 7, 1, tzinfo=UTC))
            adapter.place("m1", "Films")
            adapter.stage("Films", WalkStage.TITLES)
            adapter.hold("Films")
            runs = PostgresSyncRunRepository(session)
            service = _service(
                session,
                runs,
                PostgresMediaItemRepository(session),
                batch_size=2,
                commit=session.commit,
            )
            task = asyncio.create_task(service.reconcile(source, SyncRunKind.FULL, adapter))
            await _until(lambda: bool(adapter.unit_starts))
            # The writer is parked on its queue and the fetcher on the hold, so the
            # session is idle: a pending write the close must roll back, not commit.
            started = await runs.latest_run(source.id, SyncRunKind.FULL)
            assert started is not None, "the premise: the walk committed its run"
            await runs.save(started.evolve(items_seen=99))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with sessions() as reader:
            stored = await PostgresSyncRunRepository(reader).get(started.id)
        assert stored is not None
        assert (stored.status, stored.error, stored.items_seen) == (
            SyncRunStatus.FAILED,
            CANCELLED_ERROR,
            0,
        )
    finally:
        async with sessions() as cleanup:
            # Cascades to `sync_runs` and `sync_run_units`.
            await cleanup.execute(text("DELETE FROM sources WHERE name = :name"), {"name": MARK})
            await cleanup.commit()
```

- [ ] **Step 6: Run them to see them fail**

Run: `uv run pytest tests/unit/test_services_reconcile.py tests/unit/test_services_watch_sync.py -k cancel -p no:randomly -q`
Expected: FAIL — `rollback` is an unexpected keyword, then (once accepted) the run stays `running`.

- [ ] **Step 7: Wire `rollback` and the close into both services**

Both constructors gain `rollback: Callable[[], Awaitable[None]]` right after `commit`, stored as `self._rollback`. In `ReconcileService.reconcile`, bind the run id once the run is written (the `add` or the claimed `save`) and open the guard at the first commit, closing it after the final one:

```python
                await self._runs.save(run)
            run_id = run.id
            try:
                await self._commit()
                progress = _Progress(run)
                try:
                    ...  # the walk, unchanged
                except UsherPortError as exc:
                    ...  # unchanged
                await self._runs.save(run)
                await self._commit()
            except asyncio.CancelledError:
                # Closed as last committed, so a re-run need not wait out `STALE_AFTER`.
                await close_cancelled(
                    self._runs,
                    run_id,
                    rollback=self._rollback,
                    commit=self._commit,
                    source=source.name,
                )
                raise
```

`WatchStateSyncService.sync` takes the same shape: `run_id = run.id` after the `add`/`save` of the run, the `try` opening at `await self._commit()` and closing after the final `await self._commit()`. A cancellation before the run's first commit leaves nothing committed to close, which is why the guard starts there. Update both constructors' call sites (see **Files**).

- [ ] **Step 8: Run the narrow tests, then the integration case**

Run: `uv run pytest tests/unit/test_services_run_closing.py tests/unit/test_services_reconcile.py tests/unit/test_services_watch_sync.py -p no:randomly -q` — Expected: all pass.
Run: `uv run pytest tests/integration/test_services_reconcile.py -k cancelled -p no:randomly -q` — Expected: 1 passed.

- [ ] **Step 9: PRD 03 and the CHANGELOG**

In `docs/prd/03-sources-and-sync.md`, after the sentence ending *"…when a walk whose process died has gone stale and is resumed."*, add:

> A walk whose task is cancelled — `usher sync` stopped with Ctrl-C, say — first closes its run `failed` with `cancelled: the walk was stopped before it finished`, as last committed, so the next walk starts at once and a whole-library walk resumes. A process killed outright cannot, and its run counts as live until its heartbeat is 10 minutes old; a close that cannot finish within 10 seconds is given up with a WARNING, and a second Ctrl-C abandons it.

`CHANGELOG.md`, `[Unreleased]` → `### Fixed`:

> - **A sync stopped with Ctrl-C no longer blocks the next one for 10 minutes.** The cancelled walk closes its run `failed` with `cancelled: the walk was stopped before it finished`, as last committed, so the next `usher sync` starts at once and a whole-library walk resumes. A process killed outright still leaves its run live until its heartbeat is 10 minutes old.

- [ ] **Step 10: Full gate, then commit**

```bash
git add -A src/usher tests docs/prd/03-sources-and-sync.md CHANGELOG.md
git commit -m "sync: a cancelled walk closes its run, so a re-run starts at once"
```

---

### Task 2: Paging never skips mid-listing

**Files:**
- Modify: `src/usher/adapters/emby/paging.py` (rewrite of `OffsetWindow`)
- Modify: `src/usher/adapters/emby/adapter.py:346-439` (`_walk`, `_pages`, `_unit_pages`), `:607-639` (`_seed`)
- Test: `tests/unit/test_adapters_emby_paging.py`, `tests/unit/test_adapters_emby_adapter.py`
- Docs: `docs/prd/03-sources-and-sync.md` (the *Items are walked in ascending creation order* bullet), `.claude/rules/emby-push-and-ingest.md` (the *reach-back is clamped* rule), `CHANGELOG.md`

**Interfaces:**
- Produces: `paging.BACKUP = 2 * PAGE_OVERLAP`; `paging.key_of(entry: object) -> int | None`.
- Produces: `OffsetWindow(*, limit: int, start: int, stop: int | None = None, keyed: bool = False, after: int | None = None)` with properties `cursor`, `anchor: int | None`, `unsorted: bool`, `request_limit`; `Page.shifted` now means *moved and not read* — its `fresh` is always empty and it never ends the walk.
- Produces: `EmbyAdapter._pages(query, *, start_index, stop=None, path=None, after=None)` yields `(fresh, resume_at, anchor)` triples — Task 3 consumes the anchor.

- [ ] **Step 1: Write the failing paging tests**

Add to `tests/unit/test_adapters_emby_paging.py` (imports: `from datetime import UTC, datetime, timedelta`, plus `BACKUP` and `key_of`):

```python
_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _at(external_id: str, second: int) -> dict[str, Any]:
    created = (_BASE + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    return {"Id": external_id, "DateCreated": created}


def _key(second: int) -> int:
    return key_of(_at("k", second)) or 0


def test_a_key_is_whole_microseconds_since_the_epoch() -> None:
    assert key_of({"DateCreated": "1970-01-01T00:00:01.0000019Z"}) == 1_000_001
    assert key_of({"DateCreated": "1969-12-31T23:59:59.0000000Z"}) == -1_000_000


@pytest.mark.parametrize("entry", [{}, {"DateCreated": "garbage"}, {"DateCreated": None}, "x"])
def test_an_entry_without_a_readable_creation_time_has_no_key(entry: object) -> None:
    assert key_of(entry) is None


def _moved_window(*, keyed: bool = False) -> OffsetWindow:
    """A window whose second request, at 2, comes back holding none of the first page's ids."""
    window = OffsetWindow(limit=4, start=0, keyed=keyed)
    window.receive([_at(f"a{index}", index) for index in range(4)], 100)
    assert window.advance() == 2
    return window


def test_a_page_that_moved_is_not_read_and_does_not_end_the_walk() -> None:
    window = _moved_window()
    page = window.receive(_entries("x", "y", "z", "w"), 0)
    assert (page.shifted, page.fresh, page.ended) == (True, [], False)


def test_a_moved_page_backs_the_walk_up_by_the_backup() -> None:
    window = OffsetWindow(limit=4, start=300)
    window.receive(_entries("a", "b", "c", "d"), 1000)
    start = window.advance()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == start - BACKUP


def test_consecutive_moved_pages_double_the_backup() -> None:
    window = OffsetWindow(limit=4, start=1000)
    window.receive(_entries("a", "b", "c", "d"), 5000)
    start = window.advance()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == start - BACKUP
    window.receive(_entries("p", "q", "r", "s"), 0)
    assert window.advance() == start - 3 * BACKUP


def test_a_backup_never_goes_before_the_start_of_the_listing() -> None:
    window = _moved_window()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == 0


def test_an_accepted_page_resets_the_backup() -> None:
    window = OffsetWindow(limit=4, start=1000)
    window.receive(_entries("a", "b", "c", "d"), 5000)
    start = window.advance()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == start - BACKUP
    assert not window.receive(_entries("c", "d", "e", "f"), 0).shifted
    after = window.advance()
    window.receive(_entries("m", "n", "o", "p"), 0)
    assert window.advance() == after - BACKUP


def test_the_page_after_a_backup_is_judged_against_the_last_page_read() -> None:
    window = _moved_window()
    window.receive(_entries("x", "y", "z", "w"), 0)
    window.advance()
    page = window.receive([_at("a2", 2), _at("a3", 3), _at("b0", 4), _at("b1", 5)], 0)
    assert not page.shifted
    assert [entry["Id"] for entry in page.fresh] == ["b0", "b1"]


def test_a_moved_page_leaves_the_cursor_and_the_anchor_where_they_were() -> None:
    window = _moved_window(keyed=True)
    cursor, anchor = window.cursor, window.anchor
    window.receive([_at("x", 90), _at("y", 91)], 0)
    assert (window.cursor, window.anchor) == (cursor, anchor) == (4, _key(3))


def test_an_empty_page_after_a_reach_back_has_moved() -> None:
    assert _moved_window().receive([], 0).shifted


def test_an_empty_page_with_nothing_to_judge_it_by_ends_the_walk() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive([{"Name": "w"}, {"Name": "x"}, {"Name": "y"}, {"Name": "z"}], 10)
    window.advance()
    page = window.receive([], 0)
    assert (page.shifted, page.ended) == (False, True)


def test_a_page_at_the_start_of_the_listing_has_never_moved() -> None:
    window = _moved_window()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == 0, "the premise: the backup reached the start"
    assert not window.receive(_entries("q", "r", "s", "t"), 0).shifted


def test_a_keyed_page_starting_before_the_anchor_has_not_moved() -> None:
    """Every item after the last one read sorts at or after this page's first."""
    window = _moved_window(keyed=True)
    assert not window.receive([_at("x", 2), _at("y", 50)], 0).shifted


def test_a_keyed_page_starting_at_the_anchor_has_moved() -> None:
    """A tie cannot say which side of the last item read the page starts."""
    window = _moved_window(keyed=True)
    assert window.receive([_at("x", 3), _at("y", 50)], 0).shifted


def test_an_unkeyed_window_ignores_keys() -> None:
    window = _moved_window(keyed=False)
    assert window.receive([_at("x", 0), _at("y", 1)], 0).shifted


def test_falling_keys_mark_the_listing_unsorted_and_stop_judging_by_key() -> None:
    window = OffsetWindow(limit=4, start=0, keyed=True)
    window.receive([_at("a", 9), _at("b", 3), _at("c", 4), _at("d", 5)], 100)
    assert window.unsorted
    window.advance()
    assert window.receive([_at("x", 0), _at("y", 1)], 0).shifted


def test_an_unkeyed_window_is_never_unsorted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive([_at("a", 9), _at("b", 3)], 100)
    assert not window.unsorted


def test_the_anchor_is_the_last_entry_that_carries_a_key() -> None:
    window = OffsetWindow(limit=4, start=0, keyed=True)
    window.receive([_at("a", 1), _at("b", 2), {"Id": "c"}, {"Id": "d"}], 100)
    assert window.anchor == _key(2)


def test_a_keyed_request_that_reached_back_nothing_is_not_judged() -> None:
    """The unkeyed half is `test_a_request_that_reached_back_nothing_cannot_have_shifted`."""
    window = OffsetWindow(limit=4, start=0, keyed=True)
    window.receive([_at("a", 1)], 10)
    window.advance()
    assert window.overlap == 0, "the premise: a one-entry page reaches back nothing"
    assert not window.receive([_at("x", 7)], 0).shifted


def test_a_resumed_window_judges_its_first_page_against_its_anchor() -> None:
    window = OffsetWindow(limit=4, start=150, keyed=True, after=_key(10))
    assert window.receive([_at("x", 11), _at("y", 12)], 0).shifted


def test_a_resumed_window_accepts_a_first_page_that_starts_before_its_anchor() -> None:
    window = OffsetWindow(limit=4, start=150, keyed=True, after=_key(10))
    assert not window.receive([_at("x", 9), _at("y", 12)], 0).shifted


def test_a_tie_group_wider_than_the_backup_backs_up_to_the_start() -> None:
    """Every page ties with the anchor, so only `StartIndex` 0 can be accepted."""
    window = OffsetWindow(limit=4, start=250, keyed=True, after=_key(5))
    starts = []
    for _ in range(4):
        if not window.receive([_at(f"t{len(starts)}", 5)], 0).shifted:
            break
        starts.append(window.advance())
    assert starts == [150, 0]
```

(Most cases here exercise behaviour the old window did not have and must fail before Step 3; a few — a page at `StartIndex` 0, an unkeyed window ignoring keys — pass before and after and guard the rewrite. The existing tests in this file keep their meaning under the new rule — every one asserting `shifted` asserts a page that moved — and must still pass.)

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_adapters_emby_paging.py -p no:randomly -q`
Expected: ImportError on `BACKUP`/`key_of`, then failures.

- [ ] **Step 3: Rewrite `OffsetWindow`**

```python
"""Where each page of a `StartIndex` walk starts, what in it is new, and when it ends."""

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from usher.adapters.emby.mapping import parse_datetime

# How far a page after the first reaches back into the one before. A deletion
# behind the cursor shifts every later item left by one, so without this the
# next page skips one item per deletion, and the sweep retracts a file that is
# still there.
PAGE_OVERLAP = 50

# How far a walk asks again before a page that moved past the overlap, doubled
# for each further page that moved.
BACKUP = 2 * PAGE_OVERLAP

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def key_of(entry: object) -> int | None:
    """An entry's `DateCreated` in whole microseconds since the epoch, or `None`."""
    if not isinstance(entry, dict):
        return None
    created = parse_datetime(entry.get("DateCreated"))
    return None if created is None else (created - _EPOCH) // _MICROSECOND


@dataclass(frozen=True, slots=True)
class Page:
    """One listing response, read against the walk so far.

    A page that `shifted` moved past the overlap: nothing in it is read.
    """

    fresh: list[dict[str, Any]]
    ended: bool
    shifted: bool


class OffsetWindow:
    """The arithmetic of one walk, kept apart from the requests it plans.

    The total is read from the first page only. A page is short when it is shorter
    than the longest page served before it, or than `limit` before any. A walk ends
    on an empty page; on a short page once the cursor has reached that total; or on
    a short page that brought nothing new.

    Each page after the first reaches back `PAGE_OVERLAP` items, clamped to half the
    page before it, and an entry the previous page carried is dropped by its `Id`.
    A page that reached back yet holds none of the previous page's ids has moved,
    unless a `keyed` window sees it start before the anchor, the last `DateCreated`
    read. A page that moved is not read: the walk asks again `BACKUP` items before
    it, doubling while pages keep moving, never before 0. A resumed window starts
    judging from `after`, the anchor its last page left. Falling keys inside a page
    mark a keyed listing unsorted, and keys judge nothing after that.

    A bounded window never asks past `stop`, and reaching it ends the walk.
    """

    def __init__(
        self,
        *,
        limit: int,
        start: int,
        stop: int | None = None,
        keyed: bool = False,
        after: int | None = None,
    ) -> None:
        self.limit = limit
        self.start = start
        self.stop = stop
        self.keyed = keyed
        self.overlap = 0
        self.total: int | None = None
        self._cursor = start
        self._served = 0
        self._longest = 0
        self._previous: frozenset[str] = frozenset()
        self._served_ids: list[str | None] = []
        self._reach: list[str | None] = []
        self._reached = after is not None
        self._anchor = after
        self._backoff = 0
        self._unsorted = False
        self._first = True

    @property
    def cursor(self) -> int:
        """Where the last page read ends."""
        return self._cursor

    @property
    def anchor(self) -> int | None:
        """The last `DateCreated` key read: what a resumed window starts judging from."""
        return self._anchor

    @property
    def unsorted(self) -> bool:
        """Whether a keyed listing has served a page whose keys fall."""
        return self._unsorted

    @property
    def request_limit(self) -> int:
        """The `Limit` for the request at `start`: `limit`, cut short at `stop`."""
        if self.stop is None:
            return self.limit
        return max(0, min(self.limit, self.stop - self.start))

    def receive(self, entries: list[Any], total: object) -> Page:
        """Account for the page requested at `start`."""
        ids = [_id_of(entry) for entry in entries]
        known = [key for key in map(key_of, entries) if key is not None]
        if self.keyed and any(later < earlier for earlier, later in itertools.pairwise(known)):
            self._unsorted = True
        # `> 0`, not `>= 0`: 0 is what a listing not asked to count reports. Not a
        # `bool` either, because JSON `true` is an `int` to Python.
        if self._first and isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self.total = total
        # Against the longest page served, or `limit` before the first: a capped
        # server serves nothing longer, and its full pages past a stale total are a
        # library that grew.
        short = len(entries) < (self._longest or self.limit)
        first, self._first = self._first, False
        self._longest = max(self._longest, len(entries))
        if self._moved(ids, known):
            self._backoff = self._backoff * 2 if self._backoff else BACKUP
            return Page(fresh=[], ended=False, shifted=True)
        self._backoff = 0
        fresh = [
            entry
            for entry, external_id in zip(entries, ids, strict=True)
            if isinstance(entry, dict)
            and (external_id is None or external_id not in self._previous)
        ]
        self._cursor = self.start + len(entries)
        self._served = len(entries)
        reached = self.total is not None and self._cursor >= self.total
        drained = not first and short and not fresh
        self._previous = frozenset(external_id for external_id in ids if external_id is not None)
        self._served_ids = ids
        if known:
            self._anchor = known[-1]
        stopped = self.stop is not None and self._cursor >= self.stop
        ended = not entries or (short and reached) or drained or stopped
        return Page(fresh=fresh, ended=ended, shifted=False)

    def _moved(self, ids: list[str | None], known: list[int]) -> bool:
        """Whether the page at `start` moved past everything the request reached back over."""
        if self.start == 0 or not self._reached:
            return False
        anchor = self._anchor if self.keyed and not self._unsorted else None
        reached_for_an_id = any(external_id is not None for external_id in self._reach)
        if anchor is None and not reached_for_an_id:
            return False
        if not self._previous.isdisjoint(ids):
            return False
        # Judged against the whole previous page, not the reach-back alone: items
        # listed ahead of it can push the reach-back out of a page that skipped nothing.
        return anchor is None or not known or known[0] >= anchor

    def advance(self) -> int:
        """The next request's `StartIndex`: back into the last page read, or before one that moved."""
        if self._backoff:
            self.start = max(0, self.start - self._backoff)
            self._reach = self._served_ids
            self._reached = True
        else:
            reach = min(PAGE_OVERLAP, self._served // 2)
            self.start = max(0, self._cursor - reach)
            self._reach = self._served_ids[len(self._served_ids) - reach :]
            self._reached = reach > 0
        self.overlap = self._cursor - self.start
        return self.start


def _id_of(entry: object) -> str | None:
    if isinstance(entry, dict):
        value = entry.get("Id")
        if isinstance(value, str) and value:
            return value
    return None
```

Check the class docstring is ≤ 20 lines after edits (the prose hook counts it), and that `overlap` still reads as the existing tests expect (the reach-back of a normal advance).

- [ ] **Step 4: Run the paging tests to see them pass**

Run: `uv run pytest tests/unit/test_adapters_emby_paging.py -p no:randomly -q` — Expected: all pass, old and new.

- [ ] **Step 5: Write the failing adapter tests**

In `tests/unit/test_adapters_emby_adapter.py`, rewrite the two shift cases and add three:

```python
async def test_a_shift_past_the_overlap_asks_again_earlier_and_skips_nothing() -> None:
    """Sixty deletions behind the cursor are more than the overlap can absorb.

    The page at 50 holds none of the first page's items and starts no earlier, so it is
    not read; the walk asks again from 0 and reads every survivor.
    """
    server = FakeEmbyServer()
    library = _numbered(250)
    for item in library:
        server.add_item(item, T0)
    gone = [f"movie-{index:03d}" for index in range(60)]
    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(_deleting(server, gone), page_size=100)
        try:
            seen = [item.external_id async for item in adapter.list_items()]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    assert {item.external_id for item in library} - set(gone) - set(seen) == set()
    assert [line.rstrip("\n") for line in lines] == [
        "Living Room Emby's listing moved past the overlap before StartIndex=50; "
        "reading again from StartIndex=0"
    ]


async def test_a_shift_past_a_clamped_overlap_asks_again_from_the_start() -> None:
    """Pages of 40 reach back 20; thirty deletions move the page at 20 past them."""
    server = FakeEmbyServer()
    library = _numbered(200)
    for item in library:
        server.add_item(item, T0)
    gone = [f"movie-{index:03d}" for index in range(30)]
    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(_deleting(server, gone), page_size=40)
        try:
            seen = [item.external_id async for item in adapter.list_items()]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    assert {item.external_id for item in library} - set(gone) - set(seen) == set()
    assert [line.rstrip("\n") for line in lines] == [
        "Living Room Emby's listing moved past the overlap before StartIndex=20; "
        "reading again from StartIndex=0"
    ]


async def test_eighty_deletions_after_the_first_page_skip_nothing() -> None:
    server = FakeEmbyServer()
    library = _numbered(300)
    for item in library:
        server.add_item(item, T0)
    gone = [f"movie-{index:03d}" for index in range(80)]
    adapter = _on(_deleting(server, gone), page_size=100)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert {item.external_id for item in library} - set(gone) - set(seen) == set()


async def test_a_tail_that_moved_out_of_reach_is_still_read() -> None:
    """The page at 50 comes back empty; the old walk ended there and lost 100 to 119."""
    server = FakeEmbyServer()
    library = _numbered(120)
    for item in library:
        server.add_item(item, T0)
    gone = [f"movie-{index:03d}" for index in range(80)]
    adapter = _on(_deleting(server, gone), page_size=100)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert {f"movie-{index:03d}" for index in range(100, 120)} <= set(seen)


async def test_a_listing_out_of_date_order_warns_once_and_is_walked_by_ids() -> None:
    server = FakeEmbyServer()
    library = _numbered(5)
    for item in library:
        server.add_item(item, T0)

    def falling(request: httpx.Request) -> httpx.Response:
        response = server.handle(request)
        if not request.url.path.endswith("/Items"):
            return response
        body = response.json()
        for offset, entry in enumerate(body.get("Items", [])):
            entry["DateCreated"] = f"2026-07-20T12:00:{59 - offset:02d}.0000000Z"
        return httpx.Response(response.status_code, json=body)

    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(falling, page_size=2)
        try:
            seen = [item.external_id async for item in adapter.list_items()]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    assert set(seen) == {item.external_id for item in library}
    assert [line.rstrip("\n") for line in lines] == [
        "Living Room Emby's listing is not in DateCreated order, so its pages are judged "
        "by ids alone"
    ]
```

(`_numbered` items all carry `added_at=T0`, so every key ties: these cases also prove a tie forces a backup rather than an acceptance.)

- [ ] **Step 6: Run them to see them fail**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py -k "shift or deletions or tail or order" -p no:randomly -q`
Expected: FAIL on the new WARNING text and the skipped survivors.

- [ ] **Step 7: Rework `_pages` and its three consumers**

```python
    async def _pages(
        self,
        query: Mapping[str, str],
        *,
        start_index: int,
        stop: int | None = None,
        path: str | None = None,
        after: int | None = None,
    ) -> AsyncGenerator[tuple[list[dict[str, Any]], int, int | None]]:
        """Page one listing to its end, one request ahead of the consumer.

        Yields each page's new entries, the `StartIndex` that resumes after it (the next
        request's start, reach-back included) and the window's anchor. The next page is
        asked for as soon as a page arrives and before it is yielded; the request
        outstanding when the consumer stops is cancelled. `stop` bounds the walk, and
        one already at its stop sends nothing. A listing sorted by `SORT_BY` is keyed:
        `after` is the anchor a resumed walk judges its first page by. A page that moved
        is logged and not yielded. `start_index` is the resume point, never defaulted.
        """
        if path is None:
            path = await self._items_path()
        keyed = query.get("SortBy") == SORT_BY
        window = OffsetWindow(
            limit=self._page_size,
            start=start_index,
            stop=stop,
            keyed=keyed,
            after=after if keyed else None,
        )
        if window.request_limit == 0:
            return
        pending = self._read(path, query, window.start, window.request_limit, count=True)
        warned = False
        try:
            # `for`, not `while True`: the bound is part of the loop, and the raise
            # below is reachable only by a walk that never ended.
            for number in range(1, self._max_pages + 1):
                body = await pending
                entries = body.get("Items")
                if not isinstance(entries, list):
                    # Not a truncation: a caller must be able to tell "the library
                    # ended" from "that was not a listing at all".
                    raise PortDataMalformed(
                        "Emby's item listing carried no Items array",
                        detail=f"StartIndex={window.start}",
                    )
                asked = window.start
                page = window.receive(entries, body.get("TotalRecordCount"))
                if window.unsorted and not warned:
                    warned = True
                    logger.warning(
                        "{source}'s listing is not in DateCreated order, so its pages are "
                        "judged by ids alone",
                        source=self._source.name,
                    )
                resume_at = window.cursor
                if not page.ended and number < self._max_pages:
                    resume_at = window.advance()
                    pending = self._read(path, query, resume_at, window.request_limit, count=False)
                if page.shifted:
                    logger.warning(
                        "{source}'s listing moved past the overlap before StartIndex={start}; "
                        "reading again from StartIndex={again}",
                        source=self._source.name,
                        start=asked,
                        again=resume_at,
                    )
                    continue
                yield page.fresh, resume_at, window.anchor
                if page.ended:
                    return
        finally:
            await _settle(pending)
        raise PortDataMalformed(...)  # unchanged
```

`_walk` and `_seed` unpack `entries, _, _`; `_unit_pages` unpacks `entries, resume_at, _` (Task 3 uses the anchor). `NEXT_UP_PATH`'s query has no `SortBy`, so the seed's up-next listing stays unkeyed.

- [ ] **Step 8: Run the adapter, contract and paging tests**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_paging.py tests/unit/test_adapters_emby_contract.py -p no:randomly -q` — Expected: all pass. Fix any existing case that asserted the old WARNING text by moving it to the new one; do not delete a case that still describes true behaviour.

- [ ] **Step 9: Docs**

PRD 03, the *Items are walked in ascending creation order* bullet — replace its second and third sentences (from *"Each page after the first re-reads…"* through *"…and the next full reconcile covers what it missed."*) with:

> Each page after the first re-reads the last 50 items of the page before (half the page, if it held fewer than 100). A page that holds none of the page before's items, and does not start before the creation time of the last item read, has moved — more items than that overlap left the listing between the two pages — and is not read: the walk logs a WARNING naming both requests and asks again 100 items earlier, doubling while pages keep moving, never before the start. A listing that is not in creation order is judged by its items alone, after one WARNING; an item whose creation time moves behind the walk is read by the next full walk.

Keep the bullet's resume sentences as they are (Task 3 replaces them).

`.claude/rules/emby-push-and-ingest.md` — replace *"**A page's reach-back is clamped to half the page before it**, and a walk ends on a short page that brought nothing new (`paging.OffsetWindow`): a fixed `PAGE_OVERLAP` stalls on pages of 50 or fewer, and deletions strand the total."* with:

> - **A page's reach-back is clamped to half the page before it**, a walk ends on a short page that brought nothing new, and **a page that moved is never read** — it shares no id with the page before and does not start before the anchor's `DateCreated` (`paging.OffsetWindow`): the walk asks again `BACKUP` earlier, doubling. A fixed `PAGE_OVERLAP` stalls on pages of 50 or fewer, deletions strand the total, and a tie on the anchor counts as moved.

`CHANGELOG.md` → `### Fixed`:

> - **A walk no longer skips items when more than the page overlap leave the listing between two pages.** A page that moved past the overlap is not read: the walk logs a WARNING and asks again 100 items earlier, doubling while pages keep moving. Before, it read on, and a full walk's sweep then retracted the items it had skipped.

- [ ] **Step 10: Full gate, then commit**

```bash
git add -A src/usher/adapters/emby tests/unit docs/prd/03-sources-and-sync.md .claude/rules/emby-push-and-ingest.md CHANGELOG.md
git commit -m "emby: a page that moved past the overlap is read again from earlier, never skipped"
```

---

### Task 3: A resumed unit judges its first page by its checkpoint (creates `m10h`)

**Files:**
- Create: `src/usher/db/migrations/versions/m10h_sync_correctness.py`
- Modify: `src/usher/ports/source.py` (`UnitPage`, `SourceAdapter.list_unit`), `src/usher/domain/sync.py` (`SyncRunUnit.checkpoint`), `src/usher/db/models/sync.py` (`SyncRunUnitRow.checkpoint`), `src/usher/db/repositories/sync.py` (`save_unit`), `tests/fakes/sync_run_repository.py` (`save_unit`), `tests/fakes/source_adapter.py` (`list_unit`)
- Modify: `src/usher/services/reconcile.py` (`_fetch`, `_commit_unit`), `src/usher/adapters/emby/adapter.py` (`list_unit`, `_library_unit`, `_unit_pages`)
- Modify: every test double that replaces `list_unit` (`grep -rn 'list_unit' tests` — the reconcile unit tests' custom generators such as `_trickle`) to accept `checkpoint: str | None = None`
- Test: `tests/contract/sync_run_repository_contract.py`, `tests/contract/source_adapter_contract.py`, `tests/unit/test_adapters_emby_adapter.py`, `tests/unit/test_services_reconcile.py`, `tests/unit/test_db_migration_status.py`, `tests/integration/test_migrations.py`
- Docs: PRD 03 (the walk bullet's resume sentences), PRD 02 (`sync_run_units` row), `.claude/rules/db-and-sql.md` (landing count), `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 2's `_pages(..., after=)` and its `(fresh, resume_at, anchor)` triples.
- Produces: `UnitPage(items, resume_at, checkpoint: str | None = None)`; `SourceAdapter.list_unit(key: str, *, start_index: int = 0, checkpoint: str | None = None) -> AsyncGenerator[UnitPage]` (the port's default ignores `checkpoint`); `SyncRunUnit.checkpoint: str | None = None`; `sync_run_units.checkpoint TEXT NULL`.
- Produces: in `adapter.py`, module helpers `_checkpoint_key(checkpoint: str | None) -> int | None`, `_resume(start_index: int, checkpoint: str | None, floor: int, lower_key: int | None = None) -> tuple[int, int | None]` and `_unit_query(unit: LibraryUnit) -> dict[str, str]` — Task 4 calls `_resume` with a `lower_key` and `_unit_query` for its probes.
- Produces: `FakeSourceAdapter.unit_checkpoints: list[str | None]`, appended beside `unit_starts` (whose 2-tuples stay as they are).

- [ ] **Step 1: Write the failing repository contract cases**

In `tests/contract/sync_run_repository_contract.py` (both arms run it):

```python
    async def test_a_unit_checkpoint_is_written_with_a_position_at_least_as_far(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        parent = run(source_id)
        await repository.add(parent)
        await repository.add_units([unit(parent.id, "titles:3")])
        stored = (await repository.units_for(parent.id))[0]

        await repository.save_unit(stored.evolve(position=10, checkpoint="100"))
        await repository.save_unit(stored.evolve(position=10, checkpoint="110"))
        await repository.save_unit(stored.evolve(position=4, checkpoint="40"))

        (after,) = await repository.units_for(parent.id)
        assert (after.position, after.checkpoint) == (10, "110")

    async def test_a_completed_unit_keeps_its_checkpoint(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        parent = run(source_id)
        await repository.add(parent)
        await repository.add_units([unit(parent.id, "titles:3")])
        stored = (await repository.units_for(parent.id))[0]
        await repository.save_unit(
            stored.evolve(position=10, checkpoint="100", status=SyncRunUnitStatus.COMPLETED)
        )

        await repository.save_unit(stored.evolve(position=20, checkpoint="200"))

        (after,) = await repository.units_for(parent.id)
        assert (after.position, after.checkpoint, after.status) == (
            10,
            "100",
            SyncRunUnitStatus.COMPLETED,
        )
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_sync_run_repository_contract.py -p no:randomly -q` — Expected: FAIL, `SyncRunUnit` has no `checkpoint`.

- [ ] **Step 3: The column, the migration, both repositories**

`SyncRunUnit` gains `checkpoint: str | None = None` (after `position`), with a comment: *"The adapter's own note of where `position` stands, written only with a position at least as far."* `SyncRunUnitRow` gains `checkpoint: Mapped[str | None] = mapped_column(Text, nullable=True)`.

`src/usher/db/migrations/versions/m10h_sync_correctness.py`, shaped like `m10g_sync_run_units.py` (match its header and docstring shape; ≤ 3-line module docstring):

```python
"""Sync correctness: a unit's resume checkpoint."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10h"
down_revision: str | Sequence[str] | None = "m10g"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_run_units", sa.Column("checkpoint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_run_units", "checkpoint")
```

Postgres `save_unit` (`_UNIT_MUTABLE` is derived from the table, so `checkpoint` is already among the values; override it):

```python
        values["position"] = func.greatest(SyncRunUnitRow.position, unit.position)
        # The note moves only with a position at least as far as the stored one, so
        # the two always describe the same place.
        values["checkpoint"] = case(
            (
                literal(unit.position) >= SyncRunUnitRow.position,
                literal(unit.checkpoint, Text()),
            ),
            else_=SyncRunUnitRow.checkpoint,
        )
```

(An `UPDATE`'s `SET` expressions all read the row as it was, so `SyncRunUnitRow.position` here is the stored position.) Fake `save_unit`:

```python
        self._units[key] = unit.evolve(
            position=max(stored.position, unit.position),
            checkpoint=unit.checkpoint if unit.position >= stored.position else stored.checkpoint,
        )
```

- [ ] **Step 4: Run the contract cases to see them pass**

Run: `uv run pytest tests/unit/test_sync_run_repository_contract.py tests/integration/test_sync_run_repository.py -p no:randomly -q` — Expected: pass.

- [ ] **Step 5: The landing bookkeeping (failing first)**

- `tests/unit/test_db_migration_status.py`: `code_head_revision() == "m10h"`; append `"m10h"` to `_REPOINTING_CHAIN`; add `18: "eighteen"` to `_CARDINALS`.
- `.claude/rules/db-and-sql.md` line 49: *"**Eighteen landings, eighteen loud breaks.**"* (keep the rest of the sentence).
- `tests/integration/test_migrations.py`, the `-1` block in `test_a_full_down_and_up_cycle_restores_every_index`: it now lands at `m10g`. Re-point it, then demote m10g's assertions to a named stop:

```python
        await asyncio.to_thread(run_alembic, url, "-1")
        # **Asserted against whatever the current head actually reverses**, so every
        # new migration breaks this block and has to re-point it.
        at_m10g_unit_columns = await column_set(url, "sync_run_units")
        assert "checkpoint" not in at_m10g_unit_columns, "checkpoint should not exist below m10h"
        assert at_m10g_unit_columns, "the premise: `sync_run_units` still exists at `m10g`"

        # **A named stop at `m10f`, holding `m10g`'s.**
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10f", direction="down"))
        at_m10f_columns = await column_set(url, "sync_runs")
        assert "heartbeat_at" not in at_m10f_columns, "heartbeat_at should not exist below m10g"
        assert at_m10f_columns, "the premise: `sync_runs` still exists at `m10f`"
        at_m10f_indexes = await index_set(url)
        assert "pk_sync_run_units" not in at_m10f_indexes, (
            "sync_run_units should not exist below m10g"
        )
        assert "pk_sync_runs" in at_m10f_indexes, "the premise: the index scan sees `sync_runs`"

        # **A named stop at `m10e`, holding `m10f`'s one.**  (unchanged from here)
```

Run: `uv run pytest tests/unit/test_db_migration_status.py -p no:randomly -q` and `uv run pytest tests/integration/test_migrations.py -p no:randomly -q` — Expected: pass (they fail before the migration file exists).

- [ ] **Step 6: Write the failing walk tests**

`tests/fakes/source_adapter.py`: `list_unit(self, key, *, start_index=0, checkpoint=None)` appends `checkpoint` to a new `self.unit_checkpoints: list[str | None] = []` beside the existing `unit_starts` append, and otherwise ignores it.

`tests/unit/test_services_reconcile.py`:

```python
async def test_a_unit_commits_its_last_held_pages_checkpoint() -> None:
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(3))

    async def noted(
        key: str, *, start_index: int = 0, checkpoint: str | None = None
    ) -> AsyncGenerator[UnitPage]:
        for index in range(2):
            yield UnitPage((_item(f"m{index}"),), resume_at=index + 1, checkpoint=f"note-{index}")
        raise PortUnavailable("the page after them failed")

    fixture.adapter.list_unit = noted  # type: ignore[method-assign]
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    (films,) = [u for u in await fixture.runs.units_for(run.id) if u.unit_key == "library:Films"]
    assert (films.position, films.checkpoint, films.status) == (
        2,
        "note-1",
        SyncRunUnitStatus.FAILED,
    )


async def test_a_resumed_unit_is_handed_its_committed_checkpoint() -> None:
    fixture = _Fixture()
    run = await _given_walk(
        fixture,
        heartbeat_at=None,
        status=SyncRunStatus.FAILED,
        units=[("Films", SyncRunUnitStatus.FAILED, 2)],
        finished_at=T0,
    )
    (films,) = await fixture.runs.units_for(run.id)
    await fixture.runs.save_unit(films.evolve(checkpoint="note-1"))
    handed: list[tuple[int, str | None]] = []

    async def noting(
        key: str, *, start_index: int = 0, checkpoint: str | None = None
    ) -> AsyncGenerator[UnitPage]:
        handed.append((start_index, checkpoint))
        return
        yield

    fixture.adapter.list_unit = noting  # type: ignore[method-assign]
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert handed == [(2, "note-1")]
```

(Fit `_given_walk`'s unit-key spelling to what it stores — it names libraries; adjust the `unit_key == …` filter to match.)

`tests/unit/test_adapters_emby_adapter.py` — a dated helper, then the resume cases:

```python
def _dated(count: int) -> list[SourceItem]:
    """`_numbered`, each created a second after the one before, so no two tie."""
    return [
        replace(item, added_at=T0 + timedelta(seconds=index))
        for index, item in enumerate(_numbered(count))
    ]


def _created_key(index: int) -> str:
    """The checkpoint `_dated`'s item `index` leaves: its `DateCreated` in microseconds."""
    created = T0 + timedelta(seconds=index)
    return str((created - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1))


async def test_each_unit_page_carries_the_checkpoint_of_its_last_item() -> None:
    server = FakeEmbyServer()
    view = _library(server, 1, "Films", _dated(5))
    adapter = _adapter(server, page_size=2)
    try:
        pages = [page async for page in adapter.list_unit(f"titles:{view}")]
    finally:
        await adapter.aclose()
    assert [page.checkpoint for page in pages] == [_created_key(i) for i in (1, 2, 3, 4)]


async def test_a_resumed_unit_skips_nothing_that_left_its_library_between_attempts() -> None:
    server = FakeEmbyServer()
    library = _dated(300)
    view = _library(server, 1, "Films", library)
    adapter = _adapter(server, page_size=100)
    try:
        first: list[UnitPage] = []
        async with aclosing(adapter.list_unit(f"titles:{view}")) as pages:
            async for page in pages:
                first.append(page)
                if page.resume_at >= 150:
                    break
        assert (first[-1].resume_at, first[-1].checkpoint) == (150, _created_key(199))
        for index in range(80):
            server.remove_item(f"movie-{index:03d}")
        lines: list[str] = []
        handle = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            resumed = [
                item.external_id
                async for page in adapter.list_unit(
                    f"titles:{view}", start_index=150, checkpoint=first[-1].checkpoint
                )
                for item in page.items
            ]
        finally:
            logger.remove(handle)
    finally:
        await adapter.aclose()
    assert {f"movie-{index:03d}" for index in range(200, 300)} <= set(resumed)
    assert any("moved past the overlap" in line for line in lines)


@pytest.mark.parametrize("checkpoint", [None, "", "12x", "1.5"])
async def test_a_unit_resumed_without_a_usable_checkpoint_restarts_at_its_floor(
    checkpoint: str | None,
) -> None:
    server = FakeEmbyServer()
    view = _library(server, 1, "Films", _dated(5))
    adapter, seen = _recorded(server, page_size=2)
    try:
        _ = [p async for p in adapter.list_unit(f"titles:{view}", start_index=3, checkpoint=checkpoint)]
    finally:
        await adapter.aclose()
    assert _listings(seen)[0].url.params["StartIndex"] == "0"


async def test_a_resume_over_an_unchanged_listing_starts_at_its_resume_point_and_stays() -> None:
    server = FakeEmbyServer()
    view = _library(server, 1, "Films", _dated(10))
    adapter, seen = _recorded(server, page_size=4)
    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        _ = [
            p
            async for p in adapter.list_unit(
                f"titles:{view}", start_index=4, checkpoint=_created_key(5)
            )
        ]
    finally:
        logger.remove(handle)
        await adapter.aclose()
    starts = [request.url.params["StartIndex"] for request in _listings(seen)]
    assert starts[0] == "4" and "0" not in starts
    assert lines == []
```

(`FakeEmbyServer` emits `DateCreated` from `SourceItem.added_at`, so `_dated` orders the listing by creation time and no two items tie.)

`tests/contract/source_adapter_contract.py`: `_filler(index)` gets `added_at=T0 + timedelta(minutes=index)` so fillers no longer tie, and `test_a_unit_resumes_after_a_page_from_its_resume_at` passes `checkpoint=first.checkpoint` to the resumed `list_unit`. If another contract case depended on fillers tying at `T0`, keep that case's own items tied rather than reverting the change.

- [ ] **Step 7: Run them to see them fail**

Run: `uv run pytest tests/unit/test_services_reconcile.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_contract.py -k "checkpoint or resume" -p no:randomly -q` — Expected: FAIL.

- [ ] **Step 8: Carry the checkpoint through the port, the writer and the adapter**

`ports/source.py`: `UnitPage` gains `checkpoint: str | None = None` (documented as the adapter's own opaque note of where `resume_at` stands); `SourceAdapter.list_unit(self, key: str, *, start_index: int = 0, checkpoint: str | None = None)`, the default implementation ignoring it.

`reconcile.py`: `_fetch` calls `adapter.list_unit(unit.unit_key, start_index=unit.position, checkpoint=unit.checkpoint)`; `_commit_unit` evolves `checkpoint=pages[-1].checkpoint if pages else unit.checkpoint` beside `position`.

`adapter.py`:

```python
_CHECKPOINT = re.compile(r"-?[0-9]+")


def _checkpoint_key(checkpoint: str | None) -> int | None:
    """The `DateCreated` key a checkpoint names, or `None` for none or a malformed one."""
    if checkpoint is None or not _CHECKPOINT.fullmatch(checkpoint):
        return None
    return int(checkpoint)


def _resume(
    start_index: int, checkpoint: str | None, floor: int, lower_key: int | None = None
) -> tuple[int, int | None]:
    """Where a unit's walk starts, and the anchor its first page is judged by.

    A unit past its floor with a usable checkpoint resumes there; any other starts again
    at its floor, judged by `lower_key`.
    """
    after = _checkpoint_key(checkpoint) if start_index > floor else None
    return (start_index, after) if after is not None else (floor, lower_key)


def _unit_query(unit: LibraryUnit) -> dict[str, str]:
    """The listing a library unit walks."""
    return {
        **_listing_query(LIBRARY_SINCE_PARAM, None),
        "ParentId": unit.view_id,
        "IncludeItemTypes": unit.item_types,
    }
```

`list_unit(self, key, *, start_index=0, checkpoint=None)`: the seed ignores both; `DEFAULT_UNIT_KEY` does `start, after = _resume(start_index, checkpoint, 0)` then `self._unit_pages(query, start_index=start, after=after)`; a library unit goes to `self._library_unit(unit, start_index, checkpoint)`, which builds `_unit_query(unit)`, keeps its `stop`, and does `start, after = _resume(start_index, checkpoint, unit.lower)`. `_unit_pages(query, *, start_index, stop=None, after=None)` yields `UnitPage(items, resume_at, None if anchor is None else str(anchor))`.

- [ ] **Step 9: Run the narrow tests, then the gate's unit half**

Run: `uv run pytest tests/unit tests/contract -p no:randomly -q` — Expected: pass. (`unit_starts` assertions stay 2-tuples.)

- [ ] **Step 10: Docs**

PRD 03, the walk bullet: replace *"A resumed unit of a whole-library walk re-reads only that overlap before where it stopped, and logs nothing: if more items vanish ahead of it between attempts, it misses them, a full walk's sweep retracts them, and the next full walk reads them again. Duplicates are permitted; silent truncation is not, except across such a resume."* with:

> A resumed unit of a whole-library walk judges its first page the same way, against the creation time of the last item it committed, so items that left its library between attempts are not skipped either; a unit committed before that time was recorded starts its library again. Duplicates are permitted; silent truncation is not.

PRD 02, the `sync_run_units` row: the tuple becomes `(run_id, unit_key, stage, label, position, checkpoint, expected_items, items_seen, status)`, and after *"`position` only rises and `completed` is final"* add *"; `checkpoint` is the adapter's own opaque note of where `position` stands (for Emby, the creation time of the last item read), written only with a position at least as far"*.

`CHANGELOG.md` → `### Fixed`:

> - **A resumed unit of a whole-library walk no longer skips items that left its library between attempts.** Each unit commits the creation time of the last item it read beside its position (`sync_run_units.checkpoint`, migration `m10h`), and its first page after a resume is judged against it. A unit saved before this release restarts from its beginning.

- [ ] **Step 11: Full gate, then commit**

```bash
git add -A src/usher tests docs/prd .claude/rules/db-and-sql.md CHANGELOG.md
git commit -m "sync: a resumed unit judges its first page by the checkpoint it committed (m10h)"
```

---

### Task 4: Episode chunks are bounded by key

**Files:**
- Modify: `src/usher/adapters/emby/planning.py`, `src/usher/adapters/emby/paging.py` (`until`; `stop` and `request_limit` removed), `src/usher/adapters/emby/adapter.py` (`_pages`, `_unit_pages`, `_library_unit`, `plan_walk`, new `_boundary_keys`)
- Test: `tests/unit/test_adapters_emby_planning.py`, `tests/unit/test_adapters_emby_paging.py`, `tests/unit/test_adapters_emby_adapter.py`
- Docs: PRD 03 (the *A whole-library walk is split into units* bullet), `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 3's `_resume(start_index, checkpoint, floor, lower_key)` and `_unit_query(unit)`.
- Produces: `LibraryUnit(stage, view_id, lower=0, upper=None, lower_key: int | None = None, upper_key: int | None = None)`; `keyed_chunks(chunks: Sequence[LibraryUnit], keys: Sequence[int | None]) -> list[LibraryUnit]`; `OffsetWindow(*, limit, start, keyed=False, after=None, until: int | None = None)`; `_pages(query, *, start_index, path=None, after=None, until=None)`.

- [ ] **Step 1: Write the failing planning and paging tests**

`tests/unit/test_adapters_emby_planning.py`:

```python
def test_a_keyed_chunk_key_round_trips() -> None:
    key = "episodes:v1:100000@-5:200000@1704067200000000"
    unit = parse_unit_key(key)
    assert unit == LibraryUnit(
        WalkStage.EPISODES, "v1", 100_000, 200_000, lower_key=-5, upper_key=1_704_067_200_000_000
    )
    assert unit.key == key


def test_an_old_chunk_key_still_parses_with_no_keys() -> None:
    assert parse_unit_key("episodes:v1:0:100") == LibraryUnit(WalkStage.EPISODES, "v1", 0, 100)


@pytest.mark.parametrize(
    "key", ["episodes:v:0@x:5", "episodes:v:10@:", "episodes:v:@5:", "episodes:v:5@1:5@2"]
)
def test_a_malformed_bound_names_no_unit(key: str) -> None:
    assert parse_unit_key(key) is None


def _chunks(count: int) -> list[LibraryUnit]:
    return episode_chunks("v", count * 100, 100)


def test_each_boundary_key_bounds_the_chunks_on_both_sides() -> None:
    assert keyed_chunks(_chunks(3), [7, 9]) == [
        LibraryUnit(WalkStage.EPISODES, "v", 0, 100, upper_key=7),
        LibraryUnit(WalkStage.EPISODES, "v", 100, 200, lower_key=7, upper_key=9),
        LibraryUnit(WalkStage.EPISODES, "v", 200, None, lower_key=9),
    ]


def test_a_boundary_with_no_key_ends_the_chunks_there() -> None:
    assert keyed_chunks(_chunks(3), [7, None]) == [
        LibraryUnit(WalkStage.EPISODES, "v", 0, 100, upper_key=7),
        LibraryUnit(WalkStage.EPISODES, "v", 100, None, lower_key=7),
    ]
    assert keyed_chunks(_chunks(3), [None, 9]) == [LibraryUnit(WalkStage.EPISODES, "v", 0, None)]


def test_one_chunk_needs_no_keys() -> None:
    assert keyed_chunks(_chunks(1), []) == _chunks(1)


def test_keys_that_do_not_match_the_boundaries_are_refused() -> None:
    with pytest.raises(ValueError):
        keyed_chunks(_chunks(3), [7])
```

`tests/unit/test_adapters_emby_paging.py` — delete the three `stop`/`request_limit` cases and add:

```python
def test_a_window_ends_after_the_page_that_passes_its_until() -> None:
    window = OffsetWindow(limit=2, start=0, keyed=True, until=_key(3))
    assert not window.receive([_at("a", 2), _at("b", 3)], 100).ended
    window.advance()
    assert window.receive([_at("b", 3), _at("c", 4)], 0).ended


def test_an_unsorted_window_does_not_end_at_its_until() -> None:
    window = OffsetWindow(limit=2, start=0, keyed=True, until=_key(3))
    assert not window.receive([_at("a", 9), _at("b", 4)], 100).ended
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_adapters_emby_planning.py tests/unit/test_adapters_emby_paging.py -p no:randomly -q` — Expected: FAIL.

- [ ] **Step 3: Keyed bounds in `planning.py`; `until` in `paging.py`**

```python
_BOUND = re.compile(r"([0-9]+)(?:@(-?[0-9]+))?")


def _bound(offset: int, key: int | None) -> str:
    return str(offset) if key is None else f"{offset}@{key}"


def _parse_bound(text: str) -> tuple[int, int | None] | None:
    found = _BOUND.fullmatch(text)
    if found is None:
        return None
    offset, key = found.groups()
    return int(offset), None if key is None else int(key)
```

`LibraryUnit` gains `lower_key: int | None = None` and `upper_key: int | None = None`; its docstring adds *"A keyed chunk also carries the `DateCreated` key of the item at each bound when it was planned."* `key` becomes `f"episodes:{self.view_id}:{_bound(self.lower, self.lower_key)}:{'' if self.upper is None else _bound(self.upper, self.upper_key)}"`. `parse_unit_key`'s episodes branch:

```python
    view_id, lower_text, upper_text = parts
    lower = _parse_bound(lower_text)
    upper = _parse_bound(upper_text) if upper_text else None
    if not view_id or lower is None or (upper_text and upper is None):
        return None
    if upper is not None and upper[0] <= lower[0]:
        return None
    return LibraryUnit(
        WalkStage.EPISODES,
        view_id,
        lower[0],
        None if upper is None else upper[0],
        lower_key=lower[1],
        upper_key=None if upper is None else upper[1],
    )
```

```python
def keyed_chunks(chunks: Sequence[LibraryUnit], keys: Sequence[int | None]) -> list[LibraryUnit]:
    """`chunks` bounded by the key at each boundary, one per boundary.

    A boundary with no key ends the chunks there: the chunk before it runs to the end.
    """
    if len(keys) != len(chunks) - 1:
        raise ValueError(f"{len(chunks)} chunks have {len(chunks) - 1} boundaries, not {len(keys)}")
    keyed: list[LibraryUnit] = []
    lower_key: int | None = None
    for chunk, upper_key in zip(chunks, [*keys, None], strict=True):
        if upper_key is None:
            keyed.append(replace(chunk, upper=None, lower_key=lower_key))
            break
        keyed.append(replace(chunk, lower_key=lower_key, upper_key=upper_key))
        lower_key = upper_key
    return keyed
```

`OffsetWindow`: drop `stop`, `request_limit` and the `stopped` term; add `until: int | None = None`, stored as `self.until`, and in `receive`:

```python
        passed = (
            self.until is not None
            and not self._unsorted
            and self._anchor is not None
            and self._anchor > self.until
        )
        ended = not entries or (short and reached) or drained or passed
```

Replace the docstring's `stop` sentence with *"A window bounded by `until` ends on the page whose last key passes it."*

- [ ] **Step 4: Run them to see them pass**

Run: `uv run pytest tests/unit/test_adapters_emby_planning.py tests/unit/test_adapters_emby_paging.py -p no:randomly -q` — Expected: pass.

- [ ] **Step 5: Write the failing adapter tests**

```python
def _episodes(count: int) -> list[SourceItem]:
    """`series-0` and `count` of its episodes, each created a second after the one before."""
    return [
        _series(0),
        *(replace(_episode(index), added_at=T0 + timedelta(seconds=index)) for index in range(count)),
    ]


async def test_a_plan_bounds_its_episode_chunks_by_key() -> None:
    """Each boundary's key is that of the episode the boundary's index lands on."""
    server = FakeEmbyServer()
    view = _library(server, 1, "Shows", _episodes(250), collection_type="tvshows")
    adapter = _adapter(server, unit_max_items=100)
    try:
        plan = await adapter.plan_walk()
    finally:
        await adapter.aclose()
    episodes = [unit.key for unit in plan.units if unit.stage is WalkStage.EPISODES]
    assert episodes == [
        f"episodes:{view}:0:100@{_created_key(100)}",
        f"episodes:{view}:100@{_created_key(100)}:200@{_created_key(200)}",
        f"episodes:{view}:200@{_created_key(200)}:",
    ]


async def _read(adapter: EmbyAdapter, key: str) -> list[str]:
    try:
        return [item.external_id async for page in adapter.list_unit(key) for item in page.items]
    finally:
        await adapter.aclose()


async def test_a_keyed_chunk_reads_past_its_boundary_and_stops() -> None:
    server = FakeEmbyServer()
    view = _library(server, 1, "Shows", _episodes(300), collection_type="tvshows")
    key = f"episodes:{view}:100@{_created_key(100)}:200@{_created_key(200)}"
    read = await _read(_adapter(server, page_size=40), key)
    assert {f"episode-{i:03d}" for i in range(100, 201)} <= set(read)
    assert max(read) < "episode-260", "the chunk ran on to the end of its library"


async def test_deletions_before_a_chunk_between_plan_and_walk_skip_nothing() -> None:
    server = FakeEmbyServer()
    view = _library(server, 1, "Shows", _episodes(300), collection_type="tvshows")
    key = f"episodes:{view}:100@{_created_key(100)}:200@{_created_key(200)}"
    for index in range(80):
        server.remove_item(f"episode-{index:03d}")
    read = await _read(_adapter(server, page_size=40), key)
    assert {f"episode-{i:03d}" for i in range(100, 201)} <= set(read)


async def test_a_boundary_probe_that_finds_nothing_ends_the_chunks_unbounded() -> None:
    """The library shrank below the boundary between being counted and being probed."""
    server = FakeEmbyServer()
    view = _library(server, 1, "Shows", _episodes(150), collection_type="tvshows")

    def emptied(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("Limit") == "1" and params.get("StartIndex") == "100":
            return httpx.Response(200, json={"Items": []})
        return server.handle(request)

    adapter = EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(emptied), base_url=SOURCE.base_url),
        unit_max_items=100,
    )
    try:
        plan = await adapter.plan_walk()
    finally:
        await adapter.aclose()
    assert [u.key for u in plan.units if u.stage is WalkStage.EPISODES] == [
        f"episodes:{view}:0:"
    ]


async def test_an_old_format_chunk_reads_to_the_end_of_its_library() -> None:
    server = FakeEmbyServer()
    view = _library(server, 1, "Shows", _episodes(150), collection_type="tvshows")
    read = await _read(_adapter(server, page_size=40), f"episodes:{view}:0:100")
    assert {f"episode-{i:03d}" for i in range(150)} == set(read)
```

`plan_walk` counts a library over every type it lists, so `_episodes(250)` counts 251 and plans three chunks; the probes list episodes alone, which is why the boundary at 100 is `episode-100`. Update every existing `plan_walk` case this moves: each library with N chunks sends N−1 boundary probes (`Limit=1`, `EnableTotalRecordCount=false`, `op="boundary"`), and **a library whose episode listing ends before a boundary — any movies library counted past `unit_max_items` — now plans one unbounded episode chunk** rather than several empty ones. Delete any case pinning the old `stop` read-past (a chunk reading exactly `upper + PAGE_OVERLAP`); the keyed cases replace it. If PRD 10 or a dashboard enumerates the Emby request `op` values, add `boundary` there.

- [ ] **Step 6: Run them to see them fail**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py -k "chunk or boundary or plan" -p no:randomly -q` — Expected: FAIL.

- [ ] **Step 7: Probe the boundaries; walk chunks by key**

```python
    async def _boundary_keys(self, chunks: Sequence[LibraryUnit]) -> list[int | None]:
        """The key of the item at each chunk's lower bound, `None` where the listing ends first.

        Asked at once; one that fails raises only once every one has settled.
        """
        path = await self._items_path()
        answers = await asyncio.gather(
            *(
                self._page(
                    path,
                    {
                        **_unit_query(chunk),
                        "StartIndex": str(chunk.lower),
                        "Limit": "1",
                        "EnableTotalRecordCount": "false",
                    },
                    chunk.lower,
                    op="boundary",
                )
                for chunk in chunks
            ),
            return_exceptions=True,
        )
        keys: list[int | None] = []
        for answer in answers:
            if isinstance(answer, BaseException):
                raise answer
            entries = answer.get("Items")
            if not isinstance(entries, list):
                raise PortDataMalformed("Emby's boundary listing carried no Items array")
            keys.append(key_of(entries[0]) if entries else None)
        return keys
```

`plan_walk`'s episode loop, every library's probes asked in one round (the listing limit still paces them):

```python
        chunked = [
            (name, episode_chunks(view_id, count, self._unit_max_items))
            for (view_id, name), count in ranked
        ]
        keys = iter(await self._boundary_keys([c for _, chunks in chunked for c in chunks[1:]]))
        for name, chunks in chunked:
            bounds = [next(keys) for _ in chunks[1:]]
            for chunk in keyed_chunks(chunks, bounds):
                units.append(WalkUnit(chunk.key, WalkStage.EPISODES, chunk.label(name)))
```

(Its docstring adds *"Each episode chunk after a library's first is bounded by the key of the item at its start, every library's asked for at once."*) `_library_unit`:

```python
        # A keyed chunk starts an overlap before its bound, judged by the bound's key, and
        # reads until it passes the next chunk's: an item moving across a bound is read
        # by one chunk or both. A chunk planned without keys reads to the end.
        floor = unit.lower if unit.lower_key is None else max(0, unit.lower - PAGE_OVERLAP)
        start, after = _resume(start_index, checkpoint, floor, unit.lower_key)
        pages = self._unit_pages(_unit_query(unit), start_index=start, after=after, until=unit.upper_key)
```

`_pages` and `_unit_pages` take `until` in place of `stop`, pass it to `OffsetWindow`, drop the `request_limit == 0` early return, and read with `window.limit`.

- [ ] **Step 8: Run the Emby suites**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_contract.py tests/unit/test_adapters_emby_planning.py tests/unit/test_adapters_emby_paging.py -p no:randomly -q` — Expected: pass.

- [ ] **Step 9: Docs**

PRD 03, the units bullet: after *"…each library's episodes in chunks of `USHER_SYNC_UNIT_MAX_ITEMS` (default **100,000**), the largest library first."* add:

> Each chunk after the first is bounded by the creation time of the item at its start, read once when the walk is planned: a chunk starts 50 items before that item, further back if items have left the library since, and reads until it passes the next chunk's first item, so an item moving across a boundary is read by one chunk or both. A chunk planned before chunks carried creation times reads to the end of its library.

`CHANGELOG.md` → `### Fixed`:

> - **An episode chunk no longer skips items that move across its boundary.** Chunks are bounded by the creation time of the item at each boundary, read when the walk is planned, and each reads until it passes the next chunk's first item. A chunk planned before this release reads to the end of its library.

- [ ] **Step 10: Full gate, then commit**

```bash
git add -A src/usher/adapters/emby tests/unit docs/prd/03-sources-and-sync.md CHANGELOG.md
git commit -m "emby: episode chunks are bounded by the creation time at each boundary"
```

---

### Task 5: The watch lane never resumes; abandoned watch runs are closed; `usher sync`'s exit

**Files:**
- Modify: `src/usher/domain/sync.py` (`ABANDONED_ERROR`), `src/usher/ports/repository/sync.py` (abstract `close_abandoned`; docstrings), `src/usher/db/repositories/sync.py`, `tests/fakes/sync_run_repository.py`
- Modify: `src/usher/services/watch_sync.py` (`sync`, `_walk`, `_Progress`; delete `_superseding`, `_supersede`, `SUPERSEDED_ERROR`, `DELTA_SUPERSEDED_ERROR`), `src/usher/db/models/sync.py` (`SyncRunRow`'s `position` note), `src/usher/cli.py` (`_sync`)
- Test: `tests/contract/sync_run_repository_contract.py`, `tests/unit/test_services_watch_sync.py`, `tests/integration/test_services_watch_sync.py`, `tests/unit/test_cli_errors.py`
- Docs: PRD 03 (the *A watch-lane delta is resumable* paragraph), PRD 02 (`sync_runs` row), `.claude/rules/emby-push-and-ingest.md` (the gap-closer paragraph's last two sentences), `CHANGELOG.md`

**Interfaces:**
- Produces: `usher.domain.sync.ABANDONED_ERROR: str`; abstract `SyncRunRepository.close_abandoned(self, source_id: uuid.UUID, kinds: Sequence[SyncRunKind], *, now: datetime, error: str) -> int` — closes the source's rows of those kinds that are `running` and not `is_live` at `now`, `failed` with `error`, `error_code` null and `finished_at = now`, returning how many. Task 6 calls it with `_ITEM_LANES`.
- Removes: `watch_sync.SUPERSEDED_ERROR`, `watch_sync.DELTA_SUPERSEDED_ERROR`, the `usher.resumed_from` span attribute. The port's `watch_state(since, *, start_index=0)` keeps its parameter; the lane always passes 0.

- [ ] **Step 1: Write the failing contract cases**

```python
    async def test_close_abandoned_closes_only_running_rows_past_the_stale_bound(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        now = LATER
        stale = run(source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=now - STALE_AFTER)
        silent = run(source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None)
        alive = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            heartbeat_at=now - STALE_AFTER + timedelta(microseconds=1),
        )
        finished = run(
            source_id, kind=SyncRunKind.WATCH_STATE, status=SyncRunStatus.FAILED, error="its own"
        )
        other_kind = run(source_id, kind=SyncRunKind.FULL, heartbeat_at=None)
        other_source = run(other_source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None)
        for one in (stale, silent, alive, finished, other_kind, other_source):
            await repository.add(one)
        assert not is_live(stale, now) and not is_live(silent, now), "the premise: both dead"
        assert is_live(alive, now), "the premise: one microsecond short of stale is alive"

        closed = await repository.close_abandoned(
            source_id, (SyncRunKind.WATCH_STATE,), now=now, error=ABANDONED_ERROR
        )

        assert closed == 2
        for dead in (stale, silent):
            stored = await repository.get(dead.id)
            assert stored is not None
            assert (stored.status, stored.error, stored.error_code, stored.finished_at) == (
                SyncRunStatus.FAILED,
                ABANDONED_ERROR,
                None,
                now,
            )
        for untouched in (alive, finished, other_kind, other_source):
            assert await repository.get(untouched.id) == untouched

    async def test_close_abandoned_with_no_kinds_closes_nothing(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        dead = run(source_id, heartbeat_at=None)
        await repository.add(dead)
        assert await repository.close_abandoned(source_id, (), now=LATER, error="x") == 0
        assert await repository.get(dead.id) == dead
```

(`other_source_id` and `LATER` are the contract module's own.)

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_sync_run_repository_contract.py -k close_abandoned -p no:randomly -q` — Expected: FAIL, no such method.

- [ ] **Step 3: `ABANDONED_ERROR` and both implementations**

`domain/sync.py`, beside `CANCELLED_ERROR`:

```python
#: What a run closed by a later run of its lane says, its own process having stopped.
ABANDONED_ERROR = "abandoned: its process stopped before it finished"
```

The port's abstract method, docstring: *"Close this source's runs of `kinds` that are `running` and not live at `now`, `failed` with `error`. Returns how many. A live run is another process's, and is never touched."* Postgres:

```python
    async def close_abandoned(
        self, source_id: uuid.UUID, kinds: Sequence[SyncRunKind], *, now: datetime, error: str
    ) -> int:
        if not kinds:
            return 0
        result = await self._session.execute(
            update(SyncRunRow)
            .where(
                SyncRunRow.source_id == source_id,
                SyncRunRow.kind.in_(list(kinds)),
                SyncRunRow.status == SyncRunStatus.RUNNING,
                # `is_live`'s complement: no heartbeat, or one at least `STALE_AFTER` old.
                or_(
                    SyncRunRow.heartbeat_at.is_(None),
                    SyncRunRow.heartbeat_at <= now - STALE_AFTER,
                ),
            )
            .values(status=SyncRunStatus.FAILED, error=error, error_code=None, finished_at=now)
            .execution_options(synchronize_session="fetch")
        )
        return cast("CursorResult[Any]", result).rowcount
```

Fake:

```python
    async def close_abandoned(
        self, source_id: uuid.UUID, kinds: Sequence[SyncRunKind], *, now: datetime, error: str
    ) -> int:
        dead = [
            one
            for one in self._runs.values()
            if one.source_id == source_id
            and one.kind in kinds
            and one.status is SyncRunStatus.RUNNING
            and not is_live(one, now)
        ]
        for one in dead:
            self._runs[one.id] = one.evolve(
                status=SyncRunStatus.FAILED, error=error, error_code=None, finished_at=now
            )
        return len(dead)
```

Run the contract cases again — Expected: pass on both arms (`uv run pytest tests/unit/test_sync_run_repository_contract.py tests/integration/test_sync_run_repository.py -p no:randomly -q`).

- [ ] **Step 4: Write the failing watch-lane tests**

In `tests/unit/test_services_watch_sync.py`, the cases asserting `SUPERSEDED_ERROR`/`DELTA_SUPERSEDED_ERROR` or a resume in place (same run id, `position` carried on, `usher.resumed_from`) are rewritten to the cases below or deleted where nothing true is left to say. New cases — three matched states, so a read from 0 stores what a resume from the given run's position 2 would not:

```python
async def _three(fixture: _Fixture) -> None:
    for index in range(3):
        await fixture.given_matched(f"movie-{index}")


@pytest.mark.parametrize("heartbeat_at", [NOW - STALE_AFTER, None])
async def test_an_abandoned_watch_run_is_closed_and_a_fresh_one_reads_from_its_cursor(
    heartbeat_at: datetime | None,
) -> None:
    fixture = _Fixture()
    await _three(fixture)
    dead = await _given_watch_run(fixture, heartbeat_at=heartbeat_at)
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    finally:
        logger.remove(sink)

    closed = await fixture.runs.get(dead.id)
    assert closed is not None
    assert (closed.status, closed.error, closed.finished_at) == (
        SyncRunStatus.FAILED,
        ABANDONED_ERROR,
        NOW,
    )
    assert run.id != dead.id
    assert (run.status, run.cursor_at, run.items_seen) == (SyncRunStatus.COMPLETED, T0, 3)
    assert [await fixture.stored(f"movie-{index}") is not None for index in range(3)] == [
        True,
        True,
        True,
    ], "a state before the dead run's position was not read"
    assert [line.rstrip("\n") for line in lines] == [
        "closed 1 watch-state run(s) of Living Room Emby whose process had stopped"
    ]


async def test_a_failed_watch_run_is_not_resumed() -> None:
    fixture = _Fixture()
    await _three(fixture)
    failed = await _given_watch_run(fixture, heartbeat_at=None, status=SyncRunStatus.FAILED)
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.id != failed.id and run.items_seen == 3
    assert await fixture.runs.get(failed.id) == failed


async def test_a_live_watch_run_is_left_running() -> None:
    fixture = _Fixture()
    await _three(fixture)
    live = await _given_watch_run(
        fixture, heartbeat_at=NOW - STALE_AFTER + timedelta(microseconds=1)
    )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.id != live.id and run.status is SyncRunStatus.COMPLETED
    assert await fixture.runs.get(live.id) == live
```

(The fixture's default `_Clock(NOW)` does not step, so the bound is exact. If `fixture.stored` answers something other than `None` for a state never merged, assert on what it does answer.)

`tests/integration/test_services_watch_sync.py`: rewrite the #41 resume cases the same way — a dead unfinished run is closed abandoned and the next run reads from its cursor at `StartIndex` 0; a delta never reclaims a row.

- [ ] **Step 5: Run them to see them fail**

Run: `uv run pytest tests/unit/test_services_watch_sync.py -p no:randomly -q` — Expected: the new cases FAIL (the run is resumed or superseded).

- [ ] **Step 6: A watch run always starts afresh**

`sync()` from the span's start through the first commit becomes:

```python
            # This attempt's instant, bound once: the run's `started_at` and every
            # merge's `observed_at`.
            attempt_started = datetime.now(UTC)
            now = self._clock()
            # A dead run's row is closed, never resumed: a `StartIndex` into a listing
            # that has lost items since would skip some. A live one is another
            # process's, and this run walks beside it.
            closed = await self._runs.close_abandoned(
                source.id, (SyncRunKind.WATCH_STATE,), now=now, error=ABANDONED_ERROR
            )
            if closed:
                logger.warning(
                    "closed {count} watch-state run(s) of {source} whose process had stopped",
                    count=closed,
                    source=source.name,
                )
            cursor = await self._runs.latest_completed_cursor(source.id, SyncRunKind.WATCH_STATE)
            if cursor is not None and since_at_most is not None:
                # Never past the instant the caller's item walk began: a state saved
                # since then may be for an item that walk had not yet stored.
                cursor = min(cursor, since_at_most)
            run = SyncRun(
                source_id=source.id,
                kind=SyncRunKind.WATCH_STATE,
                cursor_at=cursor,
                started_at=attempt_started,
                heartbeat_at=now,
            )
            # Committed `RUNNING` before the walk, with the close above: an operator
            # watching a long sync needs a row to watch, and a killed process must leave
            # a trace rather than nothing.
            await self._runs.add(run)
            run_id = run.id
            try:
                await self._commit()
                ...  # Task 1's guarded walk, unchanged
```

`_walk` starts its reader at 0 (`self._read(adapter, cursor, 0, queue)`) and counts `seen` from 0; `position` is still saved with each batch as the states this run has committed. The failure log becomes `"watch-state sync of {source} failed after {seen} states: {error}"` with `seen=run.items_seen`. Delete `_superseding`, `_supersede`, both superseded constants and the `inherited`/`resumed_from` bindings and span attribute (grep `resumed_from` across `src`, `tests` and `docs` and remove what reads it). Rewrite the docstrings this touches: `sync` (no resume or supersede; *"Each run starts afresh from the lane's cursor, after closing this source's dead watch runs"*), `_Progress` (the `position` paragraph: a watch run's `position` counts its committed states and nothing reads it back), the port class docstring and `latest_incomplete_run`'s (*"A whole-library walk's"* — the watch lane no longer reads it), and `SyncRunRow`'s note on `position` for `watch_state`.

- [ ] **Step 7: `usher sync` counts a source's watch failures once**

In `cli.py`'s `_sync` loop:

```python
                mark = len(failed)
                hook = _watch_lane(pipeline.watch, source, adapter, user_id, failed)
                run = await pipeline.reconcile.reconcile(
                    source, SyncRunKind(kind), adapter, after_seed=hook
                )
                print(...)  # unchanged
                watch = await pipeline.watch.sync(
                    source, adapter, user_id=user_id, since_at_most=run.started_at
                )
                print(_watch_line(source, watch))
                # The watch run after the walk reads back to the instant the walk began,
                # covering the hook's run: it is this source's watch lane's last word.
                del failed[mark:]
                failed.extend(one for one in (run, watch) if one.status is SyncRunStatus.FAILED)
```

`tests/unit/test_cli_errors.py`: the case at line ~995 (`test_a_failed_watch_run_after_the_seed_fails_the_command_however_the_rest_ends`) becomes `test_a_watch_run_failed_by_the_hook_is_no_failure_once_the_run_after_the_walk_completes`, expecting exit 0; the case at ~1022 is re-premised as *the hook's run fails and the run after the walk, a row of its own, fails too* → exit names exactly one failed `watch_state` run; the case at ~1049 (same row completed) is deleted, since no run shares a row any more. Add a two-source case: the first source's after-walk watch run fails and the second source's whole sync completes → the exit still names that one `watch_state` failure.

- [ ] **Step 8: Run the narrow tests**

Run: `uv run pytest tests/unit/test_services_watch_sync.py tests/unit/test_cli_errors.py tests/unit/test_sync_run_repository_contract.py -p no:randomly -q` and `uv run pytest tests/integration/test_services_watch_sync.py tests/integration/test_sync_run_repository.py -p no:randomly -q` — Expected: pass.

- [ ] **Step 9: Docs**

PRD 03 — replace the whole *"**A watch-lane delta is resumable.** … a delta from the cursor or, with no cursor yet, a first walk."* paragraph with:

> **A watch-lane run is never resumed.** Each run starts afresh from the lane's cursor — never past the instant the item walk it follows began — so a failure that outlasts a page's retries costs a re-read from that cursor; a resumed `StartIndex` into a listing that has lost items since would skip some. **Until a source has completed one `watch_state` run, its watch lane has no cursor**, and its next run asks the source only for what the account has played or holds a resume position in — two filtered listings, a few requests on most libraries — and merges nothing else. Nothing schedules it.
> A watch run moves its heartbeat when it starts, with every batch it commits, and at least once a minute between commits, also while it waits on a page. Before it starts, a run closes its source's watch runs still `running` whose heartbeat is 10 minutes old or missing, `failed` with `abandoned: its process stopped before it finished`, and logs a WARNING counting them. One whose heartbeat is younger is alive and is left alone: the new run walks beside it, in a run of its own.

Also in PRD 03, wherever `usher sync`'s exit status for the watch lane is described (grep *"non-zero"* near the watch lane), state that the watch run after a walk is the source's last word: a failure of the run the seed started does not fail the command once the run after the walk completes.

PRD 02, the `sync_runs` row: *"One row per walk: an attempt that resumes an unfinished walk continues its row, and every other attempt starts one."* → *"One row per walk: an attempt that resumes an unfinished whole-library walk continues its row, and every other attempt starts one."*

`.claude/rules/emby-push-and-ingest.md`, the gap-closer paragraph — replace *"An unfinished first walk is superseded, never resumed (`watch_sync.SUPERSEDED_ERROR`): `save` only raises `position`, so its row cannot be reset. A `running` watch run whose heartbeat is under `STALE_AFTER` old is alive, first walk or delta, and is left alone: the next run walks beside it."* with:

> No watch run is resumed: each starts from the lane's cursor at `StartIndex` 0, after closing its source's dead watch runs `failed` (`ABANDONED_ERROR`). A `running` watch run whose heartbeat is under `STALE_AFTER` old is alive and left alone: the next run walks beside it.

`CHANGELOG.md` → `### Changed`:

> - **The watch lane no longer resumes an unfinished run**, withdrawing #41's resume: a `StartIndex` into a listing that has lost items since skips them. Each watch run starts afresh from the lane's cursor, so a failed delta costs a re-read from that cursor, and a run whose process stopped is closed `failed` with `abandoned: its process stopped before it finished` by the next. `usher sync` takes the watch run after a walk as that source's last word, so a failure the run after it covers no longer fails the command.

- [ ] **Step 10: Full gate, then commit**

```bash
git add -A src/usher tests docs/prd .claude/rules/emby-push-and-ingest.md CHANGELOG.md
git commit -m "watch: a run never resumes; the next one closes a dead run instead"
```

---

### Task 6: Every item walk beats; `planned`; abandoned item walks are closed

**Files:**
- Modify: `src/usher/domain/sync.py` (`SyncRun.planned`; `heartbeat_at` comment; `is_live` docstring), `src/usher/db/models/sync.py` (`SyncRunRow.planned`; `heartbeat_at` comment), `src/usher/db/migrations/versions/m10h_sync_correctness.py`, `src/usher/db/repositories/sync.py` (`_NEWEST_PLANNED`), `tests/fakes/sync_run_repository.py` (`_newest`), `src/usher/ports/repository/sync.py` (`latest_planned_run` docstring)
- Modify: `src/usher/services/reconcile.py` (`reconcile`, `_claim`, `_walk`, new `_Listed` and `_list`; delete `_supersede` and `WALK_SUPERSEDED_ERROR`)
- Test: `tests/unit/test_services_reconcile.py`, `tests/integration/test_sync_run_repository.py`, `tests/integration/test_migrations.py`, `tests/integration/test_services_reconcile.py`
- Docs: PRD 03 (the resume paragraph), PRD 02 (`sync_runs` row), `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 5's `close_abandoned` and `ABANDONED_ERROR`; Task 1's guarded walk.
- Produces: `SyncRun.planned: bool = False`; `sync_runs.planned BOOLEAN NOT NULL DEFAULT false`; `latest_planned_run` reads `planned` rows; `ReconcileService._claim(source, kind, now)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_services_reconcile.py` (`_given_walk` gains `planned=True` on the `SyncRun` it stores; cases that asserted `WALK_SUPERSEDED_ERROR` become abandoned-error cases):

```python
async def test_a_single_walk_is_stored_with_a_heartbeat_and_not_planned(fixture: _Fixture) -> None:
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
    )
    assert fixture.checkpoints[0][1] is not None, "the run's first commit carried no heartbeat"
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.planned is False


async def test_a_planned_walk_is_stored_planned(fixture: _Fixture) -> None:
    _shelve(fixture, "Films", range(2))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.planned is True


async def test_a_single_walk_beats_while_a_page_is_slow() -> None:
    fixture = _Fixture(heartbeat_seconds=0.01)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    release = asyncio.Event()

    async def slow(since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        await release.wait()
        yield _item("m1")

    fixture.adapter.list_items = slow  # type: ignore[method-assign]
    before = len(fixture.checkpoints)
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    )
    await _until_async(lambda: len(fixture.checkpoints) >= before + 3)
    release.set()
    run = await task
    beats = [heartbeat for _, heartbeat in fixture.checkpoints[before:]]
    assert run.status is SyncRunStatus.COMPLETED
    assert all(b is not None for b in beats) and beats == sorted(beats) and len(set(beats)) == len(beats)


async def test_a_single_walk_beats_with_each_batch() -> None:
    fixture = _Fixture(batch_size=2)
    for index in range(4):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    before = len(fixture.checkpoints)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    batches = [cp for cp in fixture.checkpoints[before:] if cp[0] in (2, 4)]
    assert [seen for seen, _ in batches][:2] == [2, 4]
    assert batches[0][1] is not None and batches[0][1] < batches[1][1]


async def test_a_single_walks_listing_error_drops_the_partial_batch() -> None:
    fixture = _Fixture(batch_size=2)
    for index in range(5):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.adapter.fail_after(3)
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
    )
    assert (run.status, run.items_seen) == (SyncRunStatus.FAILED, 2)


async def test_a_truncated_single_walk_does_not_wait_for_the_listing(fixture: _Fixture) -> None:
    async def endless(since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        for index in range(3):
            yield _item(f"m{index}")
        await asyncio.Event().wait()

    fixture.adapter.list_items = endless  # type: ignore[method-assign]
    run = await asyncio.wait_for(
        fixture.service.reconcile(
            fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False, max_items=2
        ),
        5.0,
    )
    assert (run.status, run.error_code, run.items_seen) == (
        SyncRunStatus.FAILED,
        CEILING_ERROR_CODE,
        2,
    )


@pytest.mark.parametrize("heartbeat_at", [NOW - STALE_AFTER, None])
async def test_a_dead_item_walk_is_closed_abandoned_before_the_next_walk(
    heartbeat_at: datetime | None,
) -> None:
    fixture = _Fixture(clock=lambda: NOW)
    dead = SyncRun(
        source_id=fixture.source.id, kind=SyncRunKind.DELTA, heartbeat_at=heartbeat_at, started_at=T0
    )
    await fixture.runs.add(dead)
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    finally:
        logger.remove(sink)
    closed = await fixture.runs.get(dead.id)
    assert closed is not None
    assert (closed.status, closed.error, closed.finished_at) == (
        SyncRunStatus.FAILED,
        ABANDONED_ERROR,
        NOW,
    )
    assert [line.rstrip("\n") for line in lines] == [
        "closed 1 item walk(s) of Living Room Emby whose process had stopped"
    ]


async def test_a_live_single_walk_is_left_running() -> None:
    fixture = _Fixture(clock=lambda: NOW)
    live = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.DELTA,
        heartbeat_at=NOW - STALE_AFTER + timedelta(microseconds=1),
        started_at=T0,
    )
    await fixture.runs.add(live)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    assert await fixture.runs.get(live.id) == live


async def test_a_dead_planned_walk_is_closed_then_resumed_in_place() -> None:
    fixture = _Fixture(clock=lambda: NOW)
    _shelve(fixture, "Films", range(2))
    dead = await _given_walk(
        fixture, heartbeat_at=NOW - STALE_AFTER, units=[("Films", SyncRunUnitStatus.PENDING, 0)]
    )
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    finally:
        logger.remove(sink)
    assert (run.id, run.status) == (dead.id, SyncRunStatus.COMPLETED)
    assert "closed 1 item walk(s) of Living Room Emby whose process had stopped" in [
        line.rstrip("\n") for line in lines
    ]
```

(`_until_async` is Task 1's `_until` with `await asyncio.sleep(0.005)` instead of `0`, since beats wait on wall time. The listing-error and truncation cases pass before and after — the old plain loop already dropped the partial batch and broke without waiting — and guard the reader rewrite; every other case must fail first. Keep the existing refusal cases: one microsecond short of `STALE_AFTER` still raises `WalkRefused`.)

`tests/integration/test_sync_run_repository.py`: rename `test_the_latest_planned_run_passes_over_a_newer_run_without_a_heartbeat` to `test_the_latest_planned_run_passes_over_a_newer_unplanned_run`; its newer row now carries a heartbeat and `planned=False`, the older one `planned=True`. Add `planned` round-trip coverage if the contract's round-trip case does not already compare every field.

`tests/integration/test_migrations.py`: a backfill case on the `test_m10b_gives_an_existing_sync_run_a_zero_position` pattern:

```python
async def test_m10h_marks_the_item_walks_that_carry_a_heartbeat_planned(postgres_url: str) -> None:
    """Below `m10h` a heartbeat on an item lane meant a whole-library walk; above, `planned` does."""
    admin, scratch, url = await scratch_database(postgres_url, "planned")
    source_id = new_id()
    rows = {
        new_id(): ("full", True, True),
        new_id(): ("delta", True, True),
        new_id(): ("full", False, False),
        new_id(): ("delta", False, False),
        new_id(): ("watch_state", True, False),
    }
    try:
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10g", direction="up"))
        scratch_engine = build_engine(url)
        try:
            async with scratch_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO sources "
                        "(id, kind, name, base_url, credentials_ref, device_id) "
                        "VALUES (:id, 'emby', 'Planned Backfill', 'http://example.invalid', "
                        "'unused', 'unused')"
                    ),
                    {"id": source_id},
                )
                for run_id, (kind, beating, _) in rows.items():
                    await conn.execute(
                        text(
                            "INSERT INTO sync_runs (id, source_id, kind, status, heartbeat_at) "
                            "VALUES (:id, :source_id, :kind, 'failed', "
                            "CASE WHEN :beating THEN now() END)"
                        ),
                        {"id": run_id, "source_id": source_id, "kind": kind, "beating": beating},
                    )
                assert "planned" not in await column_set(url, "sync_runs"), (
                    "the premise: what reads back below was written by `m10h.upgrade()`"
                )
            await asyncio.to_thread(run_alembic, url, "head")
            async with scratch_engine.connect() as conn:
                stored = dict(
                    (await conn.execute(text("SELECT id, planned FROM sync_runs"))).tuples().all()
                )
            assert stored == {run_id: planned for run_id, (_, _, planned) in rows.items()}
        finally:
            await scratch_engine.dispose()
    finally:
        await drop_database(admin, scratch)
```

(`enum_column` stores a `StrEnum`'s *value*, so the kinds are `'full'`, `'delta'` and `'watch_state'`, as the backfill spells them. If `CASE WHEN :beating` will not bind a Python `bool`, pass `"heartbeat": datetime.now(UTC) if beating else None` instead.) And in the `-1` block (still landing at `m10g`), beside Task 3's assertion: `assert "planned" not in await column_set(url, "sync_runs")`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_services_reconcile.py -p no:randomly -q` — Expected: the new cases FAIL.

- [ ] **Step 3: The column and its backfill**

`SyncRun.planned: bool = False`, commented *"A whole-library walk's: the rows a walk's claim and `live_walk` read."* `SyncRunRow.planned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))`. `m10h` (module docstring now *"Sync correctness: a unit's resume checkpoint and a whole-library walk's flag."*):

```python
    op.add_column(
        "sync_runs",
        sa.Column("planned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # Until now a heartbeat on an item lane meant a planned walk.
    op.execute(
        "UPDATE sync_runs SET planned = true "
        "WHERE kind IN ('full', 'delta') AND heartbeat_at IS NOT NULL"
    )
```

(`enum_column` binds a `StrEnum`'s value, so the stored kinds are lowercase.) Downgrade drops the column first, before Task 3's. `_NEWEST_PLANNED` swaps `heartbeat_at IS NOT NULL` for `planned`; the fake's `_newest` filters on `one.planned or not planned`; `latest_planned_run`'s docstring says *planned*, not *has a heartbeat*. Rewrite the `heartbeat_at` comments (domain and model) and `is_live`'s docstring: every walk moves its heartbeat; a row with none predates heartbeats, and is never live.

- [ ] **Step 4: Every item walk beats; each walk first closes dead ones**

`reconcile()`:

```python
            cursor = await self.cursor_for(source, kind)
            planned = plan and not max_items and (kind is SyncRunKind.FULL or cursor is None)
            now = self._clock()
            closed = await self._runs.close_abandoned(
                source.id, _ITEM_LANES, now=now, error=ABANDONED_ERROR
            )
            if closed:
                logger.warning(
                    "closed {count} item walk(s) of {source} whose process had stopped",
                    count=closed,
                    source=source.name,
                )
                # Committed at once, so a walk refused below still leaves them closed.
                await self._commit()
            try:
                claimed = await self._claim(source, kind, now) if planned else None
            except WalkRefused:
                ...  # unchanged
            if claimed is None:
                run = SyncRun(
                    source_id=source.id, kind=kind, cursor_at=cursor, heartbeat_at=now, planned=planned
                )
                ...
            else:
                run = claimed.evolve(..., heartbeat_at=now)
```

`_claim(source, kind, now)`:

```python
        newest = await self._runs.latest_incomplete_run(source.id, kind, planned=True)
        if newest is None:
            return None
        if newest.heartbeat_at is not None and is_live(newest, now):
            raise WalkRefused(_refusal(source, now - newest.heartbeat_at))
        if await self._runs.units_for(newest.id) and newest.error_code != RETRACTION_ERROR_CODE:
            return newest
        return None
```

Its docstring loses the supersede sentences: a dead run was closed before the claim, so a row read here is live (refused), resumable (units, no refused sweep), or finished and left as it is. Delete `_supersede` and `WALK_SUPERSEDED_ERROR` (grep for both).

`_walk` becomes a reader task and a writer, as `WatchStateSyncService._walk` already is:

```python
@dataclass(frozen=True, slots=True)
class _Listed:
    """What a single walk's reader hands the writer: an item, the listing's end, or its error."""

    item: SourceItem | None = None
    error: Exception | None = None
```

```python
    async def _walk(self, source, progress, adapter, cursor, max_items) -> bool:
        """Walk the source into the catalog: a reader lists while this task writes.

        `True` when it stopped at `max_items` with the source still holding more. Every
        batch commits with a new heartbeat, and a beat of the heartbeat alone falls due
        `heartbeat_seconds` after the last commit or beat. The reader's error is raised
        after every item it read ahead of it, the partial batch uncommitted. However
        the walk ends, the reader is cancelled and awaited first.
        """
        queue: asyncio.Queue[_Listed] = asyncio.Queue(maxsize=self._batch_size)
        reader = asyncio.create_task(self._list(adapter, cursor, queue))
        try:
            batch: list[SourceItem] = []
            pulled = 0
            truncated = False
            due = time.monotonic() + self._heartbeat_seconds
            while True:
                if time.monotonic() >= due:
                    await self._beat(progress)
                    due = time.monotonic() + self._heartbeat_seconds
                    continue
                try:
                    listed = await asyncio.wait_for(queue.get(), max(0.0, due - time.monotonic()))
                except TimeoutError:
                    continue
                if listed.error is not None:
                    raise listed.error
                if listed.item is None:
                    break
                pulled += 1
                # `>`, not `>=`, and the difference is a whole cursor.
                if max_items and pulled > max_items:
                    truncated = True
                    break
                batch.append(listed.item)
                if len(batch) >= self._batch_size:
                    progress.run = await self._flush(
                        source, progress.run.evolve(heartbeat_at=self._clock()), batch
                    )
                    batch = []
                    due = time.monotonic() + self._heartbeat_seconds
            if batch:
                # The trailing partial batch, on both exits.
                progress.run = await self._flush(
                    source, progress.run.evolve(heartbeat_at=self._clock()), batch
                )
            return truncated
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @staticmethod
    async def _list(
        adapter: SourceAdapter, cursor: AwareDatetime | None, queue: asyncio.Queue[_Listed]
    ) -> None:
        """The reader: put each item the listing yields on `queue`, then its end.

        It never touches the database. An error the listing raises goes on the queue in
        place of the end, and the reader stops.
        """
        try:
            async for item in adapter.list_items(since=cursor):
                await queue.put(_Listed(item))
        except Exception as exc:
            await queue.put(_Listed(error=exc))
            return
        await queue.put(_Listed())
```

- [ ] **Step 5: Run the narrow tests, then the integration modules**

Run: `uv run pytest tests/unit/test_services_reconcile.py tests/unit/test_sync_run_repository_contract.py -p no:randomly -q` — Expected: pass.
Run: `uv run pytest tests/integration/test_sync_run_repository.py tests/integration/test_migrations.py tests/integration/test_services_reconcile.py -p no:randomly -q` — Expected: pass.

- [ ] **Step 6: Docs**

PRD 03, the resume paragraph — replace *"A run whose sweep was refused is not resumed, and neither is one that stopped before its units were stored or a full run from before units existed: a fresh walk starts, and such a run left `running` is closed `failed` with `superseded: a whole-library walk restarts`. A delta's claim reads only planned walks, so the gap-closer's walk of a source with no cursor never stands in its way."* with:

> Every item walk moves its heartbeat with every commit and at least once a minute between commits, and before it starts closes its source's item walks still `running` whose heartbeat is 10 minutes old or missing, `failed` with `abandoned: its process stopped before it finished`, logging a WARNING that counts them; a whole-library walk closed this way is resumed all the same. A run whose sweep was refused is not resumed, and neither is one that stopped before its units were stored: a fresh walk starts. A walk's claim reads only whole-library walks, so a single walk's run — the gap-closer's, say — never stands in its way.

PRD 02, the `sync_runs` row: *"…and `heartbeat_at`, which a whole-library walk's writer moves on every commit and the watch lane when it starts a run, with every batch and at least once a minute between batches ([03](03-sources-and-sync.md))."* → *"…`heartbeat_at`, which every walk moves when it starts, with every commit and at least once a minute between commits ([03](03-sources-and-sync.md)); and `planned`, true for a whole-library walk."*

`CHANGELOG.md` → `### Fixed`:

> - **An item walk whose process died is closed by the next walk of its source** instead of staying `running`. Every item walk now moves its heartbeat, single walks included, and each walk first closes its source's item walks that are `running` with a heartbeat 10 minutes old or none, `failed` with `abandoned: its process stopped before it finished`. A whole-library walk is marked as one (`sync_runs.planned`, migration `m10h`), and one closed this way is still resumed.

- [ ] **Step 7: Full gate, then commit**

```bash
git add -A src/usher tests docs/prd CHANGELOG.md
git commit -m "sync: every item walk beats, and the next walk closes a dead one (planned, m10h)"
```

---

### Task 7: The trigger keeps a source merge's instant

**Files:**
- Modify: `src/usher/db/migrations/versions/m10h_sync_correctness.py`, `src/usher/db/repositories/backup.py` (`_upsert_watch_state`)
- Modify: `tests/fakes/watch_state_repository.py` (its docstring's claim that it differs from Postgres)
- Test: `tests/integration/test_watch_state_repository.py`, `tests/integration/test_restore.py`, `tests/integration/test_migrations.py`
- Docs: PRD 03 (*Conflicts* line), `.claude/rules/db-and-sql.md` (the trigger rule), `.claude/rules/emby-push-and-ingest.md` (the history-backfill rule), `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 3's `m10h` file (extended again).
- Produces: SQL function `watch_states_set_updated_at()`; trigger `trg_watch_states_set_updated_at` (same name) now executes it.

- [ ] **Step 1: Enumerate every write that updates `watch_states`**

Run: `grep -rn "watch_states" src/usher/db/repositories | grep -i "update\|conflict"` and read each statement. Each must either set `origin` to something other than `'source'` (the trigger then stamps `now()`) or set `updated_at` itself. Expected today: the source merge's `_update` (sets `updated_at = d.observed_at, origin = 'source'`), the client upsert (`origin = 'api'`), and the restore upsert (adopts the artifact's `origin`, sets no `updated_at` — fixed below). Record any other in the report.

- [ ] **Step 2: Write the failing tests**

`tests/integration/test_watch_state_repository.py` (the rolled-back `session` fixture; the contract module's `WALK_AT` and `LATER` sit months before the transaction's frozen `now()`). `test_the_update_trigger_owns_updated_at` pins the old trigger (`updated != LATER`) and is replaced by the first case below; the second and third are new:

```python
async def _stored(session: AsyncSession, title_id: uuid.UUID) -> tuple[int, datetime]:
    row = (
        await session.execute(
            text("SELECT position_seconds, updated_at FROM watch_states WHERE title_id = :t"),
            {"t": title_id},
        )
    ).one()
    return row.position_seconds, row.updated_at


async def test_a_source_merge_keeps_its_own_instant_on_the_update_path(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    """`watch_states_set_updated_at` keeps `observed_at` on an update whose origin is the source.

    Two walks overlap and the one that began first commits first. The old trigger stamped
    that update with the transaction's `now()` -- later than the second walk began -- and
    the second walk's newer read was then refused.
    """
    await repository.merge_from_source([merge(user_id, title_id)])
    assert await _stored(session, title_id) == (0, WALK_AT), "the premise: the insert path"
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=999, observed_at=LATER)]
    )
    assert await _stored(session, title_id) == (999, LATER)


async def test_an_older_read_committed_after_a_newer_one_is_refused(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    await repository.merge_from_source(
        [merge(user_id, title_id, observed_at=WALK_AT - timedelta(days=1))]
    )
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=999, observed_at=LATER)]
    )
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=5, observed_at=WALK_AT)]
    )
    assert await _stored(session, title_id) == (999, LATER)


async def test_a_client_write_over_a_source_row_is_still_stamped_now(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    await repository.merge_from_source([merge(user_id, title_id)])
    await repository.set_from_client(write(user_id, title_id, position_seconds=30))
    now = (await session.execute(text("SELECT now()"))).scalar_one()
    assert await _stored(session, title_id) == (30, now)
```

(`merge` and `write` are the contract module's builders, already imported here; fit `merge`'s default `position_seconds` in the first premise to what it really is. Update `FakeWatchStateRepository`'s docstring, which says it differs from Postgres by storing `observed_at` on both paths: now they agree.)

`tests/integration/test_restore.py`: a restored row that conflicts with a stored one, carries different values and `origin = 'source'`, reads back `updated_at == SELECT now()`, the stored row having been backdated first.

`tests/integration/test_migrations.py`, the `-1` block at `m10g`: `watch_states_set_updated_at` is absent from `pg_proc` (with the premise that `set_updated_at` is present), and `trg_watch_states_set_updated_at`'s function is `set_updated_at`:

```python
SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
WHERE t.tgname = 'trg_watch_states_set_updated_at'
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/integration/test_watch_state_repository.py tests/integration/test_restore.py -k "read or client or restored" -p no:randomly -q` — Expected: the newer-read and restore cases FAIL.

- [ ] **Step 4: The trigger, and the restore's own stamp**

`m10h` (module docstring: *"Sync correctness: a unit's checkpoint, a walk's planned flag, a source merge's own instant."*), appended to `upgrade()`:

```python
    op.execute(
        """
        CREATE FUNCTION watch_states_set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.origin IS DISTINCT FROM 'source' THEN
                NEW.updated_at = now();
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER trg_watch_states_set_updated_at ON watch_states")
    op.execute(
        "CREATE TRIGGER trg_watch_states_set_updated_at BEFORE UPDATE ON watch_states "
        "FOR EACH ROW EXECUTE FUNCTION watch_states_set_updated_at()"
    )
```

`downgrade()` begins with the reverse: drop the trigger, re-create it on `set_updated_at()`, then `DROP FUNCTION watch_states_set_updated_at()`; then Task 6's and Task 3's drops.

`backup.py`'s `_upsert_watch_state`:

```python
    assignments = ", ".join(f"{column} = excluded.{column}" for column in _WATCH_STATE_MERGED)
    # Stamped here, not by the trigger, which keeps the `updated_at` of a row whose
    # origin is the source: a restore is a write made now, whatever its origin.
    assignments += ", updated_at = now()"
```

(`updated_at` stays out of the `IS DISTINCT FROM` comparison.)

- [ ] **Step 5: Run the narrow tests**

Run: `uv run pytest tests/integration/test_watch_state_repository.py tests/integration/test_restore.py tests/integration/test_migrations.py -p no:randomly -q` — Expected: pass. If `test_migration_creates_the_updated_at_triggers` pins each trigger's function as well as its name, give `watch_states` its new function there.

- [ ] **Step 6: Docs**

PRD 03 — *"- **Conflicts:** latest `updated_at` wins."* → *"- **Conflicts:** latest wins: a source's read carries the instant its walk began, a client's write the instant it was made, and whichever commits first, an older read never overwrites a newer write or read."*

`.claude/rules/db-and-sql.md`, the trigger rule: replace *"Triggers assign `now()` unconditionally, `BEFORE UPDATE`, so a merge's own `updated_at = observed_at` lands on the *insert* path only and two updates in one transaction read back one stamp."* with:

> Triggers assign `now()` `BEFORE UPDATE`, unconditionally except on `watch_states`, whose `watch_states_set_updated_at` keeps an update's own `updated_at` when the new `origin` is `'source'` — so a source merge's `observed_at` lands on both paths, and the restore upsert stamps `now()` itself. Elsewhere two updates in one transaction read back one stamp.

`.claude/rules/emby-push-and-ingest.md`, the history-backfill rule: replace *"**A history backfill must carry its own fresh `observed_at`, and both test layers are blind to why.** The trigger stamps the *write* instant, so a backfill carrying the walk's instant is refused by the row it exists to repair; the fake accepts what Postgres refuses and `now()` is frozen per transaction."* with:

> - **A history backfill must carry its own fresh `observed_at`, and both test layers are blind to why.** It is a newer read than the walk, and a row a client wrote since the walk began refuses the walk's older instant; the fake accepts what Postgres refuses and `now()` is frozen per transaction.

(keep that bullet's last sentences, on `resolve_seasons`/`resolve_episodes`, as they are).

`CHANGELOG.md` → `### Fixed`:

> - **A watch merge that read newer state is no longer refused because an older read committed first.** The `watch_states` trigger keeps a source merge's own `updated_at` — the instant its walk began — instead of stamping the commit's (migration `m10h`); a client write and a restore still stamp `now()`.

- [ ] **Step 7: Full gate, then commit**

```bash
git add -A src/usher tests docs/prd/03-sources-and-sync.md .claude/rules CHANGELOG.md
git commit -m "db: watch_states keeps a source merge's own instant, so the newer read wins (m10h)"
```

---

### Task 8: Status rows (controller)

**Files:**
- Modify: `docs/plans/progress.md` (the *Fast first sync* and *Sync correctness* tables), `docs/prd/README.md` (both plans' rows)

- [ ] **Step 1:** In `progress.md`'s *Fast first sync* table, rows 10–20's status becomes `✅ landed (PR #95)`; in `docs/prd/README.md`, the fast-first-sync cell's *"Phase 2 in review (PR #95)"* becomes *"Phase 2 landed (PR #95)"*.
- [ ] **Step 2:** Once this branch's PR is open, the *Sync correctness* rows 1–7 and 8 read `🔨 in review (PR #N)`, and so does its README row.
- [ ] **Step 3:** `uv run pytest tests/unit/test_docs_currency.py -p no:randomly -q`, then commit `docs(plan): sync correctness is in review`.
