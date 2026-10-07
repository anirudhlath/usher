"""Inbound watch state, against port fakes and a source adapter that lies like Emby's."""

import asyncio
import contextlib
import dataclasses
import inspect
import uuid
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import AwareDatetime

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.sync import (
    ABANDONED_ERROR,
    CANCELLED_ERROR,
    STALE_AFTER,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
)
from usher.ports.errors import PortUnavailable, UsherPortError
from usher.ports.events import ClientEventKind
from usher.ports.ingest import MediaItemTarget, MediaItemUpsert, WatchStateMerge
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
from usher.services.watch_sync import MergedState, WatchStateSyncService, _watch_target

T0 = datetime(2026, 7, 1, tzinfo=UTC)
LATER = datetime(2099, 1, 1, tzinfo=UTC)
LAST_PLAYED = datetime(2026, 6, 30, 21, 14, tzinfo=UTC)
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


class _Clock:
    """A clock that reads whatever the case last set, moved on `step` at every read."""

    def __init__(self, now: datetime, *, step: timedelta = timedelta(0)) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> datetime:
        read = self.now
        self.now += self.step
        return read


def _item(external_id: str, **overrides: object) -> SourceItem:
    fields: dict[str, object] = {
        "external_id": external_id,
        "name": f"Movie {external_id}",
        "kind": SourceItemKind.MOVIE,
        "year": 2021,
    }
    fields.update(overrides)
    return SourceItem(**fields)  # type: ignore[arg-type]


class _LossySourceAdapter(FakeSourceAdapter):
    """A source whose *listing* cannot report play history.

    Verified against Emby 4.9.5.0: `GET /Users/{u}/Items` reports
    `PlayCount: 0` and omits `LastPlayedDate` for the very item whose
    `GET /Users/{u}/Items/{item}` reports `PlayCount: 2` and a real date. No
    `Fields` value, no `EnableUserData`, and no `Ids` restriction changes
    it. `get_watch_state` is inherited unchanged, which is the whole
    asymmetry this fake exists for.
    """

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncGenerator[SourceWatchState]:
        async for state in super()._walk_states(since, start_index):
            yield dataclasses.replace(state, play_count=None, last_played_at=None)


class _Fixture:
    def __init__(
        self,
        *,
        batch_size: int = 1_000,
        lossy: bool = True,
        clock: _Clock | None = None,
        heartbeat_seconds: float = 60.0,
    ) -> None:
        self.source = Source(
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-1",
            device_id=str(new_id()),
        )
        self.adapter = _LossySourceAdapter(self.source) if lossy else FakeSourceAdapter(self.source)
        self.user_id = new_id()
        self.media_items = FakeMediaItemRepository()
        self.watch_states = FakeWatchStateRepository()
        self.runs = FakeSyncRunRepository()
        self.queue = FakeJobQueue()
        self.commits = 0
        self.rollbacks = 0
        self.positions: list[int] = []
        self.saved: list[SyncRun] = []

        saved = self.runs.save

        async def _record(run: SyncRun) -> None:
            # **`positions` is the per-batch checkpoints, and "per batch" is spelled as
            # "this save carried states" rather than as "the status is RUNNING".**
            # A beat between batches saves a `RUNNING` row too, so the status test would
            # report the position it carried as though a batch had committed it.
            previous = await self.runs.get(run.id)
            if previous is not None and run.items_seen > previous.items_seen:
                self.positions.append(run.position)
            self.saved.append(run)
            await saved(run)

        self.runs.save = _record  # type: ignore[method-assign]
        self.clock = clock if clock is not None else _Clock(NOW)
        self.service = WatchStateSyncService(
            media_items=self.media_items,
            watch_states=self.watch_states,
            runs=self.runs,
            queue=self.queue,
            commit=self._commit,
            rollback=self._rollback,
            batch_size=batch_size,
            heartbeat_seconds=heartbeat_seconds,
            clock=self.clock,
        )
        self.titles: dict[str, uuid.UUID] = {}
        self.episodes: dict[str, uuid.UUID] = {}

    async def _commit(self) -> None:
        self.commits += 1

    async def _rollback(self) -> None:
        self.rollbacks += 1

    async def given_matched(
        self, external_id: str, *, episode: bool = False, changed_at: AwareDatetime = T0
    ) -> uuid.UUID:
        """A source item that is stored and matched, which a watch record needs first."""
        title_id = new_id()
        episode_id = new_id() if episode else None
        self.adapter.seed(_item(external_id), changed_at)
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=self.source.id,
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
                    last_seen_at=T0,
                )
            ]
        )
        self.titles[external_id] = title_id
        if episode_id is not None:
            self.episodes[external_id] = episode_id
        return episode_id if episode_id is not None else title_id

    async def given_completed_walk(self, *, at: AwareDatetime = T0) -> None:
        """A finished first walk, so the next run is a delta that yields every state."""
        await self.runs.add(
            SyncRun(
                source_id=self.source.id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.COMPLETED,
                started_at=at,
                finished_at=at,
            )
        )

    async def given_history(self, external_id: str, play_count: int) -> None:
        """A stored play count, as a backfill or an authoritative read would leave it.

        Merged at an instant *before* every walk in these tests, so the conflict rule
        never accounts for a preserved count on its own.
        """
        await self.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=self.user_id,
                    title_id=self.titles[external_id],
                    episode_id=None,
                    position_seconds=0,
                    played=True,
                    runtime_seconds=None,
                    observed_at=T0,
                    play_count=play_count,
                    last_played_at=LAST_PLAYED,
                )
            ]
        )

    async def stored(self, external_id: str) -> object:
        if external_id in self.episodes:
            return await self.watch_states.get_for_episode(self.user_id, self.episodes[external_id])
        return await self.watch_states.get_for_title(self.user_id, self.titles[external_id])


@pytest.fixture
def fixture() -> _Fixture:
    return _Fixture()


@pytest.fixture
def fixture_batched() -> _Fixture:
    """Batch size 2, so a five-state walk commits three times, the last one partial."""
    return _Fixture(batch_size=2)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


# -- the walk ---------------------------------------------------------------


async def test_a_walk_merges_position_and_played(fixture: _Fixture) -> None:
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=1840, played=False)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    stored = await fixture.stored("movie-1")
    assert stored is not None
    assert (stored.position_seconds, stored.played) == (1840, False)  # type: ignore[attr-defined]


async def test_a_walk_that_cannot_report_history_leaves_it_alone(fixture: _Fixture) -> None:
    """A stored play count survives a walk that reports none, end to end.

    The stored 7 was recovered by an authoritative read; the walk that follows says
    `PlayCount: 0` on the wire and `None` in the port, and the household's history has
    to survive it -- every night, forever.
    """
    await fixture.given_matched("movie-1")
    await fixture.given_history("movie-1", 7)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=True, play_count=7)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    stored = await fixture.stored("movie-1")
    assert stored is not None
    assert stored.play_count == 7  # type: ignore[attr-defined]
    assert stored.last_played_at == LAST_PLAYED  # type: ignore[attr-defined]


async def test_a_batch_of_walked_states_zeroes_none_of_their_counts(fixture: _Fixture) -> None:
    """The same property one batch wide, which is how it actually arrives.

    Thousands of states in one `merge_from_source`, all of them carrying an absent
    count, over rows holding different real ones. A service that collapsed `None` to
    `0` anywhere in the batch path erases all of them at once.
    """
    for index, count in enumerate((7, 3, 1, 12), start=1):
        await fixture.given_matched(f"movie-{index}")
        await fixture.given_history(f"movie-{index}", count)
        fixture.adapter.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=60 * index, played=True)
        )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    counts = []
    for index in range(1, 5):
        stored = await fixture.stored(f"movie-{index}")
        assert stored is not None
        counts.append(stored.play_count)  # type: ignore[attr-defined]
    assert counts == [7, 3, 1, 12]


async def test_a_source_that_reports_a_zero_has_its_zero_written(fixture: _Fixture) -> None:
    """The over-correction the `COALESCE` is not.

    "Never write a count from a merge" makes un-marking something played impossible to
    propagate, which is the same correctness bug as filtering zero states out of a
    delta walk. A source that *can* count and says zero is reporting a reset.
    """
    await fixture.given_completed_walk()
    await fixture.given_matched("movie-1")
    await fixture.given_history("movie-1", 7)
    honest = FakeSourceAdapter(fixture.source)
    honest.seed(_item("movie-1"), T0)
    honest.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=False, play_count=0)
    )
    await fixture.service.sync(fixture.source, honest, user_id=fixture.user_id)
    stored = await fixture.stored("movie-1")
    assert stored is not None
    assert stored.play_count == 0  # type: ignore[attr-defined]
    assert stored.played is False  # type: ignore[attr-defined]


