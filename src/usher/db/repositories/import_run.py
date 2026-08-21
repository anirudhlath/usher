"""Checkpoint storage for the bulk importers.

Implements `ImportRunRepository` (`usher.ports.repository`). Unlike
`usher.db.repositories.bulk`, this one *does* go through the ORM: there is
exactly one row per dataset and it is written once per batch, so the
per-statement overhead the bulk path exists to avoid is irrelevant here.

Session-poisoning decision, the opposite of `PostgresTitleRepository`'s:
`save()` rolls back the *whole* transaction on a caught `IntegrityError`
rather than confining the damage to a SAVEPOINT (`session.begin_nested()`).
`PostgresTitleRepository` needs the SAVEPOINT because its callers share one
session across many unrelated statements in a single transaction (PRD 01:
"the caller owns the session and the transaction"), so a full rollback
there would silently discard whichever of those a caller had pending. This
repository's only caller, `BootstrapService`, doesn't have that shape:
every transaction boundary on the session it hands this class brackets
exactly one batch-or-run -- a dataset's `start()` call with nothing else
yet pending, or a `_drain()` batch's data write plus its cursor save,
committed together right after (see that module's own docstring on why the
two must land in the same transaction or not at all). Whatever else is
unflushed on the session at the moment `save()` raises is, by this
construction, part of that same unit and *should* go with it, not survive
it -- so there is never a caller's independent pending work for a SAVEPOINT
to protect here, unlike `TitleRepository`'s general-purpose callers.

The rollback is required, not optional, despite `save()`'s only caller
being able to tolerate losing that unit of work: Postgres leaves a
session's entire transaction aborted after an uncaught statement error
until an explicit `ROLLBACK`, so *any* further statement on this session --
not just a retried write -- raises `sqlalchemy.exc.PendingRollbackError`
instead of whatever it was trying to do. `BootstrapService.import_dataset`'s
except handler is exactly such a further statement: it calls
`self._runs.get(dataset.name)` immediately after catching this same
`RepositoryConflict`, to build the durable `FAILED` record its own
docstring promises callers ("it does not re-raise"). Verified against real
Postgres, both the failure without this rollback and the recovery with it:
`tests/integration/test_import_run_repository.py::
test_the_session_survives_a_conflict_for_the_callers_next_statement`.
"""

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories._errors import constraint_name, is_row_refusal
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import ImportRunRepository


def _to_domain(row: ImportRunRow) -> ImportRun:
    # Same shape as PostgresTitleRepository._to_domain, and safe for the same
    # reason: ImportRunRow's 11 columns are 1:1 by name with ImportRun's 11
    # fields, so `extra="forbid"` turns any future drift into a loud
    # ValidationError instead of a silently dropped column.
    return ImportRun.model_validate(
        {column.name: getattr(row, column.name) for column in ImportRunRow.__table__.columns}
    )


class PostgresImportRunRepository(ImportRunRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(self, dataset: str, revision: str) -> ImportRun:
        existing = await self.get(dataset)
        if existing is None:
            run = ImportRun(dataset=dataset, revision=revision)
        elif existing.revision == revision:
            # Same upstream snapshot: keep the cursor and continue.
            run = existing.evolve(status=ImportRunStatus.RUNNING, error=None, finished_at=None)
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
            )
        await self.save(run)
        return run

    async def save(self, run: ImportRun) -> None:
        data = run.model_dump()
        try:
            row = await self._session.get(ImportRunRow, run.id)
            if row is None:
                self._session.add(ImportRunRow(**data))
            else:
                for key, value in data.items():
                    if key != "id":
                        setattr(row, key, value)
            await self._session.flush()
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9
            # (ADR-0043).** `position`, `rows_seen` and `rows_written` are
            # `integer` and `ImportRun` bounds all three `ge=0` with no
            # ceiling, so a resumable importer's own cursor is a validly
            # constructed model this table cannot hold. SQLAlchemy's asyncpg
            # dialect does not map SQLSTATE class 22 onto any classified
            # subclass, so that refusal arrives as a bare `DBAPIError` which
            # `except IntegrityError` did not catch, and the driver's own
            # exception crossed this port boundary untranslated -- the one
            # thing ADR-0009 forbids. `db/repositories/_errors.py` holds the
            # two measured shapes and the only copy of the predicate.
            # Everything that is *not* a row refusal -- a dropped connection,
            # a statement timeout, an undefined table -- still propagates.
            if not is_row_refusal(exc):
                raise
            # `dataset` is unique: two processes bootstrapping the same dataset
            # at once is a real operator mistake, and it must surface as a port
            # error rather than a raw sqlalchemy exception (ADR-0009).
            #
            # The rollback is not cleanup for its own sake -- without it,
            # Postgres leaves this session's transaction aborted, and the
            # *next* statement on it (not this one) raises
            # sqlalchemy.exc.PendingRollbackError instead of running. See
            # the module docstring for why a full rollback, rather than a
            # SAVEPOINT, is correct for this repository's one caller.
            await self._session.rollback()
            # **The `constraint=` widens with the `except` and the sentence
            # branches on it**, which is not the same thing as generalising the
            # sentence. `uq_import_runs_dataset` is the only *named* constraint
            # this table can violate and it is no longer the only refusal this
            # handler sees -- an out-of-range cursor is a declared width
            # rejecting a value, not a constraint firing -- so naming it
            # unconditionally sends an operator to look at an index that is
            # intact. But generalising to "violates import_runs' own bounds"
            # for *every* refusal is a regression in the opposite direction and
            # exactly where the message used to be actionable: two processes
            # bootstrapping the same dataset at once is the common operator
            # mistake, and it is the thing this handler was written for.
            #
            # `constraint_name()` answers a name for a named constraint and
            # `None` for a column refusing a value, which is precisely the
            # distinction the two sentences want. So it is read once and
            # branched on, rather than being asked to carry both meanings.
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
                select(ImportRunRow).where(ImportRunRow.dataset == dataset)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def list_runs(self) -> list[ImportRun]:
        with self._session.no_autoflush:
            result = await self._session.execute(
                select(ImportRunRow).order_by(ImportRunRow.heartbeat_at.desc())
            )
        return [_to_domain(row) for row in result.scalars()]
