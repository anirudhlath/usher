"""Search queries -- PRD 10's live measurement of what a household typed and
what happened next.

Implemented by
`usher.db.repositories.search_query.PostgresSearchQueryRepository`.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.ports.search import SearchMode

__all__ = [
    "SearchQueryRecord",
    "SearchQueryRepository",
]


@dataclass(frozen=True, slots=True)
class SearchQueryRecord:
    """One search, exactly as `SearchService` already knows it the moment it
    answers -- the retrieval half of `search_queries`' nine columns
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
    """

    id: uuid.UUID
    at: AwareDatetime
    user_id: uuid.UUID
    query: str
    mode: SearchMode
    result_count: int
    latency_ms: int


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
    - `clicked_title_id`, `played` -- **F3**, written later by
      `record_outcome()`, from whatever client action PRD 07's search routes
      grow to report it.

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
    - **`search_queries` carries no index beyond its primary key**
      (`genome_tags`' precedent, PRD 09's boundary call 9: an index whose only
      reader is a later milestone is a cost with no payer), so
      `fk_search_queries_clicked_title_id_titles`'s `SET NULL` scans this
      table on every title delete. M1 recorded the gap rather than repairing
      it; this task reports it rather than silently adding the index.

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
        self, query_id: uuid.UUID, *, clicked_title_id: uuid.UUID, played: bool
    ) -> None:
        """Attribute a search to what happened next -- **F3's write**,
        covering `clicked_title_id` and `played`.

        Updates the row `record()` wrote, keyed by its `id`. **First write
        wins.** A row that already carries a non-`NULL` `clicked_title_id` is
        left exactly as it was -- a redelivered or duplicated attribution
        cannot replace a real click with a later, less-informative one. This
        is also why the table needs no `updated_at` column and no trigger: a
        row receives at most one insert and at most one outcome update over
        its whole life, which is `llm_calls`' shape rather than
        `watch_states`' (a row updated many times as watching continues).

        A `query_id` naming no row, and a `query_id` naming a row already
        attributed, are both silent no-ops -- there is no signal a caller
        could act on differently between "nothing to attribute" and "already
        attributed", and PRD 08's redelivery rule already asks for the second
        to be harmless.

        **Raises `RepositoryConflict`** for a `clicked_title_id` naming no
        title (`fk_search_queries_clicked_title_id_titles`) -- a stale or
        forged id reaching this from a client is a caller-assembly mistake,
        not a storage failure a caller could usefully retry.
        """
