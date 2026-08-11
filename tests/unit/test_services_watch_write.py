"""`WatchWriteService` -- write locally, invalidate, publish, enqueue.

**The order is the contract and one case asserts the whole of it.**
`test_the_four_effects_happen_in_the_order_the_service_promises` drives every
collaborator through one journal, so "publish before the local write
committed" -- the defect
[ADR-0033](../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)
names -- is a reordering of a list rather than four separate assertions that
each pass on their own.

**Nothing here reaches a source, and that is structural rather than
defensive.** PRD 03's write-back is "best effort" as a description of *the
caller*: `push_watch_state` raises by contract, and a request that never
blocks or fails on a down source is only that if the request does not make
the call. The absence is asserted on the module's imports in
`tests/unit/test_api_watch.py`, because "it did not raise" is also what a
service that swallowed everything produces.

**Two divergences from Postgres that matter here**, both recorded in
`tests/fakes/watch_state_repository.py`: this fake stamps `updated_at` in
Python where a `BEFORE UPDATE` trigger owns it there, and its `last_played_at`
moves on every `played=True` write exactly as the shipped
`CASE WHEN excluded.played THEN now()` does. The second is why the
changed-row guard below is measured on `(position_seconds, played,
play_count)` and not on the whole row -- see `_changed`'s own docstring in
`services/watch_write.py`. `tests/integration/test_watch_routes.py` is what
runs the same claims against the real statement.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import WatchStateOrigin
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import MediaItem
from usher.domain.watch import WatchState
from usher.ports.errors import PortDataMalformed
from usher.ports.events import ClientEvent, ClientEventKind
from usher.ports.ingest import MediaItemUpsert, WatchStateWrite
from usher.services.rows import WATCH_STATE_ROWS
from usher.services.rows.cache import RowCache
from usher.services.watch_write import WatchWriteService

SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)


# -- recording collaborators -------------------------------------------


class _RecordingWatchStates(FakeWatchStateRepository):
    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def set_from_client(self, write: WatchStateWrite) -> WatchState:
        self._journal.append("write")
        return await super().set_from_client(write)


class _RecordingCache(RowCache):
    def __init__(self, journal: list[str]) -> None:
        super().__init__(clock=lambda: datetime.now(UTC))
        self._journal = journal
        self.invalidated: list[tuple[uuid.UUID, tuple[str, ...]]] = []

    def invalidate(self, user_id: uuid.UUID, slugs) -> None:  # type: ignore[no-untyped-def]
        self._journal.append("invalidate")
        materialised = tuple(slugs)
        self.invalidated.append((user_id, materialised))
        super().invalidate(user_id, materialised)


class _RecordingEvents(FakeEventPublisher):
    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def publish(self, event: ClientEvent) -> None:
        self._journal.append(f"publish:{event.kind.value}")
        await super().publish(event)


class _RecordingQueue(FakeJobQueue):
    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def enqueue(self, requests) -> int:  # type: ignore[no-untyped-def]
        self._journal.append("enqueue")
        return await super().enqueue(requests)


class _Household:
    """Every collaborator the service holds, sharing one journal."""

    def __init__(self) -> None:
        self.journal: list[str] = []
        self.watch_states = _RecordingWatchStates(self.journal)
        self.media_items = FakeMediaItemRepository()
        self.queue = _RecordingQueue(self.journal)
        self.events = _RecordingEvents(self.journal)
        self.cache = _RecordingCache(self.journal)
        self.user_id = uuid.uuid4()

    async def commit(self) -> None:
        self.journal.append("commit")

    def service(self) -> WatchWriteService:
        return WatchWriteService(
            watch_states=self.watch_states,
            media_items=self.media_items,
            queue=self.queue,
            events=self.events,
            commit=self.commit,
            cache=self.cache,
        )

    async def add_copy(
        self,
        *,
        title_id: uuid.UUID,
        episode_id: uuid.UUID | None = None,
        external_id: str,
        source_id: uuid.UUID | None = None,
    ) -> str:
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=source_id or uuid.uuid4(),
                    external_id=external_id,
                    title_id=title_id,
                    episode_id=episode_id,
                    container="mkv",
                    video_codec="hevc",
                    audio_codec="truehd",
                    width=3840,
                    height=2160,
                    hdr_format=None,
                    audio_channels=8,
                    file_size_bytes=1,
                    runtime_seconds=9360,
                    added_at=None,
                    last_seen_at=SEEN_AT,
                )
            ]
        )
        return external_id


@pytest.fixture
def household() -> _Household:
    return _Household()


def _keys(household: _Household) -> list[str]:
    return sorted(job.key for job in household.queue.jobs_of(JobKind.WATCH_WRITEBACK))


def _frames(household: _Household, kind: ClientEventKind) -> list[ClientEvent]:
    return [event for event in household.events.published if event.kind is kind]


# -- the copy read -----------------------------------------------------


async def test_a_title_write_enqueues_one_job_per_source_copy_and_not_one_per_episode_file(
    household: _Household,
) -> None:
    """The headline case, and the 20,001-row read it is about.

    An episode's `media_items` row carries its series' `title_id` **and** its
    own `episode_id`, so a title write that read `media_items` on `title_id`
    alone would answer a 20,000-episode series with one row per episode file
    and put 20,000 jobs on the queue for one press.
    `MediaItemRepository.list_for_title` is the read that carries
    `AND episode_id IS NULL` -- measured at 1 row in 0.251 ms against 20,001
    rows and 22.901 ms without it (`.claude/rules/db-and-sql.md`) -- and this
    case is what pins that the title path uses it.

    **The premise is asserted rather than assumed**: twenty episode rows are
    seeded and the case checks they are there, because a fixture that seeded
    none would satisfy `len(keys) == 1` while measuring nothing.
    """
    series_id = uuid.uuid4()
    source_id = uuid.uuid4()
    await household.add_copy(title_id=series_id, external_id="emby-series", source_id=source_id)
    episode_ids = [uuid.uuid4() for _ in range(20)]
    for number, episode_id in enumerate(episode_ids):
        await household.add_copy(
            title_id=series_id,
            episode_id=episode_id,
            external_id=f"emby-episode-{number}",
            source_id=source_id,
        )
    # The premise, read the only way this port can express it: every one of
    # the twenty episode rows is really there, and every one of them really
    # carries the series' `title_id`. A fixture that seeded none would satisfy
    # the assertion below while measuring nothing at all.
    for episode_id in episode_ids:
        found = await household.media_items.list_for_episode(episode_id)
        assert [copy.title_id for copy in found] == [series_id]

    await household.service().set_for_title(
        user_id=household.user_id, title_id=series_id, position_seconds=61, played=False
    )

    assert _keys(household) == ["emby-series"]


async def test_an_episode_write_enqueues_for_the_episodes_own_copy(
    household: _Household,
) -> None:
    """`list_for_episode`, never `list_for_title`. The series' own row is the
    one `list_for_title` would answer with and it is the wrong file: writing a
    resume position for episode 3 back onto the series' folder item is a write
    to something nobody played."""
    series_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    await household.add_copy(title_id=series_id, external_id="emby-series")
    await household.add_copy(
        title_id=series_id, episode_id=episode_id, external_id="emby-episode-3"
    )

    await household.service().set_for_episode(
        user_id=household.user_id, episode_id=episode_id, position_seconds=61, played=False
    )

    assert _keys(household) == ["emby-episode-3"]


async def test_one_job_per_source_when_two_sources_hold_the_same_title(
    household: _Household,
) -> None:
    """Two copies, two jobs. A household with two servers has to have both
    told, and `Job.key` is the source's own `external_id` -- so this is two
    rows rather than one with a list on it."""
    title_id = uuid.uuid4()
    await household.add_copy(title_id=title_id, external_id="living-room-42")
    await household.add_copy(title_id=title_id, external_id="loft-99")

    await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert _keys(household) == ["living-room-42", "loft-99"]


async def test_a_title_the_household_owns_no_copy_of_still_writes_locally(
    household: _Household,
) -> None:
    """`domain/watch.py`'s first sentence: watch state attaches to the
    canonical `Title`, not to a `MediaItem`, so it survives adding, changing
    or losing a source. Nothing is enqueued, and that is correct rather than a
    gap -- there is no source to tell."""
    title_id = uuid.uuid4()

    stored = await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert stored.position_seconds == 61
    assert await household.watch_states.get_for_title(household.user_id, title_id) is not None
    assert _keys(household) == []


async def test_the_write_back_is_enqueued_at_visible_priority(household: _Household) -> None:
    """80, and the reason is the number's neighbours rather than the number.
    Client-originated, so above every background sweep; below `DEMAND`, which
    means "a client opened this title right now" and is a read a client is
    blocking on. A write-back is not."""
    title_id = uuid.uuid4()
    await household.add_copy(title_id=title_id, external_id="living-room-42")

    await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    job = household.queue.jobs_of(JobKind.WATCH_WRITEBACK)[0]
    assert job.priority == JobPriority.VISIBLE
    assert JobPriority.BACKFILL < job.priority < JobPriority.DEMAND


# -- the order --------------------------------------------------------


async def test_the_four_effects_happen_in_the_order_the_service_promises(
    household: _Household,
) -> None:
    """Write locally, invalidate, publish, enqueue -- with the commit between
    the first and the second.

    **This is ADR-0033 as an executable statement.** An event is a claim about
    *committed* state, which is an ordering rule and not a durability one: a
    subscriber told a position landed and then reading it back through a
    second connection must find it. Publishing before the commit is the defect
    the ADR names, and against this journal it is a swap of two entries.

    The enqueue is deliberately last, and it rides the request's own commit
    boundary (`api/deps.get_session`) rather than this service's. See
    `WatchWriteService`'s module docstring for what a crash in that window
    costs and why it is not the outbox this project has twice refused.
    """
    title_id = uuid.uuid4()
    await household.add_copy(title_id=title_id, external_id="living-room-42")

    await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert household.journal == [
        "write",
        "commit",
        "invalidate",
        *[f"publish:{ClientEventKind.ROW_INVALIDATED.value}"] * len(WATCH_STATE_ROWS),
        f"publish:{ClientEventKind.WATCHSTATE_UPDATED.value}",
        "enqueue",
    ]
    assert len(WATCH_STATE_ROWS) >= 2, "the per-slug fan-out is the premise of the line above"


# -- invalidate and publish, guarded on the row having changed ---------


async def test_a_changed_write_invalidates_the_rows_and_publishes_both_kinds_of_frame(
    household: _Household,
) -> None:
    """The push lane's pair, on the push lane's terms
    (`services/push.py:176-211`): the cache drop and one `row.invalidated`
    per slug are two calls rather than one, because the cache is a dict and a
    dict that published events would be a second publisher nobody could see.
    """
    title_id = uuid.uuid4()

    await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert household.cache.invalidated == [(household.user_id, WATCH_STATE_ROWS)]
    invalidations = _frames(household, ClientEventKind.ROW_INVALIDATED)
    assert [frame.data["slug"] for frame in invalidations] == list(WATCH_STATE_ROWS)
    assert all(frame.title_id is None for frame in invalidations), (
        "a row is not a title -- ports/events.py says why this frame carries no filter key"
    )


async def test_the_watchstate_frame_carries_the_target_and_the_new_state(
    household: _Household,
) -> None:
    """PRD 07's payload, and the same three keys
    `PushApplyService._publish_watch_states` builds -- so a client handling
    the source's echo and a client handling its own write parse one shape.

    The frame carries the title id, which is what lets a client ignore its own
    echo instead of re-rendering on every second of playback it caused.
    """
    title_id = uuid.uuid4()

    await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    frame = _frames(household, ClientEventKind.WATCHSTATE_UPDATED)[0]
    assert frame.title_id == title_id
    assert frame.episode_id is None
    assert frame.data["position_seconds"] == 61
    assert frame.data["played"] is False
    assert isinstance(frame.data["observed_at"], str)


async def test_an_episode_frame_carries_both_ids(household: _Household) -> None:
    """`ClientEvent.title_id` is the **filter key** and an episode event
    carries its series' title alongside its own episode id -- a client
    watching a series subscribes with the series' title id, because that is
    the only id it has before it fetches a season.

    Here the episode has no series row to reach, so the frame carries the
    episode id and no title id: the service publishes what the stored row
    names, and `watch_states` holds exactly one of the two by CHECK. Recorded
    rather than papered over -- resolving the series would be a second read
    per press for a filter key `GET /events` can already be given by a client
    that fetched the season.
    """
    episode_id = uuid.uuid4()

    await household.service().set_for_episode(
        user_id=household.user_id, episode_id=episode_id, position_seconds=61, played=False
    )

    frame = _frames(household, ClientEventKind.WATCHSTATE_UPDATED)[0]
    assert frame.episode_id == episode_id
    assert frame.title_id is None


async def test_a_repeat_write_of_identical_state_publishes_nothing(
    household: _Household,
) -> None:
    """The guard `PushApplyService` states and this service inherits: a write
    that changed nothing is a full recompose per second of playback.

    Both directions in one file -- the case above is the changed one, and its
    positive assertions are what stop this one passing against a service that
    never publishes at all.
    """
    title_id = uuid.uuid4()
    service = household.service()
    await service.set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )
    household.events.published.clear()
    household.cache.invalidated.clear()

    await service.set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert household.events.published == []
    assert household.cache.invalidated == []


async def test_a_repeat_mark_played_publishes_nothing_even_though_last_played_at_moved(
    household: _Household,
) -> None:
    """The reason `_changed` reads three fields rather than comparing rows.

    Both arms of the shipped statement stamp `last_played_at` whenever
    `excluded.played` is true -- `CASE WHEN excluded.played THEN now()` has no
    "and it was not already played" clause -- so a second press of *Mark
    watched* moves that column while changing nothing a client can act on. A
    guard written as `before != after` is therefore dead on exactly this path,
    which is the one a household presses twice.
    """
    title_id = uuid.uuid4()
    service = household.service()
    await service.mark_title_played(user_id=household.user_id, title_id=title_id, played=True)
    first = await household.watch_states.get_for_title(household.user_id, title_id)
    household.events.published.clear()

    await service.mark_title_played(user_id=household.user_id, title_id=title_id, played=True)

    second = await household.watch_states.get_for_title(household.user_id, title_id)
    assert first is not None and second is not None
    assert second.last_played_at != first.last_played_at, (
        "the premise: the stored row did move, and the guard still declined to publish"
    )
    assert household.events.published == []


async def test_a_repeat_write_still_enqueues_the_write_back(household: _Household) -> None:
    """The enqueue is **not** guarded on the local row having changed, and the
    asymmetry is deliberate.

    The guard above measures Usher's own row before and after; it says nothing
    about the source, which may be out of step because an earlier write-back
    failed. `(kind, key)` coalesces, so an unchanged repeat costs one statement
    that usually writes zero rows -- against a household whose write silently
    never reaches its server.
    """
    title_id = uuid.uuid4()
    await household.add_copy(title_id=title_id, external_id="living-room-42")
    service = household.service()
    await service.set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )
    household.journal.clear()

    await service.set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert household.journal == ["write", "commit", "enqueue"]
    assert _keys(household) == ["living-room-42"]


async def test_a_deployment_with_no_row_cache_still_publishes(household: _Household) -> None:
    """`cache=None` is a real deployment rather than a test affordance -- the
    CLI's own roots compose no screen -- and it must not silence the frames a
    connected client is waiting on."""
    service = WatchWriteService(
        watch_states=household.watch_states,
        media_items=household.media_items,
        queue=household.queue,
        events=household.events,
        commit=household.commit,
        cache=None,
    )

    await service.set_for_title(
        user_id=household.user_id, title_id=uuid.uuid4(), position_seconds=61, played=False
    )

    assert _frames(household, ClientEventKind.WATCHSTATE_UPDATED)


# -- what the local write itself does ----------------------------------


async def test_the_local_write_is_recorded_as_the_households_own(
    household: _Household,
) -> None:
    """`origin = api` is the correctness property this route extends, not a
    label. It is what stops the next walk mistaking Usher's own write for the
    source's truth and round-tripping it back."""
    title_id = uuid.uuid4()

    stored = await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert stored.origin is WatchStateOrigin.API


