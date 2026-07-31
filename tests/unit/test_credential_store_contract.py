"""The credential contract against the in-memory double. No Docker.

tests/integration/test_credential_store.py runs the identical assertions
against Postgres, plus the three at-rest encryption cases this fake has no
way to satisfy.
"""

import uuid

import pytest

from tests.contract.credential_store_contract import CredentialStoreContract
from tests.fakes.credential_store import FakeCredentialStore
from usher.domain.ids import new_id
from usher.ports.credentials import CredentialStore


class TestFakeCredentialStore(CredentialStoreContract):
    @pytest.fixture
    def store(self) -> FakeCredentialStore:
        return FakeCredentialStore()

    async def owner(self, store: CredentialStore) -> uuid.UUID:
        return new_id()
