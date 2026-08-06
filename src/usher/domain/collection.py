"""TMDb's franchise grouping, and the one thing it cannot express.

PRD 02: *"TMDb franchise grouping ('The Matrix Collection'). Powers franchise
rows and 'you own 2 of 4' completeness signals."*

**`belongs_to_collection` is a field of `/movie/{id}` and has no `/tv/{id}`
counterpart** -- verified against the recorded payloads, where
`series.json` has no such key and nothing plays its role. Three consequences,
written here because each of them is otherwise discovered rather than known:

1. `FranchiseProvider` fires on movies only, and on a television-only
   household PRD 06's condition (">= 2 owned titles in a collection") is
   unsatisfiable **by construction** rather than by absence of data. That is
   the distinction an operator debugging a missing row needs, so the provider
   says it and PRD 06 is annotated in the same commit.
2. **No series grouping is invented.** The three available fallbacks --
   grouping by name prefix, grouping by `networks`, and reading Emby's
   `TmdbCollection` provider-id key (real, observed in M4's key-space sweep,
   and a *movie* collection id attached to whatever Emby chose) -- each
   produce a populated, plausible, wrong row. This milestone's opening
   section is entirely about that failure.
3. `titles.collection_id` is NULL on every series row, permanently. A series
   row carrying a non-NULL one is a defect, and it is the fourth wrong
   implementation `CollectionRepository`'s contract suite must kill.

**No `overview` and no artwork.** `belongs_to_collection` is `{id, name,
poster_path, backdrop_path}`; the overview and `parts[]` are on
`/collection/{id}`, a second network call boundary call 4 refuses, and artwork
is M9's whole table. Boundary call 3 settled the shape of this choice one
route over: *"The choice is between an always-null field and no field."*
"""

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class Collection(DomainModel):
    id: uuid.UUID = Field(default_factory=new_id)
    # ADR-0003 again: the *only* thing that makes a re-derivation an update
    # rather than a duplicate, because the derivation mints a fresh UUIDv7 per
    # sighting exactly as ingest does for seasons.
    tmdb_id: int | None = None
    name: str = Field(min_length=1)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