async def test_marking_played_keeps_the_stored_position(household: _Household) -> None:
    """`POST /played` carries no body, so the position it writes is the one
    already stored. Zeroing it here would be Emby's `POST /PlayedItems`
    behaviour imported into the local row -- the source clears the resume
    point and Usher deliberately does not, because `GET /titles/{id}` renders
    both and a client showing 0 s for a film it finished has lost information
    the household can never get back."""
    title_id = uuid.uuid4()
    service = household.service()
    await service.set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=4000, played=False
    )

    stored = await service.mark_title_played(
        user_id=household.user_id, title_id=title_id, played=True
    )

    assert stored.played is True
    assert stored.position_seconds == 4000


async def test_unmarking_played_does_not_clear_the_position(household: _Household) -> None:
    """The local half of M3's destructive-route finding, asserted on the
    stored row.

    `DELETE /Users/{u}/PlayedItems/{item}` is destructive well beyond its
    name -- measured against Emby 4.9.5.0, it resets `PlayCount`, clears
    `LastPlayedDate` *and* clears a non-zero resume position -- and
    `EmbyAdapter.push_watch_state` already refuses to use it. This route must
    not do at the database what the adapter declines to do at the source.
    """
    title_id = uuid.uuid4()
    service = household.service()
    await service.set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=4000, played=True
    )
    played = await household.watch_states.get_for_title(household.user_id, title_id)
    assert played is not None and played.play_count == 1

    stored = await service.mark_title_played(
        user_id=household.user_id, title_id=title_id, played=False
    )

    assert stored.played is False
    assert stored.position_seconds == 4000
    assert stored.play_count == 1
    assert stored.last_played_at == played.last_played_at


