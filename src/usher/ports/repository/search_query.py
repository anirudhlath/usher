"""Search queries -- PRD 10's live measurement of what a household typed and
what happened next.

Implemented by
`usher.db.repositories.search_query.PostgresSearchQueryRepository`.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime

from usher.ports.search import SearchMode, SearchSurface, SuggestTier

__all__ = [
    "SearchQueryRecord",
    "SearchQueryRepository",
]


@dataclass(frozen=True, slots=True)
class SearchQueryRecord:
    """One answered request, exactly as `SearchService` already knows it the
    moment it answers -- the retrieval half of `search_queries`' eleven columns
    (`docs/prd/10-telemetry-and-dashboards.md`'s two-halves table).

    **`clicked_title_id` and `played` are deliberately not fields here.**
    Neither is knowable at the instant a search answers -- a click and a play
    are things a client does *afterwards* -- and a constructor that could be
    handed them and then have to leave them unset is a constructor somebody
    eventually fills in with a guess. They are written later, by
    `SearchQueryRepository.record_outcome`, keyed by `id`.

    Not `usher.domain.search.SearchAnswer` and not a domain model at all --
    `domain/` imports nothing and this carries a `SearchMode`, which is a port
    type (`usher.ports.search`). `SearchAnswer`'s own docstring makes the
    identical argument for the identical reason
    (`usher/services/search.py:245`: *"Lives here rather than in
    `usher.domain.search` because it carries a `SearchMode`, which is a port
    type, and `domain/` imports nothing"*). A port DTO beside the port it
    belongs to, the shape `StoredTaste`, `NeighborSeed` and
    `TitleEmbeddingUpsert` already have.

    **No pydantic bounds on `result_count` or `latency_ms`, unlike a domain
    model.** Both are plain `int`, so nothing here stops a caller from handing
    `record()` a `latency_ms` the `search_queries.latency_ms` `integer` column
    cannot hold -- the repository is where that gets refused, and refuses it
    as `RepositoryConflict` rather than letting a raw driver exception cross
    the port boundary. See `SearchQueryRepository.record`.

    `id` is minted by the caller (`usher.domain.ids.new_id`), following
    `LLMCall.id` and every other UUIDv7 primary key in this schema -- the
    repository does not choose it, so a caller that needs the id before the
    row is durable (`record_outcome` is keyed by it) already has one.

    **`surface` is required and is deliberately not defaulted to `SEARCH`.**
    A default here is the *"supply a plausible wrong value to a writer that
    forgot"* failure `m09d`'s own migration comment names, one layer up from
    the database: `m10c` lands the column `NOT NULL` with no `server_default`
    for exactly that reason, and a dataclass default would put the hazard back
    where no migration can see it. Every writer states which surface it is.

    **`mode` on a `SUGGEST` row is `SearchMode.FULL_TEXT`**, and the choice is
    stated here beside `surface` because `mode` is the column every dashboard 1
    and 4 panel groups by. Both suggest tiers are btree/GIN reads with no embed
    and no fusion, which is what that member already means; a suggest row
    landing under `FUSED` would attribute a keystroke to a lane that has never
    run. The consequence travels with it and is recorded in PRD 10 rather than
    discovered from a skewed panel: **every mode-split panel now has to filter
    on `surface`**, which is a `WHERE surface = 'search'` those panels did not
    previously need. Keeping the two vocabularies in two columns is the whole
    of amendment 2 -- a fourth `SearchMode` member was the alternative and was
    declined, because `SearchMode` is `GET /search`'s `?mode=`.

    **The invariant, and both directions of it.** `surface == SEARCH` implies
    `tier is None`; `surface == SUGGEST` implies `tier is not None`. It is
    checked here rather than by a CHECK constraint, because this schema's
    constraints are Pydantic's (`db/base.py`'s `enum_column`: *"Pydantic owns
    membership validation, not the database"*) -- and it is checked in **both**
    directions, because a validator that refuses only one of them passes a test
    that only tries one. A `search` row with a tier would claim an index the
    search lanes do not have; a `suggest` row without one is the half ADR-0031
    actually wants measured, silently absent.
    """

    id: uuid.UUID
    at: AwareDatetime
    user_id: uuid.UUID
    query: str
    mode: SearchMode
    result_count: int
    latency_ms: int
    surface: SearchSurface
    #: `None` on a `search` row and never on a `suggest` one -- see the
    #: invariant above. Last and defaulted because it is the one field of the
    #: nine here that is genuinely *absent* rather than unknown; `surface`
    #: above it is required precisely so this default cannot be reached by a
    #: writer that forgot which surface it was.
    tier: SuggestTier | None = None

    def __post_init__(self) -> None:
        if self.surface is SearchSurface.SEARCH and self.tier is not None:
            raise ValueError(
                f"a {SearchSurface.SEARCH.value} row carries no tier, and this one names "
                f"{self.tier.value}: no search lane is a SuggestIndex"
            )
        if self.surface is SearchSurface.SUGGEST and self.tier is None:
            raise ValueError(
                f"a {SearchSurface.SUGGEST.value} row must name the tier that answered; "
                "which of the two indexes ran is what ADR-0031 exists to measure"
            )


class SearchQueryRepository(ABC):
    """`search_queries` -- one row per answered search, and what it led to
    (`docs/prd/10-telemetry-and-dashboards.md`'s `## Analytics tables`).

    Landed whole by `m09a` (Task M1) on the argument PRD 10 makes at length:
    *"a half-populated analytics table is worse than an empty metric"*,
    because a `NULL` in `clicked_title_id` is genuinely ambiguous between
    "not implemented" and "the household searched and clicked nothing" --
    and the second reading is exactly the signal the column exists to carry.
    So every column needs a named writer before this table means anything,
    and **this docstring is the one copy of that mapping** -- PRD 10's own
    two-halves table restates it and points here rather than carrying a
    second copy that can drift.

    - `id`, `at`, `user_id`, `query`, `mode`, `result_count`, `latency_ms` --
      **F2**, written together by `record()` at the moment a search answers.
    - `surface`, `tier` -- **M10's `m10c` and J2**, written by the same
      `record()` call, because which surface asked is known at the instant it
      answers and never afterwards. `surface` is `search` from
      `SearchService.search` and `suggest` from `SearchService.suggest`; `tier`
      is the `SuggestTier` the caller selected on the second and `None` on the
      first. **Two callers where there was one**, which is the amendment PRD 10
      named for M10 to plan rather than rediscover.
    - `clicked_title_id`, `played` -- **F3**, written later by
      `record_outcome()`, one column per client action and never both by one
      caller: `GET /titles/{id}?search_id=…` reports the click and
      `POST /titles/{id}/play` (or `/episodes/{id}/play`) reports the play.

    **One divergence from PRD 10's own grouping, recorded rather than
    smoothed over.** PRD 10 groups `user_id` with the outcome half, because on
    an authenticated deployment it needs the seam PRD 01 leaves open. This
    milestone has no authentication (boundary call, group F preamble) --
    `user_id` is the singleton default user -- so it is already known at
    query time and costs nothing to write early. **F2 writes it.**

    **This table's schema is `m09a`'s, not this docstring's to change** --
    F1 owns no migration and no DDL, and reads what M1 shipped rather than
    what an earlier draft of this plan proposed. Two facts about it worth not
    rediscovering the hard way, both already true of the table this port is
    written against:

    - **`clicked_title_id` carries a real foreign key to `titles.id`, `ON
      DELETE SET NULL`** -- not the "no foreign key at all" an earlier draft
      of this group's plan argued for, which is stale against what M1 actually
      shipped. `SET NULL` reproduces, for one row, the exact ambiguity PRD 10
      spends a paragraph refusing at the table's whole-column grain -- a title
      deleted after being clicked reads back identically to a query that was
      never clicked at all. Recorded here as the trade-off it is: the
      alternative (no FK) trades that ambiguity for an unenforced reference a
      dashboard would have to defend against instead, and M1 chose the FK.
    - **`search_queries` carried no index beyond its primary key from `m09a`
      to `m10b`** (`genome_tags`' precedent, PRD 09's boundary call 9: an
      index whose only reader is a later milestone is a cost with no payer),
      so `fk_search_queries_clicked_title_id_titles`'s `SET NULL` scans this
      table on every title delete. M1 recorded the gap rather than repairing
      it; F1 reported it rather than silently adding the index.
      ⚠️ **`m10c` added `ix_search_queries_at` and that is not this gap
      closing.** That index is on `at`, for PRD 10's retention `DELETE`; a
      `SET NULL` on `clicked_title_id` cannot use it, so the scan above is
      unchanged and the sentence that would go stale here is the *premise*
      rather than the consequence.

    Same session ownership as every other repository here: methods flush and
    return, and never commit.
    """

    @abstractmethod
    async def record(self, record: SearchQueryRecord) -> None:
        """Insert one row at query time -- **F2's write**.

        `clicked_title_id` is written `NULL` and `played` is written `false`,
        both as literals rather than left to a column default: the table
        declares no default for either (`played` is `NOT NULL` with none at
        all, `llm_calls.ok`'s precedent), because neither is knowable at the
        instant a search answers. **This method does not decide when they
        become true** -- that is `record_outcome`'s whole job, called later
        and by a different task (F3).

        Idempotent only in the sense a fresh UUIDv7 makes collision
        unreachable in practice; there is no upsert here; a repeat `id` is a
        conflict rather than a silent second write, matching
        `TitleRepository.add` and `LLMCallRepository.record`.

        **Raises `RepositoryConflict`** for anything the backing store
        refuses about the row, translated rather than left as a raw driver
        exception (ADR-0009). The enumeration is by outcome, not by
        constraint kind: a duplicate `id` (`pk_search_queries`), a `user_id`
        naming no household (`fk_search_queries_user_id_users`, `ON DELETE
        RESTRICT` -- a household's search history outlives nothing but the
        household itself), an empty `query`
        (`ck_search_queries_query_not_empty`), and **a value a column cannot
        hold at all**, which is not a constraint: `result_count` and
        `latency_ms` are plain, unbounded `int` on `SearchQueryRecord` against
        an `integer` column each -- the identical shape
        `curated_rows."position"` and `genome_tags.tag_id` measured
        (`.claude/rules/db-and-sql.md`). `2**31` is refused **client-side** by
        asyncpg's own binary encoder before a byte reaches Postgres --
        `sqlalchemy.exc.DBAPIError`, `exc.orig.__cause__` an
        `asyncpg.exceptions.DataError`, SQLSTATE `22000`, no named
        constraint -- so this repository catches on
        `is_row_refusal()` / `ROW_REFUSED_SQLSTATE_CLASSES`
        (`db/repositories/_errors.py:76-94`), never on a bare
        `IntegrityError`, which would let that one cross the port boundary
        raw.

        The session stays usable for the caller's other pending work either
        way -- the SAVEPOINT this is built on is the same one
        `LLMCallRepository.record` and `CuratedRowRepository.replace_for_user`
        use, and for the identical reason: a refused analytics write must not
        poison whatever else the caller's transaction is holding.
        """

    @abstractmethod
    async def record_outcome(
        self,
        query_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        clicked_title_id: uuid.UUID | None,
        played: bool,
    ) -> None:
        """Attribute a search to what happened next -- **F3's write**,
        covering `clicked_title_id` and `played`.

        Updates the row `record()` wrote, keyed by its `id` **and scoped to
        the household that owns it**. **Two columns, two different
        conditions, deliberately not one shared guard** -- an
        earlier version of this method keyed the whole write off
        `clicked_title_id IS NULL`, which is wrong and was corrected in
        review before it shipped: F3's own funnel calls this method *twice*
        on the same row, at two different times --
        `GET /titles/{id}?search_id=…` attributes the click, and
        `POST /titles/{id}/play` reports the play -- and a guard shared
        between the columns cannot tell that legitimate
        second call apart from a redelivered duplicate of the first, so it
        silently dropped the one fact PRD 10's `## Analytics tables` says
        this table exists to answer: *did they play anything*.

        - **`clicked_title_id` is first write wins.** A row that already
          carries a non-`NULL` value is left exactly as it was on that
          column -- a later, genuinely *different* click (someone else's
          redelivered event, or a stale retry naming the wrong result) must
          not steal credit from the result the household actually opened.
        - **`played` is monotonic and moves only toward `True`
          (`played = played OR :played`).** There is no route in F3's design
          that means "actually, undo the play", so a call carrying
          `played=False` after an earlier `played=True` is stale information
          about a fact the row already has, never a correction to write over
          it.

        Both conditions are evaluated in the same statement and neither
        blocks the other -- the click-then-play sequence above lands
        `clicked_title_id` once (on the first call) and `played` once (on
        the second), which is exactly the shape that needs two independent
        conditions rather than one.

        **`clicked_title_id` is nullable *on the argument*, and that is what
        keeps the two writers from becoming one.** The click writer passes a
        title and `played=False`; the play writer passes `played=True` and
        **no title at all**. A play writer that named the title being played
        would be one writer setting both columns, which is exactly the shape
        PRD 10 refuses -- `clicked_title_id` would then answer *"the last
        thing this household did with this search"* rather than *"which
        result it opened"*, and a play that never had a click would be
        indistinguishable from one that did. `COALESCE(clicked_title_id,
        NULL)` is the column unchanged, so passing `None` is a write that
        touches `played` alone. **A row carrying `played = true` and
        `clicked_title_id IS NULL` is therefore a legal, meaningful state**:
        the household played a result of this search without Usher ever
        being told which one it opened first.

        **`user_id` is a predicate, not a value written, and it is a security
        boundary rather than tidiness.** A `query_id` is client-supplied --
        it arrives on `?search_id=` -- and UUIDv7 is partially time-ordered
        and therefore partially guessable, so without the scope one household
        writes attribution onto another's row silently, with no error, no log
        line and no metric (`services/rows/cache.py`'s own words for the same
        failure one key over). A row whose `user_id` does not match is a
        no-op, indistinguishable to a caller from a row that does not exist,
        because a caller must not be able to tell those apart either.

        **This is still why the table needs no `updated_at` column and no
        trigger.** A row receives at most one insert and at most *two*
        outcome updates over its whole life -- bounded by F3's two writers,
        never by a redelivery count -- which is a fixed, small ceiling
        rather than `watch_states`' shape (a row updated indefinitely often
        as watching continues).

        A `query_id` naming no row is a silent no-op -- there is no signal a
        caller could act on differently between "nothing to attribute" and a
        stale or duplicate client callback, and both conditions above are
        already idempotent under a genuine redelivery of the identical call:
        the same `clicked_title_id` and the same `played` value reaching
        either condition again changes nothing.

        **Raises `RepositoryConflict`** for a `clicked_title_id` naming no
        title (`fk_search_queries_clicked_title_id_titles`) -- a stale or
        forged id reaching this from a client is a caller-assembly mistake,
        not a storage failure a caller could usefully retry. Reachable only
        when the value is actually written: `COALESCE` means a row already
        carrying a click never re-evaluates the parameter this call passed,
        and the play writer passes no title at all, so **neither of F3's two
        shipped callers can reach it** -- the click writer names the title
        whose row it has just read out of `titles`. It is the contract for
        the caller that has not been written yet.
        """

    @abstractmethod
    async def oldest(self) -> AwareDatetime | None:
        """`min(at)` -- when the oldest surviving row was answered, or `None`
        for an empty table. **M10's J5**, and the only read on this port.

        **This is not a read surface and it is not the exception the port's
        own docstring declines.** `SearchQueryRepository` has no read method
        because this table's readers are PRD 10's dashboards and *"an index
        whose only reader is a later milestone is a cost with no payer"*; one
        aggregate over one column is not a dashboard and answers nothing about
        any individual row. What it *is* is the input to
        `SearchQueryRetention.last_done()` -- the artefact reading ADR-0046's
        no-state design makes every registration owe -- so the alternative to
        it is a scheduler table, which is the thing that record exists to
        refuse. ⚠️ **The neighbouring constraint this paragraph used to name --
        `test_the_cost_ledger_has_no_read_method` -- retired in M10's D3**,
        which gave `LLMCallRepository` the windowed `list_since` that
        `ix_llm_calls_at` was shipped for. It says nothing about this port:
        `llm_calls` gained a read because a caller in `src/` needed one, and
        `search_queries` still has none. What is left of that constraint is
        `tests/unit/test_ports.py`'s parametrised entry, pinning
        `LLMCallRepository`'s abstract set as an exact set -- and **this port
        is not on that list**, so the paragraph above is the whole of what
        stands between `oldest()` and a second read beside it.

        ⚠️ **Aware, always, and the port says so because nothing else can.**
        `Scheduler._due_now` subtracts this from `datetime.now(UTC)`, and a
        naive answer raises `TypeError: can't subtract offset-naive and
        offset-aware datetimes` rather than answering wrongly.
        `search_queries.at` is `TIMESTAMP WITH TIME ZONE` (`m09a`, and read
        off `information_schema` on the live table), so the shipped
        implementation gets this from the column -- but a `TIMESTAMP` column
        one table over would not, which is why the obligation is written here
        and pinned through a real round trip rather than against a hand-built
        value.

        Cheap by construction rather than by luck: `ix_search_queries_at`
        (`m10c`) makes it an Index Only Scan of one leaf. Re-measured
        2026-09-07 on `usher_j2`, the clone of the live catalog holding
        **14,978 rows** -- `Heap Fetches: 1`, **4 buffers** (`shared hit=4`
        warm), median **0.041 ms** over seven samples (0.037-0.048).

        ⚠️ **The fetch count is 1, not the 0 this docstring claimed**, and
        all three counters moved. The obvious explanation -- a page the
        visibility map does not mark all-visible -- was checked and is
        **false**: `pg_class` reports `relpages = relallvisible = 240`,
        `n_dead_tup = 0`, and 240 is also the table's actual size, so the map
        covers every page. Why the scan still takes one heap fetch was not
        determined and is not worth a walk of Postgres internals here; what
        the caller needs is the cost, and the cost is lower than the number
        it replaces. Recorded as measured rather than explained.
        """

    @abstractmethod
    async def prune(self, *, before: datetime, limit: int) -> int:
        """Delete up to `limit` rows answered before `before`. Returns how
        many were deleted. **M10's J5**, PRD 10's retention statement.

        **`before` is a value the caller computed, never an interval this
        method interpolates.** The retention *policy* -- how long a query is
        kept -- is a setting a service reads
        (`USHER_SEARCH_QUERY_RETENTION_DAYS`), and a port that took
        `'90 days'` would be a port that took SQL. It also makes the boundary
        deterministic: a cutoff computed once from an injected clock is the
        same value for every chunk of one run, where `now()` inside the
        statement is `transaction_timestamp()` and `clock_timestamp()` moves
        under the loop (`.claude/rules/db-and-sql.md` carries both traps).

        **`limit` has no default here, on this port or on any
        implementation.** A default in three signatures is three numbers:
        `list_unwatched_candidates` shipped `limit: int = 200` on the port,
        on `PostgresTitleRepository` and on `FakeTitleRepository`, and
        setting the fake's to 5 left the whole unit suite green while setting
        the Postgres one's to 5 left the whole integration suite green --
        because no contract case called without a limit while seeding more
        than five candidates. The two arms of a contract suite could disagree
        about the size of the artefact the suite exists to pin, and an
        assertion that three literals are equal is a check that runs after
        the drift. The caller says how big a chunk is; there is one such
        caller and it reads a setting.

        **Strictly `<`, never `<=`.** A row answered at exactly `before` is
        inside the window by one microsecond of definition and is kept, which
        is the boundary PRD 10's own statement draws
        (`at < now() - interval '90 days'`). The two spellings are one
        character and both read as correct, so the contract states which and a
        case asserts it against a row placed exactly there.

        **The delete is a leaf and cannot cascade.** `search_queries`' two
        foreign keys point *outward* -- `fk_search_queries_user_id_users` is
        `ON DELETE RESTRICT` and `fk_search_queries_clicked_title_id_titles`
        is `ON DELETE SET NULL`, both read off `pg_constraint` on the live
        table -- and nothing anywhere references a `search_queries` row, so
        removing one takes no household and no title with it. Asserted rather
        than reasoned: the contract counts both tables either side.

        A caller that wants the whole population gone calls this until it
        answers fewer than `limit`, committing between chunks. **That loop
        terminates for a reason the neighbour rebuild's does not** -- a
        deleted row cannot re-satisfy the predicate, where
        `SimilarityService.rebuild`'s *"re-read what looks stale, rebuild,
        repeat"* does not terminate against a row the predicate cannot clear
        -- so the loop needs no keyset cursor and does not have one.
        """
