"""`search_queries.surface` and `.tier` written by the shipped requests.

and every place a row deliberately does **not** appear.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.cli import _suggest
from usher.composition import build_pipeline
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.users import ensure_default_user
from usher.domain.enums import TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.eval.surfaces.suggest import tier_suggester
from usher.ports.search import SuggestTier
from usher.services.search import SearchQueryBuffer

SECRET_KEY = "0123456789abcdef0123456789abcdef"

#: Carried in `sort_name` by everything this file writes, so teardown deletes
#: exactly what this file created rather than emptying a table two other
#: committing files also use.
MARK = "Search Analytics Case"

#: The one title. Long enough that a single substitution near its end stays
#: well above `pg_trgm`'s 0.3 floor -- tier 2 gates on `name % probe` before it
#: computes an edit distance, so a short typo string would be a case about the
#: floor rather than about the writer.
TYPEABLE = "Marrowlight Quay"
#: A true prefix: seven characters, so the route's four-character minimum is
#: not what these arms are about, and no leading article, because tier 1 is
#: `lower(name) LIKE 'typed%'`.
TYPED_PREFIX = "marrowl"
#: The whole name with one character substituted. Tier 1 cannot match it --
#: `LIKE` is not an edit distance -- and tier 2's `levenshtein_less_equal <= 2`
#: can.
TYPED_TYPO = "marrowlight quan"
#: Three characters: below tier 1's four and above tier 2's one, which is the
#: one string that exercises the length bound on one tier and not the other.
TYPED_SHORT = "mar"


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # Both lanes off: `dependency_overrides` do not reach the lifespan, so
        # a push lane would build the real adapter against an unreachable host
        # and a worker lane would poll the database these cases count rows in.
        push_enabled=False,
        worker_enabled=False,
        # Stated rather than defaulted, and it agrees with what ships: the
        # fixture below turns it off, so the pair reads as two configurations
        # rather than one configuration and an absence.
        search_suggest_analytics=True,
    )


@pytest.fixture
def settings_without_the_writer(postgres_url: str) -> Settings:
    """The same deployment with `USHER_SEARCH_SUGGEST_ANALYTICS` turned off.

    A second `Settings` rather than a monkeypatched field: the switch is read
    once, in `composition.build_search_service`, and a case that reached in and
    moved it afterwards would be asserting about an object no deployment
    builds. Both fixtures state their side, so the day the default moves
    neither of them is a default wearing a name.
    """
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
        search_suggest_analytics=False,
    )


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # **Before the titles, and unscoped.** `search_queries.user_id` is `ON DELETE
        # RESTRICT` on purpose -- a household's search history is user state -- so a row
        # left behind here turns a neighbouring file's `DELETE FROM users WHERE name =
        # 'default'` into a foreign-key violation rather than into a slow test.
        await session.execute(text("DELETE FROM search_queries"))
        # **Before the titles, because it resolves through them.** Every root here
        # drives the real app, so a suggest that returns a hit runs
        # `VisibilityService.seen_ids` on the way out and commits one `enrich` job per
        # skeleton -- and the `catalog` fixture's title is a skeleton by construction.
        await session.execute(
            text(
                "DELETE FROM jobs WHERE key IN ("
                "  SELECT id::text FROM titles WHERE sort_name LIKE :pattern"
                ")"
            ),
            {"pattern": f"{MARK} %"},
        )
        await session.execute(
            text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def catalog(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[uuid.UUID]:
    await _wipe(sessions)
    title = Title(
        kind=TitleKind.MOVIE,
        name=TYPEABLE,
        sort_name=f"{MARK} marrowlight",
        year=2021,
        overview="A ferryman counts the lamps along a tidal quay.",
    )
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    yield title.id
    await _wipe(sessions)


@dataclass(frozen=True, slots=True)
class _Deployment:
    """The shipped app, and the buffer its keystroke rows are handed to.

    A keystroke submits its row and does not wait for it, so a case that reads
    the table has to flush the *same* buffer that request submitted to.
    """

    client: AsyncClient
    keystrokes: SearchQueryBuffer


async def _client(settings: Settings) -> AsyncIterator[_Deployment]:
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield _Deployment(client=connected, keystrokes=app.state.search_queries)


@pytest_asyncio.fixture
async def deployment(settings: Settings, catalog: uuid.UUID) -> AsyncIterator[_Deployment]:
    async for opened in _client(settings):
        yield opened


@pytest_asyncio.fixture
async def client(deployment: _Deployment) -> AsyncClient:
    return deployment.client


@pytest_asyncio.fixture
async def keystrokes(deployment: _Deployment) -> SearchQueryBuffer:
    """The drain, for a case that reads back a row a keystroke only submitted."""
    return deployment.keystrokes


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[dict[str, object]]:
    """Every `search_queries` row.

    joined to its household so a case can assert the id is `DefaultUserIdDep`'s and not
    an invented one.
    """
    async with sessions() as reader:
        result = await reader.execute(
            text(
                "SELECT q.query, q.mode, q.surface, q.tier, q.result_count, "
                "       q.user_id IS NOT NULL AS has_household, u.is_default "
                "FROM search_queries q JOIN users u ON u.id = q.user_id "
                "ORDER BY q.id"
            )
        )
        return [dict(row) for row in result.mappings()]


async def _count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as reader:
        return int((await reader.execute(text("SELECT count(*) FROM search_queries"))).scalar_one())


async def test_a_suggest_records_its_surface_and_the_tier_that_answered(
    client: AsyncClient,
    keystrokes: SearchQueryBuffer,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """PRD 10's amendment 2.

    through the shipped routes and read back from a session no request touched.
    """
    search = await client.get("/search", params={"q": "marrowlight"})
    assert search.status_code == 200, search.text
    assert len(search.json()["results"]) == 1, "the premise: the search answered"

    assert await _rows(sessions) == [
        {
            "query": "marrowlight",
            "mode": "full_text",
            "surface": "search",
            "tier": None,
            "result_count": 1,
            "has_household": True,
            "is_default": True,
        }
    ]

    fuzzy = await client.get("/search/suggest", params={"q": TYPED_TYPO, "tier": "fuzzy"})
    assert fuzzy.status_code == 200, fuzzy.text
    assert len(fuzzy.json()["results"]) == 1, "the premise: tier 2 answered the typo"

    prefix = await client.get("/search/suggest", params={"q": TYPED_PREFIX, "tier": "prefix"})
    assert prefix.status_code == 200, prefix.text
    assert len(prefix.json()["results"]) == 1, "the premise: tier 1 answered the prefix"

    # A keystroke hands its row over and does not wait for it, so a reader that
    # did not flush would be racing the drain rather than asserting on it.
    await keystrokes.flush()
    written = await _rows(sessions)
    assert [(row["surface"], row["tier"]) for row in written] == [
        ("search", None),
        ("suggest", "fuzzy"),
        ("suggest", "prefix"),
    ], written
    assert {row["mode"] for row in written} == {"full_text"}
    assert all(row["has_household"] and row["is_default"] for row in written), written
    assert [row["query"] for row in written] == ["marrowlight", TYPED_TYPO, TYPED_PREFIX]


async def test_the_tier_on_the_row_is_the_parameter_that_selected_the_index(
    client: AsyncClient,
    keystrokes: SearchQueryBuffer,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A response and a row can never disagree about which index answered.

    The route already makes this argument for its own echo -- *"`tier=tier` and
    not a tier the service chose"* -- and the row obeys the same rule. Asserted
    against the **response's** echo rather than against a literal, so a route
    that started deriving either from a hit count fails here rather than
    passing two independent literal assertions.

    `?tier=` omitted entirely on one arm, because the default is applied by
    FastAPI and a row written from a service-side default would be right by
    accident.
    """
    answered = await client.get("/search/suggest", params={"q": TYPED_PREFIX})
    assert answered.status_code == 200, answered.text
    assert answered.json()["tier"] == "prefix", "the premise: the default tier answered"

    await keystrokes.flush()
    (row,) = await _rows(sessions)
    assert row["tier"] == answered.json()["tier"]


