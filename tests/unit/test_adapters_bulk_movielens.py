"""MovieLens tag-genome parsing and assembly, over a synthetic zip built
in this file. No network, no Docker, no real archive.

Every value here is invented. The fixture is Python literals rather than a
committed .csv or .zip because none of the four checks in
tests/unit/test_no_third_party_data.py can recognise a MovieLens row by
shape -- a genome row is three integers and a float, and links.csv is three
integers, both indistinguishable from any CSV ever written. See that module
and tests/fixtures/bulk/README.md.
"""

import zipfile
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.movielens import (
    MOVIELENS_ATTRIBUTION,
    MovieLensGenomeDataset,
    _imdb_id,
)
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed

_ROOT = "ml-latest/"

# Four movies. 90000101 is in links and in the genome (the ordinary path).
# 90000102 is in links with an imdb id the catalog will not hold (the
# repository drops it; the dataset must still yield it). 90000103 is in the
# genome and *absent from links* -- it advances `position` and not
# `rows_seen`. 90000104 is in links and absent from the genome.
#
# The imdb ids are 8 digits beginning 99, which is the reserved synthetic
# band `tests/unit/test_no_third_party_data.py` enforces once the `tt`
# prefix is applied. A *padded* id cannot be written here at all -- see
# `test_a_short_imdb_id_is_left_padded_to_seven_digits`.
_LINKS = "\n".join(
    [
        "movieId,imdbId,tmdbId",
        "90000101,99000101,90000201",
        "90000102,99000102,90000202",
        "90000104,99000104,90000204",
    ]
)
_TAGS = "\n".join(["tagId,tag", "1,a synthetic tag", "2,another synthetic tag", "3,a third"])
_SCORES = "\n".join(
    ["movieId,tagId,relevance"]
    + [
        f"{movie},{tag},{round((movie % 7 + tag) / 11, 5)}"
        for movie in (90000101, 90000102, 90000103)
        for tag in (1, 2, 3)
    ]
)


def _archive(tmp_path: Path, **members: str) -> Path:
    """Write a zip into a scratch cache directory, so the adapter reads
    exactly the shape it reads in production."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(cache / "ml-latest.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(f"{_ROOT}{name.replace('_', '-')}.csv", body)
    return cache


def _default(tmp_path: Path) -> Path:
    return _archive(tmp_path, links=_LINKS, genome_tags=_TAGS, genome_scores=_SCORES)


def _local(cache: Path) -> httpx.MockTransport:
    """Serves whatever is already in `cache`, so `ensure_local` short-circuits
    on the revision stamp and no bytes are ever transferred. The same helper
    shape `tests/unit/test_adapters_bulk_imdb.py` uses; two copies is not yet
    duplication worth a module."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


def _dataset(client: httpx.AsyncClient, cache: Path) -> MovieLensGenomeDataset:
    return MovieLensGenomeDataset(client, cache, batch_size=10, expected_tags=3)


def test_a_short_imdb_id_is_left_padded_to_seven_digits() -> None:
    """`lpad(imdbId, 7, '0')`, not bare concatenation. 79,978 of 86,537 rows
    are 7 wide and 6,559 are 8, so concatenation is correct against today's
    file and silently depends on a padding convention the file documents
    nowhere -- one unpadded row would join to nothing rather than raise.

    Asserted on the digits with the prefix applied separately: a padded id
    begins `tt00`, and `tests/unit/test_no_third_party_data.py` reserves
    only the `tt99` band, so the padded value cannot appear as a literal in
    this repository at all. Kills `f"tt{raw}"`.
    """
    assert _imdb_id("99000") == "tt" + "0099000"
    assert _imdb_id("99000001") == "tt" + "99000001"


def test_an_unusable_imdb_id_is_malformed_rather_than_skipped() -> None:
    """Measured over all 86,537 rows: none is empty, none is non-numeric,
    none is wider than 8. So any of those is an upstream format change and
    not a row to drop -- dropping it would silently shrink the join by an
    unreported amount, which is the shape of defect this whole task's
    coverage report exists to make visible. `imdb_id` is also the join key,
    so an empty one cannot be carried through as a filtered row the way a
    missing links row is."""
    for unusable in ("", "not-a-number", "990000001"):
        with pytest.raises(PortDataMalformed):
            _imdb_id(unusable)


