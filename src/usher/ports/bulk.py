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
    export date, a query date). `position` is a dataset-defined integer
    offset whose only contract is that resuming from it never *misses* a
    record — it may legitimately replay some, because every write on the
    far side is an upsert. Concretely `int`, not "opaque" the way
    `revision` is: it round-trips through `ImportRun.position` /
    `ImportRunRow.position` (`usher.domain.bootstrap`,
    `usher.db.models.bootstrap` — both a plain, non-negative `int`/
    `Integer`, verified against the schema those already ship), and every
    M2 dataset's position is already integer-shaped (a line number or a
    work-unit index). A future dataset whose upstream hands back a
    non-numeric continuation token instead would need this field widened
    together with `ImportRun`/`ImportRunRow` in the same change — not
    something to pre-empt speculatively before a real dataset needs it,
    since neither an `int | str` union nor a numeric-string convention here
    actually survives that round trip on its own: the persisted column
    stays a plain `Integer` either way.

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


# The MovieLens tag vocabulary's width, and the one place it is written.
#
# It has to agree in two places that cannot check each other -- the importer
# verifies it against `genome-tags.csv` before reading a score, and
# `GenomeScoreRow.relevance` declares `halfvec(GENOME_TAG_COUNT)` -- so it
# lives here, on the port both sides already import, rather than being
# spelled twice. `EMBEDDING_DIMENSIONS` gets away with living on the storage
# side alone because nothing outside `db/` needs it.
GENOME_TAG_COUNT = 1128


@dataclass(frozen=True, slots=True)
class GenomeTag:
    """One row of MovieLens' `genome-tags.csv`: a vector lane and its name.

    **`tag_id` is a lane index, not an identifier**, and that is the whole of
    why this DTO is a pair rather than a bare name. `GenomeVector.relevance[i]`
    is the relevance of the tag whose `tag_id` is `i + 1`, so the id is what
    binds a name to a position; carrying only the names would make the binding
    the *sequence*'s job, and a sequence that is one element short at position
    3 is still a well-formed sequence describing every later tag wrongly.

    **Measured against the real member, 2026-08-07** (`ml-latest/
    genome-tags.csv`, 18,103 bytes, read through `CachedDatasetFile.
    member_lines`): **1,128 rows**, `tagId` exactly `1…1128` and ascending, no
    name empty, no name containing a comma, 1,128 distinct names, longest 65
    characters. The file is CRLF-terminated and `member_lines`' universal-
    newline decode already removes the `\\r`, so nothing here strips one.

    **`tag_id` carries no ceiling on this dataclass**, deliberately, and the
    ceiling it does have is stated where it can be enforced against a whole
    batch rather than a row at a time: `BulkCatalogRepository.
    replace_genome_tags` refuses anything that is not exactly `1…n` before it
    writes, so the largest `tag_id` that can reach a driver is the length of
    the vocabulary being written. A `le=` here would be a second, weaker copy
    of that -- it cannot see a *gap*, which is the failure that matters, and a
    frozen dataclass validating one field in isolation is not where this
    project puts an invariant over a batch.
    """

    tag_id: int
    tag: str


