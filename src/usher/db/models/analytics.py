"""`search_queries` — [PRD 10](../../../../docs/prd/10-telemetry-and-dashboards.md)'s
second analytics table, shipped whole and with no writer.

**Whole is the point.** PRD 10 assigns this table to M9 *whole* because a
half-populated analytics table is worse than an empty metric: a dashboard
reading it cannot tell a real zero from a column nobody filled. So all nine
columns land together, and the other half of "whole" is that nothing is added
speculatively either — `requested_mode` is wire-only, and if the analytics
task finds it must be persisted, that is a request rather than a column
appended here.

**A domain record, not telemetry exhaust.** PRD 10's own framing: durable,
queryable, exact. It is also the answer to something
`.claude/rules/search-and-embeddings.md` lists as unsettled by ADR-0002's
failed typo-tolerance gate — that gate measured *synthetically mutated*
queries, and real typed ones are this table.

No `set_updated_at` trigger and no `updated_at` column: a row here records
something that already happened, which is `llm_calls`' case exactly.
`tests/integration/test_migrations.py::test_migration_creates_the_updated_at_triggers`
asserts that trigger set exactly, so this is mechanically required as well as
right.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.ports.search import SearchMode


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

    __table_args__ = (
        CheckConstraint("query <> ''", name="ck_search_queries_query_not_empty"),
        CheckConstraint("result_count >= 0", name="ck_search_queries_result_count_non_negative"),
        CheckConstraint("latency_ms >= 0", name="ck_search_queries_latency_ms_non_negative"),
        # **No index beyond the primary key**, on `genome_tags`' precedent and
        # `genome_scores`' before it. The readers are PRD 10's dashboards,
        # which do not exist yet, and an index whose reader is a later
        # milestone is `ix_titles_popularity` again — the failure PRD 09's
        # boundary call 9 names, inverted.
        #
        # The cost is stated rather than hidden: `clicked_title_id`'s SET NULL
        # has no lookup behind it, so a title delete scans this table. It is
        # the one declared delete rule in `m09a` without an index, and the
        # migration's docstring records what would reverse that.
    )
