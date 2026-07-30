"""Ports for persistence: one per aggregate, plus the bulk-load path.

Repositories are driven ports, the same as `SourceAdapter` or
`MetadataProvider` — port named for the role, implementation named for the
technology (ADR-0009). Everything here is an ABC; `usher.db.repositories.*`
holds the Postgres implementations.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from usher.domain.bootstrap import ImportRun
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId


class TitleRepository(ABC):
    """Persistence for canonical titles, kept behind a port so services
    depend on this ABC and never on `usher.db` directly — see ADR-0009.
    `usher.db.repositories.title.PostgresTitleRepository` is the concrete,
    SQLAlchemy-backed implementation; `api/`, the composition root,
    constructs it and injects it into services.

    Every method below shares one session-wide precondition, not just
    advice to whoever implements this port: **the session must carry no
    unflushed, invalid state when any method here is called.** A
    SQL-backed implementation's session typically autoflushes by default,
    so even a pure read can trigger a write — and once more than one
    repository shares a session (from the milestone that adds
    `MediaItemRepository`/`WatchStateRepository`), "nothing else pending is
    broken" stops being a safe assumption to make on this port's behalf.
    `PostgresTitleRepository`'s reads defensively suppress autoflush for
    exactly this reason (see its module docstring), but that is a backstop
    inside one implementation, not a substitute for callers upholding the
    precondition: a caller that leaves invalid state pending across a
    repository boundary can still surface a raw storage exception the next
    time *anything* on that session flushes it, including code this port
    doesn't own.

    Bulk loading deliberately bypasses this port. Measured cost of going
    through it: ~3 statements and ~1.15 ms per `add()` (SAVEPOINT / INSERT
    / RELEASE) against `PostgresTitleRepository` — roughly 4 hours of pure
    repository overhead at M2's ~12.7M skeleton rows, before a single row
    of actual bulk-dataset I/O. `TitleRow`'s server-defaults already exist
    so a raw `COPY` can omit any column it doesn't have data for; M2's bulk
    loader is expected to use that path directly, not this one.
    """

    @abstractmethod
    async def add(self, title: Title) -> None:
        """Persist a new title.

        This is an insert, not an upsert: a duplicate `title.id` — or any
        other unique constraint the backing store enforces — raises
        `RepositoryConflict` (`usher.ports.errors`). Implementations
        translate their backing store's own conflict error (e.g.
        Postgres's `IntegrityError`) into this; callers never import a
        storage-specific exception to handle it. See `update` for
        mutating a title that already exists.

        The caller owns the session and the transaction: this flushes, so
        the row and any conflict are visible immediately, but it never
        commits. Committing or rolling back is the caller's call.
        """

    @abstractmethod
    async def update(self, title: Title) -> None:
        """Persist a mutated, already-existing title — e.g.
        `title.evolve(enrichment_state=EnrichmentState.ENRICHED, ...)`
        after enrichment, which is the read-through design's whole point
        (PRD 03: stub-on-sight, then enrich in place).

        This is an update, not an upsert: a `title.id` that does not
        already exist raises `RepositoryNotFound` (`usher.ports.errors`).
        See `add` for a brand-new title.

        Same session/transaction ownership as `add`: flushes, never
        commits.

        Unconditional last-write-wins: there is no optimistic concurrency
        check (no version column, no `WHERE` clause comparing against the
        row's state as last read) — the incoming `title` simply overwrites
        whatever is currently stored, even if it was read before some
        other write landed. M4's concurrent enrichment (multiple sources
        or workers updating the same title around the same time) will
        eventually need one; not built here.
        """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> Title | None:
        """Fetch by Usher's own id, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        """Fetch by TMDb id *within its namespace*, or None if no title
        carries it.

        `kind` is not optional, and not a convenience filter. TMDb keys
        movies and TV series in separate id spaces that both land in this
        one column, and they overlap heavily: 26,968 of the 56,975 distinct
        TMDb series ids Wikidata knows are also live TMDb movie ids
        (measured 2026-07-30). "Which title has tmdb_id 550" has no single
        answer; "which movie has tmdb_id 550" does. See
        [ADR-0011](../../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).

        Every real caller already knows the kind — M4's matcher reads it off
        the source item alongside `ProviderIds.Tmdb` — so this costs nothing
        it does not already have.
        """

    @abstractmethod
    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        """Fetch by IMDb id, or None if no title carries it."""

    @abstractmethod
    async def count_by_state(self) -> dict[EnrichmentState, int]:
        """Catalog size broken down by enrichment tier.

        Always returns all three `EnrichmentState` members as keys, 0 for
        any tier with no titles — never a sparse dict. A `GROUP BY` only
        returns tiers with at least one row; an implementation must fill
        in the rest itself rather than let the query's own sparsity leak
        through (a bare `counts[EnrichmentState.ENRICHED]` must never
        raise `KeyError` just because nothing is enriched yet).
        """


@dataclass(frozen=True, slots=True)
class BulkWriteResult:
    """What one batch write actually changed, split so a re-import is
    visibly a no-op (`inserted == 0`) rather than indistinguishable from a
    first run."""

    inserted: int
    updated: int


@dataclass(frozen=True, slots=True)
class CrosswalkLinkResult:
    """Outcome of stamping crosswalk pairs onto catalog titles.

    `conflicted` is not an error condition — it is measured and expected.
    TMDb's movie and series id spaces overlap: 26,968 of the 56,975 distinct
    TMDb series ids Wikidata knows are also live TMDb *movie* ids (measured
    2026-07-30), so `titles.tmdb_id` alone cannot identify a TMDb entity and
    the unique index over it is `(tmdb_id, kind)` (ADR-0011). Two different
    IMDb ids also sometimes claim the same TMDb id (569 such ids, same
    measurement); only one can win, and the loser is counted here instead of
    raising.
    """

    linked: int
    unmatched: int
    conflicted: int


class BulkCatalogRepository(ABC):
    """Bulk writes into the catalog, deliberately *not* expressed through
    `TitleRepository`.

    Measured cost of the per-row path: ~3 statements and ~1.15 ms per
    `PostgresTitleRepository.add()` (SAVEPOINT / INSERT / RELEASE). At the
    ~1.13M titles IMDb's retained `titleType`s yield, that is ~22 minutes of
    pure repository overhead; at the full 12.7M rows IMDb lists it is ~4
    hours. `TitleRow`'s columns carry `server_default`s specifically so a raw
    `COPY` can omit every column the bulk path has no value for, and
    `TitleRepository`'s own docstring already reserves this path. Nothing
    here goes through the ORM.

    Every method is idempotent in the sense that matters for resume safety
    -- replaying a batch is an upsert, never a duplicate -- but only
    `upsert_titles` and `apply_ratings` additionally skip a row that did not
    change, which is what lets *those two* report zero on a no-op second
    pass. `upsert_tmdb_ids` and `upsert_crosswalk` have no such guard (see
    their own docstrings): a replay writes -- and counts -- every row again,
    because Postgres's `DISTINCT ON` dedup step must pick a deterministic
    winner regardless of whether anything actually changed, and doing that
    unconditionally is cheaper than an extra `IS DISTINCT FROM` comparison
    for a value nothing reads before the next call. Do not assume the
    stronger claim for those two; nothing about resume-safety requires it,
    since resuming past a batch only needs "replay never duplicates", not
    "replay is invisible".

    Same session/transaction ownership as `TitleRepository`: these flush and
    return counts; they never commit. The caller commits a batch and its
    checkpoint together, which is the whole mechanism behind "resumable".

    **`bulk_load_window` is the one deliberate exception to "never commit"**
    — see its own docstring for what it commits and the precondition that
    places on the caller. Every other method above keeps the rule.
    """

    @abstractmethod
    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        """Scope inside which the implementation may relax storage-level
        optimisations that only pay for themselves on one-row-at-a-time
        writes, restoring them on exit.

        **Precondition: the caller must have no uncommitted work on this
        session it is not prepared to have committed before entering this
        context manager.** Unlike every other method on this port, an
        implementation of `bulk_load_window` may need to commit the session
        to make schema changes durable and lock-free for the rest of the
        window — and because it is *the caller's own session*, that commit
        is not scoped to whatever this call itself changed. It commits
        everything currently pending, exactly the way calling
        `session.commit()` directly always does. This is not a hypothetical
        edge case reachable only by misuse: `PostgresBulkCatalogRepository`
        exercises it on every first bootstrap (the common case, an empty
        catalog), and its docstring records why no alternative avoids it
        (a second connection deadlocks against locks this session already
        holds; flipping this session to autocommit requires ending its
        transaction first anyway). A caller that must keep other pending
        work uncommitted across this call needs its own, separate session
        for that work.

        Named for the role, not the mechanism, because the mechanism is
        Postgres-specific: `PostgresBulkCatalogRepository` drops
        `ix_titles_sort_name` and `ix_titles_name_lower_year` and rebuilds
        them afterwards. Its own docstring states when it declines to (a
        non-empty `titles`, so the catalog stays browsable) and why the
        three *unique* partial indexes are never touched — the upserts
        below name them in `ON CONFLICT` and would fail without them.

        Exits cleanly on an exception, restoring whatever it suspended: a
        crashed import must not leave the catalog missing an index. A
        process killed mid-window can, which is why the restore is
        idempotent and rerun at the start of the next window.
        """

    @abstractmethod
    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        """Insert or update skeleton titles, keyed on `imdb_id`.

        New rows get a fresh UUIDv7 (`usher.domain.ids.new_id`) and
        `enrichment_state = skeleton` from the column default. Existing rows
        keep their id, their `created_at`, and every enrichment-tier field —
        a re-import refreshes what IMDb actually supplies (name, year,
        runtime, genres) and must never downgrade an enriched title.

        `updated` counts rows whose IMDb-supplied fields genuinely changed,
        not rows re-seen: an unchanged replay writes nothing at all, so the
        `set_updated_at` trigger does not fire across a million untouched
        rows.
        """

    @abstractmethod
    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        """Set `community_rating`/`vote_count` on titles that already exist,
        returning how many rows changed.

        Never creates a title: `title.ratings.tsv.gz` covers `titleType`s
        this milestone drops, and a rating with no title is not a catalog
        entry. Rows whose values already match are left alone, for the same
        trigger reason as `upsert_titles`.
        """

    @abstractmethod
    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        """Insert or update the TMDb id universe, keyed on
        `(tmdb_id, kind)`. Returns rows written."""

    @abstractmethod
    async def upsert_crosswalk(self, rows: Sequence[IdCrosswalkPair]) -> int:
        """Insert or update crosswalk pairs, keyed on `imdb_id`.

        A pair carrying only `tmdb_series_id` must not blank a previously
        stored `tmdb_movie_id` for the same IMDb id — the three SPARQL joins
        each fill one column, and they run as three separate passes.
        Returns rows written.
        """

    @abstractmethod
    async def link_crosswalk(self) -> CrosswalkLinkResult:
        """Stamp stored crosswalk pairs onto catalog titles, in one pass.

        Only fills a `tmdb_id`/`tvdb_id` that is currently NULL, so a value
        a later, better-informed enrichment wrote is never overwritten by
        the crosswalk -- this is a precondition on the *target* row, checked
        independently of whether the incoming pair agrees with what is
        already stored: a title that already carries a *different* value
        does not get overwritten either, and is reported as `conflicted`,
        not `linked`. Copies `popularity` across from the TMDb id universe
        at the same time, which is what makes `ix_titles_popularity` usable
        and gives M4's enrichment queue a real ordering.

        Both `titles.tmdb_id` (scoped by `kind`, ADR-0011) and
        `titles.tvdb_id` are globally unique where not NULL
        (`ix_titles_tmdb_id_kind`/`ix_titles_tvdb_id`), and the crosswalk
        data itself is not: two different IMDb ids can each name the same
        TMDb or TVDB id. At most one title may ever hold a given
        `(tmdb_id, kind)` or `tvdb_id` — an implementation must pick a
        single, deterministic winner among competing pairs rather than
        raise or leave the outcome to whichever row a scan reaches first.

        Idempotent: a second call over unchanged inputs reports
        `linked == 0`.
        """

    @abstractmethod
    async def count_titles(self) -> int:
        """How many titles the catalog holds. Used to decide whether
        `bulk_load_window` may suspend indexes, and reported by the CLI."""


class ImportRunRepository(ABC):
    """Checkpoint storage for resumable bulk imports.

    One row per dataset, holding the cursor its last committed batch
    produced. `TitleRepository`'s session/transaction ownership applies here
    too, and matters more: `save` must be flushed inside the *same*
    transaction as the batch it describes, or a crash between the two either
    loses work or claims work that was rolled back.
    """

    @abstractmethod
    async def start(self, dataset: str, revision: str) -> ImportRun:
        """Begin or resume a run for `dataset`.

        Returns the run with its cursor fields preserved when `revision`
        matches what was stored, and reset to zero when it does not — an
        upstream snapshot change restarts the import rather than splicing
        two snapshots. Either way the returned run is `RUNNING` with `error`
        and `finished_at` cleared, and it has already been persisted.
        """

    @abstractmethod
    async def save(self, run: ImportRun) -> None:
        """Persist a run's progress. Flushes, never commits.

        Raises `RepositoryConflict` if another row already claims this
        run's `dataset` — two processes bootstrapping the same dataset at
        once is an operator mistake, and it must surface as a port error
        rather than a raw storage exception (ADR-0009).

        Whether the *session* remains usable for further work after a
        caught `RepositoryConflict` is deliberately left to the
        implementation, not promised here — contrast `TitleRepository.add`/
        `update`, which use a `SAVEPOINT` specifically so it does.
        `PostgresImportRunRepository` chooses not to (see its own module
        docstring): this repository's only caller is a single bootstrap
        process, a conflict here means two importers are racing on the same
        dataset, and there is no sensible way to continue that transaction
        regardless.
        """

    @abstractmethod
    async def get(self, dataset: str) -> ImportRun | None:
        """The stored run for `dataset`, or None if it has never run."""

    @abstractmethod
    async def list_runs(self) -> list[ImportRun]:
        """Every stored run, most recent activity first — what the CLI's
        `bootstrap-status` prints."""
