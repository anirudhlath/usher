"""Persistence for configured sources.

Follows PostgresTitleRepository's two structural decisions verbatim, for
the reasons its module docstring works through at length:

- `add()`/`update()` wrap their flush in `session.begin_nested()`, a
  SAVEPOINT, rather than `session.rollback()` -- the caller owns the
  transaction, and this repository's one real caller (`SourceService.
  register`) has the credential write pending on the same session.
- Reads run inside `session.no_autoflush`, so unrelated pending state left
  on a shared session cannot make a pure read raise a storage exception
  from behind this port.
"""

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.source import SourceRow
from usher.domain.source import Source
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SourceRepository

# Written by update(); created_at is excluded because Postgres owns it, and
# id is excluded because it is the lookup key, not a mutable column.
_MUTABLE = (
    "kind",
    "name",
    "base_url",
    "credentials_ref",
    "device_id",
    "enabled",
    "supports_push",
)


def _to_domain(row: SourceRow) -> Source:
    return Source.model_validate(
        {column.name: getattr(row, column.name) for column in SourceRow.__table__.columns}
    )


class PostgresSourceRepository(SourceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, source: Source) -> None:
        row = SourceRow(**source.model_dump(exclude={"created_at", "updated_at"}))
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"source {source.id} conflicts with an existing source", constraint="pk_sources"
            ) from exc

    async def update(self, source: Source) -> None:
        try:
            async with self._session.begin_nested():
                row = await self._session.get(SourceRow, source.id)
                if row is None:
                    raise RepositoryNotFound(f"source {source.id} does not exist")
                for field in _MUTABLE:
                    setattr(row, field, getattr(source, field))
                await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"source {source.id} conflicts with an existing source", constraint=None
            ) from exc

    async def get(self, source_id: uuid.UUID) -> Source | None:
        with self._session.no_autoflush:
            row = await self._session.get(SourceRow, source_id)
        return None if row is None else _to_domain(row)

    async def list_all(self) -> list[Source]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(select(SourceRow).order_by(SourceRow.name))
            ).scalars()
            return [_to_domain(row) for row in rows]

    async def delete(self, source_id: uuid.UUID) -> bool:
        # `rowcount` lives on `CursorResult`, not the `Result[Any]`
        # `AsyncSession.execute` is typed as returning -- mypy strict rejects
        # `result.rowcount` without this narrowing (same fix as
        # PostgresBulkCatalogRepository._rowcount). Every statement here is
        # a DML statement, which always yields a `CursorResult` at runtime.
        result = await self._session.execute(delete(SourceRow).where(SourceRow.id == source_id))
        await self._session.flush()
        return cast(CursorResult[Any], result).rowcount > 0
