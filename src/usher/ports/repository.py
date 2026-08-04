"""Ports for persistence: one per aggregate, plus the bulk-load path.

Repositories are driven ports, the same as `SourceAdapter` or
`MetadataProvider` — port named for the role, implementation named for the
technology (ADR-0009). Everything here is an ABC; `usher.db.repositories.*`
holds the Postgres implementations.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from pydantic import AwareDatetime

from usher.domain.bootstrap import ImportRun
from usher.domain.collection import Collection
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.people import Credit, CreditKind, Person
from usher.domain.source import MediaItem, Source
from usher.domain.sync import SyncRun, SyncRunKind
from usher.domain.title import Title
from usher.domain.watch import WatchState
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.ingest import (
    MediaItemTarget,
    MediaItemUpsert,
    NameYearProbe,
    ProviderRef,
    SweepResult,
    WatchStateMerge,
)


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
        (measured 2026-07-30). "Which title has tmdb_id 90000550" has no single
        answer; "which movie has tmdb_id 90000550" does. See
        [ADR-0011](../../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).

        Every real caller already knows the kind — M4's matcher reads it off
        the source item alongside `ProviderIds.Tmdb` — so this costs nothing
        it does not already have.
        """

    @abstractmethod
    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        """Fetch by IMDb id, or None if no title carries it."""

    @abstractmethod
    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        """Every title named by `title_ids` that still exists, in any order.

        **A missing id is an omission, never an error.** A title deleted
        between an index write and a search read is ordinary, and the caller
        re-orders by its own ranking anyway — so returning fewer rows than
        asked for is the contract, and a caller that indexes the result by id
        must tolerate the gap.

        Exists because hydrating a 50-hit result set through `get()` is 50
        statements per search: the same round-trip-per-item shape `index_many`
        was introduced to delete from `SearchIndex`, arriving from the other
        direction.
        """

    @abstractmethod
    async def resolve_tmdb_ids(
        self, kind: TitleKind, tmdb_ids: Sequence[int]
    ) -> dict[int, uuid.UUID]:
        """`tmdb_id` -> title id **within one id space**, in one round trip.

        The reverse of `get_by_tmdb_id`, batched, and it exists for the walk
        that starts from a *payload*. `raw_payloads` has no `title_id` and no
        foreign key to `titles` (ADR-0016), so a derivation's only way back to
        a title is `(provider, kind, reference)` -- and `kind` is half of that
        key, not a convenience filter. ADR-0011: TMDb keys movies and series
        in separate spaces that overlap on 26,968 measured ids, so a
        resolution keyed on the integer alone attaches a series' cast to a
        film, silently, with the right counts.

        **Named to match `PersonRepository.resolve_tmdb_ids` and
        `CollectionRepository.resolve_tmdb_ids`**, which do the same job for
        the same reason one table over. It takes a `kind` where those two do
        not, because those two id spaces are not namespaced and this one is.

        **Ids rather than `Title`s.** The caller needs a `Credit.title_id` and
        a link target, not 31 columns per row; `list_by_ids` is the method for
        hydration and keeping the two apart is what stops a derivation from
        pulling the whole catalog through a page walk.

        A batch rather than one: a derivation page is 500 payloads, and a
        lookup per payload is the round-trip-per-item shape batching exists to
        remove.

        **Absent keys mean "no such title", never "not asked", and never an
        error.** `raw_payloads` outlives `titles`, so a payload naming a title
        deleted since the fetch is ordinary -- the same call `IndexService`
        makes when a title vanishes between the sweep and the claim. An
        implementation that raised would let one deleted title abort a whole
        derivation page.
        """

    @abstractmethod
    async def credit_names_for(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, ...]]:
        """Weight class B's input for a page of titles.

        **Not a field on `Title`, and this method is the consequence.**
        `credit_names` is in `DERIVED_COLUMNS` -- it is `credits` projected to
        names and truncated to a ranking constant, so a domain model carrying
        it would be a cast list that is not the cast, on the object
        `GET /titles/{id}` hydrates from, and `title.evolve(credit_names=...)`
        would spell an array that disagrees with the `credits` table. The
        composer needs it anyway, because the fingerprint it computes has to
        reproduce `_FINGERPRINT_SQL` byte for byte and that predicate reads
        the column.

        It is also the only port-level read of the array, which is what lets a
        *contract* case assert that `credit_names` and `credits` never
        disagree. Without it that property is assertable only against raw SQL
        on one side and a fake's private dict on the other -- two assertions
        about two implementations rather than one about the contract.

        Batch, so the backfill pays one read per page rather than one per
        title. **A title with no credits is present in the answer with an
        empty tuple, not absent**: the assembly is *positional* and an absent
        key would be spelled as a missing segment, which is precisely failure
        mode (a) in `services/search.py`'s module docstring. A `title_id`
        naming no row at all is absent, which is `list_by_ids`' rule and is a
        different thing.
        """

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
        `PostgresImportRunRepository` rolls back the whole transaction
        instead of using a SAVEPOINT (see its own module docstring): unlike
        `TitleRepository`'s general-purpose callers, its one caller,
        `BootstrapService`, never has other work pending on the session at
        this point worth a SAVEPOINT's extra round trip to protect. The
        session *does* stay usable afterward, deliberately —
        `BootstrapService.import_dataset`'s except handler continues on
        this same session to record the failure as a durable `ImportRun`,
        which is exactly why the rollback is there rather than skipped.
        """

    @abstractmethod
    async def get(self, dataset: str) -> ImportRun | None:
        """The stored run for `dataset`, or None if it has never run."""

    @abstractmethod
    async def list_runs(self) -> list[ImportRun]:
        """Every stored run, most recent activity first — what the CLI's
        `bootstrap-status` prints."""


class SourceRepository(ABC):
    """Persistence for configured sources.

    Same session/transaction ownership as `TitleRepository`: every method
    flushes so conflicts surface immediately, none commits.

    Credentials are deliberately absent from this port. `Source` carries
    only `credentials_ref`, an opaque pointer, and the secret itself lives
    behind `CredentialStore` (`usher.ports.credentials`) -- so a read here,
    which is what the admin API performs, cannot return a credential even
    by accident. That split is PRD 08's "credentials are never returned by
    any API, including admin", expressed as a type rather than as a rule.
    """

    @abstractmethod
    async def add(self, source: Source) -> None:
        """Insert. A duplicate id raises `RepositoryConflict`."""

    @abstractmethod
    async def update(self, source: Source) -> None:
        """Update an existing row. An unknown id raises
        `RepositoryNotFound`. Writes every mutable column it is given,
        including `device_id` -- PRD 08's key/credential rotation and a
        deliberate device rotation both go through here."""

    @abstractmethod
    async def get(self, source_id: uuid.UUID) -> Source | None:
        """Fetch by id, or None."""

    @abstractmethod
    async def list_all(self) -> list[Source]:
        """Every configured source, ordered by name. Includes disabled ones:
        `GET /admin/sources` has to show a source in order for an operator
        to re-enable it."""

    @abstractmethod
    async def delete(self, source_id: uuid.UUID) -> bool:
        """Remove a source. Returns whether a row was actually removed, so
        `DELETE /admin/sources/{id}` can answer 404 rather than claiming to
        have deleted something that never existed. Idempotent."""


class MediaItemRepository(ABC):
    """Persistence for "this title is available on that source".

    Same session/transaction ownership as `TitleRepository`: every method
    flushes so conflicts surface immediately, none commits.

    **Availability is retracted by exactly one method, and only after a walk
    has provably finished.** `SourceAdapter.list_items`' contract guarantees
    a walk raises rather than truncating, precisely so a caller can tell
    "the library ended" from "the adapter gave up"; that guarantee is worth
    nothing if the sweep runs either way. `mark_unseen_unavailable` is
    therefore a separate call the reconciler makes *after* the walk returns
    normally, never a side effect of `upsert_many`. See ADR-0015.
    """

    @abstractmethod
    async def upsert_many(self, rows: Sequence[MediaItemUpsert]) -> BulkWriteResult:
        """Insert or update media items, keyed on `(source_id, external_id)`.

        Flushes, never commits. Returns inserts and updates separately, so a
        re-sync is visibly a re-sync (`inserted == 0`) rather than
        indistinguishable from a first one -- which is what makes PRD 10's
        "library growth per week" panel a real measurement instead of a
        straight line.

        **A batch may contain the same `(source_id, external_id)` twice.**
        `SourceAdapter.list_items` explicitly permits duplicates within one
        walk, so an implementation must deduplicate rather than assume;
        against Postgres, not doing so raises `CardinalityViolationError`.
        The last such row in `rows` wins, matching a resumed walk's "the
        later page is the fresher read".

        **Never downgrades a matched item to unmatched.** A row already
        carrying a `title_id` (from an earlier match, or from a human
        resolving it in the review queue) keeps it when this is called with
        `title_id=None`. The reverse -- a newly-resolved `title_id` landing
        on a row that had none -- does apply. Without this rule the nightly
        walk erases every manual resolution, silently, the same night it was
        made.

        **Never hard-deletes.** PRD 02: "Soft-delete availability,
        hard-delete nothing." Rows absent from `rows` are not touched by
        this call at all.

        An item that appears in `rows` is `available = true` when this
        returns, because appearing in a walk *is* the evidence of
        availability -- which is how an item that came back comes back.

        A `title_id` or `episode_id` naming a row that does not exist raises
        `RepositoryConflict`, and leaves the session usable for the caller's
        other pending work.
        """

    @abstractmethod
    async def mark_unseen_unavailable(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> SweepResult:
        """Retract availability for everything this source did not show us.

        Sets `available = false` on every row for `source_id` whose
        `last_seen_at` is older than `seen_since` and which is currently
        available. Returns how many were retracted and how many rows the
        source has in total -- the guard's own denominator, reported because
        "3 retracted" and "3 of 4 retracted" want different responses.

        This only ever sets `false`. Restoring an item that came back is
        `upsert_many`'s doing, because appearing in a walk *is* the evidence
        of availability.

        **Raises `AvailabilitySweepRefused` and changes nothing** when the
        retraction would exceed `max_retract_fraction` of the source's
        items. `list_items` raising rather than truncating covers a walk
        that failed; this covers a walk that *succeeded* and returned far
        less than the library holds -- an unmounted drive, a library removed
        by accident, a permissions change on the source's own account.
        Neither the adapter nor Usher can distinguish that from a genuine
        mass deletion, and only one of the two is reversible, so the sweep
        declines.

        `max_retract_fraction` of `1.0` disables the guard, which is what an
        operator deliberately removing a library passes.

        Never deletes. A retracted item keeps its row, its `title_id`, and
        every watch state attached to its title.
        """

    @abstractmethod
    async def get_by_external_id(self, source_id: uuid.UUID, external_id: str) -> MediaItem | None:
        """One item as this source addresses it, or None."""

    @abstractmethod
    async def resolve_series_titles(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, uuid.UUID]:
        """Map series `external_id` -> `title_id` for those already matched.

        Exists because an episode's canonical parent is its series' `Title`,
        and a walk sorted by creation date offers no guarantee that a series
        is seen before its episodes. Batched rather than per-episode: this
        deployment holds 999,827 episodes, so a per-item lookup here is the
        difference between one query per batch and one per episode.

        Absent keys mean "not matched yet", not "no such series" -- the
        caller leaves those episodes unmatched and enqueues a re-match,
        which the next batch or the next run resolves.
        """

    @abstractmethod
    async def resolve_targets(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, MediaItemTarget]:
        """Map each `external_id` to what its row is matched to.

        The read a watch-state walk needs, and the reason it is batched is
        the reason every other read here is: a walk of `watch_state()`
        yields one record per item, and this deployment has 1,126,674 of
        them. One statement per batch, never one per state.

        Absent keys mean "not stored, or stored and not matched to
        anything" -- the same convention `resolve_series_titles` uses, and
        the same response either way (the caller counts the state
        unmatched and moves on, because a watch record with no target is
        exactly what `merge_from_source` raises `PortDataMalformed` for).

        Unlike `resolve_series_titles` this answers for *any* item, and it
        answers with both ids: an episode's row carries its series' title
        **and** its episode, and a caller that saw only the first would
        merge 40 episodes of a show into one watch state on the series.
        """

    @abstractmethod
    async def resolve_external_ids(
        self, source_id: uuid.UUID, targets: Sequence[MediaItemTarget]
    ) -> dict[MediaItemTarget, str]:
        """The inverse: how this source addresses the items behind these
        canonical targets.

        `WatchStateRepository.list_needing_history` answers in canonical
        ids, and `SourceAdapter.get_watch_state` asks in the source's own --
        so without this the history backfill has no way from one to the
        other, and would be a per-row lookup even if it did.

        A `MediaItemTarget` here is watch-state shaped: exactly one of the
        two ids is set. A title-only target matches only a row with **no**
        `episode_id`, or a series' own state would resolve to whichever of
        its episodes the planner reached first.

        A target with two copies on the same source -- a 4K and an HD file
        of one film, which is ordinary -- resolves to one of them
        deterministically: the most recently seen, then the lowest
        `external_id`. Any of them would answer, and picking arbitrarily
        would make a backfill's upstream request depend on the planner.
        Deliberately *not* also keyed on `available`: only a walk sets an
        item available and only the sweep retracts one, so the freshest
        `last_seen_at` is already the available copy in every state
        reachable through this port -- an `available DESC` ahead of it
        would be an ordering key no case could ever fail.

        Absent keys mean this source has no item for that target, which is
        the normal state of a household with two sources.
        """

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> list[MediaItem]:
        """Every copy of one title, across every source. PRD 07's
        `availability` array.

        **Unavailable copies are returned**, with `available = false`, rather
        than filtered: PRD 02 is "soft-delete availability, hard-delete
        nothing", and a client rendering "not on any source" for a film on a
        temporarily unmounted drive is showing a different fact than the one
        stored. The client decides what a retracted copy means.

        **A series' episodes are not its copies, and that is what bounds
        this read.** An episode's row carries its series' `title_id` as well
        as its own `episode_id`, so a read on `title_id` alone answers a
        series with one row per episode file -- 999,827 of the one measured
        source's 1,126,789 items are episodes, and one long-running serial
        would put thousands of entries in a response whose whole content is
        "which sources hold this". Rows with an `episode_id` are therefore
        excluded, exactly as `resolve_external_ids`' title branch excludes
        them and for the same reason. What is left is bounded by copies of
        the title *itself* -- sources times versions, single digits in a
        household -- so nothing here pages. `list_unmatched` is the method on
        this port that needed an `OFFSET`, and it is measured at 388.9 ms at
        offset 1,126,574 for exactly the reason this one must not need one.

        Ordered `available` first, then most recently seen, then by `id` --
        a total order, so a detail screen does not shuffle its badges between
        refreshes. Nothing about a `SELECT` promises an order otherwise.

        Empty is the common answer, not a missing row: the catalog holds
        1,271,138 titles and the one measured source holds 1,126,789 items,
        so the great majority of titles are on no source at all.
        """

    @abstractmethod
    async def list_unmatched(
        self, source_id: uuid.UUID | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[MediaItem]:
        """The review queue (PRD 02: "Unmatched items are never dropped").

        Ordered by `added_at` descending with `id` as a tiebreak, so paging
        is stable across calls -- an unstable order silently shows an
        operator the same item twice and hides another. `added_at` is
        nullable and sorts last, because an item a source cannot date is
        less interesting than one it dated yesterday, not more.
        """

    @abstractmethod
    async def attach_title(
        self, media_item_id: uuid.UUID, *, title_id: uuid.UUID, episode_id: uuid.UUID | None
    ) -> bool:
        """Resolve one item to a title, by hand or by a later match pass.

        Returns whether a row changed, so a caller can answer 404 rather
        than claim to have resolved something that does not exist.

        Unlike `upsert_many` this *does* write what it is given, including a
        `None` `episode_id`: it is the deliberate act of a human or of a
        re-match, not a walk's incidental "I did not look".
        """

    @abstractmethod
    async def owned_title_ids(self, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of `title_ids` this household has a copy of, on any source.

        **A retracted copy still counts.** PRD 02's availability is a soft
        delete, so `available = false` means "a source is not currently
        reporting it", and a search ranking that flipped when a source went
        down would move results for a reason unconnected to the query. The
        `owned_only` filter in `PostgresSearchIndex` carries the *identical*
        predicate: two definitions of owned is how a filtered list and a
        boosted list stop agreeing.

        **Restricted to a title's own row (`episode_id IS NULL`), and the
        reason is measured.** `IngestService` writes an episode's row with its
        series' `title_id` *and* its own `episode_id`, so an unrestricted read
        of a series is one row per episode file: 20,001 rows / 22.901 ms / 402
        buffers against 1 row / 0.251 ms / 21 buffers, on this project's own
        measurement of `list_for_title`. `resolve_external_ids`' title branch
        carries the identical clause. The bound that buys: a library that
        reported episodes but never their series row reads as not-owned for
        that series.

        One statement however many ids are asked about — the same N+1 this
        port's `list_for_title` would otherwise be used for, and worse, since
        each of those reads is a read of a whole show.
        """

    @abstractmethod
    async def count_for_source(self, source_id: uuid.UUID) -> int:
        """How many items this source has, available or not. The sweep's
        denominator, and the CLI's report."""


