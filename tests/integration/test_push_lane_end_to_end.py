"""The push lane, whole: a socket's event into catalog state and out to a
client, against real Postgres -- and what it costs.

**What is here that is not in `tests/integration/test_services_push.py`.**
That file drives `PushApplyService` directly and owns the two properties
only Postgres can express: the `observed_at` a merge must carry
(`trg_watch_states_set_updated_at` owns `updated_at`, so a push stamped with
anything earlier writes nothing at all) and ADR-0014's `COALESCE` on both
columns (`play_count` **and** `last_played_at`, because the nullable one
survives the wrong statement and a case checking only the timestamp would
ratify the bug). Neither is repeated here.

What is left is the composition: `PushSupervisor`'s own loop driving
`PushApplyService` into real repositories and out through the **real**
`InMemoryEventBus` to a real subscriber, the real `PostgresJobQueue` behind
the backfill's `(kind, key)` uniqueness, and the two measurements shaped so
a quadratic would show.

**The measurements, and what each holds fixed.** M4's lesson is that "a
statement-count assertion needs the right thing held fixed", so the
database half holds the **event count** fixed at 20 and varies the items per
event (1, then 10) -- a per-item round trip inside an event is the candidate
defect, and at 1,126,789 items on the one measured source a
`UserDataChanged` naming a thousand of them is an ordinary afternoon. The
bus half holds the **event count** fixed and varies the subscriber count at
two points far enough apart to tell linear from quadratic, which one point
cannot.

This module runs inside the integration fixture's rolled-back transaction,
so unlike the three other files this task adds it commits nothing and leaks
no `stg_*` table.
"""

import asyncio
import time
import uuid
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.source_adapter import FakeSourceAdapter
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.events import ClientEvent, ClientEventKind
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import SourceEvent, SourceEventKind, SourceWatchState
from usher.services.events import InMemoryEventBus
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.push import PushApplyService, PushSupervisor
from usher.services.watch_sync import WatchStateSyncService

SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
# Every await here is bounded. A lane's failure mode is a hang, and a hang
# in a mutation sweep reads as a mutation nothing observed rather than as
# one everything caught.
BOUND = 10.0


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> Source:
    row = Source(
        kind=SourceKind.EMBY,
        name="Push Lane Emby",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    await PostgresSourceRepository(session).add(row)
    return row


@pytest_asyncio.fixture
async def user_id(session: AsyncSession) -> uuid.UUID:
    identifier = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (:id, :name)"),
        {"id": identifier, "name": f"user-{identifier}"},
    )
    return identifier


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def queue(session: AsyncSession) -> PostgresJobQueue:
    return PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0)