async def test_marking_played_on_a_title_never_touched_starts_at_zero(
    household: _Household,
) -> None:
    """There is no stored position to keep, and `WatchStateWrite` has no
    "leave it alone" spelling -- `position_seconds` is always written. Zero is
    the only honest answer for a title the household has never opened."""
    title_id = uuid.uuid4()

    stored = await household.service().mark_title_played(
        user_id=household.user_id, title_id=title_id, played=True
    )

    assert stored.position_seconds == 0
    assert stored.played is True


async def test_a_write_naming_both_targets_is_refused(household: _Household) -> None:
    """Unreachable through the two public methods and pinned anyway, on the
    terms M4's two unreachable service guards were: the reads this service
    makes *before* `set_from_client` each need to know which target it is, so
    the port's own `num_nonnulls(title_id, episode_id) = 1` answer is restated
    one layer up rather than waited for."""
    with pytest.raises(PortDataMalformed):
        await household.service()._write(
            user_id=household.user_id,
            title_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            position_seconds=61,
            played=False,
        )


async def test_the_service_makes_no_second_read_for_the_state_it_answers_with(
    household: _Household,
) -> None:
    """`set_from_client` returns the stored row, so the route answers with the
    row the write produced rather than with a re-read of it. A re-read would
    be a second statement that can disagree -- and on Postgres it would be the
    trigger-stamped `updated_at` of a *later* instant."""
    title_id = uuid.uuid4()

    stored = await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )
    reread = await household.watch_states.get_for_title(household.user_id, title_id)

    assert reread is not None
    assert stored.id == reread.id


