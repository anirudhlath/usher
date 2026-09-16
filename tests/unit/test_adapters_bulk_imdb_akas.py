"""IMDb `title.akas` parsing and batching, over a committed synthetic slice."""

import ast
import gzip
import inspect
from collections import Counter
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
    """Gzip a committed .tsv slice into a scratch cache directory.

    The adapter then reads exactly the shape it reads in production.
    """
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return cache


def _local(cache: Path) -> httpx.MockTransport:
    """Serves whatever is already in `cache`.

    `ensure_local` short-circuits on the revision stamp, so no bytes are transferred.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


def test_an_akas_row_with_the_wrong_column_count_is_malformed() -> None:
    """A filtered row and a malformed row must not be confused.

    The first is expected and silent, the second stops the import. The column count is
    eight, taken from the pinned snapshot's own header rather than from IMDb's
    published schema, so a row splitting to any other count is a real signal.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row("tt99000020\t1\tonly three columns")
    assert exc_info.value.detail == "tt99000020"


def test_an_akas_row_preserves_an_embedded_double_quote() -> None:
    r"""A title that opens and closes with a literal `"` keeps both.

    IMDb's TSVs have no quoting mechanism, so `csv.reader`'s default
    QUOTE_MINIMAL strips the pair silently and rewrites the alias name. Swap
    `line.split("\t")` for `csv.reader` and this fails.
    """
    row = parse_akas_row(_akas_lines()[3])
    assert row is not None
    assert row.name == '"A Quoted Synthetic Alias"'


def test_the_header_line_is_filtered_not_parsed() -> None:
    assert parse_akas_row(_akas_lines()[0]) is None


def test_a_row_imdb_itself_declares_the_original_title_is_not_an_alias() -> None:
    """`isOriginalTitle = 1` is IMDb's claim that the row is the title's original title.

    Those rows are dropped and `SearchNameKind` has no `primary` member: a canonical
    name is served by `ix_titles_name_lower_prefix` on `titles`, so keeping one here
    would duplicate a row per title. A flagged row almost always casefold-equals the
    title's own `name` or `original_name`, so dropping it loses next to no alias and
    spares a DTO allocation for a large share of the file.
    """
    assert parse_akas_row(_akas_lines()[1]) is None
    assert parse_akas_row(_akas_lines()[5]) is None
    assert "A Synthetic Feature" not in [name for _, _, name in _kept()]


def test_dropping_the_flagged_rows_does_not_replace_the_writers_own_filter() -> None:
    """The parser's filter is a cheap prefix of the real one, never a substitute.

    Most retained rows still casefold-equal the title's own name, and only a comparison
    against the stored `Title` can see that. This case pins the shape: an alias
    identical to a title's canonical name reaches the caller, because the parser has no
    catalog to compare it against.
    """
    row = parse_akas_row("tt99000020\t9\tA Synthetic Feature\tUS\ten\timdbDisplay\t\\N\t0")
    assert row is not None
    assert row.name == "A Synthetic Feature"


def test_a_row_with_no_title_is_dropped() -> None:
    r"""`ck_title_search_names_name_not_empty` is `name <> ''`.

    An empty alias cannot be stored at all and a placeholder would be
    searchable, which is worse than absent. Zero of the 58,906,368 real rows
    have an empty or `\N` title -- the drop is unreachable in that snapshot and
    is here because `_optional` is what turns `\N` into `None` rather than into
    two literal characters in the catalog.
    """
    assert parse_akas_row(_akas_lines()[4]) is None


