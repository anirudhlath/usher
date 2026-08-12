"""The review queue on the wire, against a real schema.

**What only this level can see.** `tests/unit/test_api_unmatched.py` drives
both routes over `FakeMediaItemRepository`, whose keyset is a tuple comparison
in Python -- and a NULL cannot poison a comparison in Python, so the defect
this route exists to avoid is not expressible there. Against Postgres it is:
`((added_at IS NOT NULL), added_at, id) > (...)` evaluates to **NULL rather
than false** at an unkeyed boundary, so a walk drops the whole undated tail
while every page it served looks full (ADR-0034, corrected by measurement).
The undated items are precisely the population an operator is reviewing -- a
source that could not date a file is a source that told us least about it --
so the headline case below puts a NULL-dated item *on the page boundary*.

**This module commits for real, so it cleans up after itself.**
`get_session` commits every request even when the handler only read, and
CLAUDE.md records what leaving rows behind did to four tests in three other
files, each of which passed in isolation. `media_items` cascades from
`sources`, so deleting this file's own sources takes its items with them; the
titles a resolve case needs are deleted by their own marker.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.contract.media_item_repository_contract import item
from usher.api.app import create_app
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# Every source and every title this file writes carries it, so teardown deletes
# exactly what this file created rather than emptying a table another
# committing file is also using.
MARK = "Unmatched Route Case"


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
        # `media_items` is `ON DELETE CASCADE` from `sources`, so the items go
        # with them. `titles` is separate and is deleted by its own marker --
        # a resolved item holds a `title_id`, so the sources have to go first.
        await session.execute(
            text("DELETE FROM sources WHERE name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
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


async def _given_source(sessions: async_sessionmaker[AsyncSession], name: str) -> uuid.UUID:
    source = Source(
        kind=SourceKind.EMBY,
        name=f"{MARK} {name}",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    async with sessions() as session:
        await PostgresSourceRepository(session).add(source)
        await session.commit()
    return source.id


async def _given_items(
    sessions: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    dates: dict[str, datetime | None],
) -> dict[str, uuid.UUID]:
    """Seed one unmatched item per entry and answer their ids by external id."""
    async with sessions() as session:
        repository = PostgresMediaItemRepository(session)
        await repository.upsert_many(
            [
                item(source_id, external_id, added_at=added_at)
                for external_id, added_at in dates.items()
            ]
        )
        await session.commit()
        stored = {}
        for external_id in dates:
            row = await repository.get_by_external_id(source_id, external_id)
            assert row is not None
            stored[external_id] = row.id
    return stored


async def _given_title(sessions: async_sessionmaker[AsyncSession], name: str) -> uuid.UUID:
    title = Title(kind=TitleKind.SERIES, name=f"{MARK} {name}", sort_name=f"{MARK} {name}")
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title.id


async def _given_episode(
    sessions: async_sessionmaker[AsyncSession], title_id: uuid.UUID
) -> uuid.UUID:
    """One real episode of `title_id`, which needs a real season: both foreign
    keys are `NOT NULL` and `media_items.episode_id` is itself one."""
    season = Season(title_id=title_id, season_number=1)
    episode = Episode(
        title_id=title_id, season_id=season.id, season_number=1, episode_number=1, name="Pilot"
    )
    async with sessions() as session:
        repository = PostgresEpisodeRepository(session)
        await repository.upsert_seasons([season])
        await repository.upsert_episodes([episode])
        await session.commit()
    return episode.id


async def _walk(client: AsyncClient, *, limit: int) -> tuple[list[str], int]:
    """Every page of the queue, followed through the opaque cursor.

    Answers the external ids in the order served and how many requests it
    took, because "every item exactly once" is also what a single page holds
    when the cursor is never followed.
    """
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = {"limit": str(limit)} | ({"cursor": cursor} if cursor else {})
        response = await client.get("/admin/unmatched", params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages += 1
        seen.extend(entry["external_id"] for entry in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen, pages
        assert pages < 20, "the walk did not terminate"


async def test_paging_the_queue_with_a_cursor_returns_every_item_exactly_once_including_the_undated_ones(  # noqa: E501
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The case ADR-0034's corrected predicate exists for.

    Three dated items and four undated ones at `limit=3` puts page 2's
    boundary **inside the undated group**, which is the position the refuted
    row-comparison spelling resumes from by answering NULL -- dropping every
    remaining undated row while page 2 still looked full. Page 1's boundary is
    a dated item, so both arms of the predicate are walked in one case.

    `pages > 1` is asserted because a route that ignored `limit` and served
    everything at once satisfies the set assertion perfectly.
    """
    source_id = await _given_source(sessions, "queue")
    dated = {
        f"dated-{index}": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        for index in range(3)
    }
    undated: dict[str, datetime | None] = {f"undated-{index}": None for index in range(4)}
    seeded = await _given_items(sessions, source_id, {**dated, **undated})

    seen, pages = await _walk(client, limit=3)

    assert pages > 1, "one page proves nothing about resuming from a cursor"
    assert sorted(seen) == sorted(seeded), seen
    assert len(seen) == len(set(seen)), f"an item was served twice: {seen}"


