"""Collections -- the port for a re-derived franchise or box set.

Implemented by
`usher.db.repositories.collection.PostgresCollectionRepository`.
"""

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

    **Lists, not counts, and the two counts are `len()`.** PRD 06's franchise
    signal is "you own 2 of 4", which is two numbers *and* the cards to
    render. Storing `owned_count` beside `owned_title_ids` would permit the
    two to disagree, which is a state no consumer could interpret -- the same
    argument `title_neighbors`' primary key makes about `(title_id, rank)`.

    `title_ids` is every member in release order, `owned_title_ids` the subset
    with an available media item. The difference is the completeness signal,
    and it is what makes a franchise row say something a genre row cannot.
    """

    collection_id: uuid.UUID
    name: str
    title_ids: tuple[uuid.UUID, ...]
    owned_title_ids: frozenset[uuid.UUID]


class CollectionRepository(ABC):
    """Persistence for TMDb's movie franchise grouping, and the writer
    `titles.collection_id` has never had.

    **Movies only, and the port says so rather than a provider discovering
    it.** `belongs_to_collection` is a field of `/movie/{id}` with no
    `/tv/{id}` counterpart -- verified against the recorded payloads. So on a
    television-only household PRD 06's ">= 2 owned titles in a collection" is
    unsatisfiable **by construction** rather than by absence of data, which is
    the fact an operator debugging a missing row needs, and it is why
    `attach_titles` filters on kind rather than trusting its caller.

    Flushes, never commits.
    """

    @abstractmethod
    async def get(self, collection_id: uuid.UUID) -> OwnedCollection | None:
        """One franchise and the household's coverage of it, or `None` when
        the catalog does not hold it.

        **The same shape `list_owned` returns and deliberately not a narrower
        one.** PRD 06's signal is *"you own 2 of 4"*, which is two numbers and
        the cards to render; `OwnedCollection` carries the two **lists** so the
        counts are `len()` and cannot disagree with what they count. A scoped
        read that answered a bare count would reintroduce exactly the state no
        consumer could interpret.

        **No `min_owned`, and its absence is the whole difference from
        `list_owned`.** That method's floor of 2 is a statement about what
        belongs on a *screen* -- a franchise you own one of is a single film
        with a subtitle. Asking for a specific collection by id is a different
        request: the client is on a film's page and followed a link, and the
        honest answer is "you own 1 of 4". Re-applying the floor here 404s the
        franchise a household has barely started, which is the one it most
        wants to be told about.

        `None` rather than a raise, and never an empty `OwnedCollection`: the
        route above answers 404 for a franchise the catalog does not hold and
        200 with `owned_count: 0` for one it holds and the household owns none
        of. An implementation returning an empty shell for both collapses two
        different facts into one.

        **Movies only, and this statement says so itself rather than trusting
        `attach_titles`.** That filter is a property of the data source --
        `belongs_to_collection` has no `/tv/{id}` counterpart -- and `titles`
        deliberately carries no `CHECK (collection_id IS NULL OR kind =
        'movie')`, so a series carrying a collection id is storable by anything
        that writes the column. A reader that trusted the writer would put a
        television show on a franchise page.

        **Owned means an available, title-level media item**, `episode_id IS
        NULL` written into the predicate rather than implied, for
        `list_owned`'s reason restated because it is the same predicate: 999,827
        of the one measured deployment's 1,126,789 `media_items` rows are
        episodes, and its absence is otherwise indistinguishable from having
        forgotten it.

        Members in release order, exactly as `list_owned` returns them, so the
        two reads of one franchise cannot render it differently.

        **Unbounded, and the bound is stated rather than assumed.** TMDb
        franchises are single-digit to low-double-digit; there is no cursor,
        and the day one is needed it is the opaque codec `browse` already uses
        over the keyset shape `browse` already has.
        """

    @abstractmethod
    async def upsert_many(self, collections: Sequence[Collection]) -> BulkWriteResult:
        """Insert or update, keyed on `tmdb_id`.

        Keyed on `tmdb_id` rather than `Collection.id` for
        `PersonRepository.upsert_many`'s reason: the derivation mints a fresh
        UUIDv7 per sighting, so an id-keyed upsert grows a duplicate franchise
        per pass. A batch names the same collection once per member film, so
        deduplication is the common case rather than the odd one.
        """

    @abstractmethod
    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        """`tmdb_id` -> collection id, in one round trip. Absent keys mean "no
        such collection", never "not asked". Same argument as
        `PersonRepository.resolve_tmdb_ids`, and it is what
        `attach_titles`' pairs are built from."""

    @abstractmethod
    async def attach_titles(self, links: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> int:
        """Set `titles.collection_id` for each `(title_id, collection_id)`
        pair. Returns the number of rows actually **changed**.

        **Changed, not touched.** A re-derivation over an unchanged catalog
        must write zero rows: an implementation that assigns unconditionally
        produces a dead row version per movie per pass, on a table with a GIN
        index and a stored generated column. This repository has already
        recorded that shape once, in a `DO UPDATE` with no `WHERE`, and the
        returned count is what makes it observable rather than merely avoided.

        **Filters `kind = 'movie'` itself, and does not trust its caller.** A
        series carrying a movie's `belongs_to_collection` is the fourth wrong
        implementation this port's contract must kill, and the filter lives
        here because it is a property of the data source rather than of any
        one call site. `titles` deliberately carries no
        `CHECK (collection_id IS NULL OR kind = 'movie')` -- see
        `db/models/collection.py` for why -- so this is what enforces it.

        A `collection_id` naming no collection raises `RepositoryConflict`. A
        `title_id` naming no title is simply not updated: an `UPDATE` that
        matches nothing is not an error, and treating it as one would make a
        concurrent title merge fail a derivation.

        **Does not clear links outside `links`.** The scope is the pairs
        given, not "the world". An implementation that NULLs every unnamed
        title unlinks the whole catalog the first time the derivation runs
        over one page.
        """

    @abstractmethod
    async def count(self) -> int:
        """How many franchises the catalog holds -- `usher derive`'s report.

        Deliberately **not** scoped to franchises with owned members, which is
        `list_owned`'s question: this one answers "did the derivation write
        collections", and narrowing it would make an empty answer ambiguous
        between "nothing derived" and "nothing owned".
        """

    @abstractmethod
    async def list_owned(self, *, min_owned: int = 2, limit: int = 5) -> list[OwnedCollection]:
        """Franchises the household owns at least `min_owned` of, most-owned
        first.

        **No `user_id`, deliberately, and PRD 06's wording is what settles
        it.** ">= 2 owned titles in a collection" is a statement about
        *ownership*, and ownership is a property of the household's sources --
        `MediaItem` has no user and never has. A `user_id` parameter here
        would be a fiction every implementation would have to ignore. The
        row's personalisation comes from `HomeService`'s scoring, not from
        this read.

        `min_owned` defaults to 2 because a franchise you own one of is not a
        franchise row -- it is a single film with a subtitle, and it is the
        distractor this suite's case seeds.

        **Owned means an available, title-level media item.** `episode_id IS
        NULL` is part of the predicate rather than implied: `media_items`
        holds 999,827 episode rows on the one measured deployment, and a join
        on `title_id` alone reads the wrong population. Collections hold only
        movies so no episode can match today, which is exactly why the clause
        has to be written down -- its absence is otherwise indistinguishable
        from having forgotten it.

        One statement, not one per collection. Ordered by owned count
        descending, ties broken by `collection_id`.
        """
