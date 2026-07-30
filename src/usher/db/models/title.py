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

from usher.db.base import Base, enum_column
from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind


class TitleRow(Base):
    __tablename__ = "titles"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[TitleKind] = mapped_column(enum_column(TitleKind, length=16), nullable=False)

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
    # are typed as their enum despite also being plain String-backed columns
    # underneath -- not left as a bare str | None by design. See enum_column.
    status: Mapped[ProductionStatus | None] = mapped_column(
        enum_column(ProductionStatus, length=32)
    )

    # ARRAY(Text) accepts a Python tuple on write and always returns a list
    # on read (verified against real Postgres) -- Mapped[list[str]] here is
    # correct for both directions even though the domain model above these
    # rows uses tuple[str, ...]. See the note above this class.
    #
    # server_default (not just the ORM-side default= below) so a raw INSERT
    # or COPY that never mentions this column -- M2's entire bulk-load path,
    # by construction -- gets '{}' instead of a NOT NULL violation. Verified:
    # without it, `INSERT INTO titles (id, kind, name, sort_name) VALUES
    # (...)` fails on "null value in column \"genres\"".
    #
    # No GIN index yet: M9's faceted /browse (07-client-api.md) needs one for
    # facet counts at scale (measured: 78.7 ms/300k rows seq-scanned, ~3.3 s
    # projected at 12.7M) but CREATE INDEX CONCURRENTLY can add it online
    # with no table rewrite whenever M9 lands, so it is deferred, not
    # designed away.
    genres: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    original_language: Mapped[str | None] = mapped_column(String(16))
    spoken_languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    origin_countries: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    content_rating: Mapped[str | None] = mapped_column(String(32))

    community_rating: Mapped[float | None] = mapped_column(Float)
    vote_count: Mapped[int | None] = mapped_column(Integer)
    popularity: Mapped[float | None] = mapped_column(Float)

    # No index yet -- deferred for M9 alongside media_items' added_at/
    # last_seen_at/available; see the note in db/models/source.py.
    collection_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    enrichment_state: Mapped[EnrichmentState] = mapped_column(
        enum_column(EnrichmentState, length=16),
        nullable=False,
        default=EnrichmentState.SKELETON,
        server_default=text("'skeleton'"),
    )
    # Non-null => the last enrichment attempt failed; enrichment_state is
    # left untouched either way. ADR-0008.
    enrichment_error: Mapped[str | None] = mapped_column(Text)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    field_provenance: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        # Kept as a fallback for plain ORM-session updates and as a signal
        # of intent, but it is NOT what keeps this column correct for bulk
        # writes -- SQLAlchemy's onupdate is a Core-side feature with no
        # effect on raw SQL / COPY / ON CONFLICT DO UPDATE unless every
        # caller remembers to list it explicitly (M2/M4's bulk ingest is
        # ON CONFLICT DO UPDATE by definition). A BEFORE UPDATE trigger
        # (see the initial migration) is what actually guarantees it.
        nullable=False,
    )

    __table_args__ = (
        # Partial unique indexes, not a plain UNIQUE constraint: NULL never
        # collides with NULL under a normal unique index anyway, but making
        # the WHERE explicit is what lets Postgres use this index for the
        # lookup queries that already filter on "IS NOT NULL". It also means
        # an upsert against this column must repeat the same predicate --
        # `ON CONFLICT (tmdb_id) WHERE tmdb_id IS NOT NULL DO UPDATE ...` --
        # or Postgres rejects the upsert with "no unique or exclusion
        # constraint matching the ON CONFLICT specification". M2/M4's
        # upsert loaders must do this for tmdb_id/imdb_id/tvdb_id.
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
        # PRD 02 (Identity) lists all three provider IDs as unique-indexed
        # attributes; tvdb_id had no index at all until this pass. Adding it
        # before any real tvdb_id data lands matters -- once duplicates
        # exist, this unique index can't be added without a dedup pass
        # first.
        Index(
            "ix_titles_tvdb_id",
            "tvdb_id",
            unique=True,
            postgresql_where=text("tvdb_id IS NOT NULL"),
        ),
        Index("ix_titles_sort_name", "sort_name"),
        # Partial: excludes the majority 'skeleton' value. Postgres seq-scans
        # for a majority value regardless of whether it's indexed, so a full
        # index over enrichment_state is pure write cost during M2's bulk
        # load of millions of skeleton rows for zero query benefit (measured
        # 1,936 kB -> 40 kB at 300k rows, identical query plans either way).
        Index(
            "ix_titles_enrichment_state",
            "enrichment_state",
            postgresql_where=text("enrichment_state <> 'skeleton'"),
        ),
        # Descending and partial, not a plain ascending index: "most popular
        # first" is ORDER BY popularity DESC NULLS LAST, which a plain
        # ascending btree cannot serve in either scan direction (forward
        # gives ASC NULLS LAST, backward gives DESC NULLS FIRST -- neither
        # matches). Excluding NULLs means there is nothing to place "last"
        # inside the index at all, so a backward scan is directly usable.
        # Measured: the plain-ascending version fell back to a 24.8 ms seq
        # scan at 300k rows for a query a matching index serves in 0.19 ms,
        # and would cost ~340 MB at 12.7M rows indexing millions of
        # NULL-popularity skeleton rows no such query ever wants first.
        Index(
            "ix_titles_popularity",
            text("popularity DESC"),
            postgresql_where=text("popularity IS NOT NULL"),
        ),
        # PRD 03 stage 3 matches bulk-imported titles on normalised name +
        # year and calls this "why matching is fast and mostly offline" --
        # but sort_name has an explicit no-normalisation contract (Title's
        # own docstring) and ix_titles_sort_name is a plain btree on the raw
        # column, so neither can serve that lookup. Measured: a name+year
        # match seq-scanned at 14.6 ms at 300k rows, ~600 ms/item
        # extrapolated to 12.7M -- an expression index on the same
        # normalisation the matcher actually applies (lowercase; no
        # whitespace/punctuation folding) is what a btree can use.
        Index("ix_titles_name_lower_year", text("lower(name)"), "year"),
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
