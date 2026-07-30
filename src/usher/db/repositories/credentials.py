"""Encrypted-at-rest storage for source credentials.

PRD 08: credentials are encrypted using a key supplied via
`USHER_SECRET_KEY`, `Source.credentials_ref` points at the encrypted row,
and the plaintext exists only in memory in the adapter that needs it.

Fernet (AES-128-CBC with an HMAC-SHA256 authentication tag) over a key
derived from `USHER_SECRET_KEY` with HKDF-SHA256. HKDF rather than a
password-based KDF such as scrypt because the input is already
high-entropy: the documented way to produce this value is
`openssl rand -hex 32`, `Settings.secret_key` enforces `min_length=32`, and
`Settings` rejects the example placeholder outright. HKDF is the primitive
designed for deriving subkeys from an existing strong secret; scrypt's work
factor buys nothing against 32 random bytes and would cost a full KDF run
per call.

The `info` string is versioned so a future scheme change becomes a new
derivation rather than a silent reinterpretation of old ciphertext, and so
this subkey is domain-separated from any other use a later milestone makes
of `USHER_SECRET_KEY`.

The authentication tag is what makes a rotated key a *diagnosable* failure
rather than a garbage read: decrypting with the wrong key raises
`InvalidToken`, which becomes `PortDataMalformed` with the ref (never the
payload, never the key) so an operator can find the row and re-enter the
credential.

`SecretStr.get_secret_value()` is unwrapped exactly once, in `__init__`,
and the plaintext secret is not retained -- only the derived Fernet key,
which is an HKDF output and not the secret. That satisfies CLAUDE.md's
"never store the unwrapped value in a variable that outlives that call",
and re-deriving per call would be strictly worse for no benefit.
"""

import base64
import json
import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.source import SourceCredentialRow
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.errors import PortDataMalformed, RepositoryConflict

_HKDF_INFO = b"usher.source-credentials.v1"


def build_cipher(secret_key: SecretStr) -> Fernet:
    """Derive this deployment's credential-encryption key.

    Module-level and public so a rotation command (PRD 08's "a documented
    rotation command handles the bulk case") can build both the old and the
    new cipher without instantiating two repositories.
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
