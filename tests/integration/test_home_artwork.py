"""`RowCard.artwork` end to end -- a real `GET /home` over real Postgres.

**What only this level can see.** `tests/unit/test_rows_artwork.py` drives
`BaseRow._artwork` against `FakeImageRepository`, which answers out of a dict;
what it cannot exercise is the wiring -- `api/deps.py:get_image_repository`
building a `PostgresImageRepository` on the request's session, that repository
reaching `RowContext.images`, and `ImageRepository.primary_for_titles` filtering
on `kind` in SQL rather than in Python. `images=None` in `get_row_context`
type-checks, constructs (the context is a frozen dataclass with no runtime
validation) and fails as an `AttributeError` inside `hydrate` on the first
request; only a request that really builds a row sees it.

**Both kinds are seeded on the same title, deliberately.** A `portrait` shelf
asking for a poster and getting one is satisfied by a repository that ignores
`kind` entirely, as long as the title has only posters. With a backdrop stored
beside it, "ignores `kind`" and "reads the poster" are two different ids, and
the read order (`is_primary DESC, id`) decides which the sloppy implementation
would answer.

**The case commits for real and cleans up after itself.** A route goes through
`get_session`, which is the request's commit boundary, so a screen composed
from a route writes durably against the session-scoped container -- unlike
every rolled-back test in this suite. `images` and `media_items` go with their
owners' `ON DELETE CASCADE`; `titles`, `sources` and -- since issue #73 made
this a read route that promotes what it draws -- the `enrich` `jobs` are
deleted by id. The `users` row is a singleton reached by
`ON CONFLICT (name) DO NOTHING` and is left standing, as `test_rows_route.py`
leaves it.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import ImageKind, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.image import Image
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Household:
    """The ids a case needs to name a card and the image it must carry."""

    derived: uuid.UUID
    bare: uuid.UUID
    poster: uuid.UUID
    backdrop: uuid.UUID


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The route commits from a session of its own, so seeding through the
    suite's shared transaction would write rows the request's connection
    cannot see.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def household(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[_Household]:
    """Two owned titles that arrived today: one with artwork, one without.

    `RecentlyAddedProvider` is the one provider that fires on a household with
    no watch state at all, so it is the cheapest way to make the composer
    actually build a shelf -- and it is `portrait`, which is the hint that asks
    for a poster.

    Two titles rather than one, because the `null` arm has to be asserted on a
    shelf where it is not the only answer: a screen on which every card's
    `artwork` is `null` is also what a route that never read anything produces.
    """
    source = Source(
        kind=SourceKind.EMBY,
        name=f"artwork-probe-{new_id()}",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    derived = Title(
        id=new_id(),
        kind=TitleKind.MOVIE,
        name="A Film Somebody Derived",
        sort_name="a film somebody derived",
        year=2024,
    )
    bare = Title(
        id=new_id(),
        kind=TitleKind.MOVIE,
        name="A Film Nobody Derived",
        sort_name="a film nobody derived",
        year=2024,
    )
    # The backdrop is flagged and the poster is not, so an implementation
    # ignoring `kind` and taking the title's first image in read order
    # (`is_primary DESC, id`) answers the backdrop -- which is the wrong id
    # rather than no id, and is what makes the assertion below a test of the
    # filter rather than of the seeding.
    poster = Image(
        id=new_id(),
        title_id=derived.id,
        kind=ImageKind.POSTER,
        provider="tmdb",
        provider_path=f"/poster-{new_id()}.jpg",
        is_primary=False,
    )
    backdrop = Image(
        id=new_id(),
        title_id=derived.id,
        kind=ImageKind.BACKDROP,
        provider="tmdb",
        provider_path=f"/backdrop-{new_id()}.jpg",
        is_primary=True,
    )

    async with sessions() as session:
        await PostgresSourceRepository(session).add(source)
        titles = PostgresTitleRepository(session)
        await titles.add(derived)
        await titles.add(bare)
        await PostgresMediaItemRepository(session).upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id=f"artwork-probe-{one.id}",
                    title_id=one.id,
                    episode_id=None,
                    container="mkv",
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
                for one in (derived, bare)
            ]
        )
        await PostgresImageRepository(session).replace_for_titles(
            [derived.id, bare.id], [poster, backdrop]
        )
        await session.commit()

    try:
        yield _Household(derived=derived.id, bare=bare.id, poster=poster.id, backdrop=backdrop.id)
    finally:
        async with sessions() as session:
            # `images` and `media_items` cascade from the rows below them;
            # `titles` and `sources` do not.
            #
            # Neither does `jobs`: `GET /home` promotes every skeleton it draws
            # (issue #73) and `get_session` commits at the end of a successful
            # request, so this file's reads write `enrich` rows. **Before the
            # titles** -- the job's `key` is the title's id as text, so once
            # the title row is gone there is nothing left to identify them by.
            await session.execute(
                text(
                    "DELETE FROM jobs WHERE kind = 'enrich' AND key IN "
                    "(SELECT id::text FROM titles WHERE id = ANY(:ids))"
                ),
                {"ids": [derived.id, bare.id]},
            )
            await session.execute(
                text("DELETE FROM titles WHERE id = ANY(:ids)"), {"ids": [derived.id, bare.id]}
            )
            await session.execute(text("DELETE FROM sources WHERE id = :id"), {"id": source.id})
            await session.commit()


@pytest_asyncio.fixture
async def client(postgres_url: str) -> AsyncIterator[AsyncClient]:
    """The real app with no dependency overrides at all.

    That is the whole point of this file: every unit case in
    `test_api_home.py` replaces `get_row_context`, so nothing there has ever
    built the real one, and `images` is the newest of its thirteen fields.
    """
    app = create_app(
        Settings(
            database_url=postgres_url,
            secret_key="0" * 32,
            # `dependency_overrides` do not reach the lifespan, so a push lane
            # would build a real `EmbyAdapter` against `https://emby.invalid`
            # and open a socket; a worker lane would poll this database.
            push_enabled=False,
            worker_enabled=False,
        )
    )
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_a_card_carries_the_poster_its_portrait_row_asked_for(
    client: AsyncClient, household: _Household
) -> None:
    """The wiring, the SQL and the `kind` filter, in one request.

    Kills `images=None` in `get_row_context` (an `AttributeError` inside
    `hydrate`, which no unit case in `test_api_home.py` can see because every
    one of them overrides that dependency), a `primary_for_titles` that ignores
    `kind` in SQL, and the `display_hint` -> `ImageKind` mapping swapped.

    The premise is stated because with `poster == backdrop` -- or with either
    absent -- the assertion would pass against an implementation answering a
    constant.
    """
    body = (await client.get("/home")).json()
    rows = {row["slug"]: row for row in body["rows"]}

    assert household.poster != household.backdrop, "the premise: two kinds, two rows"
    assert "recently-added" in rows, "nothing built, so there is no card to read artwork off"
    assert rows["recently-added"]["display_hint"] == "portrait"

    cards = {card["title_id"]: card for card in rows["recently-added"]["cards"]}

    assert cards[str(household.derived)]["artwork"] == str(household.poster)


async def test_a_card_for_a_title_with_no_artwork_carries_null_on_the_same_shelf(
    client: AsyncClient, household: _Household
) -> None:
    """The other arm, beside a card that has one -- so `null` is a fact about
    the title rather than about the whole read.

    A catalog that has been synced and never derived holds no `images` row at
    all, which is the state this arm is the ordinary answer for. Asserting it
    on a shelf whose neighbour carries an id is what stops the case passing
    against a route that reads no artwork anywhere.
    """
    body = (await client.get("/home")).json()
    rows = {row["slug"]: row for row in body["rows"]}
    cards = {card["title_id"]: card for card in rows["recently-added"]["cards"]}

    assert cards[str(household.derived)]["artwork"] is not None, (
        "the premise: the same shelf really did render an artwork id"
    )
    assert "artwork" in cards[str(household.bare)], "the key is present and null, not absent"
    assert cards[str(household.bare)]["artwork"] is None
