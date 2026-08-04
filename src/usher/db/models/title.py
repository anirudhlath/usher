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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind

#: Columns that exist on the row and deliberately have no `Title` field.
#: A search document is not domain state -- it is an index artefact derived
#: from domain state, and a `Title` carrying a `tsvector` would put a
#: PostgreSQL full-text type in `usher.domain`, which imports nothing.
#: Membership here is the deliberate act: a *bookkeeping* column added
#: without being named here still breaks every read, loudly, which is the
#: property the 1:1 rule exists for.
#:
#: Three call sites consume this, and the second is the one that gets missed
#: -- `_to_domain` (a read), `update()`'s mutation loop (a *write*), and
#: `test_title_and_title_row_have_matching_field_sets`.
DERIVED_COLUMNS: frozenset[str] = frozenset({"search_document"})


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

    # The FK it has been waiting for since M1. PRD 02: "the one artefact that
    # exists today is titles.collection_id, a bare nullable UUID with no
    # foreign key that nothing in src/ ever writes; it is the column waiting
    # for the table, not evidence of one." M7 lands the table.
    #
    # SET NULL, and the two refused alternatives are the argument. CASCADE
    # would delete the *films* when a franchise grouping is deleted -- wrong
    # in kind, against PRD 02's own "the catalog outlives the servers".
    # RESTRICT would refuse every collection delete, because a collection with
    # no members is never written, so the refusal fires unconditionally and is
    # a table nothing can delete from. SET NULL is media_items.title_id's
    # precedent verbatim: the row is worth keeping and it just loses the link,
    # and DeriveService re-attaches it on the next pass, so a NULLed link is
    # self-healing rather than lost.
    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("collections.id", ondelete="SET NULL")
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

    # PostgreSQL recomputes this inside the same statement that writes any of
    # its six inputs, so there is no code path -- not a bulk COPY, not a
    # hand-written UPDATE, not a future migration -- that can write a title
    # and skip its document. That is the whole reason it is a generated
    # column rather than a trigger, a job, or a queue: half of M6's freshness
    # problem is deleted rather than solved.
    #
    # `Computed(..., persisted=True)` is what tells SQLAlchemy the column is
    # STORED rather than VIRTUAL and that it is not ours to write. It is
    # **not** sufficient on its own: `update()`'s mutation loop assigns every
    # column by name off `TitleRow.__table__.columns`, which reaches this one
    # regardless -- see DERIVED_COLUMNS above and db/repositories/title.py.
    #
    # Typed `str | None` because that is what asyncpg hands back for a
    # tsvector and because nothing in `src/` ever reads this attribute in
    # Python -- every consumer references it in SQL. `_to_domain` filters it
    # out by name before it can reach `Title`.
    #
    # The expression is duplicated between here and migration fa2b6c1e9d30
    # rather than shared, because an Alembic migration must not import
    # application code that can change under it. What keeps the two honest is
    # `test_migration_matches_the_orm_metadata` plus
    # `test_the_stored_document_equals_a_freshly_computed_one`, which reads
    # the live expression out of `pg_attrdef`.
    search_document: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(name, '')), 'A') "
            "|| setweight(to_tsvector('english', coalesce(original_name, '')), 'A') "
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
        # `ON CONFLICT (tmdb_id, kind) WHERE tmdb_id IS NOT NULL DO UPDATE
        # ...` -- or Postgres rejects the upsert with "no unique or
        # exclusion constraint matching the ON CONFLICT specification".
        # M2/M4's upsert loaders must do this for tmdb_id/imdb_id/tvdb_id.
        #
        # Composite and partial. `tmdb_id` alone is not unique in reality:
        # TMDb keys movies and series in separate id spaces that both land
        # in this column, and 26,968 of the 56,975 distinct TMDb series ids
        # Wikidata knows are also live TMDb movie ids (measured 2026-07-30).
        # A single-column unique index silently blocked 47.3% of TV from
        # ever getting a tmdb_id during M2's Phase 2 crosswalk. See
        # ADR-0011. Column order is (tmdb_id, kind), not (kind, tmdb_id),
        # so the index also serves a bare `WHERE tmdb_id = ?` diagnostic
        # scan; verified that `WHERE tmdb_id = 1 AND kind = 'movie'` plans
        # as `Index Scan using ix_titles_tmdb_id_kind`.
        #
        # imdb_id keeps its single-column index: `tt` ids are one global
        # namespace covering film and television alike. tvdb_id keeps its
        # own for now -- M2 only ever writes TheTVDB *series* ids (Wikidata
        # P4835), so the equivalent hazard is theoretical rather than
        # measured; see ADR-0011's consequences.
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
        # GIN over the tsvector, with the pending list turned off.
        #
        # `fastupdate` defaults to on, which defers index maintenance into an
        # unsorted pending list that *every query then scans linearly* until
        # autovacuum flushes it. That is exactly wrong for a table written in
        # million-row bursts and queried during them. Verified with
        # `pageinspect`: after 5,000 inserts, `fastupdate = off` had
        # `n_pending_pages = 0 / n_pending_tuples = 0` against `50 / 5000`
        # for the default, and on the read side a 1.6 MB pending list cost
        # 231 buffers against 30 -- 7.7x read amplification on the index
        # stage. `postgresql_with` is native Alembic here -- verified by
        # compiling the DDL, not by reading the docs.
        #
        # **This index should be suspended during a first bootstrap and the
        # edit is deliberately Task 7's**, not because the decision is
        # unclear but because `bulk.py`'s `_SUSPENDABLE_INDEXES` holds
        # literal `CREATE INDEX` strings: an entry whose text drifts from the
        # migration silently rebuilds a *different* index, and splitting the
        # dict's new entries across two tasks is how one of them drifts.
        # Task 7 adds them together, with the round-trip test that pins each
        # string against what Postgres actually built.
        Index(
            "ix_titles_search_document",
            "search_document",
            postgresql_using="gin",
            postgresql_with={"fastupdate": "off"},
        ),
        # The type-ahead path's whole index. Directly on `titles`, because
        # PRD 05's narrow `title_search_names(title_id, name, kind,
        # popularity)` table is refused (boundary call 3): its justification
        # is aliases and people names, neither of which has a data source in
        # M6, so it would hold exactly one row per title duplicating four
        # columns of this one -- a second copy to keep fresh, which is the
        # problem this milestone exists to eliminate.
        #
        # GIN, not the GiST PRD 05 specifies -- but the two answer different
        # questions and only one of them is settled. For "which rows are
        # candidates" GIN is not close: at 2.08M names it is ~110x faster on
        # the `%` path (1.671 ms vs 182.5 ms), builds in 7.5 s vs 23.1 s, and
        # is 69 MB vs 244 MB. For "the N nearest in distance order" GIN has
        # no operator class at all -- `ORDER BY name <-> q` seq-scans at
        # 3,989.9 ms where GiST answers from the index. GIN is right here
        # because the suggest path caps candidates before re-ranking, which
        # removes GIN's only exposure; a path that ever needs KNN needs a
        # GiST index rather than a tuning change. See the migration docstring.
        #
        # On the raw column: pg_trgm folds case while generating trigrams,
        # unlike the btree two entries up. `original_name` gets no index of
        # its own -- see the migration's docstring for the three reasons and
        # for the measurement that would reverse it.
        Index(
            "ix_titles_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # FranchiseProvider's whole read (CollectionRepository.list_owned),
        # and the referencing-side lookup collections' SET NULL performs on
        # every delete -- Postgres implements SET NULL by finding referencing
        # rows *by this column*, and nothing else here leads with it.
        #
        # PRD 02 deferred this index to M9 ("No index yet -- deferred for M9
        # alongside media_items' added_at/last_seen_at/available"). M7 needs it
        # now and the deferral is retracted with its reason in the same commit
        # rather than silently overridden.
        #
        # Partial: NULL on all 371,310 series rows -- belongs_to_collection is
        # movies-only -- and on the majority of the 899,828 movie rows. That
        # is ix_titles_popularity's argument, one column over.
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
