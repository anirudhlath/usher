"""The in-memory client event bus.

**The one case this file exists for is the non-blocking one, and it is
asserted twice.** "The publish completed and the slow subscriber got its
event" is what a fully serialised run produces too, and this project has
measured that trap directly -- a deleted single-flight lock passed five runs
in a row against a transport that never truly awaited. So:

- `test_publish_never_suspends_when_a_subscribers_queue_is_full` drives the
  coroutine by hand, one step, with no event loop scheduling involved at
  all. A coroutine that never awaits raises `StopIteration` on its first
  `send(None)`; one that awaits a full `asyncio.Queue` yields a future
  instead. Deterministic, microseconds, and it fails on its own assertion
  rather than on a timeout -- which matters, because the mutation it rules
  out (`await queue.put` for `put_nowait`) *deadlocks* rather than answering
  wrongly, and an unbounded case would hang the suite.
- `test_publishing_does_not_block_on_a_subscriber_that_is_not_reading`
  measures the operational form on wall-clock intervals: the publisher's own
  window must sit inside the window during which a subscriber is provably
  parked and not reading, reported as intersection-over-union the way
  `JobQueueContract.overlapping()` and M5 group B1's channel cases do.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import pytest

from tests.contract.event_publisher_contract import (
    BusFactory,
    EventBusContract,
    EventPublisherContract,
    publish_all,
)
from usher.ports.events import ClientEvent, ClientEventKind, NullEventPublisher
from usher.services.events import DeferredEventPublisher, InMemoryEventBus

# Enough publishes that the window they occupy is measurable against a
# monotonic clock rather than being rounded to zero, and enough that a
# per-event suspension would be visible as one.
_BURST = 2_000

# The publish burst above is a few milliseconds of dictionary work. A second
# is three orders of magnitude of headroom and still finite, which is the
# whole point: the mutation blocks, and an unbounded case hangs the suite
# instead of failing it.
_BOUND_SECONDS = 1.0


def _event(index: int, title_id: uuid.UUID | None = None) -> ClientEvent:
    return ClientEvent(kind=ClientEventKind.SYNC_PROGRESS, title_id=title_id, data={"seen": index})


def _one_step(coroutine: Coroutine[Any, Any, None]) -> bool:
    """Did this coroutine run to completion without ever suspending?

    The whole of "publish is not a suspension point", with no event loop
    scheduling in it. `coro.send(None)` raises `StopIteration` when the
    coroutine finished in one step and returns the awaited future when it
    parked -- so this distinguishes `put_nowait` from `await put` on a full
    queue directly, rather than by waiting to see whether something hangs.
    """
    try:
        coroutine.send(None)
    except StopIteration:
        return True
    # It parked. Close it so the abandoned frame does not leave a pending
    # putter on the queue or a "never awaited" warning behind it.
    coroutine.close()
    return False


async def test_a_subscriber_receives_what_is_published_after_it_subscribed() -> None:
    bus = InMemoryEventBus()
    async with bus.subscribe() as stream:
        await bus.publish(_event(1))
        sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
    assert sent.event.data == {"seen": 1}
    assert sent.id == 1


async def test_two_subscribers_both_receive_it() -> None:
    bus = InMemoryEventBus()
    async with bus.subscribe() as first, bus.subscribe() as second:
        await bus.publish(_event(1))
        one = await asyncio.wait_for(anext(aiter(first)), timeout=1.0)
        two = await asyncio.wait_for(anext(aiter(second)), timeout=1.0)
    assert one.event.data == two.event.data == {"seen": 1}


async def test_a_subscriber_scoped_to_titles_is_not_woken_by_unrelated_churn() -> None:
    """PRD 07: "Subscriptions are scoped by query (`?titles=id1,id2`) so a
    detail screen isn't woken by unrelated churn." A nightly walk publishes
    a `sync.progress` per batch -- 1,127 of them against the one measured
    library -- and a detail screen that re-rendered on each is the reason
    this filter exists rather than being a nicety."""
    wanted, other = uuid.uuid4(), uuid.uuid4()
    bus = InMemoryEventBus()
    async with bus.subscribe(titles=frozenset({wanted})) as stream:
        await bus.publish(_event(1, title_id=other))
        await bus.publish(_event(2))  # unscoped: a sync.progress
        await bus.publish(_event(3, title_id=wanted))
        sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
    assert sent.event.data == {"seen": 3}


async def test_an_unfiltered_subscriber_receives_everything() -> None:
    """An admin UI wants `sync.progress`, which belongs to no title. The
    filter is opt-in, and its absence means "everything" rather than
    "nothing"."""
    bus = InMemoryEventBus()
    async with bus.subscribe() as stream:
        await bus.publish(_event(1, title_id=uuid.uuid4()))
        await bus.publish(_event(2))
        first = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
        second = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
    assert [first.event.data["seen"], second.event.data["seen"]] == [1, 2]


async def test_an_episode_event_reaches_a_subscriber_scoped_to_its_series() -> None:
    """A client watching a series has the series' title id and no episode
    ids until it fetches a season, so the filter matches on `title_id` and
    an episode event carries both."""
    series = uuid.uuid4()
    bus = InMemoryEventBus()
    async with bus.subscribe(titles=frozenset({series})) as stream:
        await bus.publish(
            ClientEvent(
                kind=ClientEventKind.WATCHSTATE_UPDATED,
                title_id=series,
                episode_id=uuid.uuid4(),
                data={"played": True},
            )
        )
        sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
    assert sent.event.episode_id is not None


async def test_publish_never_suspends_when_a_subscribers_queue_is_full() -> None:
    """**The property this component exists for, decided rather than
    timed.**

    One `send(None)` into the raw coroutine: a `publish` that never awaits
    finishes in that one step and raises `StopIteration`; a `publish` that
    reaches `await queue.put(...)` on a full queue parks and hands back a
    future. No scheduler, no wall clock, no timeout -- so this fails on its
    own assertion in microseconds against the one-line mutation
    (`put_nowait` -> `await put`) that would otherwise deadlock the suite,
    and it cannot be satisfied by a serialised run because it never involves
    two tasks at all.

    The queue is filled first on purpose: `asyncio.Queue.put` on a queue with
    room does not await either, so a case that skipped this step would pass
    against the mutation.
    """
    bus = InMemoryEventBus(queue_size=1)
    async with bus.subscribe():
        assert _one_step(bus.publish(_event(1))), "the first publish should not have parked"
        # The subscriber's queue now holds one event and nobody is reading.
        assert _one_step(bus.publish(_event(2))), (
            "publish parked on a full subscriber queue: an enrichment completing at 04:00 "
            "would hang until a browser tab that closed hours ago is garbage collected"
        )


async def test_publishing_does_not_block_on_a_subscriber_that_is_not_reading() -> None:
    """The same property, measured on overlapping wall-clock intervals.

    "The publish completed" is also what a fully serialised run produces, so
    this records the window during which a subscriber is parked and not
    reading, records the publisher's own window, and asserts the second sits
    inside the first -- reported as intersection-over-union, the shape
    `JobQueueContract.overlapping()` established, because it is the only one
    that tells concurrency from a count.

    Bounded *and* measured: `wait_for` is what turns the blocking mutation
    into a failed case rather than a hung suite, and the IoU assertion is
    what stops a publisher that finished inside the bound but *outside* the
    subscriber's parked window from passing -- which is the "this case
    proves nothing" shape.
    """
    bus = InMemoryEventBus(queue_size=2)
    loop = asyncio.get_running_loop()
    parked: list[float] = []
    published: list[float] = []
    subscribed = asyncio.Event()
    reading = asyncio.Event()
    released = asyncio.Event()

    async def stopped_reader() -> None:
        async with bus.subscribe() as stream:
            iterator = aiter(stream)
            # Before the first `anext`, and it is load-bearing: `create_task`
            # only schedules, so a publish issued before this fires reaches an
            # empty subscriber set and the reader parks on `queue.get()`
            # forever. Measured -- the first draft of this case timed out on
            # its own harness rather than on the bus.
            subscribed.set()
            await anext(iterator)  # take the first, then stop reading entirely
            parked.append(loop.time())
            reading.set()
            await released.wait()
            parked.append(loop.time())

    reader = asyncio.create_task(stopped_reader())
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=_BOUND_SECONDS)
        await bus.publish(_event(0))
        await asyncio.wait_for(reading.wait(), timeout=_BOUND_SECONDS)

        published.append(loop.time())
        await publish_all(
            bus, (_event(index) for index in range(1, _BURST)), timeout=_BOUND_SECONDS
        )
        published.append(loop.time())
    finally:
        released.set()
        await asyncio.wait_for(reader, timeout=_BOUND_SECONDS)

    assert len(parked) == 2 and len(published) == 2
    overlap = min(parked[1], published[1]) - max(parked[0], published[0])
    union = max(parked[1], published[1]) - min(parked[0], published[0])
    assert union > 0.0, "the two windows were both instantaneous; this case measured nothing"
    assert overlap / union > 0.5, (
        f"the publisher ran {published[0]}-{published[1]} and the subscriber was parked "
        f"{parked[0]}-{parked[1]}: {overlap / union:.1%} of their union, so the publish did "
        "not happen while the subscriber was unread and this case proves nothing"
    )


async def test_a_cancelled_subscriber_is_removed() -> None:
    """An SSE client disconnecting is the common case, not the exception --
    a browser tab close, a phone locking, a proxy timing out. A bus that
    kept the queue would grow one per connection for the life of the
    process."""
    bus = InMemoryEventBus()
    async with bus.subscribe():
        assert bus.subscribers == 1
    assert bus.subscribers == 0


async def test_a_subscriber_removed_by_an_exception_is_still_removed() -> None:
    bus = InMemoryEventBus()
    with pytest.raises(ZeroDivisionError):
        async with bus.subscribe():
            raise ZeroDivisionError
    assert bus.subscribers == 0


async def test_an_overflowed_subscriber_gets_exactly_one_resync() -> None:
    """A queue that re-filled behind the resync would hand the client a
    second one the moment it read the first, forever."""
    bus = InMemoryEventBus(queue_size=2)
    async with bus.subscribe() as stream:
        # `publish_all`, never a bare loop: 50 publishes into a queue of two
        # that nobody is reading is precisely where the awaiting spelling
        # deadlocks, and an unbounded burst here turns the milestone's
        # headline mutation from KILLED into HUNG. It did, twice.
        await publish_all(bus, (_event(index) for index in range(50)))
        first = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(aiter(stream)), timeout=0.05)
    assert first.event.kind is ClientEventKind.RESYNC_REQUIRED


async def test_replay_honours_the_subscribers_title_filter() -> None:
    wanted = uuid.uuid4()
    bus = InMemoryEventBus()
    await bus.publish(_event(1, title_id=uuid.uuid4()))
    await bus.publish(_event(2, title_id=wanted))
    async with bus.subscribe(titles=frozenset({wanted}), last_event_id=f"{bus.epoch}-0") as stream:
        sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
    assert sent.event.data == {"seen": 2}


async def test_an_unparseable_last_event_id_is_told_to_resync() -> None:
    """A client sends back whatever it last saw, and a proxy or a bad client
    can mangle it. Raising would answer a reconnect with a 500."""
    bus = InMemoryEventBus()
    async with bus.subscribe(last_event_id="not-an-id") as stream:
        sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
    assert sent.event.kind is ClientEventKind.RESYNC_REQUIRED


async def test_a_last_event_id_at_the_head_replays_nothing_and_waits() -> None:
    """Also the off-by-one case: `>= seen` in place of `> seen` replays the
    very event the client told us it already has, which is a duplicate on
    every reconnect rather than a visible failure."""
    bus = InMemoryEventBus()
    await bus.publish(_event(1))
    async with bus.subscribe(last_event_id=f"{bus.epoch}-1") as stream:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(aiter(stream)), timeout=0.05)


async def test_replay_does_not_redeliver_what_the_queue_already_holds() -> None:
    """**A subscriber's two sources are the same `publish` calls, and the
    plan's shape overlapped them.**

    A replay resolved lazily, at the first `__anext__`, re-reads a ring that
    has meanwhile grown -- and everything published since `subscribe`
    returned is in *both* the ring and this subscriber's queue, so the client
    sees it twice. The window is not theoretical: `api/routers/events.py`
    reaches its first `anext` through an `asyncio.wait_for`, which yields to
    the loop, and the push lane publishes from another task. Resolving the
    replay inside `subscribe`, with no `await` between the snapshot and the
    `add`, is what closes it.
    """
    bus = InMemoryEventBus()
    await bus.publish(_event(1))
    async with bus.subscribe(last_event_id=f"{bus.epoch}-0") as stream:
        await bus.publish(_event(2))
        iterator = aiter(stream)
        first = await asyncio.wait_for(anext(iterator), timeout=1.0)
        second = await asyncio.wait_for(anext(iterator), timeout=1.0)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(iterator), timeout=0.05)
    assert [first.event.data["seen"], second.event.data["seen"]] == [1, 2]


# -- the deferring publisher (ADR-0033) --------------------------------------


async def test_a_deferred_publish_reaches_the_inner_publisher_only_on_the_flush() -> None:
    """The ordering, at the smallest scale that can state it.

    `tests/unit/test_services_jobs.py` asserts the interleaving against a
    real `JobWorker`; this asserts the same property of the object, so a
    worker that stopped calling `flush` and a publisher that delivered
    eagerly are two failures rather than one.
    """
    offered: list[ClientEvent] = []

    class _Counting(NullEventPublisher):
        async def publish(self, event: ClientEvent) -> None:
            offered.append(event)

    deferred = DeferredEventPublisher(_Counting())

    await deferred.publish(_event(1))

    assert deferred.held == 1
    assert offered == [], "the inner publisher was reached before the flush"

    await deferred.flush()

    assert [event.data["seen"] for event in offered] == [1]
    assert deferred.held == 0, "the flush delivered and did not let go"


async def test_a_flush_offers_what_was_held_in_the_order_it_was_raised() -> None:
    """`title.updated` then `watchstate.updated` is a client patching a card
    and then its progress; the reverse is a progress bar on a card that has
    not been rewritten yet. The port promises order *within* one subscriber's
    stream, and a buffer is a place to lose it."""
    bus = InMemoryEventBus()
    deferred = DeferredEventPublisher(bus)
    async with bus.subscribe() as stream:
        for index in range(5):
            await deferred.publish(_event(index))
        await deferred.flush()
        iterator = aiter(stream)
        seen = [
            (await asyncio.wait_for(anext(iterator), timeout=1.0)).event.data["seen"]
            for _ in range(5)
        ]
    assert seen == [0, 1, 2, 3, 4]


async def test_a_discard_offers_nothing_and_a_later_flush_offers_nothing_either() -> None:
    """Both halves, because a `discard` that only marked the buffer would
    pass the first and re-deliver a rolled-back job's frames on the next
    job's commit -- which is the defect, one caller up."""
    bus = InMemoryEventBus()
    deferred = DeferredEventPublisher(bus)
    async with bus.subscribe() as stream:
        await deferred.publish(_event(1))
        assert deferred.held == 1, "nothing was held; the discard below drops nothing"

        deferred.discard()
        await deferred.flush()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(aiter(stream)), timeout=0.05)
    assert deferred.held == 0


