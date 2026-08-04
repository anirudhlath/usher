"""User and watch-state tables."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.enums import WatchStateOrigin


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (CheckConstraint("name <> ''", name="ck_users_name_not_empty"),)


class WatchStateRow(Base):
    __tablename__ = "watch_states"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT, not CASCADE -- deliberately the opposite of MediaItem.title_id
    # (ForeignKey("titles.id", ondelete="SET NULL") in source.py). The two
    # look parallel but protect opposite things: an unmatched MediaItem row
    # is worth keeping (review queue), so losing its Title link just clears
    # it; a WatchState *is* the thing worth keeping, so losing its Title
    # link must not silently delete it. PRD 02 (Identity) requires merging
    # two Titles to be "a repointing operation rather than a primary-key
    # rewrite cascading through watch state" -- UUIDv7 identity exists
    # specifically so that's possible, and M4's four-tier matcher will
    # produce duplicate Titles to merge. A merge is "repoint every
    # watch_states/media_items row from the loser to the winner, then delete
    # the loser"; under CASCADE, any bug that deletes the loser before (or
    # instead of) repointing silently destroys watch history with no error.
    # RESTRICT makes that fail loudly at the DELETE instead. See
    # ADR-0010.
    title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="RESTRICT")
    )
    # RESTRICT, matching title_id immediately above and for the identical
    # reason (ADR-0010): a WatchState *is* the thing worth keeping. An
    # episode merge -- which M4's matcher produces, because a series
    # ingested twice under different provider ids yields two episode trees
    # -- must repoint every watch_states row before deleting the loser, and
    # RESTRICT makes skipping that step fail at the DELETE instead of
    # silently destroying history. It composes with episodes.title_id's
    # CASCADE rather than fighting it: deleting a Title cascades into
    # episodes, and this RESTRICT then refuses that cascade if any history
    # points at one. Proven against real Postgres in
    # tests/integration/test_migrations.py.
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("episodes.id", ondelete="RESTRICT")
    )

    position_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    played: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    play_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    # Renamed from updated_by: that name reads as a user FK in nearly every
    # schema, and this table has user_id right next to it. No default -- on
    # either the ORM or the database side, and deliberately not: a sync path
    # that forgets to set this must fail loudly rather than silently
    # mislabel source-pushed state as user-originated. Do not add
    # server_default here to "match" the other bulk-load-friendly columns
    # above; that would defeat the entire point. See the domain-model
    # WatchState commit.
    origin: Mapped[WatchStateOrigin] = mapped_column(
        enum_column(WatchStateOrigin, length=16), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_watch_states_user_title"),
        UniqueConstraint("user_id", "episode_id", name="uq_watch_states_user_episode"),
        # Continue Watching, and it REPLACED ix_watch_states_user_played
        # rather than joining it. That index was declared in M1 for a query
        # nobody wrote: all seven shipped watch_states statements were
        # EXPLAINed at 1,119,097 rows before the swap and not one used it --
        # _NEEDING_HISTORY leads with `played` and leaves `user_id` unbound,
        # and the getters and both merge branches drive off the single-column
        # title_id/episode_id indexes with `user_id` as a filter. This is a
        # strict prefix superset of it, so nothing it could serve is lost,
        # and two indexes where one suffices is a write cost on every merge
        # of every nightly walk for no read.
        #
        # `DESC NULLS LAST` is the correctness content, not the formatting.
        # `last_played_at` is nullable because a walk's listing cannot
        # determine it (ADR-0014); Postgres defaults a DESC sort to NULLS
        # FIRST; and a DESC-NULLS-FIRST btree cannot supply
        # `ORDER BY last_played_at DESC NULLS LAST` as an ordered scan. So
        # without the spelled-out clause the index would serve the filter,
        # the planner would fall back to a full Sort, and Continue Watching
        # would sort the household's whole per-user set on every home screen
        # while an index sat there looking like it was helping.
        #
        # `compare_metadata` *does* diff this clause -- measured by mutation
        # in both directions, against the plan's assumption that it does not
        # -- so `test_migration_matches_the_orm_metadata` is one guard here.
        # `test_the_row_read_indexes_carry_the_clauses_that_make_them_work`
        # is the other, and it is the one that does not depend on which
        # clauses a future Alembic happens to render: it reads
        # `pg_indexes.indexdef`, i.e. what Postgres will actually do.
        #
        # Measured (`scripts/measure_rows.py --scale 1126674`): _IN_PROGRESS
        # goes from a Parallel Seq Scan at 38.123 ms to an Index Scan under
        # an Incremental Sort at 0.029 ms, touching 24 buffers.
        # `list_rediscoverable` borrows it for its equality pair plus range
        # and gains almost nothing (14.614 -> 13.864 ms), because its
        # `ORDER BY play_count DESC` is a Sort no index here can serve.
        Index(
            "ix_watch_states_user_recent",
            "user_id",
            "played",
            text("last_played_at DESC NULLS LAST"),
        ),
        # uq_watch_states_user_title leads with user_id, so it can't serve a
        # lookup/delete keyed on title_id alone -- and once title_id is
        # RESTRICT (above), that FK's constraint check runs a lookup on
        # every attempted title delete, including every Title merge.
        # Without this index that check is a seq scan of watch_states.
        Index("ix_watch_states_title_id", "title_id"),
        # And the identical argument for episode_id, once M4 gave it a
        # RESTRICT target: uq_watch_states_user_episode leads with user_id,
        # so it cannot serve the FK's lookup on episode_id alone. 999,827 of
        # the one measured source's 1,126,674 items are episodes, so this is
        # the larger of the two populations, not the smaller.
        Index("ix_watch_states_episode_id", "episode_id"),
        # Mirrors WatchState's model_validator: exactly one of
        # title_id/episode_id, never neither or both.
        CheckConstraint(
            "num_nonnulls(title_id, episode_id) = 1",
            name="ck_watch_states_exactly_one_target",
        ),
        CheckConstraint(
            "position_seconds >= 0", name="ck_watch_states_position_seconds_non_negative"
        ),
        CheckConstraint(
            "runtime_seconds IS NULL OR runtime_seconds >= 0",
            name="ck_watch_states_runtime_seconds_non_negative",
        ),
        CheckConstraint("play_count >= 0", name="ck_watch_states_play_count_non_negative"),
    )