async def test_an_episodes_state_is_merged_against_its_episode_not_its_series(
    fixture: _Fixture,
) -> None:
    """An episode's `MediaItem` carries its series' `title_id` *and* its `episode_id`.

    A watch state may carry exactly one (`num_nonnulls(title_id, episode_id) = 1`), so
    the service has to collapse the pair -- and handing both through raises
    `PortDataMalformed`, which aborts a whole batch of states. Passing the *title*
    instead is quieter and worse: every episode of a show merges onto one row.
    """
    episode_id = await fixture.given_matched("episode-1", episode=True)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="episode-1", position_seconds=133, played=True)
    )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_unmatched == 0
    by_episode = await fixture.watch_states.get_for_episode(fixture.user_id, episode_id)
    assert by_episode is not None
    assert by_episode.position_seconds == 133
    assert (
        await fixture.watch_states.get_for_title(fixture.user_id, fixture.titles["episode-1"])
        is None
    ), "the episode's state landed on its series"


def test_a_target_matched_to_nothing_collapses_to_nothing() -> None:
    """`_watch_target`'s third branch, tested directly because nothing else reaches it.

    `resolve_targets` omits an unmatched item rather than answering with a pair of
    `None`s, so the service's own filter is the `targets.get(...)` miss above and this
    branch is the belt to that pair of braces.

    Kept rather than deleted, and pinned rather than trusted: a repository that
    answered with an empty pair would otherwise hand `merge_from_source` a merge
    naming neither target, and one of those aborts a whole batch.
    """
    episode, title = new_id(), new_id()
    assert _watch_target(MediaItemTarget(title_id=title, episode_id=episode)) == MediaItemTarget(
        title_id=None, episode_id=episode
    )
    assert _watch_target(MediaItemTarget(title_id=title, episode_id=None)) == MediaItemTarget(
        title_id=title, episode_id=None
    )
    assert _watch_target(MediaItemTarget(title_id=None, episode_id=None)) is None


async def test_a_state_for_an_unmatched_item_is_skipped_not_raised_on(
    fixture: _Fixture,
) -> None:
    """A merge naming neither a title nor an episode raises `PortDataMalformed`.

    An unmatched `MediaItem` produces exactly that, which would abort a whole batch
    over one item sitting in the review queue. The service filters them and counts
    them instead.
    """
    fixture.adapter.seed(_item("orphan-1"), T0)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="orphan-1", position_seconds=90, played=False)
    )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.items_unmatched == 1
    assert run.items_matched == 0
    assert run.status is SyncRunStatus.COMPLETED


async def test_a_walk_longer_than_one_batch_merges_every_state(fixture: _Fixture) -> None:
    """The trailing partial batch is flushed too.

    Seven states at a batch size of two is three full batches and one of one, and a
    walk that flushed only on the size threshold silently drops the last page of
    nearly every run -- here that is a resume position a household would notice.
    """
    fixture.service._batch_size = 2
    for index in range(7):
        await fixture.given_matched(f"movie-{index}")
        fixture.adapter.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=index + 1, played=False)
        )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.items_seen == 7
    for index in range(7):
        stored = await fixture.stored(f"movie-{index}")
        assert stored is not None, f"movie-{index} was never merged"


async def test_states_are_resolved_once_per_batch_rather_than_once_per_state(
    fixture: _Fixture,
) -> None:
    """One `resolve_targets` per batch, not one per item.

    Over a library-sized walk that is the difference between finishing and not, and it
    is invisible in every assertion about stored values.
    """
    for index in range(50):
        await fixture.given_matched(f"movie-{index}")
        fixture.adapter.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=1, played=False)
        )
    fixture.media_items.reset_calls()
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert fixture.media_items.calls == 1, fixture.media_items.calls


async def test_every_merge_in_one_walk_carries_one_instant_not_a_per_batch_now(
    fixture: _Fixture,
) -> None:
    """One instant for the whole walk, never `now()` per batch.

    The difference is PRD 03's conflict rule rather than tidiness.
    """
    seen: list[datetime] = []
    original = fixture.watch_states.merge_from_source

    async def _record(merges: object) -> int:
        seen.extend(merge.observed_at for merge in merges)  # type: ignore[attr-defined]
        return await original(merges)  # type: ignore[arg-type]

    fixture.watch_states.merge_from_source = _record  # type: ignore[method-assign]
    fixture.service._batch_size = 2
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")
        fixture.adapter.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=1, played=False)
        )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert len(set(seen)) == 1, f"three batches, three different instants: {seen}"
    # The run's own `started_at`: a watch run is never resumed, so the instant its
    # attempt began and the instant its row began are one value.
    assert seen == [run.started_at] * 5


async def test_a_walk_never_retracts_a_watch_state(fixture: _Fixture) -> None:
    """PRD 08 lists watch state as the precious set that survives everything.

    So there is no sweep here and no lane that could grow one by accident.
    `ReconcileService`'s availability sweep is the shape this must never acquire.
    """
    swept: list[object] = []
    original = fixture.media_items.mark_unseen_unavailable

    async def _record(*args: object, **kwargs: object) -> object:
        swept.append(args)
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    fixture.media_items.mark_unseen_unavailable = _record  # type: ignore[method-assign, assignment]
    await fixture.given_matched("movie-1")
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert swept == []


# -- the split: one merge chain, two callers ---------------------------------


async def test_apply_states_merges_a_batch_and_reports_its_targets(fixture: _Fixture) -> None:
    """The push lane's entry point.

    The same chain as a walk's, minus the run bookkeeping -- because a push event is not
    a run, and inventing a `sync_runs` row per `UserDataChanged` would put a row per few
    seconds of playback into a table an operator reads.
    """
    await fixture.given_matched("movie-1")
    outcome = await fixture.service.apply_states(
        fixture.source.id,
        [SourceWatchState(external_id="movie-1", position_seconds=61, played=False)],
        user_id=fixture.user_id,
        observed_at=T0,
    )
    assert outcome.merged == (
        MergedState(
            external_id="movie-1",
            target=MediaItemTarget(title_id=fixture.titles["movie-1"], episode_id=None),
        ),
    )
    assert (outcome.unmatched, outcome.rows_written) == (0, 1)
    stored = await fixture.stored("movie-1")
    assert stored is not None
    assert stored.position_seconds == 61  # type: ignore[attr-defined]


async def test_apply_states_pairs_every_target_with_the_state_it_came_from(
    fixture: _Fixture,
) -> None:
    """The pairing, and the reason it is reported rather than recovered.

    `merged` is the *matched subset*, so a caller zipping it against the batch it
    handed in has one unmatched item at the front shift every pair by one, and the
    push lane publishes item A's resume position under item B's title.

    An unmatched item first, then two matched ones, is the smallest batch
    that shows it -- with the unmatched item last, a positional zip agrees
    with the truth and ratifies the bug.
    """
    await fixture.given_matched("movie-1")
    await fixture.given_matched("movie-2")
    outcome = await fixture.service.apply_states(
        fixture.source.id,
        [
            SourceWatchState(external_id="orphan-1", position_seconds=11, played=False),
            SourceWatchState(external_id="movie-1", position_seconds=22, played=False),
            SourceWatchState(external_id="movie-2", position_seconds=33, played=False),
        ],
        user_id=fixture.user_id,
        observed_at=T0,
    )
    assert [entry.external_id for entry in outcome.merged] == ["movie-1", "movie-2"]
    assert [entry.target.title_id for entry in outcome.merged] == [
        fixture.titles["movie-1"],
        fixture.titles["movie-2"],
    ]


async def test_apply_states_does_not_commit(fixture: _Fixture) -> None:
    """The commit is the caller's, and the two callers mean different units of work.

    A walk commits per batch of a thousand and the push lane per event, so a commit in
    here would make the second impossible to state.
    """
    await fixture.given_matched("movie-1")
    before = fixture.commits
    await fixture.service.apply_states(
        fixture.source.id,
        [SourceWatchState(external_id="movie-1", position_seconds=61, played=False)],
        user_id=fixture.user_id,
        observed_at=T0,
    )
    assert fixture.commits == before


