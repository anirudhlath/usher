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

**`title.principals` and `name.basics` ARE parsed here as of M9 T6, and
what was refused was an entity design rather than the files.** M9 T3 loaded
them against a real 1,271,138-title catalog and measured the `people` +
`credits` design at **2,701,697,024 B (2.702 GB) against a 2.0 GB ceiling**
-- 2.395 GB even stripped to five columns and three indexes -- and found
two further defects that no amount of column-trimming repairs: `credits`'
only unique key is `tmdb_credit_id`, NULL on every IMDb row, so an IMDb load
**cannot be deduplicated at all** (`(title_id, person_id, kind)` cannot be
UNIQUE -- 1,341,798 collisions), and TMDb's credits carry no `nconst`, so
people cannot be merged across the two sources on an id. So **no `people`
and no `credits` row is bulk-loaded from IMDb at all**
(`.claude/rules/bootstrap-and-datasets.md`), and what the two parsers feed
instead is `IMDbCreditNamesDataset` -> `titles.credit_names`, a `text[]`
that already exists and that weight class B of `search_document` already
indexes. `title.crew` and `title.episode` keep the status this paragraph
originally described: no table, nowhere to put them. See PRD 04's Phase 0
note.

Measured 2026-07-30: `title.basics.tsv.gz` is 214.4 MiB and
`title.ratings.tsv.gz` is 8.2 MiB, so M2's bootstrap downloads ~223 MiB, not
PRD 04's 1.83 GiB (which is the total across all seven IMDb files).
`title.akas.tsv.gz` adds **510,168,971 B (486.5 MiB)**, measured at the
pinned snapshot `"19810e3eb2b0f1fa774bf4e4af94d7c6-61"` on 2026-08-11 --
more than double what the two shipped files cost together. Nothing in
`usher.cli` constructs `IMDbAkaDataset` yet, so no operator pays that today.
"""

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
# Both taken from the real headers at the same pinned pass -- `nconst
# primaryName birthYear deathYear primaryProfession knownForTitles` and
# `tconst ordering nconst category job characters`. Measured over all
# 15,563,615 and all 101,151,422 data rows respectively: zero rows split to
# any other count, so a wrong count is a real signal rather than noise.
_NAMES_COLUMNS = 6
_PRINCIPALS_COLUMNS = 6

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
    r"""One `name.basics.tsv.gz` line, or `None` if the row is filtered out.

    **Two of six columns survive, and the other four have nowhere to go.**
    See `ImdbName` for why: T3 refused the `people` entity design at 2.702 GB
    against a 2.0 GB ceiling, so a birth year has no column and this parser
    feeds a `text[]` of names rather than a table of people.

    **Filtered (returns `None`), and only these two:**

    1. The header line.
    2. A row with no `primaryName` -- `\N` or empty. **89 of 15,563,615** at
       the pin. The empty string is not stored in its place: it would be a
       *searchable* empty lexeme in weight class B on every title that person
       is credited on, which is the argument `parse_akas_row` makes against
       `ck_title_search_names_name_not_empty` one function up, arriving here
       at a column that has no such CHECK to be caught by.

    **Nothing is filtered on length, and that is a decision rather than an
    omission.** `parse_akas_row` bounds a name at `AKAS_NAME_MAX_CHARS`
    because `ck_title_search_names_name_within_btree_bound` really is a CHECK
    that would refuse the whole batch; `titles.credit_names` is an unbounded
    `text[]` with no such constraint, so a bound here would be a number this
    module invented. Measured: the longest `primaryName` in the file is
    **105** characters and none exceeds 512, so the two policies agree on
    every row that exists -- they differ only in what they would do about a
    row that does not.

    **Malformed (raises `PortDataMalformed`):** a wrong column count, or an
    `nconst` that is not `nm` + digits.
    """
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
    r"""One `title.principals.tsv.gz` line, or `None` for the header.

    **Only the header is filtered, and no row is dropped on its `category`.**
    The 13 values and their counts at the pin: `actor` 23,895,326, `actress`
    18,048,050, `self` 15,336,475, `writer` 12,046,352, `director` 8,562,157,
    `producer` 7,478,006, `editor` 5,494,276, `cinematographer` 4,069,293,
    `composer` 3,216,556, `production_designer` 1,184,416,
    `casting_director` 1,159,130, `archive_footage` 647,603, `archive_sound`
    13,782.

    A retain-list would silently drop whichever category IMDb adds next --
    the same argument `parse_akas_row` makes about `types` -- and there is
    nothing here to spend a filter on: IMDb has already applied its own
    editorial selection, capping the file at a **mean of 8.8 rows per title**
    (max 75), which is the same order as the top-ten-billed-plus-crew
    projection `services/derive._credit_names` builds from TMDb. Dropping
    `archive_footage` and `archive_sound`, the only two that read like noise,
    would remove 0.65% of the rows and cost a name on a documentary.

    `category`, `job` and `characters` are therefore read for the column
    count and discarded, exactly as `parse_akas_row` reads and discards
    `types` and `attributes`.

    **Malformed (raises `PortDataMalformed`):** a wrong column count, an
    `ordering` that is absent or non-integral, or an `nconst` that is not
    `nm` + digits.
    """
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
    """`nconst` -> `primaryName` for the whole of `name.basics`, in 345 MiB.

    **This structure is where the refusal of the `people` table is actually
    paid for.** A credit name is a join between two files of 101,151,422 and
    15,563,615 rows; with a `people` table the right-hand side would live in
    Postgres and the join would be one `INSERT ... SELECT`. Without one it
    has to be resolved before the rows cross the port, and the only place
    that can happen is in this process.

    **Direct addressing, not a `dict` and not a sorted array, and both
    alternatives were rejected on a measurement:**

    - A `dict[str, str]` over 15.5M entries costs roughly a gigabyte -- the
      per-entry table overhead alone is ~100 B before either object.
    - A sorted `array` plus `bisect` is *wrong*, not merely slower.
      `name.basics` is sorted **lexicographically by the `nconst` string**,
      which is not the integer order: an eight-digit id beginning with the
      same seven characters as a seven-digit one sorts *before* it, because
      string comparison never reaches the length. Measured, **738,680
      descents** in the integer sequence at the pinned snapshot. A bisect
      over unsorted keys answers `None` for millions of real people and each
      one is a title quietly losing a name, which is exactly the failure
      shape nothing downstream can detect. (Same family as the migration-id
      padding trap in `.claude/rules/db-and-sql.md`: an identifier minted by
      counting and compared as a string sorts wrong at the first extra
      digit.)

    So: an address table addressed by the integer inside the `nconst`, one
    `array("i")` of offsets, and one `bytearray` holding every name end to
    end. Measured over the pinned file (2026-08-11): **211,630,156 B of name
    text, 87,819,488 B of address table, 62,254,108 B of offsets =
    361,703,752 B**, built in **19.5 s** at a peak RSS of **361.3 MB**. The
    address table is bounded by the largest `nconst` (21,954,871), not by the
    number of people, and it is 43% empty at that pin -- 24% of the total, and
    the price of O(1) lookups over an id space with holes in it.

    **The table is chunked, and that is not a micro-optimisation -- a single
    flat array is a real hazard this repository can reach.** Its size is set
    by the largest id, so one outlying `nconst` sizes the whole thing: the
    reserved synthetic band this project's own fixtures must use
    (`nm99\\d{6}`, per `tests/unit/test_no_third_party_data.py`) starts at
    99,000,000, so a two-person test index allocated **396 MB** and every
    case in the file paid for it. 65,536-entry chunks make the cost
    proportional to the *occupied* id space instead: unchanged at 335 chunks
    for the real file, two chunks for a fixture.

    Not thread-safe and not intended to be: it is built once per import phase
    by the one coroutine that then streams `title.principals` against it.
    """

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
        """The id whose rows must reach one writer call together, or `None`
        when any batch boundary is safe.

        **`None` is the answer for a writer that is row-scoped**, which is
        what `upsert_titles` and `apply_ratings` are: each row is upserted on
        its own key, so splitting a file anywhere costs at most a replayed
        upsert. `IMDbAkaDataset` answers the `imdb_id`, because
        `BulkCatalogRepository.replace_aliases` is a *scoped delete* followed
        by an insert -- two calls naming one title replace each other's rows,
        and the port's own docstring says a caller batching a line-oriented
        dump has to close a title's run before it closes a batch.

        **The loss a non-`None` answer prevents is silent.** The port's
        `ValueError` guard fires for a row outside the scope, and both halves
        of a split title are inside their own call's scope, so nothing raises
        and nothing counts it. Measured over the whole pinned
        `title.akas.tsv.gz` (`"19810e3eb2b0f1fa774bf4e4af94d7c6-61"`,
        58,906,368 data rows, 2026-08-11): at the shipped `bulk_batch_size` of
        50,000, **all 924 batch boundaries land inside a title** and **3,867
        retained rows** are deleted after being written.

        Grouping is only sound if the dump keeps a title's rows **contiguous**,
        and that is measured rather than assumed: the `titleId` column of
        `title.akas` is non-decreasing over all 58,906,368 rows (**zero**
        lexicographic descents, against 1,250,830 descents in the *integer*
        inside it -- these files are string-sorted, exactly as `name.basics`
        is), and so is `title.principals`' `tconst` over all 101,151,422.
        Non-decreasing implies contiguous, because a run reopening after some
        other id would have to descend to do it.

        **A guard refusing a file whose order descends was measured and
        declined.** It is one variable and no memory, but it checks a strictly
        stronger property than the writer needs: this repository's own
        `title.akas.slice.tsv` is contiguous and *not* sorted
        (`tt99000020`, `tt99000030`, `tt99000010`), so the guard would refuse
        a file that is fine. Contiguity itself cannot be checked without
        remembering 12.7M ids.
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

    **What this class does change in `_ImdbDataset` is where a batch may
    end.** `replace_aliases` is a scoped delete followed by an insert, so a
    title whose rows straddle a batch boundary has the first half deleted by
    the second half's call -- silently, since both halves are inside their own
    call's scope and the port's `ValueError` guard never fires. `group_of`
    answers the `imdb_id` and the batching closes a title's run first, the
    same rule `IMDbCreditNamesDataset` already keeps for `title.principals`.
    """

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
    """`name.basics` x `title.principals` -> one ordered name list per title.

    **The one dataset in this module that reads two files, and it reads two
    because the join has nowhere else to happen.** Every other
    `BulkDataset` here maps one line to one record. A credit *name* is a join
    -- `title.principals` knows which `nconst` is on which `tconst`, and only
    `name.basics` knows what an `nconst` is called -- and M9's T3 refused the
    `people` table that would have let Postgres do it (2.702 GB against a
    2.0 GB ceiling). So the right-hand side is materialised in this process,
    as `ImdbNameIndex`, and what crosses the port is already resolved.

    **Why this is not two `BulkDataset`s.** Two datasets would need two
    `import_runs` rows, two revisions and an ordering constraint between them
    -- and the second would still have to rebuild the first's index from the
    file, because nothing persists it. One dataset with one composite
    revision states the real dependency instead: these two files are read
    together or not at all.

    **The composite revision is the answer to "the seven files are not one
    snapshot".** At the T3 pin, `name.basics` was regenerated 2026-08-10
    12:53:46 GMT and `title.principals` 2026-08-11 00:48:34 GMT, and
    **3,734 distinct `nconst` values over 7,701 rows** of the latter are in
    no row of the former. A single-file revision would call two genuinely
    different pairs one snapshot and replay a stored cursor across them.

    **What a resume costs, stated because it is not free.** `position` is a
    line offset into `title.principals` only; the index is rebuilt from the
    whole of `name.basics` on every run, resumed or not, at a measured
    **19.5 s and 361.3 MB**. That is the fixed cost of the first batch, and
    it is paid again after a crash.

    Scale, since this is by far the largest file this project reads:
    `BulkCursor.position` stays a plain integer line number bounded by
    101,151,423, which round-trips through `ImportRun.position`'s `Integer`
    with an order of magnitude to spare.
    """

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
        # The line count through the end of the last *completed* title. A
        # title's boundary is only visible once the first line of the next one
        # has been consumed, so `position` is always at or past it and a
        # cursor built from `position` would resume mid-title -- writing a
        # partial name list over a complete one, silently, because the write
        # is a set rather than an append.
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
