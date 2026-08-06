"""`collections` -- TMDb's movie franchise grouping.

Carries a `set_updated_at` trigger for `people`'s reason: written by a staged
`INSERT ... ON CONFLICT DO UPDATE`, a path `onupdate=` never reaches.

**Movies only.** `belongs_to_collection` is a field of `/movie/{id}` with no
`/tv/{id}` counterpart -- verified against the recorded payloads. So
`titles.collection_id` is NULL on every series row, permanently, and the
schema deliberately does **not** enforce that with a
`CHECK (collection_id IS NULL OR kind = 'movie')` on `titles`: the constraint
would encode a claim about TMDb's product decisions, and the day TMDb adds a
series equivalent (or M9's admin API offers a hand-curated grouping) it is a
migration on the catalog table plus a rewrite. `attach_titles`' own
`WHERE t.kind = 'movie'` and a CollectionRepository contract case carry the
property instead. Measured-and-declined, so it is not "fixed" in later.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base


class CollectionRow(Base):
    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # The upsert's ON CONFLICT target and resolve_tmdb_ids' lookup.
        # Partial, so the staged upsert must repeat `WHERE tmdb_id IS NOT
        # NULL` -- db/staging.py's first trap.
        #
        # Not composite with anything: `belongs_to_collection.id` is one id
        # space because there is no series equivalent, so ADR-0011's
        # movies-and-series-collide hazard does not arise. Named because its
        # absence is otherwise indistinguishable from having forgotten
        # ADR-0011.
        Index(
            "ix_collections_tmdb_id",
            "tmdb_id",
            unique=True,
            postgresql_where=text("tmdb_id IS NOT NULL"),
        ),
        CheckConstraint("name <> ''", name="ck_collections_name_not_empty"),
    )
