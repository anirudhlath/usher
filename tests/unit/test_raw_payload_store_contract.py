"""The shared contract, against the in-memory implementation."""

import pytest

from tests.contract.raw_payload_store_contract import RawPayloadStoreContract
from tests.fakes.raw_payload_store import FakeRawPayloadStore


class TestFakeRawPayloadStore(RawPayloadStoreContract):
    @pytest.fixture
    def store(self) -> FakeRawPayloadStore:
        return FakeRawPayloadStore()