@dataclass(frozen=True, slots=True)
class GenomeVector:
    """One movie's dense MovieLens tag-genome vector.

    **One vector per movie, not one score per row** — boundary call 7. The
    genome is a genuinely dense matrix: every one of the 16,376 movies
    carries a value for every one of the 1,128 tags, verified by counting.
    The tall shape `(title_id, tag_id, relevance)` stores 18,472,128 rows to
    express a matrix with no holes in it, and measures at 2,106 MB against
    45 MB for this shape, against a database PRD 08 budgets at 8-12 GB
    *total*.

    `relevance` is exactly `len(tag vocabulary)` floats — 1,128 against the
    real archive — **in ascending `tagId` order**, which is the only thing
    that makes two vectors comparable. Nothing downstream re-derives that
    order, so a vector assembled under a different one is silently wrong at
    every position; `MovieLensGenomeDataset` therefore builds it by index
    rather than by append, and the vocabulary's contiguity is checked before
    a single score is read.

    **The values are the archive's own relevances, untransformed, and that
    was measured rather than assumed.** Every relevance is non-negative with
    mean 0.111, so two *unrelated* films share a background profile and the
    obvious worry is that cosine saturates — which is the saturation
    `SimilarityService._WEIGHTS` already documents for genres. Measured over
    all 16,376 vectors and all 268,157,000 off-diagonal pairs, against a bar
    written before the run: mean 0.6101, sd 0.0913, p1 0.4075, and a
    top-10-neighbour gap of 0.2456. It does not saturate, so the vectors are
    stored as supplied. `usher.adapters.bulk.movielens` carries the bar, the
    two mean-centred variants that were measured alongside, and why neither
    ships. Do not "fix" this by centring without re-reading that.

    `imdb_id` is already `'tt' || lpad(imdbId, 7, '0')` — the adapter does
    the padding, because `links.csv` carries the digits bare and 6,559 of
    its 86,537 rows are 8 wide rather than 7. `tmdb_id` is carried but is
    not the join key: TMDb ids are unique only *per kind* (ADR-0011) and
    MovieLens is movies-only, so joining on it requires supplying that kind
    from outside the row. `movie_id` is MovieLens' own id, kept for
    diagnostics — it is what an operator has when reconciling a row against
    the archive, and it is the id a wrong implementation stores in place of
    the resolved `Title.id`.
    """

    movie_id: int
    imdb_id: str
    tmdb_id: int | None
    relevance: tuple[float, ...]


class BulkDataset[RowT](ABC):
    """A third-party bulk dataset, streamed as resumable batches.

    Implementations: `IMDbTitleDataset`, `IMDbRatingDataset`,
    `TMDbIdDataset`, `WikidataCrosswalkDataset`, `MovieLensGenomeDataset`
    (`usher.adapters.bulk`). Port named for the role, implementations for
    the service — the same split as `SourceAdapter`/`EmbyAdapter`
    (ADR-0009).
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
        """Stream batches, optionally continuing from a stored cursor.

        Plain `def`, not `async def`: this returns an `AsyncIterator`
        directly rather than a coroutine that produces one — the same shape
        `SourceAdapter.list_items` uses.

        `revision`, when given, is the value the caller's own prior call to
        `revision()` already returned this run — equivalent to resolving it
        again internally, never a different value. `None` (the default)
        means "resolve it yourself", so every existing caller and
        implementation is unaffected. Exists purely so a caller that has
        already paid the cost of `revision()` is not forced to pay it again
        for an implementation whose own `revision()` is itself expensive
        (e.g. a sequential multi-day HEAD-request scan) — free for any
        implementation whose `revision()` is already cheap.

        Contract an implementation must guarantee:
        - **Must raise, never truncate silently.** A stream that stops
          because upstream failed is otherwise indistinguishable from one
          that stopped because the dataset ended, and the caller would
          checkpoint a partial import as complete. Raise `PortUnavailable`,
          `PortRateLimited`, or `PortDataMalformed` (`usher.ports.errors`).
        - Each yielded `BulkBatch.cursor` is correct **after** that batch is
          persisted, so the caller can commit rows and cursor together.
        - `resume_from` whose `revision` differs from the current one is
          ignored, and the stream restarts from the beginning.
        - Batches may replay rows across a resume; every row is written
          through an upsert, so replay is a no-op rather than a duplicate.
        - A batch's `rows` may be empty — an implementation may yield a
          row-less batch solely to advance the cursor, e.g. past a run of
          upstream records its own filtering drops entirely, so a trailing
          run of those doesn't lose progress on a crash. A dataset with
          nothing left yields nothing further, not an empty batch.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources — the HTTP client, and any open file
        handle. Called by the caller that constructed this dataset, in a
        `finally`."""
