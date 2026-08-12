"""What every `EventPublisher` must guarantee to the services that call it.

Subclass and provide a `publisher` fixture:

    class TestFakeEventPublisher(EventPublisherContract):
        @pytest.fixture
        def publisher(self) -> EventPublisher:
            return FakeEventPublisher()

The subscriber-facing cases live in `EventBusContract` below, which
`InMemoryEventBus` runs and neither `FakeEventPublisher` nor
`DeferredEventPublisher` does -- a spy has no subscribers and a buffer has
none either, and a suite one of them "passed" by having nothing to check
would ratify a bus that never delivered.

**`DeferredEventPublisher` runs this class and needs one thing arranged for
it to mean anything**: its `publish` is an `append`, so every case here is
satisfied trivially unless something drains it. `tests/unit/test_services_events.py`
gives it an autouse fixture that flushes into a real bus with a subscriber
that never reads, bounded -- which is what puts the burst case back on the
overflow branch it was written for. A contract suite run against a component
that buffers is measuring the buffer until the drain is wired in.

**Neither class asserts that `publish` never *suspends*.** It is async
because a transport can be, and the named second implementation -- a Postgres
`LISTEN/NOTIFY` bus, for a deployment that splits the worker from the server
-- genuinely would await a connection.
What every implementation owes is that a subscriber which stopped reading
cannot slow, block or fail the publisher -- which is a statement about the
*subscriber*, not about the transport. `tests/unit/test_services_events.py`
pins the stronger, in-memory-only property (`publish` completes in one
event-loop step) where it belongs, on the one implementation that can make
it.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.services.events import SentEvent

# Larger than any plausible per-subscriber buffer, so a bus that queued
# unboundedly and a bus that overflows-and-diverts are both walked past
# their branch. `InMemoryEventBus`'s default queue is 64.
_BURST = 1_000

# The whole burst above, on an implementation that does not await a
# subscriber, is thousands of dictionary operations -- microseconds. A
# second is three orders of magnitude of headroom for a loaded CI box, and
# still nothing next to the "until a browser tab is garbage collected" this
# case exists to rule out.
_NOT_BLOCKING_SECONDS = 1.0


class EventPublisherContract:
    async def test_publish_accepts_an_event_with_no_subscribers(
        self, publisher: EventPublisher
    ) -> None:
        """The normal state of a household's server. `EnrichService`
        finishing a title at 04:00 must not care that nobody is watching."""
        await publisher.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED))

    async def test_publish_never_raises_for_a_subscriber_that_cannot_keep_up(
        self, publisher: EventPublisher
    ) -> None:
        """A browser tab that stopped reading must not fail an enrichment.

        Nothing here can *arrange* a slow subscriber through the port -- the
        port has no `subscribe` -- so this asserts the weaker, universally
        checkable half: a burst larger than any plausible buffer completes.
        `EventBusContract` asserts the real thing.

        **Bounded, and it was not at first.** Run unbounded against the
        mutation this whole file exists for -- `await queue.put(...)` for
        `put_nowait` -- it does not fail, it *deadlocks*: the burst fills the
        subscriber's queue on publish 65 of 1,000 and nothing will ever read
        it. The sweep recorded HUNG rather than KILLED, which is a mutation
        no case observed rather than one every case caught.
        """

        await publish_all(publisher, (_progress(index) for index in range(_BURST)))

    async def test_publish_is_not_a_suspension_point_a_caller_can_be_starved_on(
        self, publisher: EventPublisher
    ) -> None:
        """Bounded *and* measured, because the failure this rules out is a
        block rather than a wrong answer.

        `asyncio.wait_for` is what makes a blocking implementation fail this
        case instead of hanging the suite; the elapsed-window assertion is
        what makes an implementation that merely *dawdles* fail it too, since
        an outer bound alone is satisfied by anything that finishes inside
        the bound. Both, or the case only rules out one of the two shapes.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(
            publisher.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED)),
            timeout=_NOT_BLOCKING_SECONDS,
        )
        elapsed = loop.time() - started
        assert elapsed < _NOT_BLOCKING_SECONDS / 2, (
            f"one publish took {elapsed:.3f}s; a publisher that waits on a subscriber is "
            "the failure this port's docstring forbids"
        )


class Publishing(Protocol):
    """Anything with a `publish`, so `publish_all` serves both contracts.

    A `Protocol` for the reason `SubscribingPublisher` below states at
    length, plus one more: `EventPublisher` is an ABC and a Protocol accepts
    it structurally, where the reverse is not true.
    """

    async def publish(self, event: ClientEvent) -> None: ...


async def publish_all(
    publisher: Publishing,
    events: Iterable[ClientEvent],
    *,
    timeout: float = _NOT_BLOCKING_SECONDS,
) -> None:
    """A burst of publishes, bounded.

    **Every burst in this suite goes through here, and that is structural
    rather than tidy.** The one-line mutation these files exist to catch --
    `await queue.put(...)` for `put_nowait` -- does not answer wrongly, it
    *deadlocks*: the burst fills an unread subscriber's queue and nothing
    will ever drain it. An unbounded burst therefore turns that mutation from
    KILLED into HUNG, which reads like a mutation nothing observed. Measured
    twice on this milestone, in two different files, which is why this is a
    helper instead of a convention.
    """

    async def burst() -> None:
        for event in events:
            await publisher.publish(event)

    await asyncio.wait_for(burst(), timeout=timeout)


