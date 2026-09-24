"""Titles -- the canonical aggregate everything else hangs from."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.repository._references import TitleReference
from usher.ports.search import FilterNotSupported

__all__ = [
    "BrowseCursorPosition",
    "BrowseFacets",
    "BrowseSort",
    "TitleGenres",
    "TitleRepository",
]


@dataclass(frozen=True, slots=True)
class TitleGenres:
    """One title's id and its genre labels.

    The whole projection the write-time genre sweep reads and writes, not a
    `Title`: the backfill walks the catalog to decide whether one `text[]`
    needs touching, and hydrating an entity per row would detoast an
    overview, derived tsvector state and cast names to answer that.

    A tuple, not a list, so it compares by value against
    `canonicalise_genres`' output -- "did this row change" is one `!=`.
    """

    id: uuid.UUID
    genres: tuple[str, ...]


#: Each sort's `titles` column -- also its `Title` field, the two being 1:1 --
#: and whether it runs descending.
_ORDERS: Final[Mapping[str, tuple[str, bool]]] = MappingProxyType(
    {
        "name": ("sort_name", False),
        "year": ("year", True),
        # Keys are `BrowseSort`'s values and so the public `?sort=`
        # vocabulary; values are `Title` attributes. Renaming an attribute
        # must not rename the query string.
        "popularity": ("tmdb_popularity", True),
        "vote_count": ("tmdb_vote_count", True),
    }
)


class BrowseSort(StrEnum):
    """The closed vocabulary `browse` orders by.

    Three of the four keys are nullable -- `titles.year`,
    `titles.tmdb_popularity`, `titles.tmdb_vote_count` -- and a
    bootstrap-only catalog has them NULL throughout. Hence NULLS LAST on
    every order and the `IS NOT NULL` leg in `TitleRepository.browse`'s
    keyset predicate.

    `name` sorts on `sort_name`, the column reserved for catalog ordering and
    the one key that cannot be NULL.
    """

    NAME = "name"
    YEAR = "year"
    POPULARITY = "popularity"
    VOTE_COUNT = "vote_count"

    @classmethod
    def order_for(cls, sort: "BrowseSort") -> tuple[str, bool]:
        """`(column, descending)` for `sort`, or `FilterNotSupported`.

        A classmethod taking the value, not a property, because what it must
        refuse is an argument that is *not* a member: a raw query string, or
        a member added without an `_ORDERS` entry. Both reach the same lookup
        through `str(sort)`.

        Raising is the point. A sort quietly ignored answers with more rows
        in some other order, and more rows reads as working.
        """
        try:
            return _ORDERS[str(sort)]
        except KeyError:
            raise FilterNotSupported("sort") from None

    @classmethod
    def position_of(cls, title: Title, *, sort: "BrowseSort") -> "BrowseCursorPosition":
        """Where `title` sits in `sort`'s order -- `browse`'s `after`.

        Here, not in the caller, so the sort's key is read from `_ORDERS` in
        one place. A route spelling `title.year` for itself would be a second
        definition of `year`, free to disagree with the statement it pages.
        """
        column, _ = cls.order_for(sort)
        key: str | int | float | None = getattr(title, column)
        return BrowseCursorPosition(key=key, id=title.id)


@dataclass(frozen=True, slots=True)
class BrowseCursorPosition:
    """One row's place in a `BrowseSort`'s order: the sort key, and the id.

    Typed values, never an opaque cursor. The base64 lives in
    `usher.api.cursor`; a port accepting one would have to decode it, which
    means knowing the layer above's sort vocabulary. The route decodes,
    builds one of these, and hands it down.

    `key` is `None` for a NULL sort column, and that is a position rather
    than a missing value -- NULLs order last, so a page boundary can land
    inside the unkeyed group and the walk must resume from it. `id` is the
    UUIDv7 primary key, which is what makes the keyset a total order.
    """

    key: str | int | float | None
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class BrowseFacets:
    """What else the client could have asked for, counted.

    Each facet counts the filtered population minus its own predicate.

    The maps are `MappingProxyType` so a caller cannot mutate a count it was
    handed. Immutability only -- `mappingproxy` delegates `__hash__` to the
    dict it wraps, so this dataclass is not hashable.
    """

    genres: Mapping[str, int] = field(default_factory=dict)
    years: Mapping[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "genres", MappingProxyType(dict(self.genres)))
        object.__setattr__(self, "years", MappingProxyType(dict(self.years)))


class TitleRepository(ABC):
    """Persistence for canonical titles.

    Behind a port so services depend on this ABC, never on `usher.db`.
    """

    @abstractmethod
    async def add(self, title: Title) -> None:
        """Persist a new title.

        Insert, not upsert: a duplicate `title.id`, or any other unique
        constraint the store enforces, raises `RepositoryConflict`.
        Implementations translate the store's own conflict error into it so
        callers never import a storage-specific exception.

        The caller owns the session and transaction. This flushes, so the row
        and any conflict are visible immediately, but never commits.
        """

    @abstractmethod
    async def update(self, title: Title) -> None:
        """Persist a mutated, already-existing title.

        Update, not upsert: a `title.id` that does not exist raises
        `RepositoryNotFound`. Same session and transaction ownership as
        `add` -- flushes, never commits.

        Unconditional last-write-wins. There is no optimistic concurrency
        check, so `title` overwrites whatever is stored even if it was read
        before another write landed; concurrent enrichment of one title will
        eventually need one.
        """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> Title | None:
        """Fetch by Usher's own id, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        """Fetch by TMDb id *within its namespace*, or None if no title carries it.

        `kind` is not optional and not a convenience filter. TMDb keys movies
        and series in separate id spaces that both land in this one column,
        and roughly half of all series ids are also live movie ids. "Which
        title has this tmdb_id" has no single answer; "which movie has it"
        does. Callers read the kind off the source item anyway.
        """

    @abstractmethod
    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        """Fetch by IMDb id, or None if no title carries it."""

    @abstractmethod
    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        """Every title named by `title_ids` that still exists, in any order.

        A missing id is an omission, never an error: a title deleted between
        an index write and a search read is ordinary, and the caller re-orders
        by its own ranking anyway. Returning fewer rows than asked for is the
        contract, so a caller indexing the result by id must tolerate gaps.

        One statement, not one per hit.
        """

    @abstractmethod
    async def resolve_tmdb_ids(
        self, kind: TitleKind, tmdb_ids: Sequence[int]
    ) -> dict[int, uuid.UUID]:
        """`tmdb_id` -> title id **within one id space**, in one round trip."""

    @abstractmethod
    async def resolve_natural_keys(
        self, references: Sequence[TitleReference]
    ) -> dict[TitleReference, uuid.UUID]:
        """`TitleReference` -> the id **this** catalog holds it under, in one round trip."""

    @abstractmethod
    async def credit_names_for(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, ...]]:
        """Weight class B's input for a page of titles."""

    @abstractmethod
    async def list_owned_by_tag(
        self,
        *,
        genre: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
    ) -> list[Title]:
        """Owned titles carrying a genre and/or a keyword, best first."""

    @abstractmethod
    async def list_unwatched_candidates(
        self,
        user_id: uuid.UUID,
        *,
        genres: Sequence[str] = (),
        limit: int,
    ) -> list[Title]:
        """The curation pool: titles this household has not seen, best first."""

    @abstractmethod
    async def browse(
        self,
        *,
        sort: BrowseSort,
        genre: str | None = None,
        year: int | None = None,
        owned: bool | None = None,
        after: BrowseCursorPosition | None = None,
        limit: int,
    ) -> list[Title]:
        """One keyset page of the catalog, filtered and sorted."""

    @abstractmethod
    async def browse_facets(
        self,
        *,
        genre: str | None = None,
        year: int | None = None,
        owned: bool | None = None,
    ) -> BrowseFacets:
        """What else the same client could have asked for, counted."""

    @abstractmethod
    async def count_by_state(self) -> dict[EnrichmentState, int]:
        """Catalog size broken down by enrichment tier.

        Every `EnrichmentState` member is a key, 0 for an empty tier, never a
        sparse dict. A `GROUP BY` returns only tiers with rows, so an
        implementation fills the rest in itself: a bare
        `counts[EnrichmentState.ENRICHED]` must not raise `KeyError` merely
        because nothing is enriched yet.
        """

    @abstractmethod
    async def list_genres_page(
        self, *, limit: int = 1000, after: uuid.UUID | None = None
    ) -> list[TitleGenres]:
        """One page of the catalog's genre labels, oldest id first."""

    @abstractmethod
    async def replace_genres(self, rows: Sequence[TitleGenres]) -> int:
        """Set each title's genres, returning how many rows actually moved."""