async def test_a_flush_that_meets_a_raising_publisher_still_empties_the_buffer() -> None:
    """`publish` never raises is the port's contract, so this is a case about
    an implementation breaking it -- and the caller is `JobWorker`, on a path
    where the job is already complete and committed and there is nothing left
    to undo.

    The second frame is what makes it more than "it did not raise": a flush
    that abandoned the loop on the first failure would hold the rest until
    the next job's commit, which is the same cross-job delivery `discard`
    exists to prevent.
    """
    offered: list[ClientEvent] = []

    class _Breaking(NullEventPublisher):
        async def publish(self, event: ClientEvent) -> None:
            offered.append(event)
            raise RuntimeError("a subscriber transport blew up")

    deferred = DeferredEventPublisher(_Breaking())
    await deferred.publish(_event(1))
    await deferred.publish(_event(2))

    await deferred.flush()

    assert [event.data["seen"] for event in offered] == [1, 2]
    assert deferred.held == 0


async def test_a_deferred_publish_is_not_a_suspension_point_either() -> None:
    """The whole reason `publish` is an `append`.

    Driven one step by hand rather than timed, for the reason the module
    docstring gives: the mutation this rules out -- delivering to the inner
    publisher from `publish` -- would inherit whatever *that* publisher does,
    and the named second implementation of the port is a `LISTEN/NOTIFY`
    transport that genuinely awaits a connection. A buffer in front of it
    that awaited too would put a database round trip inside an enrichment.
    """
    deferred = DeferredEventPublisher(_Suspending())

    assert _one_step(deferred.publish(_event(1)))


