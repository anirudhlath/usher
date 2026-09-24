"""`people` and `credits`."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.people import CreditKind, CreditSource


class PersonRow(Base):
    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    # IMDb's `nconst`. `Text` rather than a bounded string for the reason
    # `titles.imdb_id` is: the id is somebody else's format and a width is a
    # claim about it. Nullable and partially unique -- see `ix_people_imdb_id`
    # below, and `domain/people.py` for why the *pair* being nullable is the
    # merge design rather than laxity.
    imdb_id: Mapped[str | None] = mapped_column(Text)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable because `created_by[]` entries carry no `known_for_department` while
    # `credits.cast[]` entries do -- verified against the recorded payloads.
    known_for_department: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # THE dedup key. Deduping on `name` instead collapses two directors who
        # share one.
        Index(
            "ix_people_tmdb_id",
            "tmdb_id",
            unique=True,
            postgresql_where=text("tmdb_id IS NOT NULL"),
        ),
        # The IMDb half of the same key, and partial for the same reason
        # `ix_titles_imdb_id` is: NULL never collides with NULL, and the explicit WHERE
        # is what lets Postgres use the index for the IS NOT NULL lookups an importer's
        # resolve step makes.
        Index(
            "ix_people_imdb_id",
            "imdb_id",
            unique=True,
            postgresql_where=text("imdb_id IS NOT NULL"),
        ),
        CheckConstraint("imdb_id IS NULL OR imdb_id <> ''", name="ck_people_imdb_id_not_empty"),
        CheckConstraint("name <> ''", name="ck_people_name_not_empty"),
        CheckConstraint("sort_name <> ''", name="ck_people_sort_name_not_empty"),
    )


class CreditRow(Base):
    __tablename__ = "credits"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # CASCADE: a credit with no person is not a record worth keeping. It carries
    # no user state and is re-derivable from a cached payload in one pass, which
    # is `seasons.title_id`'s argument verbatim -- the delete rule follows what a
    # row protects, and this one protects nothing.
    person_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    # CASCADE, and deliberately the opposite of `watch_states.title_id`'s RESTRICT.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[CreditKind] = mapped_column(enum_column(CreditKind, length=8), nullable=False)
    # NOT NULL and no server default, backfilled by the migration to the TMDb member
    # because every row this table held when the column landed came from `DeriveService`
    # reading `raw_payloads`.
    source: Mapped[CreditSource] = mapped_column(
        enum_column(CreditSource, length=8), nullable=False
    )

    tmdb_credit_id: Mapped[str | None] = mapped_column(Text)

    character: Mapped[str | None] = mapped_column(Text)
    job: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    billing_order: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Every read that filters by title, plus titles' own CASCADE lookup.
        Index("ix_credits_title_id", "title_id"),
        # list_for_person ("what else did person P work on"), plus people's
        # own CASCADE lookup. Same argument as ix_watch_states_episode_id.
        Index("ix_credits_person_id", "person_id"),
        # A CONSTRAINT, described as one rather than as a query path: nothing reads this
        # as an index.
        Index(
            "ix_credits_tmdb_credit_id",
            "tmdb_credit_id",
            unique=True,
            postgresql_where=text("tmdb_credit_id IS NOT NULL"),
        ),
        # **The dedup key for every source that is not TMDb.** The index above is
        # partial over `tmdb_credit_id IS NOT NULL`, so a row from a source that
        # mints no credit id falls outside it and would re-insert on every pass.
        Index(
            "ix_credits_source_natural_key",
            "title_id",
            "source",
            "billing_order",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("source <> 'tmdb'"),
        ),
        # No index on `kind`: two values, on a table whose every read already
        # filters on title_id or person_id, and Postgres seq-scans a majority
        # value whether or not it is indexed.
        CheckConstraint(
            "billing_order IS NULL OR billing_order >= 0",
            name="ck_credits_billing_order_non_negative",
        ),
        CheckConstraint(
            "tmdb_credit_id IS NULL OR tmdb_credit_id <> ''",
            name="ck_credits_tmdb_credit_id_not_empty",
        ),
    )
