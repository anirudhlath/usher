"""`GET /browse` through real requests against a real schema.

**What only this level can see.** `tests/unit/test_api_browse.py` drives the
route over `FakeTitleRepository`, whose ordering is Python's `sorted` and whose
keyset is a tuple comparison -- so B6's statement, its three-arm `WHERE`, its
`(key IS NOT NULL) DESC` sort key and the two facet aggregates are never
executed there. Here they are, against `pgvector/pgvector:pg17`.

**The unkeyed tail is the case this file exists for.** ADR-0034's original row
comparison is wrong for a nullable key because Postgres evaluates
`ROW(...) > ROW(...)` to **NULL, not false**, when the first differing pair
involves one -- and that is a fact about *Postgres*, not about a tuple
comparison in Python. `FakeTitleRepository` cannot express the defect at all
(a Python tuple compares `None` by raising, or not at all), so the unit arm's
version of this walk is an echo and this one is the assertion. Three of the
four sorts are nullable and `tmdb_popularity` was measured NULL on **980,523 of
1,272,367** rows of a real catalog, so the unkeyed group is most of the screen
rather than an edge.

**This module commits for real, so it cleans up after itself**, bound to its
own marker rather than emptying a table another committing file is using.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.title import Title

SECRET_KEY = "0123456789abcdef0123456789abcdef"
#: Every title this file writes carries it, so teardown deletes exactly what
#: this file created. It is also what makes the facet assertions below exact:
#: a genre nothing else in the suite uses cannot be inflated by a neighbour.
MARK = "Browse Route Case"
GENRE = "Browse-Route-Genre"


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # Both lanes off: `dependency_overrides` do not reach the lifespan, so
        # a push lane here would build the real adapter against an unreachable
        # host and open a socket.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The app reads from its own session in its own transaction, so a fixture
    that seeded through the shared rolled-back one would be handing the route
    rows it cannot see.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(
            text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@pytest_asyncio.fixture
async def client(settings: Settings, clean: None) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement SQLAlchemy issues, from every engine in the process
    -- including the app's own, which is the one under measurement.

    Captured off `before_cursor_execute` rather than transcribed: M4 replaced
    two tasks that asserted on a hand-copied lookalike of a query, because the
    copy drifts from the repository and then reads like coverage.
    """
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


async def _seed(sessions: async_sessionmaker[AsyncSession], name: str, **changes: object) -> Title:
    title = Title.model_validate(
        {
            "kind": TitleKind.MOVIE,
            "name": f"{MARK} {name}",
            "sort_name": f"{MARK} {name}",
            **changes,
        }
    )
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title


async def test_a_page_boundary_inside_the_unkeyed_group_keeps_the_rest_of_it(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Resuming from a NULL-keyed row returns the rest of the unkeyed group.

    **This is the case ADR-0034's correction exists for and the fake cannot
    make it.** With the refuted row-comparison spelling Postgres answers NULL
    rather than false for the boundary comparison, so the whole unkeyed tail
    disappears while every page served looks full -- a failure with no symptom
    a client could report.

    The premise is asserted rather than assumed: the boundary row really is
    unkeyed, and there really is more of the unkeyed group after it.
    """
    await _seed(sessions, "A keyed", genres=(GENRE,), tmdb_popularity=9.0)
    for index in range(3):
        await _seed(sessions, f"B unkeyed {index}", genres=(GENRE,), tmdb_popularity=None)

    first = await client.get("/browse", params={"sort": "popularity", "genre": GENRE, "limit": 2})
    assert first.status_code == 200, first.text
    page_one = first.json()
    assert [one["popularity"] for one in page_one["items"]] == [9.0, None], (
        "the premise: the page-one boundary is a row with a NULL sort key"
    )

    second = await client.get(
        "/browse",
        params={
            "sort": "popularity",
            "genre": GENRE,
            "limit": 2,
            "cursor": page_one["next_cursor"],
        },
    )

    assert second.status_code == 200, second.text
    names = [one["name"] for one in second.json()["items"]]
    assert names == [f"{MARK} B unkeyed 1", f"{MARK} B unkeyed 2"], (
        "the unkeyed tail was dropped by the boundary comparison"
    )


async def test_the_whole_population_is_walked_once_across_pages(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Four rows at `limit=2` -- exact exhaustion -- against the real
    statement, and no page is empty.

    Seeded so that `sort_name` order is the reverse of id order, and that is
    asserted as the case's own premise: UUIDv7 makes `ORDER BY id` and
    `ORDER BY sort_name` agree by accident for any fixture seeded
    alphabetically, and B6's statement really does end in `titles.id ASC`.
    """
    seeded = [await _seed(sessions, name, genres=(GENRE,)) for name in ("D", "C", "B", "A")]
    assert seeded[0].id < seeded[-1].id, (
        "the premise: the row that sorts last by name was minted first"
    )

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 5, "the cursor never went null; this walk does not terminate"
        params: dict[str, str | int] = {"sort": "name", "genre": GENRE, "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/browse", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"], "a cursor was minted for a page with nothing on it"
        collected.extend(one["name"] for one in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert collected == [f"{MARK} {one}" for one in ("A", "B", "C", "D")]


async def test_a_predicated_browse_carries_real_counts_from_the_two_aggregates(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The facet block, computed by B6's `unnest`/`GROUP BY` and its year
    aggregate rather than by a dict comprehension.

    **Each facet drops its own predicate**, and that is what this asserts: with
    `year` active, the *year* facet still counts both years, while the genre
    facet is narrowed to the requested year. A facet folded back onto its own
    filter answers "how many 1999 films are from 1999" and looks entirely
    correct on every request that does not use it.
    """
    await _seed(sessions, "A", genres=(GENRE, "Browse-Route-Other"), year=1999)
    await _seed(sessions, "B", genres=(GENRE,), year=2001)

    response = await client.get("/browse", params={"year": 1999, "facets": "true"})

    assert response.status_code == 200, response.text
    facets = response.json()["facets"]
    assert facets["computed"] is True
    # The genre facet keeps the year predicate, so only the 1999 title counts.
    assert facets["genres"][GENRE] == 1
    assert facets["genres"]["Browse-Route-Other"] == 1
    # The year facet drops its own predicate, so both years are still there --
    # which is what makes the counts navigable.
    assert facets["years"]["1999"] == 1
    assert facets["years"]["2001"] == 1


async def test_an_unpredicated_browse_computes_no_aggregate_at_all(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    """The 330.81 ms request is not made, and the response says why.

    The assertion is on the **statements**, not only on the body: a route that
    computed the aggregates and then declined to render them would answer an
    identical document and would still have paid for it. `browse_facets` issues
    a `GROUP BY` per arm, so the absence of any `GROUP BY` is what says the read
    did not happen.
    """
    await _seed(sessions, "A", genres=(GENRE,), year=1999)
    statement_counter.clear()

    response = await client.get("/browse", params={"facets": "true"})

    assert response.status_code == 200, response.text
    facets = response.json()["facets"]
    assert facets["computed"] is False
    assert facets["reason"] == "unpredicated"
    assert "genres" not in facets
    # The premise: this counter really saw the request's statements. A scan
    # that globbed nothing passes exactly like a scan that passes.
    assert any("FROM titles" in one for one in statement_counter), (
        "the counter saw no browse statement at all"
    )
    assert not [one for one in statement_counter if "GROUP BY" in one], (
        "an aggregate ran for a request whose answer says none did"
    )
