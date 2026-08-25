# Resumable Watch Lane (issue #41) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the watch-state walk resumable from a `StartIndex` checkpoint so a
transient failure costs one page instead of restarting an ~11-hour full-history
walk, which is what lets `USHER_WORKER_ENABLED` go back on.

**Architecture:** `SyncRun` gains a `position` (the `StartIndex` reached, advanced
per committed batch). `WatchStateSyncService.sync` reclaims the newest *incomplete*
`WATCH_STATE` run in place — keeping its id, `cursor_at` and `started_at` — and
resumes the walk from that position; `SourceAdapter.watch_state` grows a
`start_index` parameter that the Emby adapter feeds to its existing `StartIndex`
paging, decoupled from `list_items`. Resumption is sound because the walk is
ordered by the **immutable** `DateCreated`, so the walked prefix never reorders.

**Tech Stack:** Python 3.13, pydantic v2 (frozen `DomainModel`, `.evolve()`),
SQLAlchemy 2 async + asyncpg, Alembic, PostgreSQL 17 (pgvector), pytest +
testcontainers, `uv` for everything.

**Design spec:** `docs/specs/2026-08-21-issue-41-resumable-watch-lane-design.md`.
**ADR:** `docs/prd/decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md`.

---

## Before you start

Run the gate once on a clean tree so you know the baseline is green and every
later red is yours:

```bash
cd ~/code/usher-41
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports
uv run pytest tests/unit
```

Integration tests need Docker (`testcontainers`, `pgvector/pgvector:pg17`).

**Two repo rules that bite on this change:**
- **Domain models are frozen.** Use `.evolve(**changes)`, never
  `model_copy(update=...)`.
- **`position` is a Postgres keyword.** SQLAlchemy quotes the column
  automatically, but a `CheckConstraint`'s raw SQL text does not go through that
  quoting — write `'"position" >= 0'`. `curated_rows` already does exactly this
  (`src/usher/db/models/curation.py:352`).

## File structure

| File | Change | Responsibility |
|---|---|---|
| `src/usher/domain/sync.py` | Modify | `SyncRun.position` — the StartIndex a resumed walk starts from |
| `src/usher/db/models/sync.py` | Modify | `sync_runs.position` column + non-negative CHECK |
| `src/usher/db/migrations/versions/m10b_watch_lane_resume.py` | Create | Adds the column and its CHECK |
| `src/usher/ports/repository/sync.py` | Modify | `latest_incomplete_run` on the port |
| `src/usher/db/repositories/sync.py` | Modify | Its Postgres statement |
| `tests/fakes/sync_run_repository.py` | Modify | Its in-memory arm |
| `src/usher/ports/source.py` | Modify | `watch_state(since, start_index)` |
| `src/usher/adapters/emby/adapter.py` | Modify | `_walk` starts at `start_index`; `list_items` stays at 0 |
| `tests/fakes/source_adapter.py` | Modify | Fake walk honours `start_index` |
| `src/usher/services/watch_sync.py` | Modify | Reclaim-and-resume; `position` advanced per batch |
| `tests/contract/sync_run_repository_contract.py` | Modify | Contract cases for `latest_incomplete_run` |
| `tests/integration/test_migrations.py` | Modify | Re-point the `-1` block at the new head |
| Docs (Task 6) | Modify | ADR status, two stale docstrings, PRD 03 |

---

### Task 1: `SyncRun.position` — domain, ORM and migration

**Files:**
- Modify: `src/usher/domain/sync.py:56-78`
- Modify: `src/usher/db/models/sync.py`
- Create: `src/usher/db/migrations/versions/m10b_watch_lane_resume.py`
- Modify: `tests/integration/test_migrations.py`
- Test: `tests/unit/test_domain_sync.py`, `tests/integration/test_migrations.py`

- [ ] **Step 1: Write the failing domain test**

Append to `tests/unit/test_domain_sync.py`:

```python
def test_a_run_starts_at_position_zero_and_refuses_a_negative_one() -> None:
    """`position` is the StartIndex a resumed walk starts from, and it is a
    separate field from `items_seen` on purpose: the port permits an adapter
    to yield the same item twice, under which `items_seen` outruns the page
    position, and resuming from the counter would then skip.
    """
    one = SyncRun(source_id=new_id(), kind=SyncRunKind.WATCH_STATE)
    assert one.position == 0

    resumed = one.evolve(position=51_000)
    assert resumed.position == 51_000

    with pytest.raises(ValidationError):
        one.evolve(position=-1)
```

Add whatever of these imports the file does not already have, in isort order:

```python
import pytest
from pydantic import ValidationError

from usher.domain.ids import new_id
from usher.domain.sync import SyncRun, SyncRunKind
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_domain_sync.py::test_a_run_starts_at_position_zero_and_refuses_a_negative_one -v`
Expected: FAIL — `ValidationError` on the `evolve(position=…)` call, because
`SyncRun` is `extra="forbid"` and has no `position` field.

- [ ] **Step 3: Add the field**

In `src/usher/domain/sync.py`, extend the `SyncRun` docstring and add the field
immediately after `cursor_at`:

