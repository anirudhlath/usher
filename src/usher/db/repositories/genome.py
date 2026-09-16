"""Reads over `genome_scores` and `genome_tags` -- the tag-genome vectors and their lanes."""

import uuid
from typing import Any, cast

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Text, column, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.bulk import GENOME_TAG_COUNT
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import GenomeRepository, GenomeVectorRow

_COLUMNS = (
    column("title_id", PGUUID(as_uuid=True)),
    column("relevance", HALFVEC(GENOME_TAG_COUNT)),
    column("genome_revision", Text()),
)

_GET = text(
    "SELECT title_id, relevance, genome_revision FROM genome_scores "
    "WHERE title_id = CAST(:title_id AS uuid)"
).columns(*_COLUMNS)

# No `ORDER BY`: the two rows are keyed back to the caller's own arguments below
# rather than by position, the same rule `SourceEvent.watch_states` states one
# layer up.
_GET_PAIR = text(
    "SELECT title_id, relevance, genome_revision FROM genome_scores "
    "WHERE title_id = CAST(:left AS uuid) OR title_id = CAST(:right AS uuid)"
).columns(*_COLUMNS)

# `ORDER BY tag_id`, and it is not decoration: the answer is positional, so the read
# order *is* the lane order.
_VOCABULARY = text("SELECT tag_id, tag, genome_revision FROM genome_tags ORDER BY tag_id")


def _row(record: Any) -> GenomeVectorRow:
    return GenomeVectorRow(
        title_id=record.title_id,
        relevance=tuple(float(value) for value in record.relevance),
        genome_revision=record.genome_revision,
    )


class PostgresGenomeRepository(GenomeRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, title_id: uuid.UUID) -> GenomeVectorRow | None:
        with self._session.no_autoflush:
            result = await self._session.execute(_GET, {"title_id": title_id})
        record = result.first()
        return None if record is None else _row(record)

    async def get_pair(
        self, left: uuid.UUID, right: uuid.UUID
    ) -> tuple[GenomeVectorRow, GenomeVectorRow] | None:
        with self._session.no_autoflush:
            result = await self._session.execute(_GET_PAIR, {"left": left, "right": right})
        by_id = {cast(uuid.UUID, record.title_id): _row(record) for record in result}
        first, second = by_id.get(left), by_id.get(right)
        if first is None or second is None:
            return None
        if first.genome_revision != second.genome_revision:
            # Not an error: a mixed table is a real, recoverable state -- a
            # killed re-import against a new upload -- and the honest answer
            # to "compare these two" is that they are not comparable. An
            # operator sees it as `SELECT genome_revision, count(*) FROM
            # genome_scores GROUP BY 1` and fixes it with a re-import.
            return None
        return first, second

    async def vocabulary(self, revision: str) -> tuple[str, ...] | None:
        with self._session.no_autoflush:
            result = await self._session.execute(_VOCABULARY)
        rows = [
            (int(record.tag_id), str(record.tag), str(record.genome_revision)) for record in result
        ]
        if not rows:
            # Never loaded. A value rather than an error -- see the port for
            # why this half is a `None` and the next one is not.
            return None
        stored = {row_revision for _, _, row_revision in rows}
        if stored != {revision}:
            # The whole reason this table carries a third column.
            raise PortDataMalformed(
                f"the stored genome vocabulary was loaded from release "
                f"{'/'.join(sorted(stored))} and cannot name the lanes of a vector from "
                f"{revision}; re-run bootstrap --phase movielens",
                detail=revision,
            )
        if [tag_id for tag_id, _, _ in rows] != list(range(1, len(rows) + 1)):
            # Built by index, `GenomeVector`'s rule at the other end of the same
            # pairing: a gap does not drop one name, it shifts every later one.
            raise PortDataMalformed(
                f"the stored genome vocabulary is not contiguous 1...{len(rows)}; a gap "
                "moves every later lane's name",
                detail=revision,
            )
        return tuple(tag for _, tag, _ in rows)
