"""A recording `EventPublisher`.

**Where this is more forgiving than the real bus, and it is nearly
everything.** It records and never fans out, so no queue, no bound, no
overflow, no ordering across subscribers, no replay and no cancellation is
exercised by it at all -- it is a spy for services, not a bus. It also never
blocks, which means a service tested only against this would pass even if
the real bus stalled its caller.

What closes that gap is the in-memory bus running the identical
`EventPublisherContract` (M5's SSE task), with the non-blocking case
asserted on measured, overlapping intervals rather than on a completion --
a bare recorder never truly awaits, so the event loop runs each gathered
task through its whole cycle before starting the next and a serialised run
produces the identical recording.
"""

from usher.ports.events import ClientEvent, EventPublisher


class FakeEventPublisher(EventPublisher):
    def __init__(self) -> None:
        self.published: list[ClientEvent] = []

    async def publish(self, event: ClientEvent) -> None:
        self.published.append(event)
