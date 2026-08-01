"""Season and episode tables.

Both carry a `set_updated_at` trigger, added by this milestone's migration
-- which means `tests/integration/test_migrations.py`'s exact-set assertion
grows from three triggers to five, and that test is updated in the same
commit. The triggers exist for the same reason the first three do: these
tables are written by `INSERT ... ON CONFLICT DO UPDATE` from a staging
table, a path SQLAlchemy's `onupdate=` has no effect on.

Both tables are sized by the one real deployment measured: 32,409 series,
999,827 episodes. Every index here is justified against that number rather
than against a test fixture.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base


class SeasonRow(Base):
    __tablename__ = "seasons"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # CASCADE from titles, unlike watch_states: a season with no series is
    # not a record worth keeping -- it carries no user state, and it is
    # re-derivable from the provider payload in one call. ADR-0010's
    # reasoning applies to what a row *protects*, and this one protects
    # nothing. No standalone index on title_id: uq_seasons_title_season_number
    # below leads with it, so the FK's own referencing-side lookup uses that.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)

    name: Mapped[str | None] = mapped_column(Text)
    overview: Mapped[str | None] = mapped_column(Text)
    air_date: Mapped[date | None] = mapped_column(Date)
    episode_count: Mapped[int | None] = mapped_column(Integer)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("title_id", "season_number", name="uq_seasons_title_season_number"),
        CheckConstraint("season_number >= 0", name="ck_seasons_season_number_non_negative"),
        CheckConstraint(
            "episode_count IS NULL OR episode_count >= 0",
            name="ck_seasons_episode_count_non_negative",
        ),
    )


class EpisodeRow(Base):
    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    absolute_number: Mapped[int | None] = mapped_column(Integer)

    name: Mapped[str | None] = mapped_column(Text)
    overview: Mapped[str | None] = mapped_column(Text)
    air_date: Mapped[date | None] = mapped_column(Date)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # The lookup ingest performs once per episode item, 999,827 times a
        # night. Also the ON CONFLICT target for the staged upsert, which is
        # why it is a UniqueConstraint rather than a plain Index -- and it
        # leads with title_id, so titles' CASCADE finds this table's rows
        # through it without a second index.
        UniqueConstraint(
            "title_id",
            "season_number",
            "episode_number",
            name="uq_episodes_title_season_episode",
        ),
        # seasons' own CASCADE needs this; nothing else leads with season_id.
        Index("ix_episodes_season_id", "season_id"),
        # Partial like ix_titles_imdb_id -- NULL never collides with NULL and
        # an upsert against it would have to repeat the predicate -- but
        # deliberately NOT unique, which is where this departs from the
        # titles shape. Nothing in M4 looks an episode up by IMDb id: ingest
        # keys on (title_id, season_number, episode_number) above. Meanwhile
        # watch.py's own FK comment records that M4's matcher produces "two
        # episode trees" when a series is ingested twice under different
        # provider ids, and two trees enriched from two TMDb entries for the
        # same show carry the *same* episode IMDb ids. Under a unique index
        # that is an IntegrityError which aborts the whole staged COPY batch
        # -- the upsert's ON CONFLICT target is the season/episode key and
        # cannot absorb a violation of a different constraint. So uniqueness
        # here would buy a guarantee no reader needs at the price of a
        # batch-aborting failure in a workflow this schema documents as
        # expected. If an episode-level matcher ever wants uniqueness it
        # needs a merge story first, exactly as titles did.
        Index(
            "ix_episodes_imdb_id",
            "imdb_id",
            postgresql_where=text("imdb_id IS NOT NULL"),
        ),
        CheckConstraint("season_number >= 0", name="ck_episodes_season_number_non_negative"),
        CheckConstraint("episode_number >= 0", name="ck_episodes_episode_number_non_negative"),
        CheckConstraint(
            "absolute_number IS NULL OR absolute_number >= 0",
            name="ck_episodes_absolute_number_non_negative",
        ),
        CheckConstraint(
            "runtime_minutes IS NULL OR runtime_minutes >= 0",
            name="ck_episodes_runtime_minutes_non_negative",
        ),
    )
