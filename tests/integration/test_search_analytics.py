"""`search_queries.surface` and `.tier` written by the shipped requests, and
every place a row deliberately does **not** appear.

`m10c` (J1) landed the two columns and left `surface` as the literal `'search'`
in `PostgresSearchQueryRepository`'s INSERT, because the column is `NOT NULL`
with no `server_default` and the shipped writer could not omit it. This file is
what says the suggest writer landed: a `search` row and a `suggest` row are now
told apart by the table itself, and the tier that answered is on the row rather
than only on the wire.

**Why a file of its own rather than more cases in
`tests/integration/test_search_route.py`.** That file is about `GET /search`'s
*answer* -- ranking, weight classes, the DTO -- and carries a three-title
catalog shaped for an ordering premise. This one is about a **write**, needs
one title and two households' worth of nothing, and has to drive three roots
(the two routes, `usher suggest`, and the eval harness) that file has no
business knowing about. Its analytics case stays where it is and is the control
that `GET /search` still writes exactly one row.

**Every absence here is a decision with an argument, and each is asserted with
a positive control beside it**, because "no rows" is also what an unwired
fixture, a broken seed and a 500 all produce:

- a `q` below its tier's `min_query_length` -- the route returns before the
  service, so there is no answered query to record (PRD 10's *"A blank or
  whitespace-only query"* exclusion, extended to the length bound);
- a `suggest` call carrying **no household** -- `search_queries.user_id` is
  `NOT NULL` behind `ON DELETE RESTRICT`, so PRD 10's *"A search with no
  household"* exclusion applies unchanged. This is the eval harness's whole
  path: `usher.eval.surfaces.suggest` drives `pipeline.search.suggest` once per
  probe and `usher eval suggest --full` drives thousands, so an analytics
  writer that fired for it would write evaluation traffic into the table as
  though a household had typed it;
- the whole writer switched off by `USHER_SEARCH_SUGGEST_ANALYTICS=false`,
  which is **whole or nothing** and deliberately not a sample rate.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import uuid
from collections.abc import AsyncIterator

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
        # **Explicit, and the opposite of the shipped default.** The writer is
        # `false` out of the box because on tier 1 the row costs more than the
        # request; a deployment that wants the data turns it on, and that is
        # the deployment every case here is about. Stated rather than defaulted
        # so a reader is not left thinking these rows appear by themselves --
        # and so the case below that turns it *off* is a real second
        # configuration rather than the default wearing a name.
        search_suggest_analytics=True,
    )


@pytest.fixture
def settings_without_the_writer(postgres_url: str) -> Settings:
    """The same deployment with `USHER_SEARCH_SUGGEST_ANALYTICS` off -- which
    is the **shipped** default, spelled out rather than omitted.

    A second `Settings` rather than a monkeypatched field: the switch is read
    once, in `composition.build_search_service`, and a case that reached in and
    moved it afterwards would be asserting about an object no deployment
    builds. Spelled rather than defaulted so the pair above and below reads as
    two configurations rather than one configuration and an absence -- and so
    the day the default moves again, both fixtures say which side they are on.
    """
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
        search_suggest_analytics=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one: every
    root here commits in its own transaction, so a reader inside the suite's
    single transaction could not see what it wrote."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # **Before the titles, and unscoped.** `search_queries.user_id` is
        # `ON DELETE RESTRICT` on purpose -- a household's search history is
        # user state -- so a row left behind here turns a neighbouring file's
        # `DELETE FROM users WHERE name = 'default'` into a foreign-key
        # violation rather than into a slow test. This table has no column
        # this file could mark.
        await session.execute(text("DELETE FROM search_queries"))
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


async def _client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest_asyncio.fixture
async def client(settings: Settings, catalog: uuid.UUID) -> AsyncIterator[AsyncClient]:
    async for connected in _client(settings):
        yield connected


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[dict[str, object]]:
    """Every `search_queries` row, joined to its household so a case can assert
    the id is `DefaultUserIdDep`'s and not an invented one."""
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
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """PRD 10's amendment 2, through the shipped routes and read back from a
    session no request touched.

    **The positive control fires first and is not decoration.** The same case
    drives `GET /search` and asserts the row it writes carries
    `surface = 'search'` with `tier IS NULL` -- which is `m10c`'s backfill
    semantics arriving on a *new* row rather than an old one, and which is the
    only thing that distinguishes "the suggest writer works" from "every row
    this deployment writes says `suggest`".

    **Both tiers, because one tier passing is also what a writer hard-coding
    `'fuzzy'` produces.** The two arms use different probes for a reason that
    is not symmetry: tier 1 cannot match `TYPED_TYPO` and tier 2 answers a
    plain prefix too, so a single probe across both would not tell a route that
    honours `?tier=` from one that does not.

    `mode` is `full_text` on a suggest row: both tiers are btree/GIN reads with
    no embed and no fusion, which is what that member already means. **Every
    mode-split panel now has to filter on `surface`**, which is why the row
    carries one.

    Fails at `m10c`: the route wrote nothing at all, stated in its own
    docstring.
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
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
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

    (row,) = await _rows(sessions)
    assert row["tier"] == answered.json()["tier"]


async def test_a_prefix_below_its_tiers_minimum_writes_no_row_on_either_tier(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The short-`q` arm returns before the service, so there is no answered
    query to record.

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
    assert await _count(sessions) == 0

    short_on_tier_two = await client.get(
        "/search/suggest", params={"q": TYPED_SHORT, "tier": "fuzzy"}
    )
    assert short_on_tier_two.status_code == 200, short_on_tier_two.text
    assert [(row["surface"], row["tier"], row["query"]) for row in await _rows(sessions)] == [
        ("suggest", "fuzzy", TYPED_SHORT)
    ], "the control: the same string is above tier 2's minimum and is recorded"


async def test_the_switch_is_whole_or_nothing_and_leaves_the_search_row_alone(
    settings_without_the_writer: Settings,
    catalog: uuid.UUID,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`USHER_SEARCH_SUGGEST_ANALYTICS=false`, and what it does not switch off.

    **Sampling is refused and this is the shape of the refusal.** PRD 10's
    *"which absence means what"* table has five rows and every one reads a
    **count**; a sample rate makes every count an estimate and adds a sixth row
    meaning *"the row that was not written"*, indistinguishable in the data
    from every other absence. So the setting is a `bool` and both tiers obey
    it together -- asserted by driving *both*, because a switch honoured on one
    tier is the defect a single-tier case cannot see.

    The control is `GET /search` through the same app: this switch is about the
    suggest surface and must not reach the search one.
    """
    async for client in _client(settings_without_the_writer):
        for tier, probe in (("prefix", TYPED_PREFIX), ("fuzzy", TYPED_TYPO)):
            response = await client.get("/search/suggest", params={"q": probe, "tier": tier})
            assert response.status_code == 200, response.text
            assert len(response.json()["results"]) == 1, "the premise: the box answered"
        assert await _count(sessions) == 0

        assert (await client.get("/search", params={"q": "marrowlight"})).status_code == 200
        assert [(row["surface"], row["tier"]) for row in await _rows(sessions)] == [
            ("search", None)
        ], "the control: the search surface is untouched by the suggest switch"


