"""Behaviour every `SearchQueryRepository` implementation must satisfy.

**A write is observed through an abstract `SearchQueryLedger`, not through a
read method on the port** -- the port has none, by design, and
`SearchQueryRepository`'s own docstring carries the argument: this table's
readers are PRD 10's dashboards, which do not exist yet, and adding a method
so this suite could read through the port would be adding the very surface
`genome_tags`' precedent (and `llm_calls`' before it) declined. It reads the
table out of band, exactly as `LLMCallLedger` and `CuratedRowSeeder` do, and
for the identical reason.

**Every case names the wrong implementation it rules out.** A test whose
docstring cannot name what it kills is a test that kills nothing.

**"First write wins" is this port's one piece of real behaviour**, so it gets
two cases from two directions rather than one: that an outcome is written at
all, and that a second one cannot displace the first. Everything else here is
storage -- did the row land, did it land once, did it land with every column
distinct from every other.

Subclass and provide `repository`, `ledger`, `user_id` (naming a household
that actually exists, for an implementation with a foreign key) and
`add_title` (for `record_outcome`'s attribution target, same reason).

Its `ABC` shape is ADR-0001's argument applied to a test double -- a
`Protocol` would let one arm drift out of the suite silently.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import AwareDatetime

from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository
from usher.ports.search import SearchMode

#: When the search happened, not when the row is inserted -- `search_queries.at`
#: carries no server default for exactly that reason (`llm_calls.at`'s
#: precedent), so a fixture that omitted it would be testing a column this
#: schema does not have.
AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

#: What every case types unless it varies it. Invented, like every value in
#: this suite -- see `tests/fixtures/README.md`.
QUERY = "the quiet vacuum"

#: **Pairwise distinct from `LATENCY_MS`**, which is the fixture's own
#: premise: two adjacent `int` columns with the same fixture value would make
#: a write that filled one from the other invisible, the same trap
#: `llm_call_repository_contract.py`'s three-integer premise exists for.
RESULT_COUNT = 7
LATENCY_MS = 42


def search_query_record(
    *,
    user_id: uuid.UUID,
    record_id: uuid.UUID | None = None,
    at: AwareDatetime = AT,
    query: str = QUERY,
    mode: SearchMode = SearchMode.SEMANTIC,
    result_count: int = RESULT_COUNT,
    latency_ms: int = LATENCY_MS,
) -> SearchQueryRecord:
    """One `SearchQueryRecord`, with the fields a case does not care about
    filled in.

    A test-double builder, not a port method. `mode` defaults to `SEMANTIC`
    rather than `SearchMode`'s first member (`FULL_TEXT`) deliberately: a
    write that hardcoded the default would still pass a case that never
    varied it.
    """
    return SearchQueryRecord(
        id=record_id if record_id is not None else new_id(),
        at=at,
        user_id=user_id,
        query=query,
        mode=mode,
        result_count=result_count,
        latency_ms=latency_ms,
    )


@dataclass(frozen=True, slots=True)
class StoredSearchQuery:
    """The whole row, as `SearchQueryLedger.get` reads it back.

    Test infrastructure, not a port type -- `SearchQueryRepository` has no
    read method, so there is nothing on the port this could be confused for.
    """

    id: uuid.UUID
    at: AwareDatetime
    user_id: uuid.UUID
    query: str
    mode: SearchMode
    result_count: int
    latency_ms: int
    clicked_title_id: uuid.UUID | None
    played: bool


class SearchQueryLedger(ABC):
    """The stored table, read without going through the port.

    Not a read method on `SearchQueryRepository` -- the port is write-only by
    design and this module's docstring says why. **No writer that bypasses
    the port**, unlike `CuratedRowSeeder`: neither of this port's two methods
    deletes anything, so every state this suite needs is reachable through
    the port itself.
    """

    @abstractmethod
    async def get(self, query_id: uuid.UUID) -> StoredSearchQuery | None:
        """The stored row as stored, or `None` if there is none.

        Whole-row rather than one column, so a case can compare against the
        record it wrote and catch a column dropped, or two columns filled
        from one another.
        """

    @abstractmethod
    async def count(self) -> int:
        """Every row the table holds -- what makes "recorded once, not
        twice" assertable at all."""


