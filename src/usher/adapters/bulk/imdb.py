"""IMDb non-commercial datasets -> `ImdbTitle` / `ImdbRating`.

Four TSV quirks, and exactly how each is handled:

1. **`\\N` means NULL.** Not an empty string, not a literal backslash-N in the
   data. `_optional` maps it to `None`; every numeric field goes through it
   before `int()`/`float()`.
2. **There is no quoting mechanism.** IMDb's TSVs are raw tab-separated
   values, and title fields contain literal `"` characters (21 in the first
   553,395 rows of `title.basics.tsv.gz`, e.g. `tt0073045` ->
   `"Giliap"`). `csv.reader` with its default `QUOTE_MINIMAL` **silently
   strips them**, turning `"Giliap"` into `Giliap` -- verified directly. This
   module therefore uses `line.split("\\t")` and never the `csv` module.
   `csv.reader(..., quoting=csv.QUOTE_NONE)` also preserves them, but a plain
   split has nothing to misconfigure.
3. **gzip.** Handled one layer down, in `CachedDatasetFile.lines`.
4. **`isAdult` is `0`/`1`, and `titleType` needs filtering.** Adult titles are
   dropped outright (PRD 04). Only the four `titleType` values that map onto
   `TitleKind` survive; see `_RETAINED_TYPES`.

`title.principals`, `title.crew`, `title.akas`, `name.basics`, and
`title.episode` are **not** imported here. PRD 04's Phase 0 text names
cast/crew and akas, but `Person`, `Credit`, and `Episode` have no domain
models or tables yet -- there is literally nowhere to put those rows. They
land with the milestone that adds those entities; see PRD 04's corrected
Phase 0 note.

Measured 2026-07-30: `title.basics.tsv.gz` is 214.4 MiB and
`title.ratings.tsv.gz` is 8.2 MiB, so this milestone downloads ~223 MiB, not
PRD 04's 1.83 GiB (which is the total across all seven IMDb files).
"""

from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx

from usher.adapters.bulk.download import CachedDatasetFile
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbRating, ImdbTitle
from usher.ports.errors import PortDataMalformed

IMDB_BASE_URL = "https://datasets.imdbws.com/"

# The exact attribution string IMDb's non-commercial licence requires.
IMDB_ATTRIBUTION = "Information courtesy of IMDb (https://www.imdb.com). Used with permission."

# Retained titleTypes, mapped onto TitleKind. `tvEpisode` is deliberately
# absent despite PRD 04 listing it: TitleKind is movie|series only, and
# episodes are a separate entity (PRD 02) with no table yet. `short`, `video`,
# `videoGame`, `tvShort`, `tvSpecial`, and `tvPilot` are dropped as PRD 04
# specifies. Retaining exactly these four is what yields the 1,127,975
# "movies + series" figure PRD 04 itself cites.
_RETAINED_TYPES: dict[str, TitleKind] = {
    "movie": TitleKind.MOVIE,
    "tvMovie": TitleKind.MOVIE,
    "tvSeries": TitleKind.SERIES,
    "tvMiniSeries": TitleKind.SERIES,
}

_BASICS_COLUMNS = 9
_RATINGS_COLUMNS = 3


def _optional(value: str) -> str | None:
    r"""IMDb's own null sentinel. `\N` is the documented marker; an empty
    field is treated the same way because a trailing tab produces one."""
    return None if value in (r"\N", "") else value


def _optional_int(value: str, *, imdb_id: str, column: str) -> int | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        # Not silently dropped: a numeric column that stopped being numeric is
        # an upstream format change, and continuing past it would import a
        # subtly wrong catalog. PortDataMalformed carries the row id and the
        # column, never the whole line.
        raise PortDataMalformed(
            "IMDb row has a non-integer value where an integer is required",
            detail=f"{imdb_id}.{column}",
        ) from exc


def parse_basics_row(line: str) -> ImdbTitle | None:
    """One `title.basics.tsv.gz` line, or `None` if the row is filtered out.

    Filtered (returns `None`): the header line, adult titles, and every
    `titleType` outside `_RETAINED_TYPES`. Malformed (raises
    `PortDataMalformed`): a wrong column count, or a non-integer year/runtime.
    The distinction matters -- a filtered row is expected and silent, a
    malformed row stops the import.
    """
    fields = line.split("\t")
    if len(fields) != _BASICS_COLUMNS:
        raise PortDataMalformed(
            f"IMDb title.basics row has {len(fields)} columns, expected {_BASICS_COLUMNS}",
            detail=fields[0] if fields else "<empty line>",
        )
    imdb_id, title_type, primary, original, is_adult, start, end, runtime, genres = fields
    if imdb_id == "tconst":  # the header line
        return None
    kind = _RETAINED_TYPES.get(title_type)
    if kind is None or is_adult == "1":
        return None
    name = _optional(primary)
    if name is None:
        # A title with no primaryTitle cannot satisfy Title's
        # `name: str = Field(min_length=1)`, so it is dropped rather than
        # inserted with a placeholder that would then be searchable.
        return None
    return ImdbTitle(
        imdb_id=imdb_id,
        kind=kind,
        name=name,
        original_name=_optional(original),
        year=_optional_int(start, imdb_id=imdb_id, column="startYear"),
        end_year=_optional_int(end, imdb_id=imdb_id, column="endYear"),
        runtime_minutes=_optional_int(runtime, imdb_id=imdb_id, column="runtimeMinutes"),
        # `genres` is a comma-separated list inside one tab-delimited field.
        genres=tuple(g for g in (_optional(genres) or "").split(",") if g),
    )


