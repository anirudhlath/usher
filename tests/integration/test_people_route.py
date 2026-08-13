"""`GET /people/{id}` through a real request against a real schema.

**What only this level can see.** `tests/unit/test_api_people.py` drives the
route over the three port fakes, so the grouping, the ordering and the absent
`groups` key are all covered there. What is left is the wiring and the SQL:
that `create_app()`'s **un-overridden** dependency graph resolves all three
repositories onto one request-scoped session, that `PersonRepository.get`'s
statement really is scoped to the id it was handed, that `list_for_person`
answers real rows, and what the whole answer costs in statements.

The cost is the half nothing else can measure. This route is two reads and a
hydration by construction; a spelling that hydrated per credit would answer
identically and issue one statement per title, which is the round-trip-per-item
shape `list_by_ids` exists to delete.

**This module commits for real, so it cleans up after itself.** `get_session`
commits every request. `credits` cascades from both `people` and `titles`, and
`title_search_names` cascades from `titles`; nothing here writes `media_items`,
`watch_states` or `jobs`, because this route reads nothing household-scoped and
promotes nothing.
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
from usher.db.repositories.people import PostgresCreditRepository, PostgresPersonRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.people import Credit, CreditKind, CreditSource, Person
from usher.domain.title import Title

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# Every row this file writes carries it, so teardown deletes exactly what this
# file created rather than emptying a table another committing file also uses.
MARK = "People Route Case"


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
    route reads from its own session in its own transaction, so a test seeding
    through a shared rolled-back one would hand the app rows it cannot see."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # `credits` and `title_search_names` both cascade from `titles`, so
        # the titles go last and take this file's credit rows with them. The
        # people are deleted by name for the same reason the titles are by
        # sort name: a blanket `DELETE FROM people` would reach another
        # committing file's rows.
        await session.execute(
            text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
        await session.execute(
            text("DELETE FROM people WHERE sort_name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@pytest_asyncio.fixture
async def client(settings: Settings, clean: None) -> AsyncIterator[AsyncClient]:
    """**No `dependency_overrides` at all**, which is the point of this file:
    `get_person_repository`, `get_credit_repository` and `get_title_repository`
    are resolved through FastAPI's own machinery onto one `get_session`."""
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement SQLAlchemy issues, captured off
    `before_cursor_execute` rather than transcribed -- a hand-copied lookalike
    of a query drifts from the repository and then reads like coverage."""
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


async def _given_person(
    sessions: async_sessionmaker[AsyncSession], name: str, *, tmdb_id: int
) -> Person:
    person = Person(
        tmdb_id=tmdb_id, name=name, sort_name=f"{MARK} {name}", known_for_department="Directing"
    )
    async with sessions() as session:
        await PostgresPersonRepository(session).upsert_many([person])
        await session.commit()
    # The id the catalog kept, which is not necessarily the one minted above:
    # `upsert_many` is keyed on `tmdb_id`, so a re-run reuses the stored row.
    async with sessions() as session:
        stored = await PostgresPersonRepository(session).get(
            (await PostgresPersonRepository(session).resolve_tmdb_ids([tmdb_id]))[tmdb_id]
        )
    assert stored is not None
    return stored


async def _given_title(
    sessions: async_sessionmaker[AsyncSession], name: str, *, year: int | None
) -> Title:
    title = Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=f"{MARK} {name}",
        year=year,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title


async def _given_credits(sessions: async_sessionmaker[AsyncSession], *rows: Credit) -> None:
    async with sessions() as session:
        await PostgresCreditRepository(session).replace_for_titles(
            list(dict.fromkeys(row.title_id for row in rows)), list(rows), credit_names={}
        )
        await session.commit()


async def test_a_filmography_is_grouped_and_ordered_off_real_rows(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The whole answer, assembled by the shipped graph with nothing
    overridden.

    The ordering premise is asserted here too rather than only in the unit
    file: `Title.id` is a UUIDv7 minted at validation time, so seeding
    oldest-first makes id order agree with the *wrong* answer -- and Postgres'
    `list_by_ids` is a bare `IN (...)` that promises no order at all, so an
    implementation that rendered what it was handed would be reading physical
    order off a real heap rather than a dict's insertion order.
    """
    person = await _given_person(sessions, "An Invented Filmmaker", tmdb_id=93_200_001)
    oldest = await _given_title(sessions, "The Oldest", year=1974)
    newest = await _given_title(sessions, "The Newest", year=2019)
    undated = await _given_title(sessions, "The Undated", year=None)
    assert oldest.id < newest.id < undated.id, (
        "the fixture must make id order favour the wrong answer as well"
    )
    await _given_credits(
        sessions,
        Credit(
            person_id=person.id,
            title_id=oldest.id,
            kind=CreditKind.CAST,
            source=CreditSource.TMDB,
            billing_order=0,
        ),
        Credit(
            person_id=person.id,
            title_id=newest.id,
            kind=CreditKind.CAST,
            source=CreditSource.TMDB,
            billing_order=1,
        ),
        Credit(
            person_id=person.id,
            title_id=undated.id,
            kind=CreditKind.CAST,
            source=CreditSource.TMDB,
            billing_order=2,
        ),
        Credit(
            person_id=person.id,
            title_id=newest.id,
            kind=CreditKind.CREW,
            source=CreditSource.TMDB,
            job="Director",
        ),
    )

    response = await client.get(f"/people/{person.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "An Invented Filmmaker"
    assert body["known_for_department"] == "Directing"
    assert [group["role"] for group in body["groups"]] == ["cast", "Director"]
    assert [one["title_id"] for one in body["groups"][0]["titles"]] == [
        str(newest.id),
        str(oldest.id),
        str(undated.id),
    ]
    assert [one["title_id"] for one in body["groups"][1]["titles"]] == [str(newest.id)]


async def test_the_read_is_scoped_to_the_person_asked_for(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The wrong implementation this kills at the request level: a `get` or a
    `list_for_person` whose `WHERE` lost its predicate.

    Both are populated, correctly shaped and about the wrong person, and the
    second one is the sharper of the two -- it renders somebody else's
    filmography under the requested person's name, which is the front matter's
    failure mode with two people's names on it. The other person is seeded with
    the *larger* filmography, so an unscoped read answers with more rather than
    with less.
    """
    asked = await _given_person(sessions, "The One Asked For", tmdb_id=93_200_002)
    other = await _given_person(sessions, "Somebody Else", tmdb_id=93_200_003)
    theirs = await _given_title(sessions, "Their Film", year=2001)
    mine = await _given_title(sessions, "My Film", year=2002)
    second = await _given_title(sessions, "Their Other Film", year=2003)
    await _given_credits(
        sessions,
        Credit(
            person_id=asked.id, title_id=mine.id, kind=CreditKind.CAST, source=CreditSource.TMDB
        ),
        Credit(
            person_id=other.id, title_id=theirs.id, kind=CreditKind.CAST, source=CreditSource.TMDB
        ),
        Credit(
            person_id=other.id, title_id=second.id, kind=CreditKind.CAST, source=CreditSource.TMDB
        ),
    )

    body = (await client.get(f"/people/{asked.id}")).json()
    assert body["name"] == "The One Asked For"
    assert [one["title_id"] for one in body["groups"][0]["titles"]] == [str(mine.id)]


async def test_an_unknown_person_is_a_404_from_the_real_graph(client: AsyncClient) -> None:
    """The 404 through the un-overridden wiring, so it is the row that is
    missing rather than a fake that was never seeded."""
    person_id = new_id()
    response = await client.get(f"/people/{person_id}")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == f"/people/{person_id}"


async def test_the_whole_answer_costs_three_statements(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    """Two reads and a hydration, independent of how many credits there are.

    **This is the property no other level can see.** A spelling that hydrated
    per credit -- `titles.get(credit.title_id)` in a loop -- answers byte for
    byte identically and issues one statement per title, which is the
    round-trip-per-item shape `list_by_ids` was introduced to delete. Ten
    titles are seeded so the per-credit spelling would report twelve rather
    than three, and the counter is cleared after the seeding so it measures the
    request alone.

    The unknown-person arm is the other half: existence is resolved first, so a
    404 costs **one** statement rather than three. The response body of a route
    that read and discarded is identical.
    """
    person = await _given_person(sessions, "A Working Actor", tmdb_id=93_200_004)
    rows = []
    for index in range(10):
        film = await _given_title(sessions, f"Film {index}", year=1990 + index)
        rows.append(
            Credit(
                person_id=person.id,
                title_id=film.id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                billing_order=index,
                tmdb_credit_id=f"an-invented-credit-{index}",
            )
        )
    await _given_credits(sessions, *rows)

    statement_counter.clear()
    assert (await client.get(f"/people/{person.id}")).status_code == 200
    served = [one for one in statement_counter if not one.lstrip().upper().startswith(("BEGIN",))]
    assert len(served) == 3, served

    statement_counter.clear()
    assert (await client.get(f"/people/{new_id()}")).status_code == 404
    refused = [one for one in statement_counter if not one.lstrip().upper().startswith(("BEGIN",))]
    assert len(refused) == 1, refused
