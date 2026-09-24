"""TMDb's franchise grouping."""

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class Collection(DomainModel):
    id: uuid.UUID = Field(default_factory=new_id)
    # The *only* thing that makes a re-derivation an update rather than a
    # duplicate: the derivation mints a fresh UUIDv7 per sighting, exactly as
    # ingest does for seasons.
    tmdb_id: int | None = None
    name: str = Field(min_length=1)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