```python
class SyncRun(DomainModel):
    """One attempt at reconciling a source.

    `cursor_at` is the `since` this run was started from -- `None` for a
    full walk. The *next* cursor is `started_at` (widened by the adapter's
    own one-second rule), and it is advanced only by a run that completed,
    which is why it is read off this table rather than kept in memory.

    `position` is the walk's own resume point: the `start_index` a resumed
    walk asks the adapter for, advanced per **committed** batch. It is
    deliberately not `items_seen`. The two coincide on an adapter that
    yields each item once, and `SourceAdapter.watch_state` explicitly
    permits duplicates -- under which `items_seen` outruns the page position
    and resuming from the counter would skip whatever the difference is.
    Only the `WATCH_STATE` lane advances it (ADR-0042); the item lanes leave
    it at 0 and restart from their cursor.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    source_id: uuid.UUID
    kind: SyncRunKind
    status: SyncRunStatus = SyncRunStatus.RUNNING
    cursor_at: AwareDatetime | None = None
    position: int = Field(default=0, ge=0)

    items_seen: int = Field(default=0, ge=0)
```

- [ ] **Step 4: Run the domain test — it passes, and the ORM 1:1 test now fails**

Run: `uv run pytest tests/unit/test_domain_sync.py tests/unit/test_db_models.py -v`
Expected: the new domain case PASSES; `tests/unit/test_db_models.py` FAILS,
because it asserts `SyncRunRow`'s columns and `SyncRun`'s fields are 1:1 and the
column does not exist yet. That failure is the next step's instruction.

- [ ] **Step 5: Add the column and its CHECK**

In `src/usher/db/models/sync.py`, add the column after `cursor_at`:

```python
    cursor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The walk's resume point (ADR-0042). Quoted in the CHECK below because
    # `position` is a Postgres keyword; SQLAlchemy quotes the column itself,
    # and a constraint's raw SQL text does not go through that quoting --
    # `curated_rows."position"` sets the same precedent.
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
```

and the constraint into `__table_args__`, after the `items_retracted` one:

```python
        CheckConstraint("items_retracted >= 0", name="ck_sync_runs_items_retracted_non_negative"),
        CheckConstraint('"position" >= 0', name="ck_sync_runs_position_non_negative"),
    )
```

- [ ] **Step 6: Run the model tests**

Run: `uv run pytest tests/unit/test_db_models.py -v`
Expected: PASS.

- [ ] **Step 7: Write the migration**

Create `src/usher/db/migrations/versions/m10b_watch_lane_resume.py`:

```python
"""The watch lane's walk resumes from a StartIndex checkpoint.

Revision ID: m10b
Revises: m10a
Create Date: 2026-08-21

`WatchStateSyncService` read its cursor from the newest *completed*
`watch_state` run, so on a deployment where none had completed the walk was
cursorless -- the whole library, ~5,688 pages against the household this
project measures. Any transient failure recorded `FAILED`, which left no
completed run, which left no cursor, which restarted the walk. It never once
succeeded (issue #41).

This column is the resume point: the `StartIndex` the walk reached, advanced
per **committed** batch, so a crash costs the batch in flight rather than the
run. `NOT NULL DEFAULT 0` because every existing row describes a walk that
either finished or will be restarted from the top, and 0 is that.

**Only the `watch_state` lane advances it.** `FULL` and `DELTA` have a working
cursor and leave it at 0; ADR-0042 carries the argument, including why the
resume point is a page position rather than a `since` timestamp (the yielded
record carries no such field, the walk is not ordered by one, and the field is
mutable).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10b"
down_revision: str | Sequence[str] | None = "m10a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sync_runs",
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "ck_sync_runs_position_non_negative", "sync_runs", '"position" >= 0'
    )


def downgrade() -> None:
    op.drop_constraint("ck_sync_runs_position_non_negative", "sync_runs", type_="check")
    op.drop_column("sync_runs", "position")
```

- [ ] **Step 8: Re-point the migration test's `-1` block, and watch it break first**

Run: `uv run pytest tests/integration/test_migrations.py -v`
Expected: the `-1`-from-head half FAILS. This is the design, not a defect: `-1`
now lands on `m10a`'s applied state, so `m10a`'s own assertions (which had teeth)
are false there. `.claude/rules/db-and-sql.md` records this as the eleventh
landing in a row to do it.

Repair it: in `tests/integration/test_migrations.py`, move `m10a`'s displaced
assertions into the revision-pinned block (the file already has one, and the
`m10a` comment at ~line 639 shows the shape), and assert on **`m10b`'s own
artefact in the direction its `downgrade()` establishes**. `m10b` *creates* a
column, so the `-1` assertion is negative:

```python
        # **`m10b`'s artefact, re-pointed here the moment it became head** --
        # the twelfth landing in a row to do this. `m10b` *adds* a column, so
        # the assertion is negative: one step below head that column does not
        # exist, and only `m10b.upgrade()` creates it.
        assert "position" not in at_m10a_columns, "position should not exist below m10b"
```

reading `at_m10a_columns` the same way the existing block reads
`at_m09f_columns` — from `information_schema.columns` for `sync_runs` at the
`-1` revision.

- [ ] **Step 9: Run the migration tests**

Run: `uv run pytest tests/integration/test_migrations.py -v`
Expected: PASS, including `test_a_full_down_and_up_cycle_restores_every_index`
and `test_migration_matches_the_orm_metadata` (the ORM and the chain now agree).

- [ ] **Step 10: Commit**

```bash
git add src/usher/domain/sync.py src/usher/db/models/sync.py \
        src/usher/db/migrations/versions/m10b_watch_lane_resume.py \
        tests/unit/test_domain_sync.py tests/integration/test_migrations.py
git commit -m "feat(sync): sync_runs carries the walk's resume position (#41)"
```

---

### Task 2: `latest_incomplete_run` on the repository

