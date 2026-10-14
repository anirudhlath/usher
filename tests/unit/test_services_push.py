"""The push lane: applying one event, and supervising the channel."""

import asyncio
import inspect
import itertools
import time
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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
from usher.domain.jobs import JobKind
from usher.domain.rows import BuiltRow, DisplayHint, RowFamily
from usher.domain.source import Source
from usher.ports.errors import PortUnavailable
from usher.ports.events import ClientEvent, ClientEventKind
from usher.ports.ingest import MediaItemUpsert, Retraction
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceEventKind,
    SourceItem,
    SourceItemKind,
    SourceNotSupported,
    SourceWatchState,
)
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.push import PushApplyService, PushOutcome, PushSupervisor
from usher.services.rows.cache import RowCache
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
            rollback=self._rollback,
        )
        # A real `RowCache` rather than a spy: the property under test is
        # that a *cached screen goes away*, and a spy would assert that a
        # method was called, which is satisfied by a call that invalidates the
        # wrong household or the wrong slugs.
        self.rows = RowCache(clock=lambda: T0)
        self.applier = PushApplyService(
            self.ingest,
            self.watch,
            self.events,
            self._commit,
            media_items=self.media_items,
            cache=self.rows,
            max_items_per_event=max_items_per_event,
        )
        self.title_ids: dict[str, uuid.UUID] = {}

    async def _commit(self) -> None:
        self.commits += 1

    async def _rollback(self) -> None:
        return None

    async def given_row(
        self,
        external_id: str,
        *,
        title_id: uuid.UUID | None,
        episode_id: uuid.UUID | None = None,
        changed_at: AwareDatetime = T0,
    ) -> None:
        """A stored, available row for `external_id`, which the source also serves."""
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

    async def given_matched(self, external_id: str, *, changed_at: AwareDatetime = T0) -> uuid.UUID:
        title_id = new_id()
        await self.given_row(external_id, title_id=title_id, changed_at=changed_at)
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
    """The whole reason `SourceEvent` grew a payload.

    A household pausing a film emits one of these every few seconds; a lane that asked
    the source for each would spend a 1-5 s round trip per second of playback.
    """
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
    """`watch_states` is a subset keyed by `external_id`.

    So a source whose message shape this adapter could not parse -- or a second
    source that sends ids only -- still works, at one request per id.
    """
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
    """`get_watch_state` answering `None` means the source deleted it.

    That is the reconcile lane's problem: raising here would cost a reconnect
    and a gap-closing delta walk for an item that is simply gone.
    """
    await fixture.given_matched("gone")
    fixture.adapter.forget("gone")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=("gone",))
    )
    assert outcome.states_merged == 0
    assert outcome.deferred_to_delta is False


async def test_a_carried_state_keeps_its_absent_play_history(fixture: _Fixture) -> None:
    """An absent `play_count` stays absent rather than becoming a fabricated `0`.

    A `UserDataChanged` entry is neither a listing nor an item route, nothing
    here has parsed a real one, and a fabricated `0` overwrites a household's
    history permanently. The `WATCH_HISTORY` backfill recovers it.
    """
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
    """PRD 03's "latest wins" refuses a merge older than what is stored.

    Publishing anyway makes a detail screen re-render on every echo of a
    position a client set itself.
    """
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
    # Three events, and the sequence is asserted rather than filtered: one
    # `row.invalidated` per slug a watch state can move, published *before* the
    # watch-state event, so a client that refetches on the first gets a screen
    # composed after the cache was cleared.
    assert [event.kind for event in fixture.events.published] == [
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.WATCHSTATE_UPDATED,
    ]
    assert [event.data["slug"] for event in fixture.events.published[:2]] == [
        "continue-watching",
        "next-up",
    ]
    assert fixture.events.published[2].title_id == title_id
    assert fixture.events.published[2].data["position_seconds"] == 61


