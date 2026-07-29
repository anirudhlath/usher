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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
from usher.domain.enums import WatchStateOrigin


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE")
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    position_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    played: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    play_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    # Renamed from updated_by: that name reads as a user FK in nearly every
    # schema, and this table has user_id right next to it. No default --
    # see the domain-model WatchState commit.
    origin: Mapped[WatchStateOrigin] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_watch_states_user_title"),
        UniqueConstraint("user_id", "episode_id", name="uq_watch_states_user_episode"),
        Index("ix_watch_states_user_played", "user_id", "played"),
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
