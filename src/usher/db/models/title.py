"""Catalog tables."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind


class TitleRow(Base):
    __tablename__ = "titles"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[TitleKind] = mapped_column(String(16), nullable=False)

    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(String(16))
    tvdb_id: Mapped[int | None] = mapped_column(Integer)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text)
    sort_name: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    release_date: Mapped[date | None] = mapped_column(Date)
    end_year: Mapped[int | None] = mapped_column(Integer)

    overview: Mapped[str | None] = mapped_column(Text)
    tagline: Mapped[str | None] = mapped_column(Text)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    # Mapped[ProductionStatus | None], matching how kind/enrichment_state
    # are typed as their enum despite also being plain String columns
    # underneath -- not left as a bare str | None by design.
    status: Mapped[ProductionStatus | None] = mapped_column(String(32))

    # ARRAY(Text) accepts a Python tuple on write and always returns a list
    # on read (verified against real Postgres) -- Mapped[list[str]] here is
    # correct for both directions even though the domain model above these
    # rows uses tuple[str, ...]. See the note above this class.
    genres: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    original_language: Mapped[str | None] = mapped_column(String(16))
    spoken_languages: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    origin_countries: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    content_rating: Mapped[str | None] = mapped_column(String(32))

    community_rating: Mapped[float | None] = mapped_column(Float)
    vote_count: Mapped[int | None] = mapped_column(Integer)
    popularity: Mapped[float | None] = mapped_column(Float)

    collection_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    enrichment_state: Mapped[EnrichmentState] = mapped_column(
        String(16), nullable=False, default=EnrichmentState.SKELETON
    )
    # Non-null => the last enrichment attempt failed; enrichment_state is
    # left untouched either way. ADR-0008.
    enrichment_error: Mapped[str | None] = mapped_column(Text)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    field_provenance: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_titles_tmdb_id",
            "tmdb_id",
            unique=True,
            postgresql_where=text("tmdb_id IS NOT NULL"),
        ),
        Index(
            "ix_titles_imdb_id",
            "imdb_id",
            unique=True,
            postgresql_where=text("imdb_id IS NOT NULL"),
        ),
        Index("ix_titles_sort_name", "sort_name"),
        Index("ix_titles_enrichment_state", "enrichment_state"),
        Index("ix_titles_popularity", "popularity"),
        # Mirrors the domain model's Field(ge=0) / Field(ge=0, le=10) /
        # Field(min_length=1) constraints -- see the Title commit.
        CheckConstraint("year IS NULL OR year >= 0", name="ck_titles_year_non_negative"),
        CheckConstraint(
            "end_year IS NULL OR end_year >= 0", name="ck_titles_end_year_non_negative"
        ),
        CheckConstraint(
            "runtime_minutes IS NULL OR runtime_minutes >= 0",
            name="ck_titles_runtime_minutes_non_negative",
        ),
        CheckConstraint(
            "vote_count IS NULL OR vote_count >= 0", name="ck_titles_vote_count_non_negative"
        ),
        CheckConstraint(
            "popularity IS NULL OR popularity >= 0", name="ck_titles_popularity_non_negative"
        ),
        CheckConstraint(
            "community_rating IS NULL OR community_rating BETWEEN 0 AND 10",
            name="ck_titles_community_rating_range",
        ),
        CheckConstraint("name <> ''", name="ck_titles_name_not_empty"),
        CheckConstraint("sort_name <> ''", name="ck_titles_sort_name_not_empty"),
    )
