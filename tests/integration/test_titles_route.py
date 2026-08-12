"""`GET /titles/{id}` through a real request against a real schema.

**What only this level can see.** `tests/unit/test_api_titles.py` drives the
route over a fake service, and `tests/integration/test_services_titles.py`
drives the real service over real Postgres -- so what is left is the request
itself: `get_session` as the commit boundary (the promotion this route makes
is durable, not a flush the response outlives), the DTO rendering rows a
real schema produced, and the cost of one read measured off the statements
the repositories actually issued.

Two divergences the port fakes carry are on this route's read path and both
are real here: `FakeMediaItemRepository` has no foreign keys, and
`FakeWatchStateRepository` stores `observed_at` as `updated_at`.

**This module commits for real, so it cleans up after itself.** `get_session`
commits every request, and CLAUDE.md records what leaving `titles` and `jobs`
behind did to four tests in three other files, each of which passed in
isolation. `media_items` cascades from `sources`; `titles` and `jobs` do not,
`watch_states.title_id` is `ON DELETE RESTRICT`, and `seasons`/`episodes`
cascade from `titles`.
"""

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime

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
from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import EnrichmentState, HdrFormat, ImageKind, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.image import Image
from usher.domain.jobs import JobPriority
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge

SECRET_KEY = "0123456789abcdef0123456789abcdef"
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
SWEPT_AFTER = datetime(2026, 8, 2, tzinfo=UTC)
# Every title this file writes carries it, so teardown can delete exactly
# what this file created rather than emptying a table another committing
# file is also using.
MARK = "Route Case"


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # Both lanes off. `dependency_overrides` do not reach the lifespan,
        # so a push lane here would build the real adapter against an
        # unreachable host and open a socket, and a worker lane would claim
        # the very `enrich` job these cases assert on.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The route commits from its own session in its own transaction, so a test
    that seeded through a single shared transaction would be handing the app
    rows it cannot see.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        for statement in (
            # `users` first: `watch_states.user_id` is `ON DELETE CASCADE`
            # while `watch_states.title_id` is `ON DELETE RESTRICT`, so
            # removing the default user is what makes the titles deletable.
            "DELETE FROM users WHERE name = 'default'",
            "DELETE FROM jobs",
            # Takes `media_items` with it (`ON DELETE CASCADE`), which is
            # what leaves `titles` with no `media_items.title_id` referents.
            "TRUNCATE sources CASCADE",
            # Three `DROP TABLE IF EXISTS stg_*` statements were here until
            # M6, and the reason outlives them. Every write in this file goes
            # through `usher.db.staging`, which created an `UNLOGGED` table
            # with DDL -- `stg_jobs` from the demand promotion the *route*
            # makes, `stg_media_items` from the seeded copies,
            # `stg_watch_states` from the seeded progress. Postgres DDL is
            # transactional, so only a **committing** test leaked one, and it
            # surfaced as
            # `test_migrations.py::test_migration_matches_the_orm_metadata`
            # reporting schema drift in a *different file*: this module passed
            # alone and took that one down in a full run. Measured then in
            # both directions -- and the first sweep of it scored a kill for
            # the wrong reason, because two of the three were still leaking
            # while only `stg_jobs` was under test. M6 made all three
            # `CREATE TEMP TABLE ... ON COMMIT DROP`, which deletes the leak
            # rather than cleaning up after it.
        ):
            await session.execute(text(statement))
        # Last, and bound rather than interpolated: only this file's own
        # titles go, so a blanket `DELETE FROM titles` cannot reach another
        # committing file's rows.
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
    """Every SQL statement SQLAlchemy issues, from every engine in the
    process -- including the app's own, which is the one under measurement.

    Captured off `before_cursor_execute` rather than transcribed: M4
    replaced two tasks that asserted on a hand-copied lookalike of a query,
    because the copy drifts from the repository and then reads like
    coverage.
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


async def _given_source(
    sessions: async_sessionmaker[AsyncSession], name: str, *, base_url: str = "https://emby.invalid"
) -> Source:
    source = Source(
        kind=SourceKind.EMBY,
        name=name,
        base_url=base_url,
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    async with sessions() as session:
        await PostgresSourceRepository(session).add(source)
        await session.commit()
    return source


async def _given_title(
    sessions: async_sessionmaker[AsyncSession],
    name: str,
    *,
    state: EnrichmentState = EnrichmentState.STUB,
    kind: TitleKind = TitleKind.MOVIE,
) -> Title:
    title = Title(
        kind=kind, name=name, sort_name=f"{MARK} {name}", year=2021, enrichment_state=state
    )
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title


async def _given_copies(
    sessions: async_sessionmaker[AsyncSession], *copies: MediaItemUpsert
) -> None:
    async with sessions() as session:
        await PostgresMediaItemRepository(session).upsert_many(list(copies))
        await session.commit()


def _copy(
    *,
    source_id: uuid.UUID,
    title_id: uuid.UUID,
    external_id: str,
    episode_id: uuid.UUID | None = None,
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=episode_id,
        container="mkv",
        video_codec="hevc",
        audio_codec="truehd",
        width=3840,
        height=2160,
        hdr_format=HdrFormat.DOLBY_VISION,
        audio_channels=8,
        file_size_bytes=68_719_476_736,
        runtime_seconds=9360,
        added_at=None,
        last_seen_at=SEEN_AT,
    )


async def _jobs(sessions: async_sessionmaker[AsyncSession]) -> Sequence[tuple[str, str, int]]:
    async with sessions() as session:
        rows = (
            await session.execute(text("SELECT kind, key, priority FROM jobs ORDER BY key"))
        ).all()
    return [(str(kind), str(key), int(priority)) for kind, key, priority in rows]


async def test_opening_a_stub_commits_the_promotion(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`get_session` is the request's commit boundary, and the promotion is
    the first write any client-facing route in this project makes.

    Read back on a **different connection**, which is the whole assertion: a
    handler that enqueued and never committed passes every unit case (a fake
    queue is a dict) and passes `tests/integration/test_services_titles.py`
    too (that session is rolled back by design). The job has to still be
    there after the response, from somewhere else, at `DEMAND`.
    """
    title = await _given_title(sessions, "A Promoted Stub")

    response = await client.get(f"/titles/{title.id}")

    assert response.status_code == 200
    assert response.json()["enrichment_state"] == "stub"
    assert await _jobs(sessions) == [("enrich", str(title.id), int(JobPriority.DEMAND))]


