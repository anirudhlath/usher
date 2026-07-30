"""Source and availability tables."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.enums import HdrFormat, SourceKind


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[SourceKind] = mapped_column(enum_column(SourceKind, length=16), nullable=False)
    # Not unique yet: deferred, not designed away. A household is expected
    # to have very few sources, so a duplicate name is a low-consequence,
    # easily-noticed mistake rather than a correctness problem worth a
    # migration for today -- revisit if/when multiple sources of the same
    # kind become common.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    credentials_ref: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    # server_default so a raw INSERT that doesn't mention these -- any
    # loader that isn't the ORM -- gets the same default the ORM applies,
    # instead of a NOT NULL violation.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    supports_push: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (CheckConstraint("name <> ''", name="ck_sources_name_not_empty"),)


class MediaItemRow(Base):
    __tablename__ = "media_items"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="SET NULL")
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    external_id: Mapped[str] = mapped_column(Text, nullable=False)

    container: Mapped[str | None] = mapped_column(String(32))
    video_codec: Mapped[str | None] = mapped_column(String(32))
    audio_codec: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    hdr_format: Mapped[HdrFormat | None] = mapped_column(enum_column(HdrFormat, length=16))
    audio_channels: Mapped[int | None] = mapped_column(Integer)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    runtime_seconds: Mapped[int | None] = mapped_column(Integer)

    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # Deferred, not designed away: no index on added_at/last_seen_at/
    # available (or titles.collection_id in title.py). media_items is ~94k
    # rows at the 300k-title benchmark scale, so a sort or filter on any of
    # these is a few ms without one -- revisit for M9 if/when catalog size
    # or query patterns change that.
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_media_items_source_external"),
        Index("ix_media_items_title_id", "title_id"),
        Index(
            "ix_media_items_unmatched",
            "source_id",
            postgresql_where=text("title_id IS NULL"),
        ),
        CheckConstraint("width IS NULL OR width >= 0", name="ck_media_items_width_non_negative"),
        CheckConstraint("height IS NULL OR height >= 0", name="ck_media_items_height_non_negative"),
        CheckConstraint(
            "audio_channels IS NULL OR audio_channels >= 0",
            name="ck_media_items_audio_channels_non_negative",
        ),
        CheckConstraint(
            "file_size_bytes IS NULL OR file_size_bytes >= 0",
            name="ck_media_items_file_size_bytes_non_negative",
        ),
        CheckConstraint(
            "runtime_seconds IS NULL OR runtime_seconds >= 0",
            name="ck_media_items_runtime_seconds_non_negative",
        ),
    )
