"""`GET /collections/{id}` through a real request against a real schema.

**What only this level can see.** `tests/unit/test_api_collections.py` drives
the route over two fakes, so the counts, the ownership flags and the 404 are
covered there. What is left is the wiring and the SQL: that `create_app()`'s
**un-overridden** graph resolves both repositories onto one request-scoped
session, that `_GET_COLLECTION`'s `array_agg(... ORDER BY release_date ...)`
really is what the page renders in, that its own `kind = 'movie'` clause holds
against a row `attach_titles` would have refused, and what the whole answer
costs in statements.

The `available` half of `owned` is real only here as well:
`FakeCollectionRepository` models it, but nothing about the fake can show that
`media_items.available` is what the *join* reads.

**This module commits for real, so it cleans up after itself.** `get_session`
commits every request. Order matters in the teardown: `media_items` references
both `sources` and `titles`, and `titles.collection_id` references
`collections`, so the rows come out innermost-first.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import date

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
from usher.db.repositories.collection import PostgresCollectionRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.collection import Collection
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# Every row this file writes carries it, so teardown deletes exactly what this
# file created rather than emptying a table another committing file also uses.
MARK = "Collections Route Case"


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
    """Separately-committing sessions, not the suite's rolled-back one: the
    route reads from its own session in its own transaction."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        for statement, parameters in (
            ("DELETE FROM media_items WHERE external_id LIKE :pattern", {"pattern": f"{MARK} %"}),
            ("DELETE FROM sources WHERE name = :name", {"name": f"{MARK} Source"}),
            ("DELETE FROM titles WHERE sort_name LIKE :pattern", {"pattern": f"{MARK} %"}),
            # Last: `titles.collection_id` is a foreign key, so the members go
            # before the franchise they name.
            ("DELETE FROM collections WHERE name LIKE :pattern", {"pattern": f"{MARK} %"}),
        ):
            await session.execute(text(statement), parameters)
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@pytest_asyncio.fixture
async def client(settings: Settings, clean: None) -> AsyncIterator[AsyncClient]:
    """**No `dependency_overrides` at all**, which is the point of this file:
    `get_collection_repository` and `get_title_repository` are resolved through
    FastAPI's own machinery onto one `get_session`."""
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement SQLAlchemy issues, captured off
    `before_cursor_execute` rather than transcribed."""
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


class _Catalog:
    """Real `collections`, `titles`, `sources` and `media_items` rows.

    Raw SQL for the two tables no port in this file owns, and the repositories
    for the two it does -- the same split
    `tests/integration/test_collection_repository.py` makes, and for the same
    reason: this file is about the route, not about how a source is written.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._source_id: str | None = None
        self._external = 0

    async def collection(self, name: str, *, tmdb_id: int) -> Collection:
        row = Collection(tmdb_id=tmdb_id, name=f"{MARK} {name}")
        async with self._sessions() as session:
            await PostgresCollectionRepository(session).upsert_many([row])
            await session.commit()
        return row

    async def member(
        self,
        collection: Collection,
        name: str,
        *,
        release_date: date | None = None,
        kind: TitleKind = TitleKind.MOVIE,
        force: bool = False,
    ) -> Title:
        """A title, linked to the franchise.

        `force` writes `titles.collection_id` with a raw `UPDATE` instead of
        through `attach_titles`, which is the only way to get a **series** onto
        a collection: the port filters `kind = 'movie'` on the way in. That is
        exactly the row the scoped read has to refuse for itself, since `titles`
        deliberately carries no CHECK to stop it being stored.
        """
        title = Title(
            kind=kind,
            name=name,
            sort_name=f"{MARK} {name}",
            year=release_date.year if release_date else None,
            enrichment_state=EnrichmentState.ENRICHED,
        )
        async with self._sessions() as session:
            await PostgresTitleRepository(session).add(title)
            if release_date is not None:
                await session.execute(
                    text("UPDATE titles SET release_date = :date WHERE id = CAST(:id AS uuid)"),
                    {"date": release_date, "id": title.id},
                )
            if force:
                await session.execute(
                    text(
                        "UPDATE titles SET collection_id = CAST(:collection_id AS uuid) "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"collection_id": collection.id, "id": title.id},
                )
            else:
                await PostgresCollectionRepository(session).attach_titles(
                    [(title.id, collection.id)]
                )
            await session.commit()
        return title

    async def _source(self, session: AsyncSession) -> str:
        if self._source_id is None:
            self._source_id = str(new_id())
            await session.execute(
                text(
                    "INSERT INTO sources "
                    "(id, kind, name, base_url, credentials_ref, device_id, enabled) "
                    "VALUES (CAST(:id AS uuid), 'emby', :name, 'https://source.invalid', "
                    ":credentials_ref, :device_id, true)"
                ),
                {
                    "id": self._source_id,
                    "name": f"{MARK} Source",
                    "credentials_ref": f"ref-{self._source_id}",
                    "device_id": f"device-{self._source_id}",
                },
            )
        return self._source_id

    async def own(self, title: Title, *, available: bool = True) -> None:
        async with self._sessions() as session:
            source_id = await self._source(session)
            self._external += 1
            await session.execute(
                text(
                    "INSERT INTO media_items "
                    "(id, source_id, external_id, title_id, episode_id, available, last_seen_at) "
                    "VALUES (CAST(:id AS uuid), CAST(:source_id AS uuid), :external_id, "
                    "CAST(:title_id AS uuid), NULL, :available, now())"
                ),
                {
                    "id": new_id(),
                    "source_id": source_id,
                    "external_id": f"{MARK} item-{self._external}",
                    "title_id": title.id,
                    "available": available,
                },
            )
            await session.commit()


