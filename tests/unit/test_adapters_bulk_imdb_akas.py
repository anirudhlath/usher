"""IMDb `title.akas` parsing and batching, over a committed synthetic slice.

No network, no Docker, no real dataset file. Sibling of
`test_adapters_bulk_imdb.py`, which covers `title.basics`/`title.ratings`;
this file exists separately because the akas parser has a retention policy
those two do not, and every clause of that policy is measured rather than
argued (see `.claude/rules/bootstrap-and-datasets.md`).

**The file is named for `title.akas` and not for `name.basics` /
`title.principals`.** The M9 plan's T5 named this file
`test_adapters_bulk_imdb_people.py` and asked for three parsers; T3 measured
the people+credits design at 2.702 GB against a 2.0 GB ceiling and it was
refused, so the two people-side parsers are withdrawn and only the akas one
survives. A test file named for the two datasets nobody parses would be the
plan drifting into the tree.
"""

import ast
import gzip
import inspect
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk import imdb
from usher.adapters.bulk.imdb import AKAS_NAME_MAX_CHARS, IMDbAkaDataset, parse_akas_row
from usher.db.models.search import SEARCH_NAME_MAX_CHARS
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"


def _akas_lines() -> list[str]:
    return (_FIXTURES / "title.akas.slice.tsv").read_text(encoding="utf-8").splitlines()


def _kept() -> list[tuple[str, int, str]]:
    return [
        (row.imdb_id, row.ordering, row.name)
        for row in map(parse_akas_row, _akas_lines())
        if row is not None
    ]


def _stage(tmp_path: Path, source: str, name: str) -> Path:
    """gzip a committed .tsv slice into a scratch cache directory, so the
    adapter reads exactly the shape it reads in production."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return cache


def _local(cache: Path) -> httpx.MockTransport:
    """Serves whatever is already in `cache`, so `ensure_local` short-circuits
    on the revision stamp and no bytes are ever transferred."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


