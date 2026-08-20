"""In-memory BulkCatalogRepository, for unit-testing BootstrapService.

Mirrors the Postgres implementation's *observable* behaviour, not its
mechanism: the same dedupe-within-a-batch rule (and its specific,
deterministic winner -- not just "one row survives"), the same
skip-if-unchanged rule, the same namespace-aware conflict rules, the same
NULL-only-fills and global-uniqueness guards on `link_crosswalk`. Where the
real one gets those from `DISTINCT ON`, `IS DISTINCT FROM`, `NOT EXISTS`, and
composite unique indexes, this one does them in Python — and the shared
contract suite is what proves the two agree. A prior version of this file
matched the real implementation's *shape* (dedupe, skip-unchanged, kind
scoping) without matching several of its *specific rules* -- last-write-wins
instead of first/highest/smallest, and no NULL guard or cross-title
uniqueness check on `link_crosswalk` at all -- and 24/24 contract tests
still passed, because nothing exercised the difference. See the contract
module's docstring for what a Postgres-vs-fake mutation check found.

**Where this fake is more forgiving than Postgres, enumerated rather than
discovered later.** Every entry is a thing the contract suite therefore
cannot pin from the unit arm alone:

- **No foreign keys, and no `titles` row is required to exist for anything
  but a dictionary lookup.** Nothing here can produce the failure
  `EnrichService._store_hierarchy` hit on the *second* enrichment rather
  than the first -- a row naming an entity the catalog does not hold. The
  bulk methods route that through their `unmatched` counters instead, so
  the shape is modelled and the *constraint* is not.
- **`enrichment_state` is a `bool`, not the three-rung ladder.**
  `fill_credit_names`' precedence predicate is `enrichment_state =
  'skeleton'` in Postgres and `not stored.enriched` here, which agree only
  because nothing in this fake can be `stub`. A defect that deferred on the
  wrong rung is invisible from this arm.
- **`credit_names` is a Python tuple with no `text[]` semantics.** Postgres
  distinguishes `'{}'` from NULL and this does not; the column is NOT NULL
  with a `'{}'` default precisely because `usher_array_text` is STRICT, and
  a fake storing `None` would look identical to one storing `()`.
- **No transaction, so "nothing was written before the refusal" is a
  property this arm can demonstrate and Postgres cannot** -- the mirror of
  the usual direction, and the reason a divergence list needs entries for
  where the fake is *stricter* too.
- **No `set_updated_at` trigger, no GIN index and no stored generated
  column**, so the whole cost argument behind every `IS DISTINCT FROM`
  guard is unobservable here: a fake that rewrote every row on every replay
  would fail only the cases that assert the *count*.
- **Python's `str.lower()` is not Postgres's `lower()`, and `replace_aliases`
  compares names with it.** Python applies Unicode's *contextual* final-sigma
  rule and the database does not, so `"ΟΔΟΣ".lower() == "Οδος".lower()` is
  `True` here and `lower('ΟΔΟΣ') = lower('Οδος')` is **false** in Postgres --
  this fake would drop that alias as a restatement of the title's own name and
  the real one stores it. Measured over the whole pinned
  `title.akas.tsv.gz`: **32,223 of 46,202,631 retained rows (0.070%)** are in
  the two families where the three foldings disagree (German `ß`, Greek final
  sigma). Recorded rather than fixed, because reimplementing a collation in
  Python is a second implementation and not a stand-in, and because Postgres
  is *authoritative* by construction here -- the rule is "does this alias
  reach anything `ix_titles_name_lower_prefix` does not", and that index is a
  btree over the database's own `lower(name)`.
  `tests/integration/test_bulk_repository.py::
  test_the_canonical_comparison_is_the_databases_own_lower_and_not_pythons`
  is the only case in the suite that can see it, and it is deliberately not in
  the shared contract.
"""

import contextlib
import math
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace

from usher.domain.enums import SearchNameKind, TitleKind
from usher.domain.ids import new_id
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
from usher.ports.repository import (
    AliasWriteResult,
    BulkCatalogRepository,
    BulkWriteResult,
    CreditNamesFillResult,
    CrosswalkLinkResult,
    GenomeCoverage,
    GenomeWriteResult,
)

