"""The shared contract against real Postgres, plus what a dict cannot express.

A foreign key, a CHECK constraint, a collation, and a poisoned session.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.sync_run_repository_contract import (
    EARLIER,
    LATER,
    UNIT_KEYS_IN_BYTE_ORDER,
    SyncRunRepositoryContract,
    run,
    unit,
)
from tests.integration.conftest import Analyze
from usher.db.models.sync import SyncRunRow
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import _NEWEST, PostgresSyncRunRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import ABANDONED_ERROR, SyncRunKind, SyncRunStatus, SyncRunUnit
from usher.ports.errors import RepositoryConflict


@pytest.fixture
def repository(session: AsyncSession) -> PostgresSyncRunRepository:
    return PostgresSyncRunRepository(session)


async def _given_source(session: AsyncSession, name: str) -> uuid.UUID:
    source = Source(
        kind=SourceKind.EMBY,
        name=name,
        base_url="https://emby.example",
        credentials_ref=f"cred-{name}",
        device_id=f"device-{name}",
    )
    await PostgresSourceRepository(session).add(source)
    return source.id


@pytest_asyncio.fixture
async def source_id(session: AsyncSession) -> uuid.UUID:
    return await _given_source(session, "Contract Source")


@pytest_asyncio.fixture
async def other_source_id(session: AsyncSession) -> uuid.UUID:
    return await _given_source(session, "Other Source")


class TestPostgresSyncRunRepository(SyncRunRepositoryContract):
    """Every case in `SyncRunRepositoryContract`, against real Postgres."""


async def test_a_source_id_no_source_carries_is_a_port_error(
    repository: PostgresSyncRunRepository,
) -> None:
    """A dict has no foreign keys, so the fake stores a run attributed to nothing.

    Postgres raises, and the refusal has to reach the caller as a port error rather than
    as `sqlalchemy.exc`.
    """
    with pytest.raises(RepositoryConflict) as caught:
        await repository.add(run(new_id()))
    assert caught.value.constraint == "fk_sync_runs_source_id_sources"


async def test_a_caught_conflict_leaves_the_session_usable(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """Postgres aborts the whole transaction on a statement error until a ROLLBACK.

    Without a SAVEPOINT a caught conflict poisons the session for the caller's next,
    unrelated call — and this caller commits a run's checkpoint together with the batch
    it describes.
    """
    with pytest.raises(RepositoryConflict):
        await repository.add(run(new_id()))
    one = run(source_id)
    await repository.add(one)
    assert await repository.get(one.id) is not None


async def test_the_cursor_query_uses_the_source_kind_index(
    session: AsyncSession,
    repository: PostgresSyncRunRepository,
    source_id: uuid.UUID,
    analyze: Analyze,
) -> None:
    """The index is `(source_id, kind, started_at DESC)`, so the newest run is its first entry.

    "The newest completed run of this kind" must come off the index rather than out of a
    sort over a source's whole history. `status` is deliberately not a key: a scan back
    through consecutive failures is bounded by how many times in a row it failed.
    """
    for index in range(500):
        one = run(source_id, started_at=EARLIER.replace(year=2020) + (LATER - EARLIER) * index)
        await repository.add(one)
        await repository.save(one.evolve(status=SyncRunStatus.COMPLETED, finished_at=LATER))
    await analyze("sync_runs")
    plan = "\n".join(
        (
            await session.execute(
                text(
                    "EXPLAIN SELECT started_at FROM sync_runs "
                    "WHERE source_id = :source_id AND kind = :kind AND status = 'completed' "
                    "ORDER BY started_at DESC LIMIT 1"
                ),
                {"source_id": source_id, "kind": SyncRunKind.FULL.value},
            )
        )
        .scalars()
        .all()
    )
    assert "ix_sync_runs_source_kind_started" in plan, plan
    assert "Sort" not in plan, "the index is supposed to be the ordering, not a sort over it"


async def test_the_resume_query_uses_the_source_kind_index(
    session: AsyncSession,
    repository: PostgresSyncRunRepository,
    source_id: uuid.UUID,
    analyze: Analyze,
) -> None:
    """The index supplies the ordering, and the `id` tiebreak costs one Incremental Sort.

    Not `"Sort" not in plan`, which `Incremental Sort` contains: the two plans are a full
    sort over a source's whole history versus a sort of one tied group at the head of an
    index scan, so this names the node it wants plus the `Presorted Key` that says the
    index really did supply the leading key. `_NEWEST` is imported rather than
    transcribed so the assertion cannot drift off the statement it describes.
    """
    for index in range(500):
        await repository.add(
            run(
                source_id,
                kind=SyncRunKind.WATCH_STATE,
                started_at=EARLIER.replace(year=2020) + (LATER - EARLIER) * index,
            )
        )
    await analyze("sync_runs")
    plan = "\n".join(
        (
            await session.execute(
                text("EXPLAIN " + _NEWEST),
                {"source_id": source_id, "kind": SyncRunKind.WATCH_STATE.value},
            )
        )
        .scalars()
        .all()
    )
    assert "ix_sync_runs_source_kind_started" in plan, plan
    assert "Incremental Sort" in plan, plan
    assert "Presorted Key: started_at" in plan, plan


async def test_the_latest_planned_run_passes_over_a_newer_run_without_a_heartbeat(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """The newest run of a kind with a heartbeat, whatever its status, newest by `(started_at, id)`.

    The gap-closer's single walk is the newest delta and carries none. Added newer planned
    run first, so neither insertion order nor id order picks the answer. The planned read
    then tests the status of the run it found, never "the newest that is not completed".
    """
    newer = run(
        source_id,
        kind=SyncRunKind.DELTA,
        status=SyncRunStatus.COMPLETED,
        started_at=EARLIER + timedelta(hours=1),
        heartbeat_at=LATER,
    )
    older = run(
        source_id,
        kind=SyncRunKind.DELTA,
        status=SyncRunStatus.FAILED,
        started_at=EARLIER,
        heartbeat_at=EARLIER,
    )
    single = run(source_id, kind=SyncRunKind.DELTA, status=SyncRunStatus.FAILED, started_at=LATER)
    for one in (newer, older, single):
        await repository.add(one)
    assert older.started_at < newer.started_at < single.started_at, "the premise: the order"
    assert newer.id < older.id, "the premise: the newer planned run holds the smaller id"
    newest = await repository.latest_run(source_id, SyncRunKind.DELTA)
    assert newest is not None and newest.id == single.id, "the premise: the single walk is newest"

    found = await repository.latest_planned_run(source_id, SyncRunKind.DELTA)

    assert found is not None
    assert (found.id, found.status) == (newer.id, SyncRunStatus.COMPLETED)
    planned = await repository.latest_incomplete_run(source_id, SyncRunKind.DELTA, planned=True)
    assert planned is None, "the planned read handed back an older failure"
    unplanned = await repository.latest_incomplete_run(source_id, SyncRunKind.DELTA)
    assert unplanned is not None and unplanned.id == single.id


async def test_a_negative_counter_is_a_port_error(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """The CHECK mirrors `SyncRun`'s `Field(ge=0)`, and fires on the way to disk.

    Only the CHECK is still there for a caller that writes the row without going through
    the model, so it has to reach that caller as a port error.
    """
    one = run(source_id)
    await repository.add(one)
    broken = one.model_construct(**{**one.model_dump(), "items_seen": -1})
    with pytest.raises(RepositoryConflict) as caught:
        await repository.save(broken)
    assert caught.value.constraint == "ck_sync_runs_items_seen_non_negative"


async def test_started_at_survives_a_save(
    session: AsyncSession, repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """`started_at` is the availability sweep's `seen_since`, so a save must not refresh it.

    A refreshed value would let a run retract items it had already seen. There is no
    `server_default`-on-update and no trigger, and this is what says so.
    """
    one = run(source_id, started_at=EARLIER)
    await repository.add(one)
    await repository.save(one.evolve(status=SyncRunStatus.COMPLETED, finished_at=LATER))
    stored = (
        await session.execute(
            text("SELECT started_at FROM sync_runs WHERE id = :id"), {"id": one.id}
        )
    ).scalar_one()
    assert stored == EARLIER
    assert datetime.now(UTC) > EARLIER, "the fixture instant is genuinely in the past"


async def test_a_run_this_session_holds_reads_back_closed(
    session: AsyncSession, repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """`close_abandoned` is an `UPDATE`, which a row in the identity map does not see alone.

    `get` serves a row this session still holds with no SQL at all, so an unsynchronised
    close would read back `running`. The contract cannot hold a row: its `add` keeps no
    reference, so every `get` there is a fresh `SELECT`.
    """
    dead = run(source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None)
    await repository.add(dead)
    held = await session.get(SyncRunRow, dead.id)
    assert held is not None and held.status is SyncRunStatus.RUNNING, "the premise: held"

    closed = await repository.close_abandoned(
        source_id, (SyncRunKind.WATCH_STATE,), now=LATER, error=ABANDONED_ERROR
    )

    stored = await repository.get(dead.id)
    assert closed == 1 and stored is not None
    assert (stored.status, stored.error, held.status) == (
        SyncRunStatus.FAILED,
        ABANDONED_ERROR,
        SyncRunStatus.FAILED,
    )


async def test_a_refused_close_is_a_port_error_and_leaves_the_session_usable(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """`error` is the caller's text, and Postgres refuses a NUL in it as a refused row.

    The refusal reaches the caller as `RepositoryConflict`, and the SAVEPOINT leaves the
    session able to read the run back, still unclosed.
    """
    dead = run(source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None)
    await repository.add(dead)

    with pytest.raises(RepositoryConflict):
        await repository.close_abandoned(
            source_id, (SyncRunKind.WATCH_STATE,), now=LATER, error="a NUL \x00 here"
        )

    assert await repository.get(dead.id) == dead


async def test_a_negative_unit_position_is_a_port_error(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """The CHECK mirrors `SyncRunUnit.position`'s `ge=0` for a writer that skips the model."""
    one = run(source_id)
    await repository.add(one)
    broken = SyncRunUnit.model_construct(**{**unit(one.id, "alpha").model_dump(), "position": -1})
    with pytest.raises(RepositoryConflict) as caught:
        await repository.add_units([broken])
    assert caught.value.constraint == "ck_sync_run_units_position_non_negative"