class SearchQueryRepositoryContract:
    """Subclasses supply `repository`, `ledger`, `user_id` and `add_title` as
    fixtures/hooks. Not an `ABC`, matching every other contract suite here:
    the fixtures are supplied by pytest rather than by inheritance."""

    async def add_title(self) -> uuid.UUID:
        """A title `record_outcome` can legitimately attribute a click to."""
        raise NotImplementedError

    # -- record() -----------------------------------------------------------

    async def test_a_recorded_query_reads_back_with_the_mode_that_ran_and_its_latency(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """The control every other case needs.

        The wrong implementations this kills: a column dropped from the
        write; `result_count` filled from `latency_ms` or vice versa, the
        classic wrong-slot write between two adjacent integers; `mode`
        written as a constant rather than the value that ran.
        """
        assert RESULT_COUNT != LATENCY_MS, (
            "the fixture must make the two integer columns tell each other apart"
        )
        record = search_query_record(user_id=user_id)

        await repository.record(record)

        stored = await ledger.get(record.id)
        assert stored is not None, "the query was recorded and then could not be read back"
        assert stored.at == AT
        assert stored.user_id == user_id
        assert stored.query == QUERY
        assert stored.mode is SearchMode.SEMANTIC
        assert stored.result_count == RESULT_COUNT
        assert stored.latency_ms == LATENCY_MS
        assert await ledger.count() == 1

    async def test_a_recorded_query_starts_with_no_click_and_not_played(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """**`record()` writes the two outcome columns as literals, not as
        columns it leaves unset.** The wrong implementation this kills:
        `played` left NULL (the table has no default for it at all, and a
        write that relied on one would refuse the whole row) or
        `clicked_title_id` written to something other than `NULL` before any
        client has done anything.
        """
        record = search_query_record(user_id=user_id)

        await repository.record(record)

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id is None
        assert stored.played is False

    async def test_recording_the_same_query_twice_is_a_conflict_rather_than_an_update(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """The wrong implementation this kills: an upsert where an insert was
        asked for -- `ON CONFLICT (id) DO NOTHING` or `DO UPDATE`.
        `TitleRepository.add` and `LLMCallRepository.record` are the
        precedent: an insert, not an upsert, and a duplicate id raises.

        The constraint name is asserted on both arms, which is what makes the
        two agree rather than merely both raise.
        """
        record = search_query_record(user_id=user_id)
        await repository.record(record)

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record(record)

        assert raised.value.constraint == "pk_search_queries"
        assert await ledger.count() == 1

    # -- record_outcome() ----------------------------------------------------

    async def test_an_attributed_query_reads_back_with_its_click_and_played(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """The wrong implementation this kills: `record_outcome` that writes
        `played` but not `clicked_title_id`, or updates the wrong row (no
        `WHERE id = ...`, or a dropped `id` parameter)."""
        record = search_query_record(user_id=user_id)
        await repository.record(record)
        title_id = await self.add_title()

        await repository.record_outcome(record.id, clicked_title_id=title_id, played=True)

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id == title_id
        assert stored.played is True

    async def test_a_second_attribution_does_not_replace_the_first(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """**First write wins.** The wrong implementation this kills: an
        unconditional `UPDATE` with no guard, which lets a redelivered or
        duplicated attribution overwrite a real click with a later, less
        informative one -- exactly the state PRD 08's redelivery rule asks
        this port to be safe under.
        """
        record = search_query_record(user_id=user_id)
        await repository.record(record)
        first_title = await self.add_title()
        second_title = await self.add_title()
        assert first_title != second_title, (
            "the fixture must attribute to two different titles, or a replace and a no-op "
            "look identical"
        )

        await repository.record_outcome(record.id, clicked_title_id=first_title, played=False)
        await repository.record_outcome(record.id, clicked_title_id=second_title, played=True)

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id == first_title
        assert stored.played is False

    async def test_attributing_a_query_that_was_never_recorded_is_a_silent_no_op(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger
    ) -> None:
        """The wrong implementation this kills: a `record_outcome` that
        raises on an unknown id rather than leaving a table it did not
        change alone, which would make a stale or duplicate client callback
        a request failure rather than a fact about a table with nothing to
        update.
        """
        unknown = new_id()

        await repository.record_outcome(unknown, clicked_title_id=new_id(), played=True)

        assert await ledger.get(unknown) is None
        assert await ledger.count() == 0