async def test_apply_states_counts_an_unmatched_item_without_raising(fixture: _Fixture) -> None:
    """PRD 02: unmatched items are never dropped, and there will always be some.

    `merge_from_source` answers a target-less merge with `PortDataMalformed`, which on
    the push lane would take the channel down and cost a reconnect plus a gap-closing
    delta walk.
    """
    outcome = await fixture.service.apply_states(
        fixture.source.id,
        [SourceWatchState(external_id="unknown", position_seconds=1, played=False)],
        user_id=fixture.user_id,
        observed_at=T0,
    )
    assert outcome.merged == ()
    assert outcome.unmatched == 1


async def test_apply_states_enqueues_a_history_backfill_for_a_played_unknown_count(
    fixture: _Fixture,
) -> None:
    """The same merge chain, reached from the push lane exactly as it is from a walk.

    A `UserDataChanged` entry carries no trustworthy `PlayCount`, so every played item
    pushed produces one of these.
    """
    await fixture.given_matched("movie-1")
    outcome = await fixture.service.apply_states(
        fixture.source.id,
        [SourceWatchState(external_id="movie-1", position_seconds=0, played=True, play_count=None)],
        user_id=fixture.user_id,
        observed_at=T0,
    )
    queued = await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10)
    assert [(job.kind, job.key) for job in queued] == [(JobKind.WATCH_HISTORY, "movie-1")]
    assert outcome.needing_history == ("movie-1",)


async def test_apply_states_reports_merges_built_not_rows_changed(fixture: _Fixture) -> None:
    """`items_matched` on a `SyncRun` is the number of merges *built*.

    `merge_from_source` returns rows *changed*, and the two differ whenever PRD 03's
    "latest wins" refuses one -- a client set a resume position thirty seconds ago and
    a walk that started an hour ago must not stomp it. Returning the repository's count
    in `items_matched`'s place silently changes what every stored `sync_runs` row means
    and what PRD 10's dashboard plots.
    """
    await fixture.given_matched("movie-1")
    fixture.watch_states.refuse_next_merge()
    outcome = await fixture.service.apply_states(
        fixture.source.id,
        [SourceWatchState(external_id="movie-1", position_seconds=61, played=False)],
        user_id=fixture.user_id,
        observed_at=T0,
    )
    assert len(outcome.merged) == 1
    assert outcome.rows_written == 0


async def test_a_walk_still_reports_the_same_counters_after_the_split(
    fixture: _Fixture,
) -> None:
    """`items_matched` stays the number of merges *built*, not the rows written.

    `merge_from_source` returns rows *changed*, and reporting that in `items_matched`'s
    place would change what every stored `sync_runs` row means. The two agree on every
    batch where nothing is refused, which is nearly all of them, so nothing else fails.
    """
    await fixture.given_completed_walk()
    await fixture.given_matched("movie-1")
    fixture.adapter.seed(_item("orphan-1"), T0)
    fixture.watch_states.refuse_next_merge()
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert (run.items_seen, run.items_matched, run.items_unmatched) == (2, 1, 1)


# -- the enqueue, which is what bounds the backfill --------------------------


async def test_a_played_item_with_unknown_history_is_enqueued_for_backfill(
    fixture: _Fixture,
) -> None:
    """The bounded recovery.

    Enqueued rather than fetched inline: one request per item against a whole library
    is not a walk, it is a week.
    """
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=True, play_count=7)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    queued = await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10)
    assert [job.key for job in queued] == ["movie-1"]
    assert queued[0].priority == JobPriority.BACKFILL


async def test_an_unplayed_item_is_not_enqueued_for_backfill(fixture: _Fixture) -> None:
    """A household has played a few thousand of a library's million items.

    An enqueue predicate that ignored `played` would also queue every unplayed
    state a walk reports.
    """
    await fixture.given_completed_walk()
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=False)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10) == []


async def test_a_played_item_whose_count_the_source_reported_is_not_enqueued(
    fixture: _Fixture,
) -> None:
    """The other half of the predicate, and the half `played` alone does not cover.

    A source whose walk *can* count (the contract permits it, and Jellyfin's listing may
    well) needs no backfill at all -- enqueueing one per played item anyway is a
    standing queue of thousands of requests that can only ever confirm what is already
    stored.
    """
    honest = FakeSourceAdapter(fixture.source)
    await fixture.given_matched("movie-1")
    honest.seed(_item("movie-1"), T0)
    honest.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=True, play_count=4)
    )
    await fixture.service.sync(fixture.source, honest, user_id=fixture.user_id)
    assert await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10) == []


async def test_an_unmatched_played_item_is_not_enqueued_for_backfill(fixture: _Fixture) -> None:
    """There is no row to backfill into.

    A job for one parks or no-ops forever, and the review queue is the lane that
    fixes it.
    """
    fixture.adapter.seed(_item("orphan-1"), T0)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="orphan-1", position_seconds=0, played=True, play_count=2)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10) == []


# -- the backfill ------------------------------------------------------------


async def test_the_backfill_writes_the_authoritative_count(fixture: _Fixture) -> None:
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=0,
            played=True,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    await fixture.service.backfill_one(
        fixture.source, fixture.adapter, external_id="movie-1", user_id=fixture.user_id
    )
    stored = await fixture.stored("movie-1")
    assert stored is not None
    assert stored.play_count == 7  # type: ignore[attr-defined]
    assert stored.last_played_at == LAST_PLAYED  # type: ignore[attr-defined]


async def test_a_backfill_right_after_a_walk_is_not_rejected_as_stale(
    fixture: _Fixture,
) -> None:
    """`observed_at` is the instant the backfill read the source, never the run's.

    PRD 03's "latest wins" applies to the whole record, so a backfill carrying an instant
    at or before the row's stored `updated_at` writes nothing -- and a row a client wrote
    since the walk began holds a later instant than any the walk could hand it. The
    backfill would then never converge and nothing in this file would say so; the paired
    integration case is what actually closes it.
    """
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=True, play_count=9)
    )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    await fixture.service.backfill_one(
        fixture.source, fixture.adapter, external_id="movie-1", user_id=fixture.user_id
    )
    stored = await fixture.stored("movie-1")
    assert stored is not None
    assert stored.play_count == 9  # type: ignore[attr-defined]
    assert stored.updated_at > run.started_at  # type: ignore[attr-defined]


async def test_a_backfill_of_a_deleted_item_is_not_an_error(fixture: _Fixture) -> None:
    """`get_watch_state` returns `None` for an item the source no longer has.

    Exactly as `get_item` does. A backfill job for one must complete rather than park:
    the item's disappearance is
    what the reconcile lane handles, and parking here would fill the poison list with
    items that were simply deleted.
    """
    await fixture.given_matched("movie-1")
    fixture.adapter.forget("movie-1")
    assert (
        await fixture.service.backfill_one(
            fixture.source, fixture.adapter, external_id="movie-1", user_id=fixture.user_id
        )
        is False
    )


async def test_a_backfill_of_an_item_this_source_never_had_is_not_an_error(
    fixture: _Fixture,
) -> None:
    """A queued job can outlive the media item it names.

    An operator removed the source, or the review queue re-resolved it. Quiet, because
    the alternative is a parked job an operator has to dismiss by hand.
    """
    assert (
        await fixture.service.backfill_one(
            fixture.source, fixture.adapter, external_id="never-existed", user_id=fixture.user_id
        )
        is False
    )


async def test_the_backfill_asks_the_source_only_about_items_it_can_store(
    fixture: _Fixture,
) -> None:
    """The cheap check before the expensive one.

    A single-item request costs seconds; resolving the target first is one indexed
    read, and an unmatched item has nowhere for the answer to land.
    """
    fixture.adapter.seed(_item("orphan-1"), T0)
    before = fixture.adapter.authentications
    assert (
        await fixture.service.backfill_one(
            fixture.source, fixture.adapter, external_id="orphan-1", user_id=fixture.user_id
        )
        is False
    )
    assert fixture.adapter.authentications == before, "the source was asked about an unmatched item"