@pytest.fixture
def applier(
    session: AsyncSession, bus: InMemoryEventBus, queue: PostgresJobQueue
) -> PushApplyService:
    """The real chain M4 owns, on real repositories, publishing to the real
    bus. `session.flush`, not `commit`: the integration fixture owns one
    connection-bound transaction it rolls back, and what is under test is
    the SQL and the fan-out rather than durability."""
    media_items = PostgresMediaItemRepository(session)
    matching = PostgresTitleMatchRepository(session)
    return PushApplyService(
        IngestService(
            matcher=MatchService(
                titles=PostgresTitleRepository(session), matching=matching, queue=queue
            ),
            matching=matching,
            media_items=media_items,
            episodes=PostgresEpisodeRepository(session),
            queue=queue,
        ),
        WatchStateSyncService(
            media_items=media_items,
            watch_states=PostgresWatchStateRepository(session),
            runs=PostgresSyncRunRepository(session),
            queue=queue,
            commit=session.flush,
        ),
        bus,
        session.flush,
    )


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every statement SQLAlchemy issues, captured off
    `before_cursor_execute` rather than transcribed -- M4 replaced two tasks
    that asserted on a hand-copied lookalike, because the copy drifts from
    the repository and then reads like coverage. A `COPY` is invisible here
    (it runs on the raw asyncpg connection), which is the point: it is one
    command however many records stream through it."""
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


async def _given_matched_movie(
    session: AsyncSession, source: Source, external_id: str
) -> uuid.UUID:
    title = Title(kind=TitleKind.MOVIE, name=f"Film {external_id}", sort_name=f"Film {external_id}")
    await PostgresTitleRepository(session).add(title)
    await PostgresMediaItemRepository(session).upsert_many(
        [
            MediaItemUpsert(
                source_id=source.id,
                external_id=external_id,
                title_id=title.id,
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
                added_at=None,
                last_seen_at=SEEN_AT,
            )
        ]
    )
    return title.id


def _watch_event(*states: SourceWatchState) -> SourceEvent:
    return SourceEvent(
        kind=SourceEventKind.WATCH_STATE_CHANGED,
        external_ids=tuple(state.external_id for state in states),
        watch_states=states,
    )


async def test_a_pushed_watch_state_lands_and_is_published(
    session: AsyncSession,
    applier: PushApplyService,
    bus: InMemoryEventBus,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """**The milestone in one case, through the lane's own loop.**

    A `PushSupervisor` holds a channel, an event arrives on it, and the
    position lands in `watch_states` *and* reaches a subscriber on the bus
    the SSE route reads from -- with the event's `title_id` on it, which is
    the field a client filters by and the one the plan's own self-review
    found being paired wrongly.

    The supervisor is the real one: the loop, the `_note` transition, the
    gap gate and the failure counter all run. What is faked is the adapter
    (a socket is a network request) and the commit (this suite's transaction
    is rolled back).
    """
    title_id = await _given_matched_movie(session, source, "movie-1")
    adapter = FakeSourceAdapter(source)
    gaps: list[object] = []
    availability: list[object] = []
    supervisor = PushSupervisor(
        lambda pushed_source, pushed_adapter, pushed_event: applier.apply(
            pushed_source, pushed_adapter, pushed_event, user_id=user_id
        ),
        lambda pushed_source, pushed_adapter: _record(gaps, pushed_source.name),
        lambda pushed_source, available: _record(availability, available),
        max_consecutive_failures=1,
    )

    async with bus.subscribe() as stream:
        subscribed = aiter(stream)
        lane = asyncio.create_task(supervisor.run(source, adapter))
        try:
            adapter.push(
                _watch_event(
                    SourceWatchState(external_id="movie-1", position_seconds=612, played=False)
                )
            )
            # **Three frames, not one, since M7.** The lane publishes one
            # `row.invalidated` per row a watch state can move and then the
            # `watchstate.updated`, and the whole sequence is read rather than
            # searched for: a loop that read *until* it found a watch-state
            # event would pass against a lane that published forty row
            # invalidations first, which is precisely the fan-out trap 5 is
            # about. Bounded by `BOUND`, so a lane that publishes fewer fails
            # here rather than hanging.
            published = [await asyncio.wait_for(anext(subscribed), timeout=BOUND) for _ in range(3)]
        finally:
            lane.cancel()
            await asyncio.gather(lane, return_exceptions=True)

    assert [sent.event.kind for sent in published] == [
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.WATCHSTATE_UPDATED,
    ]
    # The row invalidations reach the bus even though this lane holds **no
    # `RowCache`**: the event is a client contract and the cache is a
    # server-side optimisation, so a deployment that cached nothing would still
    # tell its clients what to refetch.
    assert [sent.event.data["slug"] for sent in published[:2]] == ["continue-watching", "next-up"]
    assert published[2].event.title_id == title_id
    assert published[2].event.data["position_seconds"] == 612
    stored = await PostgresWatchStateRepository(session).get_for_title(user_id, title_id)
    assert stored is not None
    assert stored.position_seconds == 612
    # The gap-closing delta ran once, on connect, before any event -- PRD
    # 03's ordering, from the lane rather than from a unit fake.
    assert gaps == ["Push Lane Emby"]


async def _record(sink: list[object], value: object) -> None:
    sink.append(value)


async def test_a_pushed_played_item_enqueues_exactly_one_history_backfill(
    session: AsyncSession,
    applier: PushApplyService,
    queue: PostgresJobQueue,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """`(kind, key)` is unique, so a film paused and resumed six times is
    **one** `watch_history` job rather than six.

    A `UserDataChanged` entry carries no play history anybody has measured,
    so ADR-0014 makes the adapter report `play_count=None` and every pushed
    play event arrives needing a backfill. At a household's viewing rate the
    difference is a queue that stays small against one that grows with
    playback -- and `FakeJobQueue` cannot show it, because the constraint
    that collapses the six is a real unique index rather than a dict key
    that happens to match.
    """
    await _given_matched_movie(session, source, "movie-1")
    adapter = FakeSourceAdapter(source)

    for position in (60, 120, 180, 240, 300, 360):
        await applier.apply(
            source,
            adapter,
            _watch_event(
                SourceWatchState(
                    external_id="movie-1",
                    position_seconds=position,
                    played=True,
                    play_count=None,
                )
            ),
            user_id=user_id,
        )

    claimed = await queue.claim([JobKind.WATCH_HISTORY], limit=10)
    assert [job.key for job in claimed] == ["movie-1"]


async def test_the_push_lanes_cost_per_event_does_not_grow_with_the_items_in_it(
    session: AsyncSession,
    applier: PushApplyService,
    statement_counter: list[str],
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """**The measurement shaped so a quadratic would show.**

    The candidate defect is a per-item database round trip inside an event,
    so the **event count** is held fixed at 20 and the items per event are
    varied (1, then 10): twenty events costing 20k statements either way is
    the property, and "the cost per item is small" is not. `apply_states`
    resolves a whole batch in one `resolve_targets` and merges it in one
    `merge_from_source`, so ten items in one event must cost exactly what
    one does.

    Holding the *items* fixed and growing the event count instead would
    measure nothing: that is supposed to grow.

    `played=False` throughout, deliberately. A played item with no count
    enqueues a `watch_history` backfill, and `PostgresJobQueue.enqueue`
    stages through DDL -- which is a real cost, measured by the case above
    it, and which would swamp the signal this case is looking for.
    """
    many = [f"movie-{index}" for index in range(10)]
    for external_id in many:
        await _given_matched_movie(session, source, external_id)
    adapter = FakeSourceAdapter(source)

    def _state(external_id: str, position: int) -> SourceWatchState:
        return SourceWatchState(external_id=external_id, position_seconds=position, played=False)

    # Warm: both walks below run against rows that already exist, so what is
    # measured is the merge rather than the insert.
    await applier.apply(
        source, adapter, _watch_event(*(_state(one, 1) for one in many)), user_id=user_id
    )

    statement_counter.clear()
    for round_number in range(20):
        await applier.apply(
            source, adapter, _watch_event(_state("movie-0", 100 + round_number)), user_id=user_id
        )
    one_item_each = len(statement_counter)

    statement_counter.clear()
    for round_number in range(20):
        await applier.apply(
            source,
            adapter,
            _watch_event(*(_state(one, 200 + round_number) for one in many)),
            user_id=user_id,
        )
    ten_items_each = len(statement_counter)

    assert one_item_each == ten_items_each, (
        f"{one_item_each} statements for 20 events of 1 item, {ten_items_each} for 20 "
        "events of 10 -- something in the push lane costs a statement per item"
    )
    # **And the level, because flatness alone hides what this found.** Nine
    # statements per event, measured: one `resolve_targets`, then `SAVEPOINT`
    # / `DROP TABLE IF EXISTS pg_temp.stg_watch_states` / `CREATE TEMP TABLE
    # stg_watch_states` / four merge statements (an `UPDATE ... FROM` and an
    # `INSERT ... ON CONFLICT DO NOTHING` per conflict target, title and
    # episode) / `RELEASE SAVEPOINT`, plus a `COPY` this counter cannot see.
    #
    # So **a push event costs staging DDL**, and a `UserDataChanged` arriving
    # once a second during playback pays it every time. Bounded per event
    # rather than growing with anything, which is why the count is recorded
    # here rather than optimised. The *contention* half of this note is
    # settled: `stg_watch_states` used to be a fixed, shared name taking an
    # `ACCESS EXCLUSIVE` lock, so the push lane and a nightly watch-state
    # walk serialised against each other for the length of each other's
    # batch; M6 made every staging table `CREATE TEMP TABLE ... ON COMMIT
    # DROP`, so there is no shared name left to serialise on. Same fix
    # `tests/integration/test_titles_route.py` records for `stg_jobs` on the
    # read path; this is the write path's copy of it.
    assert one_item_each == 20 * 9, (
        f"{one_item_each / 20} statements per event against the nine measured "
        "2026-08-01: one resolve, four merge statements, and four of staging"
    )


async def test_the_sse_fan_out_stays_linear_in_the_subscriber_count() -> None:
    """The bus half of the same question, measured at **two** points.

    One publish is O(subscribers) by construction -- that is what a fan-out
    is -- so the plan's shape ("50 subscribers within 10x of 1") is not the
    claim: it compares a point dominated by fixed cost with one dominated by
    fan-out, and a correct implementation can legitimately fail it. What
    must not grow is the work *per subscriber*, so this measures the same
    burst at 25 and at 200 subscribers and compares the ratio against what
    each shape predicts: **8x if the per-subscriber cost is flat, ~64x if
    the fan-out is quadratic in subscribers.**

    Both ends are measured rather than predicted, on this host,
    2026-08-01: **6.0x, 6.3x, 6.2x** over three rounds as shipped (below
    8x, because a fixed per-publish cost dilutes the fan-out at the small
    end), against **25.6x** for a `publish` given an artificial O(S)
    check per subscriber. The bound sits between the two with a 2.4x
    margin below and a 1.7x margin above, and the failure message carries
    the numbers rather than a verdict. A ratio is also robust to a host
    that is uniformly slow, which a wall-clock threshold is not.

    **What this cannot see, stated rather than implied:** work proportional
    to the *replay ring* done once per subscriber. That is O(subscribers x
    ring), which is still linear in subscribers, so both points scale
    together and the ratio is unchanged. It is also the more plausible
    defect of the two, and what rules it out is `publish` being a
    `put_nowait` and a branch -- pinned by driving the coroutine one step by
    hand in `tests/unit/test_services_events.py`, and by the interval
    measurement in `tests/contract/event_publisher_contract.py`. A
    wall-clock ratio is the weakest of the three and is here for the one
    thing the other two cannot express.
    """
    events = 200
    small, large = 25, 200

    async def burst(subscribers: int) -> float:
        # A queue big enough to hold the whole burst: an overflowed
        # subscriber's `offer` returns immediately, which would make the
        # *expensive* case look cheap.
        bus = InMemoryEventBus(buffer_size=events + 8, queue_size=events + 8)
        async with AsyncExitStack() as stack:
            for _ in range(subscribers):
                await stack.enter_async_context(bus.subscribe())
            assert bus.subscribers == subscribers
            started = time.perf_counter()
            for index in range(events):
                await bus.publish(
                    ClientEvent(kind=ClientEventKind.SYNC_PROGRESS, data={"n": index})
                )
            return time.perf_counter() - started
        raise AssertionError("unreachable")  # pragma: no cover

    # The minimum of three rounds each: a scheduler hiccup can only ever
    # make a run slower, so the minimum is the least noisy estimator
    # available without a quiet host.
    at_small = min([await burst(small) for _ in range(3)])
    at_large = min([await burst(large) for _ in range(3)])

    ratio = at_large / at_small
    assert ratio < 15.0, (
        f"{large} subscribers cost {ratio:.1f}x what {small} did "
        f"({at_large * 1000:.2f} ms against {at_small * 1000:.2f} ms for {events} "
        f"publishes). Linear in subscribers predicts {large / small:.0f}x; "
        f"quadratic predicts {(large / small) ** 2:.0f}x."
    )
