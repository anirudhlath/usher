"""Checkpoint storage for the bulk importers."""

from datetime import UTC, datetime

from sqlalchemy import case, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories._errors import constraint_name, is_row_refusal, refusals_as_conflict
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import ImportRunRepository

#: The first key of every checkpoint's advisory lock, so it shares no lock with anything
#: else in the database; the second is `hashtext(dataset)`. Two datasets hashing alike
#: would serialise each other's imports, never let two processes into one.
_LOCK_NAMESPACE = 0x75736872
_TRY_LOCK = text("SELECT pg_try_advisory_lock(:namespace, hashtext(:dataset))")
_UNLOCK = text("SELECT pg_advisory_unlock(:namespace, hashtext(:dataset))")


def _to_domain(row: ImportRunRow) -> ImportRun:
    # Same shape as PostgresTitleRepository._to_domain, and safe for the same
    # reason: ImportRunRow's 11 columns are 1:1 by name with ImportRun's 11
    # fields, so `extra="forbid"` turns any future drift into a loud
    # ValidationError instead of a silently dropped column.
    return ImportRun.model_validate(
        {column.name: getattr(row, column.name) for column in ImportRunRow.__table__.columns}
    )


class PostgresImportRunRepository(ImportRunRepository):
    """Every read is `populate_existing`, because another process writes these rows.

    **The hold is a session-level advisory lock on a connection of its own**, checked out
    of the session's engine by `start()` and returned by `release()`. The session's own
    connection goes back to the pool at every commit, which would carry the lock off with
    it; a lock that dies with its connection is what lets a killed importer's `RUNNING`
    row be taken over.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._holds: dict[str, AsyncConnection] = {}

    async def start(self, dataset: str, revision: str) -> ImportRun:
        took = await self._hold(dataset)
        try:
            return await self._begin(dataset, revision)
        except BaseException:
            if took:
                await self.release(dataset)
            raise

    async def _hold(self, dataset: str) -> bool:
        """Take the advisory lock for `dataset`, or `RepositoryConflict`; whether it was new."""
        if dataset in self._holds:
            return False
        connection = await self._engine().connect()
        try:
            # Autocommit, so the connection holds the lock and never a transaction:
            # idle, it is killed by no `idle_in_transaction_session_timeout`.
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            granted = await connection.scalar(
                _TRY_LOCK, {"namespace": _LOCK_NAMESPACE, "dataset": dataset}
            )
        except BaseException:
            await connection.close()
            raise
        if not granted:
            await connection.close()
            raise RepositoryConflict(f"another process holds the import of {dataset}")
        self._holds[dataset] = connection
        return True

    def _engine(self) -> AsyncEngine:
        bind = self._session.bind
        if isinstance(bind, AsyncConnection):
            return bind.engine
        if isinstance(bind, AsyncEngine):
            return bind
        raise TypeError("PostgresImportRunRepository needs a session bound to one engine")

    async def _begin(self, dataset: str, revision: str) -> ImportRun:
        now = datetime.now(UTC)
        existing = await self.get(dataset)
        if existing is None:
            run = ImportRun(dataset=dataset, revision=revision, heartbeat_at=now)
        elif existing.revision == revision:
            # Same upstream snapshot: keep the cursor and continue.
            run = existing.evolve(
                status=ImportRunStatus.RUNNING, error=None, finished_at=None, heartbeat_at=now
            )
        else:
            # Upstream moved. Position 0 restarts the stream; the row's id and
            # started_at are kept so `bootstrap-status` still shows one row per
            # dataset rather than accumulating history this table is not for.
            run = existing.evolve(
                revision=revision,
                position=0,
                rows_seen=0,
                rows_written=0,
                status=ImportRunStatus.RUNNING,
                error=None,
                finished_at=None,
                heartbeat_at=now,
            )
        if existing is not None and existing.status is ImportRunStatus.COMPLETED:
            await self.save(existing.evolve(error=None, heartbeat_at=now))
        else:
            await self.save(run)
        return run

    async def release(self, dataset: str) -> None:
        connection = self._holds.pop(dataset, None)
        if connection is None:
            return
        try:
            await connection.execute(_UNLOCK, {"namespace": _LOCK_NAMESPACE, "dataset": dataset})
        except BaseException:
            # A connection that did not unlock may still hold the lock: closing its
            # backend is what releases it, rather than returning it to the pool.
            await connection.invalidate()
            raise
        finally:
            await connection.close()

    async def touch(self, dataset: str) -> None:
        async with refusals_as_conflict(self._session, f"the heartbeat of {dataset}"):
            await self._session.execute(
                update(ImportRunRow)
                .where(
                    ImportRunRow.dataset == dataset,
                    ImportRunRow.status == ImportRunStatus.RUNNING,
                )
                .values(heartbeat_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )

    async def note_failure(self, dataset: str, error: str) -> ImportRun:
        now = datetime.now(UTC)
        created = ImportRun(
            dataset=dataset,
            revision="unknown",
            status=ImportRunStatus.FAILED,
            error=error,
            heartbeat_at=now,
            finished_at=now,
        )
        statement = pg_insert(ImportRunRow).values(**created.model_dump())
        statement = statement.on_conflict_do_update(
            index_elements=[ImportRunRow.dataset],
            set_={
                "error": statement.excluded.error,
                "heartbeat_at": case(
                    (ImportRunRow.status == ImportRunStatus.RUNNING, ImportRunRow.heartbeat_at),
                    else_=statement.excluded.heartbeat_at,
                ),
            },
        )
        async with refusals_as_conflict(self._session, f"the failure of {dataset}"):
            result = await self._session.execute(
                statement.returning(ImportRunRow), execution_options={"populate_existing": True}
            )
        return _to_domain(result.scalar_one())

    async def save(self, run: ImportRun) -> None:
        data = run.model_dump()
        try:
            row = await self._session.get(ImportRunRow, run.id, populate_existing=True)
            if row is None:
                self._session.add(ImportRunRow(**data))
            else:
                for key, value in data.items():
                    if key != "id":
                        setattr(row, key, value)
            await self._session.flush()
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`.** `position`, `rows_seen`
            # and `rows_written` are `integer` and `ImportRun` bounds all three
            # `ge=0` with no ceiling, so a resumable importer's own cursor is a
            # validly constructed model this table cannot hold.
            if not is_row_refusal(exc):
                raise
            # `dataset` is unique, and the advisory lock is what keeps two processes
            # from racing to insert it; this is the backstop, as a port error.
            await self._session.rollback()
            constraint = constraint_name(exc)
            detail = (
                f"already exists under a different id ({constraint})"
                if constraint is not None
                else "violates import_runs' own bounds"
            )
            raise RepositoryConflict(
                f"an import run for {run.dataset} {detail}", constraint=constraint
            ) from exc

    async def get(self, dataset: str) -> ImportRun | None:
        with self._session.no_autoflush:
            result = await self._session.execute(
                select(ImportRunRow)
                .where(ImportRunRow.dataset == dataset)
                .execution_options(populate_existing=True)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def list_runs(self) -> list[ImportRun]:
        with self._session.no_autoflush:
            result = await self._session.execute(
                select(ImportRunRow)
                .order_by(ImportRunRow.heartbeat_at.desc())
                .execution_options(populate_existing=True)
            )
        return [_to_domain(row) for row in result.scalars()]
