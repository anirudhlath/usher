"""Inbound watch state, against port fakes and a source adapter that lies
the way Emby's listing route does.

**Every case here that matters is about a number the walk does not know.**
`_LossySourceAdapter` is the measured behaviour of Emby 4.9.5.0 reduced to
six lines: its walk reports `play_count=None`/`last_played_at=None` and its
single-item route reports the truth. `FakeSourceAdapter` on its own cannot
model that -- it hands back whatever the test seeded, so a service that
wrote `state.play_count or 0` would look correct against it for the walk
*and* the backfill.

**Two properties here can only be checked against real Postgres**, and
`tests/integration/test_services_watch_sync.py` is where:

- the merge preserving a stored count across a *batch* -- the fake's
  `value if value is not None else stored` is naturally `COALESCE`-shaped
  and cannot fail, and the natural SQL spelling of the same thing reads
  back `0`; and
- `backfill_one`'s `observed_at`. `FakeWatchStateRepository` stores
  `observed_at` as `updated_at`, while Postgres has a `BEFORE UPDATE`
  trigger that overwrites it with the *write* instant -- so a backfill
  carrying anything but a fresh instant is accepted here and silently
  refused there by the conflict rule, and the play count never lands.
"""

import dataclasses
import inspect
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
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
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
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
    asymmetry ADR-0014 exists for.
    """

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        async for state in super()._walk_states(since, start_index):
            yield dataclasses.replace(state, play_count=None, last_played_at=None)


class _Fixture:
    def __init__(self, *, batch_size: int = 1_000, lossy: bool = True) -> None:
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
        self.positions: list[int] = []
        self.saved: list[SyncRun] = []

        saved = self.runs.save

        async def _record(run: SyncRun) -> None:
            # **`positions` is the per-batch checkpoints, and "per batch" is
            # spelled as "this save carried states" rather than as "the
            # status is RUNNING".** `sync`'s reclaim save is `RUNNING` too,
            # so the status test would report the position an attempt
            # *started* from as though a batch had committed it -- reading
            # `[3, 5, 6]` where two batches committed. Every `_flush` save
            # adds at least one to `items_seen`; nothing else in this
            # service does. The closing save carries the terminal status and
            # no new states, so it is out either way.
            previous = await self.runs.get(run.id)
            if previous is not None and run.items_seen > previous.items_seen:
                self.positions.append(run.position)
            self.saved.append(run)
            await saved(run)

        self.runs.save = _record  # type: ignore[method-assign]
        self.service = WatchStateSyncService(
            media_items=self.media_items,
            watch_states=self.watch_states,
            runs=self.runs,
            queue=self.queue,
            commit=self._commit,
            batch_size=batch_size,
        )
        self.titles: dict[str, uuid.UUID] = {}
        self.episodes: dict[str, uuid.UUID] = {}

    async def _commit(self) -> None:
        self.commits += 1

    async def given_matched(
        self, external_id: str, *, episode: bool = False, changed_at: AwareDatetime = T0
    ) -> uuid.UUID:
        """A source item that is stored and matched -- the state every watch
        record needs before it has anywhere to land."""
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

    async def given_history(self, external_id: str, play_count: int) -> None:
        """A stored play count, as a backfill or an earlier authoritative
        read would have left it. Merged at an instant *before* every walk in
        these tests, so the conflict rule never accounts for a preserved
        count on its own."""
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
    """Batch size 2, so a five-state walk commits three times and the
    trailing partial batch is one of them."""
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
    """The end-to-end form of ADR-0014, through the service rather than the
    repository. The stored 7 was recovered by an authoritative read; the walk
    that follows says `PlayCount: 0` on the wire and `None` in the port, and
    the household's history has to survive it -- every night, forever."""
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
    """The same property one batch wide, which is how it actually arrives:
    5,000 states in one `merge_from_source`, all of them carrying an absent
    count, over rows holding different real ones. A service that collapsed
    `None` to `0` anywhere in the batch path erases all of them at once."""
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
    """The over-correction the `COALESCE` is not: "never write a count from
    a merge" makes un-marking something played impossible to propagate,
    which is the same correctness bug as filtering zero states out of a walk.
    A source that *can* count and says zero is reporting a reset."""
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
    """An episode's `MediaItem` carries its series' `title_id` *and* its
    `episode_id`, because a client browsing a season wants both. A watch
    state may carry exactly one (`num_nonnulls(title_id, episode_id) = 1`),
    so the service has to collapse the pair -- and handing both through
    raises `PortDataMalformed`, which aborts a batch of five thousand states
    over 89% of this library. Passing the *title* instead is quieter and
    worse: every episode of a show merges onto one row."""
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
    """`_watch_target`'s third branch, tested directly because nothing that
    honours `MediaItemRepository`'s contract can reach it: `resolve_targets`
    omits an unmatched item rather than answering with a pair of `None`s, so
    the service's own filter is the `targets.get(...)` miss above and this
    branch is the belt to that pair of braces.

    Kept rather than deleted, and pinned rather than trusted: a repository
    that answered with an empty pair would otherwise hand
    `merge_from_source` a merge naming neither target, and one of those
    aborts a batch of five thousand states.
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
    """A `WatchStateMerge` with neither a title nor an episode raises
    `PortDataMalformed` by contract, and an unmatched `MediaItem` produces
    exactly that -- which would abort a batch of 5,000 states over one item
    sitting in the review queue. The service filters them and counts them
    instead."""
    fixture.adapter.seed(_item("orphan-1"), T0)
    fixture.adapter.seed_state(
        SourceWatchState(external_id="orphan-1", position_seconds=90, played=False)
    )
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.items_unmatched == 1
    assert run.items_matched == 0
    assert run.status is SyncRunStatus.COMPLETED


async def test_a_walk_longer_than_one_batch_merges_every_state(fixture: _Fixture) -> None:
    """The trailing partial batch. Seven states at a batch size of two is
    three full batches and one of one, and a walk that flushed only on the
    size threshold silently drops the last page of nearly every run -- here
    that is a resume position a household would notice."""
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
    """1,126,674 items. One `resolve_targets` per batch is the difference
    between a walk that finishes and one that does not, and it is invisible
    in every assertion about stored values."""
    for index in range(50):
        await fixture.given_matched(f"movie-{index}")
        fixture.adapter.seed_state(
            SourceWatchState(external_id=f"movie-{index}", position_seconds=1, played=False)
        )
    fixture.media_items.reset_calls()
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert fixture.media_items.calls == 1, fixture.media_items.calls


async def test_every_merge_carries_the_runs_own_start_instant(fixture: _Fixture) -> None:
    """`observed_at = run.started_at`, not `now()`, and the difference is
    PRD 03's conflict rule rather than tidiness. A walk of 1,126,674 items
    takes hours; a client that sets a resume position while it is running
    knows more than the walk does, and `observed_at` is the only thing that
    says so. A per-batch `now()` creeps forward as the walk goes and
    silently starts winning those races -- and every stored value still
    looks plausible afterwards."""
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
    assert seen == [run.started_at] * 5


async def test_a_walk_never_retracts_a_watch_state(fixture: _Fixture) -> None:
    """PRD 08 lists watch state as the precious set that survives
    everything, so there is no sweep here and there is no lane that could
    grow one by accident. `ReconcileService`'s availability sweep is the
    shape this must never acquire."""
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
    """The push lane's entry point. The same chain as a walk's, minus the
    run bookkeeping -- because a push event is not a run, and inventing a
    `sync_runs` row per `UserDataChanged` would put a row per few seconds of
    playback into a table an operator reads."""
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
    """**The pairing, and the reason it is reported rather than recovered.**
    The M5 plan's own self-review found a caller zipping `merged` against
    the batch it handed in; `merged` is the *matched subset*, so one
    unmatched item at the front shifts every pair by one and the push lane
    publishes item A's resume position under item B's title.

    An unmatched item first, then two matched ones, is the smallest batch
    that shows it -- with the unmatched item last, a positional zip agrees
    with the truth and ratifies the bug."""
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
    """The commit is the caller's, and the two callers disagree about what
    a unit of work is: a walk commits per batch of a thousand, the push lane
    per event. A commit in here would make the second impossible to state."""
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
    """PRD 02: unmatched items are never dropped, and there will always be
    some. `merge_from_source` answers a target-less merge with
    `PortDataMalformed`, which on the push lane would take the channel down
    and cost a reconnect plus a gap-closing delta walk."""
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
    """ADR-0014's chain, reached from the push lane exactly as it is from a
    walk. A `UserDataChanged` entry carries no trustworthy `PlayCount`, so
    every played item pushed produces one of these."""
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
    `merge_from_source` returns rows *changed*, and the two differ whenever
    PRD 03's "latest `updated_at` wins" refuses one -- a client set a resume
    position thirty seconds ago and a walk that started an hour ago must not
    stomp it. Returning the repository's count in `items_matched`'s place
    silently changes what every stored `sync_runs` row means and what
    PRD 10's dashboard plots."""
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
    """The refactor's own regression test. `_flush` used to compute
    `items_matched` as `len(batch) - unmatched` inline; if the split starts
    reporting rows-written instead, every existing `sync_runs` row means
    something different -- and the two agree on every batch where nothing is
    refused, which is nearly all of them."""
    await fixture.given_matched("movie-1")
    fixture.adapter.seed(_item("orphan-1"), T0)
    fixture.watch_states.refuse_next_merge()
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert (run.items_seen, run.items_matched, run.items_unmatched) == (2, 1, 1)


