"""PRD 03's read-through loop, closed, through a real app.

`open -> stub -> promote -> enrich -> title.updated -> the client refetches
and gets the enriched row`. Every hop is real: a real request, real
Postgres, a real `StreamingResponse`, a real worker lane claiming from the
real queue, and `get_session`'s own commit boundary.

**The ordering is what only this level can see, and the way it is asserted
is not the way the plan sketched it.** `EnrichService` publishes *after* its
commit, so a client that refetches the instant it is told reads the enriched
row. The obvious shape -- await the enrichment, read the frame, refetch --
cannot fail against the wrong order: by the time the test is reading, both
the publish and the commit have happened whichever order they ran in, and
what is left is a race that is green on a fast host. So the publisher the
lane is given reads the title back **on its own connection, at the instant
of the publish**. A separate connection cannot see an uncommitted write, so
"published before committing" is a deterministic failure rather than a
timing one.

**Two things the app under test is not.** Its own worker lane is off and a
second `LaneSupervisor` runs one instead, because `create_app`'s lifespan
builds the TMDb provider from a real key and no test in this repository
makes a network request -- `dependency_overrides` do not reach a lifespan,
so the substitution is made where a composition root makes it. That the
lifespan *does* start a worker lane is
`tests/integration/test_lanes_in_the_server_process.py`'s claim and is not
re-made here. And `test_a_disconnect_unsubscribes` is not repeated from
`tests/unit/test_api_events.py`: that case runs against the same app
factory, the same route and the same streaming transport, and `GET /events`
touches no session at all, so a real database changes nothing about it.

**This module commits for real** -- the route's promotion, the lane's
enrichment, the default user, and three `usher.db.staging` tables that
Postgres DDL leaves behind. All of it is undone in teardown, because
CLAUDE.md records what leaving `titles` and `jobs` behind did to four tests
in three other files, each of which passed in isolation.
"""

import asyncio
import gzip
import json
import pathlib
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import usher.composition
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.streaming_asgi_transport import StreamingASGITransport
from usher.api.app import create_app
from usher.api.lanes import LaneSupervisor
from usher.composition import DefaultUserId, run_bootstrap, unit_of_work
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.bootstrap import BootstrapPhase
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.services.events import InMemoryEventBus

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# The one the `FakeMetadataProvider` holds a movie payload for, in the
# reserved synthetic band. `ix_titles_tmdb_id_kind` is unique per kind, so
# this file's teardown has to be real for a second run to work at all.
TMDB_ID = 90_000_550
MARK = "Sse Case"
# Generous against a lane that polls: the first pass is immediate, so a
# working loop closes in milliseconds and only a broken one waits.
BOUND = 20.0
HEARTBEAT_SECONDS = 0.05


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        sse_heartbeat_seconds=HEARTBEAT_SECONDS,
        # Small, so the overflow case is a burst of 32 rather than 256 --
        # and read off the *setting* rather than off `bus._queue_size`,
        # which is what the app was built from anyway.
        sse_queue_size=8,
        # See the module docstring: the lane this file needs carries a fake
        # provider, so it is built beside the app rather than by it.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        for statement in (
            "DELETE FROM users WHERE name = 'default'",
            "DELETE FROM jobs",
            "DELETE FROM raw_payloads WHERE provider = 'tmdb'",
            # Three `DROP TABLE IF EXISTS stg_*` statements stood here until
            # M6. Every write in this module goes through `usher.db.staging`,
            # which created its table with DDL -- and DDL is transactional, so
            # a committing module was the only kind that leaked one, surfacing
            # as schema drift in `test_migrations.py`, a different file that
            # then failed only in combination. The staging tables are
            # temporary now and drop at commit.
        ):
            await session.execute(text(statement))
        # **`tmdb_id` as well as the name mark, because enrichment renames
        # the row.** `FakeMetadataProvider.to_result` supplies its own
        # `name`/`sort_name` and `EnrichService` writes it, so a title this
        # file seeded as `Sse Case A Film` reads back as `A Film` the moment
        # the lane succeeds -- and a teardown keyed on the mark alone leaves
        # it behind. The next test then fails on `ix_titles_tmdb_id_kind`,
        # in a case that has nothing to do with enrichment and passes in
        # isolation. Measured, in this file, in that order.
        await session.execute(
            text("DELETE FROM titles WHERE sort_name LIKE :pattern OR tmdb_id = :tmdb_id"),
            {"pattern": f"{MARK} %", "tmdb_id": TMDB_ID},
        )
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@pytest_asyncio.fixture
async def app(settings: Settings, clean: None) -> AsyncIterator[FastAPI]:
    yield create_app(settings)