async def test_an_unmatched_item_does_not_shift_what_the_others_publish(
    fixture: _Fixture,
) -> None:
    """The mis-pairing: merged targets zipped against the batch that was handed in.

    The targets are the *matched subset*, so one unmatched item at the front
    shifts every pair by one and a client renders item A's resume position on
    item B's screen. PRD 02 guarantees there will always be unmatched items, so
    this is the ordinary case. The positions are distinct so a swap is visible;
    equal positions would pass against the bug.
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
    # The row invalidations carry no `title_id` and no position, so this pairing
    # is over the watch-state events alone -- named by kind rather than by an
    # index, because an index would silently re-point if the slug set grew.
    assert [
        (event.title_id, event.data["position_seconds"])
        for event in fixture.events.published
        if event.kind is ClientEventKind.WATCHSTATE_UPDATED
    ] == [(first, 22), (second, 33)]


async def test_a_large_watch_event_with_no_payload_defers_to_a_delta(fixture: _Fixture) -> None:
    """The same cap, on the path that actually costs a request per item.

    An adapter that cannot parse a message's payload still names the ids, and a
    request per changed item on a lane budgeted at one connection per source is
    a design defect rather than a slow path.
    """
    many = tuple(f"item-{index}" for index in range(60))
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=many)
    )
    assert outcome.deferred_to_delta is True
    assert fixture.adapter.watch_state_reads == []


async def test_a_large_watch_event_that_carries_its_payload_is_not_deferred(
    fixture: _Fixture,
) -> None:
    """The cap counts what has to be *asked for*, not what arrived.

    A source that sent sixty states in one message costs one batched merge and no
    requests at all, and deferring it would trade that for a paged walk.
    """
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
    """Emby emits `LibraryChanged` during a library scan and it can name thousands.

    A request per changed item on a lane budgeted at one connection per source
    is not slow, it is a design defect -- and a delta walk is one paged request
    per `USHER_SOURCE_PAGE_SIZE` items under `MinDateLastSaved`.
    """
    many = tuple(f"item-{index}" for index in range(60))
    outcome = await fixture.apply(SourceEvent(kind=SourceEventKind.ITEM_ADDED, external_ids=many))
    assert outcome.deferred_to_delta is True
    assert outcome.items_ingested == 0
    assert fixture.adapter.item_reads == []
    assert fixture.events.published == []


async def test_an_item_the_source_forgot_is_skipped(fixture: _Fixture) -> None:
    """`get_item` answering `None` for an item that is gone is ordinary.

    An `ItemsUpdated` and an `ItemsRemoved` for the same id can arrive in
    either order.
    """
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=("never-existed",))
    )
    assert outcome.items_ingested == 0
    assert fixture.commits == 0, "an event that stored nothing opened a transaction"


async def test_a_confirmed_removal_retracts_and_publishes_after_its_commit(
    fixture: _Fixture,
) -> None:
    """Two copies of one film are one `title.updated`; an unmatched row is retracted silently."""
    film = new_id()
    await fixture.given_row("copy-a", title_id=film)
    await fixture.given_row("copy-b", title_id=film)
    other = await fixture.given_matched("other-1")
    await fixture.given_row("unmatched-1", title_id=None)
    removed = ("copy-a", "copy-b", "other-1", "unmatched-1")
    for external_id in removed:
        fixture.adapter.forget(external_id)
    commits_at_publish: list[int] = []
    publish = fixture.events.publish

    async def _recording(event: ClientEvent) -> None:
        commits_at_publish.append(fixture.commits)
        await publish(event)

    fixture.events.publish = _recording  # type: ignore[method-assign]
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=removed)
    )
    assert outcome == PushOutcome(items_retracted=4)
    assert fixture.commits == 2, "the read's commit before asking, then the retraction's"
    assert commits_at_publish == [2, 2], "a client heard of a retraction before its commit"
    published = fixture.events.published
    assert len(published) == 2
    assert {(event.kind, event.title_id, event.episode_id) for event in published} == {
        (ClientEventKind.TITLE_UPDATED, film, None),
        (ClientEventKind.TITLE_UPDATED, other, None),
    }
    assert all(event.data == {"fields": ["availability"]} for event in published)
    for external_id in removed:
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, external_id)
        assert stored is not None and stored.available is False, external_id


async def test_a_removal_publishes_in_sorted_order_whatever_order_its_targets_iterate(
    fixture: _Fixture,
) -> None:
    """Sorted by title id, so the order a client hears does not follow set iteration.

    Literal ids, because a `UUID`'s hash and `None`'s, unlike a `str`'s, are the same on
    every run. That is not enough on its own: the fake adds the targets in the order of a
    set of `str` ids, which moves with the seed, and where two targets share a slot of the
    set's table, the one added first takes it. These three take three of the eight slots a
    three-entry set has (`hash(target) % 8` is 7, 3 and 0 for `first`, `second` and
    `third`), so they iterate in one order whatever order they arrive in, and the premise
    shows it is not the sorted one. New ids need distinct slots too: with ids ending in 0, 1
    and 6, two share slot 7 and the premise fails under some seeds. The rows are stored, and
    the event names them, in two orders that are not sorted either, so neither can stand in
    for the sort.
    """
    first, second, third = (uuid.UUID(f"00000000-0000-7000-8000-00000000000{n}") for n in "124")
    rows = {"gone-a": second, "gone-b": third, "gone-c": first}
    for external_id, title_id in rows.items():
        await fixture.given_row(external_id, title_id=title_id)
        fixture.adapter.forget(external_id)
    retract = fixture.media_items.retract
    returned: list[Retraction] = []

    async def _spying(
        source_id: uuid.UUID, external_ids: Collection[str], *, unseen_since: AwareDatetime
    ) -> Retraction:
        retraction = await retract(source_id, external_ids, unseen_since=unseen_since)
        returned.append(retraction)
        return retraction

    fixture.media_items.retract = _spying  # type: ignore[method-assign]
    await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-b", "gone-c", "gone-a"))
    )
    (retraction,) = returned
    assert [target.title_id for target in retraction.targets] != [first, second, third], (
        "the premise: the targets iterate out of sorted order"
    )
    assert [event.title_id for event in fixture.events.published] == [first, second, third]


async def test_a_removal_the_source_still_serves_changes_nothing(fixture: _Fixture) -> None:
    """What an Emby library refresh sends: `ItemsRemoved` for a file that is still there."""
    await fixture.given_matched("kept-1")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("kept-1",))
    )
    assert fixture.adapter.confirm_calls == [frozenset({"kept-1"})]
    assert outcome == PushOutcome()
    assert fixture.commits == 1, "only the read's transaction, ended before asking"
    assert fixture.events.published == []
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "kept-1")
    assert stored is not None and stored.available is True


async def test_a_removal_of_ids_usher_does_not_hold_asks_nothing(fixture: _Fixture) -> None:
    """Folders, seasons and extras are removed too, and Usher stores no row for them."""
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("folder-1", "season-1"))
    )
    assert fixture.adapter.confirm_calls == []
    assert outcome == PushOutcome()
    assert fixture.commits == 0


async def test_a_removal_larger_than_the_item_limit_is_applied_not_deferred() -> None:
    """The source is asked a hundred ids a request, so a removal never costs a delta walk."""
    fixture = _Fixture(max_items_per_event=1)
    for external_id in ("gone-1", "gone-2"):
        await fixture.given_matched(external_id)
        fixture.adapter.forget(external_id)
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1", "gone-2"))
    )
    assert outcome == PushOutcome(items_retracted=2)


async def test_a_removal_the_source_cannot_confirm_raises_and_changes_nothing(
    fixture: _Fixture,
) -> None:
    """As any failed read in the lane does."""
    await fixture.given_matched("gone-1")
    fixture.adapter.forget("gone-1")
    fixture.adapter.fail_confirm()
    with pytest.raises(PortUnavailable):
        await fixture.apply(
            SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1",))
        )
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "gone-1")
    assert stored is not None and stored.available is True
    assert fixture.commits == 1, "only the read's transaction, ended before asking"
    assert fixture.events.published == []


async def test_a_removal_naming_an_id_twice_asks_and_publishes_once(fixture: _Fixture) -> None:
    title_id = await fixture.given_matched("gone-1")
    fixture.adapter.forget("gone-1")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1", "gone-1"))
    )
    assert fixture.adapter.confirm_calls == [frozenset({"gone-1"})]
    assert outcome == PushOutcome(items_retracted=1)
    assert [event.title_id for event in fixture.events.published] == [title_id]


async def test_a_removal_of_an_already_retracted_row_asks_nothing(fixture: _Fixture) -> None:
    await fixture.given_matched("gone-1")
    fixture.adapter.forget("gone-1")
    premise = await fixture.media_items.retract(
        fixture.source.id, ["gone-1"], unseen_since=datetime.now(UTC)
    )
    assert premise.retracted == 1
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1",))
    )
    assert fixture.adapter.confirm_calls == []
    assert outcome == PushOutcome()
    assert fixture.events.published == []


async def test_a_removal_says_what_it_asked_and_what_it_retracted(fixture: _Fixture) -> None:
    await fixture.given_matched("gone-1")
    await fixture.given_matched("kept-1")
    fixture.adapter.forget("gone-1")
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        await fixture.apply(
            SourceEvent(
                kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1", "kept-1", "never-1")
            )
        )
    finally:
        logger.remove(sink)
    assert [line.rstrip("\n") for line in lines if "items removed" in line] == [
        "Living Room Emby reported 3 items removed: 2 held, 1 gone, 1 retracted, 1 still served"
    ]


async def test_a_row_read_since_the_removal_arrived_survives_a_gone_answer(
    fixture: _Fixture,
) -> None:
    """The guard is the instant the removal began, so a read after it wins over the answer.

    The log line counts the row gone and not retracted, so its numbers still add up.
    """
    await fixture.given_matched("back-1")
    fixture.adapter.forget("back-1")
    original = fixture.adapter.confirm_gone

    async def _read_back_first(external_ids: Collection[str]) -> frozenset[str]:
        await fixture.ingest.ingest_batch(
            fixture.source.id, [_item("back-1")], observed_at=datetime.now(UTC)
        )
        return await original(external_ids)

    fixture.adapter.confirm_gone = _read_back_first  # type: ignore[method-assign]
    before = datetime.now(UTC)
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        outcome = await fixture.apply(
            SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("back-1",))
        )
    finally:
        logger.remove(sink)
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "back-1")
    assert stored is not None
    assert stored.last_seen_at >= before, "the premise: read after the removal began"
    assert stored.available is True
    assert outcome == PushOutcome()
    assert fixture.events.published == []
    assert [line.rstrip("\n") for line in lines if "items removed" in line] == [
        "Living Room Emby reported 1 items removed: 1 held, 1 gone, 0 retracted, 0 still served"
    ]


async def _given_a_series(fixture: _Fixture) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """`series-1` with episodes `ep-1` to `ep-3`, plus `ep-other` of another series.

    The source no longer serves `series-1`, `ep-1` or `ep-2`; it still serves `ep-3`.
    """
    series = new_id()
    await fixture.given_row("series-1", title_id=series)
    episodes = {f"ep-{number}": new_id() for number in (1, 2, 3)}
    for external_id, episode_id in episodes.items():
        await fixture.given_row(external_id, title_id=series, episode_id=episode_id)
    await fixture.given_row("ep-other", title_id=new_id(), episode_id=new_id())
    for external_id in ("series-1", "ep-1", "ep-2"):
        fixture.adapter.forget(external_id)
    return series, episodes


async def test_a_removed_series_retracts_its_episodes_asking_about_each_once(
    fixture: _Fixture,
) -> None:
    """Emby named `ep-1` beside the series and not `ep-2`; both go, and `ep-3` stays.

    `ep-1` was asked about with the series, so it is not asked again or counted twice.
    """
    series, episodes = await _given_a_series(fixture)
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        outcome = await fixture.apply(
            SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1", "ep-1"))
        )
    finally:
        logger.remove(sink)
    assert fixture.adapter.confirm_calls == [
        frozenset({"series-1", "ep-1"}),
        frozenset({"ep-2", "ep-3"}),
    ]
    assert outcome == PushOutcome(items_retracted=3)
    expected = sorted(
        [(series, None), (series, episodes["ep-1"]), (series, episodes["ep-2"])],
        key=lambda pair: (str(pair[0]), str(pair[1])),
    )
    assert [(event.title_id, event.episode_id) for event in fixture.events.published] == expected
    for external_id, available in (
        ("series-1", False),
        ("ep-1", False),
        ("ep-2", False),
        ("ep-3", True),
        ("ep-other", True),
    ):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, external_id)
        assert stored is not None and stored.available is available, external_id
    assert [line.rstrip("\n") for line in lines if "items removed" in line] == [
        "Living Room Emby reported 2 items removed: 4 held, 3 gone, 3 retracted, 1 still served"
    ]


async def test_a_removal_ends_each_reads_transaction_before_asking_the_source(
    fixture: _Fixture,
) -> None:
    """Nothing is written before an answer, so no connection waits on one inside a transaction.

    The named rows are read, then the series' episodes; each read is committed before its
    question goes out, and the retraction after both is committed once more.
    """
    await _given_a_series(fixture)
    commits_at_ask: list[int] = []
    original = fixture.adapter.confirm_gone

    async def _recording(external_ids: Collection[str]) -> frozenset[str]:
        commits_at_ask.append(fixture.commits)
        return await original(external_ids)

    fixture.adapter.confirm_gone = _recording  # type: ignore[method-assign]
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1",))
    )
    assert outcome == PushOutcome(items_retracted=3), "the premise: both questions were asked"
    assert commits_at_ask == [1, 2]
    assert fixture.commits == 3


async def test_a_served_series_does_not_ask_about_its_episodes(fixture: _Fixture) -> None:
    """Only a series the source confirms gone takes its episodes with it, even a gone one."""
    series = new_id()
    await fixture.given_row("series-1", title_id=series)
    await fixture.given_row("ep-1", title_id=series, episode_id=new_id())
    fixture.adapter.forget("ep-1")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1",))
    )
    assert fixture.adapter.confirm_calls == [frozenset({"series-1"})]
    assert outcome == PushOutcome()


# -- the unit of work -------------------------------------------------------


async def test_applying_an_event_commits_once(fixture: _Fixture) -> None:
    """One event, one unit of work.

    A push lane holding an open transaction between events is an
    idle-in-transaction connection holding a snapshot for as long as the
    library is quiet.
    """
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
    """PRD 10's span tree.

    One root per event -- a push lane has no request above it, and unlike a job there is
    no enqueueing span to link to.
    """
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


# =========================================================================== The
# supervisor: reconnect, backoff, and the gap it closes.


class _Lane:
    """The three injected unit-of-work callables, recorded.

    Every one of them opens its own session in production, which is why they
    are callables rather than services: a supervisor that held a session
    would hold it for the life of the socket -- hours, idle in transaction,
    with a snapshot from whenever the lane started.
    """

    def __init__(self) -> None:
        self.applied: list[SourceEvent] = []
        self.gaps = 0
        self.gap_windows: list[tuple[float, float]] = []
        self.push_available: list[bool] = []
        self.outcome = PushOutcome()
        self.gap_seconds = 0.0
        # Shared with a scripted adapter that notes its own opens, so the
        # ordering case reads one list rather than monkeypatching a bound
        # method it then has to type-ignore.
        self.order: list[str] = []

    async def apply(
        self, source: Source, adapter: SourceAdapter, event: SourceEvent
    ) -> PushOutcome:
        self.applied.append(event)
        return self.outcome

    async def close_gap(self, source: Source, adapter: SourceAdapter) -> None:
        started = time.perf_counter()
        self.gaps += 1
        self.order.append("gap")
        if self.gap_seconds:
            # Real wall time, in the two cases that measure an overlap. A
            # gap closer that returned immediately would let the event loop
            # run the whole connection through its cycle before the socket
            # ever produced anything, and "the socket buffered during the
            # walk" would be a claim about a walk that took no time.
            await asyncio.sleep(self.gap_seconds)
        self.gap_windows.append((started, time.perf_counter()))

    async def set_push_available(self, source: Source, available: bool) -> None:
        self.push_available.append(available)


_DROP = SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("__drop__",))

# A frame the channel receives and maps to no event, as Emby's `Sessions` is: most of
# what an idle library's channel ever sends.
_MESSAGE = SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=("__message__",))


class _ScriptedAdapter(FakeSourceAdapter):
    """A source whose push channel is a script of connections."""

    def __init__(
        self,
        source: Source,
        connections: list[list[SourceEvent]],
        *,
        emit_interval: float = 0.0,
        max_attempts: int = 40,
        hang: bool = False,
        unbounded: bool = False,
    ) -> None:
        super().__init__(source)
        self._script = list(connections)
        self._emit_interval = emit_interval
        self._max_attempts = max_attempts
        self._hang = hang
        self._unbounded = unbounded
        self._delivered_here = 0
        self._channel_open = False
        self.push_connections = 0
        self.attempts = 0
        self.messages = 0
        self.produced_at: list[float] = []
        self.parked = asyncio.Event()

    @property
    def push_messages_received(self) -> int:
        """Every frame this source delivered, over every connection, event or not."""
        return self.messages

    @property
    def supports_push(self) -> bool:
        """Grounded in frames delivered on *this* connection.

        The lane's own history is deliberately not carried across: a fresh
        socket that upgrades and buffers must read `False` however well its
        predecessor was working, which is the whole of the rule
        `PushHealth.record_open` spells out by clearing `last_message_at`.
        """
        return self._channel_open and self._delivered_here > 0

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        self.attempts += 1
        if not self._push_supported:
            # `disable_push()` is inherited, and it raises *before* the
            # attempt is scripted: an adapter with no channel has no
            # connection to count.
            raise SourceNotSupported("push is disabled on this scripted source")
        if self.attempts > self._max_attempts:
            # Not a `UsherPortError`, deliberately: the supervisor must not
            # be able to catch it. A loop that stopped counting failures
            # fails here in milliseconds instead of spinning until an outer
            # `wait_for` that a starved event loop can never fire.
            raise AssertionError(
                f"the supervisor opened {self.attempts} channels; it is not counting failures"
            )
        if not self._script:
            if not self._unbounded:
                raise PortUnavailable("the scripted source refused the connection")
            # A proxy that upgrades and then buffers connects perfectly **every** time
            # -- there is no supply of connections to run out of, which is exactly why
            # the ceiling has to come from the failure counter rather than from the
            # world getting tired.
            return self._open([])
        return self._open(self._script.pop(0))

    @asynccontextmanager
    async def _open(self, frames: list[SourceEvent]) -> AsyncIterator[AsyncIterator[SourceEvent]]:
        self.push_connections += 1
        self._channel_open = True
        self._delivered_here = 0
        queue: asyncio.Queue[SourceEvent] = asyncio.Queue()
        producer = asyncio.create_task(self._produce(frames, queue))
        try:
            yield self._frames(queue)
        finally:
            producer.cancel()
            self._channel_open = False

    async def _produce(self, frames: list[SourceEvent], queue: asyncio.Queue[SourceEvent]) -> None:
        for frame in frames:
            await asyncio.sleep(self._emit_interval)
            self.produced_at.append(time.perf_counter())
            queue.put_nowait(frame)
        if not self._hang:
            queue.put_nowait(_DROP)

    async def _frames(self, queue: asyncio.Queue[SourceEvent]) -> AsyncIterator[SourceEvent]:
        while True:
            # One cooperative yield per iteration, for the reason
            # `EmbyPushChannel._events` spells out at length: a loop whose
            # only other await can complete without suspending starves the
            # event loop it shares with the server.
            await asyncio.sleep(0)
            self.parked.set()
            frame = await queue.get()
            if frame is _DROP:
                raise PortUnavailable("the scripted peer went away")
            self._delivered_here += 1
            self.messages += 1
            if frame is _MESSAGE:
                continue
            yield frame


class _QuietAdapter(_ScriptedAdapter):
    """A channel whose iterator *ends* rather than raising.

    The port forbids it -- an iterator that stops because the connection
    died is indistinguishable from a source with nothing more to say -- and
    an adapter can still do it. What must not happen is a lane reading that
    as a clean shutdown and returning silently, leaving a source that stops
    pushing until somebody notices by hand.
    """

    async def _frames(self, queue: asyncio.Queue[SourceEvent]) -> AsyncIterator[SourceEvent]:
        await asyncio.sleep(0)
        return
        yield  # pragma: no cover -- makes this a generator


def _ticks(step: float = 1.0) -> Callable[[], float]:
    """A monotonic clock that moves by `step` on every read."""
    state = itertools.count(0.0, step)

    def read() -> float:
        return next(state)

    return read


def _supervisor(
    lane: _Lane,
    *,
    clock: Callable[[], float] | None = None,
    sleeps: list[float] | None = None,
    **kwargs: object,
) -> PushSupervisor:
    recorded = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)
        # Yields even though it does not wait. Without this the loop can run
        # unbounded iterations without ever returning to the event loop, and
        # a mutation that stopped counting failures would hang the case
        # rather than fail it -- `asyncio.wait_for` needs the loop to run in
        # order to fire.
        await asyncio.sleep(0)

    return PushSupervisor(
        lane.apply,
        lane.close_gap,
        lane.set_push_available,
        sleep=sleep,
        clock=clock if clock is not None else _ticks(),
        # A deterministic draw, so the *schedule* is asserted rather than a
        # range. The jitter itself is asserted separately, below.
        jitter=lambda low, high: high,
        **kwargs,  # type: ignore[arg-type]
    )


def _event(external_id: str) -> SourceEvent:
    return SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=(external_id,))


def _overlap(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Intersection over union of two wall-clock windows.

    The shape `JobQueueContract.overlapping()` established and for the same
    reason: a count, an ordering or a completion is also what a *serialised*
    run produces, and only an observed intersection distinguishes them.
    """
    intersection = max(0.0, min(first[1], second[1]) - max(first[0], second[0]))
    union = max(first[1], second[1]) - min(first[0], second[0])
    return 0.0 if union <= 0 else intersection / union


