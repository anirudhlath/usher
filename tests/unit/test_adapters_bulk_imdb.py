"""IMDb TSV parsing and batching, over a committed synthetic slice.

No network, no Docker, no real dataset file.
"""

import gzip
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.imdb import (
    IMDbRatingDataset,
    IMDbTitleDataset,
    parse_basics_row,
    parse_ratings_row,
)
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"


def _basics_lines() -> list[str]:
    return (_FIXTURES / "title.basics.slice.tsv").read_text().splitlines()


def _stage(tmp_path: Path, source: str, name: str) -> Path:
    """gzip a committed .tsv slice into a scratch cache directory, so the
    adapters read exactly the shape they read in production."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return cache


def test_retains_only_the_four_titletypes_that_map_to_titlekind() -> None:
    """tvEpisode is dropped despite PRD 04 naming it: TitleKind is
    movie|series only, and Episode has no table until a later milestone.
    short, videoGame, and isAdult=1 are dropped as PRD 04 specifies."""
    kept = [row for row in map(parse_basics_row, _basics_lines()) if row is not None]
    assert [row.imdb_id for row in kept] == [
        "tt99000010",
        "tt99000020",
        "tt99000030",
        "tt99000040",
        "tt99000050",
    ]


def test_preserves_embedded_double_quotes() -> None:
    """The finding that rules out the csv module: IMDb's TSVs have no
    quoting mechanism, so a title field may open *and* close with a literal
    `"` -- and `csv.reader`'s default QUOTE_MINIMAL then strips both,
    silently. Measured against the real dump: 21 such titles in the first
    553,395 rows (CLAUDE.md records the specimen; the fixture's row is
    invented, per tests/fixtures/README.md). Delete the split-based parser
    for a csv.reader and this fails."""
    row = parse_basics_row(_basics_lines()[2])
    assert row is not None
    assert row.name == '"A Quoted Synthetic Title"'
    assert row.original_name == '"A Quoted Synthetic Title"'


def test_maps_titletype_onto_titlekind() -> None:
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    assert rows["tt99000020"].kind is TitleKind.MOVIE
    assert rows["tt99000050"].kind is TitleKind.MOVIE  # tvMovie
    assert rows["tt99000030"].kind is TitleKind.SERIES
    assert rows["tt99000040"].kind is TitleKind.SERIES  # tvMiniSeries


def test_backslash_n_becomes_none_not_a_literal() -> None:
    r"""IMDb's documented null sentinel is the two characters `\N`. Storing
    it verbatim would put a literal backslash-N in the catalog."""
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    empty = rows["tt99000050"]
    assert empty.original_name is None
    assert empty.year is None
    assert empty.end_year is None
    assert empty.runtime_minutes is None
    assert empty.genres == ()


def test_splits_the_comma_separated_genres_field() -> None:
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    assert rows["tt99000030"].genres == ("Action", "Adventure", "Drama")


def test_end_year_is_kept_for_series() -> None:
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    assert rows["tt99000030"].end_year == 2009


def test_the_header_line_is_filtered_not_parsed() -> None:
    assert parse_basics_row(_basics_lines()[0]) is None


def test_a_wrong_column_count_is_malformed() -> None:
    """A filtered row and a malformed row must not be confused: the first is
    expected and silent, the second stops the import. An upstream format
    change that silently skipped rows would import a partial catalog and
    checkpoint it as complete."""
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_basics_row("tt99000001\tmovie\tonly three columns")
    assert exc_info.value.detail == "tt99000001"


def test_a_non_integer_year_is_malformed_and_names_the_column() -> None:
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_basics_row("tt99000001\tmovie\tX\tX\t0\tnineteen\t\\N\t1\tDrama")
    assert exc_info.value.detail == "tt99000001.startYear"


def test_a_title_with_no_primary_title_is_dropped() -> None:
    r"""Title.name is Field(min_length=1). A placeholder would be
    searchable, which is worse than absent."""
    assert parse_basics_row("tt99000001\tmovie\t\\N\t\\N\t0\t1990\t\\N\t90\tDrama") is None


def test_ratings_parse_on_imdbs_own_scale() -> None:
    lines = (_FIXTURES / "title.ratings.slice.tsv").read_text().splitlines()
    rows = [row for row in map(parse_ratings_row, lines) if row is not None]
    assert rows[0].imdb_id == "tt99000020"
    assert rows[0].community_rating == 7.4
    assert rows[0].vote_count == 12_345


def test_a_rating_outside_zero_to_ten_is_malformed() -> None:
    """Title.community_rating is Field(ge=0, le=10) and the matching CHECK
    would reject it during COPY anyway -- failing here names the row."""
    with pytest.raises(PortDataMalformed):
        parse_ratings_row("tt99000001\t11.5\t100")


async def test_batches_respect_the_batch_size_and_advance_the_cursor(
    tmp_path: Path,
) -> None:
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=2)
        batches = [batch async for batch in dataset.batches()]
    assert [len(batch.rows) for batch in batches] == [2, 2, 1]
    # position counts *lines consumed*, not rows kept: 10 lines in the slice.
    assert batches[-1].cursor.position == 10
    assert batches[-1].cursor.rows_seen == 5


async def test_a_malformed_row_raises_through_batches_instead_of_truncating(
    tmp_path: Path,
) -> None:
    """The port's non-negotiable contract: a stream that stops because
    upstream is wrong must not look like one that finished. Only the
    standalone parser was exercised against this elsewhere -- this proves
    `_batches` does not catch and swallow the exception on its way past,
    which would otherwise checkpoint a partial import as complete. TMDb's
    suite already covers this for its own adapter; this is IMDb's
    equivalent."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    body = (
        b"tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\t"
        b"runtimeMinutes\tgenres\n"
        b"tt99000020\tmovie\tA Synthetic Feature\tA Synthetic Feature\t0\t1994\t"
        b"\\N\t142\tDrama\n"
        b"tt99000002\tmovie\tBad Row\tBad Row\t0\tnineteen-ninety\t\\N\t90\tDrama\n"
    )
    (cache / "title.basics.tsv.gz").write_bytes(gzip.compress(body))
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=1)
        with pytest.raises(PortDataMalformed) as exc_info:
            [batch async for batch in dataset.batches()]
    assert exc_info.value.detail == "tt99000002.startYear"


