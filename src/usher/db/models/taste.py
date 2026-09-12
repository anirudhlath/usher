"""Taste-side derived state: the MovieLens tag genome, and the per-user taste centroid."""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.ports.bulk import GENOME_TAG_COUNT


class GenomeScoreRow(Base):
    """One title's MovieLens tag-genome vector."""

    __tablename__ = "genome_scores"

    # The primary key *is* the foreign key, exactly as `title_embeddings`:
    # one vector per title, and a surrogate id would add a column nothing
    # reads while permitting two rows per title -- a state no consumer could
    # interpret.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), primary_key=True
    )
    relevance: Mapped[list[float]] = mapped_column(HALFVEC(GENOME_TAG_COUNT), nullable=False)
    genome_revision: Mapped[str] = mapped_column(Text, nullable=False)
    # `computed_at` and no `updated_at`, and no trigger -- following `title_neighbors`,
    # which has none either because "a neighbour row is a batch artefact: it is
    # computed, wholesale, by one pass, and `computed_at` is the only timestamp that
    # means anything about it." Identical here.
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GenomeTagRow(Base):
    """What each of `genome_scores.relevance`'s 1,128 lanes means."""

    __tablename__ = "genome_tags"

    # MovieLens' own lane index, never minted here -- `autoincrement=False` so
    # a `tag_id` this project invented is not even expressible. The primary
    # key *is* the natural key, exactly as `genome_scores.title_id` is: a
    # surrogate id would add a column nothing reads while permitting two rows
    # for one lane, which is a state no consumer could interpret.
    tag_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    tag: Mapped[str] = mapped_column(Text, nullable=False)
    genome_revision: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # `1…GENOME_TAG_COUNT`, the same constant `genome_scores.relevance`
        # declares its width with, so the two cannot drift. The lower bound is
        # as load-bearing as the upper: `tag_id - 1` is a list index, and `0`
        # would make lane -1 addressable.
        CheckConstraint(
            f"tag_id BETWEEN 1 AND {GENOME_TAG_COUNT}",
            name="ck_genome_tags_tag_id_in_vocabulary",
        ),
        # An empty name is a lane that reads as labelled and says nothing --
        # worse than a missing row, which the contiguity check would catch.
        CheckConstraint("tag <> ''", name="ck_genome_tags_tag_not_empty"),
        # An empty revision matches no `genome_scores` row, so every read of
        # the vocabulary would refuse and the table would be silently inert.
        CheckConstraint("genome_revision <> ''", name="ck_genome_tags_revision_not_empty"),
    )


class UserTasteRow(Base):
    """One user's taste centroid, and the fingerprint that invalidates it."""

    __tablename__ = "user_taste"

    # The primary key *is* the foreign key, exactly as `title_embeddings.title_id` and
    # `genome_scores.title_id` are.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    centroid: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIMENSIONS))
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How many titles went into the mean. Makes the refusal *countable*: a
    # gauge reading "N households have too little history for a centroid" is
    # how an operator learns that half the deployment gets no taste rows,
    # which is otherwise indistinguishable from taste rows nobody clicked.
    title_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # The artefact's age, for the operator -- and deliberately *not* the
    # invalidation. A TTL here would recompute a centroid whose inputs have
    # not moved and serve a stale one whose inputs have.
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
