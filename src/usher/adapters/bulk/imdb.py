"""IMDb non-commercial datasets -> `ImdbTitle` / `ImdbRating` / `ImdbAka`."""

import array
from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx

from usher.adapters.bulk.download import CachedDatasetFile
from usher.db.models.search import SEARCH_NAME_MAX_CHARS
from usher.domain.enums import TitleKind
from usher.ports.bulk import (
    BulkBatch,
    BulkCursor,
    BulkDataset,
    ImdbAka,
    ImdbCreditNames,
    ImdbName,
    ImdbPrincipal,
    ImdbRating,
    ImdbTitle,
)
from usher.ports.errors import PortDataMalformed

IMDB_BASE_URL = "https://datasets.imdbws.com/"

# The exact attribution string IMDb's non-commercial licence requires.
IMDB_ATTRIBUTION = "Information courtesy of IMDb (https://www.imdb.com). Used with permission."

# Retained titleTypes, mapped onto TitleKind.
_RETAINED_TYPES: dict[str, TitleKind] = {
    "movie": TitleKind.MOVIE,
    "tvMovie": TitleKind.MOVIE,
    "tvSeries": TitleKind.SERIES,
    "tvMiniSeries": TitleKind.SERIES,
}

_BASICS_COLUMNS = 9
_RATINGS_COLUMNS = 3
# Taken from the real header at the pinned snapshot
# `"19810e3eb2b0f1fa774bf4e4af94d7c6-61"` (2026-08-11), never from IMDb's published
# schema: `titleId ordering title region language types attributes isOriginalTitle`.
_AKAS_COLUMNS = 8
# Both taken from the real headers at the same pinned pass -- `nconst
# primaryName birthYear deathYear primaryProfession knownForTitles` and
# `tconst ordering nconst category job characters`. Measured over all
# 15,563,615 and all 101,151,422 data rows respectively: zero rows split to
# any other count, so a wrong count is a real signal rather than noise.
_NAMES_COLUMNS = 6
_PRINCIPALS_COLUMNS = 6

# The btree bound `ck_title_search_names_name_within_btree_bound` enforces, imported
# rather than re-spelled.
AKAS_NAME_MAX_CHARS = SEARCH_NAME_MAX_CHARS


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


def _required_int(value: str, *, imdb_id: str, column: str) -> int:
    r"""`_optional_int`, for a column whose absence is itself a format change.

    Same `\N`-then-`int()` path, so a numeric column that stopped being
    numeric is still a hard failure naming the row and the column -- and a
    `\N` where the dump has never had one is the same kind of news. Used for
    `title.akas`' `ordering`, which is present and integral on all 58,906,368
    rows of the pinned snapshot (min 1, max 300) and is the only per-title
    tiebreak a deduplicating writer has.
    """
    number = _optional_int(value, imdb_id=imdb_id, column=column)
    if number is None:
        raise PortDataMalformed(
            f"IMDb row has no {column}, which is required", detail=f"{imdb_id}.{column}"
        )
    return number


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
    every rating field on `Title` promises (`Field(ge=0, le=10)`, on
    `tmdb_vote_average` and `imdb_average_rating` alike -- ADR-0040 split the
    column and did not move the bound, because both sources use 0-10, which is
    exactly why the dual write was silent), so nothing is rescaled. A value
    outside that range is malformed rather than clamped --
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
            f"IMDb averageRating {rating} is outside the 0-10 scale every "
            "Title rating field declares",
            detail=imdb_id,
        )
    count = _optional_int(votes, imdb_id=imdb_id, column="numVotes")
    return ImdbRating(imdb_id=imdb_id, average_rating=rating, num_votes=count or 0)


def parse_akas_row(line: str) -> ImdbAka | None:
    """One `title.akas.tsv.gz` line, or `None` if the row is filtered out."""
    fields = line.split("\t")
    if len(fields) != _AKAS_COLUMNS:
        raise PortDataMalformed(
            f"IMDb title.akas row has {len(fields)} columns, expected {_AKAS_COLUMNS}",
            detail=fields[0] if fields else "<empty line>",
        )
    imdb_id, ordering, title, region, language, _types, _attributes, is_original = fields
    if imdb_id == "titleId":  # the header line
        return None
    if is_original == "1":
        # Spelled the way `parse_basics_row` spells `isAdult == "1"`, and for
        # the same reason: the measured vocabulary of this column is exactly
        # `0` and `1` with no `\N` over all 58,906,368 rows, and the flag is
        # advisory -- the writer's casefold comparison against the stored
        # title is the filter that has to be right.
        return None
    name = _optional(title)
    if name is None or len(name) > AKAS_NAME_MAX_CHARS:
        return None
    return ImdbAka(
        imdb_id=imdb_id,
        ordering=_required_int(ordering, imdb_id=imdb_id, column="ordering"),
        name=name,
        region=_optional(region),
        language=_optional(language),
    )