async def test_yields_one_dense_vector_per_movie_not_one_row_per_score(
    tmp_path: Path,
) -> None:
    """Boundary call 7. Kills an implementation that yields a record per
    line of genome-scores.csv -- which is the shape PRD 02 implies and which
    measures at 2,106 MB against 45 MB for this one."""
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        rows = [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    assert [row.movie_id for row in rows] == [90000101, 90000102]
    assert all(len(row.relevance) == 3 for row in rows)


async def test_the_vector_is_ordered_by_tag_id_not_by_file_order(tmp_path: Path) -> None:
    """The only thing that makes two vectors comparable. Kills an
    implementation that appends in the order rows arrive: with the tags of
    one movie shuffled, an append-based build produces a vector that is
    wrong at every position and raises nothing."""
    shuffled = "\n".join(
        ["movieId,tagId,relevance"] + [f"90000101,{tag},{tag / 10}" for tag in (3, 1, 2)]
    )
    cache = _archive(tmp_path, links=_LINKS, genome_tags=_TAGS, genome_scores=shuffled)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        rows = [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    assert rows[0].relevance == (0.1, 0.2, 0.3)


async def test_the_stored_relevance_is_the_archives_own_value_untransformed(
    tmp_path: Path,
) -> None:
    """Measured, not assumed: over all 268,157,000 off-diagonal pairs the
    raw vectors score mean 0.6101 / sd 0.0913 / p1 0.4075 with a
    top-10-neighbour gap of 0.2456, which clears the saturation bar written
    before that run. So the values ship as the archive supplies them.

    Kills an importer that mean-centres anyway -- per-vector centring
    (`v - mean(v)`) would turn the middle lane of this fixture into 0.0 and
    is the single most likely "improvement" a later reader makes, because
    the saturation argument is persuasive and the measurement is not in
    front of them. It is recorded in `usher.adapters.bulk.movielens`.
    """
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        rows = [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    # (90000101 % 7 + tag) / 11 for tags 1, 2, 3.
    assert rows[0].relevance == (0.27273, 0.36364, 0.45455)


async def test_a_movie_absent_from_links_advances_position_but_not_rows_seen(
    tmp_path: Path,
) -> None:
    """A genome movie with no `links.csv` row has no imdb_id and cannot be
    joined to anything, so it is filtered, not malformed. Kills an
    implementation that raises on it (which would abort the import on one
    unmatchable movie) and one that yields it with an empty imdb_id (which
    would then join to every skeleton title with a null id or to none, and
    either is silent)."""
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        batches = [batch async for batch in _dataset(client, cache).batches()]
    assert [row.movie_id for row in batches[-1].rows] == [90000101, 90000102]
    # three completed movie runs consumed, two vectors yielded
    assert batches[-1].cursor.position == 3
    assert batches[-1].cursor.rows_seen == 2


async def test_a_links_row_the_catalog_will_not_hold_is_still_yielded(
    tmp_path: Path,
) -> None:
    """The dataset does not know what the catalog holds -- it never touches
    a database, which is what lets it be unit-tested with no Docker. Kills
    an implementation that tries to pre-filter, which would need a
    repository and would break the port's layering."""
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        rows = [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    assert rows[1].imdb_id == "tt" + "99000102"
    assert rows[1].tmdb_id == 90000202


async def test_a_movie_in_links_but_absent_from_the_genome_yields_nothing(
    tmp_path: Path,
) -> None:
    """links.csv holds 86,537 movies and the genome holds 16,376 of them.
    Kills an implementation that iterates links and looks up scores, which
    would emit 70,161 zero vectors -- ADR-0014 at dataset scale."""
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        rows = [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    assert 90000104 not in {row.movie_id for row in rows}


async def test_a_short_run_is_malformed_and_names_the_movie(tmp_path: Path) -> None:
    """Every movie carries a value for every tag -- verified by counting, and
    it is what makes the dense shape correct. A run of the wrong length is
    an upstream format change, and continuing past it would store a vector
    that is wrong from the missing tag onward while raising nothing."""
    short = "\n".join(["movieId,tagId,relevance", "90000101,1,0.5", "90000101,2,0.5"])
    cache = _archive(tmp_path, links=_LINKS, genome_tags=_TAGS, genome_scores=short)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        with pytest.raises(PortDataMalformed) as exc_info:
            [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    assert exc_info.value.detail == "90000101"


async def test_a_run_whose_tags_are_not_one_to_n_is_malformed(tmp_path: Path) -> None:
    """A run of the right *length* whose tag ids are wrong. Kills an
    implementation that checks only the count and then builds by index:
    a duplicated tag plus a missing one is length-1128 and leaves one lane
    holding another lane's value and one lane holding whatever the buffer
    was initialised to."""
    duplicated = "\n".join(
        ["movieId,tagId,relevance", "90000101,1,0.5", "90000101,2,0.5", "90000101,2,0.5"]
    )
    cache = _archive(tmp_path, links=_LINKS, genome_tags=_TAGS, genome_scores=duplicated)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        with pytest.raises(PortDataMalformed) as exc_info:
            [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    assert exc_info.value.detail == "90000101"


async def test_a_movie_that_reappears_after_its_run_is_malformed(tmp_path: Path) -> None:
    """This is what turns "the file is sorted by movieId" from an assumption
    into an enforced property. Kills the version that keeps a one-movie
    buffer and no seen-set: on an unsorted file it would emit one truncated
    vector per fragment, all of them wrong, all of them silent."""
    interleaved = "\n".join(
        ["movieId,tagId,relevance"]
        + [f"90000101,{t},0.5" for t in (1, 2, 3)]
        + [f"90000102,{t},0.5" for t in (1, 2, 3)]
        + ["90000101,1,0.9"]
    )
    cache = _archive(tmp_path, links=_LINKS, genome_tags=_TAGS, genome_scores=interleaved)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        with pytest.raises(PortDataMalformed):
            [row async for batch in _dataset(client, cache).batches() for row in batch.rows]


async def test_a_non_contiguous_tag_vocabulary_is_malformed_before_any_score_is_read(
    tmp_path: Path,
) -> None:
    """`tagId` is 1...1128 contiguous, and the vector is built by index from
    that. A gap means every position after it is off by one, in every vector,
    for the whole import -- and the resulting table is indistinguishable from
    a correct one until somebody compares two releases."""
    gapped = "\n".join(["tagId,tag", "1,a synthetic tag", "2,another", "4,a fourth"])
    cache = _archive(tmp_path, links=_LINKS, genome_tags=gapped, genome_scores=_SCORES)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        with pytest.raises(PortDataMalformed, match="contiguous"):
            [row async for batch in _dataset(client, cache).batches() for row in batch.rows]


async def test_a_vocabulary_of_the_wrong_width_is_malformed(tmp_path: Path) -> None:
    """The schema declares `halfvec(1128)`. A release whose vocabulary grew
    must fail here, naming both widths, rather than 16,376 rows later inside
    a COPY with a dimension error naming neither the dataset nor the
    release."""
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = MovieLensGenomeDataset(client, cache, batch_size=10)  # expects 1128
        with pytest.raises(PortDataMalformed, match="1128"):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_a_missing_member_names_the_member(tmp_path: Path) -> None:
    """Task 18's translation, exercised end to end. The member names carry
    the `ml-latest/` root, so a future release that renames it fails on the
    first read with the name it looked for -- not with an empty import that
    reports success."""
    cache = _archive(tmp_path, genome_tags=_TAGS, genome_scores=_SCORES)  # no links.csv
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        with pytest.raises(PortDataMalformed) as exc_info:
            [row async for batch in _dataset(client, cache).batches() for row in batch.rows]
    assert exc_info.value.detail == f"{_ROOT}links.csv"


async def test_resuming_from_a_cursor_skips_completed_movies(tmp_path: Path) -> None:
    """`position` counts *completed movie runs consumed*, so a resume never
    lands mid-run. Kills a line-number cursor: resuming from line N of
    genome-scores.csv would restart inside a movie's 1,128 rows and emit a
    truncated first vector."""
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = _dataset(client, cache)
        first = await anext(dataset.batches())
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=first.cursor.revision, position=1, rows_seen=1)
            )
        ]
    assert [row.movie_id for row in resumed[0].rows] == [90000102]
    assert resumed[0].cursor.rows_seen == 2


async def test_a_cursor_from_a_different_revision_restarts_the_stream(
    tmp_path: Path,
) -> None:
    """Movie 400 of one release is not movie 400 of the next, and the tag
    vocabulary may have changed underneath it too."""
    cache = _default(tmp_path)
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        batches = [
            batch
            async for batch in _dataset(client, cache).batches(
                resume_from=BulkCursor(revision="a-stale-etag", position=2, rows_seen=2)
            )
        ]
    assert len(batches[0].rows) == 2


async def test_a_pre_resolved_revision_skips_the_head_entirely(tmp_path: Path) -> None:
    """The dataset revision *is* the archive's ETag, like IMDb and unlike
    TMDb -- so a caller that already paid for `revision()` this run needs no
    second HEAD, and `LocalFile.replaced` needs no reconciliation. Kills an
    implementation that resolves the revision again regardless."""
    cache = _default(tmp_path)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = _dataset(client, cache)
        first = [batch async for batch in dataset.batches()]
        methods.clear()
        [batch async for batch in dataset.batches(revision=first[0].cursor.revision)]
    assert "HEAD" not in methods


async def test_name_and_attribution(tmp_path: Path) -> None:
    """`name` is the `import_runs` key -- changing it orphans the checkpoint.
    `attribution` is never empty by contract, and MovieLens' licence
    requires a citation rather than a fixed disclaimer (PRD 04)."""
    cache = tmp_path / "bulk"
    async with httpx.AsyncClient() as client:
        dataset = MovieLensGenomeDataset(client, cache, batch_size=1)
    assert dataset.name == "movielens.genome"
    assert dataset.attribution == MOVIELENS_ATTRIBUTION
    assert "Harper" in dataset.attribution and "Konstan" in dataset.attribution
