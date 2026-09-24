"""`search_queries` -- PRD 10's second analytics table."""

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
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.ports.search import SearchMode, SearchSurface, SuggestTier


class SearchQueryRow(Base):
    """One search, and what it led to.

    **`mode` reuses `usher.ports.search.SearchMode` directly.** `usher.db`
    sits outside the four-layer contract, so the import is legal, and minting
    a second copy of a three-member vocabulary would only give it something to
    drift against.
    """

    __tablename__ = "search_queries"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # When the search happened, not when the row was inserted, so no
    # `server_default`. `at` rather than `created_at` because PRD 10's column
    # list says `at` — the same call `llm_calls` made one table over.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # **RESTRICT**, the asymmetric half of the two rules on this table: a
    # household's search history is user state, so deleting a user must fail
    # loudly while the record of what they searched for still exists, rather
    # than taking it silently.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[SearchMode] = mapped_column(enum_column(SearchMode, length=16), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # **SET NULL**, the other half of the asymmetry. A deleted title must not
    # delete the row recording what somebody searched for: the search
    # happened, its latency and its result count are still true, and the
    # attribution is one nullable fact about it rather than its reason to
    # exist.
    clicked_title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="SET NULL"), nullable=True
    )
    # NOT NULL with no default, `llm_calls.ok`'s precedent: the writer sets it
    # at insert and attribution updates it, so a dashboard reads a real `false`
    # rather than a column nobody filled.
    played: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Two columns rather than a fourth `SearchMode` member: `SearchMode` is `GET
    # /search`'s `?mode=` and `SearchAnswer`'s two fields, so a member no search lane
    # can serve would become reachable on a route that would have to refuse it.
    surface: Mapped[SearchSurface] = mapped_column(
        enum_column(SearchSurface, length=8), nullable=False
    )
    # **Nullable, and that is the design.** A `search` row has no tier; a
    # `NOT NULL` here would need a member meaning "not applicable", which is a
    # third entry in the one vocabulary this pair exists to keep separate.
    tier: Mapped[SuggestTier | None] = mapped_column(
        enum_column(SuggestTier, length=6), nullable=True
    )

    __table_args__ = (
        CheckConstraint("query <> ''", name="ck_search_queries_query_not_empty"),
        CheckConstraint("result_count >= 0", name="ck_search_queries_result_count_non_negative"),
        CheckConstraint("latency_ms >= 0", name="ck_search_queries_latency_ms_non_negative"),
        # For PRD 08's retention job, `DELETE FROM search_queries WHERE at <
        # :cutoff`, which without it is a sequential scan.
        Index("ix_search_queries_at", "at"),
        # `clicked_title_id`'s SET NULL has no index behind it, so a title
        # delete still scans this table.
    )
