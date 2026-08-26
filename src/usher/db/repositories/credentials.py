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
    """`source_credentials`' ciphertext, moved without being read.

    **This is the whole of what `USHER_SECRET_KEY` protects that is
    persisted, and saying the size out loud is what stops the command that
    uses it being over-built.** There are exactly two HKDF derivations over
    that key: this module's `build_cipher`
    (`info=b"usher.source-credentials.v1"`), whose output encrypts the JSON
    `{username, password}` blob in the column below, and
    `usher.services.playback_ticket.build_ticket_cipher`
    (`info=b"usher.playback-ticket.v1"`), whose output encrypts a playback
    target URL inside a ticket that is **never stored**. So rotation is one
    table, one row per configured source -- measured 2026-08-25 on the
    deployment this project runs: `SELECT count(*) FROM source_credentials`
    is **1** and the table is **48 kB**.

    ⚠️ Two neighbours that look like they belong here and do not.
    `api/cursor.py` records that `Settings.secret_key` is *deliberately not*
    what signs a keyset cursor, so cursors are outside this entirely --
    checked rather than assumed, a cursor being the other opaque string in
    this API. And the ticket cipher needs no rotation, which is a fact about
    a ticket's lifetime rather than an omission: rotating the key invalidates
    every outstanding one, which renders as a `404 ticket_invalid` the client
    answers by asking `/play` again. **"Short-lived" is five minutes** --
    `api.routers.playback.TICKET_TTL_SECONDS`, verified 2026-08-26. It lives
    at the route rather than in this subsystem because
    `services/playback_ticket.py` says in as many words that *no TTL constant
    lives here* (`redeem`'s `ttl_seconds` is required with no default), and it
    is deliberately **not** `USHER_PLAYBACK_TICKET_TTL_SECONDS`: that name
    appears in both modules only as the setting PRD 08's
    mechanism-before-the-setting rule refused, and `Settings` has no such
    field.

    **No `secret_key`, and the absent constructor argument is the design.**
    `PostgresCredentialStore` takes one because it decrypts; this class moves
    bytes it cannot open, so an instance of it is not a thing that can leak a
    credential even if a later caller misuses it.
    """

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
        # `set_updated_at` trigger -- see `SourceCredentialRow`'s docstring,
        # which named `PostgresCredentialStore` as the table's only writer
        # until this class became the second one. A rotation that left the
        # column alone would make the stamp say when the *credential* last
        # changed, which is not what any other table in this schema means by
        # it and not what an operator diagnosing a half-rotated deployment
        # needs.
        #
        # `synchronize_session=False` because nothing above this call holds a
        # `SourceCredentialRow`: the read is a projection, so there is no
        # identity map for the ORM to reconcile.
        await self._session.execute(
            update(SourceCredentialRow)
            .where(SourceCredentialRow.ref == ref)
            .values(ciphertext=ciphertext, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
