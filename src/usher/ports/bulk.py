"""Port for bulk open datasets, and the record DTOs that cross that boundary."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from usher.domain.enums import TitleKind


@dataclass(frozen=True, slots=True)
class BulkCursor:
    """Where a resumable import got to."""

    revision: str
    position: int
    rows_seen: int


@dataclass(frozen=True, slots=True)
class BulkBatch[RowT]:
    """One committable unit of work.

    the rows, plus the cursor that is correct *after* they have been persisted.

    Generic over the row type rather than carrying `Mapping[str, object]`:
    every implementation yields exactly one record shape, and a weakly-typed
    payload would push the field-name knowledge out of the adapter and into
    the loader, which is the opposite of what this port is for.
    """

    rows: tuple[RowT, ...]
    cursor: BulkCursor


@dataclass(frozen=True, slots=True)
class ImdbTitle:
    """One retained row of IMDb's `title.basics.tsv.gz`.

    Only the four `titleType` values that map onto `TitleKind` survive the
    adapter (`movie`, `tvMovie` -> MOVIE; `tvSeries`, `tvMiniSeries` ->
    SERIES). `tvEpisode`, `short`, `video`, `videoGame`, `tvSpecial`,
    `tvShort`, and adult titles are dropped — see `usher.adapters.bulk.imdb`.
    """

    imdb_id: str
    kind: TitleKind
    name: str
    original_name: str | None
    year: int | None
    end_year: int | None
    runtime_minutes: int | None
    genres: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ImdbRating:
    """One row of `title.ratings.tsv.gz`, named for the source that supplied it.

    `average_rating` is IMDb's `averageRating`, already on the 0-10 scale
    `titles.imdb_average_rating` promises, so no rescaling happens anywhere.

    **The names carry the source because the columns do.** These were
    `community_rating` and `vote_count` until ADR-0040, which is how an IMDb
    import came to overwrite TMDb's figures with nothing recording which
    source had won. **The gap is ~38x, over one identified population counted
    both ways**: of the frozen tier's 130,647 enriched rows, median TMDb
    `vote_count` **15** against a median frozen IMDb `numVotes` of **576**
    (`.claude/rules/tmdb-and-enrichment.md`, group S3). That pairing is
    before-and-after over one frozen set of ids rather than two columns read
    off one row -- no row could hold both until `m10a` and this port's own
    redirect, which is the entire defect.
    """

    imdb_id: str
    average_rating: float
    num_votes: int


@dataclass(frozen=True, slots=True)
class ImdbAka:
    """One retained row of IMDb's `title.akas.tsv.gz`: a localised alias."""

    imdb_id: str
    ordering: int
    name: str
    region: str | None
    language: str | None


@dataclass(frozen=True, slots=True)
class ImdbName:
    """One usable row of IMDb's `name.basics.tsv.gz`: a person and their name."""

    imdb_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ImdbPrincipal:
    """One row of IMDb's `title.principals.tsv.gz`: a person on a title.

    **`category`, `job` and `characters` are read and dropped**, the way
    `ImdbAka` reads and drops `types` and `attributes`: nothing downstream of
    this record has a column for a role, because there is no `credits` row.
    The 13 categories are measured in `usher.adapters.bulk.imdb` and none is
    filtered on.

    `ordering` is IMDb's own 1-based per-title rank, carried unconverted --
    there is no `billing_order` to re-base it onto. It is the only ranking the
    dump supplies and it is what orders `titles.credit_names`, whose order
    *is* the ranking. Measured over the pinned `title.principals.tsv.gz`
    (`"08ce60665889cb40c7371e1eab44a1f2-93"`, 101,151,422 data rows,
    2026-08-11): present and integral on every row, min 1, max 75, and
    ascending within every one of the 11,491,032 titles.
    """

    imdb_id: str
    ordering: int
    person_imdb_id: str


