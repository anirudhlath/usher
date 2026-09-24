"""`GET /collections/{id}` (PRD 07) -- a franchise, and how much of it the household has."""

import uuid
from collections.abc import Iterable

from pydantic import BaseModel

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.repository import OwnedCollection

__all__ = ["CollectionMemberResponse", "CollectionResponse"]


class CollectionMemberResponse(BaseModel):
    """One film in the franchise, and whether the household has a copy.

    `owned` is B6's predicate as the browse surface settled it: an
    **available**, **title-level** media item (`episode_id IS NULL`). Both
    halves matter in the same direction -- a retracted copy on an unmounted
    drive and an episode-level row each read as owned without them, and the
    page then overstates, which is the direction nobody checks. B6's documented
    consequence (a library reporting a series' episodes but not the series' own
    item reads as not-owned for that series) cannot arise here at all, because
    `belongs_to_collection` is a field of `/movie/{id}` with no `/tv/{id}`
    counterpart and a collection therefore holds only movies.

    No progress and no watch state: those are per-*user* and this route takes
    no user. Ownership is a property of the household's sources, which is the
    same argument `CollectionRepository.list_owned` makes about having no
    `user_id`.
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    enrichment_state: EnrichmentState
    owned: bool


class CollectionResponse(BaseModel):
    """A franchise and the household's coverage of it.

    Members are in release order -- the order the repository returned, which a
    franchise page renders in -- and **not** owned-first. Sorting the owned to
    the top is a plausible "show me what I can play" instinct that turns a
    timeline into two piles.
    """

    id: uuid.UUID
    name: str
    # Both `len()` of what is below, computed here and nowhere else: a second
    # source for either number is a chance for "you own 2 of 4" to be printed
    # over three cards.
    owned_count: int
    total_count: int
    titles: tuple[CollectionMemberResponse, ...]

    @classmethod
    def of(cls, collection: OwnedCollection, titles: Iterable[Title]) -> "CollectionResponse":
        """The franchise, hydrated.

        **Ordered by `collection.title_ids`, not by what `list_by_ids`
        returned.** That port promises no order at all -- "in any order", in
        its own docstring -- and the Postgres implementation is a bare
        `IN (...)`, so rendering what came back is rendering physical order.

        **A member the catalog no longer holds is dropped, not a `KeyError`.**
        `list_by_ids` returns fewer rows than asked for and a title deleted
        between the two reads is ordinary; the honest answer is a shorter list
        with counts that match it.
        """
        by_id = {title.id: title for title in titles}
        members = tuple(
            CollectionMemberResponse(
                title_id=title.id,
                kind=title.kind,
                name=title.name,
                year=title.year,
                enrichment_state=title.enrichment_state,
                owned=title.id in collection.owned_title_ids,
            )
            for title_id in collection.title_ids
            if (title := by_id.get(title_id)) is not None
        )
        return cls(
            id=collection.collection_id,
            name=collection.name,
            owned_count=len([one for one in members if one.owned]),
            total_count=len(members),
            titles=members,
        )