async def test_a_suggest_with_no_household_writes_no_row_and_the_eval_harness_is_that_caller(
    settings: Settings, catalog: uuid.UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """PRD 10's *"a search with no household"* exclusion, and the caller it is
    now load-bearing for.

    `usher.eval.surfaces.suggest.tier_suggester` builds the **real** pipeline
    through the **real** composition root -- so it holds a real
    `SearchAnalytics` over a real `PostgresSearchQueryRepository` -- and calls
    `pipeline.search.suggest(probe, limit=…, tier=…)` resolving no household at
    all. `usher eval suggest --full` drives that thousands of times. A writer
    that did not carry `_record_search`'s `user_id is None` guard would fill
    `search_queries` with evaluation traffic wearing a household's clothes, and
    every rate PRD 10 computes off this table would be measuring the harness.

    **The absence is tested rather than left to be discovered**, and its
    control is the same session writing a row for a call that *does* name a
    household -- so "no rows" is not merely what a pipeline nobody wired
    produces.

    🔴 **"No row" is the weaker half here, and this case's own sweep is what
    said so.** Planting the guard away -- `_record_suggest` recording whatever
    `user_id` it was handed -- left this case **green** in its first spelling,
    because a `NULL` `user_id` is refused by `search_queries`' own `NOT NULL`,
    the refusal becomes a `RepositoryConflict`, and `_write_row` absorbs it by
    design. So on the Postgres arm the *database* produces the absence the
    guard is supposed to produce, and the plant is visible only as an error log
    line per probe -- thousands of them under `usher eval suggest --full`. Only
    `tests/unit/test_services_search.py`'s pair killed it, and only because
    `FakeSearchQueryRepository` has no foreign keys to refuse with. **The
    inversion is the finding**: the fake being *more forgiving* is what gave
    the unit case teeth, and the arm with the real constraint is the one that
    could not see the defect. So this case asserts the sink as well as the
    table: **nothing was attempted**, not merely nothing landed.

    Fails: `SearchService.suggest` recording unconditionally, or with
    `user_id` defaulted to anything but `None`.
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

        # **The sink's own positive control, and it is why the two assertions
        # above are evidence rather than an empty list.** A household id naming
        # no `users` row is refused by `fk_search_queries_user_id_users`,
        # absorbed by `_write_row`, and logged -- which is *exactly* what the
        # planted guard produces per eval probe. If this line does not appear,
        # the two "no refusals" assertions were passing because nothing could
        # ever have reached the sink.
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