SUPERVISED_SOURCE = Source(
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-2",
    device_id="device-2",
)


@pytest.fixture
def lane() -> _Lane:
    return _Lane()


async def test_the_gap_is_closed_after_connecting_not_before(lane: _Lane) -> None:
    """PRD 03's "run a delta reconcile on reconnect", *after* the socket is up.

    Events arriving during the walk are then buffered rather than lost; the
    reverse order leaves the window between the walk and the handshake
    uncovered. The order alone is not the property -- `order == ["connected",
    "gap"]` is satisfied by a connect-close-then-walk implementation and by any
    run the event loop serialised -- so the case forces a real walk against a
    producer emitting on the open socket and asserts the two windows intersect.
    """
    adapter = _ScriptedAdapter(
        SUPERVISED_SOURCE,
        [[_event(f"i{index}") for index in range(6)]],
        emit_interval=0.005,
    )
    lane.gap_seconds = 0.04
    supervisor = _supervisor(lane, max_consecutive_failures=1)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)

    gap_window = lane.gap_windows[0]
    produced = (adapter.produced_at[0], adapter.produced_at[-1])
    overlap = _overlap(gap_window, produced)
    assert overlap >= 0.4, f"the walk and the socket did not overlap ({overlap:.1%} of the union)"
    assert produced[0] >= gap_window[0] and produced[1] <= gap_window[1], (
        "the source produced outside the walk's window, so this measured nothing"
    )
    assert [event.external_ids for event in lane.applied] == [
        (f"i{index}",) for index in range(6)
    ], "events produced while the gap-closing walk ran were lost"


