"""TMDb's franchise grouping, and the one thing it cannot express."""

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