@dataclass(frozen=True, slots=True)
class ImdbCreditNames:
    """Every name IMDb credits on one title, resolved and in rank order.

    **The join of `title.principals` and `name.basics`, done in the adapter
    because there is nowhere else to do it.** With no `people` table the
    right-hand side of that join has no home in the database, so
    `IMDbCreditNamesDataset` resolves it against an in-memory index and this
    record is what crosses the port -- already ordered, already deduplicated,
    and never empty. A title whose principals all name people `name.basics`
    does not hold yields no record at all rather than an empty one, because
    the writer *sets* `titles.credit_names` and an empty tuple would blank an
    array some other source filled.
    """

    imdb_id: str
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TmdbId:
    """One line of TMDb's daily ID export.

    The export carries no localised title, no year, and no overview — only
    an id, an original name, and popularity (verified against
    `movie_ids_*.json.gz` / `tv_series_ids_*.json.gz`). That is why Phase 1
    lands in its own table instead of creating `Title` rows: there is not
    enough here to build a catalog entry from, and Phase 2 resolves these
    ids onto titles the IMDb skeleton already holds.

    `adult` is always `False` for series — TMDb's TV export has no `adult`
    field at all (verified), so the adapter defaults it rather than
    inventing one.
    """

    tmdb_id: int
    kind: TitleKind
    original_name: str
    popularity: float
    adult: bool = False


@dataclass(frozen=True, slots=True)
class IdCrosswalkPair:
    """One IMDb id and whatever provider ids Wikidata associates with it.

    Keyed on `imdb_id` because that is the id the catalog already has after
    Phase 0. All three provider columns are independently optional: the
    three SPARQL joins that populate them (P4947, P4983, P4835) each fill
    exactly one, and an item may appear in one, two, or all three.
    """

    imdb_id: str
    tmdb_movie_id: int | None = None
    tmdb_series_id: int | None = None
    tvdb_series_id: int | None = None


# The MovieLens tag vocabulary's width, and the one place it is written.
GENOME_TAG_COUNT = 1128


@dataclass(frozen=True, slots=True)
class GenomeTag:
    """One row of MovieLens' `genome-tags.csv`: a vector lane and its name."""

    tag_id: int
    tag: str


@dataclass(frozen=True, slots=True)
class GenomeVector:
    """One movie's dense MovieLens tag-genome vector."""

    movie_id: int
    imdb_id: str
    tmdb_id: int | None
    relevance: tuple[float, ...]


class BulkDataset[RowT](ABC):
    """A third-party bulk dataset, streamed as resumable batches.

    Implementations: `IMDbTitleDataset`, `IMDbRatingDataset`,
    `IMDbAkaDataset`, `TMDbIdDataset`, `WikidataCrosswalkDataset`,
    `MovieLensGenomeDataset` (`usher.adapters.bulk`). Port named for the
    role, implementations for the service — the same split as
    `SourceAdapter`/`EmbyAdapter` (ADR-0009).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, used as the `import_runs.dataset` key.

        Changing one orphans its checkpoint, which restarts that import from zero rather
        than corrupting anything.
        """

    @property
    @abstractmethod
    def attribution(self) -> str:
        """The attribution string this dataset's licence requires a client to display (PRD 04's.

        hard rule 4).

        Never empty — a dataset with no attribution requirement returns its own name and
        source URL, so the API surface has something to serve either way.
        """

    @abstractmethod
    async def revision(self) -> str:
        """The current upstream snapshot token, cheaply.

        Raises `PortUnavailable` if upstream cannot be reached, or
        `PortRateLimited` if it answered but asked to be backed off (e.g. an
        HTTP 429) — both `usher.ports.errors`, and both real: the shared
        download helper every M2 adapter's `revision()` delegates to routes
        a 429 through exactly that translation. This is the first call a
        run makes, so an unreachable or rate-limited dataset fails before
        any write happens, and a caller must catch both from this call the
        same way it catches both from `batches()` — a port's docstring
        naming only one of the errors it actually raises is what let a
        `PortRateLimited` here escape uncaught in an earlier draft of the
        caller that drives this port.
        """

    @abstractmethod
    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[RowT]]:
        """Stream batches, optionally continuing from a stored cursor."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources — the HTTP client, and any open file handle.

        Called by the caller that constructed this dataset, in a `finally`.
        """
