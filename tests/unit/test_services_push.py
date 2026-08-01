"""The push lane: applying one event, and supervising the channel.

**What the fakes here cannot say, named rather than implied.**
`FakeWatchStateRepository` stores `observed_at` as `updated_at`, while
Postgres has a `BEFORE UPDATE` trigger that owns that column -- so a push
merge carrying anything but a fresh instant is accepted here and silently
refused there, which is the one defect in `_apply_watch_state` no case below
can reach. `tests/integration/test_services_push.py` is the paired run.
"""

import uuid
from collections.abc import Iterator
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
from usher.domain.jobs import JobKind
from usher.domain.source import Source
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
from usher.services.push import PushApplyService, PushOutcome
from usher.services.watch_sync import WatchStateSyncService

T0 = datetime(2026, 7, 1, tzinfo=UTC)


def _item(external_id: str, **overrides: object) -> SourceItem:
    fields: dict[str, object] = {
        "external_id": external_id,
        "name": f"Movie {external_id}",
        "kind": SourceItemKind.MOVIE,
        "year": 2021,
    }
    fields.update(overrides)
    return SourceItem(**fields)  # type: ignore[arg-type]


class _RecordingAdapter(FakeSourceAdapter):
    """`FakeSourceAdapter` plus a record of what was *asked* of the source.

    The whole reason `SourceEvent` carries a payload is to not ask, and a
    lane that asked anyway would merge exactly the same state -- so every
    assertion about the saving is an assertion about these two lists rather
    than about a stored value.
    """

    def __init__(self, source: Source) -> None:
        super().__init__(source)
        self.watch_state_reads: list[str] = []
        self.item_reads: list[str] = []

    async def get_watch_state(self, external_id: str) -> SourceWatchState | None:
        self.watch_state_reads.append(external_id)
        return await super().get_watch_state(external_id)

    async def get_item(self, external_id: str) -> SourceItem | None:
        self.item_reads.append(external_id)
        return await super().get_item(external_id)


class _Fixture:
    def __init__(self, *, max_items_per_event: int = 50) -> None:
        self.source = Source(
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-1",
            device_id=str(new_id()),
        )
        self.adapter = _RecordingAdapter(self.source)
        self.user_id = new_id()
        self.media_items = FakeMediaItemRepository()
        self.watch_states = FakeWatchStateRepository()
        self.titles = FakeTitleRepository()
        self.matching = FakeTitleMatchRepository(self.titles)
        self.queue = FakeJobQueue()
        self.events = FakeEventPublisher()
        self.commits = 0
        self.ingest = IngestService(
            matcher=MatchService(titles=self.titles, matching=self.matching, queue=self.queue),
            matching=self.matching,
            media_items=self.media_items,
            episodes=FakeEpisodeRepository(),
            queue=self.queue,
        )
        self.watch = WatchStateSyncService(
            media_items=self.media_items,
            watch_states=self.watch_states,
            runs=FakeSyncRunRepository(),
            queue=self.queue,
            # The same counter the applier commits through, deliberately: a
            # commit that leaked back into `apply_states` would otherwise be
            # invisible to `test_applying_an_event_commits_once`.
            commit=self._commit,
        )
        self.applier = PushApplyService(
            self.ingest,
            self.watch,
            self.events,
            self._commit,
            max_items_per_event=max_items_per_event,
        )
        self.title_ids: dict[str, uuid.UUID] = {}

    async def _commit(self) -> None:
        self.commits += 1

    async def given_matched(self, external_id: str, *, changed_at: AwareDatetime = T0) -> uuid.UUID:
        title_id = new_id()
        self.adapter.seed(_item(external_id), changed_at)
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=self.source.id,
                    external_id=external_id,
                    title_id=title_id,
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
                    last_seen_at=T0,
                )
            ]
        )
        self.title_ids[external_id] = title_id
        return title_id

    async def apply(self, event: SourceEvent) -> PushOutcome:
        return await self.applier.apply(self.source, self.adapter, event, user_id=self.user_id)


@pytest.fixture
def fixture() -> _Fixture:
    return _Fixture()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


# -- watch state ------------------------------------------------------------


async def test_a_carried_state_is_merged_without_a_request(fixture: _Fixture) -> None:
    """The whole reason `SourceEvent` grew a payload. A household pausing a
    film emits one of these every few seconds; a lane that asked the source
    for each would spend a 1-5 s round trip per second of playback."""
    await fixture.given_matched("i1")
    outcome = await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(SourceWatchState(external_id="i1", position_seconds=61, played=False),),
        )
    )
    assert outcome.states_merged == 1
    assert fixture.adapter.watch_state_reads == []
    stored = await fixture.watch_states.get_for_title(fixture.user_id, fixture.title_ids["i1"])
    assert stored is not None and stored.position_seconds == 61