async def test_the_backfill_sweep_terminates(fixture: _Fixture) -> None:
    """The convergence claim, run rather than argued.

    Seven played items whose history the walk could not report, drained three at a
    time: the population has to empty, and in the number of passes the arithmetic
    predicts rather than eventually.

    The loop is bounded so a non-converging predicate fails the case instead
    of hanging the suite -- which is exactly what a backfill that wrote
    nothing (a stale `observed_at`), or one that never moved a row out of
    `played AND play_count = 0`, would do in production.
    """
    for index in range(7):
        await fixture.given_matched(f"movie-{index}")
        fixture.adapter.seed_state(
            SourceWatchState(
                external_id=f"movie-{index}",
                position_seconds=0,
                played=True,
                play_count=index + 1,
            )
        )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert len(await fixture.watch_states.list_needing_history()) == 7

    passes = 0
    while await fixture.watch_states.list_needing_history():
        assert passes < 5, "the backfill is not converging"
        filled = await fixture.service.backfill_history(fixture.source, fixture.adapter, limit=3)
        assert filled > 0, "a pass that recovers nothing repeats forever"
        passes += 1
    assert passes == 3, passes
    for index in range(7):
        stored = await fixture.stored(f"movie-{index}")
        assert stored is not None
        assert stored.play_count == index + 1  # type: ignore[attr-defined]


async def test_a_source_that_cannot_answer_leaves_the_sweep_rotating_not_stuck(
    fixture: _Fixture,
) -> None:
    """The honest other half of the convergence claim.

    Emby's own `POST .../PlayedItems/{item}` never leaves a played item at `PlayCount:
    0`, so the predicate empties -- but that is a property of the *source*, not of this
    code, and a source that cannot say leaves rows matching forever.

    What this file can guarantee is that such a row costs one request per
    pass and does not starve the others: `list_needing_history` is
    oldest-first and a merge moves the row's `updated_at`, so the third pass
    is looking at different rows from the first.
    """

    class _AlsoLossyOnTheItemRoute(_LossySourceAdapter):
        async def get_watch_state(self, external_id: str) -> SourceWatchState | None:
            state = await super().get_watch_state(external_id)
            return None if state is None else dataclasses.replace(state, play_count=None)

    stuck = _AlsoLossyOnTheItemRoute(fixture.source)
    for index in range(4):
        await fixture.given_matched(f"movie-{index}")
        stuck.seed(_item(f"movie-{index}"), T0)
        stuck.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=0, played=True)
        )
    await fixture.service.sync(fixture.source, stuck, user_id=fixture.user_id)
    assert len(await fixture.watch_states.list_needing_history()) == 4

    first = await fixture.watch_states.list_needing_history(limit=2)
    await fixture.service.backfill_history(fixture.source, stuck, limit=2)
    second = await fixture.watch_states.list_needing_history(limit=2)
    assert len(await fixture.watch_states.list_needing_history()) == 4, "a row left the predicate"
    assert set(first).isdisjoint(second), "the sweep re-reads the same two rows forever"


async def test_the_backfill_writes_to_each_rows_own_user(fixture: _Fixture) -> None:
    """`list_needing_history` reports the owner of every row it returns.

    A backfill that wrote them all to one user would move a second household member's
    history onto the first -- silently, and only once there were two of them.

    Two rows, two users, deliberately: with one row in the sweep every reading of
    "whose row is this" agrees, and a backfill that took the first row's owner for all
    of them passes.
    """
    viewers = [new_id(), new_id()]
    for index, viewer in enumerate(viewers, start=1):
        external_id = f"movie-{index}"
        await fixture.given_matched(external_id)
        fixture.adapter.seed_state(
            SourceWatchState(
                external_id=external_id, position_seconds=0, played=True, play_count=index + 4
            )
        )
        await fixture.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=viewer,
                    title_id=fixture.titles[external_id],
                    episode_id=None,
                    position_seconds=0,
                    played=True,
                    runtime_seconds=None,
                    observed_at=T0,
                    play_count=None,
                )
            ]
        )
    assert await fixture.service.backfill_history(fixture.source, fixture.adapter) == 2
    for index, viewer in enumerate(viewers, start=1):
        mine = await fixture.watch_states.get_for_title(viewer, fixture.titles[f"movie-{index}"])
        assert mine is not None
        assert mine.play_count == index + 4
        theirs = viewers[index % 2]
        assert (
            await fixture.watch_states.get_for_title(theirs, fixture.titles[f"movie-{index}"])
            is None
        ), "one viewer's history was written to the other's row"


async def test_an_episodes_history_is_backfilled_through_its_own_file(
    fixture: _Fixture,
) -> None:
    """The backfill's reverse lookup is dominated by episodes, not by movies.

    A title-keyed reverse lookup would answer an episode's row with its series'
    `external_id` and backfill 24 episodes from one number.
    """
    episode_id = await fixture.given_matched("episode-1", episode=True)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="episode-1", position_seconds=0, played=True, play_count=2)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert await fixture.service.backfill_history(fixture.source, fixture.adapter) == 1
    stored = await fixture.watch_states.get_for_episode(fixture.user_id, episode_id)
    assert stored is not None
    assert stored.play_count == 2


# -- the run, and its failure paths ------------------------------------------


async def test_a_run_is_recorded_and_checkpointed_per_batch(fixture: _Fixture) -> None:
    fixture.service._batch_size = 2
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")
        fixture.adapter.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=1, played=False)
        )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.kind is SyncRunKind.WATCH_STATE
    assert (run.items_seen, run.items_matched) == (5, 5)
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.status is SyncRunStatus.COMPLETED
    # Three batches, plus the run's own insert and its final save.
    assert fixture.commits >= 5, fixture.commits


async def test_a_walk_that_raises_keeps_the_batches_it_already_merged(
    fixture: _Fixture,
) -> None:
    """The same trap `ReconcileService` documents, one lane over.

    `SyncRun` is frozen and the per-batch checkpoint is an evolved copy, so a failure
    handler that evolves its own pre-walk binding writes `items_seen = 0` over a
    checkpoint that recorded four.
    """
    fixture.service._batch_size = 2
    for index in range(6):
        await fixture.given_matched(f"movie-{index}")
        fixture.adapter.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=index + 1, played=False)
        )
    fixture.adapter.fail_after(4)
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.status is SyncRunStatus.FAILED
    assert run.error
    assert run.items_seen == 4
    stored = await fixture.runs.get(run.id)
    assert stored is not None
    assert stored.items_seen == 4, "the failure handler regressed the checkpoint"
    assert await fixture.stored("movie-0") is not None


async def test_a_run_that_could_not_reach_the_source_is_recorded_not_raised(
    fixture: _Fixture,
) -> None:
    """`usher sync` runs the second and third source when the first is unreachable."""
    fixture.adapter.go_offline()
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.status is SyncRunStatus.FAILED
    assert run.items_seen == 0


async def test_sync_never_raises_a_port_error(fixture: _Fixture) -> None:
    """Every `UsherPortError` subclass, not just the one the offline case happens to produce."""

    class _Boom(UsherPortError):
        pass

    for error in (_Boom("custom"), PortUnavailable("gone")):
        one = _Fixture()
        await one.given_matched("movie-1")
        one.adapter.seed_state(
            SourceWatchState(external_id="movie-1", position_seconds=1, played=False)
        )

        async def _raise(*args: object, __exc: BaseException = error, **kwargs: object) -> None:
            raise __exc

        one.watch_states.merge_from_source = _raise  # type: ignore[method-assign, assignment]
        run = await one.service.sync(one.source, one.adapter, user_id=one.user_id)
        assert run.status is SyncRunStatus.FAILED


async def test_a_bug_is_not_recorded_as_an_upstream_failure(fixture: _Fixture) -> None:
    """A `ZeroDivisionError` is a bug in this process.

    Recording it as a failed *sync* hides it behind an operational-looking row.
    """

    async def _explode(*args: object, **kwargs: object) -> None:
        raise ZeroDivisionError("a bug, not an outage")

    fixture.watch_states.merge_from_source = _explode  # type: ignore[method-assign, assignment]
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=1, played=False)
    )
    with pytest.raises(ZeroDivisionError):
        await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)


