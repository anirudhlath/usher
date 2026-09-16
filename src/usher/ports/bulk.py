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

    The rows, plus the cursor that is correct *after* they are persisted.

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
    `titles.imdb_average_rating` promises, so nothing rescales it.

    The names carry the source because the columns do. IMDb's vote counts are
    an order of magnitude above TMDb's for the same titles, so a source-blind
    name lets one import overwrite the other's figures with nothing recording
    which won.
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

    `category`, `job` and `characters` are read and dropped, as `ImdbAka`
    drops `types` and `attributes`: nothing downstream has a column for a
    role, because there is no `credits` row. No category is filtered on.

    `ordering` is IMDb's own 1-based per-title rank, carried unconverted --
    there is no `billing_order` to re-base it onto. It is the only ranking
    the dump supplies, and it is what orders `titles.credit_names`, whose
    order *is* the ranking.
    """

    imdb_id: str
    ordering: int
    person_imdb_id: str


@dataclass(frozen=True, slots=True)
class ImdbCreditNames:
    """Every name IMDb credits on one title, resolved and in rank order.

    The join of `title.principals` and `name.basics`, done in the adapter
    because with no `people` table the right-hand side has no home in the
    database. Already ordered, already deduplicated, and never empty: a title
    whose principals all name people `name.basics` does not hold yields no
    record rather than an empty one, because the writer *sets*
    `titles.credit_names` and an empty tuple would blank what another source
    filled.
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

    Implementations live in `usher.adapters.bulk`. The port is named for the
    role and they are named for the service, so a second provider of one
    dataset does not rename this.
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
        """The attribution string this dataset's licence requires a client to display.

        Never empty -- a dataset with no attribution requirement returns its
        own name and source URL, so the API has something to serve either way.
        """

    @abstractmethod
    async def revision(self) -> str:
        """The current upstream snapshot token, cheaply.

        Raises `PortUnavailable` if upstream cannot be reached, or
        `PortRateLimited` if it answered but asked to be backed off. Both are
        real: the shared download helper routes an HTTP 429 through that
        translation. This is the first call a run makes, so an unreachable or
        rate-limited dataset fails before any write happens, and a caller
        must catch both here exactly as it catches both from `batches()`.
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
