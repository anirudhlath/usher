"""The review queue's wire shapes: one queue entry, one resolution, and what
a resolution answers with.

**`external_id` is on the wire here and is refused on `GET /titles/{id}`, and
that is a decision rather than an inconsistency.** `api/routers/titles.py`'s
leak assertions exist because a *client* has no use for a source's own item id
and every route a client calls addresses things by Usher's UUIDv7 -- so an
`external_id` in a client response is a source concept escaping its adapter.
This queue is the opposite case: an operator resolving an unmatched file
resolves it by finding that file **on their own server**, and the source's own
id is the only handle that gets them there. `usher unmatched` has printed it
since M4 for exactly that reason, and a route that withheld it would be a
review queue an operator cannot review from.

**What a queue entry deliberately does not carry.** No container, codec,
resolution or file size: this is a listing an operator scans, not a detail
screen, and each of those would be a claim about what identifies a file that
nothing here has measured. `MediaItem` holds them all and a later detail route
can render them; adding them now would be a response shape chosen by
availability rather than by need. No `title_id` either -- every row on this
page has none, which is what put it here.
"""

import uuid

from pydantic import AwareDatetime, BaseModel

from usher.domain.source import MediaItem

__all__ = [
    "ResolveUnmatchedRequest",
    "ResolvedItemResponse",
    "UnmatchedItemResponse",
]


class UnmatchedItemResponse(BaseModel):
    """One item in the review queue (PRD 02: *"unmatched items are never
    dropped"*).

    `added_at` is `None` for an item its source could not date, which is not
    an edge case: it is the population this queue's keyset is built to page
    through without dropping (ADR-0034), and it sorts last because an item
    nobody can date is less interesting than one dated yesterday, not more.

    `available` is here because a copy the nightly sweep retracted is still an
    unmatched row -- PRD 02 soft-deletes availability and hard-deletes nothing
    -- and it is the fact that tells an operator whether resolving this entry
    still buys anything.
    """

    id: uuid.UUID
    source_id: uuid.UUID
    external_id: str
    added_at: AwareDatetime | None = None
    last_seen_at: AwareDatetime
    available: bool

    @classmethod
    def of(cls, entry: MediaItem) -> "UnmatchedItemResponse":
        return cls(
            id=entry.id,
            source_id=entry.source_id,
            external_id=entry.external_id,
            added_at=entry.added_at,
            last_seen_at=entry.last_seen_at,
            available=entry.available,
        )


class ResolveUnmatchedRequest(BaseModel):
    """What an operator says a file is.

    **`episode_id` is the argument `usher.cli._unmatched` said this route
    would grow.** That comment reads: *"an episode-level resolution needs an
    `Episode.id` an operator has no way to read off this listing, and M9's
    route is where that grows a second argument."* It is optional because a
    film resolves to a title and nothing else, and because an episode's
    `media_items` row carries **both** ids (`ports/ingest.py`'s
    `MediaItemTarget`) -- so a title with no episode is a complete resolution
    rather than a half-finished one.

    Both ids are Usher's own UUIDv7. `tmdb_id`/`imdb_id` are indexed
    attributes and never identifiers in an API contract.
    """

    title_id: uuid.UUID
    episode_id: uuid.UUID | None = None


class ResolvedItemResponse(BaseModel):
    """What the row now says.

    **Answered from the write rather than from a re-read, and the port is the
    reason.** `attach_title` writes exactly what it is given -- deliberately,
    since a hand resolution is an act rather than a walk's incidental
    observation -- and returns whether a row changed. A `True` from it means
    these three values are what the row holds, so a second statement to fetch
    them back would confirm the port's own contract at the cost of a round
    trip on every resolution. There is no `get`-by-id on `MediaItemRepository`
    to make it with, and adding one for a response body would be a port method
    with one caller.

    Deliberately not the queue entry's shape: an item that has just been
    resolved is no longer in the queue, so rendering it as an
    `UnmatchedItemResponse` would be a model whose name is false of the one
    thing it is used for.
    """

    id: uuid.UUID
    title_id: uuid.UUID
    episode_id: uuid.UUID | None = None