The read that decides whether a walk resumes. "The newest run for
`(source, kind)`, returned **iff** it is not completed" — the "newest, and only
if not completed" shape is what stops a stale attempt being resumed after a
later run already completed.

**Files:**
- Modify: `src/usher/ports/repository/sync.py:67` (after `latest_completed_cursor`)
- Modify: `src/usher/db/repositories/sync.py`
- Modify: `tests/fakes/sync_run_repository.py`
- Test: `tests/contract/sync_run_repository_contract.py` (runs on both arms)

- [ ] **Step 1: Write the failing contract cases**

Append to the `SyncRunRepositoryContract` class in
`tests/contract/sync_run_repository_contract.py`:

```python
    async def test_the_newest_run_is_offered_for_resumption_when_it_did_not_complete(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A `FAILED` run is what a crashed walk leaves, and it carries the
        position that walk committed. `RUNNING` is what an abandoned claim
        leaves and is resumed on exactly the same terms."""
        failed = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.FAILED,
            started_at=EARLIER,
            position=51_000,
        )
        await repository.add(failed)

        found = await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE)
        assert found is not None
        assert found.id == failed.id
        assert found.position == 51_000
        assert found.started_at == EARLIER

    async def test_a_completed_newest_run_offers_nothing_to_resume(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """The premise this method exists for: a walk that finished is not
        resumed, it is followed by a fresh delta."""
        await repository.add(
            run(
                source_id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.COMPLETED,
                started_at=EARLIER,
            )
        )
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None

    async def test_an_older_failure_is_not_resumed_behind_a_newer_completion(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """**The case the "newest, and only if not completed" shape is for.**
        A repository that answered "the newest run that is not completed"
        would hand back the old failure forever, and every later walk would
        resume from a position a completed run has already passed.
        """
        await repository.add(
            run(
                source_id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.FAILED,
                started_at=EARLIER,
                position=51_000,
            )
        )
        await repository.add(
            run(
                source_id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.COMPLETED,
                started_at=LATER,
            )
        )
        assert EARLIER < LATER, "the premise: the completion really is the newer run"
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None

    async def test_resumption_is_scoped_by_kind_and_by_source(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """The two lanes walk different upstream methods under different
        filters, so an item-lane failure is not a watch-lane resume point --
        and neither is another source's."""
        await repository.add(
            run(source_id, kind=SyncRunKind.DELTA, status=SyncRunStatus.FAILED, position=7)
        )
        await repository.add(
            run(
                other_source_id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.FAILED,
                position=9,
            )
        )
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None

    async def test_a_source_that_has_never_run_offers_nothing_to_resume(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None
```

- [ ] **Step 2: Run them and watch them fail on both arms**

Run: `uv run pytest tests/unit/test_sync_run_repository_contract.py -v`
Expected: FAIL — `AttributeError: 'FakeSyncRunRepository' object has no attribute
'latest_incomplete_run'` on all five.

- [ ] **Step 3: Declare it on the port**

In `src/usher/ports/repository/sync.py`, add after `latest_completed_cursor`:

```python
    @abstractmethod
    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> SyncRun | None:
        """The newest run of this kind, **iff it did not complete** -- the
        walk a resumed run continues. `None` when the newest one completed,
        and when there is none at all.

        **"The newest, and only if it is not completed", never "the newest
        one that is not completed."** The second spelling hands back an old
        failure forever once a later run has completed, so every later walk
        resumes from a position that completed run has already passed.

        Used by the `WATCH_STATE` lane only (ADR-0042). The item lanes have a
        working cursor and restart from it; this lane's first walk is the
        whole library, so a failure has to cost a page rather than the run.
        """
```

- [ ] **Step 4: Implement the fake**

In `tests/fakes/sync_run_repository.py`, add after `latest_completed_cursor`:

```python
    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> SyncRun | None:
        # The *newest* run, and then a status test -- never "the newest one
        # that is not completed", which resumes an old failure forever once
        # something later has completed.
        found = [
            one
            for one in self._runs.values()
            if one.source_id == source_id and one.kind is kind
        ]
        if not found:
            return None
        newest = max(found, key=lambda one: (one.started_at, one.id))
        return None if newest.status is SyncRunStatus.COMPLETED else newest
```

- [ ] **Step 5: Run the fake arm**

Run: `uv run pytest tests/unit/test_sync_run_repository_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Implement the Postgres arm**

In `src/usher/db/repositories/sync.py`, add the statement beside `_CURSOR`:

```python
# `ORDER BY started_at DESC, id DESC LIMIT 1` and *then* a status test in
# Python, rather than `WHERE status <> 'completed'`: the second spelling
# answers an old failure forever once a later run has completed. `id` breaks a
# tie on `started_at` for the reason `_LIST` does. Served by
# ix_sync_runs_source_kind_started's first qualifying entry.
_INCOMPLETE = """
SELECT * FROM sync_runs
WHERE source_id = :source_id AND kind = :kind
ORDER BY started_at DESC, id DESC
LIMIT 1
"""
```

and the method after `latest_completed_cursor`:

```python
    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> SyncRun | None:
        with self._session.no_autoflush:
            found = (
                (
                    await self._session.execute(
                        text(_INCOMPLETE), {"source_id": source_id, "kind": kind.value}
                    )
                )
                .mappings()
                .one_or_none()
            )
        if found is None:
            return None
        newest = SyncRun.model_validate(dict(found))
        return None if newest.status is SyncRunStatus.COMPLETED else newest
