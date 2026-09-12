"""Catalog tables."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    column,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind

# : Columns that exist on the row and deliberately have no `Title` field.
DERIVED_COLUMNS: frozenset[str] = frozenset({"search_document", "credit_names"})


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

    # ARRAY(Text) accepts a Python tuple on write and always returns a list on read
    # (verified against real Postgres) -- Mapped[list[str]] here is correct for both
    # directions even though the domain model above these rows uses tuple[str, ...].
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

    # **Five columns where there were three, because three of them had two writers each
    # and no way to say which one wrote a row.** `bulk/imdb.py` wrote IMDb's
    # `numVotes`/`averageRating` here and `tmdb/mapping.py` wrote TMDb's
    # `vote_count`/`vote_average` over the top, into the same column, counted over
    # different electorates -- ~38x apart over one identified population counted both
    tmdb_vote_average: Mapped[float | None] = mapped_column(Float)
    tmdb_vote_count: Mapped[int | None] = mapped_column(Integer)
    tmdb_popularity: Mapped[float | None] = mapped_column(Float)
    imdb_average_rating: Mapped[float | None] = mapped_column(Float)
    imdb_num_votes: Mapped[int | None] = mapped_column(Integer)

    # The FK it has been waiting for since M1.
    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("collections.id", ondelete="SET NULL")
    )

    # Weight class B's input, denormalised onto `titles` because a stored generated
    # expression cannot reach another table -- measured, three spellings, in migration
    # fe1d40c8b7a3's docstring.
    credit_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'")
    )

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

    # PostgreSQL recomputes this inside the same statement that writes any of its six
    # inputs, so there is no code path -- not a bulk COPY, not a hand-written UPDATE,
    # not a future migration -- that can write a title and skip its document.
    search_document: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(name, '')), 'A') "
            "|| setweight(to_tsvector('english', coalesce(original_name, '')), 'A') "
            "|| setweight(to_tsvector('english', usher_array_text(credit_names)), 'B') "
            "|| setweight(to_tsvector('english', coalesce(overview, '')), 'C') "
            "|| setweight(to_tsvector('english', coalesce(tagline, '')), 'C') "
            "|| setweight(to_tsvector('english', usher_array_text(genres)), 'D') "
            "|| setweight(to_tsvector('english', usher_array_text(keywords)), 'D')",
            persisted=True,
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        # Kept as a fallback for plain ORM-session updates and as a signal of intent,
        # but it is NOT what keeps this column correct for bulk writes -- SQLAlchemy's
        # onupdate is a Core-side feature with no effect on raw SQL / COPY / ON CONFLICT
        # DO UPDATE unless every caller remembers to list it explicitly (M2/M4's bulk
        # ingest is ON CONFLICT DO UPDATE by definition).
        nullable=False,
    )

    __table_args__ = (
        # Partial unique indexes, not a plain UNIQUE constraint: NULL never collides
        # with NULL under a normal unique index anyway, but making the WHERE explicit is
        # what lets Postgres use this index for the lookup queries that already filter
        # on "IS NOT NULL".
        Index(
            "ix_titles_tmdb_id_kind",
            "tmdb_id",
            "kind",
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
        # **`ix_titles_popularity` was here and is dropped in `ffc`, because the
        # reasoning that shaped it does not survive contact with the planner.** It read,
        # in part: *"Excluding NULLs means there is nothing to place 'last' inside the
        # index at all, so a backward scan is directly usable."* That is the load-
        # bearing claim and it is **refuted**, measured on pg17.10 against 1,271,570
        Index("ix_titles_name_lower_year", text("lower(name)"), "year"),
        # GIN over the tsvector, with the pending list turned off.
        Index(
            "ix_titles_search_document",
            "search_document",
            postgresql_using="gin",
            postgresql_with={"fastupdate": "off"},
        ),
        # The type-ahead path's tier-2 index.
        Index(
            "ix_titles_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # **Tier 1 of the two-tier suggest ADR-0002's failed gate obliges, and
        # `ix_titles_name_lower_year` two entries up is NOT this index.** That one is
        # `Index(..., text("lower(name)"), "year")` with the *default* opclass, which
        # under this database's collation cannot answer `LIKE 'pre%'` at all -- measured
        # on `pgvector/pgvector:pg17` at the pre-`m09a` schema: with `enable_seqscan =
        Index(
            "ix_titles_name_lower_prefix",
            func.lower(column("name")).label("lower_name"),
            postgresql_ops={"lower_name": "text_pattern_ops"},
        ),
        # FranchiseProvider's whole read (CollectionRepository.list_owned), and the
        # referencing-side lookup collections' SET NULL performs on every delete --
        # Postgres implements SET NULL by finding referencing rows *by this column*, and
        # nothing else here leads with it.
        Index(
            "ix_titles_collection_id",
            "collection_id",
            postgresql_where=text("collection_id IS NOT NULL"),
        ),
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
            "tmdb_vote_count IS NULL OR tmdb_vote_count >= 0",
            name="ck_titles_tmdb_vote_count_non_negative",
        ),
        CheckConstraint(
            "tmdb_popularity IS NULL OR tmdb_popularity >= 0",
            name="ck_titles_tmdb_popularity_non_negative",
        ),
        CheckConstraint(
            "tmdb_vote_average IS NULL OR tmdb_vote_average BETWEEN 0 AND 10",
            name="ck_titles_tmdb_vote_average_range",
        ),
        CheckConstraint(
            "imdb_num_votes IS NULL OR imdb_num_votes >= 0",
            name="ck_titles_imdb_num_votes_non_negative",
        ),
        CheckConstraint(
            "imdb_average_rating IS NULL OR imdb_average_rating BETWEEN 0 AND 10",
            name="ck_titles_imdb_average_rating_range",
        ),
        CheckConstraint("name <> ''", name="ck_titles_name_not_empty"),
        CheckConstraint("sort_name <> ''", name="ck_titles_sort_name_not_empty"),
    )
