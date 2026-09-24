"""The shared contract against real Postgres, plus the one thing no fake can see: the plan."""

import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.title_match_repository_contract import (
    TitleCatalog,
    TitleMatchRepositoryContract,
)
from tests.integration.conftest import Analyze
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.ingest import NameYearProbe, ProviderRef

# Enough rows that the planner reliably chooses `ix_titles_name_lower_year`
# over a seq scan. A flaky plan assertion is worse than none, so this is a
# comfortable number rather than the smallest one that works.
_PLAN_ROWS = 2_000


class _PostgresCatalog(TitleCatalog):
    def __init__(self, session: AsyncSession) -> None:
        self._titles = PostgresTitleRepository(session)

    async def given_title(
        self,
        *,
        kind: TitleKind,
        name: str,
        year: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
        enrichment_state: EnrichmentState = EnrichmentState.SKELETON,
    ) -> uuid.UUID:
        title = Title(
            kind=kind,
            name=name,
            sort_name=name,
            year=year,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            tvdb_id=tvdb_id,
            enrichment_state=enrichment_state,
        )
        await self._titles.add(title)
        return title.id


@pytest.fixture
def repository(session: AsyncSession) -> PostgresTitleMatchRepository:
    return PostgresTitleMatchRepository(session)


@pytest_asyncio.fixture
async def catalog(session: AsyncSession) -> TitleCatalog:
    return _PostgresCatalog(session)


class TestPostgresTitleMatchRepository(TitleMatchRepositoryContract):
    """Every case in `TitleMatchRepositoryContract`, against real Postgres."""


async def test_a_batch_mixing_providers_does_not_cast_an_imdb_id_to_an_integer(
    repository: PostgresTitleMatchRepository, catalog: TitleCatalog
) -> None:
    """A mixed batch never casts an IMDb reference to an integer.

    One `unnest` joined against `titles` with an `OR` over the three providers has to
    write `p.value::integer` for the TMDb and TVDB arms, and Postgres does not
    guarantee to evaluate the provider test first, so an IMDb reference alongside any
    TMDb one fails the whole page. A fake cannot reach this: Python never casts a
    value it did not ask to cast. Splitting by provider is what makes the mixed batch
    below ordinary rather than fatal.
    """
    movie = await catalog.given_title(kind=TitleKind.MOVIE, tmdb_id=90000550, name="Fight Club")
    film = await catalog.given_title(
        kind=TitleKind.MOVIE, imdb_id="tt99000020", name="A Synthetic Feature"
    )
    series = await catalog.given_title(
        kind=TitleKind.SERIES, tvdb_id=91000030, name="A Synthetic Series"
    )
    refs = [
        ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE),
        ProviderRef(provider="imdb", value="tt99000020", kind=None),
        ProviderRef(provider="tvdb", value="91000030", kind=None),
        ProviderRef(provider="tmdb", value="not-a-number", kind=TitleKind.MOVIE),
        ProviderRef(provider="zap2it", value="EP001", kind=None),
    ]
    resolved = await repository.match_by_provider_ids(refs)
    assert resolved[refs[0]] == movie
    assert resolved[refs[1]] == film
    assert resolved[refs[2]] == series
    assert len(resolved) == 3


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    seen: list[str] = []

    def record(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


async def test_a_batch_costs_a_bounded_number_of_statements(
    repository: PostgresTitleMatchRepository,
    catalog: TitleCatalog,
    session: AsyncSession,
    statement_counter: list[str],
) -> None:
    """A batch costs a bounded number of statements, which is the reason this port exists.

    `TitleRepository.get_by_tmdb_id` answers one question; a catalog walk asks
    millions, and per-item round trips turn one sync into minutes of pure latency.
    """
    for index in range(200):
        await catalog.given_title(
            kind=TitleKind.MOVIE, tmdb_id=index, name=f"Movie {index}", year=2000
        )
    await session.flush()

    statement_counter.clear()
    await repository.match_by_provider_ids(
        [
            ProviderRef(provider="tmdb", value=str(index), kind=TitleKind.MOVIE)
            for index in range(200)
        ]
    )
    assert len(statement_counter) == 1, f"200 refs cost {len(statement_counter)} statements"

    statement_counter.clear()
    await repository.match_by_name_year(
        [
            NameYearProbe(name=f"Movie {index}", year=2000, kind=TitleKind.MOVIE)
            for index in range(200)
        ]
    )
    assert len(statement_counter) == 1, f"200 probes cost {len(statement_counter)} statements"


async def test_name_year_matching_uses_the_expression_index(
    session: AsyncSession,
    catalog: TitleCatalog,
    analyze: Analyze,
) -> None:
    """Name+year matching goes through the `lower(name)` expression index.

    A query that lowercases the *probe* instead of the column cannot use that index at
    all, and the fake -- which matches on `name.lower()` in Python -- agrees with
    either spelling. Only the plan tells them apart.
    """
    for index in range(_PLAN_ROWS):
        await catalog.given_title(
            kind=TitleKind.MOVIE, name=f"Movie {index}", year=2000 + index % 20
        )
    await session.flush()
    await analyze("titles")
    plan = "\n".join(
        (
            await session.execute(
                text("EXPLAIN " + PostgresTitleMatchRepository.name_year_sql()),
                {"names": ["Movie 7"], "years": [2007], "kinds": ["movie"]},
            )
        )
        .scalars()
        .all()
    )
    assert "Index Cond: (lower(name) = lower(p.name))" in plan, plan
    assert "Seq Scan on titles" not in plan, plan


async def test_provider_id_matching_uses_the_namespaced_index(
    session: AsyncSession,
    catalog: TitleCatalog,
    analyze: Analyze,
) -> None:
    """Provider-id matching reaches the partial unique index rather than a seq scan.

    `ix_titles_tmdb_id_kind` is partial (`WHERE tmdb_id IS NOT NULL`), and a plain
    `t.tmdb_id = p.value` is what lets Postgres prove the predicate and use it. A
    `COALESCE` or an `IS NOT DISTINCT FROM` in that join condition returns the same
    rows off a seq scan.
    """
    for index in range(_PLAN_ROWS):
        await catalog.given_title(
            kind=TitleKind.MOVIE, name=f"Movie {index}", tmdb_id=index, year=2000
        )
    await session.flush()
    await analyze("titles")
    plan = "\n".join(
        (
            await session.execute(
                text(
                    "EXPLAIN SELECT p.value, p.kind, t.id "
                    "FROM unnest(CAST(:values AS integer[]), CAST(:kinds AS text[])) "
                    "AS p(value, kind) "
                    "JOIN titles t ON t.tmdb_id = p.value AND t.kind = p.kind"
                ),
                {"values": [7], "kinds": ["movie"]},
            )
        )
        .scalars()
        .all()
    )
    assert "ix_titles_tmdb_id_kind" in plan, plan
    assert "Seq Scan on titles" not in plan, plan