# -- the enqueue, which is what bounds the backfill --------------------------


async def test_a_played_item_with_unknown_history_is_enqueued_for_backfill(
    fixture: _Fixture,
) -> None:
    """The bounded recovery. Enqueued rather than fetched inline: one request
    per item against a library of 1,126,674 is not a walk, it is a week."""
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=True, play_count=7)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    queued = await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10)
    assert [job.key for job in queued] == ["movie-1"]
    assert queued[0].priority == JobPriority.BACKFILL


async def test_an_unplayed_item_is_not_enqueued_for_backfill(fixture: _Fixture) -> None:
    """1,126,674 items, of which the household has played a few thousand. An
    enqueue predicate that ignored `played` would queue the library."""
    await fixture.given_matched("movie-1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=False)
    )
    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10) == []


async def test_a_played_item_whose_count_the_source_reported_is_not_enqueued(
    fixture: _Fixture,
) -> None:
    """The other half of the predicate, and the half `played` alone does not
    cover. A source whose walk *can* count (the contract permits it, and
    Jellyfin's listing may well) needs no backfill at all -- enqueueing one
    per played item anyway is a standing queue of thousands of requests that
    can only ever confirm what is already stored."""
    honest = FakeSourceAdapter(fixture.source)
    await fixture.given_matched("movie-1")
    honest.seed(_item("movie-1"), T0)
    honest.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=True, play_count=4)
    )
    await fixture.service.sync(fixture.source, honest, user_id=fixture.user_id)
    assert await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10) == []