async def test_the_gap_runs_inside_the_channels_own_context(lane: _Lane) -> None:
    """The cheap companion to the case above, and the one that names the ordering directly.

    Kept because the overlap case would also pass against a lane that opened the socket,
    ran the walk, and *then* subscribed.
    """

    class _Noting(_ScriptedAdapter):
        @asynccontextmanager
        async def _open(
            self, frames: list[SourceEvent]
        ) -> AsyncIterator[AsyncIterator[SourceEvent]]:
            lane.order.append("connected")
            async with super()._open(frames) as stream:
                yield stream

    adapter = _Noting(SUPERVISED_SOURCE, [[_event("i1")]])
    supervisor = _supervisor(lane, max_consecutive_failures=1)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.order[:2] == ["connected", "gap"]


async def test_a_dropped_channel_is_reconnected_and_the_gap_closed_again(lane: _Lane) -> None:
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event("i1")], [_event("i2")]])
    supervisor = _supervisor(lane, clock=_ticks(step=1000.0), max_consecutive_failures=2)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert adapter.push_connections == 2
    assert lane.gaps == 2
    assert [event.external_ids for event in lane.applied] == [("i1",), ("i2",)]


async def test_the_failure_counter_is_reset_by_delivery_not_by_connection(lane: _Lane) -> None:
    """The counter is reset by delivery, not by connecting.

    A proxy that upgrades and then buffers connects perfectly every time. If
    connecting reset the counter, that source would reconnect forever,
    silently, reporting a healthy lane -- and PRD 08's rule to mark
    `supports_push = false` after `USHER_PUSH_MAX_CONSECUTIVE_FAILURES` would never
    fire, so the reconciler would go on skipping the one source it is the only
    cover for. Three connections that
    open cleanly and deliver nothing is exactly that proxy.
    """
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [], unbounded=True)
    sleeps: list[float] = []
    supervisor = _supervisor(
        lane, clock=_ticks(step=1000.0), sleeps=sleeps, max_consecutive_failures=3
    )
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert adapter.push_connections == 3
    assert lane.push_available == [False]
    assert sleeps == [5.0, 10.0], "the last failure hits the ceiling and must not sleep"


