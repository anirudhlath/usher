"""`WatchStateSyncService` against real Postgres, for the three things its
port fakes structurally cannot express.

1. **The `COALESCE`, one batch wide.** `FakeWatchStateRepository` spells the
   rule as `value if value is not None else stored`, which is naturally
   right and cannot fail. In SQL it is not: `watch_states.play_count` is
   `NOT NULL`, so the natural one-statement merge collapses the absent count
   to `0` *before* the conflict clause can read it and writes that zero over
   real history -- measured at 7 -> 0. The unit suite would ratify it. Here a
   batch carries four absent counts and one reported zero through one
   `merge_from_source`, which is the shape a walk actually produces and the
   only shape that shows the distinction is per row rather than per
   statement.
2. **`backfill_one`'s `observed_at`.** The fake stores `observed_at` as
   `updated_at`; Postgres has a `BEFORE UPDATE` trigger that overwrites it
   with the write instant. So against the fake a backfill carrying a stale
   instant is accepted and the case passes; against Postgres the conflict
   rule refuses it, the play count never lands, and the row keeps matching
   `played AND play_count = 0` forever. The row below is inserted with
   `clock_timestamp()` -- through raw SQL, because the trigger is `BEFORE
   UPDATE` and an `INSERT` is the only way to give the column a value of
   one's own -- which is exactly the state a production walk leaves behind.
3. **Foreign keys.** An episode's watch state has to name a real `episodes`
   row, and `watch_states` has a `num_nonnulls(title_id, episode_id) = 1`
   CHECK. A dict has neither, so "the episode wins over its series' title"
   is a preference there and a constraint here.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic import AwareDatetime
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.source_adapter import FakeSourceAdapter
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import SyncRunStatus
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState
from usher.services.watch_sync import WatchStateSyncService

RUN_AT = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
LAST_PLAYED = datetime(2026, 6, 30, 21, 14, tzinfo=UTC)
# What the fake adapter records as an item's change instant, and why it is
# absurd. A second `sync` resumes from the first's `started_at`, which is a
# wall-clock instant taken during the test -- so anything seeded with a
# plausible date is filtered out of the second walk by the adapter's own
# `changed_at < since` rule, exactly as `MinDateLastSavedForUser` would.
# Same device `tests/unit/test_services_reconcile.py` uses, for the same
# reason.
CHANGED_AT = datetime(2099, 1, 1, tzinfo=UTC)


class _LossyAdapter(FakeSourceAdapter):
    """Emby 4.9.5.0's measured asymmetry: the listing route cannot report
    play history, the single-item route can.

    `blind_to` names the ids whose history the *walk* drops. Everything not
    listed is reported as seeded, so one walk can carry an absent count and
    a genuinely-reported zero in the same batch -- which is the only way to
    show that the merge distinguishes them per row rather than per
    statement.
    """

    def __init__(self, source: Source, blind_to: set[str] | None = None) -> None:
        super().__init__(source)
        self._blind_to = blind_to

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        async for state in super()._walk_states(since, start_index):
            if self._blind_to is None or state.external_id in self._blind_to:
                yield dataclasses.replace(state, play_count=None, last_played_at=None)
            else:
                yield state


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> Source:
    row = Source(
        kind=SourceKind.EMBY,
        name="Watch State Source",
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
def watch_states(session: AsyncSession) -> PostgresWatchStateRepository:
    return PostgresWatchStateRepository(session)


@pytest.fixture
def media_items(session: AsyncSession) -> PostgresMediaItemRepository:
    return PostgresMediaItemRepository(session)


@pytest.fixture
def queue() -> FakeJobQueue:
    """The queue is not under test here and `PostgresJobQueue` would only
    add statements to the counts below; its own contract runs against real
    Postgres in `tests/integration/test_job_queue.py`."""
    return FakeJobQueue()


@pytest.fixture
def service(
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    queue: FakeJobQueue,
) -> WatchStateSyncService:
    return WatchStateSyncService(
        media_items=media_items,
        watch_states=watch_states,
        runs=PostgresSyncRunRepository(session),
        queue=queue,
        # `session.flush`, not `session.commit`: the integration fixture owns
        # one connection-bound transaction it rolls back, which is what makes
        # each test isolated. What is under test is the ordering of the
        # writes and the SQL they produce, not their durability.
        commit=session.flush,
        batch_size=1_000,
    )


async def _given_matched_movie(
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    source: Source,
    external_id: str,
) -> uuid.UUID:
    title = Title(kind=TitleKind.MOVIE, name=f"Film {external_id}", sort_name=f"Film {external_id}")
    await PostgresTitleRepository(session).add(title)
    await media_items.upsert_many([_upsert(source.id, external_id, title_id=title.id)])
    return title.id


def _upsert(
    source_id: uuid.UUID,
    external_id: str,
    *,
    title_id: uuid.UUID | None = None,
    episode_id: uuid.UUID | None = None,
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=episode_id,
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
        last_seen_at=RUN_AT,
    )


def _item(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"Film {external_id}",
        kind=SourceItemKind.MOVIE,
        year=2021,
    )


async def _given_stored_history(
    session: AsyncSession, user_id: uuid.UUID, title_id: uuid.UUID, play_count: int
) -> None:
    """A row as a backfill would have left it, written with raw SQL.

    Not through `merge_from_source`: the point of every case below is what
    happens to a row whose `updated_at` is its *write* instant, and only an
    `INSERT` can set that column directly -- the `BEFORE UPDATE` trigger
    owns it on every other path. `clock_timestamp()`, never `now()`, because
    `now()` is frozen at the transaction's start and the whole suite runs
    inside one transaction.
    """
    await session.execute(
        text(
            """
            INSERT INTO watch_states (
                id, user_id, title_id, position_seconds, played, play_count,
                last_played_at, origin, updated_at
            ) VALUES (
                :id, :user_id, :title_id, 0, true, :play_count,
                :last_played_at, 'source', clock_timestamp()
            )
            """
        ),
        {
            "id": new_id(),
            "user_id": user_id,
            "title_id": title_id,
            "play_count": play_count,
            "last_played_at": LAST_PLAYED,
        },
    )


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
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


async def test_a_batch_from_a_walk_zeroes_no_stored_play_count(
    session: AsyncSession,
    service: WatchStateSyncService,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """**The milestone's central question, at the layer where the answer is
    permanent.** Four rows holding real history, one walk, one
    `merge_from_source`, every count absent on the wire. The natural
    one-statement spelling of that merge reads every one of them back as
    `0`.

    The position assertions are not decoration: a merge that wrote nothing
    at all would satisfy the counts and fail these, which is the other way
    this case could pass for the wrong reason.
    """
    adapter = _LossyAdapter(source)
    counts = {"movie-1": 7, "movie-2": 3, "movie-3": 1, "movie-4": 12}
    titles: dict[str, uuid.UUID] = {}
    for index, (external_id, count) in enumerate(counts.items(), start=1):
        titles[external_id] = await _given_matched_movie(session, media_items, source, external_id)
        await _given_stored_history(session, user_id, titles[external_id], count)
        adapter.seed(_item(external_id), CHANGED_AT)
        adapter.seed_state(
            SourceWatchState(
                external_id=external_id,
                position_seconds=100 * index,
                played=True,
                play_count=count,
                last_played_at=LAST_PLAYED,
            )
        )

    run = await service.sync(source, adapter, user_id=user_id)
    assert run.status is SyncRunStatus.COMPLETED
    assert (run.items_seen, run.items_matched) == (4, 4)

    for index, (external_id, count) in enumerate(counts.items(), start=1):
        stored = await watch_states.get_for_title(user_id, titles[external_id])
        assert stored is not None
        assert stored.play_count == count, f"{external_id} lost its play count"
        assert stored.last_played_at == LAST_PLAYED, f"{external_id} lost its last played date"
        assert stored.position_seconds == 100 * index, "the merge never ran"


async def test_one_batch_keeps_an_absent_count_and_writes_a_reported_zero(
    session: AsyncSession,
    service: WatchStateSyncService,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """The two halves of ADR-0014 in one statement, which is where a
    per-statement fix passes and a per-row one is required.

    "Never write a count from a merge" preserves the 7 and makes un-marking
    something played impossible to propagate; `COALESCE(count, 0)` writes
    the reset and erases the 7. Only reading each row's own value does both.
    """
    adapter = _LossyAdapter(source, blind_to={"movie-1"})
    kept = await _given_matched_movie(session, media_items, source, "movie-1")
    reset = await _given_matched_movie(session, media_items, source, "movie-2")
    await _given_stored_history(session, user_id, kept, 7)
    await _given_stored_history(session, user_id, reset, 4)
    adapter.seed(_item("movie-1"), CHANGED_AT)
    adapter.seed(_item("movie-2"), CHANGED_AT)
    adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=61, played=True, play_count=7)
    )
    adapter.seed_state(
        SourceWatchState(external_id="movie-2", position_seconds=0, played=False, play_count=0)
    )

    await service.sync(source, adapter, user_id=user_id)

    absent = await watch_states.get_for_title(user_id, kept)
    reported = await watch_states.get_for_title(user_id, reset)
    assert absent is not None and reported is not None
    assert absent.play_count == 7, "an absent count was written as zero"
    assert reported.play_count == 0, "a reported reset was not written"
    assert reported.played is False


async def test_a_backfill_writes_over_a_row_the_walk_just_wrote(
    session: AsyncSession,
    service: WatchStateSyncService,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """The conflict rule against the backfill, which the fakes cannot stage.

    The stored row carries `clock_timestamp()` as its `updated_at` -- the
    write instant, which is what the `BEFORE UPDATE` trigger leaves on every
    row a walk merges. A backfill carrying anything at or before that
    instant writes nothing at all, the row keeps matching `played AND
    play_count = 0`, and the recovery never converges. Against
    `FakeWatchStateRepository`, whose `updated_at` is whatever
    `observed_at` it was handed, the same code passes.
    """
    adapter = _LossyAdapter(source)
    title_id = await _given_matched_movie(session, media_items, source, "movie-1")
    await _given_stored_history(session, user_id, title_id, 0)
    adapter.seed(_item("movie-1"), CHANGED_AT)
    adapter.seed_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=0,
            played=True,
            play_count=9,
            last_played_at=LAST_PLAYED,
        )
    )

    assert await service.backfill_one(source, adapter, external_id="movie-1", user_id=user_id)
    stored = await watch_states.get_for_title(user_id, title_id)
    assert stored is not None
    assert stored.play_count == 9


async def test_the_backfill_sweep_drains_the_predicate(
    session: AsyncSession,
    service: WatchStateSyncService,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """Termination, against the real `played AND play_count = 0` query and
    the real reverse lookup rather than against two dicts. Five rows in, one
    bounded pass, nothing left."""
    adapter = _LossyAdapter(source)
    for index in range(5):
        external_id = f"movie-{index}"
        await _given_matched_movie(session, media_items, source, external_id)
        adapter.seed(_item(external_id), CHANGED_AT)
        adapter.seed_state(
            SourceWatchState(
                external_id=external_id, position_seconds=0, played=True, play_count=index + 1
            )
        )
    await service.sync(source, adapter, user_id=user_id)
    assert len(await watch_states.list_needing_history()) == 5

    assert await service.backfill_history(source, adapter, limit=100) == 5
    assert await watch_states.list_needing_history() == []


async def test_an_episodes_state_lands_on_a_real_episode_row(
    session: AsyncSession,
    service: WatchStateSyncService,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """`watch_states.episode_id` is a real foreign key and
    `num_nonnulls(title_id, episode_id) = 1` is a real CHECK, so the
    collapse from "what the media item is matched to" to "what a watch state
    may carry" is enforced here and merely preferred against a dict.

    Handing both ids through raises `PortDataMalformed` and fails the run;
    handing the series' title through stores 24 episodes on one row and
    passes every FK.
    """
    series = Title(kind=TitleKind.SERIES, name="Example Series", sort_name="Example Series")
    await PostgresTitleRepository(session).add(series)
    season, episode = new_id(), new_id()
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 3)"),
        {"id": season, "title_id": series.id},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :title_id, :season_id, 3, 5)"
        ),
        {"id": episode, "title_id": series.id, "season_id": season},
    )
    await media_items.upsert_many(
        [_upsert(source.id, "episode-1", title_id=series.id, episode_id=episode)]
    )
    adapter = _LossyAdapter(source)
    adapter.seed(_item("episode-1"), CHANGED_AT)
    adapter.seed_state(SourceWatchState(external_id="episode-1", position_seconds=742, played=True))

    run = await service.sync(source, adapter, user_id=user_id)
    assert run.status is SyncRunStatus.COMPLETED, run.error
    stored = await watch_states.get_for_episode(user_id, episode)
    assert stored is not None
    assert stored.position_seconds == 742
    assert await watch_states.get_for_title(user_id, series.id) is None

    # And the backfill finds its way back to the episode's own file rather
    # than to its series'.
    adapter.seed_state(
        SourceWatchState(external_id="episode-1", position_seconds=742, played=True, play_count=2)
    )
    assert await service.backfill_history(source, adapter) == 1
    recovered = await watch_states.get_for_episode(user_id, episode)
    assert recovered is not None and recovered.play_count == 2


async def test_a_batch_of_states_costs_a_bounded_number_of_statements(
    session: AsyncSession,
    service: WatchStateSyncService,
    media_items: PostgresMediaItemRepository,
    source: Source,
    user_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """The scale property, measured against real SQL rather than a fake's
    call counter: 20 states and 200 must cost the same number of statements.
    A per-state resolve or a per-state merge is 1,126,674 round trips a
    walk.

    Not an exact number -- the staged `COPY` path issues DDL and a
    `SAVEPOINT` per merge, and pinning the total would break on any
    unrelated change to `usher.db.staging`.
    """

    async def _walk(count: int, offset: int) -> int:
        adapter = _LossyAdapter(source)
        for index in range(count):
            external_id = f"e{offset + index}"
            await _given_matched_movie(session, media_items, source, external_id)
            adapter.seed(_item(external_id), CHANGED_AT)
            adapter.seed_state(
                SourceWatchState(external_id=external_id, position_seconds=index, played=False)
            )
        statement_counter.clear()
        await service.sync(source, adapter, user_id=user_id)
        return len(statement_counter)

    small = await _walk(20, 0)
    large = await _walk(200, 1000)
    assert small == large, f"{small} statements for 20 states, {large} for 200"
