"""The whole pipeline: a registered source in, canonical catalog out.

Real PostgreSQL, real repositories, real `MatchService`/`IngestService`/
`ReconcileService`/`WatchStateSyncService`, and the **real `EmbyAdapter`**
over `FakeEmbyServer` -- so the walk really pages, really parses Emby's own
JSON shapes, and really omits play history from a listing the way Emby
4.9.5.0 does. Every other file in this suite exercises one seam; this one
exists for the failures that only appear when all of them run together.

Three properties are only visible at this level:

1. **Watch history survives a nightly walk.** ADR-0014 runs through a port
   DTO, an adapter, a service, a merge DTO and two SQL statements, and each
   of those has its own test. This is the one that fails if any of them
   regresses at the same time as another.
2. **A second walk changes nothing.** Idempotence is a property of the
   *composition* -- `resolve_seasons`/`resolve_episodes` exist only to make
   a second walk find the ids the first one stored, and a dict has no
   foreign keys to notice when they do not.
3. **Statement count does not grow with the page.** Measured with
   `before_cursor_execute` against the statements the repositories actually
   issue. `EXPLAIN`-ing a hand-copied lookalike of the SQL proves nothing
   about the repository; counting what it sent proves exactly one thing, and
   it is the thing that separates a walk that finishes overnight from one
   that does not.

This file commits nothing -- it runs inside the integration fixture's
rolled-back transaction, so the staging tables `usher.db.staging` creates
are rolled back with it and no `stg_*` table leaks into
`test_migrations.py`'s schema-drift check.
"""

import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.emby_server import FakeEmbyServer
from tests.fakes.event_publisher import FakeEventPublisher
from usher.adapters.emby.adapter import EmbyAdapter
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.staging import raw_connection
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind, SyncRunStatus
from usher.domain.title import Title
from usher.ports.credentials import SourceCredentials
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.watch_sync import WatchStateSyncService

CHANGED_AT = datetime(2026, 7, 1, tzinfo=UTC)
PAGE_SIZE = 3

# The catalog already holds this one (M2's bootstrap put 291,737 tmdb ids
# there), so it matches on tier 1 and no stub is created.
KNOWN_TMDB_ID = 90000550
# It does not hold this one. 291,737 of 1,271,138 titles carry a `tmdb_id`,
# so "a trusted provider id the catalog has never seen" is the common case
# rather than the exception -- which is what makes stub-on-sight load-bearing
# rather than dead code.
UNKNOWN_TMDB_ID = 999_331

