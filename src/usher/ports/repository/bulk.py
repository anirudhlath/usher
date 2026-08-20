"""The bulk-load path: one port for the dataset importers to write through.

Implemented by `usher.db.repositories.bulk.PostgresBulkCatalogRepository`.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from usher.ports.bulk import (
    GenomeTag,
    GenomeVector,
    IdCrosswalkPair,
    ImdbAka,
    ImdbCreditNames,
    ImdbRating,
    ImdbTitle,
    TmdbId,
)
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "AliasWriteResult",
    "BulkCatalogRepository",
    "CreditNamesFillResult",
    "CrosswalkLinkResult",
    "GenomeCoverage",
    "GenomeWriteResult",
]


@dataclass(frozen=True, slots=True)
class AliasWriteResult:
    """What one scoped alias replacement actually stored, and what it dropped.

    Four fields, because **three of the four rows this write is handed do not
    become rows** and a filter nobody can count is indistinguishable from an
    upstream that has nothing to give. Against the measured catalog the whole
    file reduces 7,536,366 retained akas rows to **1,663,364** stored aliases,
    and the two filters below are where the other 5.87M go.

    `written` counts rows stored — after the canonical filter and after the
    dedupe, so it is the number that goes against T3's bar (B) and not the
    batch's length.

    **`canonical` counts rows dropped for restating the title's own name**,
    and it is the dominant term: **5,693,570 of 7,536,366 (75.5%)**
    `lower()`-equal `titles.name` or `titles.original_name`. A row like that
    carries nothing `ix_titles_name_lower_prefix` does not already answer, so
    storing it is the one-row-per-title duplication M6's boundary call 3
    refused this table for. An operator watching this number sit at ~0 is
    watching the comparison miss, which otherwise looks exactly like a dump
    full of genuine aliases.

    **`duplicate` counts rows dropped as a repeat of `(title_id,
    lower(name))`** already kept for that title — 1,842,796 survivors
    deduplicate to 1,663,364, i.e. **9.7%**. It is not a defensive count: one
    name is legitimately listed for several regions, and the loser's `region`
    and `language` are discarded, so the number says how much locale detail
    this shape costs.

    `unmatched` counts **scoped IMDb ids resolving to no catalog title**,
    mirroring `CreditNamesFillResult.unmatched` and
    `CrosswalkLinkResult.unmatched`. It counts the scope rather than the rows,
    because the scope is what the caller is asserting it has read the upstream
    for; rows belonging to such an id are neither written nor filtered, they
    are attributed to the id that was not there.
    """

    written: int
    unmatched: int
    canonical: int
    duplicate: int


@dataclass(frozen=True, slots=True)
class CreditNamesFillResult:
    """What one batch of IMDb credit names actually changed.

    Three fields, and the third is the one that is not obvious.

    `filled` counts titles whose `credit_names` genuinely changed, not titles
    seen — the same rule `apply_ratings` reports on, and for the same reason:
    `titles` carries two GIN indexes and a stored generated column, so a
    replay that rewrote every row would be expensive and would also make "did
    this phase do anything" unanswerable.

    `unmatched` counts staged rows whose `imdb_id` is in no title, mirroring
    `GenomeWriteResult.unmatched` and `CrosswalkLinkResult.unmatched`.
    Expected to be large rather than zero: `title.principals` covers
    **11,491,032 titles** and the retained catalog holds ~1.27M of them, so
    roughly nine rows in ten match nothing. What is not acceptable is a join
    that matched almost nothing looking identical to one that matched
    everything.

    **`deferred` counts titles this source is not allowed to touch**, and it
    is the count that makes the two-writer rule auditable. `credit_names` has
    a second writer — `CreditRepository.replace_for_titles`, which writes it
    from the TMDb-derived `credits` in the same statement as the table itself
    — and this port writes no `credits` row at all. So TMDb owns every title
    it has reached and IMDb owns the rest; a title in the first set is
    counted here rather than written. On a catalog whose whole population is
    `skeleton` this is 0, and it grows with every enrichment: an operator
    watching it grow is watching the second source recede, which is correct
    and worth being able to see.
    """

    filled: int
    unmatched: int
    deferred: int


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


@dataclass(frozen=True, slots=True)
class GenomeWriteResult:
    """What one batch of genome vectors actually changed.

    `inserted`/`updated` split for the reason every write on this port
    splits them: rowcount alone reports their sum, so a re-import would be
    indistinguishable from a first run. That matters more here than
    anywhere, because "did this phase actually do anything" is the question
    the whole `movielens` phase exists to answer.

    **`unmatched` is the third field and it is the deliverable.** It counts
    staged rows whose `imdb_id` is in no title — mirroring
    `CrosswalkLinkResult(linked, unmatched, conflicted)`, which is this
    project's precedent for reporting a join's misses as a count rather than
    as silence. `links.csv` holds 86,537 movies and the catalog holds
    whatever IMDb's dump retained, so the difference is real and expected;
    what is not acceptable is a join that matched almost nothing looking
    identical to one that matched everything.
    """

    inserted: int
    updated: int
    unmatched: int


@dataclass(frozen=True, slots=True)
class GenomeCoverage:
    """Genome coverage with its denominators, because "~7%" never had one.

    PRD 05 promised *"~7% coverage"* and PRD 04 repeated it as *"~7% of the
    priority tier"*, and neither named a denominator. Measured against the
    dataset, 16,376 genome movies is **1.82%** of a full catalog's 899,828
    movies, **1.29%** of all 1,271,138 titles, and **8.7%** of PRD 04's own
    *"~189k titles with >=100 IMDb votes"* priority tier — which is the
    denominator that makes the published figure roughly right.

    **None of those is the number that matters**, which is `enriched` and
    `enriched_with_vector`: an owned household library of 2k-10k titles,
    skewed hard toward exactly the popular, English, pre-2019 movies the
    genome covers. Those three percentages are ceilings the *dataset* can
    reach; these fields are what the join actually did against *this*
    operator's catalog.

    `revisions` is `(genome_revision, count)` pairs. More than one entry is a
    correctness problem rather than a curiosity — `GenomeRepository.get_pair`
    already refuses to blend across it — and the fix is a re-import. One
    entry is the normal case and is not worth printing.
    """

    with_vector: int
    titles: int
    movies: int
    enriched: int
    enriched_with_vector: int
    revisions: tuple[tuple[str, int], ...] = ()


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
        """Set `tmdb_vote_average`/`tmdb_vote_count` on titles that already
        exist, returning how many rows changed.

        ⚠️ **Those are the TMDb-named columns and this is IMDb's data, which
        is the bug ADR-0040 names rather than a description of intent.** The
        rename made the dual write legible; redirecting this method onto
        `imdb_average_rating`/`imdb_num_votes` is a behaviour change and is
        Task 2 of `docs/plans/2026-08-19-rating-provenance-split.md`. Stated
        here because an ABC that described the write it *should* make would
        leave the implementation looking like the defect.

        Never creates a title: `title.ratings.tsv.gz` covers `titleType`s
        this milestone drops, and a rating with no title is not a catalog
        entry. Rows whose values already match are left alone, for the same
        trigger reason as `upsert_titles`.
        """

    @abstractmethod
    async def fill_credit_names(self, rows: Sequence[ImdbCreditNames]) -> CreditNamesFillResult:
        """Fill `titles.credit_names` from IMDb, for titles TMDb has not
        reached. Never creates a title, and never writes a person or a credit.

        **This method exists because an entity design was refused, and the
        refusal is the whole of its shape.** M9's T3 loaded IMDb's
        `name.basics` and `title.principals` against a real 1,271,138-title
        catalog and measured the `people` + `credits` design at
        **2,701,697,024 B (2.702 GB) against a 2.0 GB ceiling** — 2.395 GB
        even stripped to five columns and three indexes. Two further findings
        made it unrepairable rather than merely large: `credits`' only unique
        key is `tmdb_credit_id`, which is NULL on every IMDb row, so an IMDb
        load **cannot be deduplicated at all** (`(title_id, person_id, kind)`
        cannot be UNIQUE — 1,341,798 collisions); and TMDb's credits carry no
        `nconst`, so people cannot be merged across the two sources on an id.
        What survives is the name *text*, which is the only part of a person
        weight class B of `search_document` ever indexed — so the names are
        written straight into the column and no entity is invented to hold
        them.

        **Precedence: a title TMDb has reached is TMDb's, and every other
        title is IMDb's.** `CreditRepository.replace_for_titles` writes
        `credit_names` in the same statement and the same transaction as
        `credits`, because *"the array and the table are two spellings of one
        fact ... split them across two calls or two transactions and they
        diverge, and the symptom is a full-text hit on a name `credits` no
        longer holds"*. This call cannot join that transaction, so it must
        leave that path's titles alone; a title it declines is reported as
        `deferred` rather than silently skipped.

        An implementation states the predicate it uses for "TMDb has reached
        this title" in its own docstring. It must be at least as strong as
        "the column is empty", because a title TMDb enriched and derived *no
        cast for* legitimately has an empty array that is still TMDb's
        answer.

        **The write is a set, not a merge**, so a caller must supply a
        title's names whole. A row carrying an empty `names` would therefore
        blank the column; `ImdbCreditNames` promises it never is.

        Order is the ranking, top-billed first, and is preserved — it is what
        makes the class-B lexemes the ones a viewer would search for.

        A batch naming the same `imdb_id` twice keeps one deterministically
        rather than failing, exactly as `upsert_titles` does and for the same
        Postgres reason. Idempotent, and a replay reports `filled = 0`: an
        unchanged row is not rewritten, so the `set_updated_at` trigger, two
        GIN indexes and a stored generated column are not paid for nothing.
        """

    @abstractmethod
    async def replace_aliases(
        self, rows: Sequence[ImdbAka], *, imdb_ids: Sequence[str]
    ) -> AliasWriteResult:
        """Replace the `alias` half of `title_search_names` for the titles
        `imdb_ids` names, from IMDb `title.akas`. Never creates a title, and
        never touches a row of any other `kind`.

        **This is the alias source M6 refused the table for the lack of.**
        Boundary call 3 declined `title_search_names` because with no aliases
        and no people it would hold one row per title duplicating four columns
        of `titles`; M7 restated that rather than renewing it, landing people
        and not aliases; and PRD 03 named the blocker outright — TMDb's
        `alternative_titles` is in neither `append_to_response` list, so
        aliases are not in `raw_payloads` at all. `title.akas` is an alias
        source that needs **no API call and no change to the crawl's request
        shape**.

        **The scope is `imdb_ids` and it is a separate argument, not something
        derived from `rows`.** A title whose akas IMDb has withdrawn
        contributes no rows, so a scope taken from the rows cannot name it and
        its stale aliases stand forever with nothing able to report them.
        Identical argument, and identical shape, to
        `CreditRepository.replace_for_titles`' `title_ids`.

        **Every row's `imdb_id` must be in `imdb_ids`, and a row outside it is
        refused with `ValueError` before anything is written.** Such a row
        would be inserted and never deletable — the next pass over that title
        deletes by a scope it is not in — which is the one row shape a
        re-import cannot repair. `ValueError` rather than `PortDataMalformed`
        or `RepositoryConflict` for `replace_genome_tags`' reason: it is a
        caller-assembly mistake, not an upstream payload and not a backing
        store refusing a row.

        **The scope must hold each title's aliases whole**, exactly as
        `fill_credit_names`' write is a set rather than a merge. Two calls
        naming one title each replace the other's rows, so a caller batching a
        line-oriented dump has to close a title's run before it closes a batch.

        **An alias equal to the title's own `name` or `original_name` is not
        stored**, compared under `lower()` — the function
        `ix_titles_name_lower_prefix` is built over, so two names differing
        only in case are one entry to every reader of this table. Measured over
        a real 1,271,138-title catalog, **5,693,570 of 7,536,366 retained akas
        rows (75.5%) are exactly this**, and keeping them would reproduce the
        duplication boundary call 3 refused rather than reverse it on purpose.
        The parser's `isOriginalTitle` filter is a cheap prefix of this rule
        and never a substitute: **70.6% of the rows that survive it still
        restate the title's own name**, and only a comparison against the
        stored `Title` can see that.

        **`region` and `language` are written rather than dropped**, which is
        what `m09a` added those two columns for: without them a French and a
        Brazilian alias of one film are indistinguishable rows. Nothing is
        *filtered* on either axis, nor on `types` or `attributes`, and that is
        a decision rather than an omission — bar (B) passed 4.8x under on rows
        and 3.2x under on bytes, so a recall-costing filter buys headroom
        nobody needs. The only axis this write filters on is the one above,
        which costs no recall at all: a dropped row's name is already reachable
        through `titles`.

        **Two rows of one title whose names are equal under `lower()` become
        one row, and the survivor is the lowest `ordering`.** The dump lists
        one name for several regions routinely — 9.7% of what survives the
        canonical filter — and the loser's `region` *and* `language` are
        discarded with it, so the winner has to be deterministic or both
        columns wobble between two runs over the identical file. `ordering` is
        the only per-title sequence `title.akas` supplies and is why `ImdbAka`
        carries it.

        Idempotent, and a replay is **not** invisible: it reports the same
        `written` again, because the delete-and-insert rewrites the same rows.
        That is the honest answer for a table with no unique constraint to
        upsert against — `m09a` ships none deliberately, and what would reverse
        that is a writer that upserts.
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
        not `linked`. Copies `tmdb_ids.popularity` across into
        `titles.tmdb_popularity` at the same time -- which makes this
        statement that column's **second** writer beside TMDb enrichment, and
        is why a populated `tmdb_popularity` says nothing about whether a row
        was enriched (ADR-0040).

        **That last clause used to continue "…which is what makes
        `ix_titles_popularity` usable and gives M4's enrichment queue a real
        ordering", and both halves were false.** The enrichment queue is
        `jobs`, claimed through `ix_jobs_claim` (`priority DESC, created_at`);
        no statement anywhere orders it by `titles.tmdb_popularity`, so the named
        consumer never existed. And the index could not have served one
        anyway — it was declared `(popularity DESC)`, i.e. NULLS FIRST, while
        every consumer in `src/` asks `DESC NULLS LAST`, which is a different
        pathkey. Measured against 1,271,570 real titles: the favourable
        spelling takes an `Index Scan` at cost 0.42..20.97 and the shipped one
        a `Parallel Seq Scan` + `Sort` at 86,142. Migration `ffc` drops it and
        records what would bring one back.

        **What the write itself is for is still real, and is now stated
        without the index:** it is what gives 22.93% of a `--phase all`
        catalog a popularity at all, which is the signal
        `PostgresSuggestIndex` orders on and `SearchService._popularity_term`
        reads. Measured 2026-08-05: 291,584 of 1,271,570 rows, of which
        exactly **3** are `0.0` — the daily export carries real values, not
        the `NOT NULL DEFAULT 0` filler the column's declaration permits.

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
    async def upsert_genome_vectors(
        self, rows: Sequence[GenomeVector], *, revision: str
    ) -> GenomeWriteResult:
        """Store genome vectors against the titles their `imdb_id` resolves
        to, returning what changed and how many resolved to nothing.

        **The `imdb_id -> titles.id` join lives inside this statement, and
        that is the whole point of the method.** The dataset cannot do it: it
        never touches a database, which is what lets it be unit-tested with
        no Docker. A service doing it would be an ad-hoc `SELECT` in
        `services/`, which contract three forbids. So the join belongs on the
        one port whose docstring already reserves the staged, set-based path.

        `revision` is the run's own resolved dataset revision, bound as a
        parameter and stored on every row it writes. It is not derived from
        `now()` and not read back out of the file: it is what makes
        `genome_scores.genome_revision` mean "the release this row came
        from", which is what `GenomeRepository.get_pair` refuses to blend
        across.

        **A staged batch may contain two rows resolving to one title, and
        that is not defensive.** Two MovieLens `movieId`s carrying the same
        `imdbId` both resolve to one `titles.id`, and a second hit on one
        conflict target is `CardinalityViolationError` — a runtime abort of
        the whole batch, not a skipped row. An implementation must pick a
        single deterministic winner rather than leaving it to whichever row
        a scan reached first.

        Idempotent in the sense that matters for resume safety: replaying a
        batch is an upsert, never a duplicate. Unlike `upsert_titles`, a
        replay is *not* invisible — it reports `updated`, which is the honest
        answer and the one an operator re-running the phase is asking for.
        """

    @abstractmethod
    async def replace_genome_tags(self, tags: Sequence[GenomeTag], *, revision: str) -> int:
        """Replace the whole genome tag vocabulary with `tags` at `revision`,
        returning how many rows it wrote.

        **A replace, not an upsert, and that is the difference between this
        and `upsert_genome_vectors` two methods up.** A vector table is
        legitimately half-migrated — a killed re-import against a new upload
        leaves rows of two releases, which `genome_revision` exists to make
        countable and `get_pair` refuses to blend across. A *vocabulary* has
        no such state: it is one artefact of 1,128 rows, read whole, and an
        upsert over a release with fewer tags would leave the tail of the
        previous one behind, still labelled with the previous revision,
        looking exactly like a complete vocabulary that happens to be mixed.
        So the old vocabulary goes and the new one lands, in one transaction,
        and the table holds exactly one release by construction.

        **Refuses, with `ValueError` and before writing anything, a `tags`
        that is not a whole vocabulary.** Three preconditions, and the
        precedent for the exception type is `CuratedRowRepository.
        replace_for_user`, which refuses a batch disagreeing with its scope
        the same way: this is a caller-assembly mistake, not an upstream
        payload and not a backing store refusing a row, so it is neither
        `PortDataMalformed` nor `RepositoryConflict`.

        - `tags` is empty. A vocabulary of no tags is not a vocabulary, and
          storing one would make "the table is empty" mean two things — never
          loaded, and loaded as nothing. Same argument as
          `ck_curated_rows_cards_not_empty` one table over.
        - `tag_id`s are not exactly `1…len(tags)`. The vector is built **by
          index**, so a gap does not lose one name, it moves every later one:
          lane 3 would be labelled with tag 4's word, permanently, on the one
          table whose entire purpose is to say what a lane means. It is also
          **what bounds `tag_id` above** — see `db/models/taste.py` for why
          that bound lives here rather than as a field constraint.
        - any `tag` is empty. A lane named by the empty string reads as
          labelled and says nothing.

        `revision` is the run's own resolved dataset revision — the same value
        `upsert_genome_vectors` stamps onto every vector, resolved once by the
        caller so the two cannot disagree across an upstream re-upload. Empty
        is refused for the same reason: it matches no `genome_scores` row, so
        it would leave the table stored and permanently unreadable.
        """

    @abstractmethod
    async def genome_coverage(self) -> GenomeCoverage:
        """Genome coverage against every denominator that has one.

        Two set-based reads -- the counts, and the `genome_revision`
        histogram -- run at the end of the `movielens` phase and printed. See
        `GenomeCoverage` for why the enriched-tier fraction is the one that
        matters and the other three are ceilings.
        """

    @abstractmethod
    async def count_titles(self) -> int:
        """How many titles the catalog holds. Used to decide whether
        `bulk_load_window` may suspend indexes, and reported by the CLI."""