async def test_a_second_run_resumes_from_the_last_completed_one(fixture: _Fixture) -> None:
    """The watch-state lane owns its own cursor.

    It walks a different method under a different upstream filter
    (`MinDateLastSavedForUser`, which selects a genuinely different set from
    `MinDateLastSaved`), so resuming from an item-lane run would skip whatever changed
    in between.
    """
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=1, played=False)
    )
    first = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert first.cursor_at is None
    await fixture.given_matched("movie-2", changed_at=LATER)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-2", position_seconds=2, played=False)
    )
    second = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert second.cursor_at == first.started_at
    assert second.items_seen == 1, "the second run re-walked what the first already had"


async def test_a_failed_run_does_not_advance_the_cursor(fixture: _Fixture) -> None:
    """Resuming from a run that failed halfway skips everything it never reached, silently."""
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=1, played=False)
    )
    completed = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    # Changed *after* the completed run, so the failing walk below has
    # something to reach the failure on: `FakeSourceAdapter` filters on
    # `changed_at < since` exactly as `MinDateLastSavedForUser` does, and a
    # walk with nothing left to yield never raises at all.
    await fixture.given_matched("movie-2", changed_at=LATER)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-2", position_seconds=2, played=False)
    )
    fixture.adapter.fail_after(0)
    failed = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert failed.status is SyncRunStatus.FAILED
    fixture.adapter.clear_failure()
    third = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert third.cursor_at == completed.started_at


# -- telemetry, and what a fake cannot say -----------------------------------


async def test_the_sync_span_is_a_child_of_whatever_is_active(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    await fixture.given_matched("movie-1")
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("server") as server:
        expected = server.get_span_context().trace_id
        await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    pipeline = [span for span in spans.get_finished_spans() if span.name == "sync.watch_state"]
    assert pipeline, [span.name for span in spans.get_finished_spans()]
    assert pipeline[0].context is not None
    assert pipeline[0].context.trace_id == expected
    assert pipeline[0].parent is not None


def test_the_service_never_imports_a_storage_or_transport_library() -> None:
    """PRD 01's layering rule, at module level.

    `import-linter` already forbids `usher.services -> usher.db`; this catches the other
    half, which no contract expresses.
    """
    import usher.services.watch_sync as module

    source = (module.__file__ or "").replace(".pyc", ".py")
    text = open(source).read()  # noqa: SIM115
    for forbidden in ("httpx", "sqlalchemy", "asyncpg", "usher.db"):
        assert f"import {forbidden}" not in text
        assert f"from {forbidden}" not in text


async def test_the_walk_is_the_only_thing_that_needs_a_source_user_map(
    fixture: _Fixture,
) -> None:
    """`SourceWatchState.source_user_id` is carried and deliberately not consulted.

    v1 has one user, and mapping a source's user ids onto Usher's is a later problem.
    What must not happen quietly is a *second* source user's state landing on the
    singleton, so the value being ignored is recorded here rather than discovered later.
    """
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=30,
            played=False,
            source_user_id="emby-user-2",
        )
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    stored = await fixture.stored("movie-1")
    assert stored is not None
    assert stored.user_id == fixture.user_id  # type: ignore[attr-defined]


# -- the deliberate silence ------------------------------------------------


async def test_the_walk_publishes_nothing_and_the_push_lane_through_the_same_chain_does(
    fixture: _Fixture,
) -> None:
    """The walk publishes nothing, which is a scale decision rather than an omission.

    A walk merges states in bulk -- a first walk every state the account has watched,
    a delta every change since its cursor -- and one `watchstate.updated` per merged
    row is a fan-out per row per night to every connected client. The push lane
    publishes because a push event *is* a change.

    The reachable version of this defect is the *shared chain*: `apply_states` has
    exactly two callers -- this walk and `PushApplyService` -- and moving the publish
    down into it is the obvious de-duplication, so this drives the walk with the very
    publisher the push lane uses.

    The second half is not decoration. Without it the case passes against a harness
    that could not observe a publish at all, which is the shape a "publishes nothing"
    assertion fails silently in.
    """
    await fixture.given_matched("i1")
    fixture.adapter.seed_state(SourceWatchState(external_id="i1", position_seconds=5, played=False))
    events = FakeEventPublisher()
    applier = PushApplyService(
        ingest=_no_ingest(),
        watch=fixture.service,
        events=events,
        commit=fixture._commit,
    )

    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert events.published == [], "the nightly walk published a client event"

    await applier.apply(
        fixture.source,
        fixture.adapter,
        SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=("i1",)),
        user_id=fixture.user_id,
    )
    # `row.invalidated` rides the same publish, and the whole sequence is
    # asserted rather than filtered: this case's entire point is *which lane
    # publishes what*, so a filtered assertion here would stop seeing the lane
    # that grew a fan-out.
    assert [event.kind for event in events.published] == [
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.WATCHSTATE_UPDATED,
    ]


def test_the_walk_has_no_event_publisher_to_publish_through() -> None:
    """The tripwire on the first step of the change above.

    There is no publisher on this service at all, so a reader who wanted a
    `watchstate.updated` per merged row has to add one here before anything else -- and
    this is the line that says why not to.
    """
    assert "events" not in inspect.signature(WatchStateSyncService.__init__).parameters


def _no_ingest() -> IngestService:
    """`PushApplyService` needs one, built rather than stubbed so the service is real."""
    titles = FakeTitleRepository()
    matching = FakeTitleMatchRepository(titles)
    queue = FakeJobQueue()
    return IngestService(
        matcher=MatchService(titles=titles, matching=matching, queue=queue),
        matching=matching,
        media_items=FakeMediaItemRepository(),
        episodes=FakeEpisodeRepository(),
        queue=queue,
    )


# -- a watch run never resumes: a failed one costs a re-read from its cursor --


async def test_a_failed_walk_is_read_again_from_its_cursor_in_a_run_of_its_own(
    fixture_batched: _Fixture,
) -> None:
    """A failed delta costs a re-read from its cursor, never a resume from its position.

    A `StartIndex` into a listing that has lost items since would skip some. Batched at
    2, so the failed attempt really did commit a position a resume could start from.
    """
    await fixture_batched.given_completed_walk()
    for index in range(6):
        await fixture_batched.given_matched(f"movie-{index}")
    fixture_batched.adapter.fail_after(3)

    first = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )
    assert first.status is SyncRunStatus.FAILED
    assert first.position == 2, (
        "the premise: one batch of two committed before the third yield raised"
    )

    fixture_batched.adapter.clear_failure()
    second = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert second.id != first.id, "the failed run's row was reclaimed"
    assert (second.status, second.cursor_at, second.items_seen) == (
        SyncRunStatus.COMPLETED,
        T0,
        6,
    )
    assert fixture_batched.adapter.resumed_from == [0, 0], (
        "the second attempt started from the failed run's position"
    )
    assert await fixture_batched.runs.get(first.id) == first, "the failed run was written again"


async def test_a_walk_whose_newest_run_completed_starts_fresh(fixture: _Fixture) -> None:
    """The other half: a completed walk is followed by a delta from its `started_at`.

    At position zero, not by a resume.
    """
    await fixture.given_matched("movie-0")
    done = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert done.status is SyncRunStatus.COMPLETED

    again = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert again.id != done.id, "a completed run is not reclaimed"
    assert again.position == 0
    assert again.cursor_at == done.started_at
    assert fixture.adapter.resumed_from == [0, 0]


async def test_the_position_advances_per_committed_batch(fixture_batched: _Fixture) -> None:
    """`position` counts the states this run has committed, never the batch in flight.

    It is saved with every batch, the trailing partial one included, and nothing reads
    it back: a watch run is never resumed.
    """
    await fixture_batched.given_completed_walk()
    for index in range(5):
        await fixture_batched.given_matched(f"movie-{index}")

    run = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.position == 5
    assert fixture_batched.positions == [2, 4, 5], (
        "position must be saved with every batch, including the trailing partial one"
    )


