"""Sources -- the configured media servers Usher syncs from.

Implemented by `usher.db.repositories.source.PostgresSourceRepository`.
"""

import uuid
from abc import ABC, abstractmethod

from usher.domain.source import Source

__all__ = [
    "SourceRepository",
]


class SourceRepository(ABC):
    """Persistence for configured sources.

    Same session/transaction ownership as `TitleRepository`: every method
    flushes so conflicts surface immediately, none commits.

    Credentials are deliberately absent from this port. `Source` carries
    only `credentials_ref`, an opaque pointer, and the secret itself lives
    behind `CredentialStore` (`usher.ports.credentials`) -- so a read here,
    which is what the admin API performs, cannot return a credential even
    by accident. That split is PRD 08's "credentials are never returned by
    any API, including admin", expressed as a type rather than as a rule.
    """

    @abstractmethod
    async def add(self, source: Source) -> None:
        """Insert. A duplicate id raises `RepositoryConflict`."""

    @abstractmethod
    async def update(self, source: Source) -> None:
        """Update an existing row. An unknown id raises
        `RepositoryNotFound`. Writes every mutable column it is given,
        including `device_id` -- PRD 08's key/credential rotation and a
        deliberate device rotation both go through here."""

    @abstractmethod
    async def get(self, source_id: uuid.UUID) -> Source | None:
        """Fetch by id, or None."""

    @abstractmethod
    async def list_all(self) -> list[Source]:
        """Every configured source, ordered by name. Includes disabled ones:
        `GET /admin/sources` has to show a source in order for an operator
        to re-enable it."""

    @abstractmethod
    async def delete(self, source_id: uuid.UUID) -> bool:
        """Remove a source. Returns whether a row was actually removed, so
        `DELETE /admin/sources/{id}` can answer 404 rather than claiming to
        have deleted something that never existed. Idempotent."""