def test_region_and_language_are_kept_and_backslash_n_becomes_none() -> None:
    r"""`region` and `language` make two aliases of one film distinguishable rows.

    NULL means "not specific to a region", which is a different fact from any
    code, so `\N` must become `None` and never the literal two characters.
    """
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
    """`BulkCatalogRepository.replace_aliases` refuses an over-long name for the whole call.

    One such row would take a ten-thousand-row batch with it, so the parser drops it
    first. Over-long rows are vanishingly rare upstream and none is in today's catalog;
    the filter exists because the catalog grows while the refusal stays per-call.

    Both sides of the boundary are asserted: 512 is stored, 513 is not.
    """
    at_bound = "x" * AKAS_NAME_MAX_CHARS
    over = "x" * (AKAS_NAME_MAX_CHARS + 1)
    kept = parse_akas_row(f"tt99000020\t5\t{at_bound}\tUS\ten\timdbDisplay\t\\N\t0")
    assert kept is not None
    assert len(kept.name) == AKAS_NAME_MAX_CHARS
    assert parse_akas_row(f"tt99000020\t6\t{over}\tUS\ten\timdbDisplay\t\\N\t0") is None


def test_the_bound_this_parser_filters_on_is_the_one_the_check_constraint_enforces() -> None:
    """Two copies of a number that must agree is how they stop agreeing.

    The filter above is only worth anything if it is the *same* 512 the table's
    `ck_title_search_names_name_within_btree_bound` is spelled with.

    Asserted structurally as well as by value, because by value it cannot fail:
    re-spelling `AKAS_NAME_MAX_CHARS = SEARCH_NAME_MAX_CHARS` as a literal `512` is
    behaviourally identical today, and the two would then drift apart the first time
    the CHECK moves. Only reading the binding can tell them apart.
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
    """The same call `_optional_int` makes for `startYear`, for the same reason.

    A numeric column that stopped being numeric is an upstream format change, and
    continuing past it would import a subtly wrong catalog.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row("tt99000020\tsecond\tAn Alias\tFR\tfr\timdbDisplay\t\\N\t0")
    assert exc_info.value.detail == "tt99000020.ordering"


