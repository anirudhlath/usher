"""Encrypted-at-rest storage for source credentials."""

import base64
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.source import SourceCredentialRow
from usher.ports.credentials import (
    CredentialCiphertextStore,
    CredentialStore,
    SourceCredentials,
)
from usher.ports.errors import PortDataMalformed, RepositoryConflict

_HKDF_INFO = b"usher.source-credentials.v1"


def build_cipher(secret_key: SecretStr) -> Fernet:
    """Derive this deployment's credential-encryption key.

    Module-level and public so a rotation command (PRD 08's "a documented
    rotation command handles the bulk case") can build both the old and the
    new cipher without instantiating two repositories.

    ✅ **That caller exists since M10's K7** and is `cli._rotate`, which makes
    the two calls at the composition root -- `usher.services` may not import
    `usher.db` (`pyproject.toml`'s third contract), so `RotationService` is
    handed two `Fernet` objects and never a key. This function had **no caller
    in `src/` from M3 until then**, and the seam is why the rotation service
    holds no plaintext key: `get_secret_value()` is unwrapped exactly once,
    here, and only the HKDF output outlives the call.
    """
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        secret_key.get_secret_value().encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


class PostgresCredentialStore(CredentialStore):
    def __init__(self, session: AsyncSession, secret_key: SecretStr) -> None:
        self._session = session
        self._cipher = build_cipher(secret_key)

    async def put(self, ref: str, credentials: SourceCredentials, *, owner_id: uuid.UUID) -> None:
        blob = self._cipher.encrypt(
            json.dumps(
                {
                    "username": credentials.username,
                    "password": credentials.password.get_secret_value(),
                }
            ).encode("utf-8")
        )
        now = datetime.now(UTC)
        statement = (
            pg_insert(SourceCredentialRow)
            .values(ref=ref, source_id=owner_id, ciphertext=blob, updated_at=now)
            .on_conflict_do_update(
                index_elements=["ref"],
                set_={"ciphertext": blob, "source_id": owner_id, "updated_at": now},
            )
        )
        # SAVEPOINT rather than session.rollback(), for the reason
        # PostgresTitleRepository's module docstring spells out: the caller
        # owns the transaction, and a full rollback here would discard
        # whatever else it had pending -- which, for the one caller that
        # exists, is the `sources` INSERT this row's foreign key points at.
        try:
            async with self._session.begin_nested():
                await self._session.execute(statement)
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"credentials for ref {ref} could not be stored; the owning source does not exist"
            ) from exc

    async def get(self, ref: str) -> SourceCredentials | None:
        with self._session.no_autoflush:
            row = await self._session.get(SourceCredentialRow, ref)
        if row is None:
            return None
        try:
            payload = self._cipher.decrypt(row.ciphertext)
            record = json.loads(payload.decode("utf-8"))
            return SourceCredentials(
                username=str(record["username"]),
                password=SecretStr(str(record["password"])),
            )
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            # `detail` names the row, never its contents -- PortDataMalformed's
            # own docstring: "It must never carry a credential or a whole
            # payload." `str(exc)` is deliberately not interpolated either;
            # a json decoder's message quotes the text it choked on.
            raise PortDataMalformed(
                "stored source credentials could not be decrypted -- USHER_SECRET_KEY "
                "may have been rotated, or the row corrupted",
                detail=f"credentials_ref={ref}",
            ) from exc

    async def delete(self, ref: str) -> None:
        await self._session.execute(
            delete(SourceCredentialRow).where(SourceCredentialRow.ref == ref)
        )


class PostgresCredentialRotationStore(CredentialCiphertextStore):
    """`source_credentials`' ciphertext, moved without being read."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_refs(self) -> Sequence[str]:
        # `ORDER BY ref` so a report, a rerun and an interrupted run all name
        # the rows in the same order. `ref` is the primary key, so this is
        # the index order rather than a sort.
        rows = await self._session.execute(
            select(SourceCredentialRow.ref).order_by(SourceCredentialRow.ref)
        )
        return list(rows.scalars().all())

    async def read_ciphertext(self, ref: str) -> bytes | None:
        # A one-column projection rather than `session.get`, which would put
        # a `SourceCredentialRow` in the identity map for the length of a
        # rotation -- and `db-and-sql.md`'s issue #8 entry is what a caught
        # conflict does to one of those. Nothing here needs the entity.
        row = await self._session.execute(
            select(SourceCredentialRow.ciphertext).where(SourceCredentialRow.ref == ref)
        )
        return row.scalar_one_or_none()

    async def write_ciphertext(self, ref: str, ciphertext: bytes) -> None:
        # `updated_at` is set here because `source_credentials` carries no
        # `set_updated_at` trigger -- see `SourceCredentialRow`'s docstring, which named
        # `PostgresCredentialStore` as the table's only writer until this class became
        # the second one.
        await self._session.execute(
            update(SourceCredentialRow)
            .where(SourceCredentialRow.ref == ref)
            .values(ciphertext=ciphertext, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
