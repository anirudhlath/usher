"""What every `EventPublisher` must guarantee to the services that call it.

Subclass and provide a `publisher` fixture:

    class TestFakeEventPublisher(EventPublisherContract):
        @pytest.fixture
        def publisher(self) -> EventPublisher:
            return FakeEventPublisher()

The subscriber-facing cases live in `EventBusContract` below, which
`InMemoryEventBus` runs and `FakeEventPublisher` does not -- a spy has no
subscribers, and a suite it "passed" by having nothing to check would ratify
a bus that never delivered.

**Neither class asserts that `publish` never *suspends*.** It is async
because a transport can be, and the named second implementation (a Postgres
`LISTEN/NOTIFY` bus, ADR-0019's seam) genuinely would await a connection.
What every implementation owes is that a subscriber which stopped reading
cannot slow, block or fail the publisher -- which is a statement about the
*subscriber*, not about the transport. `tests/unit/test_services_events.py`
pins the stronger, in-memory-only property (`publish` completes in one
event-loop step) where it belongs, on the one implementation that can make
it.
"""

import asyncio

from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher

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

        async def burst() -> None:
            for index in range(_BURST):
                await publisher.publish(
                    ClientEvent(kind=ClientEventKind.SYNC_PROGRESS, data={"seen": index})
                )

        await asyncio.wait_for(burst(), timeout=_NOT_BLOCKING_SECONDS)

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