```

Add `SyncRunStatus` to the existing domain import at the top of the file:

```python
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
```

- [ ] **Step 7: Run the Postgres arm**

Run: `uv run pytest tests/integration/test_sync_run_repository.py -v`
Expected: PASS (needs Docker).

- [ ] **Step 8: Commit**

```bash
git add src/usher/ports/repository/sync.py src/usher/db/repositories/sync.py \
        tests/fakes/sync_run_repository.py tests/contract/sync_run_repository_contract.py
git commit -m "feat(sync): latest_incomplete_run, the read a resumed walk starts from (#41)"
```

---

### Task 3: `watch_state(start_index=…)` on the port and the adapters

**Files:**
- Modify: `src/usher/ports/source.py:536` and `:422-469` region of the Emby adapter
- Modify: `src/usher/adapters/emby/adapter.py:422` (`_walk`), `:471` (`list_items`),
  `:534` (`watch_state`), `:556` (`_watch_state`)
- Modify: `tests/fakes/source_adapter.py:330` (`watch_state`), `:333` (`_walk_states`)
- Test: `tests/unit/test_adapters_emby_adapter.py`

- [ ] **Step 1: Write the failing adapter test**

Append to `tests/unit/test_adapters_emby_adapter.py`, in its `--- the walk ---`
section. `_on(handler)` (line 92) is the file's helper for an adapter over a
hand-written transport, `_authenticated(request)` (line 105) is the
authentication leg every such handler shares, and the `try/finally … aclose()`
is the shape every case in the file uses:

```python
async def test_a_watch_state_walk_resumes_from_the_start_index_it_is_given() -> None:
    """The whole of #41's resume: the walk asks Emby for the page it stopped
    at rather than for page one. The walk's own order is
    `SortBy=DateCreated,SortName` ascending and `DateCreated` is immutable,
    so the prefix already walked does not reorder between attempts.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        requested.append(request.url.params["StartIndex"])
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 51_000})

    adapter = _on(handler)
    try:
        states = [one async for one in adapter.watch_state(start_index=50_000)]
    finally:
        await adapter.aclose()

    assert states == []
    assert requested == ["50000"], "the walk asked for page one instead of resuming"


async def test_an_item_walk_always_starts_at_the_beginning() -> None:
    """`list_items` shares `_walk` and must not inherit the watch lane's
    resume point: the item lanes have a working cursor and restart from it.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        requested.append(request.url.params["StartIndex"])
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    adapter = _on(handler)
    try:
        assert [one async for one in adapter.list_items()] == []
    finally:
        await adapter.aclose()
    assert requested == ["0"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py -k resumes_from_the_start_index -v`
Expected: FAIL — `TypeError: watch_state() got an unexpected keyword argument
'start_index'`.

- [ ] **Step 3: Widen the port**

In `src/usher/ports/source.py`, change the `watch_state` signature and extend its
docstring:

```python
    @abstractmethod
    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        """Watch state from the source, optionally since a cursor.

        Same `since`-inclusivity, no-ordering, possible-duplicates,
        must-stream, and must-raise-never-truncate contract as
        `list_items`.

        `start_index` resumes a walk that was interrupted: it is the number
        of records to skip, and an implementation that pages an upstream
        passes it straight through as that page offset. **It is only
        meaningful under a stable order, which this port does not promise
        and an adapter may** -- the Emby adapter walks
        `SortBy=DateCreated,SortName` ascending over an immutable creation
        date, so its walked prefix does not reorder between attempts. An
        adapter with no stable order must ignore it rather than skip
        arbitrary records, and say so.

        Emits a state for every item the walk covers, including states that
        are entirely zero. Filtering those out looks like an obvious saving
        and is a correctness bug: un-marking something played *is* an
        all-zero state, so an implementation that skipped them could never
        propagate a reset -- the delta walk would find the changed item and
        then discard exactly the record describing the change.

        May report `play_count`/`last_played_at` as `None` -- "this read
        cannot say" -- and must, rather than reporting a zero, whenever the
        listing it walks does not carry them. See `SourceWatchState`.
        """
```

- [ ] **Step 4: Thread it through the Emby adapter**

In `src/usher/adapters/emby/adapter.py`, change `_walk`'s signature and its
`start` initialiser:

```python
    async def _walk(
        self, *, since_param: str, since: AwareDatetime | None, start_index: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        user_id = await self._session.user_id()
        # The resume point (#41, ADR-0042). `list_items` never passes one --
        # the item lanes restart from their cursor; the watch lane's first
        # walk is the whole library and has to survive a transient failure.
        start = start_index
```

Then `watch_state` / `_watch_state`:

```python
    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
```

(keep the existing docstring body, and add to it:)

```
        `start_index` resumes an interrupted walk at that page offset. Sound
        here because this walk is ordered by `DateCreated`, which no edit
        moves, so the prefix already walked does not reorder underneath a
        resumed attempt; a *deletion* shifts it by one and costs the shifted
        item this run, which the merge's idempotent upsert picks up on the
        next one.
```

```python
        return self._watch_state(since, start_index)

    async def _watch_state(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        user_id = await self._session.user_id()
        async for payload in self._walk(
            since_param=USER_DATA_SINCE_PARAM, since=since, start_index=start_index
        ):
```

`list_items` and `_list_items` are unchanged — they call `_walk` without
`start_index` and take its `0` default, which is the decoupling the second test
pins.

- [ ] **Step 5: Widen the fake adapter**

In `tests/fakes/source_adapter.py`:

```python
    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        return self._walk_states(since, start_index)

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        await self._ready()
        yielded = 0
        # `start_index` skips records the way a paged upstream does, over this
        # fake's own insertion order -- which is stable, so a resumed walk
        # here means what it means against Emby's `DateCreated` ordering.
        for external_id in list(self._items)[start_index:]:
```

The rest of the loop body is unchanged.

- [ ] **Step 6: Update `_LossySourceAdapter`'s override**

Its signature has to match or mypy fails. In
`tests/unit/test_services_watch_sync.py`:

```python
    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        async for state in super()._walk_states(since, start_index):
            yield dataclasses.replace(state, play_count=None, last_played_at=None)
```

- [ ] **Step 7: Run the adapter tests and the type checker**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_contract.py -v && uv run mypy src tests`
Expected: PASS, and mypy clean. If another `SourceAdapter` implementation exists
that mypy now flags, widen its `watch_state` the same way.

- [ ] **Step 8: Commit**

```bash
git add src/usher/ports/source.py src/usher/adapters/emby/adapter.py \
        tests/fakes/source_adapter.py tests/unit/test_adapters_emby.py \
        tests/unit/test_services_watch_sync.py
git commit -m "feat(source): watch_state resumes from a start_index (#41)"
```

---

### Task 4: `WatchStateSyncService` reclaims and resumes

The core of the fix. Everything before this was scaffolding.

**Files:**
- Modify: `src/usher/services/watch_sync.py:115-128` (`_Progress`), `:169-222` (`sync`),
  `:324-360` (`_walk`), `:427-450` (`_flush`)
- Test: `tests/unit/test_services_watch_sync.py`

- [ ] **Step 1: Write the failing service tests**

Append to `tests/unit/test_services_watch_sync.py`:

```python
async def test_a_failed_walk_is_resumed_from_the_position_it_committed(
    fixture_batched: _Fixture,
) -> None:
    """**Issue #41.** A crashed walk left no completed run, so the next one
    had no cursor and re-walked the whole library -- for ~5,688 pages, which
    is where the next transient failure came from. It resumes instead.

    Batched at 2 deliberately: at the default 1,000 a six-item walk that
    fails part-way has committed *nothing*, so the position it resumes from
    would be 0 and the case would pass against a service that never
    checkpoints at all.
    """
    for index in range(6):
        await fixture_batched.given_matched(f"movie-{index}")
    fixture_batched.adapter.fail_after(3)

    first = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )
    assert first.status is SyncRunStatus.FAILED
    assert first.position == 2, (
        "the premise: one batch of two committed before the third yield raised"
    )

    fixture_batched.adapter.clear_failure()
    second = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert second.id == first.id, "the run row is reclaimed, not duplicated"
    assert second.status is SyncRunStatus.COMPLETED
    assert second.started_at == first.started_at, (
        "the reclaimed row keeps its original start instant, so the next delta's "
        "`since` covers everything saved since the logical walk began"
    )
    assert fixture_batched.adapter.resumed_from == [0, 2], (
        "the second attempt asked page one again instead of resuming"
    )


async def test_a_walk_whose_newest_run_completed_starts_fresh(fixture: _Fixture) -> None:
    """The other half: a completed walk is followed by a delta from its
    `started_at`, at position zero, not by a resume."""
    await fixture.given_matched("movie-0")
    done = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert done.status is SyncRunStatus.COMPLETED

    again = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert again.id != done.id, "a completed run is not reclaimed"
    assert again.position == 0
    assert again.cursor_at == done.started_at
    assert fixture.adapter.resumed_from == [0, 0]


async def test_the_position_advances_per_committed_batch(fixture_batched: _Fixture) -> None:
    """`position` is committed progress, never the batch in flight: a crash
    re-walks exactly the uncommitted batch, which the merge's idempotent
    upsert makes free."""
    for index in range(5):
        await fixture_batched.given_matched(f"movie-{index}")

    run = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.position == 5
    assert fixture_batched.positions == [2, 4, 5], (
        "position must be saved with every batch, including the trailing partial one"
    )


async def test_a_failed_walk_keeps_the_position_it_reached(
    fixture_batched: _Fixture,
) -> None:
    """`_Progress`' reason, extended to the resume point: a failure handler
    holding the pre-walk run would write `position = 0` over a checkpoint
    that recorded two, and the next attempt would restart from the top --
    which is the #41 loop with extra steps.
    """
    for index in range(6):
        await fixture_batched.given_matched(f"movie-{index}")
    fixture_batched.adapter.fail_after(3)

    run = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert run.status is SyncRunStatus.FAILED
    assert run.position == 2
    stored = await fixture_batched.runs.get(run.id)
    assert stored is not None and stored.position == 2, (
        "the durable checkpoint regressed, so the next attempt restarts from the top"
    )
```

These need three affordances. Add to `_Fixture.__init__` a batched variant and a
position recorder — put this fixture beside the existing `fixture` one:

```python
@pytest.fixture
def fixture_batched() -> _Fixture:
    """Batch size 2, so a five-state walk commits three times and the
    trailing partial batch is one of them."""
    return _Fixture(batch_size=2)
```

and in `_Fixture.__init__`, after `self.commits = 0`:

```python
        self.positions: list[int] = []
```

and record them by wrapping the fake repository's `save` in `_Fixture.__init__`,
after `self.runs = FakeSyncRunRepository()`:

```python
        saved = self.runs.save

        async def _record(run: SyncRun) -> None:
            if run.status is SyncRunStatus.RUNNING:
                # The per-batch checkpoints only. `sync`'s own closing save
                # carries the terminal status, and counting it would repeat
                # the last position and hide a missing trailing flush.
                self.positions.append(run.position)
            await saved(run)

        self.runs.save = _record  # type: ignore[method-assign]
```

Add `SyncRun` to the existing `usher.domain.sync` import in that file.

`FakeSourceAdapter` needs to record the resume points and to have its failure
switch turned back off. In `tests/fakes/source_adapter.py`, add to `__init__`:

```python
        self.resumed_from: list[int] = []
```

record it at the top of `_walk_states`, after `await self._ready()`:

```python
        self.resumed_from.append(start_index)
```

`clear_failure()` (line 157) already exists and is what the resume case uses to
turn the failure switch back off — `fail_after(count: int)` does not take `None`,
and widening it would be a second way to say the same thing.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_services_watch_sync.py -k "resumed_from_the_position or starts_fresh or advances_per_committed or keeps_the_position" -v`
Expected: FAIL, all four. `position` exists as a field (Task 1) and nothing
writes it, so the two failure cases fail on `assert 0 == 2`, the per-batch case
on `assert [0, 0, 0] == [2, 4, 5]`, and the resume case on
`assert second.id == first.id` — a second `SyncRun` is minted every time.

- [ ] **Step 3: Carry `position` through the walk**

In `src/usher/services/watch_sync.py`, `_walk` takes and threads the resume point:

```python
    async def _walk(
        self,
        progress: _Progress,
        source_id: uuid.UUID,
        adapter: SourceAdapter,
        cursor: AwareDatetime | None,
        user_id: uuid.UUID,
        start_index: int,
    ) -> None:
```

(keep the existing docstring, and add to it:)

```
        `start_index` is where a resumed attempt picks up -- the position the
        last attempt *committed*, so the batch that was in flight when it
        died is re-walked. That is free: every write here is an idempotent
        upsert and this lane retracts nothing.
```

```python
        batch: list[SourceWatchState] = []
        seen = start_index
        async for state in adapter.watch_state(since=cursor, start_index=start_index):
            batch.append(state)
            seen += 1
            if len(batch) >= self._batch_size:
                progress.run = await self._flush(progress.run, source_id, batch, user_id, seen)
                batch = []
        if batch:
            # The trailing partial batch. A walk's count is almost never a
            # multiple of the batch size, so omitting this drops the last
            # page of nearly every run -- here, a household's most recent
            # resume positions.
            progress.run = await self._flush(progress.run, source_id, batch, user_id, seen)
```

and `_flush` records it:

```python
    async def _flush(
        self,
        run: SyncRun,
        source_id: uuid.UUID,
        batch: Sequence[SourceWatchState],
        user_id: uuid.UUID,
        position: int,
    ) -> SyncRun:
        outcome = await self.apply_states(
            source_id, batch, user_id=user_id, observed_at=run.started_at
        )
        run = run.evolve(
            items_seen=run.items_seen + len(batch),
            # `len(outcome.merged)`, never `outcome.rows_written`: this
            # column has always meant "states this walk had somewhere to
            # put", and a merge refused by "latest `updated_at` wins" is
            # still one of those.
            items_matched=run.items_matched + len(outcome.merged),
            items_unmatched=run.items_unmatched + outcome.unmatched,
            # Committed progress, saved with the batch it describes: a crash
            # re-walks the batch in flight and nothing before it.
            position=position,
        )
        await self._runs.save(run)
        # One commit per batch, exactly like `ReconcileService`: a crash
        # costs the batch in flight, never the walk.
        await self._commit()
        return run
```

- [ ] **Step 4: Reclaim the run in `sync`**

Replace the opening of `sync`'s span body (the `cursor`/`run`/`add` lines,
`watch_sync.py:185-192`) with:

```python
            # **The newest incomplete run is resumed in place** (#41,
            # ADR-0042): its id, its `cursor_at` and -- load-bearing -- its
            # `started_at`, so that when the walk finally completes,
            # `latest_completed_cursor` reads an instant covering everything
            # saved since the logical walk *began*. A fresh `started_at` per
            # attempt would skip whatever changed between the first attempt
            # and the last.
            resuming = await self._runs.latest_incomplete_run(
                source.id, SyncRunKind.WATCH_STATE
            )
            if resuming is None:
                cursor = await self._runs.latest_completed_cursor(
                    source.id, SyncRunKind.WATCH_STATE
                )
                run = SyncRun(
                    source_id=source.id, kind=SyncRunKind.WATCH_STATE, cursor_at=cursor
                )
                # Committed `RUNNING` before the walk: an operator watching a
                # long sync needs a row to watch, and a killed process must
                # leave a trace rather than nothing.
                await self._runs.add(run)
            else:
                cursor = resuming.cursor_at
                run = resuming.evolve(status=SyncRunStatus.RUNNING, error=None, finished_at=None)
                await self._runs.save(run)
            start_index = run.position
            span.set_attribute("usher.resumed_from", start_index)
            await self._commit()
            progress = _Progress(run)
            try:
                await self._walk(progress, source.id, adapter, cursor, user_id, start_index)
```

Extend `sync`'s docstring, replacing the "Always incremental" paragraph:

```
        Always incremental from the last *completed* watch-state run, and
        **resumed in place from the last incomplete one**. This lane owns its
        own cursor: it walks a different method under a different upstream
        filter (`MinDateLastSavedForUser`, measured as genuinely different
        from the item lane's `MinDateLastSaved` -- 29,005 against 28,934
        items over the same 30-day window), so a cursor borrowed from a
        `FULL` or `DELTA` run would skip whatever changed in between. Unlike
        the item lanes there is no "full" variant to protect, because nothing
        here retracts.

        **The first walk on a fresh deployment is the whole library and that
        is why it resumes** (#41). With no completed run there is no cursor,
        so the walk is cursorless -- ~5,688 pages against the household this
        project measures -- and before ADR-0042 a single transient failure
        anywhere in it recorded `FAILED`, which left no completed run, which
        left no cursor, which restarted the walk. It never once succeeded.
        A resumed attempt continues from the position the last one committed,
        so a failure costs a page.
```

Also update the `_Progress` docstring to name the new field it carries:

```python
class _Progress:
    """The run as the walk has most recently checkpointed it.

    Mutable on purpose, and for the reason `ReconcileService._Progress`
    states at length: `SyncRun` is frozen, `_flush` saves an evolved copy
    per batch, and a failure handler holding the pre-walk value regresses
    the durable checkpoint to zero on every failure. Since ADR-0042 that
    applies to `position` as well as to the counters, and there it is the
    difference between a resumed walk and #41's restart loop.
    """
```

- [ ] **Step 5: Run the service tests**

Run: `uv run pytest tests/unit/test_services_watch_sync.py -v`
Expected: PASS, all cases including the pre-existing ones.

- [ ] **Step 6: Run the whole unit suite and the gate**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports`
Expected: PASS, `lint-imports` 12 kept / 0 broken.

- [ ] **Step 7: Run the integration suite**

Run: `uv run pytest tests/integration -v`
Expected: PASS (needs Docker). `tests/integration/test_services_watch_sync.py` and
`tests/integration/test_sse_end_to_end.py` are the ones this change can reach.

- [ ] **Step 8: Commit**

```bash
git add src/usher/services/watch_sync.py tests/unit/test_services_watch_sync.py \
        tests/fakes/source_adapter.py
git commit -m "fix(watch): the watch lane resumes instead of restarting (#41)"
```

---

### Task 5: Plant the regression, to prove the tests have teeth

The repo's standing rule: a test that was never seen red proves nothing. This task
plants the pre-fix behaviour back and confirms exactly which cases fail.

**Files:** none committed — every edit is reverted.

- [ ] **Step 1: Back up the file**

```bash
cp src/usher/services/watch_sync.py /var/tmp/watch_sync.py.bak
md5sum src/usher/services/watch_sync.py
```

`/var/tmp`, not `/tmp` — `/tmp` is tmpfs on this host.

- [ ] **Step 2: Plant the restart loop**

In `src/usher/services/watch_sync.py`'s `sync`, replace the resume lookup with the
pre-fix spelling — i.e. change

```python
            resuming = await self._runs.latest_incomplete_run(
                source.id, SyncRunKind.WATCH_STATE
            )
```

to

```python
            resuming = None
```

- [ ] **Step 3: Run and record which cases die**

Run: `uv run pytest tests/unit/test_services_watch_sync.py -v`
Expected: FAIL — `test_a_failed_walk_is_resumed_from_the_position_it_committed`
fails on `assert second.id == first.id`, and nothing else in the file fails.
If any *other* case fails, that is collateral worth understanding before
continuing; if *no* case fails, the resume test has no teeth and must be fixed.

- [ ] **Step 4: Plant the `_Progress` regression**

Restore the file, then change `sync`'s failure handler from `progress.run` back to
the pre-walk binding:

```bash
cp /var/tmp/watch_sync.py.bak src/usher/services/watch_sync.py
```

then in the `except UsherPortError` arm change `run = progress.run.evolve(` to
`run = run.evolve(`.

- [ ] **Step 5: Run and record**

Run: `uv run pytest tests/unit/test_services_watch_sync.py -v`
Expected: FAIL — `test_a_failed_walk_keeps_the_position_it_reached` fails on
`assert 0 == 2`, alongside the pre-existing `items_seen` case that already covers
this arm.

- [ ] **Step 6: Restore and verify byte-for-byte**

```bash
cp /var/tmp/watch_sync.py.bak src/usher/services/watch_sync.py
md5sum src/usher/services/watch_sync.py   # must match Step 1
git status --porcelain                     # must be empty
uv run pytest tests/unit/test_services_watch_sync.py
```

Never `git checkout` to undo a plant — it discards uncommitted work, not just the
plant.

- [ ] **Step 7: Record the verdicts in the ADR**

Replace the ADR's `## Evidence` section
(`docs/prd/decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md`)
with what was actually observed, in this shape, filling in the counts from the runs
above:

```markdown
## Evidence

Every case was seen red before the implementation existed, and the two plants
below were run against the finished code and restored `md5sum`-verified.

| plant | verdict | cases failed |
|---|---|---|
| `latest_incomplete_run` never consulted (the pre-fix restart loop) | KILLED | 1 — `test_a_failed_walk_is_resumed_from_the_position_it_committed` |
| the failure handler evolves the pre-walk run rather than `progress.run` | KILLED | 2 — the `position` case and the pre-existing `items_seen` one |

The `since`-cursor alternative is infeasible rather than declined, and the
design spec records why against exact `ports/source.py` and
`adapters/emby/adapter.py` line references: the yielded record carries no
`DateLastSavedForUser`, the walk is sorted by `DateCreated,SortName`, and the
field is mutable.
```

- [ ] **Step 8: Commit**

```bash
git add docs/prd/decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md
git commit -m "docs(adr): ADR-0042 records the plant verdicts (#41)"
```

---

### Task 6: Correct the docs this change invalidates

The PRD-current rule: a change that alters behaviour and leaves the PRD stale is
incomplete. Two `src/` docstrings now make claims that are false.

**Files:**
- Modify: `src/usher/services/reconcile.py:28-34`
- Modify: `src/usher/domain/sync.py:1-14` (module docstring)
- Modify: `docs/prd/03-sources-and-sync.md`
- Modify: `docs/prd/decisions/0042-...md` (status line)

- [ ] **Step 1: Correct `reconcile.py`'s "no mid-walk cursor" claim**

Its module docstring says there is *"no mid-walk cursor to resume from -- the port
offers `since` and nothing finer"*. That is now false of the watch lane. Replace
that sentence with:

```
Batches are committed as they go, with the run's counters. Unlike bootstrap
the *item* lanes have no mid-walk cursor to resume from, so a crashed full
walk is re-run from the start -- which is safe because every write is an
upsert and the sweep never ran. **The watch lane is the exception since
ADR-0042**: its first walk is the whole library with no cursor to bound it,
so it resumes from a `StartIndex` checkpoint on its own `sync_runs` row.
That difference is a fact about the two lanes' *cursors* rather than a
disagreement about design -- an item lane restarting costs a delta window,
and the watch lane restarting cost ~11 hours and never converged (#41).
```

- [ ] **Step 2: Correct `domain/sync.py`'s module docstring**

It opens *"One row per attempt, not one per source: unlike `ImportRun` (a
checkpoint updated in place) this is a history"*. Add the exception after that
first paragraph:

```
**The `WATCH_STATE` lane is the one exception and it is deliberate**
(ADR-0042): an incomplete watch run is reclaimed in place by the next
attempt -- same row, same `started_at`, `position` carried forward -- so
that lane's rows are a history of *logical walks* rather than of attempts.
The item lanes are unchanged, and every row still records which run last
finished cleanly, which is what ADR-0015's sweep guard rests on.
```

- [ ] **Step 3: Correct the `SyncRunRepository` port docstring**

`src/usher/ports/repository/sync.py:24-30` makes the same "one row per attempt,
contrast `ImportRunRepository`" claim. Append to it:

```
    `latest_incomplete_run` is the one affordance that reads against that
    grain, for the `WATCH_STATE` lane only -- see ADR-0042.
```

- [ ] **Step 4: Update PRD 03**

In `docs/prd/03-sources-and-sync.md`, find the watch-state sync section and add,
beside the existing statement of the lane's cursor:

```markdown
The watch lane's walk is **resumable**. Its cursor comes from the newest
completed run, so a deployment that has never completed one walks the whole
library — ~5,688 pages against the measured household. Before
[ADR-0042](decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md)
a single transient failure in that walk recorded `FAILED`, which left no
completed run, which left no cursor, which restarted the walk; it never once
completed (#41). A run now checkpoints the `StartIndex` it has committed on
`sync_runs.position` and the next attempt reclaims that row and resumes from
it, so a failure costs a page rather than the run. The item lanes
(`FULL`/`DELTA`) are unchanged: they have a working cursor and leave
`position` at 0.
```

- [ ] **Step 5: Flip the ADR's status**

In `docs/prd/decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md`,
change the status line from `**Status:** Proposed.` to
`**Status:** Accepted. Implemented 2026-08-21.` and update the
`docs/prd/decisions/README.md` row's status cell from `Proposed —` to
`Accepted —`.

- [ ] **Step 6: Run the PRD link check**

```bash
cd ~/code/usher-41 && python3 - <<'EOF'
import re, pathlib
roots = list(pathlib.Path("docs/prd").rglob("*.md"))
roots += [pathlib.Path("CLAUDE.md"), pathlib.Path("README.md")]
bad = []
for md in roots:
    for link in re.findall(r'\]\(([^)#][^)]*\.md)\)', md.read_text()):
        if not (md.parent / link).resolve().exists():
            bad.append(f"{md}: {link}")
print("\n".join(bad) if bad else "OK")
EOF
```

Expected: `OK`.

- [ ] **Step 7: Run the decision-register test**

Run: `uv run pytest tests/unit/test_decision_register.py -v`
Expected: PASS — every ADR file has a row and no two claim the same number.

- [ ] **Step 8: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && uv run pytest`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/usher/services/reconcile.py src/usher/domain/sync.py \
        src/usher/ports/repository/sync.py docs/prd/03-sources-and-sync.md \
        docs/prd/decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md \
        docs/prd/decisions/README.md
git commit -m "docs: the watch lane resumes, and three docstrings said otherwise (#41)"
```

---

## After the plan

**Re-enabling the worker is the operator's separate step, not part of this branch.**
Once this merges and deploys, `USHER_WORKER_ENABLED=false` in `~/code/usher/.env`
can be removed and the stack restarted; the queued `bootstrap|all`, `bootstrap|imdb`
and the `match` backlog drain then. Watch the first watch-lane run: it is still the
whole library, and what has changed is that it converges. `sync_runs.position`
climbing across attempts is the evidence it is working.

**Do not delete `.env`'s lane-split comment** — rewrite it to say the loop is fixed
and reference ADR-0042, so the next reader knows the setting was deliberate and why
it stopped being needed.
