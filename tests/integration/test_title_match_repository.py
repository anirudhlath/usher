"""The shared contract against real Postgres, plus the one thing no fake can
see: the plan.

`FakeTitleMatchRepository` matches on `name.lower()` in Python, so it agrees
with `lower(name)` by construction. The wrong spelling --
`lower(:probe) = t.name`, or `t.name ILIKE :probe` -- returns *identical
rows* while seq-scanning 1,271,138 of them per probe. No assertion on results
can tell them apart, which is why the last two cases here assert on
`EXPLAIN`.
"""

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
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.ingest import NameYearProbe, ProviderRef

# What it took for the planner to reliably choose `ix_titles_name_lower_year`
# over a seq scan here. Measured on `pgvector/pgvector:pg17`: at 200 rows (the
# plan's suggestion) it still chose a seq scan, at 2,000 it chose the index
# every run. A flaky plan assertion is worse than none, so this is the
# comfortable number rather than the smallest one that ever worked.
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
    """The plan's own single-join spelling, refuted. One `unnest` joined
    against `titles` with an `OR` over the three providers has to write
    `p.value::integer` for the TMDb and TVDB arms, and Postgres does not
    guarantee to evaluate the provider test first -- so a batch carrying
    `('imdb', 'tt99000020')` alongside any TMDb ref answers
    `invalid input syntax for type integer: "tt99000020"` and the whole page
    of 5,000 items fails.

    A fake cannot reach this at all: Python never casts a value it did not
    ask to cast. Splitting by provider is what makes the mixed batch below
    ordinary rather than fatal.
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
    """The whole reason this port exists. `TitleRepository.get_by_tmdb_id`
    answers one question and a walk asks 1,126,674 of them; at ~0.1 ms per
    indexed point lookup that is minutes of pure round trips per sync -- and
    the name+year tier extrapolates to ~600 ms per item unindexed."""
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
    session: AsyncSession, catalog: TitleCatalog
) -> None:
    """A query that lowercases the *probe* instead of the column cannot use an
    expression index on `lower(name)` at all, and the fake -- which matches on
    `name.lower()` in Python -- agrees with either spelling. Only the plan
    tells them apart.

    Explains the repository's own statement, binds and all, rather than a
    hand-copied lookalike: a plan assertion about a query nothing issues reads
    like coverage and is worse than none.

    **Asserted on the `Index Cond`, not on an index name, and that is a
    correction `m09a` forced.** This read `"ix_titles_name_lower_year" in
    plan` while that was the only expression index on `lower(name)`; `m09a`
    added a second (`ix_titles_name_lower_prefix`, `lower(name)
    text_pattern_ops`, tier 1 of the two-tier suggest), whose opclass family
    contains `=`, so the planner may serve this equality from either. The
    `Index Cond` is the property the case is actually about and it is strictly
    stronger than a name: an index name in a plan does not prove the *column*
    was the thing lowercased.

    **The swap costs nothing, measured rather than assumed.** On
    `pgvector/pgvector:pg17` at 200,000 titles, `EXPLAIN (ANALYZE, BUFFERS)`
    over this exact statement: `ix_titles_name_lower_prefix` gives
    `cost=0.42..8.45`, **4 buffers, 0.031 ms**, and dropping it so
    `ix_titles_name_lower_year` must serve gives `cost=0.43..8.45`, **4
    buffers, 0.031 ms** -- byte-identical plans below the index node. The
    narrower index wins the tie because it is one column narrower; the
    two-column one remains the only one that can also serve the `year`
    predicate from the index, which is why both are kept.
    """
    for index in range(_PLAN_ROWS):
        await catalog.given_title(
            kind=TitleKind.MOVIE, name=f"Movie {index}", year=2000 + index % 20
        )
    await session.flush()
    await session.execute(text("ANALYZE titles"))
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
    session: AsyncSession, catalog: TitleCatalog
) -> None:
    """`ix_titles_tmdb_id_kind` is unique and partial (`WHERE tmdb_id IS NOT
    NULL`), and `t.tmdb_id = p.value` is what lets Postgres prove the
    predicate and use it. A `COALESCE` or an `IS NOT DISTINCT FROM` in that
    join condition would return the same rows off a seq scan of 1,271,138."""
    for index in range(_PLAN_ROWS):
        await catalog.given_title(
            kind=TitleKind.MOVIE, name=f"Movie {index}", tmdb_id=index, year=2000
        )
    await session.flush()
    await session.execute(text("ANALYZE titles"))
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
