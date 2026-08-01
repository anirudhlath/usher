"""The priority work queue table (PRD 03, PRD 08)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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
from usher.domain.jobs import JobKind, JobPriority, JobStatus


class JobRow(Base):
    """One outstanding unit of work.

    No `set_updated_at` trigger, unlike `seasons`/`episodes`: this table's
    only writer is `PostgresJobQueue`, which sets `updated_at` explicitly on
    every one of its statements. The triggers exist for tables also written
    by staged bulk upserts; nothing bulk-loads through a path that could
    forget. Same call `SourceCredentialRow` made, for the same reason.

    A completed job's row is deleted, so this table's steady-state size is
    the outstanding work, not the work ever done. A first full walk of the
    one measured source enqueues 1,126,674 match jobs at once and then
    drains them; the churn is why `ix_jobs_claim` is partial on `pending`
    rather than covering the whole table.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[JobKind] = mapped_column(enum_column(JobKind, length=32), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(int(JobPriority.NEW)))
    )
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, length=16), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    traceparent: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_jobs_kind_key"),
        # The claim query, exactly: WHERE status = 'pending' AND
        # (run_after IS NULL OR run_after <= now()) ORDER BY priority DESC,
        # created_at. Partial on 'pending' so parked poison and in-flight
        # claims are not indexed at all -- the whole population this index
        # exists to order is the pending one, and at a 1.1M-item backfill
        # the other two are noise. run_after is deliberately not a key: it
        # is NULL for almost every job, so the ordering scan's first tuple
        # normally qualifies, and the OR predicate a nullable column forces
        # is not range-scannable anyway. The cost is bounded by the number
        # of backed-off jobs, which the attempt ceiling caps by parking them
        # out of this index entirely.
        Index(
            "ix_jobs_claim",
            text("priority DESC"),
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # PRD 08: "Parked jobs are listed in the admin API and counted in
        # metrics." Both are `WHERE status = 'parked'` scans, and there are
        # few enough parked rows for a partial index to be tiny.
        Index("ix_jobs_parked", "kind", postgresql_where=text("status = 'parked'")),
        CheckConstraint("key <> ''", name="ck_jobs_key_not_empty"),
        CheckConstraint("priority BETWEEN 0 AND 100", name="ck_jobs_priority_range"),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
    )
