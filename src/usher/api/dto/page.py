"""PRD 07's page envelope: the items, and where to resume."""

from pydantic import BaseModel


class Page[ItemT](BaseModel):
    """One page of a keyset-paged listing.

    `next_cursor` is opaque: it is `usher.api.cursor`'s artefact, a client
    hands it back unread, and nothing about its contents is part of this
    contract. See ADR-0034.
    """

    items: list[ItemT]
    next_cursor: str | None = None


__all__ = ["Page"]
