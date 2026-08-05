"""Reads over `genome_scores`, the MovieLens tag-genome vectors.

Implements `GenomeRepository`. Read-only: the writer is
`BulkCatalogRepository.upsert_genome_vectors`, because writing this table is
a staged, `COPY`-scale, set-based join from `imdb_id` to `titles.id`.

**Two rows are fetched by two equality predicates ORed together, not by
`title_id IN (:left, :right)`, and that is deliberate.** A self-pair is a
legitimate call — it is what a caller does when it has not yet excluded the
seed from its own candidate list — and `IN` plus a `len(rows) == 2` check
finds one row for it and reports the vector as missing.
`GenomeRepositoryContract` has the case.

**`CAST(:x AS uuid)`, never `:x::uuid`.** SQLAlchemy's `text()`
bind-parameter regex treats a name immediately followed by `::` as a
Postgres cast and skips the bind entirely, so the literal string
`:left::uuid` reaches asyncpg, which answers `PostgresSyntaxError: syntax
error at or near ":"`. The same regex scans `--` comments, so a comment here
must not quote a colon-prefixed parameter spelling either.

**A bare `text()` hands `halfvec` back as a *string*, and `.columns()` is
what fixes it.** asyncpg has no codec for a pgvector type, so it returns the
extension's text output form -- `'[0.1,0.2,...]'` -- and SQLAlchemy has no
column type to attach a result processor to, because a `text()` construct
carries no type information at all. `tuple(record.relevance)` on that string
yields 2,256 one-character strings and raises nothing until something tries
arithmetic on them -- a wrong value of the right shape, which is this
milestone's headline failure at the driver boundary. Declaring the result
columns with `.columns(...)` gives SQLAlchemy the `HALFVEC` type, whose
result processor parses that form.

**It parses it into a plain `list[float]`, not into a `HalfVector`** --
verified against pgvector 0.8.6's own SQLAlchemy type, whose
`result_processor` calls `HalfVector._from_db` and returns a list when numpy
is absent. So there is no `.to_list()` to call, and code written for one is
an `AttributeError` at the first read. Recorded because the obvious
expectation is the other one.
"""

import uuid
from typing import Any, cast

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Text, column, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.bulk import GENOME_TAG_COUNT
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

# Two equality predicates rather than `IN`: see the module docstring for the
# self-pair the `IN` spelling gets wrong. No `ORDER BY` -- the two rows are
# keyed back to the caller's own arguments below rather than by position,
# which is the same rule `SourceEvent.watch_states` states one layer up.
_GET_PAIR = text(
    "SELECT title_id, relevance, genome_revision FROM genome_scores "
    "WHERE title_id = CAST(:left AS uuid) OR title_id = CAST(:right AS uuid)"
).columns(*_COLUMNS)


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