async def test_an_id_with_no_carried_state_is_fetched(fixture: _Fixture) -> None:
    """`watch_states` is a subset keyed by `external_id`, so a source whose
    message shape this adapter could not parse -- or a second source that
    sends ids only -- still works, at one authoritative request per id."""
    await fixture.given_matched("i2")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="i2", position_seconds=7, played=True, play_count=3)
    )
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=("i2",))
    )
    assert fixture.adapter.watch_state_reads == ["i2"]
    assert outcome.states_merged == 1


async def test_an_item_the_source_no_longer_has_is_skipped_not_raised(
    fixture: _Fixture,
) -> None:
    """`get_watch_state` answering `None` means the source deleted it, which
    is the reconcile lane's problem. Raising here would cost a reconnect and
    a gap-closing delta walk for an item that is simply gone."""
    await fixture.given_matched("gone")
    fixture.adapter.forget("gone")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=("gone",))
    )
    assert outcome.states_merged == 0
    assert outcome.deferred_to_delta is False


async def test_a_carried_state_keeps_its_absent_play_history(fixture: _Fixture) -> None:
    """ADR-0014 on the push path, which is where the plan says a third
    payload shape lives: a `UserDataChanged` entry is neither a listing nor
    an item route, nothing in this repository has parsed a real one, and a
    fabricated `0` overwrites a household's history permanently. Absent
    stays absent, and the `WATCH_HISTORY` backfill recovers it."""
    await fixture.given_matched("i1")
    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(
                SourceWatchState(
                    external_id="i1", position_seconds=0, played=True, play_count=None
                ),
            ),
        )
    )
    queued = await fixture.queue.claim([JobKind.WATCH_HISTORY], limit=10)
    assert [job.key for job in queued] == ["i1"]
    stored = await fixture.watch_states.get_for_title(fixture.user_id, fixture.title_ids["i1"])
    assert stored is not None and stored.play_count == 0


async def test_a_watch_event_publishes_only_rows_that_changed(fixture: _Fixture) -> None:
    """PRD 03's "latest `updated_at` wins" refuses a merge whose observation
    is older than what a client already wrote. Publishing anyway makes a
    detail screen re-render on every echo of a position it set itself."""
    await fixture.given_matched("i1")
    fixture.watch_states.refuse_next_merge()
    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(SourceWatchState(external_id="i1", position_seconds=3, played=False),),
        )
    )
    assert fixture.events.published == []


async def test_a_watch_event_that_changed_something_publishes_it(fixture: _Fixture) -> None:
    title_id = await fixture.given_matched("i1")
    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(SourceWatchState(external_id="i1", position_seconds=61, played=False),),
        )
    )
    assert [event.kind for event in fixture.events.published] == [
        ClientEventKind.WATCHSTATE_UPDATED
    ]
    assert fixture.events.published[0].title_id == title_id
    assert fixture.events.published[0].data["position_seconds"] == 61


async def test_an_unmatched_item_does_not_shift_what_the_others_publish(
    fixture: _Fixture,
) -> None:
    """**The mis-pairing the M5 plan's own self-review found.** Its draft
    zipped the merged targets against the batch it had handed in; the
    targets are the *matched subset*, so one unmatched item at the front
    shifts every pair by one and a client renders item A's resume position
    on item B's screen.

    PRD 02 guarantees there will always be unmatched items, so this is the
    ordinary case rather than an edge one. The positions are distinct so a
    swap is visible; equal positions would pass against the bug.
    """
    first = await fixture.given_matched("i1")
    second = await fixture.given_matched("i2")
    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("orphan", "i1", "i2"),
            watch_states=(
                SourceWatchState(external_id="orphan", position_seconds=11, played=False),
                SourceWatchState(external_id="i1", position_seconds=22, played=False),
                SourceWatchState(external_id="i2", position_seconds=33, played=False),
            ),
        )
    )
    assert [
        (event.title_id, event.data["position_seconds"]) for event in fixture.events.published
    ] == [(first, 22), (second, 33)]