class WatchStateRepository(ABC):
    """Persistence for watch state, and the inbound merge from a source.

    Same session/transaction ownership as `TitleRepository`: flushes, never
    commits.

    **`merge_from_source` is `COALESCE`-shaped, and that is a correctness
    property rather than an implementation detail.** `WatchStateMerge`'s
    `play_count`/`last_played_at` are `int | None`/`datetime | None`, where
    `None` means "the read that produced this could not determine it" and
    `0`/a datetime are positive claims. A source's *listing* frequently
    cannot determine them -- verified against Emby 4.9.5.0, where a listing
    reports `PlayCount: 0` for an item played twice -- so an implementation
    that wrote `None` through as `0`, or that wrote the DTO's default,
    replaces the household's real play history with zeros on every nightly
    walk, silently. ADR-0014.
    """

    @abstractmethod
    async def merge_from_source(self, merges: Sequence[WatchStateMerge]) -> int:
        """Apply inbound watch records, returning how many rows changed.

        Upserts on `(user_id, title_id)` or `(user_id, episode_id)` with
        `origin = source`.

        - `position_seconds` and `played` are always written.
        - `runtime_seconds`, `play_count` and `last_played_at` are written
          **only when not `None`**; `None` leaves the stored value exactly as
          it was.
        - A stored row whose `updated_at` is newer than `observed_at` is left
          alone entirely. PRD 03: "latest `updated_at` wins" -- a client that
          set a resume position thirty seconds ago knows more than a walk
          that started an hour ago, and without this the nightly reconcile
          stomps it.

        **A batch may contain the same target twice**, for the same reason
        `MediaItemRepository.upsert_many` must tolerate it: a walk may yield
        an item more than once. The merge with the latest `observed_at` wins.

        A merge naming neither a `title_id` nor an `episode_id`, or naming
        both, raises `PortDataMalformed` -- `WatchState`'s own model
        validator and the `num_nonnulls(title_id, episode_id) = 1` CHECK say
        the same thing, and a caller must not receive that as a raw storage
        exception. This is not only about the error's type: an
        implementation that splits a batch into a title branch and an
        episode branch would otherwise write a both-targets merge *twice*,
        as two half-rows.
        """

    @abstractmethod
    async def list_needing_history(
        self, *, limit: int = 500
    ) -> list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]:
        """`(user_id, title_id, episode_id)` for rows that are played but
        whose play count is unknown.

        "Unknown" is spelled `played AND play_count = 0`, because
        `watch_states.play_count` is `NOT NULL DEFAULT 0` and a walk that
        could not determine the count leaves the default in place. Slightly
        lossy -- an item genuinely played once whose history a source then
        cleared matches too -- and self-healing, because the backfill's
        single-item read is idempotent and Emby never leaves a played item
        at `PlayCount: 0`. ADR-0014 records the alternative (a nullable
        column) and why it was not taken.

        Bounded by `limit` and ordered oldest-first, because this is the
        queue-filling query for a backfill that costs one upstream request
        per row and must never be handed the whole library.
        """

    @abstractmethod
    async def get_for_title(self, user_id: uuid.UUID, title_id: uuid.UUID) -> WatchState | None:
        """One user's state for one title, or None."""

    @abstractmethod
    async def get_for_episode(self, user_id: uuid.UUID, episode_id: uuid.UUID) -> WatchState | None:
        """One user's state for one episode, or None.

        Not a convenience twin of `get_for_title`: 999,827 of the one
        measured source's 1,126,674 items are episodes, so this is the
        majority read, and it is a genuinely different query -- a different
        unique constraint, and a different branch of `merge_from_source`'s
        SQL. Without it, an implementation whose `COALESCE` is right for
        titles and wrong for episodes has nothing that can tell.
        """


