"""`GET /search` through a real request against a real schema.

**What only this level can see.** `tests/unit/test_api_search.py` drives the
router over a scripted `SearchIndex`, and `tests/integration/
test_services_search.py` drives the real service over real Postgres -- so what
is left for this file is the *request*: the shipped `get_search_service`
resolving through `usher.composition.build_search_service` into a real
`PostgresSearchIndex` on the request's session, and the answer that produces
travelling back through the DTO.

**The unit file's fake has no text analysis at all** -- substring matching over
casefolded fields, no stemming, no `tsquery` parsing, no `ts_rank`, no weight
classes -- so *every* claim about what a query actually matches is only true
here. That is why the ranking case below asks a question the fake could not be
asked: a name match must outrank an overview match, which is `setweight` plus
`ts_rank` rather than a hand-coded constant.

**This module commits for real, so it cleans up after itself.** `get_session`
commits every request; CLAUDE.md records what leaving `titles` behind did to
four tests in three other files, each of which passed in isolation.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import HdrFormat, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert

SECRET_KEY = "0123456789abcdef0123456789abcdef"
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)

# Every title this file writes carries it in `sort_name`, so teardown deletes
# exactly what this file created rather than emptying a table another
# committing file is also using.
MARK = "Search Route Case"

# A word invented for this file and shared by both seeded titles, so a query
# for it matches two rows and the *order* is assertable. One holds it in its
# name and the other only in its overview: under `setweight`'s A/D split the
# name match must win, and under any implementation that lost the weighting
# they tie and the `id` tiebreak decides -- which is why the ids are ordered
# deliberately below.
TERM = "kestrelbound"

# The suggest cases' title, invented for this file. Long enough that a
# one-character substitution near its end stays well above `pg_trgm`'s 0.3
# similarity floor -- tier 2 gates on `name % prefix` before it computes an
# edit distance, so a short typo string is a case about the floor rather than
# about the tier.
TYPEABLE = "Thornquill Vane"
#: A true prefix of it. Seven characters, so the route's four-character
#: minimum is not what this arm is about.
TYPED_PREFIX = "thornqu"
#: The whole name with one substituted character. Tier 1 cannot match it --
#: `LIKE` is not an edit distance -- and tier 2's `levenshtein_less_equal <= 2`
#: over the same-length head can.
TYPED_TYPO = "thornquill vame"


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # Both lanes off. `dependency_overrides` do not reach the lifespan, so
        # a push lane here would build the real adapter against an unreachable
        # host and a worker lane would poll the same database these cases
        # assert on.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one: the
    route reads from its own session in its own transaction, so a test that
    seeded through a single shared transaction would be handing the app rows it
    cannot see."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # `TRUNCATE sources CASCADE` takes `media_items` with it, which is what
        # leaves this file's titles with no referents.
        await session.execute(text("TRUNCATE sources CASCADE"))
        # PRD 10's analytics rows, since M9's F2: every `GET /search` through
        # this file writes one and commits it. **They have to go before any
        # other file deletes the default user** -- `search_queries.user_id` is
        # `ON DELETE RESTRICT` on purpose (a household's search history is user
        # state), so a row left behind here turns a neighbouring file's
        # `DELETE FROM users WHERE name = 'default'` into a foreign-key
        # violation rather than into a slow test. Unscoped rather than marked,
        # because this table has no column this file could mark.
        await session.execute(text("DELETE FROM search_queries"))
        await session.execute(
            text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


class _Catalog:
    """The seeded titles, by the role each plays in an assertion."""

    def __init__(self, named: uuid.UUID, described: uuid.UUID, typeable: uuid.UUID) -> None:
        self.named = named
        self.described = described
        self.typeable = typeable


@pytest_asyncio.fixture
async def catalog(sessions: async_sessionmaker[AsyncSession], clean: None) -> _Catalog:
    """One title carrying `TERM` in its **name**, one carrying it only in its
    **overview**, and one owned copy of the second.

    The described title is created **first**, so its UUIDv7 sorts below the
    named one's. That is the ordering premise this file needs rather than a
    detail: `SearchService._rank` breaks a score tie with `(-score, title_id)`,
    so an implementation that lost the weight classes would put the *described*
    title first -- and with the ids the other way round a broken implementation
    and a correct one would produce the same list. Asserted in the case itself.

    The owned copy hangs off the described title so that `owned` and the top
    position disagree; a DTO that hard-coded either would be visible.
    """
    described = Title(
        kind=TitleKind.MOVIE,
        name="A Winter Field Study",
        sort_name=f"{MARK} winter",
        year=2019,
        overview=f"A naturalist follows one {TERM} hawk across a frozen estuary.",
    )
    named = Title(
        kind=TitleKind.MOVIE,
        name=f"The {TERM.capitalize()} Hour",
        sort_name=f"{MARK} hour",
        year=2021,
    )
    # The suggest cases' own title, and it deliberately carries **neither**
    # `TERM` nor a leading article: tier 1 is `lower(name) LIKE 'typed%'`, so a
    # name beginning "The " is unreachable by anything a viewer would type at
    # it, and a name matching `TERM` would add a third row to every `/search`
    # assertion above.
    typeable = Title(
        kind=TitleKind.MOVIE,
        name=TYPEABLE,
        sort_name=f"{MARK} thornquill",
        year=2020,
    )
    source = Source(
        kind=SourceKind.EMBY,
        name="Search Route Emby",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    async with sessions() as session:
        titles = PostgresTitleRepository(session)
        await titles.add(described)
        await titles.add(named)
        await titles.add(typeable)
        await PostgresSourceRepository(session).add(source)
        await session.commit()
    async with sessions() as session:
        await PostgresMediaItemRepository(session).upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id=f"emby-{described.id}",
                    title_id=described.id,
                    episode_id=None,
                    container="mkv",
                    video_codec="hevc",
                    audio_codec="truehd",
                    width=3840,
                    height=2160,
                    hdr_format=HdrFormat.HDR10,
                    audio_channels=8,
                    file_size_bytes=1,
                    runtime_seconds=5400,
                    added_at=None,
                    last_seen_at=SEEN_AT,
                )
            ]
        )
        await session.commit()
    return _Catalog(named=named.id, described=described.id, typeable=typeable.id)


@pytest_asyncio.fixture
async def client(settings: Settings, catalog: _Catalog) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_the_shipped_graph_answers_a_real_full_text_search(
    client: AsyncClient, catalog: _Catalog
) -> None:
    """The whole path with nothing overridden: `get_search_service` ->
    `build_search_service` -> `PostgresSearchIndex` on the request's session.

    **The ordering premise is asserted first**, because UUIDv7 makes
    `ORDER BY id` and `ORDER BY <the real key>` agree by accident: with the
    described title's id *below* the named title's, a scorer that lost the
    weight classes ties the two and the `title_id` tiebreak puts the described
    one first. So the expected order is the reverse of the accidental one.
    """
    assert catalog.described < catalog.named, (
        "the seeding order stopped producing the id order this case rests on; "
        "an unweighted scorer would now pass"
    )

    response = await client.get("/search", params={"q": TERM})
    assert response.status_code == 200, response.text
    body = response.json()
    ids = [result["title_id"] for result in body["results"]]
    assert ids == [str(catalog.named), str(catalog.described)], body

    # `owned` is a real `media_items` join rather than a constant, and the one
    # owned copy is on the title that is *not* first -- so a DTO that returned
    # `owned` for the top hit, or for every hit, is visible.
    assert [result["owned"] for result in body["results"]] == [False, True]
    assert body["query"] == TERM
    assert body["requested_mode"] == "full_text"
    assert body["mode"] == "full_text"
    # A `full_text` request reports 0.0 because no semantic lane ran. That is a
    # statement about the request, not about the catalog.
    assert body["semantic_coverage"] == 0.0
    assert body["expanded_query"] is None


async def test_a_fused_request_is_served_as_full_text_on_an_api_only_deployment(
    client: AsyncClient, catalog: _Catalog
) -> None:
    """`create_app`'s lifespan builds a model only when `worker_enabled`, and
    this app has it off -- which is the shipped shape of an API-only
    deployment, not a test contrivance.

    The results are the full-text ones and every row of them is correct; the
    only thing that says the deployment is narrowed is the two mode fields
    disagreeing. Without them a client sees `semantic_coverage == 0.0`, which
    is also what a healthy fused search over a catalog with no embeddings
    reports.
    """
    body = (await client.get("/search", params={"q": TERM, "mode": "fused"})).json()
    assert body["requested_mode"] == "fused"
    assert body["mode"] == "full_text"
    assert [result["title_id"] for result in body["results"]] == [
        str(catalog.named),
        str(catalog.described),
    ]


async def test_a_semantic_request_is_refused_rather_than_quietly_narrowed(
    client: AsyncClient,
) -> None:
    """`fused` narrows because a whole lane is left; `semantic` refuses,
    because the caller asked the one question full text cannot answer.

    Driven through the real graph rather than a raised fake, so this is also
    the proof that `build_search_service` really does hand the API a
    `SearchService` with no embedder -- an override could not tell that apart
    from a route that raises on its own.
    """
    response = await client.get("/search", params={"q": TERM, "mode": "semantic"})
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == ProblemCode.VALIDATION_FAILED.value
    assert body["instance"] == "/search"


async def test_a_blank_query_is_answered_without_touching_the_index(
    client: AsyncClient,
) -> None:
    """200 with no results, from the service's own guard.

    Worth an integration case rather than only a unit one because the guard is
    what keeps a keystroke-driven client off a 1.27M-row `ts_rank`: this is the
    request a search box sends between every character.
    """
    response = await client.get("/search", params={"q": "   "})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"] == []
    assert body["mode"] == "full_text"
    assert body["expanded_query"] is None


async def test_the_limit_is_clamped_by_the_deployments_own_ceiling(
    postgres_url: str, catalog: _Catalog
) -> None:
    """The route declares no maximum, so an absurd `?limit=` is the
    deployment's ceiling rather than a 422 or a scan.

    `search_result_limit = 1` here, against two matching titles: one comes
    back. A route that had re-declared a `le=` of its own would answer 422 for
    the same request, and a route that passed the number through unclamped
    would answer with both.

    Built as a fresh `Settings` rather than a `model_copy(update=...)` of the
    fixture's: that spelling skips validation entirely, and this file's whole
    point is that the number reaching the service is the validated one.
    """
    app = create_app(
        Settings(
            database_url=postgres_url,
            secret_key=SECRET_KEY,
            push_enabled=False,
            worker_enabled=False,
            search_result_limit=1,
        )
    )
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/search", params={"q": TERM, "limit": 10_000})

    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 1


# --- `GET /search/suggest`, the two tiers over the real indexes ------------


async def test_the_two_tiers_are_two_indexes_in_the_composed_graph(
    client: AsyncClient, catalog: _Catalog
) -> None:
    """**The one claim only this level can make: `build_search_service` really
    constructs two different `SuggestIndex` implementations, and each one is
    reachable by name from the wire.**

    Every unit case in `tests/unit/test_api_suggest.py` is satisfied by a
    factory that handed one index to both slots and by fakes that are two
    objects either way -- what distinguishes them is *which statement runs
    against which index in the real schema*. `PostgresPrefixSuggestIndex`
    reads `ix_titles_name_lower_prefix`, which exists only because `m09a`
    shipped it; `PostgresSuggestIndex` reads the GIN trigram index M6 shipped.

    Three arms, and all three are needed. Tier 1 on a true prefix proves the
    btree path answers at all. Tier 1 on a typo proves it is **not** the
    trigram index wearing tier 1's name -- the arm that fails if both slots
    hold `PostgresSuggestIndex`. Tier 2 on the same typo proves the fuzzy slot
    is not `PostgresPrefixSuggestIndex` -- the arm that fails if both slots
    hold the btree.
    """
    found = await client.get("/search/suggest", params={"q": TYPED_PREFIX, "tier": "prefix"})
    missed = await client.get("/search/suggest", params={"q": TYPED_TYPO, "tier": "prefix"})
    forgiven = await client.get("/search/suggest", params={"q": TYPED_TYPO, "tier": "fuzzy"})

    assert found.status_code == 200, found.text
    assert [row["title_id"] for row in found.json()["results"]] == [str(catalog.typeable)], (
        found.text
    )
    assert found.json()["tier"] == "prefix"

    assert missed.json()["results"] == [], missed.text
    assert [row["title_id"] for row in forgiven.json()["results"]] == [str(catalog.typeable)], (
        forgiven.text
    )
    assert forgiven.json()["tier"] == "fuzzy"


async def test_the_minimum_prefix_length_is_in_force_on_the_shipped_route(
    client: AsyncClient, catalog: _Catalog
) -> None:
    """Three characters of a real prefix of a real seeded title, through the
    real graph: an empty box and a `min_query_length` that says why.

    The fourth character is what makes this a statement about the bound rather
    than about the catalog -- the same title, one character further in, comes
    back. Fails: the bound left in a unit fixture and never wired to the
    shipped router, which every case in the unit file would still pass.
    """
    refused = await client.get("/search/suggest", params={"q": TYPED_PREFIX[:3]})
    served = await client.get("/search/suggest", params={"q": TYPED_PREFIX[:4]})

    assert refused.status_code == 200, refused.text
    assert refused.json()["results"] == []
    assert refused.json()["min_query_length"] == 4
    assert [row["title_id"] for row in served.json()["results"]] == [str(catalog.typeable)], (
        served.text
    )


async def test_a_blank_suggest_is_answered_without_touching_either_index(
    client: AsyncClient,
) -> None:
    """200 with no results, which is the request a search box sends on every
    backspace to zero. On tier 1 the query it replaces is `LIKE '%'` over
    1,271,138 rows plus a 10.9M-row union, collected, de-duplicated and sorted
    to answer a question nobody asked."""
    for tier in ("prefix", "fuzzy"):
        response = await client.get("/search/suggest", params={"q": "   ", "tier": tier})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "query": "   ",
            "tier": tier,
            "min_query_length": 4 if tier == "prefix" else 1,
            "results": [],
        }


async def test_one_answered_request_writes_exactly_one_search_queries_row(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], catalog: _Catalog
) -> None:
    """PRD 10's analytics row, through the shipped request and read back from a
    session the request never touched.

    **`get_session` commits when the handler returns, so this reads a committed
    row either way** -- which is exactly why the durability claim is made one
    layer down, in
    `tests/integration/test_services_search.py::
    test_the_analytics_row_is_committed_and_a_second_session_can_read_it`, on
    the root that has no such commit. What this file adds is the three facts
    only a request has: that the wiring is reached at all, that the household
    is `DefaultUserIdDep`'s and not an invented one, and that **one request is
    one row**.

    Two requests rather than one, because a single request cannot tell "one row
    per request" from "one row, ever" -- and the second asks for a mode that
    degrades, so the `mode` column is asserted against a request whose
    `requested_mode` differs from it.
    """
    first = await client.get("/search", params={"q": TERM})
    second = await client.get("/search", params={"q": TERM, "mode": "fused"})
    assert (first.status_code, second.status_code) == (200, 200), second.text
    assert second.json()["requested_mode"] == "fused", "the premise: the second request degraded"
    assert len(first.json()["results"]) == 2, "the premise: the search answered"

    async with sessions() as reader:
        rows = (
            await reader.execute(
                text(
                    "SELECT q.query, q.mode, q.result_count, q.latency_ms, "
                    "q.clicked_title_id, q.played, u.is_default "
                    "FROM search_queries q JOIN users u ON u.id = q.user_id ORDER BY q.id"
                )
            )
        ).all()

    assert [(one.query, one.mode, one.result_count) for one in rows] == [
        (TERM, "full_text", 2),
        (TERM, "full_text", 2),
    ]
    # The outcome half is F3's, and it is written as literals rather than left
    # to a column default -- so a dashboard reads a real `false` rather than a
    # column nobody filled.
    assert [(one.clicked_title_id, one.played) for one in rows] == [(None, False), (None, False)]
    # Not an invented id: `DefaultUserIdDep` resolves PRD 01's singleton, which
    # is the only household this deployment has.
    assert [one.is_default for one in rows] == [True, True]
    assert all(one.latency_ms >= 0 for one in rows), rows


async def test_a_keystroke_writes_no_row_on_either_tier(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], catalog: _Catalog
) -> None:
    """`GET /search/suggest` records nothing -- answered, refused, or blank --
    and the control is a `/search` request through the same client.

    Storing a `SuggestTier` under `search_queries.mode`, a `SearchMode`, would
    be two vocabularies under one name; and tier 1 at p50 0.6 ms against full
    text's 33.3 ms means a keystroke-driven client would out-number the
    searches by an order of magnitude in every mode-split panel. The argument
    is in `SearchService.suggest`'s docstring and in PRD 10, and this is what
    says the shipped route agrees with it.

    Four requests, because the route has three arms -- answered, below
    `min_query_length`, and blank -- and a writer placed on any one of them is
    a different defect.
    """
    for params in (
        {"q": TYPED_PREFIX},
        {"q": TYPED_PREFIX[:3]},
        {"q": "   "},
        {"q": TYPED_TYPO, "tier": "fuzzy"},
    ):
        assert (await client.get("/search/suggest", params=params)).status_code == 200, params

    async with sessions() as reader:
        assert await _analytics_rows(reader) == 0

    assert (await client.get("/search", params={"q": TERM})).status_code == 200
    async with sessions() as reader:
        assert await _analytics_rows(reader) == 1, (
            "the control: this deployment does write a row on the search path"
        )


async def _analytics_rows(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT count(*) FROM search_queries"))).scalar_one())
