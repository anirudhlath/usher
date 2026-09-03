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

**`record_outcome`'s two columns are two different facts under two different
conditions, and conflating them into one guard is a real defect a review
caught by reading rather than by running anything -- see the module docstring
on the port for the corrected argument.** `clicked_title_id` is genuine
attribution: first write wins, because a later, different click must not
steal credit from the result the household actually opened.  `played` is
whether *anything* happened after that click, and F3's own funnel
(`GET /titles/{id}?search_id=…` for the click, `POST /titles/{id}/play` for
the play, at two different times) means the ordinary path is **a second call
on the same row that only means to flip `played`** -- not a duplicate
delivery of the first call, and not a second, different click. A guard keyed
on `clicked_title_id IS NULL` cannot tell that call apart from either of
those and silently drops it, which is the shape this suite now has four
cases for rather than one: a later click does not steal an earlier one's
attribution; a later play reaches a row a click already attributed; a play
that had no click before it is a legal row with `played` true and the click
still `NULL`; and `played` never reverts once it is true.

**The play writer passes no title, and every case below spells it that
way.** `clicked_title_id=None` is what stops the second call from being one
writer that sets both columns -- see the port. The one case that passes a
title *and* `played=True` in a single call is the storage control, and says
so.

**The household scope is the one predicate here that is a security
boundary**, so it is in the shared contract rather than only in the
Postgres arm: a `query_id` arrives from a client and must not let one
household write attribution onto another's row.
`test_a_search_belonging_to_another_household_is_not_attributed` carries its
own positive control, because a repository that stopped writing at all
passes the negative half.

**`oldest()` and `prune()` are M10's J5 and are contract rather than
storage.** `SearchQueryRetention.last_done()` is built on `min(at)` --
ADR-0046's no-state design makes every registration read a completion time
off the artefact it maintains -- so `max` in place of `min`, an empty table
inventing an age, and a naive datetime are all failures of the *scheduler*
one layer up rather than of this table. `prune`'s `<`-not-`<=` boundary and
its exact return value are contract for the same reason: the boundary is one
character and the count is the chunk loop's only terminator.

Everything else here is storage -- did the row land, did it land once, did
it land with every column distinct from every other.