async def test_a_failed_walk_keeps_the_position_it_reached(
    fixture_batched: _Fixture,
) -> None:
    """`_Progress`' reason, for `position` as for the counters.

    A failure handler holding the pre-walk run reports `position = 0` for an attempt
    that committed two.

    The defect is visible on the run the service **returns**, which is asserted first.
    `save` is non-destructive -- `position` merges as `GREATEST` on both arms, so the
    stored row keeps its 2 whatever a failed attempt hands it -- and the durable read
    after it is a different claim: that the per-batch checkpoint reached the repository
    at all, and that the two agree.
    """
    await fixture_batched.given_completed_walk()
    for index in range(6):
        await fixture_batched.given_matched(f"movie-{index}")
    fixture_batched.adapter.fail_after(3)

    run = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert run.status is SyncRunStatus.FAILED
    # **This is the line that catches the regression**; the durable read below is a
    # different claim.
    assert run.position == 2, (
        "the failure handler evolved its pre-walk binding, so the run this attempt "
        "reports has lost the page it committed"
    )
    stored = await fixture_batched.runs.get(run.id)
    assert stored is not None and stored.position == 2, (
        "the run the service returned and the row it left behind disagree: the "
        "per-batch checkpoint never reached the repository"
    )


async def test_a_first_walk_that_already_failed_keeps_its_own_error(fixture: _Fixture) -> None:
    """Closed already, so it is not relabelled: its error says why that walk failed."""
    failed = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.FAILED,
        position=40,
        error="GET /Users/{user_id}/Items returned HTTP 502",
        started_at=T0,
        finished_at=T0,
    )
    await fixture.runs.add(failed)

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.id != failed.id, "the failed first walk was resumed"
    assert fixture.adapter.resumed_from == [0]
    stored = await fixture.runs.get(failed.id)
    assert stored is not None
    assert stored.error == "GET /Users/{user_id}/Items returned HTTP 502"


# -- a dead watch run is closed, and a live one is left alone ----------------


async def _given_watch_run(
    fixture: _Fixture,
    *,
    heartbeat_at: datetime | None,
    status: SyncRunStatus = SyncRunStatus.RUNNING,
    delta: bool = True,
    cursor_at: datetime = T0,
) -> SyncRun:
    """An unfinished watch run two states in, as another process left it, live or dead.

    A delta follows a walk this stores first, completed at `T0`, and reads from
    `cursor_at`; a first walk has neither. The run starts an hour after `T0`, so it is
    the newest.
    """
    if delta:
        await fixture.given_completed_walk(at=T0)
    run = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=status,
        cursor_at=cursor_at if delta else None,
        position=2,
        items_seen=2,
        heartbeat_at=heartbeat_at,
        started_at=T0 + timedelta(hours=1),
    )
    await fixture.runs.add(run)
    return run


async def _three(fixture: _Fixture) -> None:
    for index in range(3):
        await fixture.given_matched(f"movie-{index}")


@contextlib.contextmanager
def _warnings() -> Iterator[list[str]]:
    """Every WARNING logged inside the block, one message per entry."""
    lines: list[str] = []
    sink = logger.add(
        lambda line: lines.append(line.rstrip("\n")), level="WARNING", format="{message}"
    )
    try:
        yield lines
    finally:
        logger.remove(sink)


@pytest.mark.parametrize(
    "heartbeat_at", [NOW - STALE_AFTER, None], ids=["ten-minutes-old", "no-heartbeat"]
)
async def test_an_abandoned_watch_run_is_closed_and_a_fresh_one_reads_from_its_cursor(
    heartbeat_at: datetime | None,
) -> None:
    """Closed by this start, so its cursor, older than the lane's, is the one read from."""
    fixture = _Fixture()
    await _three(fixture)
    dead = await _given_watch_run(
        fixture, heartbeat_at=heartbeat_at, cursor_at=T0 - timedelta(days=1)
    )
    lane = await fixture.runs.latest_completed_cursor(fixture.source.id, SyncRunKind.WATCH_STATE)
    assert dead.cursor_at is not None and lane is not None and dead.cursor_at < lane, (
        "the premise: the dead run read from before the lane's cursor"
    )
    with _warnings() as lines:
        run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    closed = await fixture.runs.get(dead.id)
    assert closed is not None
    assert (closed.status, closed.error, closed.finished_at) == (
        SyncRunStatus.FAILED,
        ABANDONED_ERROR,
        NOW,
    )
    assert run.id != dead.id
    assert (run.status, run.cursor_at, run.items_seen) == (
        SyncRunStatus.COMPLETED,
        dead.cursor_at,
        3,
    )
    assert [await fixture.stored(f"movie-{index}") is not None for index in range(3)] == [
        True,
        True,
        True,
    ], "a state before the dead run's position was not read"
    assert lines == ["closed 1 watch-state run(s) of Living Room Emby whose process had stopped"]


async def test_a_failed_watch_run_is_not_resumed() -> None:
    """Closed already, so it is neither written nor counted as closed."""
    fixture = _Fixture()
    await _three(fixture)
    failed = await _given_watch_run(fixture, heartbeat_at=None, status=SyncRunStatus.FAILED)
    with _warnings() as lines:
        run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.id != failed.id and run.items_seen == 3
    assert await fixture.runs.get(failed.id) == failed
    assert lines == []


async def test_a_live_watch_run_is_left_running() -> None:
    """Nothing is closed, so nothing is logged, and a live run's cursor is not carried."""
    fixture = _Fixture()
    await _three(fixture)
    live = await _given_watch_run(
        fixture,
        heartbeat_at=NOW - STALE_AFTER + timedelta(microseconds=1),
        cursor_at=T0 - timedelta(days=1),
    )
    with _warnings() as lines:
        run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.id != live.id and run.status is SyncRunStatus.COMPLETED
    assert await fixture.runs.get(live.id) == live
    assert run.cursor_at == T0
    assert lines == []


async def test_a_live_first_watch_walk_is_left_alone_and_a_new_one_completes_beside_it(
    fixture: _Fixture,
) -> None:
    """Closing it would write under a live walk, so this walk takes a row of its own."""
    await fixture.given_matched("movie-0")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-0", position_seconds=0, played=True)
    )
    live = await _given_watch_run(fixture, heartbeat_at=NOW, delta=False)

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert await fixture.runs.get(live.id) == live, "the live first walk's row was written"
    assert run.id != live.id
    assert (run.status, run.cursor_at, run.items_matched) == (SyncRunStatus.COMPLETED, None, 1)
    assert fixture.adapter.resumed_from == [0]
    assert len(await fixture.runs.list_for_source(fixture.source.id)) == 2


async def test_a_watch_walk_that_fails_before_its_first_batch_still_leaves_its_heartbeat(
    fixture: _Fixture,
) -> None:
    """The insert carries the first beat, so a walk that dies at once is still dated."""
    await fixture.given_matched("movie-0")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-0", position_seconds=0, played=True)
    )
    fixture.adapter.fail_after(0)
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert (run.status, run.items_seen) == (SyncRunStatus.FAILED, 0), "the premise: no batch"
    assert run.heartbeat_at == NOW


async def test_every_batch_of_a_watch_walk_moves_its_heartbeat() -> None:
    """A clock a second on at every read: the insert reads `NOW`, and each batch the next."""
    fixture = _Fixture(batch_size=2, clock=_Clock(NOW, step=timedelta(seconds=1)))
    await fixture.given_completed_walk()
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")

    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert fixture.positions == [2, 4, 5], "the premise: three batches committed"
    beats = [saved.heartbeat_at for saved in fixture.saved if saved.status is SyncRunStatus.RUNNING]
    assert beats == [NOW + timedelta(seconds=seconds) for seconds in (1, 2, 3)]


# -- a failed run's cursor is carried until a run that covers it completes ----

WALK_BEGAN = datetime(2026, 9, 1, tzinfo=UTC)


async def _given_a_failed_run_after_a_walk(fixture: _Fixture) -> tuple[SyncRun, SyncRun]:
    """`usher sync`'s two watch runs, the first completed and the second failed.

    The first, the walk's hook, reads before the walk stores `movie-1`, whose state was
    saved after the walk began; the second reads from the walk's start and fails on it.
    """
    await fixture.given_completed_walk(at=T0)
    seeded = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    await fixture.given_matched("movie-1", changed_at=WALK_BEGAN + timedelta(hours=12))
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=640, played=False)
    )
    fixture.adapter.fail_after(0)
    after_walk = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=WALK_BEGAN
    )
    assert (seeded.status, after_walk.status) == (SyncRunStatus.COMPLETED, SyncRunStatus.FAILED)
    assert after_walk.cursor_at == WALK_BEGAN < seeded.started_at, (
        "the premise: the failed run read from before the completed run began"
    )
    return seeded, after_walk