async def test_a_delivering_channel_resets_the_counter(lane: _Lane) -> None:
    """The other direction: a working lane must not park itself on an ordinary blip.

    Three connections that each deliver one event and then drop, followed by
    one that delivers nothing. With the reset the ceiling of two is reached on
    the fourth connection; without it, on the second.
    """
    adapter = _ScriptedAdapter(
        SUPERVISED_SOURCE,
        [[_event("i1")], [_event("i2")], [_event("i3")]],
        unbounded=True,
    )
    supervisor = _supervisor(lane, clock=_ticks(step=1000.0), max_consecutive_failures=2)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert adapter.push_connections == 4
    assert True in lane.push_available


async def test_a_connection_that_delivered_only_messages_resets_the_counter(lane: _Lane) -> None:
    """Delivery is a message, not an event.

    An idle library's channel sends frames that map to no event and little else, so a
    counter reset only by events parks a working lane after five quiet spells however
    far apart they fall. Three connections that each deliver one such frame and drop,
    then one that delivers nothing: with the reset the ceiling of two is reached on the
    fourth connection; without it, on the second.
    """
    adapter = _ScriptedAdapter(
        SUPERVISED_SOURCE, [[_MESSAGE], [_MESSAGE], [_MESSAGE]], unbounded=True
    )
    supervisor = _supervisor(lane, clock=_ticks(step=1000.0), max_consecutive_failures=2)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.applied == [], "the premise: no connection delivered an event"
    assert adapter.messages == 3, "the premise: three connections each delivered a frame"
    assert adapter.push_connections == 4