Subclass and provide `repository`, `ledger`, `counts` (the two tables a row
points *at*, for the leaf-delete case), `user_id` (naming a household
that actually exists, for an implementation with a foreign key), `add_user`
(a *second* household, for the scope case) and `add_title` (for
`record_outcome`'s attribution target, same reason).

Its `ABC` shape is ADR-0001's argument applied to a test double -- a
`Protocol` would let one arm drift out of the suite silently.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import AwareDatetime

from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository
from usher.ports.search import SearchMode, SearchSurface, SuggestTier

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
    surface: SearchSurface = SearchSurface.SEARCH,
    tier: SuggestTier | None = None,
) -> SearchQueryRecord:
    """One `SearchQueryRecord`, with the fields a case does not care about
    filled in.

    A test-double builder, not a port method. `mode` defaults to `SEMANTIC`
    rather than `SearchMode`'s first member (`FULL_TEXT`) deliberately: a
    write that hardcoded the default would still pass a case that never
    varied it.

    ⚠️ **`surface` is defaulted here and is required on the record itself**,
    and the asymmetry is deliberate rather than an oversight. `m10c` refuses a
    `server_default` and `SearchQueryRecord` refuses a field default for the
    same reason -- a plausible wrong value supplied to a writer that forgot --
    but a *test-double builder* has no writer to forget: every case here states
    the surface it is about, and the one that varies it is the pair below. A
    default in the fixture cannot reach production; one on the record can.
    """
    return SearchQueryRecord(
        id=record_id if record_id is not None else new_id(),
        at=at,
        user_id=user_id,
        query=query,
        mode=mode,
        result_count=result_count,
        latency_ms=latency_ms,
        surface=surface,
        tier=tier,
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
    surface: SearchSurface
    tier: SuggestTier | None


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


@dataclass(frozen=True, slots=True)
class ReferenceRowCounts:
    """How many households and titles exist, either side of a prune.

    Two numbers rather than a set of ids, because the claim is that
    *nothing* went, and a set comparison over two seeded rows is the same
    claim with more to keep in step.
    """

    users: int
    titles: int


class ReferenceCounts(ABC):
    """The two tables a `search_queries` row points **at**, counted out of
    band.

    Test infrastructure. It exists because *"the delete is a leaf"* is the
    kind of claim that is true, obvious, and asserted by nothing -- and the
    row being deleted names both tables, so the absurd implementation is one
    somebody could write. Same out-of-band shape as `SearchQueryLedger`
    beside it: neither table is reachable through this port.
    """

    @abstractmethod
    async def read(self) -> ReferenceRowCounts:
        """`count(*)` on `users` and on `titles`, right now."""


class SearchQueryRepositoryContract:
    """Subclasses supply `repository`, `ledger`, `user_id` and `add_title` as
    fixtures/hooks. Not an `ABC`, matching every other contract suite here:
    the fixtures are supplied by pytest rather than by inheritance."""

    async def add_title(self) -> uuid.UUID:
        """A title `record_outcome` can legitimately attribute a click to."""
        raise NotImplementedError

    async def add_user(self) -> uuid.UUID:
        """A *second* household, distinct from the `user_id` fixture.

        Only the scope case needs it, and it needs a real one: on an
        implementation with a foreign key, an invented id would make
        "another household's call does not land" true for the wrong
        reason -- the write would be refused rather than scoped out.
        """
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
        assert stored.surface is SearchSurface.SEARCH
        assert stored.tier is None
        assert await ledger.count() == 1

    async def test_a_suggest_row_stores_the_surface_and_the_tier_that_answered(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """`m10c`'s two columns, round-tripped -- PRD 10's amendment 2.

        **Both tiers, in one case, because a write that hard-coded either
        member passes a case that only ever stores the other.** The pairing is
        also what says the two columns are not filled from each other:
        `surface` reads `suggest` on both rows while `tier` differs, which no
        single-row assertion can distinguish.

        The wrong implementations this kills: `surface` written as the literal
        `'search'`, which is what shipped at `m10c` and was correct only while
        `SearchService.search` was the sole caller; `tier` dropped from the
        statement, leaving the column NULL on a row that names the surface
        whose whole point is which index ran; and the two bound the wrong way
        round, which no `NOT NULL` can catch because both columns take a
        string.

        `mode` is `FULL_TEXT` here rather than the file's `SEMANTIC` default,
        because that is the value a suggest row really carries: neither tier
        embeds and neither fuses.
        """
        rows = {
            tier: search_query_record(
                user_id=user_id,
                query=f"{QUERY[: 4 + len(tier.value)]}",
                mode=SearchMode.FULL_TEXT,
                surface=SearchSurface.SUGGEST,
                tier=tier,
            )
            for tier in SuggestTier
        }
        assert len(rows) == len(SuggestTier) > 1, "the premise: both tiers are distinct rows"

        for record in rows.values():
            await repository.record(record)

        stored = {tier: await ledger.get(record.id) for tier, record in rows.items()}
        assert all(one is not None for one in stored.values()), stored
        assert {tier: one.surface for tier, one in stored.items() if one is not None} == dict(
            dict.fromkeys(SuggestTier, SearchSurface.SUGGEST)
        )
        assert {tier: one.tier for tier, one in stored.items() if one is not None} == {
            tier: tier for tier in SuggestTier
        }
        assert await ledger.count() == len(SuggestTier)

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
        """The storage control, and the one case here that writes both
        columns in a single call -- **no shipped caller does**, deliberately
        (the click writer names a title, the play writer names none), so this
        exists to prove both columns are reachable at all rather than to
        model the funnel.

        The wrong implementation this kills: `record_outcome` that writes
        `played` but not `clicked_title_id`, or updates the wrong row (no
        `WHERE id = ...`, or a dropped `id` parameter)."""
        record = search_query_record(user_id=user_id)
        await repository.record(record)
        title_id = await self.add_title()

        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=title_id, played=True
        )

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id == title_id
        assert stored.played is True

    async def test_a_later_click_does_not_steal_an_earlier_titles_attribution(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """**First write wins, on `clicked_title_id` specifically.** The
        wrong implementation this kills: an unconditional `UPDATE` with no
        guard on that column, which lets a second, genuinely different click
        -- someone else's redelivered event, or a stale retry naming the
        wrong result -- overwrite a real attribution with a less informative
        one.

        `played` is held constant (`False` on both calls) so this case is
        about `clicked_title_id` alone; the sibling cases below are what
        pins `played`'s own, different condition.
        """
        record = search_query_record(user_id=user_id)
        await repository.record(record)
        first_title = await self.add_title()
        second_title = await self.add_title()
        assert first_title != second_title, (
            "the fixture must attribute to two different titles, or a replace and a no-op "
            "look identical"
        )

        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=first_title, played=False
        )
        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=second_title, played=False
        )

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id == first_title

    async def test_a_later_play_reaches_a_query_already_attributed_to_a_click(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """**The funnel `record_outcome` exists to serve, and the one a
        shared `clicked_title_id IS NULL` guard silently drops.** F3's two
        writers fire at two different times on the *same* row: viewing a
        result from a search (`GET /titles/{id}?search_id=…`) attributes the
        click, and playing it (`POST /titles/{id}/play`) is a later, separate
        call that names **no** title and only reports `played`. The wrong
        implementation this kills: a
        guard that keys the whole `UPDATE` off `clicked_title_id IS NULL`,
        which treats this second call as if it were a duplicate delivery of
        the first and silently drops the one fact PRD 10's
        `## Analytics tables` says this table exists to answer -- *did they
        play anything*.

        It also kills a `SET clicked_title_id = :clicked_title_id` with no
        `COALESCE`: the play writer's `None` would blank the attribution the
        click had already earned.
        """
        record = search_query_record(user_id=user_id)
        await repository.record(record)
        title_id = await self.add_title()

        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=title_id, played=False
        )
        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=None, played=True
        )

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id == title_id
        assert stored.played is True

    async def test_a_play_with_no_click_before_it_is_played_with_the_click_still_null(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """**A legal state, not a hole**, and the reason
        `clicked_title_id` is nullable on the argument as well as on the
        column. A client can hold a `search_id` and go straight to
        `POST /titles/{id}/play` -- it never asked Usher for the detail page,
        so nothing told Usher which result it opened. The row then says
        *"this search led to a play, and which result is unknown"*, which is
        a different fact from *"this search led to nothing"* and from
        *"this search led to a click that went nowhere"*.

        The wrong implementation this kills: a `record_outcome` that treats
        an absent click as nothing to do and returns early, so the whole
        no-click half of PRD 10's funnel is silently unrecorded; and, one
        step quieter, a play writer forced to name a title because the
        parameter is not nullable -- which would make `clicked_title_id`
        answer *"the last thing this household did"* rather than *"which
        result it opened"*.
        """
        record = search_query_record(user_id=user_id)
        await repository.record(record)

        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=None, played=True
        )

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id is None
        assert stored.played is True

    async def test_a_search_belonging_to_another_household_is_not_attributed(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """**The scope is a security boundary and this is where it is
        pinned.** A `query_id` reaches this port from a query parameter and
        UUIDv7 is partially time-ordered, so an unscoped `WHERE id = :id`
        lets one household write attribution onto another's row -- silently,
        with no error, no log line and no metric.

        **The positive control is in the same case and is what makes the
        negative half mean anything**: the byte-identical call from the
        owning household must land. Without it a repository whose
        `record_outcome` did nothing at all would pass.

        The wrong implementation this kills: `WHERE id = :id` with the
        `user_id` conjunct dropped -- and, because the two calls differ only
        in that argument, nothing else.
        """
        record = search_query_record(user_id=user_id)
        await repository.record(record)
        title_id = await self.add_title()
        stranger = await self.add_user()
        assert stranger != user_id, (
            "the fixture must supply two different households, or the refusal and the "
            "control are the same call"
        )

        await repository.record_outcome(
            record.id, user_id=stranger, clicked_title_id=title_id, played=True
        )

        refused = await ledger.get(record.id)
        assert refused is not None
        assert refused.clicked_title_id is None, "another household attributed this search"
        assert refused.played is False, "another household reported a play against this search"

        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=title_id, played=True
        )

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id == title_id, (
            "the control: the owning household's identical call must land"
        )
        assert stored.played is True

    async def test_played_does_not_revert_to_false_once_true(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """`played`'s own condition is monotonic -- it only ever moves toward
        `True` -- and that is a decision this case pins rather than leaves
        implicit. The wrong implementation this kills: writing `played` from
        the call's own value unconditionally (`SET played = :played`), which
        would let a later call that has not itself observed a play erase the
        evidence that one already happened -- there is no route in F3's
        design that means "actually, undo the play", so a call carrying
        `played=False` after `played=True` is stale information, not a
        correction.
        """
        record = search_query_record(user_id=user_id)
        await repository.record(record)

        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=None, played=True
        )
        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=None, played=False
        )

        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.played is True

    async def test_attributing_a_query_that_was_never_recorded_is_a_silent_no_op(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """The wrong implementation this kills: a `record_outcome` that
        raises on an unknown id rather than leaving a table it did not
        change alone, which would make a stale or duplicate client callback
        a request failure rather than a fact about a table with nothing to
        update.

        This is also the shape a client holding a `search_id` from a
        database that has since been pruned produces -- PRD 10's retention
        is an operator's `DELETE`, so a stale id outliving its row is
        ordinary rather than hostile.
        """
        unknown = new_id()

        await repository.record_outcome(
            unknown, user_id=user_id, clicked_title_id=None, played=True
        )

        assert await ledger.get(unknown) is None
        assert await ledger.count() == 0

    # -- oldest() and prune(), M10's J5 -------------------------------------

    async def test_the_oldest_row_is_what_min_at_answers_and_an_empty_table_is_none(
        self, repository: SearchQueryRepository, user_id: uuid.UUID
    ) -> None:
        """`SearchQueryRetention.last_done()` is built on this, so both
        halves are contract rather than storage.

        The wrong implementations this kills: `max(at)` in place of `min(at)`,
        which is the identical mistake `SimilarityService.computed_at()`
        refuses one artefact over (*"the newest row would report a whole-table
        rebuild as fresh the moment its first page committed"*) -- here it
        would report a table that has *just been written to* as needing no
        prune, forever. And an empty table answering *some* timestamp rather
        than `None`, which is the difference between "nothing to prune" and a
        fabricated age.

        The three rows are seeded **out of order** (middle, oldest, newest),
        so an implementation answering "the first row written" rather than the
        smallest `at` is a failure rather than a coincidence.

        ⚠️ **Aware, and asserted here rather than only on the Postgres arm.**
        `Scheduler._due_now` subtracts this from an aware `now`; a naive
        answer is a `TypeError` at the tick, not a wrong number. The Postgres
        arm is the one where this is a real round trip through a column type
        and it is the reason the assertion is in the shared suite: a fake that
        hands back what it was given would pass it for the wrong reason if
        nothing else asked.
        """
        assert await repository.oldest() is None, (
            "an empty table has no oldest row and must not invent one"
        )

        middle = datetime(2026, 6, 15, 9, 30, tzinfo=UTC)
        oldest = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        newest = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
        assert oldest < middle < newest, "the fixture must order the three rows it is about"
        for at in (middle, oldest, newest):
            await repository.record(search_query_record(user_id=user_id, at=at))

        answered = await repository.oldest()

        assert answered == oldest
        assert answered is not None and answered.tzinfo is not None, (
            "an aware datetime, or the scheduler's due comparison raises TypeError"
        )

    async def test_pruning_removes_what_is_before_the_cutoff_and_keeps_the_row_exactly_on_it(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """The boundary, and the row *exactly* at the cutoff is the arm that
        makes it one.

        The wrong implementations this kill: `<=` for `<`, which is one
        character and reads as correct either way -- so the case places a row
        at exactly `before` and requires it to survive, because a case that
        only checked "the old row disappeared" passes against both spellings
        and against one that deletes everything. And a prune that ignores its
        argument and deletes the table, which the two surviving rows rule out.

        `cutoff` is 90 days before `AT` only so the arithmetic reads like the
        statement PRD 10 prices; nothing here depends on the number.
        """
        cutoff = AT - timedelta(days=90)
        before = search_query_record(user_id=user_id, at=cutoff - timedelta(microseconds=1))
        exactly = search_query_record(user_id=user_id, at=cutoff)
        after = search_query_record(user_id=user_id, at=cutoff + timedelta(days=1))
        for record in (before, exactly, after):
            await repository.record(record)

        deleted = await repository.prune(before=cutoff, limit=100)

        assert deleted == 1
        assert await ledger.get(before.id) is None
        assert await ledger.get(exactly.id) is not None, (
            "a row answered at exactly the cutoff is inside the window: the statement is `<`"
        )
        assert await ledger.get(after.id) is not None
        assert await ledger.count() == 2

    async def test_a_prune_deletes_at_most_its_limit_and_repeating_it_drains_the_rest(
        self, repository: SearchQueryRepository, ledger: SearchQueryLedger, user_id: uuid.UUID
    ) -> None:
        """Chunking, from the caller's side: the count is the loop's only
        terminator, so it has to be exact.

        The wrong implementations this kills: a `limit` the statement builds
        and ignores, which makes `SearchQueryRetention.run()` hold one
        transaction over a year of keystrokes; a return value that is the
        limit rather than the rows affected, which makes the loop never
        terminate; and a return value that is the *remaining* count, which
        makes it terminate one chunk early and leave rows behind.

        Five expired rows against a limit of two: 2, 2, 1, and the fourth call
        answers 0 with the table already empty. The fourth is not decoration --
        a `prune` that answered its limit unconditionally would pass the first
        three.
        """
        cutoff = AT
        for offset in range(5):
            await repository.record(
                search_query_record(user_id=user_id, at=cutoff - timedelta(days=offset + 1))
            )
        assert await ledger.count() == 5

        answers = [await repository.prune(before=cutoff, limit=2) for _ in range(4)]

        assert answers == [2, 2, 1, 0]
        assert await ledger.count() == 0

    async def test_pruning_a_household_s_searches_takes_neither_the_household_nor_a_title(
        self,
        repository: SearchQueryRepository,
        ledger: SearchQueryLedger,
        user_id: uuid.UUID,
        counts: ReferenceCounts,
    ) -> None:
        """`search_queries` is a leaf, asserted rather than reasoned.

        Its two foreign keys point *outward* -- `user_id` is `ON DELETE
        RESTRICT` and `clicked_title_id` is `ON DELETE SET NULL` -- so nothing
        references these rows and a delete cannot cascade. The wrong
        implementation this kills is a prune spelled through the household
        (`DELETE FROM users ...` with the analytics rows following) or one
        that clears `titles` to satisfy the reference; both are absurd to read
        and neither is absurd to write, because the row being deleted *names*
        both tables.

        The premise is that the deleted row genuinely referenced both: a click
        is attributed to a real title before the prune, so `clicked_title_id`
        is non-`NULL` when the row goes.
        """
        title_id = await self.add_title()
        record = search_query_record(user_id=user_id, at=AT - timedelta(days=365))
        await repository.record(record)
        await repository.record_outcome(
            record.id, user_id=user_id, clicked_title_id=title_id, played=True
        )
        stored = await ledger.get(record.id)
        assert stored is not None and stored.clicked_title_id == title_id, (
            "the premise: the row being pruned really does reference a title"
        )
        before = await counts.read()
        assert before.users >= 1 and before.titles >= 1, (
            "the premise: there is a household and a title that could have been taken"
        )

        assert await repository.prune(before=AT, limit=100) == 1

        assert await ledger.count() == 0
        assert await counts.read() == before
