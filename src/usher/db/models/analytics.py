"""`search_queries`.

[PRD 10](../../../../docs/prd/10-telemetry-and-dashboards.md)'s second analytics table,
"""

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
    sits outside the four-layer contract (`layers = ["usher.api",
    "usher.services", "usher.ports", "usher.domain"]`), so the import is
    legal, and `usher/domain/search.py`'s docstring deliberately declares no
    `SearchMode` of its own — a decision this table honours rather than
    reverses by minting a second, drift-capable copy of a three-member
    vocabulary.
    """

    __tablename__ = "search_queries"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # When the search happened, not when the row was inserted, so no
    # `server_default`. `at` rather than `created_at` because PRD 10's column
    # list says `at` — the same call `llm_calls` made one table over.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # **RESTRICT**, and it is the asymmetric half of the two rules on this
    # table. A household's search history is user state, which is the side of
    # ADR-0010's asymmetry `fk_watch_states_episode_id_episodes` already sits
    # on: deleting a user must fail loudly while the record of what they
    # searched for still exists, rather than taking it silently.
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
    # NOT NULL with no default, `llm_calls.ok`'s precedent. The writer sets it
    # at insert (nothing has been played yet) and attribution updates it, so a
    # dashboard reads a real `false` rather than a column nobody filled —
    # which is the failure the "whole" in PRD 10's comment is about.
    played: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # **The tenth and eleventh columns, `m10c`, PRD 10's amendment 2.** They are two
    # columns rather than a fourth `SearchMode` member because `SearchMode` is `GET
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
        # **One index since `m10c`, and it has a reader named in PRD 10 itself** —
        # `DELETE FROM search_queries WHERE at < now() - interval '90 days'`, which that
        # document records as a sequential scan *"until somebody adds one"*.
        Index("ix_search_queries_at", "at"),
        # The cost of `m09a`'s original decision is stated rather than hidden,
        # and is unchanged by the index above: `clicked_title_id`'s SET NULL
        # has no lookup behind it, so a title delete still scans this table.
    )
