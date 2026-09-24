"""Checkpoint storage for the bulk importers."""

import contextlib
from datetime import UTC, datetime

from sqlalchemy import TextClause, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories._errors import constraint_name, is_row_refusal, refusals_as_conflict
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import ImportRunRepository

#: The first key of every checkpoint's advisory lock, so it shares no lock with anything
#: else in the database; the second is `hashtext(dataset)`. Two datasets hashing alike
#: would serialise each other's imports, never let two processes into one. A hold takes
#: the key exclusive and a read takes the same key shared, which is what excludes them.
_LOCK_NAMESPACE = 0x75736872
_TRY_LOCK = text("SELECT pg_try_advisory_lock(:namespace, hashtext(:dataset))")
_UNLOCK = text("SELECT pg_advisory_unlock(:namespace, hashtext(:dataset))")
_TRY_READ = text("SELECT pg_try_advisory_lock_shared(:namespace, hashtext(:dataset))")
_UNREAD = text("SELECT pg_advisory_unlock_shared(:namespace, hashtext(:dataset))")
# The lock's row in `pg_locks`, which shows each key as an unsigned `oid`. `hashtext` is a
# signed `int4`, and the cast keeps its bits, so a name hashing negative matches too.
_STILL_LOCKED = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
    " AND classid = CAST(:namespace AS oid) AND objid = CAST(hashtext(:dataset) AS oid)"
    " AND objsubid = 2 AND pid = pg_backend_pid() AND mode = '{mode}')"
)
_STILL_HELD = text(_STILL_LOCKED.format(mode="ExclusiveLock"))
_STILL_READ = text(_STILL_LOCKED.format(mode="ShareLock"))


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
    of the session's engine by `hold()` and returned by `release()`. The session's own
    connection goes back to the pool at every commit, which would carry the lock off with
    it; a lock that dies with its connection is what lets a killed importer's `RUNNING`
    row be taken over. So an importer holds two connections, and `Settings` counts both.

    **Reads live on one more connection**, all of a repository's reads together, checked
    out by the first `hold_for_reading()` and returned by `release_reads()`. Being
    another backend is what makes a repository's read refuse its own hold. A phase that
    reads holds three connections at once: its session, its reads, its own hold.

    **The same property ends a live hold silently**: `idle_session_timeout`, a proxy's
    idle cut or a server restart closes the connection and frees the lock with nothing
    said. `touch` confirms the hold and the reads on theirs -- which also keeps them from
    idling -- and one found gone is dropped with a `RepositoryConflict`. Giving back a
    lock whose connection has ended is no error: the lock went with it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._holds: dict[str, AsyncConnection] = {}
        self._reader: AsyncConnection | None = None
        self._reads: set[str] = set()

    async def start(self, dataset: str, revision: str) -> ImportRun:
        took = dataset not in self._holds
        await self.hold(dataset)
        try:
            return await self._begin(dataset, revision)
        except BaseException:
            if took:
                await self.release(dataset)
            raise

    async def hold(self, dataset: str) -> None:
        if dataset in self._holds:
            try:
                await self._confirm(dataset)
            except RepositoryConflict:
                pass  # Dropped as lost: taken again below, or refused.
            else:
                return
        connection = await self._lock_connection()
        try:
            granted = await connection.scalar(
                _TRY_LOCK, {"namespace": _LOCK_NAMESPACE, "dataset": dataset}
            )
        except BaseException:
            # The lock may have been granted before whatever ended this: closed, the
            # connection would go back to the pool holding it. Ending its backend frees it.
            await _discard(connection)
            raise
        if not granted:
            await connection.close()
            raise RepositoryConflict(
                f"another process is importing {dataset} or running a phase that reads it"
            )
        self._holds[dataset] = connection

    async def hold_for_reading(self, dataset: str) -> bool:
        if dataset in self._reads:
            return True
        if self._reader is None:
            self._reader = await self._lock_connection()
        try:
            granted = await self._reader.scalar(
                _TRY_READ, {"namespace": _LOCK_NAMESPACE, "dataset": dataset}
            )
        except BaseException:
            # As in `hold`: granted or not, only ending the backend is sure to free it,
            # and every read on it goes too.
            await self._drop_reads()
            raise
        if granted:
            self._reads.add(dataset)
        elif not self._reads:
            await self.release_reads()  # Nothing on it: back to the pool.
        return bool(granted)

    async def release_reads(self) -> None:
        connection, reads = self._reader, sorted(self._reads)
        self._reader, self._reads = None, set()
        if connection is not None:
            await _give_back(connection, _UNREAD, reads)

    async def _drop_reads(self) -> None:
        """Forget every read, ending the backend they were on so none outlives it."""
        connection, self._reader, self._reads = self._reader, None, set()
        if connection is not None:
            await _discard(connection)

    async def _lock_connection(self) -> AsyncConnection:
        """A connection of the session's engine to hold locks on, and nothing else."""
        connection = await self._engine().connect()
        try:
            # Autocommit, so the connection holds the lock and never a transaction:
            # idle, it is killed by no `idle_in_transaction_session_timeout`.
            await connection.execution_options(isolation_level="AUTOCOMMIT")
        except BaseException:
            await _discard(connection)
            raise
        return connection

    async def _confirm(self, dataset: str) -> None:
        """`RepositoryConflict` unless this repository's hold on `dataset` is alive."""
        connection = self._holds.get(dataset)
        if connection is None:
            raise RepositoryConflict(f"this process does not hold the import of {dataset}")
        why = await _why_lost(connection, _STILL_HELD, dataset)
        if why is not None:
            del self._holds[dataset]
            await _discard(connection)
            raise RepositoryConflict(f"lost the hold on the import of {dataset}: {why}")

    async def _confirm_reads(self) -> None:
        """`RepositoryConflict`, and every read dropped, unless each read is alive."""
        if self._reader is None:
            return
        for dataset in sorted(self._reads):
            why = await _why_lost(self._reader, _STILL_READ, dataset)
            if why is not None:
                await self._drop_reads()
                raise RepositoryConflict(
                    f"lost the shared hold on {dataset}, which this reads: {why}"
                )

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
        if connection is not None:
            await _give_back(connection, _UNLOCK, [dataset])

    async def touch(self, dataset: str) -> None:
        try:
            await self._confirm(dataset)
        except RepositoryConflict:
            # A server restart ends the reads' connection with the hold's: read too, so
            # one found gone is dropped now. The conflict raised is the hold's.
            with contextlib.suppress(RepositoryConflict):
                await self._confirm_reads()
            raise
        await self._confirm_reads()
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