async def test_a_flush_with_nothing_held_offers_nothing() -> None:
    """`JobWorker` flushes after every completed job and most handlers
    publish nothing at all -- `match`, `derive`, `index`, `curate` and
    `watch_writeback` are five of the seven kinds. A flush that offered a
    sentinel, or re-offered the last job's frames, would wake every
    subscriber on the box once per claimed job."""
    offered: list[ClientEvent] = []

    class _Counting(NullEventPublisher):
        async def publish(self, event: ClientEvent) -> None:
            offered.append(event)

    deferred = DeferredEventPublisher(_Counting())

    await deferred.flush()

    assert offered == []


class _Suspending(NullEventPublisher):
    """A publisher that parks, standing in for the `LISTEN/NOTIFY` transport
    `ports/events.py` names as the second implementation."""

    async def publish(self, event: ClientEvent) -> None:
        await asyncio.sleep(0)


class TestDeferredEventPublisher(EventPublisherContract):
    """The port's own guarantees, run against the buffer.

    `EventBusContract` is deliberately not inherited: a `DeferredEventPublisher`
    has no subscribers, and a suite it "passed" by having nothing to check
    would ratify a publisher that never delivered -- which is the same
    argument `event_publisher_contract.py` makes for `FakeEventPublisher`.

    The inner publisher is a real bus with a subscriber that never reads
    (below), so the burst case walks the overflow branch on the flush rather
    than being answered by a buffer that swallowed everything.
    """

    @pytest.fixture
    def bus(self) -> InMemoryEventBus:
        return InMemoryEventBus()

    @pytest.fixture
    def publisher(self, bus: InMemoryEventBus) -> DeferredEventPublisher:
        return DeferredEventPublisher(bus)

    @pytest.fixture(autouse=True)
    async def _one_subscriber_that_never_reads(self, bus: InMemoryEventBus) -> AsyncIterator[None]:
        async with bus.subscribe():
            yield

    @pytest.fixture(autouse=True)
    async def _flush_whatever_the_case_held(
        self, publisher: DeferredEventPublisher
    ) -> AsyncIterator[None]:
        """Every contract case ends with a flush, bounded.

        Without it the shared suite measures the `append` and nothing else --
        `test_publish_never_raises_for_a_subscriber_that_cannot_keep_up`
        would be a list of 1,000 items growing, which no implementation can
        fail. With it the burst reaches a real subscriber's full queue and
        the case means what it says on the bus.
        """
        yield
        await asyncio.wait_for(publisher.flush(), timeout=_BOUND_SECONDS)


class TestInMemoryEventBus(EventPublisherContract, EventBusContract):
    @pytest.fixture
    def publisher(self) -> InMemoryEventBus:
        return InMemoryEventBus()

    @pytest.fixture
    def make_bus(self) -> BusFactory:
        return InMemoryEventBus

    @pytest.fixture(autouse=True)
    async def _one_subscriber_that_never_reads(
        self, publisher: InMemoryEventBus
    ) -> AsyncIterator[None]:
        """The shared suite runs against a bus with a subscriber attached and
        nobody reading it.

        Without this it exercises the *empty* bus, which every implementation
        satisfies trivially -- and the guarantee the port states is about a
        subscriber that stopped reading, so the case that says a burst of
        1,000 completes has to be run against one to mean anything. With it,
        `test_publish_never_raises_for_a_subscriber_that_cannot_keep_up`
        really does walk the overflow branch.
        """
        async with publisher.subscribe():
            yield