@pytest.fixture
def bus(app: FastAPI) -> InMemoryEventBus:
    """The bus `create_app` built, read back rather than installed.

    A fixture that set `app.state.events` itself would pass against a
    `create_app` that never built one -- and the whole loop below depends on
    the publisher a *lane* holds and the subscriber a *route* holds being
    the same object.
    """
    built = app.state.events
    assert isinstance(built, InMemoryEventBus)
    return built


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A streaming transport, not `httpx.ASGITransport`.

    The stock one runs the ASGI app to completion before returning a
    response, so `client.stream("GET", "/events")` against a route whose
    whole purpose is not to complete blocks inside the transport forever --
    a hang rather than a failure. See `tests/fakes/streaming_asgi_transport.py`.
    """
    async with LifespanManager(app) as manager:
        transport = StreamingASGITransport(manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


class _CommittedStateProbe(EventPublisher):
    """The real bus, plus what the database had committed at publish time.

    This is the ordering assertion. `EnrichService` publishes after its
    commit so a client that refetches immediately reads the enriched row;
    publishing first passes every unit case (a fake repository has no
    transaction) and races here. Reading the row back on **this publisher's
    own session** -- a different connection, in a different transaction --
    cannot see an uncommitted write, so the wrong order is a recorded
    `stub` rather than a flaky refetch.

    **It records the rows the handler *wrote*, not only the row the event is
    about**, and the two answer different questions.
    `titles.enrichment_state` answers *"was the client told too early?"* --
    it is the subject of the frame, and the whole `?titles=` contract is that
    a client may refetch it. The `jobs` rows answer *"what is still open at
    the instant of the frame?"*, which is
    [ADR-0033](../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)'s
    subject: the enrich handler stages two `BACKFILL` requests
    (`enrich.py:270-277`) into a transaction that is `JobWorker`'s rather
    than its own, and that transaction does not close until
    `complete(job.id)` + `_commit()` (`jobs.py:143-147`).

    **Until G2 those two reads disagreed, and now they cannot.** This probe
    recorded `[('enrich', 'running')]` -- the handler's own claim and neither
    of the jobs it had just enqueued -- because the frame was offered from
    inside that window. The worker now holds the frame until the window is
    closed, so the same read on the same second connection is the state a
    client can act on: the enqueues committed, and the claim gone with the
    `DELETE` that completed it.
    """

    def __init__(self, inner: EventPublisher, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._inner = inner
        self._sessions = sessions
        self.seen: list[tuple[ClientEventKind, str | None]] = []
        self.jobs_seen: list[list[tuple[str, str]]] = []

    async def publish(self, event: ClientEvent) -> None:
        committed: str | None = None
        jobs: list[tuple[str, str]] = []
        if event.title_id is not None:
            async with self._sessions() as session:
                committed = (
                    await session.execute(
                        text("SELECT enrichment_state FROM titles WHERE id = :id"),
                        {"id": event.title_id},
                    )
                ).scalar_one_or_none()
                jobs = [
                    (row[0], row[1])
                    for row in (
                        await session.execute(
                            text("SELECT kind, status FROM jobs WHERE key = :key ORDER BY kind"),
                            {"key": str(event.title_id)},
                        )
                    ).all()
                ]
        self.seen.append((event.kind, committed))
        self.jobs_seen.append(jobs)
        await self._inner.publish(event)


@pytest_asyncio.fixture
async def worker(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], bus: InMemoryEventBus
) -> AsyncIterator[tuple[LaneSupervisor, _CommittedStateProbe]]:
    """A real worker lane over a fake metadata provider, publishing into the
    app's own bus.

    The same `LaneSupervisor` the server's lifespan builds, wired by the
    same `unit_of_work` -- what differs is the provider (no network) and
    that this one is started by the test rather than by the lifespan.
    """
    probe = _CommittedStateProbe(bus, sessions)
    lane_settings = settings.model_copy(update={"worker_enabled": True})
    provider = FakeMetadataProvider()
    supervisor = LaneSupervisor(
        lane_settings,
        unit_of_work(sessions, lane_settings, events=probe, provider=provider),
        probe,
        user_id=DefaultUserId(sessions),
        provider=provider,
        idle_seconds=0.05,
    )
    try:
        yield supervisor, probe
    finally:
        await supervisor.stop()


async def _given_stub(sessions: async_sessionmaker[AsyncSession], name: str) -> Title:
    title = Title(
        kind=TitleKind.MOVIE,
        tmdb_id=TMDB_ID,
        name=name,
        sort_name=f"{MARK} {name}",
        year=1988,
        enrichment_state=EnrichmentState.STUB,
    )
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title


async def _read_frame(lines: AsyncIterator[str]) -> str:
    """Read until a blank line, skipping heartbeat comments."""
    collected: list[str] = []
    while True:
        line = await anext(lines)
        if line.startswith(":"):
            continue
        if line == "":
            if collected:
                return "\n".join(collected)
            continue
        collected.append(line)


async def _wait_for_subscriber(bus: InMemoryEventBus, *, count: int = 1) -> None:
    """The route subscribes inside its response generator, so the
    subscription lands when the first chunk is produced rather than when the
    request returns. Publishing before it lands is a publish to nobody, and
    this project has already had one concurrency case time out on exactly
    that harness bug rather than on the code it was written for.

    `count` is for the two-subscriber case below, where waiting for *one*
    would let the bootstrap start with the filtered stream not yet attached
    -- and "the filtered subscriber saw nothing" would then be true for the
    wrong reason, which is the failure that case's liveness control exists
    to make impossible."""
    for _ in range(400):
        if bus.subscribers >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(
        f"fewer than {count} subscribers appeared on the bus; this case would measure nothing"
    )


async def _job_xmin(sessions: async_sessionmaker[AsyncSession], key: uuid.UUID) -> str | None:
    """The row version. `xmin` is the transaction that last wrote this row,
    so an unchanged one is proof no new row version was created -- which a
    `SELECT priority` cannot show, since a rewrite to the same value reads
    identically."""
    async with sessions() as session:
        return (
            await session.execute(
                text("SELECT xmin::text FROM jobs WHERE kind = 'enrich' AND key = :key"),
                {"key": str(key)},
            )
        ).scalar_one_or_none()


async def test_opening_a_stub_promotes_it_and_the_client_is_told_when_it_lands(
    client: httpx.AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    bus: InMemoryEventBus,
    worker: tuple[LaneSupervisor, _CommittedStateProbe],
) -> None:
    """**The loop PRD 03 diagrams, end to end, in one case.**

    A client opens a stub and gets it immediately; the open promotes its
    enrichment to `DEMAND`; a worker lane in this process claims it, enriches
    it, commits, and publishes; the client is told on the SSE stream it
    already had open; and the refetch that the notice provokes reads the
    enriched row.

    The stream is opened with `?titles=`, so this also asserts the filter a
    real client would use rather than an unfiltered firehose: PRD 07's detail
    screen subscribes to one title.

    **This case is also the one that found the heartbeat defect**, and it is
    the reason a case that looks like an end-to-end demonstration is worth
    its cost. The enrichment takes long enough for several
    `sse_heartbeat_seconds` to elapse, and the route used to cancel its own
    pending `__anext__` on each one -- which closes the async generator, so
    the stream ended before the event it was waiting for ever arrived. It
    fails against that route today.
    """
    supervisor, probe = worker
    stub = await _given_stub(sessions, "A Film")

    async with client.stream("GET", f"/events?titles={stub.id}") as stream:
        lines = aiter(stream.aiter_lines())
        await _wait_for_subscriber(bus)

        opened = await client.get(f"/titles/{stub.id}")
        assert opened.status_code == 200
        assert opened.json()["enrichment_state"] == "stub"
        async with sessions() as session:
            priority = (
                await session.execute(
                    text("SELECT priority FROM jobs WHERE kind = 'enrich' AND key = :key"),
                    {"key": str(stub.id)},
                )
            ).scalar_one_or_none()
        assert priority == 100, "the open did not promote to DEMAND, or did not commit"

        await supervisor.start()
        frame = await asyncio.wait_for(_read_frame(lines), timeout=BOUND)
        assert "event: title.updated" in frame
        assert str(stub.id) in frame

        refetched = await client.get(f"/titles/{stub.id}")

    # **The deterministic half first**, so the failure that gets reported is
    # the structural one rather than the racy one. Measured: with the
    # publish moved before the commit, the refetch below *also* fails here
    # -- but only because this probe's own database round trip suspends the
    # lane between the two, which is an accident of the harness rather than
    # a property of the code. On a host where the commit won that race, the
    # refetch would be green and this line would still be red.
    # **The positive control, before any claim is read out of the probe.** A
    # publisher that never ran records nothing, and every assertion about
    # what it saw then passes vacuously -- `[] == []`. Measured while writing
    # ADR-0033: the sibling harness for `push._apply_items` recorded exactly
    # that, because the fixture had seeded no title the match ladder could
    # find, and read as a result it would have said "the availability event
    # publishes nothing".
    assert probe.seen, "the probe recorded no publish at all; nothing below measures anything"
    assert probe.seen == [(ClientEventKind.TITLE_UPDATED, "enriched")], (
        "at the instant of the publish, another connection could not yet see the "
        "enrichment -- the publish is happening before the commit"
    )
    assert refetched.json()["enrichment_state"] == "enriched", (
        "the client was told before the enrichment committed"
    )
    # **The residual window, closed.** ADR-0033 measured its exact contents
    # -- the two `BACKFILL` requests `enrich.py:270-277` stages and the
    # `DELETE` that completes the job -- and G2 made the ordering a property
    # of `JobWorker` rather than of each handler, so the frame is offered
    # after `complete(job.id)` and its commit. This line read
    # `[[("enrich", "running")]]` until then: the handler's own claim and
    # neither of the jobs it had just enqueued.
    #
    # **This is the whole of what the change bought, on the wire.** Every
    # write the unit of work made is committed before the client hears about
    # it, so a client acting on the frame -- `?titles=` says refetch -- reads
    # a catalog with no half-finished job in it. It is also the assertion
    # that would go red first if a future `_run` flushed early, because the
    # `enrich` row reappears as `running` the instant the flush moves back
    # inside the window.
    assert probe.jobs_seen == [[("derive", "pending"), ("index", "pending")]], (
        "at the instant of the frame every write the job made should be committed -- "
        "the two BACKFILL enqueues visible and the claim gone with the DELETE"
    )
    # And the job is gone rather than parked: a lane that "completed" by
    # failing would still have published nothing, but a lane that published
    # and then parked would leave a client told about work that did not land.
    #
    # **A single read, and that is G1's bounded poll retired rather than
    # merely tidied.** `_job_xmin_settles` existed because the client was
    # told strictly before the completing commit, so this assertion raced it
    # -- 6 failures in 13 runs unplanted, 5 of 5 with a 0.25 s delay planted
    # between the handler returning and `complete()`. The frame the test
    # already read above is now offered *after* that commit, so the state is
    # committed before the reader can reach this line and there is nothing
    # left to wait for. Restoring the poll would hide exactly the regression
    # the line above catches.
    assert await _job_xmin(sessions, stub.id) is None


async def test_a_second_open_writes_no_row(
    client: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """M4's `WHERE jobs.priority < excluded.priority`, called from a client
    for the first time.

    A detail screen a user opens twice must not cost a row version.
    Asserted on `xmin` rather than on the stored priority, because a rewrite
    to the same value is invisible to a `SELECT`: `enqueue` answering "1 row
    written" for the second open is both a wrong number and, at a nightly
    walk's scale, 1,126,674 dead row versions a night. `FakeJobQueue` counts
    that same re-enqueue as a write, so no unit case can see this.
    """
    stub = await _given_stub(sessions, "A Twice-Opened Film")

    await client.get(f"/titles/{stub.id}")
    before = await _job_xmin(sessions, stub.id)
    await client.get(f"/titles/{stub.id}")

    assert before is not None
    assert await _job_xmin(sessions, stub.id) == before, "the second open rewrote the job row"


async def test_a_slow_client_is_told_to_resync_and_the_publisher_is_unaffected(
    client: httpx.AsyncClient, bus: InMemoryEventBus, settings: Settings
) -> None:
    """PRD 07's one in-stream failure vocabulary, delivered as a real SSE
    frame down a real response body.

    A client that opens the stream and does not read it fills its queue. The
    publisher must finish anyway -- `EnrichService` completing a title at
    04:00 may not wait on a browser tab that closed hours ago -- and the
    client must be *told* rather than left quietly stale.

    The burst is deliberately tight and unawaited between publishes:
    `InMemoryEventBus.publish` never suspends (pinned by driving the
    coroutine one step by hand in `tests/unit/test_services_events.py`), so
    the route's generator cannot drain the queue mid-burst and the overflow
    is deterministic rather than a race. A publish that started awaiting
    would show up here as no `resync_required` at all.
    """
    async with client.stream("GET", "/events") as stream:
        lines = aiter(stream.aiter_lines())
        await _wait_for_subscriber(bus)

        started = time.perf_counter()
        for index in range(settings.sse_queue_size * 4):
            await bus.publish(ClientEvent(kind=ClientEventKind.SYNC_PROGRESS, data={"n": index}))
        elapsed = time.perf_counter() - started

        # **One frame, and that is the assertion.** An overflowed subscriber
        # has its queue emptied and one `resync_required` put in its place,
        # so the very next thing this client can read is that notice and
        # nothing else -- reading a second would wait forever on a stream
        # that is (correctly) sending only heartbeats.
        frame = await asyncio.wait_for(_read_frame(lines), timeout=BOUND)

    assert "event: resync_required" in frame, frame
    assert '"reason":"buffer_overflow"' in frame, frame
    # The publisher was not slowed by the subscriber that stopped reading.
    # A weak bound on purpose -- the guarantee is asserted on measured
    # intervals in `tests/contract/event_publisher_contract.py` and on a
    # hand-driven coroutine in `tests/unit/test_services_events.py`; what
    # this adds is that it stays true with a real response body attached to
    # the other end.
    assert elapsed < 1.0, f"{settings.sse_queue_size * 4} publishes took {elapsed:.3f}s"


async def test_a_bootstrap_batch_reaches_an_unfiltered_subscriber_and_never_a_filtered_one(
    client: httpx.AsyncClient,
    bus: InMemoryEventBus,
    sessions: async_sessionmaker[AsyncSession],
    postgres_url: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bootstrap.progress` on the wire: the row PRD 07's SSE table carried
    with no milestone against it until M9's E7.

    **Both arms, because "the filtered subscriber saw nothing" is also what a
    dead subscriber produces.** The filtered stream is proved live by a
    `title.updated` published after the bootstrap, carrying the title it
    subscribed to -- without that control this case passes against a route
    that never subscribed, a bus that dropped everything, and a filter that
    rejects every frame.

    **Two batches, not one.** `bulk_batch_size=2` over the committed
    five-row IMDb slice gives three, which is what distinguishes one frame
    per *batch* from one per *run* -- the `0% to 100%` failure
    `ReconcileService._publish_progress` already names for `sync.progress`.
    The frames are read in order and their cursors must ascend, which is the
    half a set-membership assertion would miss.

    Driven through `composition.run_bootstrap` rather than through
    `BootstrapService`, so the publisher this case observes is the one the
    shared dispatch really constructs. Nothing downloads: the same
    `MockTransport` handler `tests/integration/test_admin_bootstrap.py` uses,
    over the same committed synthetic slice.
    """
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    fixtures = pathlib.Path(__file__).parent.parent / "fixtures" / "bulk"
    for source, name in (
        ("title.basics.slice.tsv", "title.basics.tsv.gz"),
        ("title.ratings.slice.tsv", "title.ratings.tsv.gz"),
    ):
        (cache / name).write_bytes(gzip.compress((fixtures / source).read_bytes()))

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    watched = uuid.uuid4()

    try:
        async with (
            client.stream("GET", "/events") as everything,
            client.stream("GET", f"/events?titles={watched}") as detail,
        ):
            unfiltered = aiter(everything.aiter_lines())
            filtered = aiter(detail.aiter_lines())
            await _wait_for_subscriber(bus, count=2)

            async with sessions() as session:
                await run_bootstrap(
                    PostgresBulkCatalogRepository(session),
                    PostgresImportRunRepository(session),
                    session.commit,
                    Settings(
                        database_url=postgres_url,
                        secret_key=SECRET_KEY,
                        bulk_data_dir=cache,
                        bulk_batch_size=2,
                    ),
                    BootstrapPhase.IMDB,
                    report=lambda _: None,
                    events=bus,
                )

            first = await asyncio.wait_for(_read_frame(unfiltered), timeout=BOUND)
            second = await asyncio.wait_for(_read_frame(unfiltered), timeout=BOUND)

            # The liveness control for the filtered arm, published *after*
            # the bootstrap so the frames it must not have seen are already
            # behind it in the same stream's ordering.
            await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, title_id=watched))
            control = await asyncio.wait_for(_read_frame(filtered), timeout=BOUND)
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(text("DELETE FROM titles WHERE imdb_id LIKE 'tt99%'"))
            await cleanup.execute(text("DELETE FROM import_runs WHERE dataset LIKE 'imdb.title.%'"))
            await cleanup.commit()

    assert "event: bootstrap.progress" in first, first
    assert "event: bootstrap.progress" in second, second
    opening = json.loads(first.split("\n")[2].removeprefix("data: "))
    following = json.loads(second.split("\n")[2].removeprefix("data: "))
    assert opening["dataset"] == "imdb.title.basics"
    assert opening["phase"] == "imdb"
    assert "title_id" not in opening, "an admin frame with a title id is a detail screen's problem"
    assert following["position"] > opening["position"], (
        "the premise: two batches, so this is one frame per batch and not one per run"
    )
    assert following["rows_seen"] > opening["rows_seen"]

    # The filtered subscriber's very first frame is the control, which is
    # only true if it received none of the bootstrap frames before it.
    assert "event: title.updated" in control, control
    assert str(watched) in control
