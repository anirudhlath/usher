"""Behaviour every `CredentialStore` implementation must satisfy.

Deliberately silent about *how* the secret is stored. "Encrypted at rest"
is a property of a persistent store and cannot be asserted against an
in-memory dict, so it is pinned directly against Postgres in
tests/integration/test_credential_store.py (three cases: the raw column is
not the plaintext, a different key cannot read it, and deleting the owning
source removes it). Asserting it here would either force the in-memory fake
to carry a cipher it has no reason to have, or -- worse -- be written so
loosely that both implementations pass it while only one is actually
encrypting.

Subclass and provide a `store` fixture plus an `owner` hook:

    class TestFakeCredentialStore(CredentialStoreContract):
        @pytest.fixture
        def store(self) -> FakeCredentialStore:
            return FakeCredentialStore()

        async def owner(self, store: CredentialStore) -> uuid.UUID:
            return new_id()
"""

import uuid

from pydantic import SecretStr

from usher.ports.credentials import CredentialStore, SourceCredentials

WRONG = SourceCredentials(username="usher", password=SecretStr("wrong-password"))
RIGHT = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))


class CredentialStoreContract:
    async def owner(self, store: CredentialStore) -> uuid.UUID:
        """An id that `put`'s `owner_id` may legitimately reference.

        A hook rather than a plain `new_id()` because a real store may
        enforce referential integrity against it -- the Postgres
        implementation's `source_credentials.source_id` is a foreign key
        with `ON DELETE CASCADE`, so its subclass has to insert a source
        row first.
        """
        raise NotImplementedError

    async def test_put_then_get_round_trips(self, store: CredentialStore) -> None:
        owner = await self.owner(store)
        await store.put("ref-1", RIGHT, owner_id=owner)
        fetched = await store.get("ref-1")
        assert fetched is not None
        assert fetched.username == "usher"
        assert fetched.password.get_secret_value() == "correct-horse-battery"

    async def test_get_returns_none_for_an_unknown_ref(self, store: CredentialStore) -> None:
        assert await store.get("never-stored") is None

    async def test_put_replaces_an_existing_secret(self, store: CredentialStore) -> None:
        """Both re-registering a source with a corrected password and PRD
        08's key rotation land here. A store that inserted instead of
        upserting would raise on the second call, or -- worse -- keep
        serving the old secret."""
        owner = await self.owner(store)
        await store.put("ref-1", WRONG, owner_id=owner)
        await store.put("ref-1", RIGHT, owner_id=owner)
        fetched = await store.get("ref-1")
        assert fetched is not None
        assert fetched.password.get_secret_value() == "correct-horse-battery"

    async def test_refs_are_independent(self, store: CredentialStore) -> None:
        """Rules out a store keyed on the owner rather than the ref, which
        would make PRD 08's rotation (write under a new ref, flip
        `Source.credentials_ref`, delete the old) overwrite the very secret
        it is meant to be replacing."""
        owner = await self.owner(store)
        await store.put("ref-old", WRONG, owner_id=owner)
        await store.put("ref-new", RIGHT, owner_id=owner)
        old = await store.get("ref-old")
        new = await store.get("ref-new")
        assert old is not None and old.password.get_secret_value() == "wrong-password"
        assert new is not None and new.password.get_secret_value() == "correct-horse-battery"

    async def test_delete_removes_the_secret(self, store: CredentialStore) -> None:
        owner = await self.owner(store)
        await store.put("ref-1", RIGHT, owner_id=owner)
        await store.delete("ref-1")
        assert await store.get("ref-1") is None

    async def test_delete_is_idempotent(self, store: CredentialStore) -> None:
        """`DELETE /admin/sources/{id}` removes a source and its credentials
        in two steps; a retry after a partial failure must not fail on the
        step that already succeeded."""
        await store.delete("never-stored")
        owner = await self.owner(store)
        await store.put("ref-1", RIGHT, owner_id=owner)
        await store.delete("ref-1")
        await store.delete("ref-1")
        assert await store.get("ref-1") is None