class TitleMatchRepository(ABC):
    """Batch lookups over `titles`, for the ingest pipeline.

    Mostly PRD 03's match stage, plus the one triage read stage 1 needs
    (`enrichment_states`) -- which belongs here rather than on
    `TitleRepository` for exactly the reason the rest of this port does.

    Separate from `TitleRepository` because the shape is different in the way
    that matters: `TitleRepository.get_by_tmdb_id` answers one question, and a
    walk asks 1,126,674 of them. At ~0.1 ms per indexed point lookup that is
    minutes of pure round trips per sync, and the name+year tier is far worse
    -- measured at 300k rows, an unindexed name+year match seq-scans in
    14.6 ms, which extrapolates to ~600 ms per item at the catalog's real
    1,271,138.

    So every method here takes a batch and returns a mapping. `MatchService`
    turns one page of source items into three sets, issues a handful of
    statements, and joins the answers in memory.

    Reads only. Same session-wide precondition as `TitleRepository`: the
    session must carry no unflushed, invalid state when these are called.
    """

    @abstractmethod
    async def match_by_provider_ids(
        self, refs: Sequence[ProviderRef]
    ) -> dict[ProviderRef, uuid.UUID]:
        """Resolve provider references to title ids, in a bounded number of
        round trips regardless of batch size.

        Keys absent from the result mean "no title carries this", which is a
        different answer from "not asked" -- so an implementation must never
        silently drop a ref it found nothing for, and a caller can iterate its
        own probes rather than the result.

        `ProviderRef.kind` is honoured where it is set and required where the
        provider is namespaced. TMDb keys movies and series in overlapping
        integer spaces (26,968 shared ids, measured), so a TMDb ref *without*
        its kind names nothing and resolves to nothing rather than to whichever
        of the two a scan reaches first; IMDb's namespace is global, so an IMDb
        ref carries no kind and one that carries anyway is still answered.
        ADR-0011.

        A ref whose `value` is not a valid integer for a provider whose column
        is an integer is skipped, not raised on: a source is free to report
        `ProviderIds.Tmdb: "unknown"`, and that is a matching failure, not a
        pipeline failure. Raising would abort a whole batch of 5,000 items over
        one bad string.

        A provider this implementation does not know is skipped for the same
        reason -- a source is free to report `ProviderIds.Zap2It`, and the
        answer to "which title carries it" is honestly "none that I can tell".

        A batch may contain the same ref twice. It is answered once.
        """

    @abstractmethod
    async def match_by_name_year(
        self, probes: Sequence[NameYearProbe]
    ) -> dict[NameYearProbe, uuid.UUID]:
        """PRD 03 stage 3: normalised name plus a year within +/-1, scoped by
        kind.

        Case-insensitive, via the same `lower(name)` the
        `ix_titles_name_lower_year` expression index is built on -- an
        implementation that lowercases in Python and compares against the raw
        column cannot use that index and seq-scans 1,271,138 rows per probe.

        **An ambiguous probe resolves to nothing.** Several titles sharing a
        name, a kind, and a year within one is common (remakes, and IMDb's own
        duplicate entries), and PRD 03 stage 5 is explicit that no *confident*
        match means the review queue. Picking the first row a scan reaches is a
        coin flip that attaches watch state to the wrong film.

        A probe with `year=None` resolves to nothing rather than matching on
        name alone -- a bare name is not an identity claim at a catalog of
        1.27M titles.

        A batch may contain the same probe twice. It is answered once, and a
        duplicate is emphatically not ambiguity: an implementation that counted
        candidate rows without deduplicating its own input first would report
        every repeated probe as ambiguous and send the whole page to the review
        queue.
        """

    @abstractmethod
    async def enrichment_states(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, EnrichmentState]:
        """`title_id` -> its tier, for a whole batch.

        Ingest enqueues an `enrich` job for every title a walk touched that is
        not already enriched, and skips the ones that are. Answering that with
        `TitleRepository.get` is one round trip per distinct title per batch --
        the same per-item defect this port exists to remove, arriving in stage
        1 instead of stage 2. It reads one column, so it stays a state map
        rather than a `Title` map: the caller compares through
        `ENRICHMENT_RANK` (ADR-0008) and needs nothing else.

        Absent keys mean "no such title", never "not asked". A batch may name
        the same id twice; it is answered once.
        """