async def test_resuming_from_a_cursor_skips_what_was_committed(tmp_path: Path) -> None:
    """The property "resumable" reduces to. `position` is a line offset, and
    the file is re-read from the top because a gzip member is not seekable."""
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=10)
        first = await anext(dataset.batches())
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=first.cursor.revision, position=5, rows_seen=2)
            )
        ]
    assert [row.imdb_id for row in resumed[0].rows] == ["tt99000040", "tt99000050"]
    assert resumed[0].cursor.rows_seen == 4


async def test_a_cursor_from_a_different_revision_restarts_the_stream(
    tmp_path: Path,
) -> None:
    """Line 5 of yesterday's dump is not line 5 of today's. Restarting is
    slow; splicing two snapshots is wrong."""
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=10)
        batches = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision="a-stale-etag", position=5, rows_seen=2)
            )
        ]
    assert len(batches[0].rows) == 5


async def test_dataset_names_and_attribution(tmp_path: Path) -> None:
    """`name` is the import_runs key -- changing one orphans its checkpoint.
    `attribution` is IMDb's required exact string (PRD 04)."""
    cache = tmp_path / "bulk"
    async with httpx.AsyncClient() as client:
        titles = IMDbTitleDataset(client, cache, batch_size=1)
        ratings = IMDbRatingDataset(client, cache, batch_size=1)
    assert titles.name == "imdb.title.basics"
    assert ratings.name == "imdb.title.ratings"
    assert titles.attribution == (
        "Information courtesy of IMDb (https://www.imdb.com). Used with permission."
    )


async def test_a_pre_resolved_revision_skips_the_extra_head_entirely(
    tmp_path: Path,
) -> None:
    """The port's `batches(revision=...)` parameter exists so a caller that
    already paid for `revision()` this run is not forced to pay for it
    again. For IMDb the dataset-level revision *is* the file-level ETag (no
    TMDb-style date/ETag split), so passing it through must skip the HEAD
    `_batches` would otherwise reissue -- and since the file and stamp are
    already cached from the first call, no request at all should follow."""
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=10)
        first = [batch async for batch in dataset.batches()]
        calls.clear()
        second = [batch async for batch in dataset.batches(revision=first[0].cursor.revision)]
    assert calls == []
    assert len(second) == 1
    assert second[0].rows == first[0].rows


async def test_batches_accepts_both_a_resume_cursor_and_a_pre_resolved_revision(
    tmp_path: Path,
) -> None:
    """The two parameters are independent and must compose: a caller that
    already resolved this run's revision and is also resuming a checkpoint
    passes both at once."""
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=10)
        first = await anext(dataset.batches())
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=first.cursor.revision, position=5, rows_seen=2),
                revision=first.cursor.revision,
            )
        ]
    assert [row.imdb_id for row in resumed[0].rows] == ["tt99000040", "tt99000050"]


def _local(cache: Path) -> httpx.MockTransport:
    """Serves whatever is already in `cache`, so `ensure_local` short-circuits
    on the revision stamp and no bytes are ever transferred. This is how the
    suite exercises the real batching path without downloading."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)