async def test_push_available_is_written_true_on_first_delivery(lane: _Lane) -> None:
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event("i1")]])
    supervisor = _supervisor(lane, max_consecutive_failures=1)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.push_available[0] is True


async def test_push_available_is_written_once_per_transition(lane: _Lane) -> None:
    """`sources` is a table an operator reads and a row an `UPDATE` rewrites.

    A lane that wrote on every message would issue one statement per second of playback
    for a value that changed once.
    """
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event(f"i{index}") for index in range(5)]])
    supervisor = _supervisor(lane, max_consecutive_failures=1)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.push_available == [True, False]


async def test_the_backoff_doubles_and_is_capped(lane: _Lane) -> None:
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [], unbounded=True)
    sleeps: list[float] = []
    supervisor = _supervisor(
        lane,
        clock=_ticks(step=1000.0),
        sleeps=sleeps,
        max_consecutive_failures=6,
        backoff_seconds=5.0,
        max_backoff_seconds=40.0,
    )
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    # `jitter=lambda low, high: high`, so these are the ceilings of each
    # interval: 5, 10, 20, 40, 40. Five sleeps for six failures -- the last
    # failure hits the ceiling and stops rather than sleeping.
    assert sleeps == [5.0, 10.0, 20.0, 40.0, 40.0]