async def test_an_unmatched_played_item_is_not_enqueued_for_backfill(fixture: _Fixture) -> None:
    """There is no row to backfill into. A job for one parks or no-ops
    forever, and the review queue is the lane that fixes it."""
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
    """`observed_at` is the instant the backfill read the source, never the
    run's. PRD 03's "latest `updated_at` wins" applies to the whole record,
    so a backfill carrying an instant at or before the row's stored
    `updated_at` writes nothing -- and against Postgres, where a `BEFORE
    UPDATE` trigger stamps the *write* instant, "at or before" is every
    instant the walk could hand it. The backfill would then never converge
    and nothing in this file would say so; the paired integration case is
    what actually closes it."""
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
    """`get_watch_state` returns `None` for an item the source no longer
    has, exactly as `get_item` does. A backfill job for one must complete
    rather than park: the item's disappearance is what the reconcile lane
    handles, and parking here would fill the poison list with items that
    were simply deleted."""
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
    """A queued job outliving the media item it names -- an operator removed
    the source, or the review queue re-resolved it. Quiet, because the
    alternative is a parked job an operator has to dismiss by hand."""
    assert (
        await fixture.service.backfill_one(
            fixture.source, fixture.adapter, external_id="never-existed", user_id=fixture.user_id
        )
        is False
    )