# Sorts after every real id, mirroring SQL's NULLS LAST (Python's default,
# None first, is the opposite of what the real ORDER BY does).
_NULLS_LAST = math.inf


def _crosswalk_sort_key(pair: IdCrosswalkPair) -> tuple[float, float, float]:
    """Mirrors the real implementation's tie-break for two crosswalk rows
    sharing one `imdb_id` in a single batch: `ORDER BY tmdb_movie_id NULLS
    LAST, tmdb_series_id NULLS LAST, tvdb_series_id NULLS LAST` -- smallest
    non-null id wins, column by column."""
    return (
        _NULLS_LAST if pair.tmdb_movie_id is None else float(pair.tmdb_movie_id),
        _NULLS_LAST if pair.tmdb_series_id is None else float(pair.tmdb_series_id),
        _NULLS_LAST if pair.tvdb_series_id is None else float(pair.tvdb_series_id),
    )


class _StoredTitle:
    __slots__ = (
        "credit_names",
        "enriched",
        "facts",
        "id",
        "imdb_id",
        "kind",
        "popularity",
        "rating",
        "tmdb_id",
        "tvdb_id",
    )

    def __init__(self, row: ImdbTitle) -> None:
        self.imdb_id = row.imdb_id
        self.kind = row.kind
        self.facts = row
        # A UUIDv7 minted here, exactly as the real `upsert_titles` mints one
        # per staged row. It exists so the genome cases can assert that the
        # stored key is *this* id and not MovieLens' own integer -- which is
        # the whole of the wrong implementation they kill, and is unassertable
        # against a fake that keys everything on `imdb_id`.
        self.id = new_id()
        self.tmdb_id: int | None = None
        self.tvdb_id: int | None = None
        self.popularity: float | None = None
        self.rating: tuple[float, int] | None = None
        # `enrichment_state <> 'skeleton'`, as a bool. Nothing on this port
        # writes it; the contract's seeder does, because the enriched-tier
        # coverage fraction is the one number that matters -- and because it
        # is the predicate `fill_credit_names` defers on.
        self.enriched = False
        # `titles.credit_names`, whose column default is `'{}'` rather than
        # NULL: `usher_array_text` is STRICT, so a NULL would null the whole
        # `search_document` and drop the title out of every full-text index.
        self.credit_names: tuple[str, ...] = ()