@pytest.fixture
def catalog(sessions: async_sessionmaker[AsyncSession]) -> _Catalog:
    return _Catalog(sessions)


async def test_a_franchise_renders_in_release_order_with_its_completeness(
    client: AsyncClient, catalog: _Catalog
) -> None:
    """The whole answer, assembled by the shipped graph with nothing
    overridden.

    **The ordering premise is asserted**, and it is the one a UUIDv7 primary
    key gives away for free: the films are seeded latest-first, so insertion
    order and id order both agree with the wrong answer, and `list_by_ids` is a
    bare `IN (...)` that promises no order at all. An implementation rendering
    what it was handed would be rendering physical order off a real heap.

    "You own 1 of 3" is the shape PRD 06 asks for, and the unowned members are
    present rather than filtered -- a list narrowed to what the household has
    reads "1 of 1".
    """
    franchise = await catalog.collection("A Trilogy", tmdb_id=98_200_001)
    latest = await catalog.member(franchise, "The Third", release_date=date(2011, 1, 1))
    earliest = await catalog.member(franchise, "The First", release_date=date(2001, 1, 1))
    middle = await catalog.member(franchise, "The Second", release_date=date(2005, 1, 1))
    assert latest.id < earliest.id < middle.id, (
        "the fixture must make id order disagree with release order"
    )
    await catalog.own(middle)

    response = await client.get(f"/collections/{franchise.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == f"{MARK} A Trilogy"
    assert (body["owned_count"], body["total_count"]) == (1, 3)
    assert [(one["title_id"], one["owned"]) for one in body["titles"]] == [
        (str(earliest.id), False),
        (str(middle.id), True),
        (str(latest.id), False),
    ]


async def test_a_franchise_the_household_owns_one_of_is_readable_where_the_home_row_declines_it(
    client: AsyncClient, catalog: _Catalog, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The difference between the two reads, at the request level.

    `list_owned`'s floor of 2 is asserted here as the premise rather than
    assumed, on the same rows the route then answers over -- so "the route
    returned it" is a statement about `get` carrying no `min_owned`, not about
    a fixture that happened to qualify.
    """
    franchise = await catalog.collection("Barely Started", tmdb_id=98_200_002)
    members = [
        await catalog.member(franchise, f"Part {index}", release_date=date(2000 + index, 1, 1))
        for index in range(4)
    ]
    await catalog.own(members[0])

    async with sessions() as session:
        listed = await PostgresCollectionRepository(session).list_owned()
    assert franchise.id not in {one.collection_id for one in listed}, (
        "the premise: at one owned member this franchise is below list_owned's floor"
    )

    body = (await client.get(f"/collections/{franchise.id}")).json()
    assert (body["owned_count"], body["total_count"]) == (1, 4)


async def test_a_retracted_copy_does_not_count_as_owned(
    client: AsyncClient, catalog: _Catalog
) -> None:
    """`media_items.available` is what the join reads, and only this arm can
    show it: the sweep sets it false for every item a walk stops seeing, so a
    film on a temporarily unmounted drive is an ordinary state.

    The wrong implementation overstates -- "you own 2 of 2" for a household
    that can play one -- which is the direction nobody checks.
    """
    franchise = await catalog.collection("Half Mounted", tmdb_id=98_200_003)
    playable = await catalog.member(franchise, "On Disk", release_date=date(1999, 1, 1))
    retracted = await catalog.member(
        franchise, "On An Unmounted Drive", release_date=date(2003, 1, 1)
    )
    await catalog.own(playable)
    await catalog.own(retracted, available=False)

    body = (await client.get(f"/collections/{franchise.id}")).json()
    assert (body["owned_count"], body["total_count"]) == (1, 2)
    assert [one["title_id"] for one in body["titles"] if one["owned"]] == [str(playable.id)]


async def test_a_series_carrying_a_collection_id_is_not_on_the_franchise_page(
    client: AsyncClient, catalog: _Catalog
) -> None:
    """The fourth wrong implementation, at a second call site and against a row
    that really is in the table.

    `attach_titles` refuses to write it and `titles` carries no
    `CHECK (collection_id IS NULL OR kind = 'movie')`, so the row is storable
    by anything else that touches the column -- which is why the scoped read
    filters for itself rather than trusting the writer. The series is seeded
    owned, so an unfiltered read reports "you own 2 of 2" for a franchise that
    is one film and a television show.
    """
    franchise = await catalog.collection("One Film And A Series", tmdb_id=98_200_004)
    movie = await catalog.member(franchise, "The Film", release_date=date(2007, 1, 1))
    series = await catalog.member(
        franchise,
        "The Spinoff Series",
        release_date=date(2009, 1, 1),
        kind=TitleKind.SERIES,
        force=True,
    )
    await catalog.own(movie)
    await catalog.own(series)

    body = (await client.get(f"/collections/{franchise.id}")).json()
    assert [one["title_id"] for one in body["titles"]] == [str(movie.id)]
    assert (body["owned_count"], body["total_count"]) == (1, 1)


async def test_an_unknown_collection_is_a_404_from_the_real_graph(client: AsyncClient) -> None:
    """The 404 through the un-overridden wiring, so it is the row that is
    missing rather than a fake that was never seeded."""
    collection_id = new_id()
    response = await client.get(f"/collections/{collection_id}")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == f"/collections/{collection_id}"


async def test_the_whole_answer_costs_two_statements(
    client: AsyncClient, catalog: _Catalog, statement_counter: list[str]
) -> None:
    """One read and one hydration, independent of how many members there are.

    A spelling that hydrated per member -- `titles.get(title_id)` in a loop --
    answers byte for byte identically and issues one statement per film. Eight
    members are seeded so that spelling would report nine rather than two, and
    the counter is cleared after the seeding so it measures the request alone.

    The 404 arm is the other half: existence is resolved by the first read, so
    a franchise the catalog does not hold costs **one** statement and never
    reaches `list_by_ids`.
    """
    franchise = await catalog.collection("A Long Franchise", tmdb_id=98_200_005)
    for index in range(8):
        await catalog.member(franchise, f"Part {index}", release_date=date(1990 + index, 1, 1))

    statement_counter.clear()
    assert (await client.get(f"/collections/{franchise.id}")).status_code == 200
    served = [one for one in statement_counter if not one.lstrip().upper().startswith("BEGIN")]
    assert len(served) == 2, served

    statement_counter.clear()
    assert (await client.get(f"/collections/{new_id()}")).status_code == 404
    refused = [one for one in statement_counter if not one.lstrip().upper().startswith("BEGIN")]
    assert len(refused) == 1, refused