async def test_a_failed_runs_cursor_is_carried_past_a_newer_completion(fixture: _Fixture) -> None:
    """The lane's cursor is the completed run's start; the next run reads from the failed one's.

    From the completion, `movie-1`'s state, saved before it, would never be read.
    """
    _, after_walk = await _given_a_failed_run_after_a_walk(fixture)
    fixture.adapter.clear_failure()

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert (run.status, run.cursor_at, run.items_seen) == (
        SyncRunStatus.COMPLETED,
        after_walk.cursor_at,
        1,
    )
    assert await fixture.stored("movie-1") is not None


async def test_a_carried_cursor_survives_a_run_that_fails_again(fixture: _Fixture) -> None:
    _, after_walk = await _given_a_failed_run_after_a_walk(fixture)
    again = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert (again.status, again.cursor_at) == (SyncRunStatus.FAILED, after_walk.cursor_at)
    fixture.adapter.clear_failure()

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert (run.status, run.cursor_at, run.items_seen) == (
        SyncRunStatus.COMPLETED,
        after_walk.cursor_at,
        1,
    )


async def test_a_failed_run_carries_nothing_once_a_run_that_covers_it_completes(
    fixture: _Fixture,
) -> None:
    """Started after it, from its cursor, that run read the failed one's window whole."""
    failed = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.FAILED,
        cursor_at=T0 - timedelta(days=1),
        started_at=T0,
        finished_at=T0,
    )
    covering = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.COMPLETED,
        cursor_at=failed.cursor_at,
        started_at=T0 + timedelta(hours=1),
        finished_at=T0 + timedelta(hours=1),
    )
    for one in (failed, covering):
        await fixture.runs.add(one)
    lane = await fixture.runs.latest_completed_cursor(fixture.source.id, SyncRunKind.WATCH_STATE)
    assert lane == covering.started_at > failed.started_at, "the premise: it started after"

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.cursor_at == lane


async def test_a_failed_runs_cursor_outlives_a_run_that_walked_beside_it(
    fixture: _Fixture,
) -> None:
    """The run beside it read from the lane's later cursor, so it covered nothing.

    `usher sync`'s run after the walk reads from the walk's start; a run in another
    process, started while it was alive, reads from the lane's cursor and completes.
    The failed run's window is still read again.
    """
    lane_at = WALK_BEGAN + timedelta(days=1)
    await fixture.given_completed_walk(at=lane_at)
    after_walk = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.FAILED,
        cursor_at=WALK_BEGAN,
        started_at=lane_at + timedelta(hours=1),
    )
    beside = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.COMPLETED,
        cursor_at=lane_at,
        started_at=lane_at + timedelta(hours=2),
    )
    for one in (after_walk, beside):
        await fixture.runs.add(one)
    await fixture.given_matched("movie-1", changed_at=WALK_BEGAN + timedelta(hours=12))
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=640, played=False)
    )
    assert beside.started_at > after_walk.started_at, "the premise: it started after"
    assert beside.cursor_at is not None and after_walk.cursor_at is not None
    assert beside.cursor_at > after_walk.cursor_at, "the premise: and read from later"

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert (run.status, run.cursor_at, run.items_seen) == (
        SyncRunStatus.COMPLETED,
        after_walk.cursor_at,
        1,
    )
    assert await fixture.stored("movie-1") is not None


async def test_the_oldest_carried_cursor_is_read_from_not_the_newest_runs(
    fixture: _Fixture,
) -> None:
    await fixture.given_completed_walk(at=T0)
    for days, hours in ((2, 1), (1, 2)):
        await fixture.runs.add(
            SyncRun(
                source_id=fixture.source.id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.FAILED,
                cursor_at=T0 - timedelta(days=days),
                started_at=T0 + timedelta(hours=hours),
            )
        )

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.cursor_at == T0 - timedelta(days=2)


async def test_a_failed_first_walk_is_followed_by_a_first_walk(fixture: _Fixture) -> None:
    await _given_watch_run(fixture, heartbeat_at=None, status=SyncRunStatus.FAILED, delta=False)
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert (run.status, run.cursor_at) == (SyncRunStatus.COMPLETED, None)


async def test_a_failed_first_walk_newer_than_a_completion_is_carried_as_one(
    fixture: _Fixture,
) -> None:
    """`None` reads from the beginning, which is older than the completion's instant."""
    await fixture.given_completed_walk(at=T0)
    await fixture.runs.add(
        SyncRun(
            source_id=fixture.source.id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.FAILED,
            started_at=T0 + timedelta(hours=1),
        )
    )

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert (run.status, run.cursor_at) == (SyncRunStatus.COMPLETED, None)


async def test_a_lane_without_a_cursor_reads_from_the_beginning_whatever_is_carried(
    fixture: _Fixture,
) -> None:
    """`None` is older than any instant: the lane's own, as much as a failed run's."""
    await fixture.runs.add(
        SyncRun(
            source_id=fixture.source.id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.FAILED,
            cursor_at=T0,
            started_at=T0 + timedelta(hours=1),
        )
    )
    lane = await fixture.runs.latest_completed_cursor(fixture.source.id, SyncRunKind.WATCH_STATE)
    assert lane is None, "the premise: the lane has no cursor"

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert (run.status, run.cursor_at) == (SyncRunStatus.COMPLETED, None)


# -- since_at_most: the run after a walk reads back to the walk's start ------


async def test_since_at_most_moves_a_fresh_deltas_cursor_back(fixture: _Fixture) -> None:
    """A state saved after the walk began, and before this lane's last run, is read."""
    walk_began = datetime(2026, 9, 1, tzinfo=UTC)
    await fixture.given_completed_walk(at=walk_began + timedelta(days=1))
    await fixture.given_matched("movie-0")
    await fixture.given_matched("movie-1", changed_at=walk_began + timedelta(hours=12))
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=640, played=False)
    )

    run = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=walk_began
    )

    assert run.cursor_at == walk_began
    assert run.items_seen == 1, "the walk did not read from the moved cursor"


async def test_since_at_most_never_moves_a_cursor_forward(fixture: _Fixture) -> None:
    await fixture.given_completed_walk(at=T0)
    run = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=LATER
    )
    assert run.cursor_at == T0


async def test_since_at_most_leaves_a_first_walk_without_a_cursor(fixture: _Fixture) -> None:
    """No completed run, so nothing to move back: the walk asks for what was watched."""
    run = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=T0
    )
    assert run.cursor_at is None


# -- beat: a caller's run, waiting on this walk, kept alive by it ------------


async def test_beat_is_awaited_once_after_each_batch_the_walk_commits() -> None:
    """Each beat sees its batch's commit and not the next one's.

    A walk commits once as it starts, once per batch and once as it ends, so the
    commits a beat counts are its batch's place plus the start.
    """
    fixture = _Fixture(batch_size=2)
    await fixture.given_completed_walk()
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")
    beats: list[int] = []

    async def beat() -> None:
        beats.append(fixture.commits)

    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id, beat=beat)

    assert fixture.positions == [2, 4, 5], "the premise: three batches committed"
    assert fixture.commits == 5, "the premise: one commit to start, one per batch, one to end"
    assert beats == [2, 3, 4]


@pytest.mark.parametrize("heartbeat_seconds", [0, -0.5, float("nan")])
def test_a_service_whose_heartbeat_is_not_positive_is_refused(heartbeat_seconds: float) -> None:
    """Its beat would always be due, so the walk would beat and never take a state."""
    with pytest.raises(ValueError, match=f"a positive period, not {heartbeat_seconds} seconds"):
        _Fixture(heartbeat_seconds=heartbeat_seconds)


class _StallingSourceAdapter(_LossySourceAdapter):
    """A source whose walk stalls before its state at `stall_after`, as a page under retry does.

    `stalled` is set as the stall begins. `release` ends it, raising `error` if one was
    set; `cancelled` is set if the walk is cancelled while it stalls. `closed` is set
    once the walk is closed, however it ends.
    """

    def __init__(self, source: Source, *, stall_after: int) -> None:
        super().__init__(source)
        self.stall_after = stall_after
        self.stalled = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = asyncio.Event()
        self.error: Exception | None = None

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncGenerator[SourceWatchState]:
        try:
            yielded = 0
            async for state in super()._walk_states(since, start_index):
                if yielded == self.stall_after:
                    await self._stall()
                yield state
                yielded += 1
        finally:
            self.closed.set()

    async def _stall(self) -> None:
        self.stalled.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.error is not None:
            raise self.error