async def test_the_backfill_asks_the_source_only_about_items_it_can_store(
    fixture: _Fixture,
) -> None:
    """The cheap check before the expensive one. PRD 01 measures a
    single-item request at 1-5 s; resolving the target first is one indexed
    read, and an unmatched item has nowhere for the answer to land."""
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
    """**The convergence claim, run rather than argued.** Seven played items
    whose history the walk could not report, drained three at a time: the
    population has to empty, and it has to empty in the number of passes the
    arithmetic predicts rather than eventually.

    The loop is bounded so a non-converging predicate fails the case instead
    of hanging the suite -- which is exactly what a backfill that wrote
    nothing (a stale `observed_at`), or one that never moved a row out of
    `played AND play_count = 0`, would do in production."""
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
    """The honest other half of the convergence claim. Emby's own `POST
    .../PlayedItems/{item}` never leaves a played item at `PlayCount: 0`, so
    the predicate empties -- but that is a property of the *source*, not of
    this code, and a source that cannot say leaves rows matching forever.

    What this file can guarantee is that such a row costs one request per
    pass and does not starve the others: `list_needing_history` is
    oldest-first and a merge moves the row's `updated_at`, so the third pass
    is looking at different rows from the first."""

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
    """`list_needing_history` reports the owner of every row it returns, and
    a backfill that wrote them all to one user would move a second
    household member's history onto the first -- silently, and only once
    there were two of them.

    Two rows, two users, deliberately: with one row in the sweep every
    reading of "whose row is this" agrees, and a backfill that took the
    first row's owner for all of them passes. The second row is what makes
    the property observable at all.
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
    """989,827 episodes: the backfill's reverse lookup is dominated by this
    case, not by movies. A title-keyed reverse lookup would answer an
    episode's row with its series' `external_id` and backfill 24 episodes
    from one number."""
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
    """The same trap `ReconcileService` documents, one lane over: `SyncRun`
    is frozen and the per-batch checkpoint is an evolved copy, so a failure
    handler that evolves its own pre-walk binding writes `items_seen = 0`
    over a checkpoint that recorded four."""
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
    """`usher sync` across three sources needs the second and third to run
    when the first is unreachable."""
    fixture.adapter.go_offline()
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert run.status is SyncRunStatus.FAILED
    assert run.items_seen == 0


async def test_sync_never_raises_a_port_error(fixture: _Fixture) -> None:
    """Every `UsherPortError` subclass, not just the one the offline case
    happens to produce."""

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
    """A `ZeroDivisionError` is a bug in this process. Recording it as a
    failed *sync* hides it behind an operational-looking row."""

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
    """The watch-state lane owns its own cursor: it walks a different method
    under a different upstream filter (`MinDateLastSavedForUser`, measured as
    genuinely different from `MinDateLastSaved` -- 29,005 vs 28,934 items over
    the same window), so resuming from an item-lane run would skip whatever
    changed in between."""
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
    """Resuming from a run that failed halfway skips everything it never
    reached, silently."""
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
    """ADR-0009 and PRD 01's layering rule, at module level. `import-linter`
    already forbids `usher.services -> usher.db`; this catches the other
    half, which no contract expresses."""
    import usher.services.watch_sync as module

    source = (module.__file__ or "").replace(".pyc", ".py")
    text = open(source).read()  # noqa: SIM115
    for forbidden in ("httpx", "sqlalchemy", "asyncpg", "usher.db"):
        assert f"import {forbidden}" not in text
        assert f"from {forbidden}" not in text


async def test_the_walk_is_the_only_thing_that_needs_a_source_user_map(
    fixture: _Fixture,
) -> None:
    """`SourceWatchState.source_user_id` is carried and deliberately not
    consulted: M4 has one user (PRD 01's authentication seam) and mapping a
    source's user ids onto Usher's is M5's problem. What must not happen
    quietly is a *second* source user's state landing on the singleton, so
    the value being ignored is recorded here rather than discovered later."""
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
    """**Deliberate, and it is a scale decision rather than an omission.**

    A walk merges up to 1,126,789 states; one `watchstate.updated` per merged
    row is a fan-out per row per night to every connected client, and every
    one of them is the source echoing back state that has not changed since
    the last walk. The push lane publishes because a push event *is* a
    change.

    The reachable version of this defect is the *shared chain*:
    `apply_states` has exactly two callers -- this walk and
    `PushApplyService` -- and moving the publish down into it is the obvious
    de-duplication. So this drives the walk with the very publisher the push
    lane uses. Verified by doing it: a publish added to `apply_states` fails
    this case on its first assertion.

    The second half is not decoration. Without it the case passes against a
    harness that could not observe a publish at all, which is the shape a
    "publishes nothing" assertion fails silently in.
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
    # M7's `row.invalidated` rides the same publish, and the whole sequence is
    # asserted rather than filtered: this case's entire point is *which lane
    # publishes what*, so a filtered assertion here would stop seeing the lane
    # that grew a fan-out.
    assert [event.kind for event in events.published] == [
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.WATCHSTATE_UPDATED,
    ]


def test_the_walk_has_no_event_publisher_to_publish_through() -> None:
    """The tripwire on the first step of the change above. There is no
    publisher on this service at all, so a reader who wanted a
    `watchstate.updated` per merged row has to add one here before anything
    else -- and this is the line that says why not to."""
    assert "events" not in inspect.signature(WatchStateSyncService.__init__).parameters


def _no_ingest() -> IngestService:
    """`PushApplyService` needs one and this case never reaches an item
    event; built rather than stubbed so the service is the real one."""
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


# -- the resume, which is what makes the first full walk completable --------


async def test_a_failed_walk_is_resumed_from_the_position_it_committed(
    fixture_batched: _Fixture,
) -> None:
    """**Issue #41.** A crashed walk left no completed run, so the next one
    had no cursor and re-walked the whole library -- for ~5,688 pages, which
    is where the next transient failure came from. It resumes instead.

    Batched at 2 deliberately: at the default 1,000 a six-item walk that
    fails part-way has committed *nothing*, so the position it resumes from
    would be 0 and the case would pass against a service that never
    checkpoints at all.
    """
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

    assert second.id == first.id, "the run row is reclaimed, not duplicated"
    assert second.status is SyncRunStatus.COMPLETED
    assert second.started_at == first.started_at, (
        "the reclaimed row keeps its original start instant, so the next delta's "
        "`since` covers everything saved since the logical walk began"
    )
    assert fixture_batched.adapter.resumed_from == [0, 2], (
        "the second attempt asked page one again instead of resuming"
    )


async def test_a_walk_whose_newest_run_completed_starts_fresh(fixture: _Fixture) -> None:
    """The other half: a completed walk is followed by a delta from its
    `started_at`, at position zero, not by a resume."""
    await fixture.given_matched("movie-0")
    done = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert done.status is SyncRunStatus.COMPLETED

    again = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert again.id != done.id, "a completed run is not reclaimed"
    assert again.position == 0
    assert again.cursor_at == done.started_at
    assert fixture.adapter.resumed_from == [0, 0]


async def test_the_position_advances_per_committed_batch(fixture_batched: _Fixture) -> None:
    """`position` is committed progress, never the batch in flight: a crash
    re-walks exactly the uncommitted batch, which the merge's idempotent
    upsert makes free."""
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
    """`_Progress`' reason, extended to the resume point: a failure handler
    holding the pre-walk run would write `position = 0` over a checkpoint
    that recorded two, and the next attempt would restart from the top --
    which is the #41 loop with extra steps.
    """
    for index in range(6):
        await fixture_batched.given_matched(f"movie-{index}")
    fixture_batched.adapter.fail_after(3)

    run = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert run.status is SyncRunStatus.FAILED
    assert run.position == 2
    stored = await fixture_batched.runs.get(run.id)
    assert stored is not None and stored.position == 2, (
        "the durable checkpoint regressed, so the next attempt restarts from the top"
    )


class _DuplicatingSourceAdapter(_LossySourceAdapter):
    """A source whose walk yields every record twice.

    The port permits it -- `SourceAdapter.watch_state` promises no ordering
    and no uniqueness, and ADR-0042 declines to defend against a divergence
    no source it has measured produces.

    **What it buys the case below is not the divergence, and saying so is
    the correction (2026-08-25).** A duplicated yield moves `items_seen` and
    `position` by exactly one step each -- `_walk` seeds its counter at the
    resume point and `_flush` advances both from the same batch -- so on a
    fresh run the two stay equal however many times a source repeats itself,
    and this adapter cannot separate them on its own. The gap of five below
    is the *reclaimed row's*, seeded to match what `m10b` backfills onto a
    row #41 left `RUNNING`.

    What the duplication does buy is the **unit**: with six yields over
    three items, `position == 6` says the checkpoint is an offset into the
    stream this walk yielded rather than a count of distinct items, which is
    the reading `start_index` is honoured under one layer down and is
    unobservable on any adapter that yields each record once.

    **`start_index` counts yields here, which is what the port says and what
    a resumed walk checkpoints.** The base walk is therefore asked for the
    whole stream and the skip is applied to the *doubled* one: skipping first
    would make this adapter's offset a raw-item offset, i.e. a resume point
    it cannot honour, which is the same mistake `FakeSourceAdapter._walk_states`
    carries a comment against one layer down.
    """

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        yielded = 0
        async for state in super()._walk_states(since, 0):
            for _ in range(2):
                if yielded >= start_index:
                    yield state
                yielded += 1


async def test_the_resume_point_is_the_position_and_not_the_counter(
    fixture_batched: _Fixture,
) -> None:
    """**`position` and `items_seen` are two statements, and the migration
    that added the first is what makes a row carrying both reachable.**

    `m10b` backfills `position = 0` onto every row that predates it --
    including the three `RUNNING` rows aged 7-11 h that issue #41 observed,
    each of them carrying a real six-figure `items_seen`. So the very first
    walk after this lands reclaims a row whose counter says five and whose
    checkpoint says zero, and it has to believe the checkpoint: `items_seen`
    is a running total of states *yielded* over the life of the run
    (`items_matched` is the merged half), and nothing promises a total
    accumulated across attempts is a page offset into this one. A `_flush`
    spelling `position = items_seen + len(batch)` reads identically on every
    fresh walk in this file and sends the next attempt eight pages past
    anything this one reached.

    The duplicate is the same distinction arriving from the source instead
    of from the migration: six yields over three items, counted six times,
    over a page position that a count of yields is only accidentally equal
    to. Re-walking them costs nothing -- every write on this lane is an
    idempotent upsert and it retracts nothing -- which is exactly why the
    cheap number is the wrong one to resume from.

    **What this case does *not* cover, measured rather than assumed
    (2026-08-25).** It reads as an argument about `_walk`'s `seen =
    start_index`, and it is blind to that spelling: the row it seeds carries
    `position = 0`, so `seen = 0` and `seen = start_index` are the same
    statement here and the `seen = 0` plant passes this case untouched. What
    it holds is the pair either side of that -- the resume point is read off
    `position` and not off `items_seen`, and `_flush` checkpoints the page
    it was handed rather than one derived from the counter. The counter's
    *origin* is covered by
    `test_each_failed_attempt_resumes_further_in_than_the_last`, which needs
    a third attempt to see it at all.
    """
    dupes = _DuplicatingSourceAdapter(fixture_batched.source)
    for index in range(3):
        await fixture_batched.given_matched(f"movie-{index}")
        dupes.seed(_item(f"movie-{index}"), T0)
    await fixture_batched.runs.add(
        SyncRun(
            source_id=fixture_batched.source.id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.RUNNING,
            position=0,
            items_seen=5,
        )
    )

    run = await fixture_batched.service.sync(
        fixture_batched.source, dupes, user_id=fixture_batched.user_id
    )

    assert dupes.resumed_from == [0], "the walk resumed from the counter, not the checkpoint"
    assert run.position == 6, "six yields is six pages of this walk"
    assert run.items_seen == 11, "the five it inherited, plus the six it yielded"
    assert run.items_seen > run.position, (
        "the counter and the checkpoint have collapsed into one number"
    )


async def test_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned(
    fixture_batched: _Fixture,
) -> None:
    """**`RUNNING` is the designed trace of a hard kill, not an anomaly.**

    `sync` commits `RUNNING` before it walks precisely so a process that
    dies mid-walk leaves a row behind rather than nothing -- and issue #41's
    deployment has three of them, aged 7-11 h, which is what a worker
    killed during an eleven-hour walk looks like. A resume that recognised
    only `FAILED` would mint a fresh run beside each one and start at page
    one: #41 again, with the stuck rows still on the operator's dashboard
    and nothing to say they were ever superseded.

    `latest_incomplete_run` is the read that makes the distinction
    unnecessary -- newest, then "not completed" -- and this is the case that
    holds it to that, because "only a `FAILED` run resumes" survives its
    entire contract suite.

    **The reclaim also has to clear the last attempt's verdict**, which is
    why the row seeded below carries one. `usher sync-status` renders
    `error=...` for any truthy value whatever status sits beside it, so a
    `RUNNING` row still holding "source went away mid-walk" reports a fault
    as happening *now* -- on the one command an operator runs to diagnose
    this lane, about the walk that is currently repairing it. A
    `finished_at` on a running row is the same lie about the other end of
    the interval, and it is what PRD 10's duration panel subtracts.
    """
    for index in range(6):
        await fixture_batched.given_matched(f"movie-{index}")
    abandoned = SyncRun(
        source_id=fixture_batched.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.RUNNING,
        position=3,
        items_seen=3,
        items_matched=3,
        error="source went away mid-walk",
        finished_at=T0,
    )
    await fixture_batched.runs.add(abandoned)

    run = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert run.id == abandoned.id, "a fresh run was minted beside the abandoned one"
    assert run.started_at == abandoned.started_at, (
        "the reclaimed row lost the instant the logical walk began"
    )
    assert run.status is SyncRunStatus.COMPLETED
    assert fixture_batched.adapter.resumed_from == [3], "the abandoned row's position was ignored"
    assert run.items_seen == 6, "the three it inherited, plus the three still to walk"
    assert len(await fixture_batched.runs.list_for_source(fixture_batched.source.id)) == 1, (
        "the stuck row is still there and a second one is beside it"
    )
    # Asserted on the *reclaim* save rather than on the returned run, which
    # is `COMPLETED` and carries a legitimate `finished_at`: the window this
    # is about is the hours the row spends `RUNNING`, which is the whole time
    # an operator is watching it.
    reclaimed = fixture_batched.saved[0]
    assert reclaimed.status is SyncRunStatus.RUNNING, (
        "the premise: the first save is the reclaim, before any batch"
    )
    assert reclaimed.error is None, "a running walk reports the last attempt's outage as its own"
    assert reclaimed.finished_at is None, "a running walk carries an instant it finished at"


async def test_each_failed_attempt_resumes_further_in_than_the_last(
    fixture_batched: _Fixture,
) -> None:
    """**Three attempts, because two cannot tell a walk that converges from
    one that is stuck.** Every other case in this file either completes or
    fails exactly once, and a walk that resumes at the right page *once* and
    then never advances again satisfies all of them: its `items_seen` still
    climbs attempt after attempt, so every counter an operator watches reads
    healthy while the checkpoint sits on the same page forever. That is
    #41's symptom exactly, with a `position` column added.

    Measured against the shape that produces it -- a `_walk` counting only
    the states *this attempt* yielded rather than starting the count at the
    page it resumed from (`seen = 0` in place of `seen = start_index`) --
    which walks `[0, 2, 4]` on the shipped code and `[0, 2, 2]` under the
    defect, and passes everything else in this file.

    **"Everything else" includes the case whose name reads as if it owned
    this defect**, and that is worth naming rather than leaving to be
    rediscovered: `test_the_resume_point_is_the_position_and_not_the_counter`
    seeds `position = 0`, where the two spellings are one statement, so it
    cannot distinguish them. This is the only case in the file that can, and
    the mechanism is why three attempts are needed: the mutant's *second*
    attempt saves the page it started from, `GREATEST` correctly refuses to
    pull the stored checkpoint back, and the third therefore resumes exactly
    where the second did.
    """
    for index in range(8):
        await fixture_batched.given_matched(f"movie-{index}")

    reached = []
    for _ in range(2):
        fixture_batched.adapter.fail_after(3)
        attempt = await fixture_batched.service.sync(
            fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
        )
        assert attempt.status is SyncRunStatus.FAILED, "the premise: this attempt really did fail"
        reached.append(attempt.position)

    fixture_batched.adapter.clear_failure()
    done = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert reached == sorted(set(reached)), (
        f"the second failure did not get further than the first: {reached}"
    )
    assert done.position > reached[-1]
    assert fixture_batched.adapter.resumed_from == [0, 2, 4], (
        "an attempt asked to resume from a page an earlier one had already passed"
    )
    assert done.status is SyncRunStatus.COMPLETED
    assert done.items_seen == 8, "a state was walked twice, or never"


async def test_a_resumed_attempt_merges_at_its_own_start_not_the_reclaimed_runs(
    fixture_batched: _Fixture,
) -> None:
    """**The reclaimed `started_at` is the cursor's, and it must not become
    the merge's.** PRD 03 settles a conflict on "latest `updated_at` wins",
    and `watch_states` has a `BEFORE UPDATE` trigger that stamps the *write*
    instant -- so a row the push lane touched an hour ago reads back an
    `updated_at` of an hour ago, and a resumed walk merging under a run that
    began *days* ago loses to it. The walk that exists to repair those rows
    would write nothing to exactly the rows most recently in play, and the
    rows it *creates* would be stamped days in the past, which reorders
    `list_needing_history`'s oldest-first drain and leaves the taste
    watermark motionless.

    One instant per attempt rather than per batch, which is the property
    `test_every_merge_carries_the_runs_own_start_instant` states for a first
    attempt: a walk of 1.14M items takes hours, and a creeping `now()` starts
    winning races against clients who know more than it does.
    """
    seen: list[datetime] = []
    original = fixture_batched.watch_states.merge_from_source

    async def _record(merges: object) -> int:
        seen.extend(merge.observed_at for merge in merges)  # type: ignore[attr-defined]
        return await original(merges)  # type: ignore[arg-type]

    fixture_batched.watch_states.merge_from_source = _record  # type: ignore[method-assign]
    for index in range(3):
        await fixture_batched.given_matched(f"movie-{index}")
    abandoned = SyncRun(
        source_id=fixture_batched.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.RUNNING,
        started_at=T0,
    )
    await fixture_batched.runs.add(abandoned)

    run = await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    assert run.started_at == T0, (
        "the premise: the row still carries the instant the logical walk began, "
        "which is what the next delta's `since` will be"
    )
    assert len(seen) == 3, "the premise: three states really were merged"
    assert seen == [seen[0]] * 3, "the instant crept forward between batches"
    assert seen[0] > T0, "a resumed walk merged under an instant weeks in the past"


async def test_the_span_records_the_page_the_walk_resumed_from(
    fixture_batched: _Fixture, spans: InMemorySpanExporter
) -> None:
    """The one number that separates a converging resume from a stuck one,
    and the only place it is legible without reading `sync_runs`. Emitted
    since ADR-0042 and asserted here, because an attribute nothing reads is
    an attribute nothing notices the loss of."""
    for index in range(6):
        await fixture_batched.given_matched(f"movie-{index}")
    await fixture_batched.runs.add(
        SyncRun(
            source_id=fixture_batched.source.id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.RUNNING,
            position=4,
        )
    )

    await fixture_batched.service.sync(
        fixture_batched.source, fixture_batched.adapter, user_id=fixture_batched.user_id
    )

    walks = [one for one in spans.get_finished_spans() if one.name == "sync.watch_state"]
    assert walks, [one.name for one in spans.get_finished_spans()]
    assert walks[0].attributes is not None
    assert walks[0].attributes["usher.resumed_from"] == 4