class SubscribingPublisher(Protocol):
    """The shape `EventBusContract` is written against.

    **A `Protocol`, and `EventPublisher` is an `ABC`.** Not ADR-0001 being
    ignored, for the reason `usher.adapters.emby.push.SessionLike` already
    states one package over: ADR-0001 governs *ports*, and this is neither a
    port nor in `src/` at all. It cannot be an ABC: subscription is
    deliberately absent from `EventPublisher` (a `LISTEN/NOTIFY`
    implementation subscribes on a dedicated connection whose lifecycle has
    nothing in common with an in-memory queue's), and a second ABC bolted on
    here would put back exactly what that decision removed -- while living in
    `tests/`, which `src/` may not import from and therefore may not inherit
    from either.
    """

    @property
    def epoch(self) -> str: ...

    async def publish(self, event: ClientEvent) -> None: ...

    def subscribe(
        self,
        *,
        titles: frozenset[uuid.UUID] | None = ...,
        last_event_id: str | None = ...,
    ) -> AbstractAsyncContextManager[AsyncIterator[SentEvent]]: ...


# Sizes are constructor arguments rather than fixed, because three of the
# four cases below are *about* a bound being reached and a suite that had to
# publish 64 events to reach one would be measuring patience.
BusFactory = Callable[..., SubscribingPublisher]


class EventBusContract:
    """What an `EventPublisher` that *also* offers subscription must
    guarantee to one client's stream.

    Separate from `EventPublisherContract` because `FakeEventPublisher` has
    no subscribers, and a suite it "passed" by having nothing to check would
    ratify a bus that never delivered. Subclass and provide a `make_bus`
    fixture.

    Every case here is about a **single** subscriber's stream, deliberately:
    that is the guarantee a Postgres `LISTEN/NOTIFY` transport could also
    make, and a contract drawn around an in-process queue's ordering across
    subscribers would be a contract only this implementation can satisfy.
    """

    async def test_a_subscriber_that_overflows_is_told_to_resync(
        self, make_bus: BusFactory
    ) -> None:
        """PRD 07's exact requirement. Dropping events silently leaves a
        client confidently stale, which is worse than telling it to refetch:
        it has no way to find out."""
        bus = make_bus(queue_size=3)
        async with bus.subscribe() as stream:
            await publish_all(bus, (_progress(index) for index in range(10)))
            sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
        assert sent.event.kind is ClientEventKind.RESYNC_REQUIRED
        assert sent.event.data == {"reason": "buffer_overflow"}

    async def test_replay_resumes_after_the_last_event_the_client_saw(
        self, make_bus: BusFactory
    ) -> None:
        """The reconnect PRD 07 designed for. Without it a client that
        dropped its connection for two seconds during a walk loses whatever
        landed in them, with nothing to say so."""
        bus = make_bus()
        for index in (1, 2, 3):
            await bus.publish(_progress(index))
        async with bus.subscribe(last_event_id=f"{bus.epoch}-1") as stream:
            iterator = aiter(stream)
            second = await asyncio.wait_for(anext(iterator), timeout=1.0)
            third = await asyncio.wait_for(anext(iterator), timeout=1.0)
        assert [second.event.data["seen"], third.event.data["seen"]] == [2, 3]

    async def test_a_last_event_id_older_than_the_buffer_is_told_to_resync(
        self, make_bus: BusFactory
    ) -> None:
        """Replaying whatever is still in the ring and calling it a resume is
        the failure: the client silently misses the events that fell off the
        front and has no way to learn it."""
        bus = make_bus(buffer_size=3)
        await publish_all(bus, (_progress(index) for index in range(10)))
        async with bus.subscribe(last_event_id=f"{bus.epoch}-1") as stream:
            sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
        assert sent.event.kind is ClientEventKind.RESYNC_REQUIRED
        assert sent.event.data == {"reason": "buffer_expired"}

    async def test_a_last_event_id_from_a_previous_process_is_told_to_resync(
        self, make_bus: BusFactory
    ) -> None:
        """**The one that is impossible without the epoch.** The ring is
        in-memory, so ids restart at 1 with the process. A client
        reconnecting with `Last-Event-ID: 40` after a restart would be
        replayed events 41+ of a completely different sequence -- a
        plausible-looking stream that is silently wrong."""
        bus = make_bus()
        await bus.publish(_progress(1))
        async with bus.subscribe(last_event_id="deadbeef-40") as stream:
            sent = await asyncio.wait_for(anext(aiter(stream)), timeout=1.0)
        assert sent.event.kind is ClientEventKind.RESYNC_REQUIRED
        assert sent.event.data == {"reason": "unknown_epoch"}


def _progress(index: int) -> ClientEvent:
    return ClientEvent(kind=ClientEventKind.SYNC_PROGRESS, data={"seen": index})
