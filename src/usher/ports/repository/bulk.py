"""The bulk-load path: one port for the dataset importers to write through."""

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
    """What one scoped alias replacement actually stored, and what it dropped."""

    written: int
    unmatched: int
    canonical: int
    duplicate: int


@dataclass(frozen=True, slots=True)
class CreditNamesFillResult:
    """What one batch of IMDb credit names actually changed."""

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
    """Genome coverage with its denominators, because "~7%" never had one."""

    with_vector: int
    titles: int
    movies: int
    enriched: int
    enriched_with_vector: int
    revisions: tuple[tuple[str, int], ...] = ()


class BulkCatalogRepository(ABC):
    """Bulk writes into the catalog, deliberately *not* expressed through `TitleRepository`."""

    @abstractmethod
    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        """Scope inside which the implementation may relax storage-level optimisations that only.

        pay for themselves on one-row-at-a-time writes, restoring them on exit.
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
        """Set `imdb_average_rating`/`imdb_num_votes` on titles that already exist.

        returning how many rows changed.
        """

    @abstractmethod
    async def fill_credit_names(self, rows: Sequence[ImdbCreditNames]) -> CreditNamesFillResult:
        """Fill `titles.credit_names` from IMDb, for titles TMDb has not reached.

        Never creates a title, and never writes a person or a credit.
        """

    @abstractmethod
    async def replace_aliases(
        self, rows: Sequence[ImdbAka], *, imdb_ids: Sequence[str]
    ) -> AliasWriteResult:
        """Replace the `alias` half of `title_search_names` for the titles `imdb_ids` names.

        from IMDb `title.akas`.

        Never creates a title, and never touches a row of any other `kind`.
        """

    @abstractmethod
    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        """Insert or update the TMDb id universe, keyed on `(tmdb_id, kind)`.

        Returns rows written.
        """

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
        """Stamp stored crosswalk pairs onto catalog titles, in one pass."""

    @abstractmethod
    async def upsert_genome_vectors(
        self, rows: Sequence[GenomeVector], *, revision: str
    ) -> GenomeWriteResult:
        """Store genome vectors against the titles their `imdb_id` resolves to.

        returning what changed and how many resolved to nothing.
        """

    @abstractmethod
    async def replace_genome_tags(self, tags: Sequence[GenomeTag], *, revision: str) -> int:
        """Replace the whole genome tag vocabulary with `tags` at `revision`.

        returning how many rows it wrote.
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
        """How many titles the catalog holds.

        Used to decide whether `bulk_load_window` may suspend indexes, and reported by
        the CLI.
        """