async def test_availability_spans_two_sources_and_keeps_a_retracted_copy(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Two real `sources` rows, two real foreign keys, and a retraction that
    a real `UPDATE` produced.

    PRD 08's rule is that a degraded source narrows the answer rather than
    failing it, and what "narrowed" means on the wire is a badge still
    present with `available: false`. The ordering that puts the retracted
    copy last is Postgres's, not a Python `sort`, and it survives the DTO.
    """
    mine = await _given_source(sessions, "Living Room Emby")
    theirs = await _given_source(sessions, "Loft Emby")
    title = await _given_title(sessions, "A Film On Two Servers", state=EnrichmentState.ENRICHED)
    await _given_copies(
        sessions,
        _copy(source_id=mine.id, title_id=title.id, external_id="mine-1"),
        _copy(source_id=theirs.id, title_id=title.id, external_id="theirs-1"),
    )
    async with sessions() as session:
        await PostgresMediaItemRepository(session).mark_unseen_unavailable(
            theirs.id, seen_since=SWEPT_AFTER, max_retract_fraction=1.0
        )
        await session.commit()

    body = (await client.get(f"/titles/{title.id}")).json()

    assert [(copy["source"], copy["available"]) for copy in body["availability"]] == [
        ("Living Room Emby", True),
        ("Loft Emby", False),
    ]
    # PRD 07's "nothing in this surface mentions a media server", against a
    # real row rather than a fake's: the source's own item id is the thing
    # that must not escape, and the *name* an operator typed is a correct
    # value for a badge -- so this asserts on the id and on the key, never on
    # a vendor word.
    rendered = json.dumps(body)
    assert "mine-1" not in rendered
    assert "external_id" not in rendered


async def test_an_episodes_watch_state_does_not_leak_onto_its_series(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`watch_states` has a `num_nonnulls(title_id, episode_id) = 1` CHECK,
    so an episode's progress and its series' progress are separate rows that
    a dict cannot keep apart by constraint -- and `list_for_title`'s
    `episode_id IS NULL` is the whole of the bound on the availability half.

    Rendered: a series a user has watched one episode of reports
    `watch_state: null` and **one** badge, not one badge per episode file.
    At 999,827 episodes among 1,126,789 items on the one measured source,
    the wrong answer here is a response whose size is the size of the show.
    """
    source = await _given_source(sessions, "Living Room Emby")
    series = await _given_title(
        sessions, "A Series", state=EnrichmentState.ENRICHED, kind=TitleKind.SERIES
    )
    season_id, episode_id = new_id(), new_id()
    async with sessions() as session:
        await session.execute(
            text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :t, 1)"),
            {"id": season_id, "t": series.id},
        )
        await session.execute(
            text(
                "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
                "VALUES (:id, :t, :s, 1, 1)"
            ),
            {"id": episode_id, "t": series.id, "s": season_id},
        )
        await session.commit()
    await _given_copies(
        sessions,
        _copy(source_id=source.id, title_id=series.id, external_id="series-1"),
        _copy(
            source_id=source.id,
            title_id=series.id,
            external_id="episode-1",
            episode_id=episode_id,
        ),
    )
    # The default user is created by the request that needs it, so one
    # request has to have happened before a watch state can name it.
    assert (await client.get(f"/titles/{series.id}")).status_code == 200
    async with sessions() as session:
        user_id = (
            await session.execute(text("SELECT id FROM users WHERE name = 'default'"))
        ).scalar_one()
        await PostgresWatchStateRepository(session).merge_from_source(
            [
                WatchStateMerge(
                    user_id=user_id,
                    title_id=None,
                    episode_id=episode_id,
                    position_seconds=1840,
                    played=False,
                    runtime_seconds=2700,
                    observed_at=SEEN_AT,
                )
            ]
        )
        await session.commit()

    body = (await client.get(f"/titles/{series.id}")).json()

    assert body["watch_state"] is None, "an episode's progress is not the series' progress"
    assert len(body["availability"]) == 1