async def test_a_prefix_below_its_tiers_minimum_writes_no_row_on_either_tier(
    client: AsyncClient,
    keystrokes: SearchQueryBuffer,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The short-`q` arm returns before the service, so there is no answered query to record.

    PRD 10 excludes *"a blank or whitespace-only query"* because a search box
    sends one between every character; the length bound is the same exclusion
    with a number on it, and it is stated in the route rather than inferred.

    **Four requests, because the route has three arms and the minimum is per
    tier**: blank on both tiers, and a three-character `q` that is below tier
    1's four and above tier 2's one -- so the same string must write nothing on
    one tier and a row on the other. Without that asymmetry a writer keyed on
    a single global minimum passes.
    """
    for tier in ("prefix", "fuzzy"):
        blank = await client.get("/search/suggest", params={"q": "   ", "tier": tier})
        assert blank.status_code == 200, blank.text
        assert blank.json()["results"] == []

    short_on_tier_one = await client.get(
        "/search/suggest", params={"q": TYPED_SHORT, "tier": "prefix"}
    )
    assert short_on_tier_one.status_code == 200, short_on_tier_one.text
    # Flushed before every absence too: an unflushed buffer makes *every* count
    # zero, which is the shape that would satisfy this case for the wrong
    # reason.
    await keystrokes.flush()
    assert await _count(sessions) == 0

    short_on_tier_two = await client.get(
        "/search/suggest", params={"q": TYPED_SHORT, "tier": "fuzzy"}
    )
    assert short_on_tier_two.status_code == 200, short_on_tier_two.text
    await keystrokes.flush()
    assert [(row["surface"], row["tier"], row["query"]) for row in await _rows(sessions)] == [
        ("suggest", "fuzzy", TYPED_SHORT)
    ], "the control: the same string is above tier 2's minimum and is recorded"


async def test_the_switch_is_whole_or_nothing_and_leaves_the_search_row_alone(
    settings_without_the_writer: Settings,
    catalog: uuid.UUID,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`USHER_SEARCH_SUGGEST_ANALYTICS=false`, and what it does not switch off."""
    async for deployment in _client(settings_without_the_writer):
        client = deployment.client
        for tier, probe in (("prefix", TYPED_PREFIX), ("fuzzy", TYPED_TYPO)):
            response = await client.get("/search/suggest", params={"q": probe, "tier": tier})
            assert response.status_code == 200, response.text
            assert len(response.json()["results"]) == 1, "the premise: the box answered"
        # Flushed, or this absence would be the buffer holding rows rather than
        # the switch refusing them.
        await deployment.keystrokes.flush()
        assert await _count(sessions) == 0

        assert (await client.get("/search", params={"q": "marrowlight"})).status_code == 200
        assert [(row["surface"], row["tier"]) for row in await _rows(sessions)] == [
            ("search", None)
        ], "the control: the search surface is untouched by the suggest switch"


async def test_a_suggest_with_no_household_writes_no_row_and_the_eval_harness_is_that_caller(
    settings: Settings, catalog: uuid.UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """PRD 10's *"a search with no household"* exclusion.

    and the caller it is now load- bearing for.
    """
    engine = build_engine(settings.database_url.get_secret_value())
    lines: list[str] = []
    sink = logger.add(lines.append, level="TRACE", serialize=True)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            ask = tier_suggester(session, settings, "fuzzy")
            assert len(await ask(TYPED_TYPO, 5)) == 1, "the premise: the eval probe was answered"
            await session.commit()
        assert await _count(sessions) == 0
        assert not _refusals(lines), lines

        async with factory() as session:
            household = await ensure_default_user(session)
            control = build_pipeline(session, settings)
            assert len(await control.search.suggest(TYPED_TYPO, tier=SuggestTier.FUZZY)) == 1
            assert await _count(sessions) == 0, (
                "the premise: even here, a call with no household writes nothing"
            )
            assert not _refusals(lines), lines
            await control.search.suggest(TYPED_TYPO, tier=SuggestTier.FUZZY, user_id=household)
            await session.commit()

        # **The sink's own positive control, and it is why the two assertions above are
        # evidence rather than an empty list.** A household id naming no `users` row is
        # refused by `fk_search_queries_user_id_users`, absorbed by `_write_row`, and
        # logged -- which is *exactly* what the planted guard produces per eval probe.
        async with factory() as session:
            stranger = build_pipeline(session, settings)
            await stranger.search.suggest(TYPED_TYPO, tier=SuggestTier.FUZZY, user_id=new_id())
            await session.rollback()
        assert _refusals(lines), (
            "the control: this sink can see a refused analytics row, and the two "
            "assertions above are therefore about the writer rather than about logging"
        )
    finally:
        logger.remove(sink)
        await engine.dispose()

    assert [(row["surface"], row["tier"]) for row in await _rows(sessions)] == [
        ("suggest", "fuzzy")
    ], "the control: this very session writes a row once a household is named"


def _refusals(lines: list[str]) -> list[str]:
    """Every log line `_write_row` emitted for a row the store refused."""
    return [one for one in lines if "analytics row was refused" in one]


async def test_usher_suggest_writes_the_row_and_commits_it(
    settings: Settings,
    catalog: uuid.UUID,
    sessions: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI root, and the reason the commit is in the service.

    `cli._session_for` yields a session and disposes the engine **without ever
    committing**, so a suggest writer that inherited the caller's commit
    boundary would be correct on the route and silently lose every CLI row --
    which is exactly why `_record_search` commits in the service rather than
    leaving it to the caller. The row is read back through a **different
    engine**, after the command's own engine is disposed, so nothing about this
    assertion can be satisfied by an uncommitted write.

    `usher suggest` also has to resolve a household it never had, the same way
    `usher search` does (`ensure_default_user`), because
    `search_queries.user_id` is `NOT NULL` behind a real foreign key.

    Fails: the row left uncommitted; the household not resolved (the row is
    then not written at all, which this case cannot tell from a lost commit --
    hence the `surface`/`tier` assertion rather than a bare count).
    """
    await _suggest(settings, prefix=TYPED_TYPO, limit=5, tier="fuzzy")
    assert TYPEABLE in capsys.readouterr().out, "the premise: the command answered"

    assert [(row["surface"], row["tier"], row["query"]) for row in await _rows(sessions)] == [
        ("suggest", "fuzzy", TYPED_TYPO)
    ]