async def test_a_page_that_exactly_exhausts_the_queue_carries_no_next_cursor(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The off-by-one, against the statement that really runs.

    Six items at `limit=3` makes the second page full **and** final, which is
    the only arrangement that can see it -- the seven-item walk above stays
    green under the naive "the page is full so there is more" spelling because
    `7 % 3 != 0`.
    """
    source_id = await _given_source(sessions, "exhaustion")
    await _given_items(
        sessions,
        source_id,
        {f"orphan-{index}": datetime(2026, 1, 1, tzinfo=UTC) for index in range(6)},
    )

    first = (await client.get("/admin/unmatched", params={"limit": "3"})).json()
    assert first["next_cursor"] is not None
    second = (
        await client.get("/admin/unmatched", params={"limit": "3", "cursor": first["next_cursor"]})
    ).json()

    assert len(second["items"]) == 3, "the premise: the last page is exactly full"
    assert second["next_cursor"] is None, "and it is the last one"


async def test_resolving_an_item_commits_the_row_and_the_queue_no_longer_holds_it(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Durable, not a flush the response outlives: read back on a second
    session, after the request's own transaction is gone. `get_session` is the
    request's commit boundary and this is the one write in this task."""
    source_id = await _given_source(sessions, "resolve")
    seeded = await _given_items(sessions, source_id, {"orphan": None})
    title = await _given_title(sessions, "A Resolved Film")

    response = await client.post(
        f"/admin/unmatched/{seeded['orphan']}/resolve", json={"title_id": str(title)}
    )

    assert response.status_code == 200, response.text
    async with sessions() as session:
        stored = await PostgresMediaItemRepository(session).get_by_external_id(source_id, "orphan")
    assert stored is not None
    assert (stored.title_id, stored.episode_id) == (title, None)
    assert (await client.get("/admin/unmatched")).json()["items"] == []


async def test_an_unknown_title_is_refused_by_the_route_rather_than_by_a_foreign_key(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """What only a schema with real foreign keys can say.

    Against `FakeMediaItemRepository` there are no foreign keys at all, so its
    unit twin cannot tell "the route refused this" from "there was nothing
    here to refuse". Here `media_items.title_id` really does reference
    `titles`, so an unchecked write is an `IntegrityError` --
    `PostgresMediaItemRepository` translates it to `RepositoryConflict`, which
    no handler catches, which is a **500** for a value a client typed. The
    route's own read is what makes it a 422 instead.
    """
    source_id = await _given_source(sessions, "ghost title")
    seeded = await _given_items(sessions, source_id, {"orphan": None})

    response = await client.post(
        f"/admin/unmatched/{seeded['orphan']}/resolve", json={"title_id": str(new_id())}
    )

    assert response.status_code == 422, response.text
    async with sessions() as session:
        stored = await PostgresMediaItemRepository(session).get_by_external_id(source_id, "orphan")
    assert stored is not None
    assert stored.title_id is None


async def test_nothing_in_the_schema_stops_an_episode_of_another_title_so_the_route_must(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The premise for the check, measured rather than asserted about.

    `media_items` carries `title_id` and `episode_id` as two independent
    foreign keys with **no CHECK tying them together** -- deliberately, since
    an episode's row is supposed to hold its series' title beside its own
    episode. So the first half of this case writes the mismatched pair through
    the port directly and watches Postgres accept it: nothing downstream
    detects a file pointed at an episode of another series. The second half is
    the same pair through the route, refused, with the row read back unchanged.
    """
    source_id = await _given_source(sessions, "episode mismatch")
    seeded = await _given_items(sessions, source_id, {"direct": None, "routed": None})
    wanted = await _given_title(sessions, "The Series An Operator Meant")
    other = await _given_title(sessions, "Some Other Series")
    stray = await _given_episode(sessions, other)

    async with sessions() as session:
        repository = PostgresMediaItemRepository(session)
        written = await repository.attach_title(seeded["direct"], title_id=wanted, episode_id=stray)
        await session.commit()
    assert written is True, "the premise: the database accepts an episode of another title"

    response = await client.post(
        f"/admin/unmatched/{seeded['routed']}/resolve",
        json={"title_id": str(wanted), "episode_id": str(stray)},
    )

    assert response.status_code == 422, response.text
    async with sessions() as session:
        stored = await PostgresMediaItemRepository(session).get_by_external_id(source_id, "routed")
    assert stored is not None
    assert (stored.title_id, stored.episode_id) == (None, None)