async def test_two_copies_sharing_an_external_id_become_one_write_back(
    household: _Household,
) -> None:
    """Two sources addressing the same item by the same string collapse to one
    job -- `Job.key` is unique across sources, which `domain/jobs.py` records
    as a deliberate trade with a known cost rather than as a property anyone
    wanted.

    **This pins the outcome and cannot pin who produced it**, which is the
    honest version of a case that was first written as *"the service
    deduplicates"*: the sweep measured `dict.fromkeys` deleted from
    `_enqueue_write_back` and it **survived all 47 cases**, because both arms
    of `JobQueue.enqueue` deduplicate already. The `dict.fromkeys` is kept for
    the order it preserves and is documented there as equivalent; the answer
    below is a statement about the pair.
    """
    title_id = uuid.uuid4()
    await household.add_copy(title_id=title_id, external_id="shared-id")
    await household.add_copy(title_id=title_id, external_id="shared-id")

    await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert _keys(household) == ["shared-id"]


async def test_an_unavailable_copy_is_still_told(household: _Household) -> None:
    """`list_for_title` returns retracted copies with `available = false`
    rather than dropping them (PRD 02: soft-delete availability), and the
    write-back is enqueued for them too.

    The common cause of a retraction is a temporarily unmounted drive, and
    D8's handler completes rather than parks for an item a source no longer
    has -- so including it costs one job that completes, and excluding it
    costs a household whose write never reaches a server that was merely
    offline when they pressed play.
    """
    title_id = uuid.uuid4()
    source_id = uuid.uuid4()
    await household.add_copy(title_id=title_id, external_id="gone-42", source_id=source_id)
    await household.media_items.mark_unseen_unavailable(
        source_id, seen_since=datetime.now(UTC), max_retract_fraction=1.0
    )
    copies: Sequence[MediaItem] = await household.media_items.list_for_title(title_id)
    assert [copy.available for copy in copies] == [False]

    await household.service().set_for_title(
        user_id=household.user_id, title_id=title_id, position_seconds=61, played=False
    )

    assert _keys(household) == ["gone-42"]
