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
    # (ForeignKey("titles.id", ondelete="SET NULL") in source.py).
    title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="RESTRICT")
    )
    # RESTRICT, matching title_id immediately above and for the identical reason
    # (ADR-0010): a WatchState *is* the thing worth keeping.
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
    # Renamed from updated_by: that name reads as a user FK in nearly every schema, and
    # this table has user_id right next to it.
    origin: Mapped[WatchStateOrigin] = mapped_column(
        enum_column(WatchStateOrigin, length=16), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_watch_states_user_title"),
        UniqueConstraint("user_id", "episode_id", name="uq_watch_states_user_episode"),
        # Continue Watching, and it REPLACED ix_watch_states_user_played rather than
        # joining it.
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