class EpisodeRepository(ABC):
    """Persistence for the season/episode hierarchy under a series `Title`.

    Seasons and episodes are one aggregate here rather than two ports: an
    episode cannot exist without its season, both arrive from the same
    provider payload, and every write is a batch over one series.

    Same session/transaction ownership as `TitleRepository`: every method
    flushes so conflicts surface immediately, none commits.
    """

    @abstractmethod
    async def upsert_seasons(self, seasons: Sequence[Season]) -> BulkWriteResult:
        """Insert or update, keyed on `(title_id, season_number)`.

        Never overwrites a non-null field with a null one, for the same reason
        `upsert_episodes` does not: ingest can create a season from a source's
        own number alone and enrichment fills the rest in.

        A `title_id` no title carries raises `RepositoryConflict` rather than a
        raw storage error, and leaves the session usable for the caller's other
        pending work.

        A batch may contain the same `(title_id, season_number)` twice -- a
        walk yields episodes, and a whole season's worth of them name the same
        season -- so an implementation deduplicates rather than assuming. The
        last such row wins.
        """

    @abstractmethod
    async def upsert_episodes(self, episodes: Sequence[Episode]) -> BulkWriteResult:
        """Insert or update, keyed on `(title_id, season_number,
        episode_number)`.

        Never overwrites a non-null field with a null one: ingest creates an
        episode from a source's own numbers alone (no name, no air date) and
        enrichment fills the rest in, and the next nightly walk must not blank
        what enrichment wrote. Same `COALESCE` rule
        `MediaItemRepository.upsert_many` applies to `title_id`, for the same
        reason.

        A `title_id` or `season_id` naming a row that does not exist raises
        `RepositoryConflict`.

        Tolerates a duplicate within one batch, as `upsert_seasons` does.
        """

    @abstractmethod
    async def resolve_seasons(
        self, keys: Sequence[tuple[uuid.UUID, int]]
    ) -> dict[tuple[uuid.UUID, int], uuid.UUID]:
        """`(title_id, season_number)` -> season id, in one round trip.

        Exists because `upsert_seasons` reports counts rather than ids, and it
        cannot report the caller's: ingest mints a fresh UUIDv7 per sighting,
        and a season the catalog already holds keeps the id it was inserted
        with. So the id an episode's `season_id` must carry is knowable only by
        reading it back.

        **Keyed across titles, not scoped to one.** A batch of 1,000 episodes
        off a walk sorted by creation date routinely spans hundreds of series
        -- an episode arrives the week it airs, not with its siblings -- so a
        per-title signature is one round trip per series in the batch, which at
        999,827 episodes is the same design defect batching exists to avoid.

        Absent keys mean "no such season", never "not asked".
        """

    @abstractmethod
    async def resolve_episodes(
        self, keys: Sequence[tuple[uuid.UUID, int, int]]
    ) -> dict[tuple[uuid.UUID, int, int], uuid.UUID]:
        """`(title_id, season_number, episode_number)` -> episode id, in one
        round trip. 999,827 episodes means this cannot be a lookup per item,
        and -- for the reason `resolve_seasons` states -- not a lookup per
        series either.

        `title_id` is part of the key rather than a separate argument because
        every series has an S01E01: a resolve that dropped it hangs one show's
        episodes off another's, and 32,409 series makes that a certainty.

        Absent keys mean "no such episode under this series", never "not
        asked", so a caller iterates its own probes.
        """

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> tuple[list[Season], list[Episode]]:
        """Everything under one series, seasons then episodes, each ordered by
        its own numbering. Used by enrichment to decide what changed, and by
        the CLI's report."""


