"""`PushApplyService` against real Postgres, where three cases need what no port fake can express.

The three removal cases repeat unit cases against the table that holds the `available` flag.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.event_publisher import FakeEventPublisher

# The queue is a fake here deliberately: `PostgresJobQueue` has its own
# contract run against real Postgres, and no case reads what is queued. Each
# is about what the real tables do: the `watch_states` merge and its trigger,
# the foreign keys a pushed item's rows must satisfy, or the `available` flag
# a removal may clear.
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.source_adapter import FakeSourceAdapter
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.events import ClientEventKind
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import (
    SourceEvent,
    SourceEventKind,
    SourceItem,
    SourceItemKind,
    SourceWatchState,
)
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.push import PushApplyService
from usher.services.watch_sync import WatchStateSyncService

SEEN_AT = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
LAST_PLAYED = datetime(2026, 6, 30, 21, 14, tzinfo=UTC)
# What the event would carry if the lane stamped anything but `now()`. A
# plausible instant, in the past, exactly as a `UserDataChanged` frame's own
# timestamp or the last walk's `started_at` would be.
EVENT_INSTANT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> Source:
    row = Source(
        kind=SourceKind.EMBY,
        name="Push Source",
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
def events() -> FakeEventPublisher:
    return FakeEventPublisher()


@pytest.fixture
def applier(
    session: AsyncSession,
    watch_states: PostgresWatchStateRepository,
    events: FakeEventPublisher,
) -> PushApplyService:
    media_items = PostgresMediaItemRepository(session)
    titles = PostgresTitleRepository(session)
    matching = PostgresTitleMatchRepository(session)
    queue = FakeJobQueue()
    return PushApplyService(
        IngestService(
            matcher=MatchService(titles=titles, matching=matching, queue=queue),
            matching=matching,
            media_items=media_items,
            episodes=PostgresEpisodeRepository(session),
            queue=queue,
        ),
        WatchStateSyncService(
            media_items=media_items,
            watch_states=watch_states,
            runs=PostgresSyncRunRepository(session),
            queue=queue,
            commit=session.flush,
            rollback=session.rollback,
        ),
        events,
        # `session.flush`, not `session.commit`: the integration fixture owns
        # one connection-bound transaction it rolls back, which is what makes
        # each test isolated. What is under test is the SQL, not durability.
        session.flush,
        media_items=media_items,
    )


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


async def _given_stored_history(
    session: AsyncSession, user_id: uuid.UUID, title_id: uuid.UUID, play_count: int
) -> None:
    """A row as a walk plus a backfill would have left it.

    Written with raw SQL and stamped with the instant Postgres wrote it.
    `clock_timestamp()`, never `now()`: `now()` is frozen at the
    transaction's start and this whole suite is one transaction, so a row stamped
    with it is *not* later than an instant taken during the test and the refusal this
    file exists to detect would not happen.
    """
    await session.execute(
        text(
            """
            INSERT INTO watch_states (
                id, user_id, title_id, position_seconds, played, play_count,
                last_played_at, origin, updated_at
            ) VALUES (
                :id, :user_id, :title_id, 30, true, :play_count,
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


async def test_a_pushed_state_lands_on_a_row_a_walk_just_wrote(
    session: AsyncSession,
    applier: PushApplyService,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """The property no fake can hold.

    The row's stored `updated_at` is the instant Postgres wrote it, which is later
    than every timestamp the event itself could carry. A lane stamping the event's
    own instant -- or the last walk's, or a value cached when the socket opened -- is
    refused by the conflict rule and writes nothing, so the household's resume
    position never moves however many events arrive. `EVENT_INSTANT` is what such a
    lane would use; nothing here passes it, and the row is *newer* than it by
    construction.
    """
    title_id = await _given_matched_movie(session, source, "movie-1")
    await _given_stored_history(session, user_id, title_id, 7)
    assert datetime.now(UTC) > EVENT_INSTANT

    outcome = await applier.apply(
        source,
        FakeSourceAdapter(source),
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("movie-1",),
            watch_states=(
                SourceWatchState(external_id="movie-1", position_seconds=612, played=True),
            ),
        ),
        user_id=user_id,
    )
    assert outcome.states_merged == 1
    stored = await watch_states.get_for_title(user_id, title_id)
    assert stored is not None
    assert stored.position_seconds == 612, "the merge was refused as stale"


async def test_a_pushed_state_zeroes_neither_play_count_nor_last_played_at(
    session: AsyncSession,
    applier: PushApplyService,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """Absence is not zero, on the push path, where the answer is permanent.

    A `UserDataChanged` entry is a payload shape nobody here has captured, so the
    adapter reports its play history as absent -- and the natural one-statement
    spelling of this merge reads that absence back as `0`, because `play_count` is
    `NOT NULL` and the insert path's `COALESCE` runs before the conflict clause could
    see the `NULL`. **Both columns, deliberately**: `last_played_at` is nullable and
    therefore survives that same wrong statement, so a case asserting only the
    timestamp passes against the bug.
    """
    title_id = await _given_matched_movie(session, source, "movie-1")
    await _given_stored_history(session, user_id, title_id, 7)

    await applier.apply(
        source,
        FakeSourceAdapter(source),
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("movie-1",),
            watch_states=(
                SourceWatchState(
                    external_id="movie-1",
                    position_seconds=0,
                    played=True,
                    play_count=None,
                    last_played_at=None,
                ),
            ),
        ),
        user_id=user_id,
    )
    stored = await watch_states.get_for_title(user_id, title_id)
    assert stored is not None
    assert stored.play_count == 7
    assert stored.last_played_at == LAST_PLAYED


async def test_a_pushed_item_is_ingested_and_published(
    session: AsyncSession,
    applier: PushApplyService,
    events: FakeEventPublisher,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """The item half, through the real repositories.

    `IngestService`'s two known defects (a minted season/episode id that names no row)
    are invisible to every port fake and fail on a foreign key here.
    """
    adapter = FakeSourceAdapter(source)
    adapter.seed(
        SourceItem(
            external_id="movie-9",
            name="A Pushed Film",
            kind=SourceItemKind.MOVIE,
            year=2021,
            container="mkv",
            # A provider id the catalog does not hold, so the ladder falls through to
            # stub-on-sight and the item ends up with a `title_id`.
            provider_ids={"tmdb": "90000551"},
        ),
        SEEN_AT,
    )
    outcome = await applier.apply(
        source,
        adapter,
        SourceEvent(kind=SourceEventKind.ITEM_ADDED, external_ids=("movie-9",)),
        user_id=user_id,
    )
    assert outcome.items_ingested == 1
    stored = await PostgresMediaItemRepository(session).get_by_external_id(source.id, "movie-9")
    assert stored is not None and stored.available is True
    assert [event.kind for event in events.published] == [ClientEventKind.TITLE_UPDATED]


async def test_a_removal_the_source_confirms_retracts_the_row(
    session: AsyncSession,
    applier: PushApplyService,
    events: FakeEventPublisher,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """Against the table that holds the flag: the source no longer serves movie-1."""
    title_id = await _given_matched_movie(session, source, "movie-1")
    outcome = await applier.apply(
        source,
        FakeSourceAdapter(source),
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("movie-1",)),
        user_id=user_id,
    )
    assert outcome.items_retracted == 1
    stored = await PostgresMediaItemRepository(session).get_by_external_id(source.id, "movie-1")
    assert stored is not None and stored.available is False
    assert [(event.kind, event.title_id) for event in events.published] == [
        (ClientEventKind.TITLE_UPDATED, title_id)
    ]


async def test_a_removal_the_source_still_serves_leaves_the_row_available(
    session: AsyncSession,
    applier: PushApplyService,
    events: FakeEventPublisher,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """What an Emby library refresh sends: `ItemsRemoved` for a file that is still there."""
    await _given_matched_movie(session, source, "movie-1")
    adapter = FakeSourceAdapter(source)
    adapter.seed(
        SourceItem(
            external_id="movie-1",
            name="Film movie-1",
            kind=SourceItemKind.MOVIE,
            year=2021,
            container="mkv",
        ),
        SEEN_AT,
    )
    outcome = await applier.apply(
        source,
        adapter,
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("movie-1",)),
        user_id=user_id,
    )
    assert outcome.items_retracted == 0
    stored = await PostgresMediaItemRepository(session).get_by_external_id(source.id, "movie-1")
    assert stored is not None and stored.available is True
    assert events.published == []


async def _given_a_series_with_episodes(
    session: AsyncSession, source: Source, episodes: int
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """`series-1`, one season, and `ep-0` onward, each with its `media_items` row."""
    series = Title(kind=TitleKind.SERIES, name="Pushed Series", sort_name="Pushed Series")
    await PostgresTitleRepository(session).add(series)
    season, episode_ids = new_id(), [new_id() for _ in range(episodes)]
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": season, "title_id": series.id},
    )
    for number, episode_id in enumerate(episode_ids, start=1):
        await session.execute(
            text(
                "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
                "VALUES (:id, :title_id, :season_id, 1, :number)"
            ),
            {"id": episode_id, "title_id": series.id, "season_id": season, "number": number},
        )
    rows = [("series-1", None)] + [(f"ep-{index}", one) for index, one in enumerate(episode_ids)]
    await PostgresMediaItemRepository(session).upsert_many(
        [
            MediaItemUpsert(
                source_id=source.id,
                external_id=external_id,
                title_id=series.id,
                episode_id=episode_id,
                container=None if episode_id is None else "mkv",
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
            for external_id, episode_id in rows
        ]
    )
    return series.id, episode_ids


async def test_a_removed_series_retracts_its_episodes_the_source_confirms_gone(
    session: AsyncSession,
    applier: PushApplyService,
    events: FakeEventPublisher,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """Emby names the series alone; `ep-0` and `ep-1` go with it, and the served `ep-2` stays."""
    series, episode_ids = await _given_a_series_with_episodes(session, source, episodes=3)
    adapter = FakeSourceAdapter(source)
    adapter.seed(
        SourceItem(external_id="ep-2", name="Episode 3", kind=SourceItemKind.EPISODE),
        SEEN_AT,
    )
    outcome = await applier.apply(
        source,
        adapter,
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1",)),
        user_id=user_id,
    )
    assert adapter.confirm_calls == [frozenset({"series-1"}), frozenset({"ep-0", "ep-1", "ep-2"})]
    assert outcome.items_retracted == 3
    repository = PostgresMediaItemRepository(session)
    for external_id, available in (
        ("series-1", False),
        ("ep-0", False),
        ("ep-1", False),
        ("ep-2", True),
    ):
        stored = await repository.get_by_external_id(source.id, external_id)
        assert stored is not None and stored.available is available, external_id
    assert {(event.title_id, event.episode_id) for event in events.published} == {
        (series, None),
        (series, episode_ids[0]),
        (series, episode_ids[1]),
    }
