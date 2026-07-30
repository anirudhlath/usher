"""TMDb daily ID export parsing. No network, no key, no real export."""

import datetime as dt
import gzip
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed, PortUnavailable

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"
_TODAY = dt.date(2026, 7, 30)


def _stage(tmp_path: Path, source: str, name: str) -> Path:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return cache


def _serving(cache: Path, available: set[str]) -> httpx.MockTransport:
    """404s every export except the ones named in `available`, mirroring the
    real host: today's export does not exist until ~08:00 UTC."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name not in available:
            return httpx.Response(404)
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


async def test_parses_the_movie_export(tmp_path: Path) -> None:
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_30_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    by_id = {row.tmdb_id: row for row in rows}
    assert by_id[278].original_name == "The Shawshank Redemption"
    assert by_id[278].popularity == 45.5
    assert by_id[278].kind is TitleKind.MOVIE
    assert by_id[99991].adult is True


async def test_missing_popularity_defaults_to_zero(tmp_path: Path) -> None:
    """Never None: `tmdb_ids.popularity` is NOT NULL, and a crawl queue
    ordered by NULL has no ordering."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_30_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        rows = {r.tmdb_id: r async for batch in dataset.batches() for r in batch.rows}
    assert rows[99992].popularity == 0.0


async def test_the_tv_export_uses_original_name_and_has_no_adult_field(
    tmp_path: Path,
) -> None:
    """Both asymmetries in one test, because both are real: the TV export
    spells the name `original_name` and omits `adult` entirely (verified
    against tv_series_ids_*.json.gz). A parser that read `original_title`
    would raise on every TV row."""
    cache = _stage(tmp_path, "tv_series_ids.slice.jsonl", "tv_series_ids_07_30_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"tv_series_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.SERIES, batch_size=10, today=_TODAY)
        rows = {r.tmdb_id: r async for batch in dataset.batches() for r in batch.rows}
    assert rows[1399].original_name == "Game of Thrones"
    assert rows[1399].adult is False
    assert rows[45].kind is TitleKind.SERIES


async def test_walks_back_to_the_newest_export_that_exists(tmp_path: Path) -> None:
    """Exports publish around 08:00 UTC, so today's 404s for part of the
    day. A run that failed then would fail every morning."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_28_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_28_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        assert await dataset.revision() == "2026-07-28"


async def test_no_export_within_the_window_is_unavailable(tmp_path: Path) -> None:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    async with httpx.AsyncClient(transport=_serving(cache, set())) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        with pytest.raises(PortUnavailable):
            await dataset.revision()


async def test_a_line_that_is_not_json_is_malformed(tmp_path: Path) -> None:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    (cache / "movie_ids_07_30_2026.json.gz").write_bytes(gzip.compress(b"not json at all\n"))
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        with pytest.raises(PortDataMalformed):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_dataset_names_are_distinct_per_kind(tmp_path: Path) -> None:
    """Two datasets, two checkpoints. A shared name would make the series
    import resume from the movie import's line offset."""
    cache = tmp_path / "bulk"
    async with httpx.AsyncClient() as client:
        movies = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=1, today=_TODAY)
        series = TMDbIdDataset(client, cache, kind=TitleKind.SERIES, batch_size=1, today=_TODAY)
    assert movies.name == "tmdb.ids.movie"
    assert series.name == "tmdb.ids.series"
    assert "not endorsed or certified by TMDB" in movies.attribution