def parse_ratings_row(line: str) -> ImdbRating | None:
    """One `title.ratings.tsv.gz` line, or `None` for the header.

    `averageRating` is already on IMDb's 0-10 scale, which is the scale
    `Title.community_rating` promises (`Field(ge=0, le=10)`), so nothing is
    rescaled. A value outside that range is malformed rather than clamped --
    the matching CHECK constraint would reject it during `COPY` anyway, and
    failing here names the offending row.
    """
    fields = line.split("\t")
    if len(fields) != _RATINGS_COLUMNS:
        raise PortDataMalformed(
            f"IMDb title.ratings row has {len(fields)} columns, expected {_RATINGS_COLUMNS}",
            detail=fields[0] if fields else "<empty line>",
        )
    imdb_id, average, votes = fields
    if imdb_id == "tconst":
        return None
    try:
        rating = float(average)
    except ValueError as exc:
        raise PortDataMalformed(
            "IMDb title.ratings row has a non-numeric averageRating", detail=imdb_id
        ) from exc
    if not 0.0 <= rating <= 10.0:
        raise PortDataMalformed(
            f"IMDb averageRating {rating} is outside the 0-10 scale Title.community_rating "
            "declares",
            detail=imdb_id,
        )
    count = _optional_int(votes, imdb_id=imdb_id, column="numVotes")
    return ImdbRating(imdb_id=imdb_id, community_rating=rating, vote_count=count or 0)


class _ImdbDataset[RowT](BulkDataset[RowT]):
    """Shared streaming/batching machinery for both IMDb files.

    Subclasses supply a filename, a name, and a row parser. Everything about
    resumption, batching, and cursor arithmetic lives here once.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        *,
        batch_size: int,
        base_url: str = IMDB_BASE_URL,
    ) -> None:
        self._file = CachedDatasetFile(client, base_url + self.filename, cache_dir)
        self._batch_size = batch_size

    @property
    @abstractmethod
    def filename(self) -> str:
        """The dataset file's name under `IMDB_BASE_URL`."""

    @abstractmethod
    def parse(self, line: str) -> RowT | None:
        """Parse one line, or return None for a header or filtered row."""

    @property
    def attribution(self) -> str:
        return IMDB_ATTRIBUTION

    async def revision(self) -> str:
        return await self._file.revision()

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[RowT]]:
        return self._batches(resume_from, revision)

    async def _batches(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[RowT]]:
        # `revision`, when given, is the value the caller's own prior call to
        # `revision()` already resolved this run -- for IMDb the dataset-level
        # revision *is* the underlying file's ETag (unlike TMDb, which has a
        # separate date-shaped checkpoint revision), so it can be used
        # directly with no HEAD at all, not just to skip a re-scan.
        resolved = revision if revision is not None else await self._file.revision()
        # A stored cursor from a different upstream snapshot is discarded, not
        # trusted: line N of yesterday's dump is not line N of today's. Every
        # write downstream is an upsert, so restarting is slow, not wrong.
        usable = resume_from if resume_from and resume_from.revision == resolved else None
        skip = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0
        await self._file.ensure_local(resolved)

        batch: list[RowT] = []
        position = skip
        for line in self._file.lines(skip=skip):
            # position counts *lines consumed*, not rows kept, because that is
            # what `skip` replays against. Incremented before the filter so a
            # resume never re-reads a line it already decided to drop.
            position += 1
            parsed = self.parse(line)
            if parsed is None:
                continue
            batch.append(parsed)
            if len(batch) >= self._batch_size:
                rows_seen += len(batch)
                yield BulkBatch(
                    rows=tuple(batch),
                    cursor=BulkCursor(revision=resolved, position=position, rows_seen=rows_seen),
                )
                batch = []
        if batch:
            rows_seen += len(batch)
            yield BulkBatch(
                rows=tuple(batch),
                cursor=BulkCursor(revision=resolved, position=position, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        # The httpx client is owned by whoever constructed it (the CLI's
        # composition root), which also closes it -- closing a shared client
        # from here would break the sibling dataset using the same one.
        return None

    def local_lines(self, *, skip: int = 0) -> Iterator[str]:
        """Escape hatch for tests and diagnostics: iterate the cached file
        with no HTTP at all."""
        return self._file.lines(skip=skip)


class IMDbTitleDataset(_ImdbDataset[ImdbTitle]):
    @property
    def filename(self) -> str:
        return "title.basics.tsv.gz"

    @property
    def name(self) -> str:
        return "imdb.title.basics"

    def parse(self, line: str) -> ImdbTitle | None:
        return parse_basics_row(line)


class IMDbRatingDataset(_ImdbDataset[ImdbRating]):
    @property
    def filename(self) -> str:
        return "title.ratings.tsv.gz"

    @property
    def name(self) -> str:
        return "imdb.title.ratings"

    def parse(self, line: str) -> ImdbRating | None:
        return parse_ratings_row(line)
