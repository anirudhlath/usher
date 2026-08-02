"""`EventPublisherContract` against the two publishers that fan out to
nobody.

`FakeEventPublisher` records; `NullEventPublisher` discards. Both are the
degenerate end of the port, and running the shared suite against them is
what keeps the contract satisfiable by something other than the bus -- PRD
08's `usher work` as a standalone process publishes nowhere, and that has to
be a real implementation rather than a `None` check in three services.

`InMemoryEventBus` runs this same suite plus `EventBusContract` in
`tests/unit/test_services_events.py`.
"""

import pytest

from tests.contract.event_publisher_contract import EventPublisherContract
from tests.fakes.event_publisher import FakeEventPublisher
from usher.ports.events import EventPublisher, NullEventPublisher


class TestFakeEventPublisher(EventPublisherContract):
    @pytest.fixture
    def publisher(self) -> EventPublisher:
        return FakeEventPublisher()


class TestNullEventPublisher(EventPublisherContract):
    @pytest.fixture
    def publisher(self) -> EventPublisher:
        return NullEventPublisher()