async def test_a_stalled_page_moves_the_heartbeat_and_awaits_the_beat_before_it_arrives() -> None:
    """A page riding out its retries can outlast `STALE_AFTER`, so the walk beats on a deadline.

    Each beat carries the committed position, never the state held for the next batch.
    Every beat sees one more commit and a later heartbeat than the beat before it, the
    batch's own beat included, which puts each beat after its own save and commit.
    """
    fixture = _Fixture(
        batch_size=2, heartbeat_seconds=0.01, clock=_Clock(NOW, step=timedelta(seconds=1))
    )
    adapter = fixture.adapter = _StallingSourceAdapter(fixture.source, stall_after=3)
    await fixture.given_completed_walk()
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")
    beats: list[tuple[int, SyncRun, bool]] = []

    async def beat() -> None:
        beats.append((fixture.commits, fixture.saved[-1], adapter.stalled.is_set()))

    walk = asyncio.create_task(
        fixture.service.sync(fixture.source, adapter, user_id=fixture.user_id, beat=beat)
    )
    try:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5):
                while sum(stalled for *_, stalled in beats) < 3:
                    await asyncio.sleep(0.01)
        assert adapter.stalled.is_set(), "the premise: the page stalled"
        assert fixture.positions == [2], "the premise: one batch committed, the third state held"
        stalled_beats = sum(stalled for *_, stalled in beats)
        assert stalled_beats >= 3, "fewer than three beats while the page stalled"
        commits = [count for count, *_ in beats]
        heartbeats = [saved.heartbeat_at for _, saved, _ in beats if saved.heartbeat_at is not None]
        assert commits == sorted(set(commits)), "a beat with no commit of its own before it"
        assert len(heartbeats) == len(beats)
        assert heartbeats == sorted(set(heartbeats)), "a beat with no save of its own before it"
        # A slow process can take a beat before the first batch commits, at position 0.
        carried = [(saved.items_seen, saved.position) for _, saved, _ in beats]
        assert set(carried) <= {(0, 0), (2, 2)}, "a beat carried the state held for a batch"
        assert carried[-1] == (2, 2), "a beat while stalled lost the committed position"
    finally:
        adapter.release.set()
        run = await walk
    assert (run.status, run.items_seen, run.items_matched) == (SyncRunStatus.COMPLETED, 5, 5)
    assert fixture.positions == [2, 4, 5]


async def test_a_page_that_fails_after_a_stall_fails_the_run_at_its_committed_position() -> None:
    """The beats during the stall committed nothing of the state held for the next batch."""
    fixture = _Fixture(batch_size=2, heartbeat_seconds=0.01)
    adapter = fixture.adapter = _StallingSourceAdapter(fixture.source, stall_after=3)
    await fixture.given_completed_walk()
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")
    beats: list[int] = []

    async def beat() -> None:
        beats.append(fixture.commits)

    walk = asyncio.create_task(
        fixture.service.sync(fixture.source, adapter, user_id=fixture.user_id, beat=beat)
    )
    try:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5):
                while len(beats) < 2:
                    await asyncio.sleep(0.01)
        assert len(beats) >= 2, "the premise: a beat while the page stalled"
    finally:
        adapter.error = PortUnavailable("the page's retries ran out")
        adapter.release.set()
        run = await walk
    assert (run.status, run.error) == (SyncRunStatus.FAILED, "the page's retries ran out")
    assert (run.position, run.items_seen) == (2, 2)
    assert fixture.positions == [2]
    stored = await fixture.runs.get(run.id)
    assert stored is not None and (stored.position, stored.items_seen) == (2, 2)


async def test_a_walk_that_fails_on_its_own_side_cancels_a_stalled_reader() -> None:
    """However the walk ends, its reader is cancelled and awaited: here a beat's commit raised."""
    fixture = _Fixture(heartbeat_seconds=0.01)
    adapter = fixture.adapter = _StallingSourceAdapter(fixture.source, stall_after=0)
    await fixture.given_completed_walk()
    await fixture.given_matched("movie-0")
    commit = fixture.service._commit

    async def _commit_until_the_stall() -> None:
        if adapter.stalled.is_set():
            raise ConnectionError("the database went away")
        await commit()

    fixture.service._commit = _commit_until_the_stall
    with pytest.raises(ConnectionError, match="the database went away"):
        async with asyncio.timeout(5):
            await fixture.service.sync(fixture.source, adapter, user_id=fixture.user_id)
    assert fixture.commits == 1, "the premise: the run's start committed, and the beat did not"
    assert adapter.cancelled.is_set(), "the walk ended with its reader still stalled"
    assert asyncio.all_tasks() - {asyncio.current_task()} == set(), "a task outlived the walk"


async def test_a_walk_that_fails_on_its_own_side_closes_the_listing_its_reader_holds() -> None:
    """The reader waits on a full queue, its listing parked at a state, and is closed too.

    Closed before the walk's error reaches the caller, not once the listing is collected.
    """
    fixture = _Fixture(batch_size=2)
    adapter = fixture.adapter = _StallingSourceAdapter(fixture.source, stall_after=5)
    await fixture.given_completed_walk()
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")
    commit = fixture.service._commit

    async def _commit_only_the_start() -> None:
        if fixture.commits:
            raise ConnectionError("the database went away")
        await commit()

    fixture.service._commit = _commit_only_the_start
    with pytest.raises(ConnectionError, match="the database went away"):
        async with asyncio.timeout(5):
            await fixture.service.sync(fixture.source, adapter, user_id=fixture.user_id)
    assert fixture.commits == 1, "the premise: the run's start committed, and its batch did not"
    assert not adapter.stalled.is_set(), "the premise: the listing never stalled"
    assert adapter.closed.is_set(), "the walk ended with its listing still open"
    assert asyncio.all_tasks() - {asyncio.current_task()} == set(), "a task outlived the walk"


class _CloseRaisingSourceAdapter(_StallingSourceAdapter):
    """A stalling source whose walk raises as it is closed, however it ends."""

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncGenerator[SourceWatchState]:
        try:
            async for state in super()._walk_states(since, start_index):
                yield state
        finally:
            raise RuntimeError("the listing's close failed")


async def test_a_walk_that_fails_on_its_own_side_ends_though_its_listings_close_raises() -> None:
    """The stalled reader, cancelled, stays cancelled when its listing's close raises.

    The batch's commit waits until the listing stalls with two states queued behind it,
    then raises. A reader that put the close's error on that full queue would wait on a
    queue nothing reads, and the walk, awaiting the reader, would never end.
    """
    fixture = _Fixture(batch_size=2)
    adapter = fixture.adapter = _CloseRaisingSourceAdapter(fixture.source, stall_after=4)
    await fixture.given_completed_walk()
    for index in range(6):
        await fixture.given_matched(f"movie-{index}")
    commit = fixture.service._commit

    async def _commit_failing_once_the_listing_stalls() -> None:
        if fixture.commits:
            await adapter.stalled.wait()
            raise ConnectionError("the database went away")
        await commit()

    fixture.service._commit = _commit_failing_once_the_listing_stalls
    with pytest.raises(ConnectionError, match="the database went away"):
        async with asyncio.timeout(5):
            await fixture.service.sync(fixture.source, adapter, user_id=fixture.user_id)
    assert fixture.commits == 1, "the premise: the run's start committed, and its batch did not"
    assert adapter.cancelled.is_set(), "the premise: the reader was cancelled at its stall"
    assert adapter.closed.is_set(), "the premise: the listing was closed"
    assert asyncio.all_tasks() - {asyncio.current_task()} == set(), "a task outlived the walk"


async def test_a_cancelled_watch_walk_closes_its_run_and_stays_cancelled() -> None:
    fixture = _Fixture()
    adapter = fixture.adapter = _StallingSourceAdapter(fixture.source, stall_after=0)
    await fixture.given_completed_walk()
    await fixture.given_matched("movie-0")
    task = asyncio.create_task(
        fixture.service.sync(fixture.source, adapter, user_id=fixture.user_id)
    )
    async with asyncio.timeout(5):
        await adapter.stalled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task
    stored = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.WATCH_STATE)
    assert stored is not None
    assert (stored.status, stored.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)
    assert fixture.rollbacks == 1