def _person_key(nconst: str, *, imdb_id: str) -> int:
    """The integer inside an `nconst`, which is what the name index addresses.

    Refuses anything that is not `nm` + digits with `PortDataMalformed`
    rather than skipping it. Measured over all 15,563,615 rows of the pinned
    `name.basics.tsv.gz` and all 101,151,422 of `title.principals.tsv.gz`:
    **zero** ids of any other shape, so one arriving is an upstream format
    change and not a row to route around.
    """
    if not nconst.startswith("nm") or not nconst[2:].isdigit():
        raise PortDataMalformed(
            "IMDb row has a person id that is not an nconst", detail=f"{nconst}.nconst"
        )
    return int(nconst[2:])


def parse_names_row(line: str) -> ImdbName | None:
    """One `name.basics.tsv.gz` line, or `None` if the row is filtered out."""
    fields = line.split("\t")
    if len(fields) != _NAMES_COLUMNS:
        raise PortDataMalformed(
            f"IMDb name.basics row has {len(fields)} columns, expected {_NAMES_COLUMNS}",
            detail=fields[0] if fields else "<empty line>",
        )
    nconst, primary = fields[0], fields[1]
    if nconst == "nconst":  # the header line
        return None
    _person_key(nconst, imdb_id=nconst)
    name = _optional(primary)
    if name is None:
        return None
    return ImdbName(imdb_id=nconst, name=name)


def parse_principals_row(line: str) -> ImdbPrincipal | None:
    """One `title.principals.tsv.gz` line, or `None` for the header."""
    fields = line.split("\t")
    if len(fields) != _PRINCIPALS_COLUMNS:
        raise PortDataMalformed(
            f"IMDb title.principals row has {len(fields)} columns, expected {_PRINCIPALS_COLUMNS}",
            detail=fields[0] if fields else "<empty line>",
        )
    imdb_id, ordering, nconst, _category, _job, _characters = fields
    if imdb_id == "tconst":  # the header line
        return None
    _person_key(nconst, imdb_id=imdb_id)
    return ImdbPrincipal(
        imdb_id=imdb_id,
        ordering=_required_int(ordering, imdb_id=imdb_id, column="ordering"),
        person_imdb_id=nconst,
    )


