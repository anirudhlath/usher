"""Titles -- the canonical aggregate everything else hangs from.

Implemented by `usher.db.repositories.title.PostgresTitleRepository`.

`credit_names_for` is on this port and not on `CreditRepository`: it reads
the denormalised `titles.credit_names` column, and `services/index.py`
calls it on the title repository it already holds.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title

__all__ = [
    "TitleRepository",
]


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
    async def list_owned_by_tag(
        self,
        *,
        genre: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
    ) -> list[Title]:
        """Owned titles carrying a genre and/or a keyword, best first.

        **The retrieval half of `GenreAffinityProvider` and the whole of
        `SeasonalProvider`, and it did not exist.** `ff_row_read_indexes`
        reasons about it from the other side -- *"its retrieval half is
        bounded to owned titles (single-digit thousands) before array
        containment is consulted"* -- which is the shape this signature makes
        mandatory rather than hoped for: the ownership semi-join is inside the
        statement, not a filter the caller applies to the catalog's top N. The
        difference is not style. Taking the twenty most popular horror films
        in a 1.27M-row catalog and *then* asking which are owned returns
        nothing at all on a normal household.

        **Owned here means "has an available copy", with no `episode_id IS
        NULL` bound, and that is a deliberate divergence from
        `MediaItemRepository.owned_title_ids`.** That method answers about one
        row per title and carries the bound so that asking it about an episode
        cannot report a missing episode file as owned. This one asks "can the
        household play something of this title", and for a series the answer
        is yes when any episode file exists -- a series owned only through its
        episodes is the normal case on a library that is 89% episodes, and
        excluding it would make every television title unreachable by every
        row built on this read. A semi-join, so a 20,000-episode series costs
        one probe rather than 20,000 rows.

        **Both predicates given means both must match**; neither given returns
        `[]` and reads nothing. An unpredicated call is a request for the
        library ordered by popularity, which is the popular-titles fallback
        spelled as a query -- so the port declines to express it.

        Ordered `popularity DESC NULLS LAST, vote_count DESC NULLS LAST, id`.
        The second key is not decoration: `titles.popularity` was measured
        NULL on all 1,271,138 rows of a bootstrap-only catalog and is
        `NOT NULL DEFAULT 0` in `tmdb_ids`, so on a partially-linked catalog a
        crosswalk-linked skeleton at 0.0 outranks an unlinked title with half
        a million votes. That hazard is recorded rather than solved here --
        it is the same one M6's suggest path took `vote_count` for -- and the
        `id` tail is what makes two reads of one unchanged catalog agree.

        Nothing about *watched* is expressed here. `played_title_ids` answers
        that, over the ids this returns, because the two questions have
        different bounds and folding them together would make the limit mean
        something different on every household.

        ⚠️ **`list_unwatched_candidates` below deliberately does the opposite,
        and this cross-reference exists so neither sentence is read as the
        rule.** The argument in the paragraph above is about *this* `limit`,
        which is a candidate budget feeding a 20-card row; there `limit` **is**
        the answer's size, so a watched-filter applied after it shrinks the
        result most for the household with the most history, and a caller
        cannot repair that without an unbounded over-read. Same two questions,
        opposite correct answers, because the two limits mean different things.
        """

    @abstractmethod
    async def list_unwatched_candidates(
        self,
        user_id: uuid.UUID,
        *,
        genres: Sequence[str] = (),
        limit: int,
    ) -> list[Title]:
        """The curation pool: titles this household has not seen, best first.

        **`limit` has no default, and that is a decision rather than an
        omission.** It shipped as `= 200` in three signatures -- here, the
        Postgres implementation and the fake -- and nothing made those three
        agree: measured, changing the fake's to `5` left the whole unit suite
        green and changing `PostgresTitleRepository`'s to `5` left the whole
        integration suite green, because no contract case called without it
        while seeding more than five candidates. Two implementation defaults
        free to disagree with each other and with the port is the exact
        failure a contract suite exists to prevent, and asserting three
        literals are equal would be a check that runs *after* the drift. So
        there is one definition and no copies, which is `DERIVED_COLUMNS`' and
        `_PROVIDER_ID_CONSTRAINTS`' shape.

        The second reason is layering: 200 is a *curation policy* number --
        `USHER_CURATION_POOL_SIZE`, argued from a prompt's token budget -- and
        a persistence port has no business carrying a default for it.
        `CandidatePoolService.for_user` is the only caller in `src/` and
        always passes `limit=self._size`.

        **The whole of `CandidatePoolService`'s retrieval, and the substrate
        of [ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md).**
        The prompt addresses candidates by a small integer index, so this
        answer's *size*, *order* and *stability* are what the index means. A
        pool that comes back short, or in a different order on a second read
        of an unchanged catalog, is a prompt whose handles denote something
        else than they did an hour ago.

        **Membership is "unwatched", and nothing else.** Ownership and
        popularity are ranking keys rather than filters, because PRD 06 says
        the pool *"spans the whole catalog, not just the library, so
        suggestions can include things to seek out"* -- and because a
        popularity floor is a constant nobody measured that would empty the
        pool on a catalog with no vote counts.

        **Ordered `owned DESC, carries an affinity genre DESC, vote_count
        DESC NULLS LAST, id`**, which is M8 boundary call 5's own enumeration
        of the signals that need no model -- *"unwatched, owned or popular,
        genre affinity, `titles.vote_count`"* -- read as the order it is
        written in:

        - **Owned first**, because a shelf the household can play tonight is
          worth more than one it has to go and find, and an unowned card
          renders with `RowCard.owned = False` rather than being unreachable.
          The two are strata rather than a blend: there is no measured
          exchange rate between "in the library" and "half a million votes",
          and inventing one would be a number this project could not defend.
        - **Then genre affinity**, the only household-shaped signal in the
          base order. `genres` is `TasteService.genre_affinity`'s answer
          projected to names, and **empty is the common case rather than a
          degenerate one**: it is what a household with no watch history
          produces, and what every household produces before its first sync.
          So it is a sort key and never a predicate -- as a predicate it would
          hand an empty pool to exactly those households, which is
          `GenreAffinityProvider`'s corrected failure arriving one layer down.
        - **Then `vote_count`, and deliberately not `popularity`.**
          `list_owned_by_tag` leads with `popularity` and this read does not,
          which is a divergence rather than an oversight: `titles.popularity`
          was measured NULL on all **1,271,138** rows of a `--phase imdb`
          catalog (M6, 2026-08-03) and is `NOT NULL DEFAULT 0` in `tmdb_ids`,
          so on a partially-linked catalog a crosswalk-linked skeleton at
          `0.0` outranks an unlinked title with half a million votes. That
          hazard is bounded there -- the read is scoped to owned titles,
          single-digit thousands -- and unbounded here, where the candidate
          set is the whole catalog and the skeletons are most of it.

          ⚠️ **That total and the one below it are four lines apart and
          differ, which is deliberate and is why both carry their date.**
          1,271,138 is M6's `--phase imdb` catalog; 1,271,570 is M7 Task 36's
          `--phase all` one, measured 2026-08-05 after `link_crosswalk` ran
          and 432 more titles had landed. Two measurements of two catalogs, a
          milestone apart — not one number restated wrongly, which is exactly
          the failure the next bullet exists to record.
        - **Then `id`, and it decides *membership* rather than merely order.**
          This is the canonical statement of the tiebreak's argument; the
          contract case and PRD 06 point here rather than restating it.

          The two keys above `vote_count` are **booleans**, so they partition
          rather than order, and `vote_count` itself is NULL on **732,220 of a
          measured 1,271,570-title catalog** -- the bootstrap writes it on
          539,350 through `BulkCatalogRepository.apply_ratings` (measured
          2026-08-05, M7 Task 36; the number is recorded in
          `adapters/search/postgres.py`). So the ordinary shape of this answer
          is four strata whose tails are one large tie, and `limit` falls
          inside one of them: with no total order, two reads of one unchanged
          household return different **sets**, not merely different orders,
          and ADR-0028's index->UUID map is then a map of a pool that no
          longer exists. This repository has been bitten by an `ORDER BY` with
          no `id` tail twice (`list_owned_by_tag` records one, `UPDATE …
          RETURNING` the other).

          ⚠️ **Not the argument `list_owned_by_tag` makes for its own `id`
          tail, and an earlier draft of this docstring made that one by
          swapping the column into it.** It claimed `vote_count` is NULL on
          *every* row of a bootstrap-only catalog, which the same measurement
          refutes: under `NULLS LAST` the 539,350 voted rows sort **above**
          every unvoted one, so on exactly that catalog `vote_count` is what
          orders the head of the pool. The `popularity` sentence above is the
          one that survives being read that way, because `popularity` really
          is NULL until `link_crosswalk` runs.

        **"Unwatched" is `played`, rolled up through `episodes.title_id`, and
        it is the same predicate `played_title_ids` spells.** Both halves are
        needed and each rules out a different populated answer: `played`
        rather than "has a watch state", because a walk writes a row per item
        it observed and that predicate is the owned library -- so the pool
        would become everything the household does *not* own; and the
        roll-up, because a watched episode's row carries `episode_id` with a
        NULL `title_id`, so a title-keyed exclusion offers back every series
        the household is midway through, on a library that is 89% episodes.

        **It is inside the statement rather than subtracted afterwards, which
        is this port's one real departure from `list_owned_by_tag`'s recorded
        position.** That method says *"nothing about watched is expressed
        here … folding them together would make the limit mean something
        different on every household"*, and for a 60-candidate budget feeding
        a 20-card row that is right. Here it is exactly backwards: `limit`
        **is** the pool size, ADR-0028's measurements are scoped to 200, and a
        filter applied after a `LIMIT` shrinks the pool most for the household
        with the most history -- the household curation is worth the most to.
        A caller cannot repair that without an unbounded over-read.

        `limit` rows at most, fewer only when the catalog holds fewer. An
        empty answer means the household has seen everything in a catalog this
        small, which is a real state on a fresh install and not an error.

        **The cost is a scan and a top-N sort of the whole catalog, and that
        is accepted rather than indexed.** No index can serve this order --
        two of its four keys are computed, and `ffc` already dropped
        `ix_titles_popularity` after measuring that a plain descending btree
        does not serve `DESC NULLS LAST` anyway -- and adding one for a
        statement that runs once per household per night would be
        `ix_titles_popularity`'s mistake repeated. M8's boundary call 2 is
        what makes that affordable: generation is a background job and is
        never on a request path.
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
