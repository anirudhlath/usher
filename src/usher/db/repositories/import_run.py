"""Checkpoint storage for the bulk importers."""

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
            # (ADR-0044).** `position`, `rows_seen` and `rows_written` are `integer` and
            # `ImportRun` bounds all three `ge=0` with no ceiling, so a resumable
            # importer's own cursor is a validly constructed model this table cannot
            # hold.
            if not is_row_refusal(exc):
                raise
            # `dataset` is unique: two processes bootstrapping the same dataset at once
            # is a real operator mistake, and it must surface as a port error rather
            # than a raw sqlalchemy exception (ADR-0009).
            await self._session.rollback()
            # **The `constraint=` widens with the `except` and the sentence branches on
            # it**, which is not the same thing as generalising the sentence.
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
