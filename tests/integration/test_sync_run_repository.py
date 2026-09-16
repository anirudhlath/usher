"""The shared contract against real Postgres, plus what a dict cannot express.

A foreign key, a CHECK constraint, and a poisoned session.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.sync_run_repository_contract import (
    EARLIER,
    LATER,
    SyncRunRepositoryContract,
    run,
)
from tests.integration.conftest import Analyze
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import _INCOMPLETE, PostgresSyncRunRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind, SyncRunStatus
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
    index really did supply the leading key. `_INCOMPLETE` is imported rather than
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
                text("EXPLAIN " + _INCOMPLETE),
                {"source_id": source_id, "kind": SyncRunKind.WATCH_STATE.value},
            )
        )
        .scalars()
        .all()
    )
    assert "ix_sync_runs_source_kind_started" in plan, plan
    assert "Incremental Sort" in plan, plan
    assert "Presorted Key: started_at" in plan, plan


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