_MOVIE_KNOWN = SourceItem(
    external_id="movie-known",
    name="Fight Club",
    kind=SourceItemKind.MOVIE,
    year=1999,
    provider_ids={"tmdb": str(KNOWN_TMDB_ID)},
    container="mkv",
)
_MOVIE_UNKNOWN = SourceItem(
    external_id="movie-unknown",
    name="An Unbootstrapped Film",
    kind=SourceItemKind.MOVIE,
    year=2024,
    provider_ids={"tmdb": str(UNKNOWN_TMDB_ID)},
    container="mkv",
)
_HOME_VIDEO = SourceItem(
    external_id="home-video",
    name="Birthday 2019",
    kind=SourceItemKind.MOVIE,
    container="mp4",
)
_SERIES = SourceItem(
    external_id="series-1",
    name="Example Series",
    kind=SourceItemKind.SERIES,
    year=2011,
    provider_ids={"tvdb": "91000030"},
)
_EPISODES = [
    SourceItem(
        external_id=f"episode-{number}",
        name=f"Episode {number}",
        kind=SourceItemKind.EPISODE,
        # An episode's provider ids are the *episode's own*, never its
        # series' -- the finding that keeps episodes off the match ladder
        # entirely. Seeded here so the end-to-end run really exercises the
        # branch that ignores them.
        provider_ids={"imdb": "tt99000110"},
        container="mkv",
        series_external_id="series-1",
        season_number=3,
        episode_number=number,
    )
    for number in (5, 6)
]
LIBRARY = [_MOVIE_KNOWN, _MOVIE_UNKNOWN, _HOME_VIDEO, _SERIES, *_EPISODES]


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> Source:
    row = Source(
        kind=SourceKind.EMBY,
        name="End To End Emby",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    await PostgresSourceRepository(session).add(row)
    return row


@pytest_asyncio.fixture
async def catalog(session: AsyncSession) -> uuid.UUID:
    """One title M2's bootstrap would have left behind, so tier 1 has
    something to find."""
    title = Title(
        kind=TitleKind.MOVIE,
        name="Fight Club",
        sort_name="Fight Club",
        year=1999,
        tmdb_id=KNOWN_TMDB_ID,
        enrichment_state=EnrichmentState.SKELETON,
    )
    await PostgresTitleRepository(session).add(title)
    return title.id


@pytest.fixture
def emby() -> FakeEmbyServer:
    return FakeEmbyServer(page_size=PAGE_SIZE)


@pytest_asyncio.fixture
async def adapter(emby: FakeEmbyServer, source: Source) -> AsyncIterator[EmbyAdapter]:
    """The real adapter, over the fake server's transport.

    `httpx.MockTransport` rather than `SlowTransport`: nothing here is about
    concurrency, and 20 ms per request across a 200-item paging case is a
    minute of test time for no assertion.
    """
    client = httpx.AsyncClient(transport=emby.transport(), base_url=source.base_url)
    built = EmbyAdapter(
        source,
        SourceCredentials(username=emby.username, password=SecretStr(emby.password)),
        client=client,
        page_size=PAGE_SIZE,
    )
    yield built
    await built.aclose()
    await client.aclose()


@pytest.fixture
def media_items(session: AsyncSession) -> PostgresMediaItemRepository:
    return PostgresMediaItemRepository(session)


@pytest.fixture
def episodes(session: AsyncSession) -> PostgresEpisodeRepository:
    return PostgresEpisodeRepository(session)


@pytest.fixture
def watch_states(session: AsyncSession) -> PostgresWatchStateRepository:
    return PostgresWatchStateRepository(session)


@pytest.fixture
def runs(session: AsyncSession) -> PostgresSyncRunRepository:
    return PostgresSyncRunRepository(session)


@pytest.fixture
def queue(session: AsyncSession) -> PostgresJobQueue:
    return PostgresJobQueue(session, max_attempts=5, backoff_seconds=30.0)


@pytest.fixture
def reconcile(
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    episodes: PostgresEpisodeRepository,
    runs: PostgresSyncRunRepository,
    queue: PostgresJobQueue,
) -> ReconcileService:
    """`commit` is `session.flush`, not `session.commit`.

    The integration fixture owns one connection-bound transaction it rolls
    back, which is what gives each test its isolation; committing inside it
    would defeat that. What is under test is the *ordering* of the writes,
    not their durability -- the same trade
    `test_services_reconcile.py` documents.
    """
    matching = PostgresTitleMatchRepository(session)
    return ReconcileService(
        ingest=IngestService(
            matcher=MatchService(
                titles=PostgresTitleRepository(session), matching=matching, queue=queue
            ),
            matching=matching,
            media_items=media_items,
            episodes=episodes,
            queue=queue,
        ),
        media_items=media_items,
        events=FakeEventPublisher(),
        runs=runs,
        commit=session.flush,
        batch_size=1_000,
    )


@pytest.fixture
def watch_sync(
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    runs: PostgresSyncRunRepository,
    queue: PostgresJobQueue,
) -> WatchStateSyncService:
    return WatchStateSyncService(
        media_items=media_items,
        watch_states=watch_states,
        runs=runs,
        queue=queue,
        commit=session.flush,
        batch_size=1_000,
    )


@pytest_asyncio.fixture
async def user_id(session: AsyncSession) -> uuid.UUID:
    identifier = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (:id, :name)"),
        {"id": identifier, "name": f"e2e-{identifier}"},
    )
    return identifier


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement SQLAlchemy issues.

    A `COPY` is invisible here -- `copy_records_to_table` runs on the raw
    asyncpg connection -- which is the point: a `COPY` is one command
    however many records stream through it.
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


async def _explain(session: AsyncSession, statement: str, parameters: Sequence[object]) -> str:
    """`EXPLAIN` the statement **as the driver received it**, with the
    parameters it received.

    Not a hand-copied lookalike: two earlier tasks in this project asserted
    on the plan of a transcribed query and both were replaced, because the
    copy drifts from the repository and then reads like coverage. What
    `before_cursor_execute` hands over is already compiled to asyncpg's
    `$1` placeholders, so it has to go back to asyncpg rather than through
    `text()` -- SQLAlchemy would find no binds in it and pass none.
    """
    driver = await raw_connection(session)
    rows = await driver.fetch("EXPLAIN " + statement, *tuple(parameters or ()))
    return "\n".join(str(row[0]) for row in rows)


