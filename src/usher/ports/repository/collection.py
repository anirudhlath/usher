"""Collections -- the port for a re-derived franchise or box set."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from usher.domain.collection import Collection
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "CollectionRepository",
    "OwnedCollection",
]


@dataclass(frozen=True, slots=True)
class OwnedCollection:
    """A franchise and the household's coverage of it.

    Lists, not counts: the franchise signal is "you own 2 of 4", which needs
    two numbers *and* the cards to render, and storing a count beside the
    list would let the two disagree into a state no consumer can interpret.

    `title_ids` is every member in release order, `owned_title_ids` the
    subset with an available media item. The difference is the completeness
    signal, and it is what a genre row cannot say.
    """

    collection_id: uuid.UUID
    name: str
    title_ids: tuple[uuid.UUID, ...]
    owned_title_ids: frozenset[uuid.UUID]


class CollectionRepository(ABC):
    """Persistence for TMDb's movie franchise grouping, and the writer of `titles.collection_id`.

    Movies only, said here rather than discovered by a provider:
    `belongs_to_collection` is a field of `/movie/{id}` with no `/tv/{id}`
    counterpart. On a television-only household a franchise row is therefore
    unsatisfiable by construction rather than by absence of data -- the fact
    an operator debugging a missing row needs, and why `attach_titles`
    filters on kind rather than trusting its caller.

    Flushes, never commits.
    """

    @abstractmethod
    async def get(self, collection_id: uuid.UUID) -> OwnedCollection | None:
        """One franchise and the household's coverage of it, or `None` when the catalog lacks it."""

    @abstractmethod
    async def upsert_many(self, collections: Sequence[Collection]) -> BulkWriteResult:
        """Insert or update, keyed on `tmdb_id`.

        Keyed on `tmdb_id` rather than `Collection.id`: the derivation mints
        a fresh UUIDv7 per sighting, so an id-keyed upsert grows a duplicate
        franchise per pass. A batch names one collection once per member
        film, so deduplication is the common case.
        """

    @abstractmethod
    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        """`tmdb_id` -> collection id, in one round trip.

        Absent keys mean "no such collection", never "not asked".
        `attach_titles`' pairs are built from this.
        """

    @abstractmethod
    async def attach_titles(self, links: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> int:
        """Set `titles.collection_id` for each `(title_id, collection_id)` pair.

        Returns the number of rows actually **changed**.
        """

    @abstractmethod
    async def count(self) -> int:
        """How many franchises the catalog holds.

        Not scoped to franchises with owned members -- that is `list_owned`'s
        question. Narrowing it would make an empty answer ambiguous between
        "nothing derived" and "nothing owned".
        """

    @abstractmethod
    async def list_owned(self, *, min_owned: int = 2, limit: int = 5) -> list[OwnedCollection]:
        """Franchises the household owns at least `min_owned` of, most-owned first."""