async def test_the_images_key_renders_real_rows_and_leaks_no_provider_url(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`images` end to end: rows a real `replace_for_titles` wrote, ordered by
    a real `ORDER BY`, rendered by the DTO, over a real request.

    Two things only this level can say. The order is the *statement's* --
    against the fake it is a Python key function, and a deleted `ORDER BY
    is_primary DESC, id` leaves heap order, which is why the backdrop is
    written first so its UUIDv7 id is the smaller of the two. And the leak
    assertion is against the *serialised body*: the CDN base and the
    provider's own path are what a client would need to go around this API,
    and PRD 07's "clients never see provider image URLs and never need a
    provider key" is a claim about these bytes.
    """
    title = await _given_title(sessions, "A Film With Artwork", state=EnrichmentState.ENRICHED)
    backdrop = Image(
        title_id=title.id,
        kind=ImageKind.BACKDROP,
        provider="tmdb",
        provider_path="/a-backdrop.jpg",
        is_primary=False,
    )
    poster = Image(
        title_id=title.id,
        kind=ImageKind.POSTER,
        provider="tmdb",
        provider_path="/a-poster.jpg",
        is_primary=True,
    )
    logo = Image(
        title_id=title.id,
        kind=ImageKind.LOGO,
        provider="tmdb",
        provider_path="/a-logo.svg",
        is_primary=False,
    )
    assert backdrop.id < poster.id, (
        "the premise: the unflagged image sorts first by id, so is_primary is the "
        "only thing that can put the poster in front of it"
    )
    async with sessions() as session:
        await PostgresImageRepository(session).replace_for_titles(
            [title.id], [backdrop, poster, logo]
        )
        await session.commit()

    response = await client.get(f"/titles/{title.id}")
    body = response.json()

    assert body["images"] == [
        {"id": str(poster.id), "kind": "poster"},
        {"id": str(backdrop.id), "kind": "backdrop"},
    ]
    assert "image.tmdb.org" not in response.text
    assert "/a-poster.jpg" not in response.text


async def test_a_title_whose_only_artwork_is_declined_carries_no_images_key(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The filter's residual through a real request: the row is in Postgres,
    the response has no `images` key at all, and the two are only reconcilable
    through `usher.images.references`. Asserted here as well as in the unit
    file because `response_model_exclude_unset` is a route-level flag, so the
    key's absence is a property of the request rather than of the DTO."""
    title = await _given_title(sessions, "A Film With Only A Logo", state=EnrichmentState.ENRICHED)
    async with sessions() as session:
        await PostgresImageRepository(session).replace_for_titles(
            [title.id],
            [
                Image(
                    title_id=title.id,
                    kind=ImageKind.LOGO,
                    provider="tmdb",
                    provider_path="/only-a-logo.svg",
                    is_primary=True,
                )
            ],
        )
        await session.commit()

    body = (await client.get(f"/titles/{title.id}")).json()

    assert "images" not in body
    async with sessions() as session:
        stored = await PostgresImageRepository(session).list_for_title(title.id)
    assert len(stored) == 1, "the premise: the row is stored and the read is what drops it"


async def test_a_title_read_costs_the_same_statements_however_many_copies_it_has(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    """**The shape that would catch a quadratic, on the read path.**

    `TitleReadService.detail` is seven reads and a promotion, and none of
    them may be per copy, per source, per credit, per person or per image: a
    household's detail screen is the request a client makes most, and a film
    on three servers must not cost three round trips. One copy on one source against
    five copies on three sources, and the statement counts have to be *equal*
    -- not "small", which a per-copy read of a title with two copies also
    satisfies.

    Captured off `before_cursor_execute`, so what is counted is what the
    repositories sent rather than what this file believes they send. Both
    titles are stubs, so both reads also issue the promotion; the warm-up
    request below is what keeps `ensure_default_user`'s one-time `INSERT`
    out of the measurement.
    """
    first_source = await _given_source(sessions, "Living Room Emby")
    one_copy = await _given_title(sessions, "A Film On One Server")
    await _given_copies(
        sessions, _copy(source_id=first_source.id, title_id=one_copy.id, external_id="only-1")
    )
    assert (await client.get(f"/titles/{one_copy.id}")).status_code == 200

    statement_counter.clear()
    assert (await client.get(f"/titles/{one_copy.id}")).status_code == 200
    small = len(statement_counter)

    second, third = (
        await _given_source(sessions, "Loft Emby"),
        await _given_source(sessions, "Shed Emby"),
    )
    many_copies = await _given_title(sessions, "A Film On Three Servers")
    await _given_copies(
        sessions,
        *(
            _copy(source_id=source.id, title_id=many_copies.id, external_id=f"copy-{index}")
            for index, source in enumerate((first_source, first_source, second, second, third))
        ),
    )

    statement_counter.clear()
    body = (await client.get(f"/titles/{many_copies.id}")).json()
    large = len(statement_counter)

    assert len(body["availability"]) == 5, "the fixture did not produce five copies"
    assert small == large, (
        f"{small} statements for one copy on one source, {large} for five copies on "
        "three -- something on this read costs a statement per copy or per source"
    )
    # **And the absolute level, because flatness alone would not have shown
    # what this measurement found.** Thirteen, not the service's seven: one
    # `ensure_default_user` read, the seven reads `detail` documents, and
    # **five for the promotion** -- `SAVEPOINT`, `DROP TABLE IF EXISTS
    # pg_temp.stg_jobs`, `CREATE TEMP TABLE stg_jobs`, the `INSERT ...
    # SELECT`, `RELEASE SAVEPOINT` -- plus a `COPY` on the raw asyncpg
    # connection that this counter cannot see at all.
    #
    # It was ten against four service reads until M9's `credits` key, which
    # adds one `list_for_title` per `CreditKind`, and twelve against six until
    # its `images` key. **Each time both numbers moved by the same amount and
    # the flatness assertion above is untouched**, which is the distinction
    # this bound exists to draw: one more statement *per request* is a cost,
    # and one more *per copy* would be a defect.
    # `PostgresJobQueue.enqueue` is M4's bulk path, and PRD 03's demand
    # promotion is the first caller that invokes it **once per client
    # request** rather than once per batch of a walk. The count is unchanged
    # by M6 and the *contention* is not: this used to be `CREATE UNLOGGED
    # TABLE` on a fixed, shared name, so a detail-screen open and a nightly
    # walk's batch serialised against each other for the length of the
    # other's whole transaction (measured at 819 ms). A temporary table has
    # nothing shared to lock, which is why five statements per request is now
    # a cost rather than a contention point --
    # `tests/integration/test_staging_lock.py` is where that is asserted.
    assert small <= 13, f"one title read issued {small} statements: {statement_counter}"


async def test_the_route_answers_with_the_source_down(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """PRD 08's governing rule at the boundary, against real infrastructure:
    the source row and its credential are intact, the host does not exist,
    and the read is unaffected -- because nothing on this path calls it.

    `https://emby.invalid` is a reserved TLD that cannot resolve, so an
    implementation that *did* reach for the adapter fails loudly here rather
    than silently succeeding against a host that happens to answer. Under
    the netguard the same call raises `NETWORK BLOCKED` instead. "It did not
    raise" is weak evidence on its own, which is why
    `tests/unit/test_services_titles.py` asserts the absence structurally,
    on the service's own imports; this is the same claim at the layer an
    operator would actually observe it.
    """
    source = await _given_source(sessions, "Unreachable Emby", base_url="https://emby.invalid")
    title = await _given_title(sessions, "A Film On A Dead Server", state=EnrichmentState.ENRICHED)
    await _given_copies(
        sessions, _copy(source_id=source.id, title_id=title.id, external_id="stranded-1")
    )

    response = await client.get(f"/titles/{title.id}")

    assert response.status_code == 200
    body = response.json()
    assert [(copy["source"], copy["available"]) for copy in body["availability"]] == [
        ("Unreachable Emby", True)
    ]
    assert body["watch_state"] is None


async def test_an_unknown_id_is_a_404_against_a_real_schema(client: AsyncClient) -> None:
    """PRD 07's RFC 9457 envelope, from a read that really went to Postgres
    and really found nothing -- `usher.ports.errors` draws the line this
    rests on: absence is not `PortUnavailable`.

    It read `== {"detail": "title not found"}` until M9. Changing a 4xx body
    is a client-visible break, so the cases that pinned the old shape move in
    the commit that changes it rather than quietly afterwards."""
    title_id = new_id()
    response = await client.get(f"/titles/{title_id}")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "https://usher.dev/errors/not-found",
        "title": "Not found",
        "status": 404,
        "code": "not_found",
        "detail": "title not found",
        "instance": f"/titles/{title_id}",
    }
