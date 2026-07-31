"""Sync-run history and the provider payload cache."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.sync import SyncRunKind, SyncRunStatus


class SyncRunRow(Base):
    """One attempt at reconciling a source. A history, not a checkpoint --
    contrast `ImportRunRow`, which is exactly one row per dataset.

    No `set_updated_at` trigger and no `updated_at` column: a run's
    interesting timestamps are `started_at` and `finished_at`, and both are
    written explicitly by the one service that owns the row.
    """

    __tablename__ = "sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # CASCADE: run bookkeeping about a source that no longer exists protects
    # nothing and cannot be attributed to anything. Same call
    # media_items.source_id makes, and the opposite of the RESTRICT that
    # guards user state.
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[SyncRunKind] = mapped_column(enum_column(SyncRunKind, length=16), nullable=False)
    status: Mapped[SyncRunStatus] = mapped_column(
        enum_column(SyncRunStatus, length=16), nullable=False, server_default=text("'running'")
    )
    cursor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_matched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_unmatched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_retracted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # "The cursor for the next delta walk" is a single-row lookup:
        # the newest COMPLETED run of a kind for a source. Descending on
        # started_at so it is the index's first entry rather than its last.
        # status is not a key: a source's runs are a handful a day, so a
        # scan back through consecutive failures to the last clean run is
        # bounded by how many times in a row it failed. It also serves
        # sources' own CASCADE, leading with source_id.
        Index("ix_sync_runs_source_kind_started", "source_id", "kind", text("started_at DESC")),
        CheckConstraint("items_seen >= 0", name="ck_sync_runs_items_seen_non_negative"),
        CheckConstraint("items_matched >= 0", name="ck_sync_runs_items_matched_non_negative"),
        CheckConstraint("items_unmatched >= 0", name="ck_sync_runs_items_unmatched_non_negative"),
        CheckConstraint("items_retracted >= 0", name="ck_sync_runs_items_retracted_non_negative"),
    )


class RawPayloadRow(Base):
    """A provider response, cached verbatim so reprocessing never refetches.

    **Providers only.** PRD 03's ingest stage previously said to store every
    *source* item's raw payload here; at 1,126,674 items and ~8 kB apiece
    that is ~9 GB against a database PRD 08 budgets at 8-12 GB total, to
    cache something re-readable from the source in one request. Corrected in
    PRD 03 and PRD 02; see
    [ADR-0016](../../../../docs/prd/decisions/0016-raw-payloads-cache-providers-not-sources.md).

    `fetched_at` is also what enforces TMDb's <=6-month caching term (PRD
    04's licensing constraint, PRD 10's dashboard-5 panel). PRD 02 listed a
    separate `provider_cache_meta` table for exactly that timestamp; one
    column answers it once, so that table is not created. `fetched_at`'s
    `server_default` covers the INSERT arm only -- an upsert that refreshes
    a payload must set it explicitly, because a stale timestamp on fresh
    data is precisely the compliance answer the column exists to give.
    """

    __tablename__ = "raw_payloads"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "kind", "reference", name="uq_raw_payloads_provider_kind_reference"
        ),
        # The compliance query: "oldest fetched_at against the 6-month
        # ceiling" (PRD 10, dashboard 5). Ascending, because it asks for the
        # minimum.
        Index("ix_raw_payloads_fetched_at", "fetched_at"),
        CheckConstraint("provider <> ''", name="ck_raw_payloads_provider_not_empty"),
        CheckConstraint("reference <> ''", name="ck_raw_payloads_reference_not_empty"),
    )
