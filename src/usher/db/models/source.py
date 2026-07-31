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
    LargeBinary,
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
    # SET NULL, mirroring title_id immediately above and for the identical
    # reason (ADR-0010): an unmatched MediaItem is worth keeping -- it is
    # the review queue -- so losing its Episode link just clears it. M4's
    # migration is what finally gives this column a target; it was a
    # dangling PGUUID from M1 until the episodes table existed.
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("episodes.id", ondelete="SET NULL")
    )
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
        # Same argument as ix_media_items_title_id, for the FK M4 added: an
        # episode DELETE makes Postgres find every referencing row here to
        # SET NULL it, and uq_media_items_source_external leads with
        # source_id so it cannot serve that lookup. Without this index the
        # check is a seq scan of media_items -- 999,827 episode rows at the
        # one measured deployment -- once per episode deleted, and
        # episodes.title_id is ON DELETE CASCADE, so deleting one series
        # fires it once per episode of that series.
        Index("ix_media_items_episode_id", "episode_id"),
        Index(
            "ix_media_items_unmatched",
            "source_id",
            postgresql_where=text("title_id IS NULL"),
        ),
        # The availability sweep's `UPDATE`, and the claim is deliberately
        # that narrow. Measured against pgvector/pgvector:pg17 at 1,126,674
        # rows on one source with 200 stale -- the realistic nightly shape --
        # by `scripts/measure_ingest.py --scale 1126674`:
        #
        # - `UPDATE ... WHERE source_id = :x AND available AND last_seen_at <
        #   :since` goes from `Seq Scan` (`Rows Removed by Filter:
        #   1,126,474`, 173 ms) to `Index Scan using ix_media_items_sweep`
        #   with an `Index Cond` on all three columns, 102 ms.
        # - `mark_unseen_unavailable`'s *guard* -- `count(*)` plus a
        #   `count(*) FILTER (...)` over the source -- is a `Parallel Seq
        #   Scan` either way (87 ms with, 86 ms without). ADR-0015's ceiling
        #   is a fraction, so the total is unavoidable and a source that *is*
        #   the whole table gives `source_id` no selectivity to work with.
        #   This index does not help that statement and is not claimed to.
        #
        # Column order is (equality, equality, range), which is what lets the
        # `UPDATE` seek straight to the stale tail rather than filter the
        # whole source.
        Index("ix_media_items_sweep", "source_id", "available", "last_seen_at"),
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


class SourceCredentialRow(Base):
    """Encrypted source credentials, addressed by the opaque
    `Source.credentials_ref`.

    A separate table rather than two more columns on `sources`, so a plain
    `SELECT * FROM sources` -- what every admin read, every debugging
    session, and every glance at a `pg_dump` does -- cannot return a
    ciphertext at all. PRD 08's "credentials are never returned by any API,
    including admin" becomes a property of the schema rather than of
    whoever wrote the serializer.

    `source_id` is a foreign key with `ON DELETE CASCADE` even though the
    primary key is `ref`: deleting a source is two writes (drop the
    credential, drop the source), and a crash between them would otherwise
    leave an encrypted orphan with nothing left to attribute it to.

    No `set_updated_at` trigger, unlike titles/sources/media_items: this
    table has exactly one writer (`PostgresCredentialStore`), which sets
    `updated_at` on both branches of its upsert. The three existing
    triggers exist because their tables are also written by bulk `COPY` and
    raw SQL paths that bypass the ORM; nothing bulk-loads credentials.
    """

    __tablename__ = "source_credentials"

    ref: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_source_credentials_source_id", "source_id"),
        CheckConstraint("ref <> ''", name="ck_source_credentials_ref_not_empty"),
    )
