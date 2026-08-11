"""IMDb non-commercial datasets -> `ImdbTitle` / `ImdbRating` / `ImdbAka`.

Five TSV quirks, and exactly how each is handled:

1. **`\\N` means NULL.** Not an empty string, not a literal backslash-N in the
   data. `_optional` maps it to `None`; every numeric field goes through it
   before `int()`/`float()`.
2. **There is no quoting mechanism.** IMDb's TSVs are raw tab-separated
   values, and a title field may both open and close with a literal `"`
   character (21 such titles in the first 553,395 rows of
   `title.basics.tsv.gz` -- measured directly; `CLAUDE.md` names the
   specimen, which is not repeated here because nothing shipped in this
   package quotes a real row). `csv.reader` with its default
   `QUOTE_MINIMAL` **silently strips both quotes** off such a field --
   verified directly, and pinned by
   `tests/unit/test_adapters_bulk_imdb.py::test_preserves_embedded_double_quotes`
   against an invented title of the same shape. This
   module therefore uses `line.split("\\t")` and never the `csv` module.
   `csv.reader(..., quoting=csv.QUOTE_NONE)` also preserves them, but a plain
   split has nothing to misconfigure.
3. **gzip.** Handled one layer down, in `CachedDatasetFile.lines`.
4. **`isAdult` is `0`/`1`, and `titleType` needs filtering.** Adult titles are
   dropped outright (PRD 04). Only the four `titleType` values that map onto
   `TitleKind` survive; see `_RETAINED_TYPES`.
5. **A tab-delimited column may itself be multi-valued, and the inner
   separator is `\\x02`.** `title.basics`' `genres` uses a comma;
   `title.akas`' `types` and `attributes` use the ASCII STX character.
   Measured on the pinned `title.akas.tsv.gz`: **429 of 58,906,368 rows carry
   a two-valued `types`** (`imdbDisplay\\x02dvd` is the commonest, 207 rows).
   Nothing here parses either column, but a reader that assumed the
   separator was a tab would call those 429 rows nine-column and malformed,
   which is why `title.akas.slice.tsv` carries one.

**`title.akas` IS imported here as of M9, and this paragraph used to say the
opposite.** It read *"there is literally nowhere to put those rows"*, and
that was true until `m09a` created `title_search_names` with a `region` and
a `language` column. `parse_akas_row` is what changed it.

`title.principals`, `name.basics`, `title.crew` and `title.episode` are
still **not** imported here, and the first two are refused on a measurement
rather than on a missing table. M9 T3 loaded them against a real
1,271,138-title catalog and measured the `people` + `credits` design at
**2,701,697,024 B (2.702 GB) against a 2.0 GB ceiling** -- 2.395 GB even
stripped to five columns and three indexes -- so the entity design was
refused and **no `people` or `credits` row is bulk-loaded from IMDb at all**
(`.claude/rules/bootstrap-and-datasets.md`). `title.crew` and
`title.episode` keep the status this paragraph originally described: no
table, nowhere to put them. See PRD 04's Phase 0 note.

Measured 2026-07-30: `title.basics.tsv.gz` is 214.4 MiB and
`title.ratings.tsv.gz` is 8.2 MiB, so M2's bootstrap downloads ~223 MiB, not
PRD 04's 1.83 GiB (which is the total across all seven IMDb files).
`title.akas.tsv.gz` adds **510,168,971 B (486.5 MiB)**, measured at the
pinned snapshot `"19810e3eb2b0f1fa774bf4e4af94d7c6-61"` on 2026-08-11 --
more than double what the two shipped files cost together. Nothing in
`usher.cli` constructs `IMDbAkaDataset` yet, so no operator pays that today.
"""

from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx

from usher.adapters.bulk.download import CachedDatasetFile
from usher.db.models.search import SEARCH_NAME_MAX_CHARS
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbAka, ImdbRating, ImdbTitle
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
# Taken from the real header at the pinned snapshot
# `"19810e3eb2b0f1fa774bf4e4af94d7c6-61"` (2026-08-11), never from IMDb's
# published schema: `titleId ordering title region language types attributes
# isOriginalTitle`. Measured over all 58,906,368 data rows of that file, zero
# split to any other count -- so a wrong count is a real signal here rather
# than noise to be tolerated. Re-confirmed on `title.principals` and
# `name.basics` in the same pass, which is why the same claim is made for all
# three in `.claude/rules/bootstrap-and-datasets.md`.
_AKAS_COLUMNS = 8

# The btree bound `ck_title_search_names_name_within_btree_bound` enforces,
# imported rather than re-spelled. Two copies of a number that must agree is
# how they stop agreeing, and this one has to be *the same* 512 the CHECK
# carries or the filter below is decoration.
#
# `usher.adapters` importing `usher.db` is unusual and it is deliberate: the
# `db is driven, not driving` contract lists `usher.domain`, `usher.ports` and
# `usher.services` as its sources, so an adapter is outside it, and
# `adapters/search/postgres.py` already reaches into `usher.db` for
# `constraint_name`. The alternative -- widening `usher.ports.bulk` with a
# search-table bound that has nothing to do with bulk loading -- puts the
# number in a worse place to keep it in a tidier one.
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