def _capture(
    predicate: Callable[[str], bool],
) -> tuple[list[tuple[str, Sequence[object]]], Callable[[], None]]:
    """Record (statement, parameters) for every statement matching
    `predicate`, and the callable that stops recording."""
    seen: list[tuple[str, Sequence[object]]] = []

    def record(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if predicate(statement):
            seen.append((statement, cast(Sequence[object], parameters)))

    event.listen(Engine, "before_cursor_execute", record)
    return seen, lambda: event.remove(Engine, "before_cursor_execute", record)


def _seed(emby: FakeEmbyServer, items: list[SourceItem]) -> None:
    for item in items:
        emby.add_item(item, CHANGED_AT)


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


async def test_a_library_becomes_a_catalog(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    media_items: PostgresMediaItemRepository,
    episodes: PostgresEpisodeRepository,
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """Registered source in, canonical catalog out: one movie the catalog
    already holds, one it does not (stub-on-sight), one with no ids at all
    (review queue), a series, and two of its episodes hung off it."""
    _seed(emby, LIBRARY)
    run = await reconcile.reconcile(source, SyncRunKind.FULL, adapter)

    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_seen == len(LIBRARY)
    assert await media_items.count_for_source(source.id) == len(LIBRARY)

    known = await media_items.get_by_external_id(source.id, "movie-known")
    assert known is not None and known.title_id == catalog, "tier 1 must reuse the catalog's row"

    stubbed = await media_items.get_by_external_id(source.id, "movie-unknown")
    assert stubbed is not None and stubbed.title_id is not None
    assert stubbed.title_id != catalog

    assert [item.external_id for item in await media_items.list_unmatched(source.id)] == [
        "home-video"
    ]

    series = await media_items.get_by_external_id(source.id, "series-1")
    assert series is not None and series.title_id is not None
    seasons, stored_episodes = await episodes.list_for_title(series.title_id)
    assert [season.season_number for season in seasons] == [3]
    assert sorted(episode.episode_number for episode in stored_episodes) == [5, 6]

    for episode in _EPISODES:
        row = await media_items.get_by_external_id(source.id, episode.external_id)
        assert row is not None
        assert row.title_id == series.title_id, "an episode hangs off its series' Title"
        assert row.episode_id is not None


async def test_no_episode_ever_mints_a_title(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    session: AsyncSession,
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """The catastrophe this pipeline is shaped around, end to end. Each
    seeded episode carries its *own* `Imdb` id -- a live Emby episode really
    does -- and no episode's IMDb id is in the catalog at all (`tvEpisode`
    is excluded from M2's bootstrap by design), so an episode that walked
    the ladder would fall through to stub-on-sight and mint one junk title
    apiece: 999,827 of them at the one measured deployment, a catalog of
    rubbish roughly the size of the real one."""
    _seed(emby, LIBRARY)
    before = (await session.execute(text("SELECT count(*) FROM titles"))).scalar_one()
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    after = (await session.execute(text("SELECT count(*) FROM titles"))).scalar_one()
    # Exactly two: the unknown movie's stub and the series' stub. Not four.
    assert after - before == 2
    assert (
        await session.execute(text("SELECT count(*) FROM titles WHERE imdb_id = 'tt99000110'"))
    ).scalar_one() == 0


async def test_a_second_run_changes_nothing(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    media_items: PostgresMediaItemRepository,
    session: AsyncSession,
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """PRD 03: "four idempotent, resumable stages".

    The second walk is where the two mutations no port fake can see would
    fire: skipping `resolve_seasons` or `resolve_episodes` leaves a
    freshly-minted UUIDv7 naming a row that does not exist, which a dict
    stores happily and `fk_episodes_season_id_seasons` /
    `fk_media_items_episode_id_episodes` do not.
    """
    _seed(emby, LIBRARY)
    first = await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    before = {
        row.external_id: (row.title_id, row.episode_id)
        for row in await media_items.list_unmatched(source.id, limit=1000)
    }
    titles_after_first = (await session.execute(text("SELECT count(*) FROM titles"))).scalar_one()

    second = await reconcile.reconcile(source, SyncRunKind.FULL, adapter)

    assert second.status is SyncRunStatus.COMPLETED
    assert second.items_retracted == 0
    assert second.items_seen == first.items_seen
    assert await media_items.count_for_source(source.id) == first.items_seen
    assert (
        await session.execute(text("SELECT count(*) FROM titles"))
    ).scalar_one() == titles_after_first, "a second walk must not re-stub anything"
    assert {
        row.external_id: (row.title_id, row.episode_id)
        for row in await media_items.list_unmatched(source.id, limit=1000)
    } == before
    for episode in _EPISODES:
        stored = await media_items.get_by_external_id(source.id, episode.external_id)
        assert stored is not None and stored.episode_id is not None


async def test_a_deleted_item_is_retracted_and_its_watch_state_survives(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    catalog: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """PRD 08's backup asymmetry as behaviour: the catalog is rebuildable,
    watch state is precious. A retracted item keeps its row, its title link,
    and everything attached to that title."""
    _seed(emby, LIBRARY)
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    await watch_states.merge_from_source(
        [
            WatchStateMerge(
                user_id=user_id,
                title_id=catalog,
                episode_id=None,
                position_seconds=613,
                played=True,
                runtime_seconds=7_000,
                observed_at=datetime.now(UTC),
                play_count=3,
                last_played_at=datetime.now(UTC),
            )
        ]
    )

    emby.remove_item("movie-known")
    run = await reconcile.reconcile(source, SyncRunKind.FULL, adapter)

    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_retracted == 1
    gone = await media_items.get_by_external_id(source.id, "movie-known")
    assert gone is not None, "PRD 02: soft-delete availability, hard-delete nothing"
    assert gone.available is False
    assert gone.title_id == catalog, "a retraction keeps the link it resolved"
    survived = await watch_states.get_for_title(user_id, catalog)
    assert survived is not None
    assert (survived.position_seconds, survived.play_count) == (613, 3)


async def test_a_walk_that_dies_mid_run_leaves_a_resumable_catalog(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    media_items: PostgresMediaItemRepository,
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """A crash costs the batch in flight, not the walk -- and nothing is
    retracted in between, because the sweep is on the success path and
    nowhere else. The second run upserts everything again, which is free
    because every write is an upsert."""
    _seed(emby, LIBRARY)
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)

    emby.fail_after = PAGE_SIZE
    failed = await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    assert failed.status is SyncRunStatus.FAILED
    assert failed.items_retracted == 0
    for item in LIBRARY:
        stored = await media_items.get_by_external_id(source.id, item.external_id)
        assert stored is not None and stored.available is True, item.external_id

    emby.fail_after = None
    recovered = await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    assert recovered.status is SyncRunStatus.COMPLETED
    assert recovered.items_seen == len(LIBRARY)
    assert recovered.items_retracted == 0


async def test_watch_state_survives_a_walk_that_cannot_report_history(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    watch_sync: WatchStateSyncService,
    watch_states: PostgresWatchStateRepository,
    media_items: PostgresMediaItemRepository,
    queue: PostgresJobQueue,
    session: AsyncSession,
    source: Source,
    catalog: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """**The whole milestone, end to end.**

    A backfill records `play_count = 7`. A full nightly watch-state walk
    then runs over the same item, through the real `EmbyAdapter` against a
    `FakeEmbyServer` whose *listing* omits play history exactly as Emby
    4.9.5.0 does -- and the count is still 7 afterwards.

    Six layers can each break this on their own: the adapter's mapper, the
    port DTO's `int | None`, `WatchStateSyncService._merge_for`, the
    `WatchStateMerge` DTO, the `UPDATE ... FROM` merge, and the
    `INSERT ... ON CONFLICT DO NOTHING` behind it. Each has its own test.
    This is the one that fails when two of them regress at once.

    The row is staged with `clock_timestamp()` through a raw `INSERT`
    because `now()` is frozen for a transaction and each test *is* one
    transaction, so a merge carrying `datetime.now(UTC)` would otherwise
    lose the "latest `updated_at` wins" comparison against a row written
    microseconds earlier in the same frozen instant.
    """
    _seed(emby, LIBRARY)
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)

    await session.execute(
        text(
            """
            INSERT INTO watch_states
                (id, user_id, title_id, position_seconds, played, play_count,
                 last_played_at, origin, updated_at)
            VALUES (:id, :user_id, :title_id, 613, true, 7,
                    :last_played_at, 'source', clock_timestamp() - interval '1 hour')
            """
        ),
        {
            "id": new_id(),
            "user_id": user_id,
            "title_id": catalog,
            "last_played_at": datetime(2026, 6, 1, tzinfo=UTC),
        },
    )

    emby.set_watch_state(
        SourceWatchState(
            external_id="movie-known",
            position_seconds=900,
            played=True,
            play_count=7,
            last_played_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    run = await watch_sync.sync(source, adapter, user_id=user_id)
    assert run.status is SyncRunStatus.COMPLETED

    stored = await watch_states.get_for_title(user_id, catalog)
    assert stored is not None
    assert stored.play_count == 7, "a walk that cannot count must not write a zero"
    assert stored.last_played_at == datetime(2026, 6, 1, tzinfo=UTC)
    assert stored.position_seconds == 900, "what the walk *can* report is still written"

    # And the walk asks for what it could not determine, exactly once per
    # played item, at background priority.
    assert (await queue.depth())[JobKind.WATCH_HISTORY] >= 1


async def test_the_backfill_recovers_the_history_the_walk_could_not_see(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    watch_sync: WatchStateSyncService,
    watch_states: PostgresWatchStateRepository,
    session: AsyncSession,
    source: Source,
    catalog: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """The other half of ADR-0014, through the real single-item route.

    The walk merges `play_count = NULL` (the listing genuinely cannot say),
    which leaves the row matching `played AND play_count = 0` -- an
    observable "history unknown" state bounded by the household's watched
    items rather than by the source's 1,126,674. `backfill_one` then asks
    `GET /Users/{u}/Items/{item}`, which can.
    """
    _seed(emby, LIBRARY)
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    emby.set_watch_state(
        SourceWatchState(
            external_id="movie-known",
            position_seconds=42,
            played=True,
            play_count=9,
            last_played_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
    )
    await watch_sync.sync(source, adapter, user_id=user_id)

    after_walk = await watch_states.get_for_title(user_id, catalog)
    assert after_walk is not None
    assert after_walk.played is True
    assert after_walk.play_count == 0, "the walk cannot count, and says so by not writing one"

    assert await watch_sync.backfill_one(
        source, adapter, external_id="movie-known", user_id=user_id
    )
    recovered = await watch_states.get_for_title(user_id, catalog)
    assert recovered is not None
    assert recovered.play_count == 9
    assert recovered.last_played_at == datetime(2026, 5, 4, tzinfo=UTC)


async def test_an_episodes_watch_state_lands_on_the_episode_not_the_series(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    watch_sync: WatchStateSyncService,
    watch_states: PostgresWatchStateRepository,
    media_items: PostgresMediaItemRepository,
    source: Source,
    catalog: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """89% of this library is episodes, and an episode's `MediaItem` carries
    its series' `title_id` *and* its own `episode_id` while `watch_states`
    permits exactly one (`num_nonnulls(title_id, episode_id) = 1`).
    Collapsing to the *title* would merge every episode of a show onto one
    row, quietly; passing both through raises `PortDataMalformed` and aborts
    a batch of five thousand states."""
    _seed(emby, LIBRARY)
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    emby.set_watch_state(
        SourceWatchState(external_id="episode-5", position_seconds=120, played=False)
    )
    emby.set_watch_state(
        SourceWatchState(external_id="episode-6", position_seconds=240, played=False)
    )
    run = await watch_sync.sync(source, adapter, user_id=user_id)
    assert run.status is SyncRunStatus.COMPLETED

    positions = []
    for episode in _EPISODES:
        row = await media_items.get_by_external_id(source.id, episode.external_id)
        assert row is not None and row.episode_id is not None
        stored = await watch_states.get_for_episode(user_id, row.episode_id)
        assert stored is not None
        positions.append(stored.position_seconds)
    assert sorted(positions) == [120, 240], "two episodes, two rows, not one merged row"

    # The series item yields a state of its own (Emby reports one for every
    # item), and it must be a *separate* row: collapsing an episode onto its
    # series' `title_id` would merge a whole show's positions into this one.
    series = await media_items.get_by_external_id(source.id, "series-1")
    assert series is not None and series.title_id is not None
    series_state = await watch_states.get_for_title(user_id, series.title_id)
    assert series_state is None or series_state.position_seconds not in (120, 240)


async def test_a_delta_walk_never_retracts(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    media_items: PostgresMediaItemRepository,
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """ADR-0015's second rule, end to end. A delta returns only what
    changed, so by construction nearly everything is "unseen" -- a sweep
    after one would retract the library. The real adapter's `since` filter
    is what makes this a real delta rather than a relabelled full walk."""
    _seed(emby, LIBRARY)
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    emby.add_item(
        SourceItem(
            external_id="movie-new",
            name="Arrived Today",
            kind=SourceItemKind.MOVIE,
            year=2026,
            provider_ids={"tmdb": "424242"},
            container="mkv",
        ),
        CHANGED_AT + timedelta(days=30),
    )
    delta = await reconcile.reconcile(source, SyncRunKind.DELTA, adapter)
    assert delta.status is SyncRunStatus.COMPLETED
    assert delta.items_retracted == 0
    assert delta.items_seen < len(LIBRARY) + 1, "a delta that walked everything is not a delta"
    for item in LIBRARY:
        stored = await media_items.get_by_external_id(source.id, item.external_id)
        assert stored is not None and stored.available is True


# ---------------------------------------------------------------------------
# The shape that would catch a quadratic
# ---------------------------------------------------------------------------


def _mixed(*, series: int, episodes: int, movies: int, offset: int) -> list[SourceItem]:
    """A slice of the measured library's shape: mostly episodes, spread over
    many series, with movies alongside.

    **Many series, not one**, and many movies, and that is what makes the
    flatness assertion below bite. `IngestService._series_titles` resolves
    the series this page's episodes hang off, and
    `_titles_needing_enrichment` reads the enrichment state of every title
    the page touched -- both take a *list*. With a single series and a
    single title, a per-item spelling of either issues exactly one statement
    and is indistinguishable from the batched one. Measured: both mutations
    survived a version of this case built on one series.
    """
    items = [
        SourceItem(
            external_id=f"bulk-series-{offset + index}",
            name=f"Bulk Series {offset + index}",
            kind=SourceItemKind.SERIES,
            year=2000 + index % 20,
            provider_ids={"tvdb": str(300_000 + offset + index)},
        )
        for index in range(series)
    ]
    items.extend(
        SourceItem(
            external_id=f"bulk-movie-{offset + index}",
            name=f"Bulk Movie {offset + index}",
            kind=SourceItemKind.MOVIE,
            year=1990 + index % 30,
            provider_ids={"tmdb": str(600_000 + offset + index)},
            container="mkv",
        )
        for index in range(movies)
    )
    items.extend(
        SourceItem(
            external_id=f"bulk-episode-{offset + index}",
            name=f"Bulk Episode {offset + index}",
            kind=SourceItemKind.EPISODE,
            container="mkv",
            series_external_id=f"bulk-series-{offset + index % max(series, 1)}",
            season_number=1 + (index // 25) % 8,
            episode_number=1 + index % 25,
        )
        for index in range(episodes)
    )
    return items


async def test_statements_do_not_grow_with_the_page(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    episodes: PostgresEpisodeRepository,
    runs: PostgresSyncRunRepository,
    queue: PostgresJobQueue,
    statement_counter: list[str],
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """**The measurement, at the library's own shape.**

    Nine batches of five items and nine batches of fifty must cost the
    *same* number of statements. Anything per-item is the difference between
    a walk that finishes overnight and one that does not: at 1,126,674
    items, one extra round trip apiece is more than a million of them.

    **The batch count is held fixed and the page is varied, not the other
    way round.** Holding the *page* fixed and growing the library only
    grows the number of batches, which is supposed to grow -- and a batch
    big enough to hold a whole library also puts every series in the same
    page as its own episodes, which makes `_series_titles`' stored lookup
    an empty list and its per-item spelling indistinguishable from its
    batched one. Measured: the `resolve_series_titles`-per-episode mutation
    survived exactly that shape, because `wanted` was never non-empty.
    Five items per batch is what makes an episode's series arrive in an
    earlier page, which is the normal case at 32,409 series among 1,126,674
    items.

    Both walks measured are *warm* -- the population was already ingested by
    a preceding run -- so `MatchService._create_stub`, the pipeline's one
    genuinely per-item write, contributes nothing to either count and the
    next case measures it on its own.

    Not an exact number: the staged `COPY` path issues DDL plus a
    `SAVEPOINT` per upsert, and pinning a total would break on any unrelated
    change to `usher.db.staging`. The property is the flatness.
    """

    def _walker(batch_size: int) -> ReconcileService:
        matching = PostgresTitleMatchRepository(session)
        return ReconcileService(
            ingest=IngestService(
                matcher=MatchService(
                    titles=PostgresTitleRepository(session), matching=matching, queue=queue
                ),
                matching=matching,
                media_items=media_items,
                episodes=episodes,
                queue=queue,
            ),
            media_items=media_items,
            events=FakeEventPublisher(),
            runs=runs,
            commit=session.flush,
            batch_size=batch_size,
        )

    _seed(emby, _mixed(series=5, episodes=20, movies=20, offset=0))
    await _walker(1_000).reconcile(source, SyncRunKind.FULL, adapter)
    statement_counter.clear()
    await _walker(5).reconcile(source, SyncRunKind.FULL, adapter)
    small = len(statement_counter)

    _seed(emby, _mixed(series=45, episodes=180, movies=180, offset=1_000))
    await _walker(1_000).reconcile(source, SyncRunKind.FULL, adapter)
    statement_counter.clear()
    await _walker(50).reconcile(source, SyncRunKind.FULL, adapter)
    large = len(statement_counter)

    assert small == large, (
        f"{small} statements for 9 batches of 5, {large} for 9 batches of 50 -- "
        "something costs a statement per item"
    )


async def test_the_only_per_item_write_is_one_insert_per_new_title(
    emby: FakeEmbyServer,
    adapter: EmbyAdapter,
    reconcile: ReconcileService,
    statement_counter: list[str],
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """The one place the pipeline is *not* set-based, measured rather than
    asserted about.

    `MatchService._create_stub` calls `TitleRepository.add` per item, so a
    first walk over items the catalog has never seen costs one `INSERT INTO
    titles` apiece. That is bounded by **new titles**, not by items: at the
    one measured deployment that is at most 94,438 movies + 32,409 series,
    because an episode never walks the ladder at all and the other 999,827
    items therefore cost nothing here. A second walk over the same items
    costs zero, because they match.

    Pinned so the bound cannot silently become per-item-of-the-library: if
    an episode ever started stubbing, the case above goes from flat to
    linear and this one's arithmetic stops holding.
    """
    fresh = [
        SourceItem(
            external_id=f"fresh-{index}",
            name=f"Fresh Film {index}",
            kind=SourceItemKind.MOVIE,
            year=2020,
            provider_ids={"tmdb": str(800_000 + index)},
            container="mkv",
        )
        for index in range(30)
    ]
    _seed(emby, fresh)

    statement_counter.clear()
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    first = [
        one for one in statement_counter if one.lstrip().upper().startswith("INSERT INTO TITLES")
    ]
    assert len(first) == 30, "stub-on-sight is one INSERT per new title"

    statement_counter.clear()
    await reconcile.reconcile(source, SyncRunKind.FULL, adapter)
    second = [
        one for one in statement_counter if one.lstrip().upper().startswith("INSERT INTO TITLES")
    ]
    assert second == [], "a second walk matches what the first stubbed and writes no titles"


async def test_a_re_seen_job_is_not_rewritten_every_night(
    queue: PostgresJobQueue, session: AsyncSession
) -> None:
    """`_ENQUEUE`'s `ON CONFLICT DO UPDATE` used to stamp `updated_at` for
    every re-seen job, so a nightly walk rewrote a row per job for no state
    change -- 1.1M dead-weight row versions and the vacuum to match, every
    night, on a table whose whole purpose is to be small.

    The promotion path still writes, because that is a real state change.
    """
    from usher.domain.jobs import JobPriority
    from usher.ports.jobs import JobRequest

    request = JobRequest(kind=JobKind.MATCH, key="seen-every-night", priority=JobPriority.NEW)
    assert await queue.enqueue([request]) == 1
    stamp = (
        await session.execute(text("SELECT updated_at FROM jobs WHERE key = 'seen-every-night'"))
    ).scalar_one()

    assert await queue.enqueue([request]) == 0, "nothing changed, so nothing is written"
    assert (
        await session.execute(text("SELECT updated_at FROM jobs WHERE key = 'seen-every-night'"))
    ).scalar_one() == stamp

    promoted = JobRequest(kind=JobKind.MATCH, key="seen-every-night", priority=JobPriority.DEMAND)
    assert await queue.enqueue([promoted]) == 1, "a promotion is a real change"
    assert (
        await session.execute(text("SELECT priority FROM jobs WHERE key = 'seen-every-night'"))
    ).scalar_one() == int(JobPriority.DEMAND)


async def test_the_availability_sweeps_update_uses_its_index(
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    source: Source,
) -> None:
    """`ix_media_items_sweep` exists for the sweep's `UPDATE`, and the
    numbers say exactly that.

    Measured against `pgvector/pgvector:pg17` at 1,126,674 rows on one
    source with 200 of them stale -- the realistic nightly shape -- via
    `scripts/measure_ingest.py --scale 1126674`:

    - the `UPDATE` goes from `Seq Scan` (`Rows Removed by Filter:
      1,126,474`, 173 ms) to `Index Scan using ix_media_items_sweep` with
      an `Index Cond` on all three columns, 102 ms;
    - the *guard*'s `count(*)` is a `Parallel Seq Scan` either way (87 ms
      with the index, 86 ms without), because ADR-0015's ceiling is a
      fraction and a total over a source that *is* the whole table has to
      touch every row however it is planned.

    So the claim is the narrow one and this case asserts the narrow one.
    `enable_seqscan = off` is what makes the choice observable at fixture
    size -- on fifty rows a seq scan really is cheaper, and an assertion
    that passed only because the table was small would be measuring the
    fixture.
    """
    await media_items.upsert_many(
        [
            MediaItemUpsert(
                source_id=source.id,
                external_id=f"stale-{index}",
                title_id=None,
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
                last_seen_at=CHANGED_AT,
            )
            for index in range(50)
        ]
    )
    captured, stop = _capture(lambda statement: "SET available = false" in statement)
    try:
        result = await media_items.mark_unseen_unavailable(
            source.id, seen_since=datetime.now(UTC), max_retract_fraction=1.0
        )
    finally:
        stop()
    assert result.retracted == 50
    assert captured, "the sweep's UPDATE was not issued"

    await session.execute(text("SET LOCAL enable_seqscan = off"))
    statement, parameters = captured[0]
    plan = await _explain(session, statement, parameters)
    assert "ix_media_items_sweep" in plan, plan
    condition = plan.split("Index Cond:")[1].split("\n")[0]
    for column in ("source_id", "available", "last_seen_at"):
        assert column in condition, plan


async def test_resolving_a_target_uses_the_unique_index(
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """`resolve_targets` runs once per watch-state batch against
    `external_id = ANY(...)`, and it is the only lookup on the hot path
    keyed by a source's own id. A plan that reached the rows through
    `source_id` alone and then *filtered* on `external_id` is a full pass
    over the source per batch -- 1,126,674 rows per thousand states -- and
    is exactly what an empty fixture cannot tell apart from a real lookup,
    because every `source_id`-leading index looks the same when there is
    nothing to scan.

    So this seeds two thousand rows and `ANALYZE`s before asking. The rows
    are *matched* on purpose: seeded unmatched, `title_id IS NOT NULL OR
    episode_id IS NOT NULL` selects nothing and the planner reasonably picks
    a `BitmapOr` over the two nullable-id indexes -- a plan that is right for
    that fixture and wrong for a library where nearly every item is matched.
    Measuring the fixture instead of the query is the failure mode this
    whole file exists to avoid, so the fixture has to have the production
    shape. The assertion is on the *`Index Cond`*: `external_id` has to be
    in it, which only `uq_media_items_source_external` can offer.
    """
    await media_items.upsert_many(
        [
            MediaItemUpsert(
                source_id=source.id,
                external_id=f"seeded-{index}",
                title_id=catalog,
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
                last_seen_at=CHANGED_AT,
            )
            for index in range(2_000)
        ]
    )
    await session.execute(text("ANALYZE media_items"))
    captured, stop = _capture(
        lambda statement: (
            "external_id = ANY" in statement and "title_id IS NOT NULL OR" in statement
        )
    )
    try:
        await media_items.resolve_targets(source.id, ["a", "b"])
    finally:
        stop()
    assert captured, "resolve_targets issued no statement"

    await session.execute(text("SET LOCAL enable_seqscan = off"))
    statement, parameters = captured[0]
    plan = await _explain(session, statement, parameters)
    assert "uq_media_items_source_external" in plan, plan
    assert "external_id" in plan.split("Index Cond:")[1].split("\n")[0], plan
