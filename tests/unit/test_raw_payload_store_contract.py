"""The shared contract, against the in-memory implementation.

Half of a pair. This one stores Python objects rather than JSONB, so
`test_a_payload_survives_nesting_and_nulls` proves the assertion is
expressible and `tests/integration/test_raw_payload_store.py` proves it
survives a real serialise/deserialise round trip.
"""

import pytest

from tests.contract.raw_payload_store_contract import RawPayloadStoreContract
from tests.fakes.raw_payload_store import FakeRawPayloadStore


class TestFakeRawPayloadStore(RawPayloadStoreContract):
    @pytest.fixture
    def store(self) -> FakeRawPayloadStore:
        return FakeRawPayloadStore()
