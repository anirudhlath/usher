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
    """One title's id and its genre labels — the whole projection the
    write-time genre sweep reads and writes.

    **Not a `Title`.** `usher genres --backfill` walks 1.27M rows to decide
    whether two of thirty-three columns need touching, and hydrating an
    entity per row would detoast an overview, a tsvector's worth of derived
    state and up to ten cast names to answer a question about a `text[]`.
    `credit_names_for`'s mapping is the nearest precedent on this port: a
    read shaped by what the caller does with it rather than by the aggregate.

    A tuple rather than a list, so it compares by value against
    `canonicalise_genres`' own output — which is what makes "did this row
    change" one `!=` rather than a normalisation of two container types.
    """

    id: uuid.UUID
    genres: tuple[str, ...]


# : Each sort's `titles` column -- which is also its `Title` field, because the : two
# are 1:1 by the rule `db/models/title.py` and `_to_domain` share -- and : whether it
# runs descending.
_ORDERS: Final[Mapping[str, tuple[str, bool]]] = MappingProxyType(
    {
        "name": ("sort_name", False),
        "year": ("year", True),
        # The keys are `BrowseSort`'s values and therefore the public
        # `?sort=` vocabulary; the values are `Title` attributes. ADR-0040
        # moved the attributes and deliberately left the vocabulary alone.
        "popularity": ("tmdb_popularity", True),
        "vote_count": ("tmdb_vote_count", True),
    }
)


class BrowseSort(StrEnum):
    """The closed vocabulary `browse` orders by.

    Four members, and three of the four keys are **nullable** —
    `titles.year`, `titles.tmdb_popularity` and `titles.tmdb_vote_count` all
    are, and `tmdb_popularity` was measured NULL on all 1,271,138 rows of a
    bootstrap-only catalog. That is why every order here is NULLS LAST and why the keyset
    predicate carries an `IS NOT NULL` leg: see `TitleRepository.browse`.

    `name` sorts on `sort_name` rather than on `name`, which is the column
    `Title.sort_name`'s own comment reserves for "catalog ordering", and it is
    the one key that cannot be NULL.
    """

    NAME = "name"
    YEAR = "year"
    POPULARITY = "popularity"
    VOTE_COUNT = "vote_count"

    @classmethod
    def order_for(cls, sort: "BrowseSort") -> tuple[str, bool]:
        """`(column, descending)` for `sort`, or `FilterNotSupported`.

        **A classmethod taking the value rather than a property**, because the
        argument this has to refuse is precisely one that is *not* a member:
        a route mapping a query string, or a later member added here without
        an entry in `_ORDERS`. `str(sort)` is the member's value for a real
        member and the raw string for anything cast into the annotation, so
        both reach the same lookup.

        Raising is the whole point, and it is `FilterNotSupported`'s own
        stated argument one port over: a sort quietly ignored answers with
        *more* rows in some other order, and more rows reads as working. A
        `/browse` that silently fell back to `id` would page correctly, look
        correct, and be a different screen.
        """
        try:
            return _ORDERS[str(sort)]
        except KeyError:
            raise FilterNotSupported("sort") from None

    @classmethod
    def position_of(cls, title: Title, *, sort: "BrowseSort") -> "BrowseCursorPosition":
        """Where `title` sits in `sort`'s order — what a caller hands back as
        `browse`'s `after`.

        Here rather than in the caller so the sort's key is read from
        `_ORDERS` in one place. A route that spelled `title.year` for itself
        would be a second definition of what `year` means, free to disagree
        with the statement it is paging.
        """
        column, _ = cls.order_for(sort)
        key: str | int | float | None = getattr(title, column)
        return BrowseCursorPosition(key=key, id=title.id)


@dataclass(frozen=True, slots=True)
class BrowseCursorPosition:
    """One row's place in a `BrowseSort`'s order: the sort key, and the id.

    **Typed values, never a cursor.**
    [ADR-0034](../../../docs/prd/decisions/0034-the-cursor-carries-a-position.md)
    holds that no port takes an opaque cursor — the base64 lives in
    `usher.api.cursor` and a port that accepted one would have to decode it,
    which means knowing the sort vocabulary of the layer above. So the route
    decodes, builds one of these, and hands it down.

    `key` is `None` for a row whose sort column is NULL, and that is a
    *position* rather than a missing value: `browse` orders NULLs last, so a
    page boundary can land inside the unkeyed group and the walk has to be
    able to resume from it. `id` is the UUIDv7 primary key, which is what
    makes the keyset a total order (ADR-0003, and `CursorSpec` refuses a
    keyset that does not end in one).
    """

    key: str | int | float | None
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class BrowseFacets:
    """What else the client could have asked for, counted.

    Each facet is computed over the filtered population **minus its own
    predicate** — see `TitleRepository.browse_facets`, which is where that
    rule is argued.

    Both maps are wrapped in a `MappingProxyType` so a caller cannot mutate a
    count it was handed. Immutability only: `mappingproxy` delegates
    `__hash__` to the dict it wraps, which is `None`, so this dataclass is not
    hashable and does not claim to be (CLAUDE.md).
    """

    genres: Mapping[str, int] = field(default_factory=dict)
    years: Mapping[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "genres", MappingProxyType(dict(self.genres)))
        object.__setattr__(self, "years", MappingProxyType(dict(self.years)))


