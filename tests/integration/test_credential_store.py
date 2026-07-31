"""PostgresCredentialStore against real Postgres.

The contract suite runs here unchanged; the four cases below are the ones
the in-memory fake cannot express, and they are the ones PRD 08's rules
actually reduce to.
"""

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.credential_store_contract import RIGHT, CredentialStoreContract
from usher.db.models.source import SourceCredentialRow, SourceRow
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.ports.credentials import CredentialStore
from usher.ports.errors import PortDataMalformed, RepositoryConflict

KEY = SecretStr("0" * 32)
OTHER_KEY = SecretStr("1" * 32)


async def _seed_source(session: AsyncSession) -> uuid.UUID:
    source_id = new_id()
    session.add(
        SourceRow(
            id=source_id,
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-1",
            device_id=str(new_id()),
        )
    )
    await session.flush()
    return source_id


class TestPostgresCredentialStoreContract(CredentialStoreContract):
    @pytest.fixture
    def store(self, session: AsyncSession) -> PostgresCredentialStore:
        return PostgresCredentialStore(session, KEY)

    async def owner(self, store: CredentialStore) -> uuid.UUID:
        # Reaches into the store's own session rather than taking a second
        # `session` fixture argument: the credential row's foreign key must
        # point at a source visible in *this* store's transaction, and two
        # sessions on the same connection would not see each other's
        # unflushed work. (`flake8-self`/SLF is not in this project's ruff
        # selection, so no suppression is needed.)
        assert isinstance(store, PostgresCredentialStore)
        return await _seed_source(store._session)


async def test_the_stored_column_is_not_the_plaintext(session: AsyncSession) -> None:
    """PRD 08's whole point. Reads the raw column rather than going through
    `get`, because `get` decrypts -- a store that "encrypted" by base64ing
    would satisfy a round-trip test and fail this one."""
    owner = await _seed_source(session)
    await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=owner)
    await session.flush()
    stored = (
        await session.execute(
            select(SourceCredentialRow.ciphertext).where(SourceCredentialRow.ref == "ref-1")
        )
    ).scalar_one()
    assert b"correct-horse-battery" not in stored
    assert b"usher" not in stored


async def test_a_different_secret_key_cannot_read_it(session: AsyncSession) -> None:
    """Rotating USHER_SECRET_KEY must be a loud, diagnosable failure that
    names the row, not a silent garbage read and not a `None` that would
    look like an unconfigured source."""
    owner = await _seed_source(session)
    await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=owner)
    await session.flush()
    with pytest.raises(PortDataMalformed) as exc_info:
        await PostgresCredentialStore(session, OTHER_KEY).get("ref-1")
    assert exc_info.value.detail == "credentials_ref=ref-1"
    assert "correct-horse-battery" not in str(exc_info.value)


async def test_deleting_the_source_cascades_to_its_credentials(session: AsyncSession) -> None:
    """The reason `owner_id` is on the port at all. Without the cascade, a
    crash between "delete the credential" and "delete the source" leaves an
    encrypted row nothing can attribute or clean up."""
    owner = await _seed_source(session)
    await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=owner)
    await session.flush()
    await session.execute(delete(SourceRow).where(SourceRow.id == owner))
    await session.flush()
    remaining = (
        (
            await session.execute(
                select(SourceCredentialRow.ref).where(SourceCredentialRow.ref == "ref-1")
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


async def test_put_for_an_unknown_owner_is_a_port_error(session: AsyncSession) -> None:
    """A raw sqlalchemy.exc.IntegrityError escaping here would break the
    "db is driven, not driving" contract exactly the way it did in
    PostgresTitleRepository before its translation was added."""
    with pytest.raises(RepositoryConflict):
        await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=new_id())


async def test_the_session_survives_that_conflict(session: AsyncSession) -> None:
    """The SAVEPOINT, not just the translation. `SourceService.register`
    inserts the source and then the credential on one session; if a failed
    `put` poisoned the transaction, the caller's rollback path could not
    even read back what it had already written."""
    store = PostgresCredentialStore(session, KEY)
    with pytest.raises(RepositoryConflict):
        await store.put("ref-1", RIGHT, owner_id=new_id())
    owner = await _seed_source(session)
    await store.put("ref-2", RIGHT, owner_id=owner)
    assert await store.get("ref-2") is not None
