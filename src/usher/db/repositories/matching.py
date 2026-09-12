"""Batch matching for PRD 03's match stage."""

import uuid
from collections.abc import Sequence

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.enums import EnrichmentState
from usher.ports.ingest import NameYearProbe, ProviderRef
from usher.ports.repository import TitleMatchRepository

# `t.kind = p.kind` is not optional and not a convenience filter: TMDb's movie
# and series id spaces overlap on 26,968 ids (measured), so `tmdb_id` alone
# identifies nothing. ADR-0011.
_MATCH_TMDB = """
SELECT p.value AS value, p.kind AS kind, t.id AS id
FROM unnest(CAST(:values AS integer[]), CAST(:kinds AS text[])) AS p(value, kind)
JOIN titles t ON t.tmdb_id = p.value AND t.kind = p.kind
"""

# `tt` ids are one global namespace, so no kind participates -- an IMDb ref
# that carries one is answered anyway, because filtering on it would drop
# every match whose catalog kind disagrees with what a source guessed.
_MATCH_IMDB = "SELECT imdb_id AS value, id FROM titles WHERE imdb_id = ANY(:values)"

_MATCH_TVDB = "SELECT tvdb_id AS value, id FROM titles WHERE tvdb_id = ANY(:values)"

_MATCH_NAME_YEAR = """
WITH probe AS (
    SELECT * FROM unnest(
        CAST(:names AS text[]), CAST(:years AS integer[]), CAST(:kinds AS text[])
    ) AS p(name, year, kind)
), candidate AS (
    SELECT p.name AS name, p.year AS year, p.kind AS kind, t.id AS id,
           count(*) OVER (PARTITION BY p.name, p.year, p.kind) AS matches
    FROM probe p
    -- lower(t.name), not lower(<the probe>) against t.name -- see the module
    -- docstring. Note the deliberate circumlocution: SQLAlchemy's text()
    -- bind-parameter regex scans SQL comments too, so writing the wrong
    -- spelling out literally here declares a bind parameter nothing supplies
    -- and every call raises `A value is required for bind parameter 'name'`.
    -- `t.year BETWEEN p.year - 1 AND p.year + 1` is also what
    -- makes a probe with no year, and a title with no year, resolve to
    -- nothing: NULL propagates through BETWEEN and the row never qualifies.
    -- Spelling it any other way (COALESCE, IS NOT DISTINCT FROM) would match
    -- a 2016 probe against every undated IMDb skeleton of the same name.
    JOIN titles t
      ON lower(t.name) = lower(p.name)
     AND t.kind = p.kind
     AND t.year BETWEEN p.year - 1 AND p.year + 1
)
-- `matches = 1`, so an ambiguous probe drops out entirely rather than picking
-- a winner. PRD 03 stage 5: no *confident* match means the review queue, and
-- a coin flip between two remakes attaches the household's watch history to
-- the wrong film, silently.
SELECT name, year, kind, id FROM candidate WHERE matches = 1
"""

# `id = ANY(...)` over the primary key, so one index scan for the whole batch.
# One column, not the row: the caller compares tiers through `ENRICHMENT_RANK`
# and hydrating 500 `Title`s a batch to read one enum would be the expensive
# way to answer a cheap question.
_ENRICHMENT_STATES = "SELECT id, enrichment_state FROM titles WHERE id = ANY(:ids)"


class PostgresTitleMatchRepository(TitleMatchRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def name_year_sql() -> str:
        """The literal name+year statement, for `EXPLAIN` in the integration
        suite. A plan assertion against a hand-copied lookalike drifts from
        the statement that actually runs, and the whole point of this one is
        that a wrong spelling returns identical rows."""
        return _MATCH_NAME_YEAR

    async def match_by_provider_ids(
        self, refs: Sequence[ProviderRef]
    ) -> dict[ProviderRef, uuid.UUID]:
        # `dict.fromkeys` deduplicates while keeping the caller's order.
        unique = list(dict.fromkeys(refs))
        resolved: dict[ProviderRef, uuid.UUID] = {}

        # TMDb: keyed on the (id, kind) pair, and a ref with no kind names
        # nothing rather than one of two arbitrarily.
        tmdb = {
            (number, ref.kind.value): ref
            for ref in unique
            if ref.provider == "tmdb"
            and ref.kind is not None
            and (number := _as_int(ref.value)) is not None
        }
        if tmdb:
            rows = await self._fetch(
                _MATCH_TMDB,
                {
                    "values": [value for value, _ in tmdb],
                    "kinds": [kind for _, kind in tmdb],
                },
            )
            for row in rows:
                resolved[tmdb[(row["value"], row["kind"])]] = row["id"]

        # IMDb: one global namespace, so the value alone is the key.
        imdb = {ref.value: ref for ref in unique if ref.provider == "imdb"}
        if imdb:
            for row in await self._fetch(_MATCH_IMDB, {"values": list(imdb)}):
                resolved[imdb[row["value"]]] = row["id"]

        tvdb = {
            number: ref
            for ref in unique
            if ref.provider == "tvdb" and (number := _as_int(ref.value)) is not None
        }
        if tvdb:
            for row in await self._fetch(_MATCH_TVDB, {"values": list(tvdb)}):
                resolved[tvdb[row["value"]]] = row["id"]

        # Every other provider is skipped rather than raised on. Emby reports
        # whatever `ProviderIds` a library's scrapers wrote, and "none that I
        # can tell" is the honest answer to one this catalog has no column
        # for.
        return resolved

    async def match_by_name_year(
        self, probes: Sequence[NameYearProbe]
    ) -> dict[NameYearProbe, uuid.UUID]:
        unique = list(dict.fromkeys(probes))
        if not unique:
            return {}
        by_key = {(probe.name, probe.year, probe.kind.value): probe for probe in unique}
        rows = await self._fetch(
            _MATCH_NAME_YEAR,
            {
                "names": [probe.name for probe in unique],
                "years": [probe.year for probe in unique],
                "kinds": [probe.kind.value for probe in unique],
            },
        )
        return {by_key[(row["name"], row["year"], row["kind"])]: row["id"] for row in rows}

    async def enrichment_states(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, EnrichmentState]:
        unique = list(dict.fromkeys(title_ids))
        if not unique:
            return {}
        rows = await self._fetch(_ENRICHMENT_STATES, {"ids": unique})
        return {row["id"]: EnrichmentState(row["enrichment_state"]) for row in rows}

    async def _fetch(self, statement: str, parameters: dict[str, object]) -> Sequence[RowMapping]:
        # `.mappings()` rather than attribute access on `Row`: SQLAlchemy types a
        # `text()` result's rows as `Any`-free `Row[Any]`, so `row.value` is an `object`
        # under mypy strict and every read of it is an error.
        with self._session.no_autoflush:
            result = await self._session.execute(text(statement), parameters)
        return result.mappings().all()


def _as_int(value: str) -> int | None:
    """A source is free to report `ProviderIds.Tmdb: "unknown"`. That is a
    matching failure, not a pipeline failure -- and casting it in SQL would
    abort a whole batch of 5,000 items over one bad string."""
    try:
        return int(value)
    except ValueError:
        return None