def test_a_missing_ordering_is_malformed_rather_than_a_row_with_no_tiebreak() -> None:
    r"""The answer for *this* column is a refusal rather than a `None`.

    `\N` reaches `_optional` before `int()` here as everywhere. `ordering` is
    the only per-title tiebreak a deduplicating writer has, and 0 of the
    58,906,368 real rows lack one (min 1, max 300).
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row("tt99000020\t\\N\tAn Alias\tFR\tfr\timdbDisplay\t\\N\t0")
    assert exc_info.value.detail == "tt99000020.ordering"


def test_the_ordering_carried_is_imdbs_own_one_based_value_unconverted() -> None:
    """`ordering` is carried through as IMDb's own 1-based value.

    Nothing downstream of this parser is 0-based -- `title_search_names` has no rank
    column at all -- so the first row of a title is 1.
    """
    assert [(imdb_id, ordering) for imdb_id, ordering, _ in _kept()] == [
        ("tt99000020", 2),
        ("tt99000020", 3),
        ("tt99000030", 2),
        ("tt99000030", 3),
        ("tt99000010", 1),
        ("tt99000010", 2),
    ]


def test_a_multi_valued_types_field_is_not_a_column_boundary() -> None:
    r"""`types` is multi-valued and its separator is `\x02`, not a tab.

    A row carrying `imdbDisplay\x02dvd` is ordinary; a parser that expected a
    tab there would see nine columns and call it malformed.
    """
    row = parse_akas_row(_akas_lines()[9])
    assert row is not None
    assert row.name == "A Synthetic Festival Title"


def test_no_row_is_filtered_on_its_region_or_its_types() -> None:
    """Retention filters on storability and on IMDb's original-title flag, nothing else.

    A recall-costing filter would buy headroom nobody needs; IMDb may add a `types`
    value without warning, so a retain-list silently drops the next category and a
    drop-list silently admits it; and `region` has no small set worth keeping. A
    `working` title and a `festival` title are therefore both stored.
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
    `_ImdbDataset` so `GET /meta/attribution`'s static scan still sees a bare module-
    level constant.
    """
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
    """`position` is a line offset and `rows_seen` is a kept-row count.

    They diverge further here than for `title.basics`, because this parser filters more
    of what it reads.
    """
    cache = _stage(tmp_path, "title.akas.slice.tsv", "title.akas.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbAkaDataset(client, cache, batch_size=2)
        batches = [batch async for batch in dataset.batches()]
    assert [len(batch.rows) for batch in batches] == [2, 2, 2]
    assert batches[-1].cursor.position == 10
    assert batches[-1].cursor.rows_seen == 6


async def test_resuming_from_a_cursor_skips_what_was_committed(tmp_path: Path) -> None:
    r"""A resume replays lines, never rows, and here the two counts come apart.

    **Six lines consumed, two rows kept**, because the header, a flagged
    original title and a `\N` title all sit inside that prefix. Resuming from
    `position=6, rows_seen=2` must yield the remaining four and continue the
    tally rather than restart it.

    The premise is asserted rather than assumed -- if the fixture's line
    ordering ever changed, an unasserted `rows_seen=2` would just be a wrong
    number nothing noticed.
    """
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
    """A stream that stops because upstream is wrong must not look like one that finished.

    Exercised here as well as against the standalone parser because `_batches` must not
    catch and swallow the exception on its way past, which would checkpoint a partial
    import as complete.
    """
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
    """`PortDataMalformed.detail` is a locator, not a payload.

    An alias line can be hundreds of characters wide, and this error is read by an
    operator and written to a log.
    """
    line = "tt99000020\t1\t" + "an unusually wide alias " * 40
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_akas_row(line)
    assert exc_info.value.detail == "tt99000020"
    assert "an unusually wide alias" not in str(exc_info.value)


async def test_a_titles_aliases_are_never_split_across_two_batches(tmp_path: Path) -> None:
    """A batch is a transaction and `replace_aliases` is a scoped delete then an insert.

    A title split across two batches loses the aliases in the first: the second call's
    scope names that title again and the delete takes the rows the first call wrote.
    """
    per_title = Counter(imdb_id for imdb_id, _, _ in _kept())
    assert max(per_title.values()) > 1, "the premise: some title has more than one alias"

    cache = _stage(tmp_path, "title.akas.slice.tsv", "title.akas.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        batches = [batch async for batch in IMDbAkaDataset(client, cache, batch_size=1).batches()]

    seen: list[str] = []
    for batch in batches:
        titles = list(dict.fromkeys(row.imdb_id for row in batch.rows))
        assert not set(titles) & set(seen), (
            f"title(s) {sorted(set(titles) & set(seen))} reach the writer in two batches; "
            "the second scoped replace deletes what the first wrote"
        )
        seen += titles
    assert seen == ["tt99000020", "tt99000030", "tt99000010"]


async def test_the_cursor_advances_to_a_title_boundary_and_a_resume_loses_nothing(
    tmp_path: Path,
) -> None:
    """`position` counts lines consumed through the end of the last completed title.

    Not lines read: a title's last line is only knowable once the first line of the
    next one has been consumed, so a cursor built from lines read would resume
    mid-title and the resumed half's scoped replace would delete the committed half.

    The boundary lands on the last line before the next title's first *retained* row,
    because a filtered line produces no record and cannot close a group. Skipping that
    line on resume skips a row the parser drops anyway; letting a filtered line close
    the open group would move the cursor past rows no writer has seen.
    """
    cache = _stage(tmp_path, "title.akas.slice.tsv", "title.akas.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        first = await anext(IMDbAkaDataset(client, cache, batch_size=1).batches())
        assert first.cursor.position == 6, "the premise: the boundary is inside the title's run"
        assert [row.imdb_id for row in first.rows] == ["tt99000020", "tt99000020"]
        resumed = [
            row
            async for batch in IMDbAkaDataset(client, cache, batch_size=10).batches(
                resume_from=first.cursor
            )
            for row in batch.rows
        ]
    assert [row.imdb_id for row in resumed] == [
        "tt99000030",
        "tt99000030",
        "tt99000010",
        "tt99000010",
    ]