async def _why_lost(connection: AsyncConnection, still: TextClause, dataset: str) -> str | None:
    """Why `connection` no longer has `dataset`'s lock as `still` reads it, or `None`."""
    try:
        held = await connection.scalar(still, {"namespace": _LOCK_NAMESPACE, "dataset": dataset})
    except DBAPIError as exc:
        # A disconnect on the lock's own connection *is* the lock lost; anything else is
        # not this function's to name.
        if not exc.connection_invalidated:
            raise
        return "its connection ended"
    return None if held else "its lock is gone"


async def _give_back(connection: AsyncConnection, unlock: TextClause, datasets: list[str]) -> None:
    """Unlock each of `datasets` on `connection` with `unlock`, then close it.

    One that did not unlock may still hold a lock, so its backend is ended rather than
    returned to the pool. One that had already ended -- a server restart,
    `idle_session_timeout` -- took every lock on it along: given back, and no error.
    """
    try:
        for dataset in datasets:
            await connection.execute(unlock, {"namespace": _LOCK_NAMESPACE, "dataset": dataset})
    except DBAPIError as exc:
        await _discard(connection)
        if not exc.connection_invalidated:
            raise
    except BaseException:
        await _discard(connection)
        raise
    else:
        await connection.close()


async def _discard(connection: AsyncConnection) -> None:
    """Close `connection` by ending its backend, so no lock on it outlives it in the pool."""
    try:
        await connection.invalidate()
    finally:
        await connection.close()
