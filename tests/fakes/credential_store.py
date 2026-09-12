"""In-memory CredentialStore."""

import uuid
from dataclasses import dataclass

from usher.ports.credentials import CredentialStore, SourceCredentials


@dataclass(frozen=True)
class _Entry:
    credentials: SourceCredentials
    owner_id: uuid.UUID


class FakeCredentialStore(CredentialStore):
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    async def put(self, ref: str, credentials: SourceCredentials, *, owner_id: uuid.UUID) -> None:
        self._entries[ref] = _Entry(credentials=credentials, owner_id=owner_id)

    async def get(self, ref: str) -> SourceCredentials | None:
        entry = self._entries.get(ref)
        return None if entry is None else entry.credentials

    async def delete(self, ref: str) -> None:
        self._entries.pop(ref, None)

    def owner_of(self, ref: str) -> uuid.UUID | None:
        """Test-only probe. Not part of the port -- nothing in `src/` reads
        an owner back, because `owner_id` exists solely so a real backing
        store can cascade a delete."""
        entry = self._entries.get(ref)
        return None if entry is None else entry.owner_id