def parse_akas_row(line: str) -> ImdbAka | None:
    r"""One `title.akas.tsv.gz` line, or `None` if the row is filtered out.

    **The retention policy, stated in full, with what each clause was measured
    to cost.** Every figure below is against the pinned snapshot
    `"19810e3eb2b0f1fa774bf4e4af94d7c6-61"` (58,906,368 data rows, 2026-08-11),
    joined where a join is needed to a 1,272,367-title catalog built from the
    `title.basics` this host had cached -- a *different* upstream snapshot,
    `"128751cb2f3132bd73bdf08c7f4def5d-27"`, because IMDb does not regenerate
    the seven files together and reporting them as one would be the error T3
    already recorded.

    **Retained:** every row whose `title` can actually be stored.

    **Filtered (returns `None`), and only these three:**

    1. **The header line.**
    2. **A row IMDb itself flags `isOriginalTitle = 1`.** That is IMDb's claim
       that the row *is* the title's original title, not an alias of it, and
       `SearchNameKind` has deliberately no `primary` member -- a canonical
       name is served by `ix_titles_name_lower_prefix` on `titles`, so storing
       one here is the one-row-per-title duplication M6's boundary call 3
       refused the table for. **12,703,704 of 58,906,368 rows (21.6%)** carry
       the flag, **not one of them carries a `region`** (0 of 12,703,704), and
       `types` reads exactly `original` on all of them. Against the catalog,
       1,272,135 of the 7,541,357 retained rows are flagged and **1,272,111
       (99.998%) casefold-equal the title's own `name` or `original_name`**;
       dropping every flagged row costs **7 aliases out of 1,663,330** after
       deduplication, because 17 of the 24 disagreeing rows repeat a name a
       non-flagged row already carries. The hazard this clause could have had
       -- a title whose `original_name` is NULL, leaving the flagged aka as
       the only carrier -- is empirically zero: **0 of the 1,272,367 catalog
       titles have no `originalTitle`.**
    3. **A row whose `title` cannot be stored**: empty or `\N` (0 of
       58,906,368, so unreachable in this snapshot, and here because
       `ck_title_search_names_name_not_empty` is `name <> ''` and a
       placeholder would be *searchable*), or longer than
       `AKAS_NAME_MAX_CHARS` (**33 rows, longest 831**, none of them in
       today's catalog). The length clause is not tidiness: the writer's
       contract refuses an over-long name for the **whole call**, so one such
       row would take a ten-thousand-row batch with it, and the catalog grows
       while the refusal stays per-call.

    **Nothing is filtered on `region`, `language`, `types` or `attributes`**,
    and that is a decision rather than an omission. Bar (B) passed **4.8x
    under on rows and 3.2x under on bytes**, so a recall-costing filter buys
    headroom nobody needs. `types` has 23 distinct values here and IMDb's own
    documentation says new ones may be added without warning, so a retain-list
    silently drops the next category and a drop-list silently admits it.
    `region` has 251 values whose seven largest are 5.4-5.8M rows each --
    there is no small set to keep. And `attributes` is 185 free-text values
    (`transliterated title`, `alternative spelling`, ...) with no vocabulary
    to filter against at all.

    **What this parser cannot do, and does not pretend to.** The rule that an
    alias equal to the title's own `name` or `original_name` is not an alias
    needs the stored `Title`, and a parser has no catalog. Clause 2 is a cheap
    prefix of it, never a substitute: of the 6,269,222 retained rows that
    survive clause 2, **4,426,783 (70.6%) still casefold-equal the title's own
    name** and only the writer can see that.

    **Malformed (raises `PortDataMalformed`):** a wrong column count, or an
    `ordering` that is absent or non-integral. The error carries the row id
    and the column, never the line -- an alias line runs to 831 characters.
    """
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


class IMDbAkaDataset(_ImdbDataset[ImdbAka]):
    """`title.akas.tsv.gz`, on the same machinery as the other two.

    **The plan's own risk about a heavily-filtered file yielding a row-less
    batch is inverted by the measurement, so nothing here changes
    `_ImdbDataset`.** That risk was written for three files at once; of the
    one that survives, `title.akas` keeps **78.4%** of its lines
    (58,906,368 read, 12,703,704 flagged rows dropped), which makes it by far
    the *least* filtered dataset this class has ever streamed --
    `title.basics` keeps 1,271,138 of 12,678,891, i.e. **10.0%**, and has
    shipped that way since M2. A trailing run of filtered lines costs a
    re-read on resume and never a lost row, because `position` counts lines
    consumed rather than rows kept and every write downstream is an upsert.

    Scale, since this is 4.6x `title.basics`' line count: `BulkCursor.
    position` stays a plain integer line number, bounded by 58,906,369 here,
    which round-trips through `ImportRun.position`'s `Integer` with three
    orders of magnitude to spare.
    """

    @property
    def filename(self) -> str:
        return "title.akas.tsv.gz"

    @property
    def name(self) -> str:
        return "imdb.title.akas"

    def parse(self, line: str) -> ImdbAka | None:
        return parse_akas_row(line)