async def test_a_runs_units_go_with_it(
    session: AsyncSession, repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """`ON DELETE CASCADE`: a plan means nothing without its run."""
    one = run(source_id)
    await repository.add(one)
    await repository.add_units([unit(one.id, "alpha"), unit(one.id, "bravo")])
    count = text("SELECT count(*) FROM sync_run_units WHERE run_id = :id")
    assert (await session.execute(count, {"id": one.id})).scalar_one() == 2, "the premise"

    await session.execute(text("DELETE FROM sync_runs WHERE id = :id"), {"id": one.id})

    assert (await session.execute(count, {"id": one.id})).scalar_one() == 0


async def test_a_caught_unit_conflict_leaves_the_session_usable(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """The writer commits a unit with the batch it describes, as it does a run."""
    with pytest.raises(RepositoryConflict):
        await repository.add_units([unit(new_id(), "alpha")])
    one = run(source_id)
    await repository.add(one)
    await repository.add_units([unit(one.id, "alpha")])
    assert [each.unit_key for each in await repository.units_for(one.id)] == ["alpha"]


async def test_the_unit_key_column_alone_orders_its_keys_unlike_bytes(
    session: AsyncSession, repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """The premise of the contract's key-order case on this arm, asked of the database.

    That case sees a dropped `COLLATE "C"` only while the column's own collation orders
    its keys unlike bytes; a database created under the C locale would hide the plant.
    """
    one = run(source_id)
    await repository.add(one)
    await repository.add_units([unit(one.id, key) for key in UNIT_KEYS_IN_BYTE_ORDER])
    in_column_order = (
        await session.execute(
            text("SELECT unit_key FROM sync_run_units WHERE run_id = :id ORDER BY unit_key"),
            {"id": one.id},
        )
    ).scalars()
    assert list(in_column_order) != list(UNIT_KEYS_IN_BYTE_ORDER), (
        "the premise: the column's own collation orders these keys as bytes do"
    )