def test_an_akas_row_with_the_wrong_column_count_is_malformed() -> None:
    """A filtered row and a malformed row must not be confused: the first is
    expected and silent, the second stops the import.

    The column count is **eight**, taken from the real header measured at the
    pinned snapshot `"19810e3eb2b0f1fa774bf4e4af94d7c6-61"` --
    `titleId ordering title region language types attributes isOriginalTitle`
    -- and not from IMDb's published schema. Measured over all 58,906,368 data
    rows of that file, **zero** split to any other count, so a wrong count is
    a real signal here rather than noise to be tolerated.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row("tt99000020\t1\tonly three columns")
    assert exc_info.value.detail == "tt99000020"


def test_an_akas_row_preserves_an_embedded_double_quote() -> None:
    """The finding that rules out the csv module, re-confirmed on this file.

    IMDb's TSVs have no quoting mechanism, so a `title` may open *and* close
    with a literal `"` -- and `csv.reader`'s default QUOTE_MINIMAL then strips
    both, silently. Measured over the whole pinned `title.akas.tsv.gz`:
    **39,880 rows carry a `"` in `title` and 6,344 of them open with one**, so
    a csv-based parser would silently rewrite 6,344 alias names. Swap
    `line.split("\\t")` for `csv.reader` and this fails.
    """
    row = parse_akas_row(_akas_lines()[3])
    assert row is not None
    assert row.name == '"A Quoted Synthetic Alias"'


def test_the_header_line_is_filtered_not_parsed() -> None:
    assert parse_akas_row(_akas_lines()[0]) is None


def test_a_row_imdb_itself_declares_the_original_title_is_not_an_alias() -> None:
    """`isOriginalTitle = 1` is IMDb's own claim that the row *is* the title's
    original title, and `SearchNameKind` has deliberately no `primary` member:
    a canonical name is served by `ix_titles_name_lower_prefix` on `titles`,
    so storing one here is exactly the one-row-per-title duplication M6's
    boundary call 3 refused the table for.

    Measured, not assumed. Against the pinned snapshot joined to a
    1,272,367-title catalog: **1,272,135 of the 7,541,357 retained rows are
    flagged, 1,272,111 of them (99.998%) casefold-equal the title's own `name`
    or `original_name`, and dropping every flagged row here costs exactly
    **7 aliases out of 1,663,330** after deduplication** -- because the other
    17 of the 24 disagreeing rows repeat a name a non-flagged row already
    carries. It buys the removal of 12,703,704 of 58,906,368 rows (21.6%)
    before a DTO is allocated for any of them.
    """
    assert parse_akas_row(_akas_lines()[1]) is None
    assert parse_akas_row(_akas_lines()[5]) is None
    assert "A Synthetic Feature" not in [name for _, _, name in _kept()]


def test_dropping_the_flagged_rows_does_not_replace_the_writers_own_filter() -> None:
    """The parser's filter is a cheap prefix of the real one, never a
    substitute: of the 6,269,222 retained rows that survive it, **4,426,783
    (70.6%) still casefold-equal the title's own name** and only a comparison
    against the stored `Title` can see that. This case pins the shape -- an
    alias identical to a title's canonical name reaches the caller, because
    the parser has no catalog to compare it against.
    """
    row = parse_akas_row("tt99000020\t9\tA Synthetic Feature\tUS\ten\timdbDisplay\t\\N\t0")
    assert row is not None
    assert row.name == "A Synthetic Feature"


def test_a_row_with_no_title_is_dropped() -> None:
    r"""`ck_title_search_names_name_not_empty` is `name <> ''`, so an empty
    alias cannot be stored at all and a placeholder would be searchable, which
    is worse than absent. Zero of the 58,906,368 real rows have an empty or
    `\N` title -- the drop is unreachable in that snapshot and is here because
    `_optional` is what turns `\N` into `None` rather than into two literal
    characters in the catalog."""
    assert parse_akas_row(_akas_lines()[4]) is None


def test_region_and_language_are_kept_and_backslash_n_becomes_none() -> None:
    r"""`title_search_names` has `region` and `language` precisely so a French
    and a Brazilian alias of one film are distinguishable rows. NULL means
    "not specific to a region", which is a different fact from any code, so
    `\N` must become `None` and never the literal two characters."""
    rows = {
        (row.imdb_id, row.ordering): row
        for row in map(parse_akas_row, _akas_lines())
        if row is not None
    }
    brazilian = rows[("tt99000030", 2)]
    french = rows[("tt99000030", 3)]
    assert (brazilian.region, brazilian.language) == ("BR", "pt")
    assert (french.region, french.language) == ("FR", "fr")
    assert rows[("tt99000020", 3)].language is None
    assert rows[("tt99000010", 1)].region is None


def test_a_name_over_the_btree_bound_is_dropped_here_not_refused_in_a_batch() -> None:
    """`SearchNameRepository`'s contract refuses an over-long name for the
    **whole call**, so one such row would take a ten-thousand-row batch with
    it. Measured on the pinned file: **33 of 58,906,368 rows exceed 512
    characters and the longest is 831** -- and none of the 33 is in today's
    catalog, so this filter is unreachable today and exists because the
    catalog grows while the refusal stays per-call.

    Both sides of the boundary are asserted: 512 is stored, 513 is not.
    """
    at_bound = "x" * AKAS_NAME_MAX_CHARS
    over = "x" * (AKAS_NAME_MAX_CHARS + 1)
    kept = parse_akas_row(f"tt99000020\t5\t{at_bound}\tUS\ten\timdbDisplay\t\\N\t0")
    assert kept is not None
    assert len(kept.name) == AKAS_NAME_MAX_CHARS
    assert parse_akas_row(f"tt99000020\t6\t{over}\tUS\ten\timdbDisplay\t\\N\t0") is None


def test_the_bound_this_parser_filters_on_is_the_one_the_check_constraint_enforces() -> None:
    """Two copies of a number that must agree is how they stop agreeing. The
    filter above is only worth anything if it is the *same* 512 the table's
    `ck_title_search_names_name_within_btree_bound` is spelled with.

    **Asserted structurally as well as by value, because by value it cannot
    fail.** Re-spelling `AKAS_NAME_MAX_CHARS = SEARCH_NAME_MAX_CHARS` as a
    literal `512` is behaviourally identical today and is exactly the defect
    this case exists to stop -- the two numbers would then drift apart the
    first time the CHECK moves, silently, in the direction that lets a row the
    database refuses through. Only reading the binding can tell them apart.
    """
    assert AKAS_NAME_MAX_CHARS == SEARCH_NAME_MAX_CHARS
    module = ast.parse(inspect.getsource(imdb))
    bound = [
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "AKAS_NAME_MAX_CHARS"
            for target in node.targets
        )
    ]
    assert len(bound) == 1
    assert isinstance(bound[0].value, ast.Name)
    assert bound[0].value.id == "SEARCH_NAME_MAX_CHARS"


def test_a_non_integer_ordering_is_malformed_and_names_the_column() -> None:
    """The same call `_optional_int` makes for `startYear`, for the same
    reason: a numeric column that stopped being numeric is an upstream format
    change, and continuing past it would import a subtly wrong catalog."""
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row("tt99000020\tsecond\tAn Alias\tFR\tfr\timdbDisplay\t\\N\t0")
    assert exc_info.value.detail == "tt99000020.ordering"


def test_a_missing_ordering_is_malformed_rather_than_a_row_with_no_tiebreak() -> None:
    r"""`\N` reaches `_optional` before `int()` here as everywhere, and the
    answer for *this* column is a refusal rather than a `None`: `ordering` is
    the only per-title tiebreak a deduplicating writer has, and 0 of the
    58,906,368 real rows lack one (min 1, max 300)."""
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row("tt99000020\t\\N\tAn Alias\tFR\tfr\timdbDisplay\t\\N\t0")
    assert exc_info.value.detail == "tt99000020.ordering"


def test_the_ordering_carried_is_imdbs_own_one_based_value_unconverted() -> None:
    """Stated because the sibling task that is *not* being built converted
    one: `title.principals`' 1-based `ordering` was to be re-based onto
    `Credit.billing_order`. Nothing downstream of this parser is 0-based --
    `title_search_names` has no rank column at all -- so the value is IMDb's
    own, unchanged, and the first row of a title is 1."""
    assert [(imdb_id, ordering) for imdb_id, ordering, _ in _kept()] == [
        ("tt99000020", 2),
        ("tt99000020", 3),
        ("tt99000030", 2),
        ("tt99000030", 3),
        ("tt99000010", 1),
        ("tt99000010", 2),
    ]


def test_a_multi_valued_types_field_is_not_a_column_boundary() -> None:
    """`types` is multi-valued and its separator is `\\x02`, not a tab --
    measured: 429 of the 58,906,368 rows carry more than one value, e.g.
    `imdbDisplay\\x02dvd` (207). A parser that expected a tab there would see
    nine columns and call a perfectly ordinary row malformed."""
    row = parse_akas_row(_akas_lines()[9])
    assert row is not None
    assert row.name == "A Synthetic Festival Title"


def test_no_row_is_filtered_on_its_region_or_its_types() -> None:
    """The retention policy filters on storability and on IMDb's own
    original-title flag, and on nothing else. Measured reasons: bar (B) passed
    **4.8x under on rows and 3.2x under on bytes**, so a recall-costing filter
    buys headroom nobody needs; `types` has 23 values in this snapshot and
    IMDb's own documentation says new ones may be added without warning, so a
    retain-list silently drops the next category and a drop-list silently
    admits it; and `region` has 251 values of which the seven largest are
    5.4-5.8M rows each, so there is no small set to keep.

    A `working` title and a `festival` title are therefore both stored.
    """
    assert [name for _, _, name in _kept()][-2:] == [
        "A Synthetic Working Title",
        "A Synthetic Festival Title",
    ]


async def test_the_akas_dataset_names_itself_and_carries_imdbs_attribution(
    tmp_path: Path,
) -> None:
    """`name` is the `import_runs` key -- changing one orphans its checkpoint.
    `attribution` is IMDb's required exact string (PRD 04), inherited from
    `_ImdbDataset` so `GET /meta/attribution`'s static scan still sees a bare
    module-level constant."""
    async with httpx.AsyncClient() as client:
        dataset = IMDbAkaDataset(client, tmp_path / "bulk", batch_size=1)
    assert dataset.name == "imdb.title.akas"
    assert dataset.filename == "title.akas.tsv.gz"
    assert dataset.attribution == (
        "Information courtesy of IMDb (https://www.imdb.com). Used with permission."
    )


async def test_batches_advance_the_cursor_by_lines_consumed_not_rows_kept(
    tmp_path: Path,
) -> None:
    """`position` is a line offset and `rows_seen` is a kept-row count, and
    the two differ here by more than they do for `title.basics`: the slice has
    ten lines and six of them survive."""
    cache = _stage(tmp_path, "title.akas.slice.tsv", "title.akas.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbAkaDataset(client, cache, batch_size=2)
        batches = [batch async for batch in dataset.batches()]
    assert [len(batch.rows) for batch in batches] == [2, 2, 2]
    assert batches[-1].cursor.position == 10
    assert batches[-1].cursor.rows_seen == 6


async def test_resuming_from_a_cursor_skips_what_was_committed(tmp_path: Path) -> None:
    """A resume replays lines, never rows, and this file is where the two
    counts come apart: **six lines consumed, two rows kept**, because the
    header, a flagged original title and a `\\N` title all sit inside that
    prefix. Resuming from `position=6, rows_seen=2` must yield the remaining
    four and continue the tally rather than restart it.

    The premise is asserted rather than assumed -- if the fixture's line
    ordering ever changed, an unasserted `rows_seen=2` would just be a wrong
    number nothing noticed."""
    cache = _stage(tmp_path, "title.akas.slice.tsv", "title.akas.tsv.gz")
    assert len([row for row in map(parse_akas_row, _akas_lines()[:6]) if row is not None]) == 2
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbAkaDataset(client, cache, batch_size=10)
        first = await anext(dataset.batches())
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=first.cursor.revision, position=6, rows_seen=2)
            )
        ]
    assert [row.name for row in resumed[0].rows] == [
        "Uma Série Sintética",
        "Une Série Synthétique",
        "A Synthetic Working Title",
        "A Synthetic Festival Title",
    ]
    assert resumed[0].cursor.rows_seen == 6


async def test_a_malformed_row_raises_through_batches_instead_of_truncating(
    tmp_path: Path,
) -> None:
    """The port's non-negotiable contract: a stream that stops because
    upstream is wrong must not look like one that finished. Exercised here as
    well as against the standalone parser because `_batches` must not catch
    and swallow the exception on its way past, which would checkpoint a
    partial import as complete."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    body = (
        b"titleId\tordering\ttitle\tregion\tlanguage\ttypes\tattributes\tisOriginalTitle\n"
        b"tt99000020\t2\tUn Long Metrage Synthetique\tFR\tfr\timdbDisplay\t\\N\t0\n"
        b"tt99000021\tsecond\tBad Row\tFR\tfr\timdbDisplay\t\\N\t0\n"
    )
    (cache / "title.akas.tsv.gz").write_bytes(gzip.compress(body))
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbAkaDataset(client, cache, batch_size=1)
        with pytest.raises(PortDataMalformed) as exc_info:
            [batch async for batch in dataset.batches()]
    assert exc_info.value.detail == "tt99000021.ordering"


def test_the_malformed_error_names_the_row_and_never_carries_the_line() -> None:
    """`PortDataMalformed.detail` is a locator, not a payload: an alias line
    can be 831 characters wide and this error is read by an operator and
    written to a log."""
    line = "tt99000020\t1\t" + "an unusually wide alias " * 40
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row(line)
    assert exc_info.value.detail == "tt99000020"
    assert "an unusually wide alias" not in str(exc_info.value)