def test_the_backoff_draws_from_the_upper_half_of_the_interval() -> None:
    """PRD 08's equal jitter, not full jitter.

    Full jitter's minimum draw is arbitrarily close to zero, so a share of failures
    retry effectively immediately -- the hot loop the backoff exists to prevent, merely
    rationed. The half-interval floor is what makes "a failed connection is not
    instantly retried" a property rather than a probability.
    """
    draws: list[tuple[float, float]] = []

    def record(low: float, high: float) -> float:
        draws.append((low, high))
        return low

    lane = _Lane()
    supervisor = PushSupervisor(
        lane.apply,
        lane.close_gap,
        lane.set_push_available,
        backoff_seconds=8.0,
        max_backoff_seconds=1000.0,
        jitter=record,
    )
    for failures in (1, 2, 3):
        supervisor._backoff(failures)
    assert draws == [(4.0, 8.0), (8.0, 16.0), (16.0, 32.0)]


def test_the_backoff_defaults_to_a_real_uniform_draw() -> None:
    """Every case above injects the jitter, so the default is asserted directly.

    All of them pass against a default of `lambda low, high: low`, which is not
    jitter at all and puts every source in a household on the same schedule.
    """
    lane = _Lane()
    supervisor = PushSupervisor(lane.apply, lane.close_gap, lane.set_push_available)
    draws = {round(supervisor._backoff(3), 6) for _ in range(200)}
    assert len(draws) > 100, "the default jitter is not drawing a range"
    assert min(draws) >= 10.0 and max(draws) < 20.0


async def test_a_deferred_event_triggers_a_gap_close(lane: _Lane) -> None:
    """`deferred_to_delta` is the applier declining to resolve items one at a time.

    The supervisor is what turns that into a paged walk.
    """
    lane.outcome = PushOutcome(deferred_to_delta=True)
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event("i1")]])
    supervisor = _supervisor(lane, clock=_ticks(step=1000.0), max_consecutive_failures=1)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.gaps == 2


async def test_gap_closing_is_rate_limited(lane: _Lane) -> None:
    """A flapping socket plus a delta per reconnect is a paged walk every few seconds.

    The first delta after a real outage is the expensive one and is not
    skipped; the tenth in a minute is.
    """
    lane.outcome = PushOutcome(deferred_to_delta=True)
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event(f"i{index}") for index in range(5)]])
    # A clock that does not move, so every later request lands inside the
    # interval.
    supervisor = _supervisor(
        lane,
        clock=lambda: 100.0,
        max_consecutive_failures=1,
        gap_min_interval_seconds=60.0,
    )
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.gaps == 1
    assert len(lane.applied) == 5, "the rate limit swallowed the events too"


async def test_the_first_gap_after_an_outage_is_never_skipped(lane: _Lane) -> None:
    """The other half of the rate limit, which a counter assertion cannot see.

    `gate.at` is `None` until one has run, so the expensive walk after a real
    outage happens however recently the clock says something did. The interval
    bounds *cadence* -- how often a flapping socket may trigger a delta -- and
    says nothing about how large any one delta is; the size is refused in
    `LaneSupervisor._close_gap`, since this supervisor has no repository and
    cannot know whether a cursor exists.
    """
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event("i1")]])
    supervisor = _supervisor(
        lane, clock=lambda: 0.0, max_consecutive_failures=1, gap_min_interval_seconds=1e9
    )
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.gaps == 1


async def test_a_source_with_no_push_channel_is_marked_and_left_alone(lane: _Lane) -> None:
    """The port's `SourceNotSupported` contract.

    Not a failure to retry: an adapter saying it has no channel will say the
    same thing on every reconnect, and PRD 03's reconciler is the cover.
    `attempts` is the assertion with teeth and the other three are not -- a loop
    letting `SourceNotSupported` fall through to `except UsherPortError` reaches
    the ceiling instead of returning, so every visible end state is identical
    and only the wasted attempts and backoff sleeps differ.
    """
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event("i1")]])
    adapter.disable_push()
    sleeps: list[float] = []
    supervisor = _supervisor(lane, sleeps=sleeps, max_consecutive_failures=5)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert adapter.attempts == 1, "an adapter with no channel was asked again"
    assert sleeps == []
    assert lane.push_available == [False]
    assert adapter.push_connections == 0
    assert lane.gaps == 0


async def test_a_channel_that_ends_quietly_counts_as_a_failure(lane: _Lane) -> None:
    """The port forbids an iterator that ends rather than raising, and one still can.

    Counted as a failure rather than treated as a clean shutdown, because the
    alternative is a lane that returns silently and a source that stops pushing
    until somebody notices by hand.
    """
    adapter = _QuietAdapter(SUPERVISED_SOURCE, [], unbounded=True)
    supervisor = _supervisor(lane, clock=_ticks(step=1000.0), max_consecutive_failures=2)
    await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert adapter.push_connections == 2
    assert lane.push_available == [False]


async def test_cancellation_stops_the_lane_without_marking_the_source(lane: _Lane) -> None:
    """Shutdown is not a push failure.

    A lifespan that cancelled its lanes and left `supports_push = false` behind would
    disable push on every source on every restart of the server until a walk re-enabled
    it.
    """
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[]], hang=True)
    supervisor = _supervisor(lane, max_consecutive_failures=5)
    task = asyncio.create_task(supervisor.run(SUPERVISED_SOURCE, adapter))
    # Waited for rather than slept past: `asyncio.sleep(0)` once is not
    # enough to reach the parked `recv`, and a cancel that landed earlier
    # would test a different code path every time the scheduler felt like it.
    await asyncio.wait_for(adapter.parked.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lane.push_available == []


async def test_a_bug_in_the_lane_is_not_swallowed_as_a_push_failure(lane: _Lane) -> None:
    """`JobWorker`'s rule, one lane over.

    A `ZeroDivisionError` is a bug in this process, and a loop that counted it
    as a push failure would spend a backoff schedule and then park a healthy
    source with an error message describing nothing an operator can act on.
    """

    async def explode(source: Source, adapter: SourceAdapter, event: SourceEvent) -> PushOutcome:
        raise ZeroDivisionError("a bug, not an outage")

    lane.apply = explode  # type: ignore[method-assign]
    adapter = _ScriptedAdapter(SUPERVISED_SOURCE, [[_event("i1")]])
    supervisor = _supervisor(lane, max_consecutive_failures=3)
    with pytest.raises(ZeroDivisionError):
        await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=5.0)
    assert lane.push_available == [True], "a bug marked the source unavailable"


