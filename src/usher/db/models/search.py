"""The semantic half's two tables, plus M9's narrow name table."""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    column,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.enums import SearchNameKind

#: The character bound on `title_search_names.name`: Postgres refuses a btree entry
#: over `BTMaxItemSize` (2,704 bytes on the standard 8 kB page), and
#: `ix_title_search_names_name_lower_prefix` is a btree over `lower(name)`.
SEARCH_NAME_MAX_CHARS = 512

#: The one place the vector width is written down on the storage side.
EMBEDDING_DIMENSIONS = 1024


class TitleEmbeddingRow(Base):
    """One title's vector, and the two facts that make staleness a query."""

    __tablename__ = "title_embeddings"

    # The primary key *is* the foreign key: one vector per title, and the absence of a
    # row is itself the first disjunct of the stale predicate.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIMENSIONS))
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    # md5 hex of the exact assembled text. Deliberately `Text` with a
    # not-empty CHECK rather than `String(32)` with a length CHECK: pinning
    # the digest's width into the schema makes changing the digest a
    # migration, and the scheme's whole value is that a change to *what* is
    # hashed invalidates rows through the predicate instead.
    source_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # No trigger and no `onupdate=`: the one writer sets this explicitly, with
    # `now()` rather than `clock_timestamp()`, because nothing computes an
    # interval against it and a batch whose rows share one instant is the more
    # honest record of a batch.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("model_name <> ''", name="ck_title_embeddings_model_name_not_empty"),
        CheckConstraint(
            "source_fingerprint <> ''", name="ck_title_embeddings_fingerprint_not_empty"
        ),
        # HNSW at pgvector's own defaults: nothing about this catalog's size or
        # recall has given a reason to depart from them.
        Index(
            "ix_title_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
        # **Partial on the same predicate `stored_model_names` carries.** That
        # read is a `DISTINCT` over this column, and with no index it walks a
        # heap holding 1024-lane vectors inline to name one string. The
        # predicate keeps the index to the population the guard is about, so a
        # written refusal -- never a seed -- costs nothing to keep in it.
        Index(
            "ix_title_embeddings_model_name",
            "model_name",
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )


class TitleNeighborRow(Base):
    """A precomputed "more like this" list, one row per (title, neighbour) pair.

    `GET /titles/{id}/similar` is an index scan rather than a similarity
    computation, so the freshness of these rows is a property of when the batch
    last ran -- which is why `computed_at` is written by the batch and is the
    only timestamp here.

    **The one derived artefact that does not carry the fingerprint of its
    input.** No per-row predicate can say a neighbour list is stale, because a
    title's neighbours change when *some other title* gets an embedding. An
    oldest-`computed_at` reading stands in, and it is the weaker guarantee.
    """

    __tablename__ = "title_neighbors"

    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    neighbor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    # The batch's own ordering, stored rather than re-derived. Reading back
    # `ORDER BY score DESC` reproduces it only up to float ties, and a tie
    # broken differently on two reads shows a client two different "most
    # similar" titles for the same catalog.
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # **What this score *means*, as 32 hex characters** — the md5 of `_WEIGHTS`,
    # `_NEIGHBORS_PER_TITLE` and `_CANDIDATE_POOL`, minted by
    # `usher.services.similar.blend_fingerprint()`.
    blend_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("title_id", "neighbor_id", name="pk_title_neighbors"),
        # The cascade's own lookup. Postgres implements ON DELETE CASCADE by
        # finding referencing rows *by that column*, and the primary key leads
        # with `title_id`, so without this every title deletion sequentially
        # scans this table.
        Index("ix_title_neighbors_neighbor_id", "neighbor_id"),
        # `NeighborRebuildJob.last_done()` is `min(computed_at)`, asked once a
        # tick forever. Without this it is a sequential scan of the whole
        # table to learn that a job is not due; with it the planner takes the
        # first live entry off the index.
        Index("ix_title_neighbors_computed_at", "computed_at"),
        # No `(title_id, rank)` index. The read is `WHERE title_id = :id
        # ORDER BY rank`, and the primary key's leading column already serves
        # the lookup; what remains is a sort of at most `limit` rows, which
        # is single digits to low tens by construction. An index to avoid
        # that sort would be write cost for nothing.
        CheckConstraint("title_id <> neighbor_id", name="ck_title_neighbors_not_self"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_title_neighbors_score_range"),
        CheckConstraint("rank >= 0", name="ck_title_neighbors_rank_non_negative"),
    )


class TitleSearchNameRow(Base):
    """One name a title can be found by: an alias from the akas dump, or a person's."""

    __tablename__ = "title_search_names"

    # A surrogate key, unlike `title_embeddings`' and `genome_tags`'. There is
    # no natural one: `(title_id, name, kind, region, language)` admits two
    # identical akas rows from one dump, and a five-column primary key over a
    # 512-character text column is a btree entry this table already has a
    # CHECK about.
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # CASCADE, `title_embeddings`' case rather than `watch_states`': a search
    # name protects no user state and is fully re-derivable from the title
    # plus a loader.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[SearchNameKind] = mapped_column(
        enum_column(SearchNameKind, length=16), nullable=False
    )
    # IMDb `title.akas`' own `region` and `language`, nullable because most
    # rows carry one and not the other and plenty carry neither. NULL means
    # "not specific to a region", which is a different fact from any code.
    # Deliberately unconstrained beyond nullability: the vocabularies are the
    # dump's, and a CHECK here would be a migration the day IMDb adds a code.
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("name <> ''", name="ck_title_search_names_name_not_empty"),
        # The btree bound `SEARCH_NAME_MAX_CHARS` states, as a CHECK -- so an
        # over-long name is a classifiable refusal rather than an index error.
        CheckConstraint(
            f"length(name) <= {SEARCH_NAME_MAX_CHARS}",
            name="ck_title_search_names_name_within_btree_bound",
        ),
        # The cascade's own lookup, and the leading column of the
        # `(title_id, kind)` delete scope -- `ix_title_neighbors_neighbor_id`'s
        # argument two classes up.
        Index("ix_title_search_names_title_id", "title_id"),
        # **Tier 1 of the two-tier suggest, on the half that holds aliases and
        # people.** A `text_pattern_ops` btree over `lower(name)`, not a GIN
        # trigram index: suggest is a prefix match, and the btree answers one in
        # a fraction of the trigram path's time on a catalog this size.
        Index(
            "ix_title_search_names_name_lower_prefix",
            func.lower(column("name")).label("lower_name"),
            postgresql_ops={"lower_name": "text_pattern_ops"},
        ),
        # No index on `kind`. Two members and no selectivity, and the delete
        # scope's leading column is already indexed above.
    )
