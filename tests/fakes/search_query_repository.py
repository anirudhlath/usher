"""In-memory `SearchQueryRepository`.

**Where this is more forgiving than Postgres, on purpose.** Four places, each
of which the paired `tests/integration/test_search_query_repository.py` run
is what actually closes:

- **No foreign keys**, so `record()` stores a row for a `user_id` no `users`
  row names and `record_outcome()` attributes to a `clicked_title_id` no
  `titles` row names. Both refusals are Postgres-only, `llm_calls`' and
  `curated_rows`' precedent for the identical shape.
- **No CHECK constraints.** `ck_search_queries_query_not_empty`,
  `ck_search_queries_result_count_non_negative` and
  `ck_search_queries_latency_ms_non_negative` are enforced here not at all --
  `SearchQueryRecord` is a plain, unvalidated dataclass, unlike a pydantic
  domain model, so there is nothing here to fire even by accident.
- **No client-side encoder to refuse an out-of-range `int`.** Postgres's
  `integer` columns and asyncpg's binary encoder are what make `2**31`
  unstorable; a Python `int` has no such ceiling, so
  `test_a_latency_the_column_cannot_hold_is_a_port_error` is Postgres-only.
- **No transaction and no SAVEPOINT**, so nothing here exercises the property
  the SAVEPOINT buys: that a refused write leaves the caller's session usable
  for whatever else it is holding.

**The duplicate-`id` refusal on `record()` is modelled exactly rather than
diverged**, name and all, `FakeLLMCallRepository`'s reason: `record()` is an
insert, never an upsert, and a service could otherwise violate that through
every unit test in the milestone and only discover it against a real primary
key.

**`record_outcome`'s two conditions are modelled exactly too, and separately**
-- `clicked_title_id` first-write-wins, `played` monotonic-or -- because
together they are this port's one piece of real behaviour rather than a
storage detail. A fake that shared one guard between them (the shape a review
caught before it shipped: `db/repositories/search_query.py`'s module
docstring has the corrected argument) would pass every other case in the
contract and hide the defect that guard produces -- F3's own funnel calls
this twice on one row, a click and then a play, and the second call must
still land.
"""

import uuid

from usher.ports.errors import RepositoryConflict
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository


class FakeSearchQueryRepository(SearchQueryRepository):
    def __init__(self) -> None:
        #: Every recorded query, keyed by id -- the table, not a screen.
        self.rows: dict[uuid.UUID, SearchQueryRecord] = {}
        #: `(clicked_title_id, played)` per query id, defaulting to the same
        #: `(None, False)` `record()` writes literally on the Postgres arm.
        self.outcomes: dict[uuid.UUID, tuple[uuid.UUID | None, bool]] = {}

    async def record(self, record: SearchQueryRecord) -> None:
        # `pk_search_queries`, modelled rather than diverged -- see the module
        # docstring. Checked before the write, so a refused call leaves the
        # table exactly as it was, which is also what the real one's SAVEPOINT
        # buys on the arm that has a transaction.
        if record.id in self.rows:
            raise RepositoryConflict(
                f"search query {record.id} is already recorded", constraint="pk_search_queries"
            )
        self.rows[record.id] = record
        self.outcomes[record.id] = (None, False)

    async def record_outcome(
        self, query_id: uuid.UUID, *, clicked_title_id: uuid.UUID, played: bool
    ) -> None:
        if query_id not in self.rows:
            # No row named this id -- a silent no-op, matching the real
            # statement's zero-rows-affected `UPDATE`.
            return
        already_clicked, already_played = self.outcomes[query_id]
        # First write wins on `clicked_title_id` alone: a later, genuinely
        # different click must not steal credit from the result the
        # household actually opened. `already_clicked` is never overwritten
        # once set.
        winning_click = already_clicked if already_clicked is not None else clicked_title_id
        # Monotonic on `played` alone, and independent of the guard above --
        # this is the whole fix. A call that only means to report a play
        # (the same title, `played=True`, arriving after the click that
        # already attributed this row) must still land, and once `played`
        # is `True` a later `False` is stale information rather than a
        # correction to write over it.
        self.outcomes[query_id] = (winning_click, already_played or played)
