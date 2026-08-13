"""In-memory `CollectionRepository`.

**Where this is more forgiving than Postgres, on purpose.** Six places, each
of which the paired `tests/integration/test_collection_repository.py` run is
what actually closes:

- **`titles` is a mapping this fake is handed**, so `attach_titles` can apply
  the `kind = 'movie'` filter at all. In SQL that is a `WHERE` clause a
  mutation deletes; here it is an `if` a mutation deletes; the case kills
  both, which is the one place these two implementations fail identically.
  Named first because it is the divergence a reader would otherwise assume
  cuts the other way.
- **No foreign keys**, so `attach_titles` here cannot raise
  `RepositoryConflict` for a `collection_id` naming no collection.
  `test_a_link_to_no_collection_is_a_port_error` is Postgres-only.
- **`IS DISTINCT FROM` is Python's `!=`**, which already treats `None`
  correctly. In SQL `<>` does not -- `NULL <> :x` is NULL, so a first attach
  writes nothing at all -- which is why the contract asserts the *first*
  call's count as well as the second's.
- **No stored generated column and no GIN index**, so the whole cost the
  `IS DISTINCT FROM` guard exists to avoid is invisible here. The guard is
  observable only through the returned count, which is why the port promises
  *changed* rather than *touched*.
- **`xmax = 0` has no analogue.** `inserted`/`updated` are dict membership,
  which *is* the answer rather than a measurement of it.
- **No release date, so `get`'s members come back in insertion order.** The
  real one orders them `release_date NULLS LAST, year NULLS LAST, title_id`,
  which is the order a franchise page renders in -- so the shared contract
  asserts on the member *set* and only
  `tests/integration/test_collection_repository.py` can assert the sequence.
  Same divergence `list_owned` already carries, at a second read.

`titles` and `media_items` are test-double affordances written only by
`FakeCollectionSeeder`; the port never writes either.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from usher.domain.collection import Collection
from usher.domain.enums import TitleKind
from usher.ports.repository import BulkWriteResult, CollectionRepository, OwnedCollection


@dataclass(frozen=True, slots=True)
class SeededMediaItem:
    """One `media_items` row.

    `episode_id` is modelled because `list_owned`'s predicate excludes those
    rows: `IngestService` writes an episode's row with its series' `title_id`
    **and** its own `episode_id`, so a join on `title_id` alone reads a series
    as owned once per episode file -- 999,827 of the one measured deployment's
    1,126,789 items.
    """

    title_id: uuid.UUID
    episode_id: uuid.UUID | None
    available: bool


@dataclass
class _Catalog:
    kinds: dict[uuid.UUID, TitleKind] = field(default_factory=dict)
    collection_ids: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)
    order: list[uuid.UUID] = field(default_factory=list)
    media_items: list[SeededMediaItem] = field(default_factory=list)


class FakeCollectionRepository(CollectionRepository):
    def __init__(self) -> None:
        self._by_tmdb_id: dict[int, Collection] = {}
        self._anonymous: dict[uuid.UUID, Collection] = {}
        self.catalog = _Catalog()
        self.calls = 0

    def reset_calls(self) -> None:
        self.calls = 0

    def _name_of(self, collection_id: uuid.UUID) -> str:
        for one in (*self._by_tmdb_id.values(), *self._anonymous.values()):
            if one.id == collection_id:
                return one.name
        raise KeyError(collection_id)

    async def get(self, collection_id: uuid.UUID) -> OwnedCollection | None:
        self.calls += 1
        try:
            name = self._name_of(collection_id)
        except KeyError:
            return None
        # `kind = 'movie'` here as well as in `attach_titles`, and not shared
        # with it: the real one repeats the clause in a second statement, so a
        # fake that filtered once would make the second statement's copy
        # unobservable on this arm.
        members = [
            title_id
            for title_id in self.catalog.order
            if self.catalog.collection_ids.get(title_id) == collection_id
            and self.catalog.kinds.get(title_id) is TitleKind.MOVIE
        ]
        return OwnedCollection(
            collection_id=collection_id,
            name=name,
            # Insertion order, because this fake has no release date. The
            # sequence is therefore asserted only in the integration arm; see
            # the module docstring's sixth divergence.
            title_ids=tuple(members),
            owned_title_ids=frozenset(members) & self._owned_titles(),
        )

    def _owned_titles(self) -> set[uuid.UUID]:
        """`episode_id IS NULL AND available`, which both reads apply.

        One helper here and **two written-out copies in Postgres**, where
        `_LIST_OWNED` and `_GET_COLLECTION` are separate statements. That is a
        divergence worth knowing about rather than tidying: each of those
        copies has its own contract case, and a mutation to one of them fails
        only that case there while failing both here.
        """
        return {
            item.title_id
            for item in self.catalog.media_items
            if item.episode_id is None and item.available
        }

    async def upsert_many(self, collections: Sequence[Collection]) -> BulkWriteResult:
        self.calls += 1
        inserted = updated = 0
        deduped: dict[int, Collection] = {}
        anonymous: list[Collection] = []
        for incoming in collections:
            if incoming.tmdb_id is None:
                anonymous.append(incoming)
            else:
                deduped[incoming.tmdb_id] = incoming

        for tmdb_id, incoming in deduped.items():
            existing = self._by_tmdb_id.get(tmdb_id)
            if existing is None:
                self._by_tmdb_id[tmdb_id] = incoming
                inserted += 1
            else:
                # `name` assigned rather than COALESCEd: NOT NULL and always
                # supplied, so preserving a stored one makes a corrected name
                # unfixable.
                self._by_tmdb_id[tmdb_id] = existing.evolve(name=incoming.name)
                updated += 1

        for incoming in anonymous:
            self._anonymous[incoming.id] = incoming
            inserted += 1

        return BulkWriteResult(inserted=inserted, updated=updated)

    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        self.calls += 1
        return {
            tmdb_id: self._by_tmdb_id[tmdb_id].id
            for tmdb_id in dict.fromkeys(tmdb_ids)
            if tmdb_id in self._by_tmdb_id
        }

    async def attach_titles(self, links: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> int:
        self.calls += 1
        changed = 0
        for title_id, collection_id in links:
            # `kind = 'movie'` here rather than in the caller:
            # `belongs_to_collection` has no `/tv/{id}` counterpart, so a
            # series carrying a collection id is a defect wherever it came
            # from. A title this fake was never told about is simply not
            # updated -- an UPDATE that matches nothing is not an error.
            if self.catalog.kinds.get(title_id) is not TitleKind.MOVIE:
                continue
            if self.catalog.collection_ids.get(title_id) != collection_id:
                self.catalog.collection_ids[title_id] = collection_id
                changed += 1
        return changed

    async def count(self) -> int:
        return len(self._by_tmdb_id) + len(self._anonymous)

    async def list_owned(self, *, min_owned: int = 2, limit: int = 5) -> list[OwnedCollection]:
        self.calls += 1
        owned_titles = self._owned_titles()
        members: dict[uuid.UUID, list[uuid.UUID]] = {}
        for title_id in self.catalog.order:
            collection_id = self.catalog.collection_ids.get(title_id)
            if collection_id is not None:
                members.setdefault(collection_id, []).append(title_id)

        rows = []
        for collection_id, title_ids in members.items():
            owned = frozenset(title_ids) & owned_titles
            if len(owned) < min_owned:
                continue
            rows.append(
                OwnedCollection(
                    collection_id=collection_id,
                    name=self._name_of(collection_id),
                    # Every member, not the owned subset: "you own 2 of 4"
                    # reads "2 of 2" otherwise, i.e. a completeness signal
                    # that always reads complete.
                    title_ids=tuple(title_ids),
                    owned_title_ids=owned,
                )
            )
        rows.sort(key=lambda row: (-len(row.owned_title_ids), row.collection_id))
        return rows[:limit]