class TitleRepository(ABC):
    """Persistence for canonical titles, kept behind a port so services depend on this ABC
    and never on `usher.db` directly — see ADR-0009.
    """

    @abstractmethod
    async def add(self, title: Title) -> None:
        """Persist a new title.

        This is an insert, not an upsert: a duplicate `title.id` — or any
        other unique constraint the backing store enforces — raises
        `RepositoryConflict` (`usher.ports.errors`). Implementations
        translate their backing store's own conflict error (e.g.
        Postgres's `IntegrityError`) into this; callers never import a
        storage-specific exception to handle it. See `update` for
        mutating a title that already exists.

        The caller owns the session and the transaction: this flushes, so
        the row and any conflict are visible immediately, but it never
        commits. Committing or rolling back is the caller's call.
        """

    @abstractmethod
    async def update(self, title: Title) -> None:
        """Persist a mutated, already-existing title — e.g.
        `title.evolve(enrichment_state=EnrichmentState.ENRICHED, ...)`
        after enrichment, which is the read-through design's whole point
        (PRD 03: stub-on-sight, then enrich in place).

        This is an update, not an upsert: a `title.id` that does not
        already exist raises `RepositoryNotFound` (`usher.ports.errors`).
        See `add` for a brand-new title.

        Same session/transaction ownership as `add`: flushes, never
        commits.

        Unconditional last-write-wins: there is no optimistic concurrency
        check (no version column, no `WHERE` clause comparing against the
        row's state as last read) — the incoming `title` simply overwrites
        whatever is currently stored, even if it was read before some
        other write landed. M4's concurrent enrichment (multiple sources
        or workers updating the same title around the same time) will
        eventually need one; not built here.
        """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> Title | None:
        """Fetch by Usher's own id, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        """Fetch by TMDb id *within its namespace*, or None if no title
        carries it.

        `kind` is not optional, and not a convenience filter. TMDb keys
        movies and TV series in separate id spaces that both land in this
        one column, and they overlap heavily: 26,968 of the 56,975 distinct
        TMDb series ids Wikidata knows are also live TMDb movie ids
        (measured 2026-07-30). "Which title has tmdb_id 90000550" has no single
        answer; "which movie has tmdb_id 90000550" does. See
        [ADR-0011](../../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).

        Every real caller already knows the kind — M4's matcher reads it off
        the source item alongside `ProviderIds.Tmdb` — so this costs nothing
        it does not already have.
        """

    @abstractmethod
    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        """Fetch by IMDb id, or None if no title carries it."""

    @abstractmethod
    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        """Every title named by `title_ids` that still exists, in any order.

        **A missing id is an omission, never an error.** A title deleted
        between an index write and a search read is ordinary, and the caller
        re-orders by its own ranking anyway — so returning fewer rows than
        asked for is the contract, and a caller that indexes the result by id
        must tolerate the gap.

        Exists because hydrating a 50-hit result set through `get()` is 50
        statements per search: the same round-trip-per-item shape `index_many`
        was introduced to delete from `SearchIndex`, arriving from the other
        direction.
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

        Always returns all three `EnrichmentState` members as keys, 0 for
        any tier with no titles — never a sparse dict. A `GROUP BY` only
        returns tiers with at least one row; an implementation must fill
        in the rest itself rather than let the query's own sparsity leak
        through (a bare `counts[EnrichmentState.ENRICHED]` must never
        raise `KeyError` just because nothing is enriched yet).
        """

    @abstractmethod
    async def list_genres_page(
        self, *, limit: int = 1000, after: uuid.UUID | None = None
    ) -> list[TitleGenres]:
        """One page of the catalog's genre labels, oldest id first."""

    @abstractmethod
    async def replace_genres(self, rows: Sequence[TitleGenres]) -> int:
        """Set each title's genres, returning how many rows actually moved."""