class SyncRunRepository(ABC):
    """Per-source sync history. Flushes, never commits.

    One row per attempt, not one per source -- contrast `ImportRunRepository`,
    which is a checkpoint updated in place. PRD 10's dashboard 3 plots run
    outcomes over time, and ADR-0015's sweep guard rests on being able to say
    *which* run last finished cleanly.
    """

    @abstractmethod
    async def add(self, run: SyncRun) -> None:
        """Insert. A duplicate id raises `RepositoryConflict`."""

    @abstractmethod
    async def save(self, run: SyncRun) -> None:
        """Update an existing run. An unknown id raises `RepositoryNotFound`.

        `started_at` is not mutable through this call in any meaningful sense:
        it is the sweep's own `seen_since`, so a run that could rewrite it
        after the fact could retract items it had already seen.
        """

    @abstractmethod
    async def get(self, run_id: uuid.UUID) -> SyncRun | None:
        """Fetch by id, or None."""

    @abstractmethod
    async def latest_completed_cursor(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> AwareDatetime | None:
        """`started_at` of the newest run of this kind that **completed**, or
        `None` if none has.

        Deliberately not "the newest run": a delta walk resuming from a run
        that failed halfway would skip everything that run never reached, and
        would do it silently. Reading only completed runs means a failure costs
        a re-walk of a window rather than a hole in the catalog.

        Scoped by kind because the two lanes use different upstream filters
        (`MinDateLastSaved` vs `MinDateLastSavedForUser`, measured as genuinely
        different: 28,934 vs 29,005 items over the same 30-day window), so one
        cursor cannot serve both.
        """

    @abstractmethod
    async def list_for_source(self, source_id: uuid.UUID, *, limit: int = 20) -> list[SyncRun]:
        """Newest first, with `id` as a tiebreak so paging is stable. PRD 10's
        dashboard 3 ("sync run outcomes and duration") and the CLI's
        `sync-status`."""


@dataclass(frozen=True, slots=True)
class CachedPayload:
    """One `raw_payloads` row, as a walk sees it.

    Carries `kind` and `reference` rather than a `title_id`, because the table
    has neither a `title_id` column nor a foreign key to `titles` (ADR-0016:
    the cache is keyed `(provider, kind, reference)` and nothing else). The
    caller resolves back to a title through that pair, and **the pair is the
    whole key** -- ADR-0011: `tmdb_id` is unique per kind, and 26,968 measured
    TMDb ids are live in both the movie and the series id space.

    `id` is here so the caller can pass it back as `after`. It is deliberately
    not a `title_id` in disguise.

    Declared immediately above the port that returns it rather than beside
    `NeighborSeed`/`StoredEmbedding`, because this module has no
    `from __future__ import annotations` -- an abstract method's return
    annotation is evaluated when the class body runs, so the name has to
    already exist. That is also where `TitleEmbeddingUpsert` sits relative to
    `TitleEmbeddingRepository`.
    """

    id: uuid.UUID
    kind: str
    reference: str
    payload: dict[str, Any]
    fetched_at: AwareDatetime


class RawPayloadStore(ABC):
    """The provider response cache (PRD 02's `raw_payloads`).

    **Providers only, never source items.** PRD 03's ingest stage previously
    said to store every source item's payload here; at 1,126,674 items and
    ~8 kB apiece that is ~9 GB against a database PRD 08 budgets at 8-12 GB
    total, to avoid a refetch that costs one request. ADR-0016.

    `fetched_at` is also the TMDb <=6-month cache-term clock (PRD 04's
    licensing constraint), which is why PRD 02's separate
    `provider_cache_meta` table is not created.

    Flushes, never commits.
    """

    @abstractmethod
    async def get(
        self, provider: str, kind: str, reference: str
    ) -> tuple[dict[str, Any], AwareDatetime] | None:
        """The cached payload and when it was fetched, or None.

        The timestamp is returned rather than kept internal because the
        caller's question is never just "is it cached" -- it is "is it cached
        recently enough to use", and TMDb's caching term makes that a
        compliance question as well as a freshness one.
        """

    @abstractmethod
    async def put(self, provider: str, kind: str, reference: str, payload: dict[str, Any]) -> None:
        """Store or replace, stamping `fetched_at` to now.

        Refreshing an entry **must** move `fetched_at`. A stale timestamp on
        fresh data is precisely the wrong answer to the one compliance question
        this column exists to answer, and it is silent: the payload is correct
        and only the clock lies.

        `provider`, `kind` and `reference` are plain strings rather than a
        domain model, so nothing validates them before they reach the store.
        A key the backing store rejects -- an empty `provider` or `reference`
        -- raises `RepositoryConflict`, not a storage-specific exception, and
        leaves the session usable for the caller's other pending work.
        """

    @abstractmethod
    async def oldest_fetched_at(self, provider: str) -> AwareDatetime | None:
        """The compliance query: the oldest cache entry for a provider, which
        is what PRD 10's dashboard-5 panel plots against TMDb's 6-month
        ceiling. `None` when the provider has no entries at all."""

    @abstractmethod
    async def count(self, provider: str) -> int:
        """How many payloads this provider has cached.

        The denominator of `usher derive`'s coverage report, and it is printed
        as a **count beside another count** rather than as a percentage: PRD
        08 requires every command to work against an empty database, and a
        derived-coverage percentage is `0/0` on exactly that deployment.
        """

    @abstractmethod
    async def iterate(
        self, provider: str, *, limit: int = 500, after: uuid.UUID | None = None
    ) -> list[CachedPayload]:
        """One page of this provider's cached payloads, oldest id first.

        **A keyset cursor, not an offset**, for the reason `list_stale`'s is
        one: `OFFSET` pagination is measured in this repository at 43.7 ms at
        offset 0 and 388.9 ms at offset 1,126,574 -- linear per page, quadratic
        to drain -- and a derivation's entire job is to walk a population to
        exhaustion. Pass the last `id` of a page as `after` to get the next
        one; an empty list means drained.

        Ordered by `id`, which is the primary key and therefore a **total**
        order. `fetched_at` is not: the INSERT arm's `server_default` is
        `now()` = `transaction_timestamp()`, so every row a bootstrap
        transaction writes shares one instant, and a page boundary inside that
        group drops the rest of it with nothing to say so.

        **Scoped by `provider`, and deliberately not by `kind`.** The
        derivation needs both TMDb id spaces in one walk, and
        `CachedPayload.kind` is what keeps them apart -- a signature that took
        `kind` would invite two walks and a caller that forgot the second. It
        is not scoped by freshness either: a payload outside `EnrichService`'s
        window is still a payload, and refusing to derive from it would mean a
        re-derivation that silently covered less of the catalog than the last
        one.
        """


@dataclass(frozen=True, slots=True)
class TitleEmbeddingUpsert:
    """One title's vector and the two facts that make its staleness a query.

    `embedding` is `None` for a **refused** title — one whose composed
    document is degenerate. That is a written outcome, not a skipped one: it
    stops the title matching the stale predicate, starts it matching a
    separate countable one, and gets it re-claimed exactly once when
    enrichment changes the text. Measured: every whitespace-only input
    embeds to the identical vector, cosine 1.0000 exactly, so a degenerate
    document is an unbounded cluster at the top of every similar-titles
    result rather than a bad result.

    `model_name` carries the runtime as well as the checkpoint
    (`fastembed:BAAI/bge-small-en-v1.5`), because two runtimes of the same
    weights differ by 6x the halfvec quantisation error and are not
    interchangeable without a re-embed.
    """

    title_id: uuid.UUID
    embedding: tuple[float, ...] | None
    model_name: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class StoredEmbedding:
    """What is currently stored for one title, as `get` answers it.

    Deliberately not `TitleEmbeddingUpsert` even though it carries the same
    facts: the `title_id` is absent because the caller passed it, and the two
    types travelling in opposite directions is what keeps a read from being
    handed straight back to a write without the caller deciding to.

    Its one consumer is the index stage's idempotence check, and that check
    is a comparison of **both** `model_name` and `source_fingerprint` — a
    skip on existence alone passes every redelivery case and then never
    updates a vector again, which is a stale index that does not raise, it
    answers.
    """

    embedding: tuple[float, ...] | None
    model_name: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class NeighborSeed:
    """One embedded title, carrying the tag sets the blend needs — so a page
    read answers the seed half in one statement rather than ids here plus a
    second `list_by_ids` pulling 31 columns per row for two of them."""

    title_id: uuid.UUID
    genres: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NeighborCandidate:
    """One candidate neighbour and the raw signals it offers.

    **`cosine`, never a distance.** pgvector's `<=>` is a distance and the
    blend wants agreement, so `1 - (a <=> b)` happens once, in the adapter,
    rather than in a scorer that would then have to know which operator
    produced its input. A signal list whose members disagree about direction is
    how a weight silently becomes a penalty.

    It may be **negative**, and clamping is deliberately the *service's* job
    rather than this port's: `title_neighbors.score` is `CHECK (score >= 0 AND
    score <= 1)`, so the clamp has to hold for every implementation of this
    port rather than for the one that remembered.
    """

    title_id: uuid.UUID
    cosine: float
    genres: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoredNeighbor:
    """One row of `title_neighbors`, as the service computed it.

    `neighbor_title_id` rather than the row's own `neighbor_id`: on this side
    of the port the two ids are both title ids and calling one of them
    `neighbor_id` reads like a `title_neighbors` primary key travelling in a
    DTO. The repository maps it.
    """

    title_id: uuid.UUID
    neighbor_title_id: uuid.UUID
    score: float
    rank: int


class TitleEmbeddingRepository(ABC):
    """Persistence for the semantic half, and the home of the one predicate
    three consumers share.

    Unlike the search document — a stored generated column PostgreSQL keeps
    correct inside every write of its inputs — an embedding needs a model,
    so it is a job, and jobs can fail, park, or never be enqueued at all.
    This port is where that asymmetry is paid for: rather than trusting the
    queue, every row records *what* was embedded and *by what*, and
    "is this stale?" becomes a query the backfill, the gauge and a test all
    ask the same way.

    Same session ownership as every other repository here: methods flush and
    return counts, and never commit. `model_name` is a parameter on every
    method rather than a constructor argument read from settings — `db/`
    may not import `config`, and a repository that knew the deployment's
    model could not be asked "how many rows would a model swap invalidate?"
    """

    @abstractmethod
    async def upsert_many(self, rows: Sequence[TitleEmbeddingUpsert]) -> BulkWriteResult:
        """Write a batch, insert-or-update, keyed on `title_id`.

        Idempotent by construction: PRD 08's redelivery rule, and the job
        queue *will* redeliver. A batch carrying the same `title_id` twice
        keeps the later row — last-wins on the batch's own order. A
        `title_id` naming no title raises `RepositoryConflict`, translated
        from the backing store's own error, and leaves the session usable for
        the caller's other pending work.
        """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> StoredEmbedding | None:
        """One title's stored row, or `None` if it has never been indexed.

        The index stage reads this *before* asking a model for anything, and
        that read is what makes redelivery free rather than merely safe:
        `JobWorker.startup()` requeues everything left `running`, so a
        process killed between a handler returning and `complete` committing
        produces a second delivery of work already done. At ~83 texts/s a
        requeued backfill that re-embedded would re-run the whole enriched
        tier.

        `None` is "no row", which is the first disjunct of the stale
        predicate — a title that has never been indexed and one whose text
        has moved are the same question to a caller, and both are answered by
        embedding it.
        """

    @abstractmethod
    async def list_stale(
        self, model_name: str, *, limit: int = 100, after: uuid.UUID | None = None
    ) -> list[Title]:
        """One page of titles needing an embedding, oldest id first.

        **A keyset cursor, not an offset.** `MediaItemRepository.list_unmatched`'s
        `OFFSET` pagination is measured at 43.7 ms at offset 0 and 388.9 ms
        at offset 1,126,574 — linear per page, quadratic to drain — which
        is fine for an operator reading the first few pages and wrong for a
        backfill, whose entire job is to walk a population to exhaustion.
        Pass the last id of a page as `after` to get the next one; an empty
        list means drained.

        The population is `enrichment_state <> 'skeleton'` (boundary call 4),
        for which `ix_titles_enrichment_state` is already the partial index
        that exists. A skeleton title's document is a generated column, so it
        is fully indexed with no job at all.
        """

    @abstractmethod
    async def count_stale(self, model_name: str) -> int:
        """How many titles the predicate currently claims.

        A plain `int`, synchronously consumable by a caller that caches it —
        **never wired directly to an OTel observable callback.** The SDK
        invokes those from the metric reader's background thread and every
        call here is a coroutine on asyncpg, so a querying callback would
        have to bounce onto the event loop and block the exporter thread on
        it. `telemetry.register_queue_gauges` already records the shape.
        """

    @abstractmethod
    async def count_refused(self, model_name: str) -> int:
        """How many titles are current *and* have no vector — the composer
        refused their document as degenerate.

        **This must not overlap `count_stale`.** Spelled as a bare
        `embedding IS NULL` it would also count rows refused under an older
        model, which are stale; the two counters would then sum above the
        population and "the backfill has drained" would stop being an
        observable condition.
        """

    @abstractmethod
    async def list_embedded(
        self, *, after: uuid.UUID | None = None, limit: int = 500
    ) -> list[NeighborSeed]:
        """Titles with a **non-NULL** embedding, in `id` order, after `after`.

        A keyset cursor for the reason `list_stale`'s is one: `OFFSET`
        pagination is measured in this repository at 43.7 ms at offset 0 and
        388.9 ms at offset 1,126,574 — linear per page, quadratic to drain.

        **NULL embeddings are excluded here rather than by the caller.** A
        refused title is written as a row with a NULL embedding so it stops
        matching the stale predicate; it has no vector to search from and is
        not a seed. Excluding it in the caller would mean every future caller
        has to remember.
        """

    @abstractmethod
    async def nearest_for(
        self, seed_ids: Sequence[uuid.UUID], *, limit: int
    ) -> dict[uuid.UUID, list[NeighborCandidate]]:
        """The `limit` nearest candidates for each seed, nearest first.

        **Excludes the seed itself and every NULL-embedding row**, and both are
        the implementation's job rather than the caller's. Self-exclusion,
        because cosine with itself is 1.0 and every neighbour list would
        otherwise open with the title the reader is already looking at.
        NULL-exclusion, because `embedding <=> :seed` is NULL, NULLs sort last
        on an ascending order, and so they arrive only when the population is
        smaller than `limit` — at which point they are either a type error or,
        under a careless `coalesce`, a distance of 0 pinning every refused
        title to the top of every list.

        **A page of seeds rather than one**, so a rebuild costs one statement
        per page instead of one per title: the same round-trip-per-item shape
        `index_many` was introduced to delete from `SearchIndex`, at 10,000
        instead of 1.3M and still worth not reintroducing.

        **Exact, not approximate.** PRD 05: brute-force exact cosine at this
        scale, 10k x 384. Recall loss in a live query is per-query; recall loss
        in a precomputed artefact is permanent, and this one is read until the
        next rebuild. The `halfvec` quantisation figures do **not** license an
        approximate index here — that would be laundering one measurement into
        a claim about another, and this milestone has not measured HNSW recall.

        Ties on distance break on `title_id`, so *which* candidates enter the
        pool is decided rather than left to the executor.

        A seed with no embedding, or none at all, is simply absent from the
        answer — never a key mapped to an empty list, which a caller would have
        to distinguish from "computed and found nothing".
        """

    @abstractmethod
    async def count_without_embedding(self) -> int:
        """Rows carrying a `NULL` embedding — the written refusals.

        The second half of the predicate pair, and it exists so the exclusion
        above is *observable*: a rebuild that silently skipped a growing swathe
        of the catalog reads exactly like one with nothing to skip. `usher
        similar --rebuild` prints it.

        Deliberately **not** `count_refused`'s number: that one is scoped to a
        `model_name` and answers "how many are current and vectorless", which
        is a question about the backfill draining. This one answers "how many
        rows can never be a seed", which is a question about the artefact's
        coverage, and it stays true across a model swap.
        """


class TitleNeighborRepository(ABC):
    """`title_neighbors` — the precomputed similarity artefact (PRD 05).

    **The one derived artefact in M6 whose freshness is not a per-row
    predicate**, and the port says so rather than implying otherwise. A row is
    stale when either title's embedding moved — which is a predicate, and
    which subsumes the metadata case for free because `compose_document`
    includes genres and keywords. But a row is *also* stale when some third
    title's embedding moved into its neighbourhood, and that is not decidable
    without recomputing the row. So the artefact carries an age, not a
    fingerprint, and it is rebuilt rather than repaired.

    Same session ownership as every other repository here: methods flush and
    return counts, and never commit.
    """

    @abstractmethod
    async def replace(
        self, seed_ids: Sequence[uuid.UUID], neighbors: Sequence[ScoredNeighbor]
    ) -> int:
        """Replace every stored row for `seed_ids` with `neighbors`.

        **`seed_ids` is passed separately from the rows and that is not
        redundancy.** A seed whose neighbours all disappeared — the other
        enriched titles were deleted, or every candidate became degenerate —
        contributes no rows at all, so an implementation deriving the delete's
        scope from `neighbors` deletes nothing for it and leaves its stale
        neighbours in place through every future rebuild. It is the one row
        shape a rebuild cannot repair.

        Returns the number of rows written, which is what makes an operator's
        rebuild report a number rather than a reassurance.
        """

    @abstractmethod
    async def list_for(self, title_id: uuid.UUID, *, limit: int) -> list[ScoredNeighbor]:
        """One seed's stored neighbours, best first, ties broken by id.

        Read back by the batch's own stored `rank` rather than by re-sorting on
        `score`: reproducing the order from the score works only up to float
        ties, and a tie broken differently on two reads shows a client two
        different "most similar" titles for one catalog.
        """

    @abstractmethod
    async def computed_at(self) -> AwareDatetime | None:
        """The **oldest** stored row's timestamp, or `None` if none exists.

        Oldest rather than newest: the newest would report a whole-table
        rebuild as fresh the moment the first page committed, which is this
        milestone's own failure mode ("looks healthy while describing
        yesterday") wearing an accessor.

        `None` means *never computed*, which is a different fact from "this
        title has no neighbours" and is what stops `usher similar` sending an
        operator to look at the wrong thing.
        """


@dataclass(frozen=True, slots=True)
class CreditedPerson:
    """One credit and the person it names, in one row rather than two reads.

    A bare `Credit` carries a `person_id` and nothing renderable, so a port
    returning them hands every caller the same second query. That is the N+1
    this milestone's front matter names, relocated into the port rather than
    removed -- and a port that *offers* an N+1 is worse than one a caller
    invents, because it looks sanctioned.
    """

    person_id: uuid.UUID
    name: str
    kind: CreditKind
    character: str | None
    job: str | None
    department: str | None
    billing_order: int | None


@dataclass(frozen=True, slots=True)
class PersonCredit:
    """One of a person's credits, with the title it is on.

    The mirror of `CreditedPerson`: the person is the thing already known, so
    what travels is the title id. Hydration into a `RowCard` is
    `TitleRepository`'s, which is what keeps this port from growing a second
    opinion about what a title is.
    """

    title_id: uuid.UUID
    kind: CreditKind
    character: str | None
    job: str | None
    billing_order: int | None


@dataclass(frozen=True, slots=True)
class RecurringPerson:
    """A person who recurs across the titles one user has actually played.

    `watched_title_count` is a count of **distinct titles**, never of credits.
    A person credited twice on one film -- two jobs, or two characters, both
    of which TMDb genuinely emits -- would otherwise read as two titles, and a
    one-film person would out-rank a four-film one. The row this feeds says
    "you keep watching this person"; counting credits makes it say something
    else with total confidence, which is exactly the failure this milestone
    opens by describing.

    `kind` and `job` travel because the row's own text needs them: "More from
    <name>" is a worse row than "Directed by <name>", and a provider holding
    only a name cannot tell the two apart.
    """

    person_id: uuid.UUID
    name: str
    kind: CreditKind
    job: str | None
    watched_title_count: int


@dataclass(frozen=True, slots=True)
class OwnedCollection:
    """A franchise and the household's coverage of it.

    **Lists, not counts, and the two counts are `len()`.** PRD 06's franchise
    signal is "you own 2 of 4", which is two numbers *and* the cards to
    render. Storing `owned_count` beside `owned_title_ids` would permit the
    two to disagree, which is a state no consumer could interpret -- the same
    argument `title_neighbors`' primary key makes about `(title_id, rank)`.

    `title_ids` is every member in release order, `owned_title_ids` the subset
    with an available media item. The difference is the completeness signal,
    and it is what makes a franchise row say something a genre row cannot.
    """

    collection_id: uuid.UUID
    name: str
    title_ids: tuple[uuid.UUID, ...]
    owned_title_ids: frozenset[uuid.UUID]


class PersonRepository(ABC):
    """Persistence for canonical people (PRD 02's `Person`).

    PRD 02: *"People are canonical entities, so 'more from this director' is a
    join rather than a string match."* Identity is Usher's own UUIDv7;
    `tmdb_id` is a nullable indexed attribute and never identity (ADR-0003),
    which is what makes **two directors who share a name two rows**. An
    implementation that dedupes on `name` is the first wrong implementation
    this port's contract suite exists to kill.

    Same session ownership as every other repository here: methods flush so
    conflicts surface immediately, none commits.

    **No `get(person_id)`.** Nothing in M7 reads one person by id --
    `GET /people/{id}` is M9's (PRD 07's endpoint table, boundary call 6) --
    and the only thing a row needs is a name, which `RecurringPerson`
    carries. `SearchIndex`' settled argument applies unchanged: *"A port
    method whose only test is its own test is a liability, and the failure
    mode of a rare path is that it has rotted by the time somebody needs
    it."*
    """

    @abstractmethod
    async def upsert_many(self, people: Sequence[Person]) -> BulkWriteResult:
        """Insert or update, keyed on `tmdb_id`.

        **Keyed on `tmdb_id`, not on `Person.id`.** The derivation mints a
        fresh UUIDv7 per sighting exactly as ingest does for seasons, so an
        upsert keyed on the id inserts a duplicate row per pass and the
        catalog grows a copy of every actor every time `usher derive` runs.

        **Never overwrites a non-null field with a null one.** This is not
        the defensive version of the `COALESCE` rule -- it is required, and
        the payload says why: a `created_by[]` entry carries no
        `known_for_department` while a `credits.cast[]` entry does, so the
        same person arrives with it and without it *inside one derivation
        pass*. An unconditional assignment blanks an actor's department the
        moment they also created a series, silently, on a field
        `PeopleProvider` reads.

        A batch may contain the same `tmdb_id` many times -- one derivation
        pass spans many titles and a working actor is on several of them -- so
        an implementation deduplicates rather than assuming. The last such row
        wins.

        A person with a `None` `tmdb_id` is inserted, never merged: the
        uniqueness index is partial and NULL never collides with NULL. Two
        such people are two rows, which is the only answer available when
        there is no identity to compare.
        """

    @abstractmethod
    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        """`tmdb_id` -> person id, in one round trip.

        Exists for `EpisodeRepository.resolve_seasons`' reason, restated
        because it is the same defect: `upsert_many` reports counts rather
        than ids, and it cannot report the caller's -- the derivation mints a
        fresh UUIDv7 per sighting and a person the catalog already holds keeps
        the id it was inserted with. So the id a `Credit.person_id` must carry
        is knowable only by reading it back.

        **A batch rather than one, and the number is the argument.** A single
        enriched movie names tens of people; the enriched tier is 2k-10k
        titles. A lookup per person is the round-trip-per-item shape batching
        exists to remove.

        Absent keys mean "no such person", never "not asked", so a caller
        iterates its own probes rather than reading a short answer as a full
        one.
        """

    @abstractmethod
    async def count(self) -> int:
        """How many people the catalog holds. `usher derive`'s report, and the
        one number that tells an operator a derivation ran at all."""

    @abstractmethod
    async def list_recurring_for_user(
        self, user_id: uuid.UUID, *, min_titles: int = 2, limit: int = 10
    ) -> list[RecurringPerson]:
        """People who recur across the titles this user has played, most
        first.

        **This is the method the N+1 hazard is about.** The obvious shape is
        "list the user's watch states, then `list_for_title` each one" -- one
        statement per watched title, against a history the one measured
        deployment sizes at up to 1,126,789 states. This answers it in one
        statement instead, and the port exists in this shape so a provider
        cannot express the other one.

        **`watched_title_count` counts distinct titles, never credits.** A
        person credited twice on one film reads as two titles otherwise, and a
        one-film person out-ranks a four-film one -- a row that is populated,
        ordered, plausible and wrong, which is the failure mode this milestone
        exists to refuse.

        **Episode watch state counts toward its series**, and an
        implementation reading only `watch_states.title_id` misses it. 999,827
        of the one measured source's 1,126,674 items are episodes, so a
        People row built from `title_id` alone is a row about films on a
        library that is mostly television. Twelve watched episodes of one
        series are **one** title in this count, which is the other half of why
        the count is distinct.

        `min_titles` defaults to 2 because "recurring" is PRD 06's word and
        one appearance is not a recurrence. `played` is the predicate, not
        "has a watch state": a row with `played = false, position_seconds = 0`
        is a state a sync created, not something the user watched.

        Ordered by count descending, ties broken by `person_id` so two reads
        of one catalog agree -- the `list_for`/`nearest_for` rule.
        """


class CreditRepository(ABC):
    """Persistence for `credits` -- the join that makes "more from this
    director" a lookup.

    **The write is a replace, not an upsert, and that is the port's central
    decision.** A title's credit set changes upstream: a name is corrected, a
    role is removed, a mis-attributed actor is deleted. An upsert can express
    every one of those except the last, and the last is the one that leaves a
    permanently wrong row -- so the unit of work is "this title's credits are
    now exactly these", which only a scoped replace can say.

    Same session ownership as every other repository here: flushes, never
    commits.
    """

    @abstractmethod
    async def replace_for_titles(
        self,
        title_ids: Sequence[uuid.UUID],
        credits: Sequence[Credit],
        *,
        credit_names: Mapping[uuid.UUID, Sequence[str]],
    ) -> int:
        """Replace every stored credit for `title_ids` with `credits`, and
        write `titles.credit_names` for the same scope in the same call.

        **`credit_names` is not a second write and may not become one.** It is
        weight class B's input -- `credits` projected to names and truncated
        to a ranking constant -- and a stored generated column cannot reach
        another table, which is the whole reason it exists as a column at all
        (boundary call 5, measured in migration `fe1d40c8b7a3`). The array and
        the table are two spellings of one fact: split them across two calls
        or two transactions and they diverge, and the symptom is a full-text
        hit on a name `credits` no longer holds. Keyword-only and **without a
        default**, so a caller cannot forget it.

        **Scoped by `title_ids`, exactly as the delete is.** A title in scope
        but absent from the mapping has its array emptied rather than left
        alone -- same argument, same sentence: a title whose credits all
        disappeared upstream contributes no rows, so a scope derived from the
        rows leaves its stale names in place forever.

        Order within each sequence is the ranking and is preserved. It is
        top-billed first, which is what makes the class-B lexemes the ones a
        viewer would search for.

        **`title_ids` is passed separately from the rows and that is not
        redundancy** -- `TitleNeighborRepository.replace`'s argument, arriving
        at a second table for the same reason. A title whose credits all
        disappeared upstream contributes no rows at all, so an implementation
        deriving the delete's scope from `credits` deletes nothing for it and
        leaves its stale credits in place through every future derivation. It
        is the one row shape a re-derivation cannot repair.

        Returns the number of credit rows written, which is what makes
        `usher derive`'s report a number rather than a reassurance.

        A `title_id` or `person_id` naming a row that does not exist raises
        `RepositoryConflict` rather than a raw storage error, and leaves the
        session usable for the caller's other pending work -- the derivation
        commits a batch of credits together with its job checkpoint.

        Idempotent by construction: PRD 08's redelivery rule, and the job
        queue *will* redeliver. Running it twice with the same arguments
        produces the same rows and the same count.

        A batch carrying the same `tmdb_credit_id` twice keeps one of them;
        the partial unique index is what makes a *scoping* bug raise instead
        of doubling a title's cast, and tolerating an in-batch duplicate is
        what stops a payload that lists a credit twice from failing the whole
        derivation.
        """

    @abstractmethod
    async def list_for_title(
        self, title_id: uuid.UUID, *, kind: CreditKind | None = None, limit: int = 20
    ) -> list[CreditedPerson]:
        """One title's credits, top-billed first, with the person joined in.

        **Ordered by `billing_order`, nulls last, ties broken by
        `person_id`.** "Top billed" is what PRD 06's People row means and what
        a client's cast list renders; an implementation that drops
        `billing_order` returns provider-JSON order, which is *usually* right
        and is therefore invisible until it is not. That is the front matter's
        second named wrong implementation for this suite.

        **`kind` filters and may not be ignored.** Asking for cast and
        receiving crew is the third named wrong implementation, and it has the
        property that makes this milestone dangerous: the answer is populated,
        correctly shaped, and about the wrong people. `None` means both, in
        one ordering.

        Called by `usher derive`'s report, and it is the surface every
        `replace_for_titles` case asserts through -- a write port with no read
        can only assert on counts, which cannot tell a correct row from a
        wrong one. M9's `GET /titles/{id}` cast block is its first
        client-facing caller.
        """

    @abstractmethod
    async def count_titles_with_credits(self) -> int:
        """How many **distinct titles** hold at least one credit.

        Titles, never credit rows: a report counting rows says "412,000
        credits" where an operator asked "did my library get derived", and one
        heavily-credited film moves it by fifty. This is the numerator beside
        `RawPayloadStore.count`'s denominator, and the two are printed
        unreduced.
        """

    @abstractmethod
    async def list_for_person(self, person_id: uuid.UUID, *, limit: int = 50) -> list[PersonCredit]:
        """Everything one person is credited on -- `PeopleProvider`'s cards.

        Scoped to the person, and an implementation that forgets the filter
        returns the whole table in physical order, which satisfies every
        membership assertion and no positional one. The contract case seeds a
        second person's credits for exactly that reason.

        One call per person and **not** an N+1: `PeopleProvider` emits 0-2
        rows (PRD 06's own table), so this is at most two statements. The
        unbounded question -- *which* people -- is
        `PersonRepository.list_recurring_for_user`, in one statement, which is
        where the fan-out actually lived.

        Ordered by `billing_order` nulls last then `title_id`, so a person's
        headline roles lead and two reads agree.
        """


class CollectionRepository(ABC):
    """Persistence for TMDb's movie franchise grouping, and the writer
    `titles.collection_id` has never had.

    **Movies only, and the port says so rather than a provider discovering
    it.** `belongs_to_collection` is a field of `/movie/{id}` with no
    `/tv/{id}` counterpart -- verified against the recorded payloads. So on a
    television-only household PRD 06's ">= 2 owned titles in a collection" is
    unsatisfiable **by construction** rather than by absence of data, which is
    the fact an operator debugging a missing row needs, and it is why
    `attach_titles` filters on kind rather than trusting its caller.

    Flushes, never commits.
    """

    @abstractmethod
    async def upsert_many(self, collections: Sequence[Collection]) -> BulkWriteResult:
        """Insert or update, keyed on `tmdb_id`.

        Keyed on `tmdb_id` rather than `Collection.id` for
        `PersonRepository.upsert_many`'s reason: the derivation mints a fresh
        UUIDv7 per sighting, so an id-keyed upsert grows a duplicate franchise
        per pass. A batch names the same collection once per member film, so
        deduplication is the common case rather than the odd one.
        """

    @abstractmethod
    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        """`tmdb_id` -> collection id, in one round trip. Absent keys mean "no
        such collection", never "not asked". Same argument as
        `PersonRepository.resolve_tmdb_ids`, and it is what
        `attach_titles`' pairs are built from."""

    @abstractmethod
    async def attach_titles(self, links: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> int:
        """Set `titles.collection_id` for each `(title_id, collection_id)`
        pair. Returns the number of rows actually **changed**.

        **Changed, not touched.** A re-derivation over an unchanged catalog
        must write zero rows: an implementation that assigns unconditionally
        produces a dead row version per movie per pass, on a table with a GIN
        index and a stored generated column. This repository has already
        recorded that shape once, in a `DO UPDATE` with no `WHERE`, and the
        returned count is what makes it observable rather than merely avoided.

        **Filters `kind = 'movie'` itself, and does not trust its caller.** A
        series carrying a movie's `belongs_to_collection` is the fourth wrong
        implementation this port's contract must kill, and the filter lives
        here because it is a property of the data source rather than of any
        one call site. `titles` deliberately carries no
        `CHECK (collection_id IS NULL OR kind = 'movie')` -- see
        `db/models/collection.py` for why -- so this is what enforces it.

        A `collection_id` naming no collection raises `RepositoryConflict`. A
        `title_id` naming no title is simply not updated: an `UPDATE` that
        matches nothing is not an error, and treating it as one would make a
        concurrent title merge fail a derivation.

        **Does not clear links outside `links`.** The scope is the pairs
        given, not "the world". An implementation that NULLs every unnamed
        title unlinks the whole catalog the first time the derivation runs
        over one page.
        """

    @abstractmethod
    async def count(self) -> int:
        """How many franchises the catalog holds -- `usher derive`'s report.

        Deliberately **not** scoped to franchises with owned members, which is
        `list_owned`'s question: this one answers "did the derivation write
        collections", and narrowing it would make an empty answer ambiguous
        between "nothing derived" and "nothing owned".
        """

    @abstractmethod
    async def list_owned(self, *, min_owned: int = 2, limit: int = 5) -> list[OwnedCollection]:
        """Franchises the household owns at least `min_owned` of, most-owned
        first.

        **No `user_id`, deliberately, and PRD 06's wording is what settles
        it.** ">= 2 owned titles in a collection" is a statement about
        *ownership*, and ownership is a property of the household's sources --
        `MediaItem` has no user and never has. A `user_id` parameter here
        would be a fiction every implementation would have to ignore. The
        row's personalisation comes from `HomeService`'s scoring, not from
        this read.

        `min_owned` defaults to 2 because a franchise you own one of is not a
        franchise row -- it is a single film with a subtitle, and it is the
        distractor this suite's case seeds.

        **Owned means an available, title-level media item.** `episode_id IS
        NULL` is part of the predicate rather than implied: `media_items`
        holds 999,827 episode rows on the one measured deployment, and a join
        on `title_id` alone reads the wrong population. Collections hold only
        movies so no episode can match today, which is exactly why the clause
        has to be written down -- its absence is otherwise indistinguishable
        from having forgotten it.

        One statement, not one per collection. Ordered by owned count
        descending, ties broken by `collection_id`.
        """
