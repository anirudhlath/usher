"""Port for bulk open datasets, and the record DTOs that cross that boundary.

A `BulkDataset` produces already-normalised records from a third-party bulk
dump. It never writes: persistence is `BulkCatalogRepository`'s job
(`usher.ports.repository`), so a dataset implementation can be unit-tested
against committed slices with no database, and the loader can be tested
with no network.

**Ship importers, never data.** No implementation of this port may embed,
commit, or ship third-party metadata — IMDb and TMDb both prohibit
redistribution (PRD 04). Test fixtures are small, hand-written, obviously
synthetic slices; CI never downloads.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from usher.domain.enums import TitleKind


@dataclass(frozen=True, slots=True)
class BulkCursor:
    """Where a resumable import got to.

    `slots=True` on this and every record below: a batch holds tens of
    thousands of these at once, and `__slots__` drops per-instance `__dict__`
    overhead. Cheap, and the only reason it is worth mentioning at all.

    `revision` is an opaque upstream-snapshot token (an HTTP `ETag`, an
    export date, a query date). `position` is an opaque, dataset-defined
    offset whose only contract is that resuming from it never *misses* a
    record — it may legitimately replay some, because every write on the
    far side is an upsert.

    A stored cursor is only usable if its `revision` still matches what the
    dataset reports now. When upstream has moved, the importer restarts from
    `position = 0` rather than splicing two different snapshots together.
    """

    revision: str
    position: int
    rows_seen: int


@dataclass(frozen=True, slots=True)
class BulkBatch[RowT]:
    """One committable unit of work: the rows, plus the cursor that is
    correct *after* they have been persisted.

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
    """One row of IMDb's `title.ratings.tsv.gz`.

    `community_rating` is IMDb's `averageRating`, already on the 0-10 scale
    `Title.community_rating` promises (`Field(ge=0, le=10)`), so no
    rescaling happens anywhere.
    """

    imdb_id: str
    community_rating: float
    vote_count: int


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


class BulkDataset[RowT](ABC):
    """A third-party bulk dataset, streamed as resumable batches.

    Implementations: `IMDbTitleDataset`, `IMDbRatingDataset`,
    `TMDbIdDataset`, `WikidataCrosswalkDataset` (`usher.adapters.bulk`).
    Port named for the role, implementations for the service — the same
    split as `SourceAdapter`/`EmbyAdapter` (ADR-0009).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, used as the `import_runs.dataset` key. Changing
        one orphans its checkpoint, which restarts that import from zero
        rather than corrupting anything."""

    @property
    @abstractmethod
    def attribution(self) -> str:
        """The attribution string this dataset's licence requires a client
        to display (PRD 04's hard rule 4). Never empty — a dataset with no
        attribution requirement returns its own name and source URL, so the
        API surface has something to serve either way."""

    @abstractmethod
    async def revision(self) -> str:
        """The current upstream snapshot token, cheaply.

        Raises `PortUnavailable` if upstream cannot be reached — this is the
        first call a run makes, so an unreachable dataset fails before any
        write happens.
        """

    @abstractmethod
    def batches(self, *, resume_from: BulkCursor | None = None) -> AsyncIterator[BulkBatch[RowT]]:
        """Stream batches, optionally continuing from a stored cursor.

        Plain `def`, not `async def`: this returns an `AsyncIterator`
        directly rather than a coroutine that produces one — the same shape
        `SourceAdapter.list_items` uses.

        Contract an implementation must guarantee:
        - **Must raise, never truncate silently.** A stream that stops
          because upstream failed is otherwise indistinguishable from one
          that stopped because the dataset ended, and the caller would
          checkpoint a partial import as complete. Raise `PortUnavailable`,
          `PortRateLimited`, or `PortDataMalformed` (`usher.ports.errors`).
        - Each yielded `BulkBatch.cursor` is correct **after** that batch is
          persisted, so the caller can commit rows and cursor together.
        - `resume_from` whose `revision` differs from `revision()` is
          ignored, and the stream restarts from the beginning.
        - Batches may replay rows across a resume; every row is written
          through an upsert, so replay is a no-op rather than a duplicate.
        - No batch is empty. A dataset with nothing left yields nothing.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources — the HTTP client, and any open file
        handle. Called by the caller that constructed this dataset, in a
        `finally`."""
