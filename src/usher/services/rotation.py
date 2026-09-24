"""Rotate `USHER_SECRET_KEY`: two ciphers, one row at a time, no ledger."""

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
