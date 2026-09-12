"""`EventPublisherContract` against the two publishers that fan out to nobody."""

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
