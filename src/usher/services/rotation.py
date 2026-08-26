"""Rotate `USHER_SECRET_KEY`: two ciphers, one row at a time, no ledger.

`usher rotate-secret --new-key-env <VAR>` re-encrypts every
`source_credentials` row from the cipher the current key derives to the one a
new key derives. `usher.db.repositories.credentials` owns the survey of what
that key protects and the measurement of how big the table is (one row per
configured source; **1 row and 48 kB** on the deployment this project runs,
2026-08-25). What this module owns is the *order of operations*, and every
one of them is a decision about what an interrupted run leaves behind.

## Two ciphers and no plaintext key

`build_cipher` was made public for exactly this -- its docstring says so,
*"so a rotation command can build both the old and the new cipher without
instantiating two repositories"* -- and it has had no caller in `src/` since
M3. This service is that first caller, reached through its constructor: it is
handed two `Fernet` objects and never a `SecretStr`, because `build_cipher`
unwraps `get_secret_value()` exactly once inside itself and retains only the
HKDF output. That is the property that makes holding two ciphers at once
safe, and it is why `RotationService` cannot build them itself even if the
layering allowed it (`pyproject.toml`'s third import contract: `usher.db` is
driven, not driving, so the composition root does the two calls).

The plaintext credential exists in this module for the length of one
statement -- `_plaintext` hands it to `encrypt` and the name goes out of
scope with the loop body. It is never returned, never counted, never
formatted, and `RotationReport` carries **refs only**, which is the same line
`PortDataMalformed` draws in `db/repositories/credentials.py`: name the row,
never its contents.

## Per-row commit, which is the opposite of `usher restore` and deliberate

`services/restore.py` does the whole file in one transaction with one commit
at the end, and says why: an unresolved reference in the last row has to roll
back the first. Rotation takes the other side of that trade, for a reason
that is about the *operator's next move* rather than about consistency.

A single transaction over N rows means an interrupted rotation leaves **every
row on the old key**, while the operator -- who ran this command precisely
because they were changing keys -- has already put the new key in their
`.env`. The next start then cannot read a single credential. Per-row commit
leaves a **mixed** state instead, and a mixed state is recoverable:

- **It is diagnosable rather than silent.** Fernet's authentication tag makes
  a wrong key an `InvalidToken`, which `PostgresCredentialStore.get` turns
  into `PortDataMalformed` naming the ref, and
  `GET /admin/sources/{id}/status` already renders that as an unreachable,
  unauthenticated source with a re-enter-your-credentials detail. A
  half-rotated deployment degrades into the exact screen it was designed to
  degrade into.
- **Re-running is the recovery, and it needs no ledger.** Every row is tried
  with the **new** cipher first and skipped if it already opens; only then is
  the old one tried. So a second run over a half-rotated table finishes it,
  and a run over a fully-rotated table is a no-op reporting *N already
  rotated*. The resumption story is a decrypt attempt, and there is no state
  to keep. Trying the old cipher first would be correct on a fresh table and
  would **double-encrypt** every row a previous run had already moved --
  still readable, so nothing would notice until the run after that.
- **A row that opens under neither key is refused, named and counted**, and
  the command exits 1 having rotated the rest. That row is the one an
  operator has to re-enter, and it must not be hidden behind a success. It is
  also left exactly as it was: writing anything onto it would destroy the one
  copy a restored key could still have read.

⚠️ `PortDataMalformed` is **not** in `cli.OPERATOR_ERRORS` and this module
does not put it there. A row no key opens never becomes an exception at all
here -- `read_ciphertext` does not decrypt, so the failure is a `None` from
`_plaintext` and a counted refusal. That is the per-command *handling*
ADR-0026 permits, as distinct from the per-command *boundary* it rejects.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from usher.ports.credentials import CredentialCiphertextStore

__all__ = ["RotationReport", "RotationService"]


@dataclass(frozen=True)
class RotationReport:
    """What one rotation did, by ref.

    Three tuples rather than three counts, because the refused list is the
    only actionable half -- an operator re-enters those credentials -- and a
    count cannot say which. Refs and never credentials: a `credentials_ref`
    is an opaque token this project minted, which is what makes it safe to
    print (`ports/credentials.py`'s own argument for it being opaque).
    """

    rotated: tuple[str, ...]
    already: tuple[str, ...]
    refused: tuple[str, ...]

    @property
    def rows(self) -> int:
        return len(self.rotated) + len(self.already) + len(self.refused)


def _plaintext(cipher: Fernet, ciphertext: bytes) -> bytes | None:
    """What this cipher opens, or `None` if it is not this cipher's token.

    `InvalidToken` and nothing wider. `PostgresCredentialStore.get` catches
    `ValueError`, `KeyError` and `TypeError` beside it because it goes on to
    `json.loads` the result and build a `SourceCredentials`; this does not
    parse the plaintext at all, so those cannot arise. Rotation moving opaque
    bytes is deliberate twice over: it means a payload shape change needs no
    change here, and it means the credential is never structured, never
    indexed and never anywhere a formatter could reach it.
    """
    try:
        return cipher.decrypt(ciphertext)
    except InvalidToken:
        return None


class RotationService:
    def __init__(
        self,
        *,
        store: CredentialCiphertextStore,
        old_cipher: Fernet,
        new_cipher: Fernet,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        self._store = store
        self._old_cipher = old_cipher
        self._new_cipher = new_cipher
        self._commit = commit

    async def rotate(self) -> RotationReport:
        """Move every row this store holds onto the new cipher.

        The row is re-read inside the loop rather than fetched in the listing:
        `list_refs` and each read are separated by a commit, so a source
        deleted through the admin API part-way through a run is reachable, and
        it is an absence rather than a row whose key was lost.
        """
        rotated: list[str] = []
        already: list[str] = []
        refused: list[str] = []
        for ref in await self._store.list_refs():
            ciphertext = await self._store.read_ciphertext(ref)
            if ciphertext is None:
                continue
            if _plaintext(self._new_cipher, ciphertext) is not None:
                already.append(ref)
                continue
            plaintext = _plaintext(self._old_cipher, ciphertext)
            if plaintext is None:
                refused.append(ref)
                continue
            await self._store.write_ciphertext(ref, self._new_cipher.encrypt(plaintext))
            await self._commit()
            rotated.append(ref)
        return RotationReport(
            rotated=tuple(rotated), already=tuple(already), refused=tuple(refused)
        )