class FakeBulkCatalogRepository(BulkCatalogRepository):
    def __init__(self) -> None:
        self._titles: dict[str, _StoredTitle] = {}
        self._tmdb_ids: dict[tuple[int, TitleKind], TmdbId] = {}
        self._crosswalk: dict[str, IdCrosswalkPair] = {}
        self._genome: dict[uuid.UUID, tuple[tuple[float, ...], str]] = {}
        # `tag_id -> (tag, genome_revision)`. Independent of `_genome`
        # because the two tables are: nothing joins them, and the only
        # relationship they have is the equality a reader compares.
        self._genome_tags: dict[int, tuple[str, str]] = {}
        # `title_search_names`, flat: (title_id, kind, name, region, language).
        # A list rather than a dict keyed on anything, because the real table
        # carries **no unique constraint** -- `m09a` ships none deliberately --
        # so a dict would model an idempotence the database does not provide
        # and the doubling case would pass against a writer that never deletes.
        self._search_names: list[tuple[uuid.UUID, str, str, str | None, str | None]] = []
        self.window_depth = 0

    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        return self._window()

    @contextlib.asynccontextmanager
    async def _window(self) -> AsyncIterator[None]:
        # Suspends nothing -- there is no index to suspend -- but still
        # tracks entry/exit so the contract's "restores on an exception"
        # case observes something rather than passing vacuously.
        self.window_depth += 1
        try:
            yield
        finally:
            self.window_depth -= 1

    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        # First occurrence wins within a batch: the real implementation
        # assigns each staged row a fresh UUIDv7 in input order and runs
        # `SELECT DISTINCT ON (imdb_id) * FROM stg_titles ORDER BY imdb_id,
        # id` -- id ascending means the earliest-generated, i.e. first-seen,
        # row survives. Postgres requires *some* deterministic winner
        # regardless: one statement may not hit the same ON CONFLICT target
        # twice.
        deduped: dict[str, ImdbTitle] = {}
        for row in rows:
            deduped.setdefault(row.imdb_id, row)
        inserted = updated = 0
        for imdb_id, row in deduped.items():
            existing = self._titles.get(imdb_id)
            if existing is None:
                self._titles[imdb_id] = _StoredTitle(row)
                inserted += 1
            elif existing.facts != row:
                existing.facts = row
                existing.kind = row.kind
                updated += 1
        return BulkWriteResult(inserted=inserted, updated=updated)

    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        # Unlike upsert_titles, the real implementation's in-batch dedup
        # (`DISTINCT ON (imdb_id) ... ORDER BY imdb_id`) has no secondary
        # tie-break column, so which of two same-imdb_id ratings wins is
        # planner-dependent -- deliberately not pinned here either; only
        # "exactly one wins" is a real guarantee (see the contract test).
        changed = 0
        for row in {r.imdb_id: r for r in rows}.values():
            stored = self._titles.get(row.imdb_id)
            if stored is None:
                continue
            incoming = (row.average_rating, row.num_votes)
            if stored.rating != incoming:
                stored.rating = incoming
                changed += 1
        return changed

    async def fill_credit_names(self, rows: Sequence[ImdbCreditNames]) -> CreditNamesFillResult:
        # First occurrence wins within a batch, mirroring `upsert_titles`
        # above and the real implementation's `SELECT DISTINCT ON (imdb_id)
        # ... ORDER BY imdb_id, ordinal`.
        deduped: dict[str, ImdbCreditNames] = {}
        for row in rows:
            deduped.setdefault(row.imdb_id, row)
        filled = unmatched = deferred = 0
        for imdb_id, row in deduped.items():
            stored = self._titles.get(imdb_id)
            if stored is None:
                unmatched += 1
            elif stored.enriched:
                # The precedence rule, and the whole of what this fake can
                # model of it: `CreditRepository.replace_for_titles` owns the
                # column for every title TMDb has reached.
                deferred += 1
            elif stored.credit_names != row.names:
                stored.credit_names = row.names
                filled += 1
        return CreditNamesFillResult(filled=filled, unmatched=unmatched, deferred=deferred)

    async def replace_aliases(
        self, rows: Sequence[ImdbAka], *, imdb_ids: Sequence[str]
    ) -> AliasWriteResult:
        scope = dict.fromkeys(imdb_ids)
        # Before anything is deleted, and naming the offender: the row would
        # be inserted under a title no later scope deletes, which is the one
        # shape a re-import cannot repair. `sorted` so the message is the same
        # sentence whichever order the batch arrived in.
        stray = sorted({row.imdb_id for row in rows} - set(scope))
        if stray:
            raise ValueError(f"title.akas rows name titles outside the replacement scope: {stray}")
        resolved = {imdb_id: self._titles.get(imdb_id) for imdb_id in scope}
        unmatched = sum(1 for stored in resolved.values() if stored is None)
        kept = [
            entry
            for entry in self._search_names
            if not (
                entry[1] == SearchNameKind.ALIAS.value
                and entry[0] in {stored.id for stored in resolved.values() if stored is not None}
            )
        ]
        self._search_names = kept

        canonical = duplicate = 0
        # Lowest `ordering` wins, and the sort is stable, so two rows sharing
        # an `ordering` keep arrival order -- the real statement's final
        # `ORDER BY ... id` tie-break, whose ids ascend with arrival.
        seen: set[tuple[uuid.UUID, str]] = set()
        for row in sorted(rows, key=lambda one: one.ordering):
            stored = resolved.get(row.imdb_id)
            if stored is None:
                # Attributed to the scoped id that resolved to nothing, which
                # `unmatched` already counted. Counting it again here would
                # make the same absence two numbers.
                continue
            # `str.lower()`, not `casefold()`: the comparison has to be the
            # one `ix_titles_name_lower_prefix` is built over, and that index
            # is `lower(name)`.
            folded = row.name.lower()
            if folded == stored.facts.name.lower() or (
                stored.facts.original_name is not None
                and folded == stored.facts.original_name.lower()
            ):
                canonical += 1
                continue
            if (stored.id, folded) in seen:
                duplicate += 1
                continue
            seen.add((stored.id, folded))
            self._search_names.append(
                (stored.id, SearchNameKind.ALIAS.value, row.name, row.region, row.language)
            )
        return AliasWriteResult(
            written=len(seen), unmatched=unmatched, canonical=canonical, duplicate=duplicate
        )

    def search_names(self, imdb_id: str) -> tuple[tuple[str, str, str | None, str | None], ...]:
        """Every stored `title_search_names` row for a title, as
        `(kind, name, region, language)` ascending."""
        stored = self._titles.get(imdb_id)
        if stored is None:
            return ()
        return tuple(
            sorted(
                (entry[1], entry[2], entry[3], entry[4])
                for entry in self._search_names
                if entry[0] == stored.id
            )
        )

    def seed_person_search_name(self, imdb_id: str, name: str) -> None:
        """What `CreditRepository.replace_for_titles` leaves behind for one
        credited person: `kind = 'person'`, no region and no language."""
        stored = self._titles[imdb_id]
        self._search_names.append((stored.id, SearchNameKind.PERSON.value, name, None, None))

    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        # Highest popularity wins within a batch, matching `ORDER BY
        # tmdb_id, kind, popularity DESC`. There is no IS DISTINCT FROM
        # guard on this upsert (see repository.py's docstring on this
        # method), so -- unlike upsert_titles/apply_ratings -- this always
        # counts every distinct key, whether or not the stored row's data
        # actually changes; a replay reports the same count again, not
        # zero.
        winners: dict[tuple[int, TitleKind], TmdbId] = {}
        for row in rows:
            key = (row.tmdb_id, row.kind)
            current = winners.get(key)
            if current is None or row.popularity > current.popularity:
                winners[key] = row
        self._tmdb_ids.update(winners)
        return len(winners)

    async def upsert_crosswalk(self, rows: Sequence[IdCrosswalkPair]) -> int:
        # Same "no IS DISTINCT FROM guard" absence as upsert_tmdb_ids: a
        # replay reports the same count again, not zero.
        winners: dict[str, IdCrosswalkPair] = {}
        for row in rows:
            current = winners.get(row.imdb_id)
            if current is None or _crosswalk_sort_key(row) < _crosswalk_sort_key(current):
                winners[row.imdb_id] = row

        for row in winners.values():
            stored = self._crosswalk.get(row.imdb_id)
            self._crosswalk[row.imdb_id] = (
                row
                if stored is None
                # COALESCE, not `or`: an id of 0 is falsy but not absent,
                # so `or` would wrongly fall through to `stored`'s value --
                # unreachable with real TMDb/TVDB ids in practice, but
                # `is not None` is free. The three SPARQL joins each fill
                # one column and run as three separate passes, so a later
                # pass carrying only one column must not blank what an
                # earlier pass stored in the other two.
                else replace(
                    stored,
                    tmdb_movie_id=(
                        row.tmdb_movie_id if row.tmdb_movie_id is not None else stored.tmdb_movie_id
                    ),
                    tmdb_series_id=(
                        row.tmdb_series_id
                        if row.tmdb_series_id is not None
                        else stored.tmdb_series_id
                    ),
                    tvdb_series_id=(
                        row.tvdb_series_id
                        if row.tvdb_series_id is not None
                        else stored.tvdb_series_id
                    ),
                )
            )
        return len(winners)

    async def link_crosswalk(self) -> CrosswalkLinkResult:
        linked = unmatched = conflicted = 0
        claimed = {
            (stored.tmdb_id, stored.kind)
            for stored in self._titles.values()
            if stored.tmdb_id is not None
        }
        for imdb_id, pair in self._crosswalk.items():
            for tmdb_id, kind in (
                (pair.tmdb_movie_id, TitleKind.MOVIE),
                (pair.tmdb_series_id, TitleKind.SERIES),
            ):
                if tmdb_id is None:
                    continue
                stored = self._titles.get(imdb_id)
                if stored is None or stored.kind is not kind:
                    unmatched += 1
                    continue
                if stored.tmdb_id == tmdb_id:
                    continue  # already linked; a replay, not a conflict
                # `stored.tmdb_id is not None`: only fills a currently-NULL
                # tmdb_id (this method's own port docstring). A title that
                # already carries a *different* id must not be silently
                # retargeted -- that would overwrite popularity a later,
                # better-informed enrichment pass already wrote, not merely
                # misreport a count. Without this guard the fake reports a
                # *link* here where Postgres reports a *conflict* (measured
                # directly), and both the id and popularity get overwritten.
                if stored.tmdb_id is not None or (tmdb_id, kind) in claimed:
                    conflicted += 1
                    continue
                stored.tmdb_id = tmdb_id
                universe = self._tmdb_ids.get((tmdb_id, kind))
                if universe is not None:
                    stored.popularity = universe.popularity
                claimed.add((tmdb_id, kind))
                linked += 1

        # tvdb_id: same NULL-only-fills guard, plus global uniqueness
        # (ix_titles_tvdb_id is a unique partial index -- two titles cannot
        # legitimately hold the same one, the exact "fake ignores
        # provider-id uniqueness" class documented on
        # title_repository_contract.py's own duplicate-tvdb-id test).
        # `tvdb_winner` mirrors the real implementation's own dedup of the
        # *stored crosswalk data itself* -- `SELECT DISTINCT ON
        # (tvdb_series_id) ... ORDER BY tvdb_series_id, imdb_id` -- which
        # runs before any title is even considered, because Wikidata can
        # associate the same tvdb id with more than one imdb_id.
        #
        # Deliberately does not touch linked/unmatched/conflicted: the real
        # implementation's classification query is scoped to the tmdb-only
        # _CROSSWALK_PAIRS view, and its tvdb UPDATE's rowcount is never
        # read, so tvdb linking is invisible to those three counters there
        # too.
        tvdb_winner: dict[int, str] = {}
        for imdb_id, pair in self._crosswalk.items():
            if pair.tvdb_series_id is None:
                continue
            current = tvdb_winner.get(pair.tvdb_series_id)
            if current is None or imdb_id < current:
                tvdb_winner[pair.tvdb_series_id] = imdb_id
        claimed_tvdb = {
            stored.tvdb_id for stored in self._titles.values() if stored.tvdb_id is not None
        }
        for tvdb_id, winner_imdb_id in tvdb_winner.items():
            stored = self._titles.get(winner_imdb_id)
            if stored is not None and stored.tvdb_id is None and tvdb_id not in claimed_tvdb:
                stored.tvdb_id = tvdb_id
                claimed_tvdb.add(tvdb_id)

        return CrosswalkLinkResult(linked=linked, unmatched=unmatched, conflicted=conflicted)

    async def upsert_genome_vectors(
        self, rows: Sequence[GenomeVector], *, revision: str
    ) -> GenomeWriteResult:
        # First occurrence wins among rows resolving to one title, mirroring
        # `SELECT DISTINCT ON (t.id) ... ORDER BY t.id, s.imdb_id` -- the real
        # statement *must* pick one, because a second hit on the same ON
        # CONFLICT target is a CardinalityViolationError that aborts the whole
        # batch. Here it is a dict; there it is a clause a mutation deletes.
        #
        # `kind is MOVIE` is the `AND t.kind = 'movie'` of the real statement.
        # `imdb_id` is unique per title regardless of kind, so this changes
        # nothing against today's data and is exactly why it needs a case.
        by_title: dict[uuid.UUID, tuple[float, ...]] = {}
        unmatched = 0
        for row in rows:
            stored = self._titles.get(row.imdb_id)
            if stored is None or stored.kind is not TitleKind.MOVIE:
                unmatched += 1
                continue
            by_title.setdefault(stored.id, row.relevance)
        inserted = updated = 0
        for title_id, relevance in by_title.items():
            if title_id in self._genome:
                updated += 1
            else:
                inserted += 1
            self._genome[title_id] = (relevance, revision)
        return GenomeWriteResult(inserted=inserted, updated=updated, unmatched=unmatched)

    async def replace_genome_tags(self, tags: Sequence[GenomeTag], *, revision: str) -> int:
        # Before the clear, exactly as the real one refuses before its DELETE.
        # This is the arm where that ordering is observable: the Postgres one
        # wraps both statements in a SAVEPOINT, so the same mutation there
        # rolls the delete back with the raise and stays green -- the finding
        # `replace_for_user` already carries.
        _refuse_partial_vocabulary(tags, revision)
        # Assignment, not `update`: a replace leaves no row of the previous
        # release behind, which is the one behaviour separating this method
        # from `upsert_genome_vectors` above.
        self._genome_tags = {tag.tag_id: (tag.tag, revision) for tag in tags}
        return len(tags)

    async def genome_coverage(self) -> GenomeCoverage:
        revisions: dict[str, int] = {}
        for _, revision in self._genome.values():
            revisions[revision] = revisions.get(revision, 0) + 1
        enriched = [stored for stored in self._titles.values() if stored.enriched]
        return GenomeCoverage(
            with_vector=len(self._genome),
            titles=len(self._titles),
            movies=sum(1 for s in self._titles.values() if s.kind is TitleKind.MOVIE),
            enriched=len(enriched),
            # Counted by walking *titles*, not by counting vectors: the two
            # agree only while every genome-bearing title happens to be
            # enriched, which is the mutation the contract case kills.
            enriched_with_vector=sum(1 for s in enriched if s.id in self._genome),
            revisions=tuple(sorted(revisions.items())),
        )

    async def count_titles(self) -> int:
        return len(self._titles)

    # --- test-only accessors, mirroring the contract's readback hooks ----

    def title_id(self, imdb_id: str) -> uuid.UUID | None:
        stored = self._titles.get(imdb_id)
        return stored.id if stored else None

    def genome(self, title_id: uuid.UUID) -> tuple[float, ...] | None:
        found = self._genome.get(title_id)
        return None if found is None else found[0]

    def genome_keys(self) -> set[object]:
        return set(self._genome)

    def genome_tags(self) -> tuple[tuple[int, str, str], ...]:
        return tuple(
            (tag_id, tag, revision) for tag_id, (tag, revision) in sorted(self._genome_tags.items())
        )

    def mark_enriched(self, imdb_id: str) -> None:
        stored = self._titles.get(imdb_id)
        if stored is not None:
            stored.enriched = True

    def mark_derived(self, imdb_id: str, names: tuple[str, ...]) -> None:
        """The state `DeriveService` leaves a title in: off the skeleton tier,
        with `credit_names` written from the TMDb-derived `credits`."""
        stored = self._titles.get(imdb_id)
        if stored is not None:
            stored.enriched = True
            stored.credit_names = names

    def credit_names(self, imdb_id: str) -> tuple[str, ...] | None:
        stored = self._titles.get(imdb_id)
        return stored.credit_names if stored else None

    def popularity(self, imdb_id: str) -> float | None:
        stored = self._titles.get(imdb_id)
        return stored.popularity if stored else None

    def tmdb_id(self, imdb_id: str) -> int | None:
        stored = self._titles.get(imdb_id)
        return stored.tmdb_id if stored else None

    def tvdb_id(self, imdb_id: str) -> int | None:
        stored = self._titles.get(imdb_id)
        return stored.tvdb_id if stored else None

    def name(self, imdb_id: str) -> str | None:
        stored = self._titles.get(imdb_id)
        return stored.facts.name if stored else None


def _refuse_partial_vocabulary(tags: Sequence[GenomeTag], revision: str) -> None:
    """The port's four `replace_genome_tags` refusals, modelled rather than
    diverged -- kept identical to
    `usher.db.repositories.bulk._refuse_partial_vocabulary`, which holds the
    argument for each. The contract suite is what holds the two together.

    Modelled rather than diverged because two of the four have no Postgres
    constraint behind them at all: `ck_genome_tags_tag_id_in_vocabulary`
    cannot see a *gap*, and an empty `tags` is a legal `DELETE` followed by a
    legal zero-row `INSERT`. A fake that skipped them would let a caller-
    assembly bug through every unit test in the milestone.
    """
    if not tags:
        raise ValueError("a genome vocabulary of no tags is not a vocabulary")
    if sorted(tag.tag_id for tag in tags) != list(range(1, len(tags) + 1)):
        raise ValueError(
            f"a genome vocabulary is tags 1...{len(tags)} and this one is not; "
            "the vector is built by index and a gap renames every later lane"
        )
    if any(not tag.tag for tag in tags):
        raise ValueError("a genome tag with no name is a lane that reads as labelled")
    if not revision:
        raise ValueError("a genome vocabulary must record the release it came from")