async def test_revision_none_does_not_double_head_the_winning_file(tmp_path: Path) -> None:
    """`_newest_available`'s own scan already resolves the winning file's
    ETag via the HEAD that proved it exists. Re-deriving that ETag with a
    second HEAD to the identical URL right before `ensure_local` -- which an
    earlier draft of this adapter did -- is pure waste on every run."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_30_2026.json.gz")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name != "movie_ids_07_30_2026.json.gz":
            return httpx.Response(404)
        methods.append(request.method)
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        _ = [batch async for batch in dataset.batches()]
    assert methods == ["HEAD"]


async def test_a_pre_resolved_revision_skips_the_backward_scan(tmp_path: Path) -> None:
    """The port's `batches(revision=...)` parameter exists precisely for
    this adapter: without it, a caller that already resolved this run's
    revision via `revision()` forces `batches()` to redo the whole
    multi-day backward walk. Passing it through must go straight to the
    known day's file instead of re-probing 07-30 and 07-29 first."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_28_2026.json.gz")
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        requests_seen.append(name)
        if name != "movie_ids_07_28_2026.json.gz":
            return httpx.Response(404)
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        batches = [batch async for batch in dataset.batches(revision="2026-07-28")]
    # A single request, straight to the known day -- an unresolved revision
    # would have probed 07-30 and 07-29 first (both 404) before ever
    # reaching this file.
    assert requests_seen == ["movie_ids_07_28_2026.json.gz"]
    assert len(batches) == 1


async def test_position_counts_lines_consumed_not_rows_kept(tmp_path: Path) -> None:
    """A blank line is filtered (`_parse` returns None for it) but is still
    a line the file offset has to account for. If `position` counted kept
    rows instead of raw lines consumed, `skip=` on a resume would
    desynchronise from what `lines(skip=...)` actually skips -- silently
    replaying or dropping rows depending on how many filtered lines fall
    before the resume point. IMDb's suite guards this same invariant with
    its own titleType filtering; this is TMDb's equivalent, using a blank
    line since TMDb's parser otherwise keeps everything it doesn't reject
    outright."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    body = (
        b'{"adult":false,"id":1,"original_title":"A","popularity":1.0,"video":false}\n'
        b"\n"
        b'{"adult":false,"id":2,"original_title":"B","popularity":2.0,"video":false}\n'
    )
    (cache / "movie_ids_07_30_2026.json.gz").write_bytes(gzip.compress(body))
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        batches = [batch async for batch in dataset.batches()]
    assert len(batches) == 1
    assert [row.tmdb_id for row in batches[0].rows] == [1, 2]
    # 3 raw lines (id=1, blank, id=2); only 2 are kept.
    assert batches[0].cursor.position == 3
    assert batches[0].cursor.rows_seen == 2


async def test_resuming_from_a_real_cursor_reproduces_no_gap_and_no_duplicate(
    tmp_path: Path,
) -> None:
    """The end-to-end resume guarantee, chained through a real cursor rather
    than a hand-constructed one: whatever a real first call's cursor claims
    was consumed, resuming from exactly that cursor must continue without
    re-yielding an already-committed row or skipping an uncommitted one.
    Unlike a hardcoded `position=`, this cannot pass by coincidence if the
    line-counting arithmetic is wrong."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_30_2026.json.gz")

    async def _ids(resume_from: BulkCursor | None = None) -> list[int]:
        async with httpx.AsyncClient(
            transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
        ) as client:
            dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=1, today=_TODAY)
            return [
                row.tmdb_id
                async for batch in dataset.batches(resume_from=resume_from)
                for row in batch.rows
            ]

    full_run_ids = await _ids()

    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=1, today=_TODAY)
        first_batch = await anext(dataset.batches())

    resumed_ids = await _ids(resume_from=first_batch.cursor)
    committed_then_resumed = [row.tmdb_id for row in first_batch.rows] + resumed_ids
    assert committed_then_resumed == full_run_ids


async def test_a_cursor_from_a_different_revision_restarts_the_stream(
    tmp_path: Path,
) -> None:
    """Position 2 of yesterday's export is not position 2 of today's --
    restarting is slow, splicing two snapshots is wrong. IMDb's suite has
    the equivalent of this test; the plan's TMDb suite omitted it."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_30_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY)
        batches = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision="2020-01-01", position=2, rows_seen=1)
            )
        ]
    assert len(batches[0].rows) == 4
