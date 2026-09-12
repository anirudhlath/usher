"""A recording `EventPublisher`."""

from usher.ports.events import ClientEvent, EventPublisher


class FakeEventPublisher(EventPublisher):
    def __init__(self) -> None:
        self.published: list[ClientEvent] = []

    async def publish(self, event: ClientEvent) -> None:
        self.published.append(event)