class ImdbNameIndex:
    """`nconst` -> `primaryName` for the whole of `name.basics`, in 345 MiB."""

    __slots__ = ("_blob", "_chunks", "_offsets")

    #: The address table's sentinel for "no name.basics row addresses this
    #: id". -1 rather than 0, because 0 is a legitimate row index.
    _MISSING = -1

    #: 65,536 `int32` slots = 256 KB per chunk. Large enough that the real,
    #: dense id space costs the same as one flat array (335 chunks, all but
    #: the last fully used); small enough that a lone id nine orders of
    #: magnitude away costs 256 KB rather than its own address space.
    _CHUNK_BITS = 16

    def __init__(self) -> None:
        self._chunks: dict[int, array.array[int]] = {}
        self._offsets = array.array("i", [0])
        self._blob = bytearray()

    def add(self, row: ImdbName) -> None:
        """Store one parsed `name.basics` row.

        A second row for an `nconst` already held overwrites it. Measured:
        **0 duplicate `nconst` values** in 15,563,615 rows, so the rule is
        stated rather than exercised, and last-write-wins is chosen only
        because it costs nothing to spell.
        """
        chunk, slot = divmod(_person_key(row.imdb_id, imdb_id=row.imdb_id), 1 << self._CHUNK_BITS)
        table = self._chunks.get(chunk)
        if table is None:
            # Allocated whole on first touch rather than grown: the largest
            # `nconst` is not knowable without reading the file twice, and a
            # chunk is 256 KB either way.
            table = array.array("i", [self._MISSING]) * (1 << self._CHUNK_BITS)
            self._chunks[chunk] = table
        table[slot] = len(self._offsets) - 1
        self._blob += row.name.encode("utf-8")
        self._offsets.append(len(self._blob))

    def get(self, nconst: str) -> str | None:
        """The stored name, or `None` if no `name.basics` row holds this id.

        `None` is routine rather than exceptional: the seven IMDb dumps are
        not one snapshot, and **3,734 distinct `nconst` values over 7,701
        rows** of the pinned `title.principals` are in no `name.basics` row at
        all.
        """
        chunk, slot = divmod(_person_key(nconst, imdb_id=nconst), 1 << self._CHUNK_BITS)
        table = self._chunks.get(chunk)
        if table is None:
            return None
        index = table[slot]
        if index == self._MISSING:
            return None
        return self._blob[self._offsets[index] : self._offsets[index + 1]].decode("utf-8")

    def __len__(self) -> int:
        return len(self._offsets) - 1

    @property
    def nbytes(self) -> int:
        """What this index costs, so an importer can report it.

        The buffers only -- Python's own per-object overhead is a handful of
        headers and a small dict, and is not worth modelling. Measured
        against peak RSS on the real file: 361,703,752 B reported against
        361.3 MB observed.
        """
        return (
            len(self._blob)
            + self._offsets.itemsize * len(self._offsets)
            + sum(table.itemsize * len(table) for table in self._chunks.values())
        )


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

    def group_of(self, row: RowT) -> str | None:
        """The id whose rows must reach one writer call together, or `None` when any batch
        boundary is safe.
        """
        return None

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
        # Lines consumed through the last point a batch may safely end at. For
        # an ungrouped dataset that is every kept row, so this tracks
        # `position` and the arithmetic below is what it always was; for a
        # grouped one it lags until the open group closes, which is only
        # visible once the *next* group's first line has been consumed.
        boundary = skip
        group: str | None = None
        for line in self._file.lines(skip=skip):
            # position counts *lines consumed*, not rows kept, because that is
            # what `skip` replays against. Incremented before the filter so a
            # resume never re-reads a line it already decided to drop.
            position += 1
            parsed = self.parse(line)
            if parsed is None:
                # A filtered line belongs to no group, so it can only extend
                # the boundary while none is open -- inside an open group it
                # would move the cursor past rows not yet handed to a writer.
                if group is None:
                    boundary = position
                continue
            key = self.group_of(parsed)
            if key is None:
                batch.append(parsed)
                boundary = position
                if len(batch) >= self._batch_size:
                    rows_seen += len(batch)
                    yield BulkBatch(
                        rows=tuple(batch),
                        cursor=BulkCursor(
                            revision=resolved, position=boundary, rows_seen=rows_seen
                        ),
                    )
                    batch = []
                continue
            if key != group:
                if group is not None:
                    boundary = position - 1
                    # Checked here rather than after the append: a full batch
                    # is only allowed out at a group boundary, so a group
                    # larger than `batch_size` overshoots it rather than
                    # splitting. `title.akas`' largest run is 300 rows and
                    # `title.principals`' is 75, against a 50,000 default.
                    if len(batch) >= self._batch_size:
                        rows_seen += len(batch)
                        yield BulkBatch(
                            rows=tuple(batch),
                            cursor=BulkCursor(
                                revision=resolved, position=boundary, rows_seen=rows_seen
                            ),
                        )
                        batch = []
                group = key
            batch.append(parsed)
        # End of file closes whatever group was open, so the boundary is every
        # line consumed -- including a trailing run of filtered ones, which is
        # what keeps a resume from re-reading them.
        boundary = position
        if batch:
            rows_seen += len(batch)
            yield BulkBatch(
                rows=tuple(batch),
                cursor=BulkCursor(revision=resolved, position=boundary, rows_seen=rows_seen),
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


class IMDbAkaDataset(_ImdbDataset[ImdbAka]):
    """`title.akas.tsv.gz`, on the same machinery as the other two."""

    @property
    def filename(self) -> str:
        return "title.akas.tsv.gz"

    @property
    def name(self) -> str:
        return "imdb.title.akas"

    def parse(self, line: str) -> ImdbAka | None:
        return parse_akas_row(line)

    def group_of(self, row: ImdbAka) -> str | None:
        return row.imdb_id


# `name.basics=<etag>;title.principals=<etag>`. Spelled out rather than
# hashed or concatenated bare, because this string is what
# `usher bootstrap-status` prints out of `import_runs.revision` and an
# operator reading it needs to see *which* file moved. The column is `Text`
# with only a `<> ''` CHECK, so length is free.
_COMPOSITE_REVISION = "name.basics={names};title.principals={principals}"


class IMDbCreditNamesDataset(BulkDataset[ImdbCreditNames]):
    """`name.basics` x `title.principals` -> one ordered name list per title."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        *,
        batch_size: int,
        base_url: str = IMDB_BASE_URL,
    ) -> None:
        self._names = CachedDatasetFile(client, base_url + "name.basics.tsv.gz", cache_dir)
        self._principals = CachedDatasetFile(
            client, base_url + "title.principals.tsv.gz", cache_dir
        )
        self._batch_size = batch_size

    @property
    def name(self) -> str:
        return "imdb.credit_names"

    @property
    def attribution(self) -> str:
        return IMDB_ATTRIBUTION

    async def revision(self) -> str:
        return _COMPOSITE_REVISION.format(
            names=await self._names.revision(), principals=await self._principals.revision()
        )

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[ImdbCreditNames]]:
        return self._batches(resume_from, revision)

    async def _batches(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[ImdbCreditNames]]:
        # Both component revisions are resolved even when `revision` was
        # supplied, because `ensure_local` needs each file's own ETag and the
        # composite cannot be taken apart safely -- an ETag may itself contain
        # the separator. Two HEADs against a cached file, once per run.
        names_revision = await self._names.revision()
        principals_revision = await self._principals.revision()
        resolved = revision or _COMPOSITE_REVISION.format(
            names=names_revision, principals=principals_revision
        )
        usable = resume_from if resume_from and resume_from.revision == resolved else None
        skip = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0

        await self._names.ensure_local(names_revision)
        await self._principals.ensure_local(principals_revision)
        index = ImdbNameIndex()
        for line in self._names.lines():
            person = parse_names_row(line)
            if person is not None:
                index.add(person)

        batch: list[ImdbCreditNames] = []
        position = skip
        # The line count through the end of the last *completed* title.
        boundary = skip
        title: str | None = None
        principals: list[ImdbPrincipal] = []

        for line in self._principals.lines(skip=skip):
            position += 1
            principal = parse_principals_row(line)
            if principal is None:
                # The header, and nothing else -- no principals row is
                # filtered. It belongs to no title, so it can only extend the
                # boundary while no title is open.
                if title is None:
                    boundary = position
                continue
            if principal.imdb_id != title:
                if title is not None:
                    row = _credit_names(title, principals, index)
                    if row is not None:
                        batch.append(row)
                    boundary = position - 1
                    if len(batch) >= self._batch_size:
                        rows_seen += len(batch)
                        yield BulkBatch(
                            rows=tuple(batch),
                            cursor=BulkCursor(
                                revision=resolved, position=boundary, rows_seen=rows_seen
                            ),
                        )
                        batch = []
                title, principals = principal.imdb_id, []
            principals.append(principal)

        if title is not None:
            row = _credit_names(title, principals, index)
            if row is not None:
                batch.append(row)
            boundary = position
        if batch:
            rows_seen += len(batch)
            yield BulkBatch(
                rows=tuple(batch),
                cursor=BulkCursor(revision=resolved, position=boundary, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        # The httpx client is owned by whoever constructed it (the CLI's
        # composition root), which also closes it.
        return None


def _credit_names(
    imdb_id: str, principals: list[ImdbPrincipal], index: ImdbNameIndex
) -> ImdbCreditNames | None:
    """One title's principals, resolved to names -- or `None` if none resolve.

    Three rules, each measured against the pinned dump:

    - **Sorted by `ordering`.** The order *is* the ranking, and it is what
      weight class B indexes first. The real file already ascends within
      every one of its 11,491,032 titles, so the sort is unobservable against
      production data -- which is exactly why the fixture is deliberately
      disordered and the case asserts that premise.
    - **Deduplicated, keeping first position.** **9,404,442 of 101,151,422
      rows** repeat a person already credited on the same title (a director
      who also wrote it). Repeating the name inflates its term frequency in
      the tsvector for no reason a searcher would recognise -- the same
      argument `services/derive._credit_names` makes on the TMDb side.
    - **`None`, never an empty tuple.** 156 titles in the pinned dump have
      every principal dangling. An empty tuple would reach the writer and
      *blank* whatever `credit_names` another source had filled, which is the
      one shape a re-import cannot repair.
    """
    names: list[str] = []
    for principal in sorted(principals, key=lambda one: one.ordering):
        name = index.get(principal.person_imdb_id)
        if name is not None:
            names.append(name)
    if not names:
        return None
    return ImdbCreditNames(imdb_id=imdb_id, names=tuple(dict.fromkeys(names)))