async def test_a_pushed_watch_state_invalidates_that_users_rows(fixture: _Fixture) -> None:
    """The push lane invalidates, because a push event *is* a change.

    The same sentence PRD 07 uses for `watchstate.updated`. Asserted on a
    *cached screen going away* rather than on a call being made: the slug set
    and the household are both part of being right, and a spy asserting
    `invalidate` was called is satisfied by a call that dropped somebody else's
    screen.
    """
    await fixture.given_matched("i1")
    other = new_id()
    fixture.rows.put_screen(fixture.user_id, (), ttl=timedelta(seconds=30))
    fixture.rows.put_row(
        fixture.user_id, "continue-watching", _built_row(), ttl=timedelta(seconds=30)
    )
    fixture.rows.put_screen(other, (), ttl=timedelta(seconds=30))

    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(SourceWatchState(external_id="i1", position_seconds=61, played=False),),
        )
    )

    assert fixture.rows.get_screen(fixture.user_id) is None
    assert fixture.rows.get_row(fixture.user_id, "continue-watching") is None
    assert fixture.rows.get_screen(other) is not None, "another household's screen was dropped"


async def test_a_refused_merge_leaves_the_cached_screen_alone(fixture: _Fixture) -> None:
    """Guarded on `rows_written`, exactly as the publish beside it is.

    An unmatched item merges nothing, and dropping a warm screen for it is a full
    recompose bought with no change to show for it -- once per second of playback, on
    the item a client just set.
    """
    fixture.rows.put_screen(fixture.user_id, (), ttl=timedelta(seconds=30))

    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("never-matched",),
            watch_states=(
                SourceWatchState(external_id="never-matched", position_seconds=61, played=False),
            ),
        )
    )

    assert fixture.rows.get_screen(fixture.user_id) is not None


async def test_the_nightly_walk_invalidates_nothing(fixture: _Fixture) -> None:
    """A nightly walk merges states in bulk, so it must not invalidate per row.

    The walk's changes reach the screen through the 30 s screen TTL and a
    demand read instead. Kills an `invalidate` call added to
    `WatchStateSyncService`'s merge loop, which is where it is most natural to
    write it and where nothing else would notice: the cache is correct and the
    screens are fresh. The service is handed no cache at all, so the mutation
    has to *add a constructor argument* to be written.
    """
    await fixture.given_matched("i1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="i1", position_seconds=61, played=False)
    )
    fixture.rows.put_screen(fixture.user_id, (), ttl=timedelta(seconds=30))

    run = await fixture.watch.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.items_matched == 1, "the walk merged nothing, so the case proves nothing"
    assert fixture.rows.get_screen(fixture.user_id) is not None
    assert "cache" not in inspect.signature(WatchStateSyncService.__init__).parameters


def _built_row() -> BuiltRow:
    return BuiltRow(
        slug="continue-watching",
        title="Continue Watching",
        family=RowFamily.SOURCE,
        display_hint=DisplayHint.LANDSCAPE,
        ttl=timedelta(seconds=60),
    )


async def test_a_pushed_watch_state_publishes_row_invalidated_for_the_rows_it_moved(
    fixture: _Fixture,
) -> None:
    """The push lane publishes because a push event *is* a change.

    One event per invalidated slug, and the slug set is small and fixed, so the
    fan-out is per *event* rather than per merged row. The payload is the slug
    and nothing else: PRD 07's client action is "refetch that row", and a frame
    with an empty `data` is a well-shaped instruction with no object.
    """
    await fixture.given_matched("i1")

    await fixture.apply(
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("i1",),
            watch_states=(SourceWatchState(external_id="i1", position_seconds=61, played=False),),
        )
    )

    invalidations = [
        event for event in fixture.events.published if event.kind is ClientEventKind.ROW_INVALIDATED
    ]
    assert [event.data for event in invalidations] == [
        {"slug": "continue-watching"},
        {"slug": "next-up"},
    ]
    assert all(event.title_id is None for event in invalidations), (
        "a row is not a title, and a title_id here is a filter key that half-works"
    )


async def test_the_nightly_walk_publishes_no_row_invalidated(fixture: _Fixture) -> None:
    """A nightly walk merges states in bulk, so it must not publish per row.

    One `row.invalidated` per merged row is a fan-out per row per night to every
    connected client *and* a thundering herd of refetches at 04:00. Kills a
    publish added to `WatchStateSyncService`'s merge loop, the most natural
    place to write it and the place nothing else would notice: the screens are
    fresh and the cache is correct. The service is handed no `EventPublisher` at
    all, so the mutation has to add a constructor argument to be written.
    """
    await fixture.given_matched("i1")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="i1", position_seconds=61, played=False)
    )

    run = await fixture.watch.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.items_matched == 1, "the walk merged nothing, so the case proves nothing"
    assert fixture.events.published == []
    assert "events" not in inspect.signature(WatchStateSyncService.__init__).parameters
