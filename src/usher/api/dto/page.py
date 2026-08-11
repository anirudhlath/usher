"""PRD 07's page envelope: the items, and where to resume.

Two fields, and the argument for this file is mostly about the third one that
is not here.

**No `total`.** A count over a filtered 1.3M-row catalog is a sequential scan,
paid on *every* page, for a number a client renders once and a keyset page
cannot use for anything -- there is no page N to jump to. PRD 07 rules out
offset paging on measured grounds (`list_unmatched`'s `OFFSET` is 43.7 ms at
offset 0 and 388.9 ms at offset 1,126,574), and a `total` is the same cost
arriving through a different clause. `/browse`'s facet counts are a different
question, over a different aggregate, and are group B's.

**`next_cursor` is `str | None` and is always present**, which is deliberately
*not* the convention the rest of `api/dto/` keeps. Elsewhere an empty value is
an absent key (`RowCard` carries no artwork field at all rather than a null
one, and `ProblemResponse.errors` is dropped when there is nothing to say).
Here a client takes both arms on every listing it renders, so "the key is
missing" and "there is no next page" would be the same bytes on the wire and a
client would learn the difference by guessing.

**Generic over the item type**, so `/openapi.json` describes `TitleSummary[]`
rather than `{"type": "object"}` -- the same argument `api/dto/health.py` made
for typing the health responses, and worth restating because the untyped
spelling costs nothing at the server and costs a generated client every field.
"""

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