async def test_a_large_watch_event_with_no_payload_defers_to_a_delta(fixture: _Fixture) -> None:
    """The same cap, on the path that actually costs a request per item. An
    adapter that cannot parse a message's payload still names the ids, and
    at 1,126,789 items a request per changed item on a lane budgeted at one
    connection per source is a design defect rather than a slow path."""
    many = tuple(f"item-{index}" for index in range(60))
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=many)
    )
    assert outcome.deferred_to_delta is True
    assert fixture.adapter.watch_state_reads == []


async def test_a_large_watch_event_that_carries_its_payload_is_not_deferred(
    fixture: _Fixture,
) -> None:
    """The cap counts what has to be *asked for*, not what arrived. A source
    that sent sixty states in one message costs one batched merge and no
    requests at all, and deferring it would trade that for a paged walk."""
    ids = tuple(f"i{index}" for index in range(60))
    for external_id in ids:
        await fixture.given_matched(external_id)
    outcome = await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=ids,
            watch_states=tuple(
                SourceWatchState(external_id=external_id, position_seconds=5, played=False)
                for external_id in ids
            ),
        )
    )
    assert outcome.deferred_to_delta is False
    assert outcome.states_merged == 60
    assert fixture.adapter.watch_state_reads == []


# -- items ------------------------------------------------------------------


async def test_an_added_item_is_fetched_and_ingested(fixture: _Fixture) -> None:
    fixture.adapter.seed(_item("movie-1", provider_ids={"tmdb": "90000550"}), T0)
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_ADDED, external_ids=("movie-1",))
    )
    assert outcome.items_ingested == 1
    assert fixture.adapter.item_reads == ["movie-1"]
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "movie-1")
    assert stored is not None
    assert [event.kind for event in fixture.events.published] == [ClientEventKind.TITLE_UPDATED]


async def test_a_large_event_defers_to_a_delta_instead_of_a_request_per_item(
    fixture: _Fixture,
) -> None:
    """Emby emits `LibraryChanged` during a library scan and it can name
    thousands. At 1,126,789 items a request per changed item on a lane
    budgeted at one connection per source is not slow, it is a design
    defect -- and a delta walk is one paged request per 200 items under
    `MinDateLastSaved`, which is the mechanism M4 built for this shape."""
    many = tuple(f"item-{index}" for index in range(60))
    outcome = await fixture.apply(SourceEvent(kind=SourceEventKind.ITEM_ADDED, external_ids=many))
    assert outcome.deferred_to_delta is True
    assert outcome.items_ingested == 0
    assert fixture.adapter.item_reads == []
    assert fixture.events.published == []


async def test_an_item_the_source_forgot_is_skipped(fixture: _Fixture) -> None:
    """`get_item` answers `None` for an item that is gone, and an event
    naming one is ordinary: an `ItemsUpdated` and an `ItemsRemoved` for the
    same id can arrive in either order."""
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=("never-existed",))
    )
    assert outcome.items_ingested == 0
    assert fixture.commits == 0, "an event that stored nothing opened a transaction"


async def test_a_removed_item_retracts_nothing(fixture: _Fixture) -> None:
    """ADR-0015: availability is retracted only by a walk that provably
    finished. An Emby library refresh emits `ItemsRemoved` for items that
    have not gone anywhere, and Usher cannot tell that from a deletion --
    one of which is irreversible. PRD 08 prices the delay: availability goes
    stale, not wrong. Counted and logged so it is visible rather than
    invisible."""
    await fixture.given_matched("gone-1")
    await fixture.given_matched("gone-2")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1", "gone-2"))
    )
    assert outcome.ignored == 2
    assert outcome.items_ingested == 0
    assert fixture.events.published == []
    for external_id in ("gone-1", "gone-2"):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, external_id)
        assert stored is not None and stored.available is True


# -- the unit of work -------------------------------------------------------


async def test_applying_an_event_commits_once(fixture: _Fixture) -> None:
    """A push lane holding an open transaction between events is an
    idle-in-transaction connection holding a snapshot for as long as the
    library is quiet. One event, one unit of work."""
    await fixture.given_matched("i1")
    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(SourceWatchState(external_id="i1", position_seconds=1, played=False),),
        )
    )
    assert fixture.commits == 1


async def test_the_span_names_the_event_kind(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """PRD 10's span tree. One root per event -- a push lane has no request
    above it, and unlike a job there is no enqueueing span to link to."""
    await fixture.given_matched("i1")
    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(SourceWatchState(external_id="i1", position_seconds=1, played=False),),
        )
    )
    names = {span.name for span in spans.get_finished_spans()}
    assert "push.watch_state_changed" in names
