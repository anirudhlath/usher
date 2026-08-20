"""The whole Phase 0-2 pipeline against real Postgres, over committed
synthetic slices. Nothing downloads.

This is the test that proves the parts compose: dataset -> service ->
repository -> Postgres, with checkpoints, resumption, and the crosswalk
link.
"""

import gzip
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.adapters.bulk.imdb import IMDbAkaDataset, IMDbRatingDataset, IMDbTitleDataset
from usher.adapters.bulk.movielens import MovieLensGenomeDataset
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.genome import PostgresGenomeRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import (
    GENOME_TAG_COUNT,
    GenomeTag,
    GenomeVector,
    IdCrosswalkPair,
    ImdbAka,
    ImdbTitle,
    TmdbId,
)
from usher.ports.errors import PortDataMalformed, RepositoryConflict
from usher.ports.events import NullEventPublisher
from usher.services.bootstrap import BootstrapService

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = tmp_path / "bulk"
    directory.mkdir(parents=True)
    for source, name in (
        ("title.basics.slice.tsv", "title.basics.tsv.gz"),
        ("title.ratings.slice.tsv", "title.ratings.tsv.gz"),
    ):
        (directory / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return directory


def _local(cache: Path) -> httpx.MockTransport:
    """Serves from the already-staged cache, so ensure_local short-circuits
    on the revision stamp -- see the same helper in
    tests/unit/test_adapters_bulk_imdb.py."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


async def test_phases_zero_to_two_produce_a_linked_skeleton_catalog(
    session: AsyncSession, cache: Path
) -> None:
    catalog = PostgresBulkCatalogRepository(session)
    service = BootstrapService(
        PostgresImportRunRepository(session),
        catalog,
        session.flush,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )

    async with httpx.AsyncClient(transport=_local(cache)) as client, catalog.bulk_load_window():
        titles_run = await service.import_dataset(
            IMDbTitleDataset(client, cache, batch_size=2),
            lambda rows: _written(catalog, rows),
        )
        ratings_run = await service.import_dataset(
            IMDbRatingDataset(client, cache, batch_size=10), catalog.apply_ratings
        )

    assert titles_run.status is ImportRunStatus.COMPLETED
    assert titles_run.rows_seen == 5
    assert ratings_run.status is ImportRunStatus.COMPLETED
    assert await catalog.count_titles() == 5

    await catalog.upsert_tmdb_ids(
        [
            TmdbId(
                tmdb_id=90000020,
                kind=TitleKind.MOVIE,
                original_name="Synthetic Feature",
                popularity=12.5,
            ),
            TmdbId(
                tmdb_id=90001399,
                kind=TitleKind.SERIES,
                original_name="Synthetic Series",
                popularity=31.5,
            ),
        ]
    )
    await catalog.upsert_crosswalk(
        [
            IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020),
            IdCrosswalkPair(imdb_id="tt99000030", tmdb_series_id=90001399, tvdb_series_id=91000030),
        ]
    )
    linked = await catalog.link_crosswalk()
    assert linked.linked == 2

    # `imdb_average_rating` beside `tmdb_vote_average`, and the NULL is the
    # assertion ADR-0040 bought. Nothing in phases 0-2 enriches, so the IMDb
    # ratings import above is the only writer of a rating figure anywhere in
    # this bootstrap -- and before the split it wrote that figure into
    # `tmdb_vote_average`, so this same projection read 7.4 in the TMDb column
    # and NULL in the IMDb one, exactly inverted, with nothing recording the
    # swap. No claim about phase *ordering* is involved: it is the pair of
    # columns that carries the provenance, which is why both are selected.
    # This is the whole fix seen from the bootstrap it ships in, rather than
    # from one repository call.
    result = await session.execute(
        text(
            "SELECT imdb_id, tmdb_id, tvdb_id, tmdb_popularity, imdb_average_rating, "
            "tmdb_vote_average, enrichment_state FROM titles "
            "WHERE imdb_id IN ('tt99000020','tt99000030') ORDER BY imdb_id"
        )
    )
    rows = result.all()
    assert rows[0] == ("tt99000020", 90000020, None, 12.5, 7.4, None, "skeleton")
    assert rows[1] == ("tt99000030", 90001399, 91000030, 31.5, 6.8, None, "skeleton")


async def test_the_catalog_is_queryable_between_batches(session: AsyncSession, cache: Path) -> None:
    """ADR-0005 and the spec both promise the catalog is usable during
    bootstrap. With batch_size=2 the first commit lands two titles, and a
    reader sees them before the import finishes -- this asserts the loop
    really does commit per batch rather than once at the end.

    `commits` (not just `seen`) is what actually pins that: `upsert_titles`
    writes via `COPY` straight to the connection, so `seen` alone stays
    `[2, 4, 5]` even with the per-batch `self._commit()` call deleted
    entirely -- confirmed directly by deleting it and re-running this test,
    which still passed. `commits` counts calls to the *injected* commit
    callable itself, independent of COPY's own within-session visibility,
    so it is the assertion that actually distinguishes committing per batch
    from merely writing per batch and committing once at the end."""
    catalog = PostgresBulkCatalogRepository(session)
    commits = 0

    async def counting_flush() -> None:
        nonlocal commits
        commits += 1
        await session.flush()

    service = BootstrapService(
        PostgresImportRunRepository(session),
        catalog,
        counting_flush,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )
    seen: list[int] = []

    async def write_and_peek(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        seen.append(await catalog.count_titles())
        return result.inserted + result.updated

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        await service.import_dataset(IMDbTitleDataset(client, cache, batch_size=2), write_and_peek)
    assert seen == [2, 4, 5]
    # 3 batches (2, 2, 1) + the final COMPLETED save -- see
    # test_commits_once_per_batch_plus_once_at_the_end (tests/unit/
    # test_services_bootstrap.py) for the same shape against a fake.
    assert commits == 4


async def test_a_restart_resumes_from_the_stored_checkpoint(
    session: AsyncSession, cache: Path
) -> None:
    """Simulates a crash by importing with a service whose write fails on
    the third batch, then re-running -- the second run must pick up the
    cursor the first one committed."""
    catalog = PostgresBulkCatalogRepository(session)
    runs = PostgresImportRunRepository(session)
    service = BootstrapService(
        runs, catalog, session.flush, events=NullEventPublisher(), phase=BootstrapPhase.ALL
    )

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        first = IMDbTitleDataset(client, cache, batch_size=2)
        batches = 0

        async def write_twice_then_stop(rows: Sequence[ImdbTitle]) -> int:
            nonlocal batches
            batches += 1
            if batches > 2:
                raise _Stop
            result = await catalog.upsert_titles(rows)
            return result.inserted + result.updated

        with pytest.raises(_Stop):
            await service.import_dataset(first, write_twice_then_stop)

        checkpoint = await runs.get("imdb.title.basics")
        assert checkpoint is not None
        assert checkpoint.rows_seen == 4

        # Every write below is an upsert, so `count_titles() == 5` at the end
        # would hold even if resumption were silently broken and the second
        # run restarted from line 0 -- confirmed directly, by disabling
        # resume_from in BootstrapService._drain and watching this test's
        # final assertions still pass. `batches_seen` and the imdb_ids
        # actually written close that gap: a genuine resume calls write()
        # exactly once, with only the one row (tt99000050) that was never
        # committed before the crash; a silent restart would call it three
        # times (batch_size=2 over all five rows again).
        seen_ids: list[str] = []
        batches_seen = 0

        async def write_and_record(rows: Sequence[ImdbTitle]) -> int:
            nonlocal batches_seen
            batches_seen += 1
            seen_ids.extend(row.imdb_id for row in rows)
            return await _written(catalog, rows)

        second = IMDbTitleDataset(client, cache, batch_size=2)
        run = await service.import_dataset(second, write_and_record)

    assert batches_seen == 1
    assert seen_ids == ["tt99000050"]
    assert run.status is ImportRunStatus.COMPLETED
    assert await catalog.count_titles() == 5


class _Stop(Exception):
    """Not a UsherPortError, deliberately: BootstrapService records port
    errors and swallows them, so a port error here would give a COMPLETED-
    shaped path rather than the abrupt stop this test needs."""


async def _written(catalog: PostgresBulkCatalogRepository, rows: Sequence[ImdbTitle]) -> int:
    result = await catalog.upsert_titles(rows)
    return result.inserted + result.updated


# --- the IMDb expansion phases, end to end -------------------------------


async def test_a_titles_aliases_survive_a_batch_boundary_against_real_postgres(
    session: AsyncSession, cache: Path
) -> None:
    """The alias phase's dataset and its writer, composed, against the
    statement that actually does the deleting.

    `replace_aliases` is `DELETE ... WHERE title_id = ANY(:ids) AND kind =
    'alias'` followed by an insert, so a title split across two batches has
    its first half deleted by its second half's call -- and **nothing
    raises**, because the port's `ValueError` guard is about a row outside the
    scope and both halves are inside their own. `batch_size=1` over a slice
    whose every title carries two aliases is the shape that does it.

    **This case is here as well as in `tests/unit/test_cli.py` because only
    this arm runs the DELETE.** `FakeBulkCatalogRepository` models the scope
    with a list comprehension; a defect in the statement's `= ANY(...)` or in
    its `kind` predicate is invisible from the fake, and a defect in the
    dataset's batching is visible from both. Two arms, two different things
    each can see.
    """
    for source, name in (("title.akas.slice.tsv", "title.akas.tsv.gz"),):
        (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    catalog = PostgresBulkCatalogRepository(session)
    service = BootstrapService(
        PostgresImportRunRepository(session),
        catalog,
        session.flush,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        await service.import_dataset(
            IMDbTitleDataset(client, cache, batch_size=10), lambda rows: _written(catalog, rows)
        )

        batches: list[int] = []

        async def write(rows: Sequence[ImdbAka]) -> int:
            batches.append(len(rows))
            result = await catalog.replace_aliases(
                rows, imdb_ids=list(dict.fromkeys(row.imdb_id for row in rows))
            )
            return result.written

        run = await service.import_dataset(IMDbAkaDataset(client, cache, batch_size=1), write)

    assert run.status is ImportRunStatus.COMPLETED

    stored = await session.execute(
        text(
            "SELECT t.imdb_id, n.name, n.region, n.language FROM title_search_names n "
            "JOIN titles t ON t.id = n.title_id WHERE n.kind = 'alias' "
            "ORDER BY t.imdb_id, n.name"
        )
    )
    assert [tuple(row) for row in stored.all()] == [
        ("tt99000010", "A Synthetic Festival Title", "XWW", None),
        ("tt99000010", "A Synthetic Working Title", None, None),
        ("tt99000020", '"A Quoted Synthetic Alias"', "GB", None),
        ("tt99000020", "Un Long Métrage Synthétique", "FR", "fr"),
        ("tt99000030", "Uma Série Sintética", "BR", "pt"),
        ("tt99000030", "Une Série Synthétique", "FR", "fr"),
    ]
    # The damage first, the mechanism second, in that order deliberately:
    # against `group_of -> None` this file reports six one-row calls, which is
    # *why* three aliases went missing, and the missing aliases are *what*
    # went wrong. A case that asserts the mechanism first reports a batching
    # detail for a defect whose subject is lost rows.
    assert batches == [2, 2, 2], "each title reached the writer whole, in one call"


# --- the movielens phase, end to end over a synthetic archive --------------
#
# `tt99000020` is `SHAWSHANK`'s id in the shared bulk contract; the IMDb
# fixture slice this file already stages holds it. `links.csv` carries the
# digits bare, so the archive's own column is `99000020` and the adapter's
# `zfill(7)` produces the joined form.
_ML_ROOT = "ml-latest/"
_ML_LINKS = "\n".join(
    [
        "movieId,imdbId,tmdbId",
        "90000501,99000020,90000601",  # joins to a real skeleton movie
        "90000502,99000998,90000602",  # in links, in the genome, in no title
    ]
)
# **The real 1,128, not a convenient two.** `genome_scores.relevance` is
# declared `halfvec(1128)` and the cast in the staged upsert refuses anything
# else -- measured, `asyncpg.exceptions.DataError: expected 1128 dimensions,
# not 2` -- so a narrow fixture here would exercise everything except the one
# width production runs at. Generated rather than written out: 1,128 tag names
# and 2,256 score rows are a loop, and every value is invented.
_ML_TAGS = "\n".join(
    ["tagId,tag"] + [f"{tag},synthetic tag {tag}" for tag in range(1, GENOME_TAG_COUNT + 1)]
)
_ML_SCORES = "\n".join(
    ["movieId,tagId,relevance"]
    + [
        f"{movie},{tag},{round(((movie + tag) % 97) / 97.0, 5)}"
        for movie in (90000501, 90000502)
        for tag in range(1, GENOME_TAG_COUNT + 1)
    ]
)


def _genome_cache(cache: Path) -> Path:
    with zipfile.ZipFile(cache / "ml-latest.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{_ML_ROOT}links.csv", _ML_LINKS)
        archive.writestr(f"{_ML_ROOT}genome-tags.csv", _ML_TAGS)
        archive.writestr(f"{_ML_ROOT}genome-scores.csv", _ML_SCORES)
    return cache


async def _seed_catalog(session: AsyncSession, cache: Path) -> PostgresBulkCatalogRepository:
    catalog = PostgresBulkCatalogRepository(session)
    service = BootstrapService(
        PostgresImportRunRepository(session),
        catalog,
        session.flush,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        await service.import_dataset(
            IMDbTitleDataset(client, cache, batch_size=10), _write_titles(catalog)
        )
    return catalog


def _write_titles(
    catalog: PostgresBulkCatalogRepository,
) -> Callable[[Sequence[ImdbTitle]], Awaitable[int]]:
    async def write(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        return result.inserted + result.updated

    return write


async def test_the_genome_phase_joins_on_imdb_id_and_checkpoints_by_movie_run(
    session: AsyncSession, cache: Path
) -> None:
    """The whole `movielens` phase against real Postgres: archive -> adapter
    -> staged `real[]` -> `halfvec(1128)` -> a row keyed on the resolved
    `titles.id`.

    **This is the only place the `halfvec`-over-`COPY` path runs end to end.**
    A `halfvec` had never crossed asyncpg's binary `COPY` in this repository
    before M7; the staging column is `real[]` and the cast is in the
    `INSERT ... SELECT`, and if either half were wrong this case is what
    says so rather than a production bootstrap.

    `position` is a *movie run index*, not a line number: two movies, two
    completed runs, one of which joins to nothing.
    """
    catalog = await _seed_catalog(session, _genome_cache(cache))
    service = BootstrapService(
        PostgresImportRunRepository(session),
        catalog,
        session.flush,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = MovieLensGenomeDataset(client, cache, batch_size=10)
        revision = await dataset.revision()
        misses = 0

        async def write(rows: Sequence[GenomeVector]) -> int:
            nonlocal misses
            result = await catalog.upsert_genome_vectors(rows, revision=revision)
            misses += result.unmatched
            return result.inserted + result.updated

        run = await service.import_dataset(dataset, write, revision=revision)

    assert run.status is ImportRunStatus.COMPLETED
    # Two completed movie runs consumed; both yielded a vector, one of which
    # the catalog could not place.
    assert run.position == 2
    assert misses == 1

    stored = await session.execute(
        text(
            "SELECT g.genome_revision, t.imdb_id FROM genome_scores g "
            "JOIN titles t ON t.id = g.title_id"
        )
    )
    assert [tuple(row) for row in stored] == [('"fixture"', "tt99000020")]


async def test_the_tag_vocabulary_crosses_the_whole_phase_at_the_production_width(
    session: AsyncSession, cache: Path
) -> None:
    """`genome-tags.csv` -> adapter -> `replace_genome_tags` -> 1,128 rows of
    `genome_tags` -> `GenomeRepository.vocabulary`, at the width production
    runs at.

    **1,128 rather than a convenient three**, for the reason the fixture
    comment above gives about vectors and one more that belongs to this table:
    `ck_genome_tags_tag_id_in_vocabulary` bounds `tag_id` at exactly
    `GENOME_TAG_COUNT`, so a narrow fixture exercises the column's range check
    nowhere near its edge. The last lane is asserted by name for that reason.

    The two halves are asserted together, which is the property the third
    column exists for: the vocabulary reads back **only** when asked for the
    revision the vectors carry.
    """
    catalog = await _seed_catalog(session, _genome_cache(cache))
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = MovieLensGenomeDataset(client, cache)
        revision = await dataset.revision()
        vocabulary = await dataset.tag_vocabulary(revision)
        written = await catalog.replace_genome_tags(vocabulary, revision=revision)

    assert written == GENOME_TAG_COUNT
    names = await PostgresGenomeRepository(session).vocabulary(revision)
    assert names is not None
    assert len(names) == GENOME_TAG_COUNT
    assert names[0] == "synthetic tag 1"
    assert names[-1] == f"synthetic tag {GENOME_TAG_COUNT}"

    with pytest.raises(PortDataMalformed) as exc_info:
        await PostgresGenomeRepository(session).vocabulary("a-different-release")
    assert revision in str(exc_info.value)


async def test_a_vocabulary_one_lane_wider_than_the_schema_is_refused_by_the_column(
    session: AsyncSession, cache: Path
) -> None:
    """`ck_genome_tags_tag_id_in_vocabulary` is the ceiling on `tag_id`, and
    this is the case that proves it is a *constraint* rather than an encoder
    refusal.

    A vocabulary of `GENOME_TAG_COUNT + 1` tags is contiguous `1…n`, so
    `_refuse_partial_vocabulary` passes it, and it is the smallest input that
    reaches the column's own bound. Measured here rather than argued: the
    refusal arrives as a `RepositoryConflict` **naming the constraint**, which
    is what `integer` buys over `smallint` -- under `smallint` the same shape
    at 32,768 lanes is asyncpg's unnamed encoder `DataError` instead.
    """
    catalog = PostgresBulkCatalogRepository(session)
    too_wide = tuple(
        GenomeTag(tag_id=n, tag=f"synthetic tag {n}") for n in range(1, GENOME_TAG_COUNT + 2)
    )

    with pytest.raises(RepositoryConflict) as exc_info:
        await catalog.replace_genome_tags(too_wide, revision='"fixture"')

    assert exc_info.value.constraint == "ck_genome_tags_tag_id_in_vocabulary"
    # The SAVEPOINT held: the session is still usable, which is what lets a
    # caller record the failure it still has to report.
    assert await catalog.count_titles() == 0


async def test_a_failure_that_is_not_the_rows_propagates_untranslated(
    session: AsyncSession,
) -> None:
    """`if not is_row_refusal(exc): raise` -- the half of the `except` that
    every sibling repository has and that no case had exercised here.

    A missing table is SQLSTATE `42P01`, which is neither class `22` nor class
    `23`: it is the deployment being wrong, not the vocabulary, and a caller
    that cannot tell it from a refused row retries the one thing a retry
    cannot fix. Kills `raise RepositoryConflict(...)` unconditionally, which
    is the shape an `except DBAPIError` invites.

    The `DROP` is inside the test's own rolled-back transaction, so nothing
    outlives it.
    """
    catalog = PostgresBulkCatalogRepository(session)
    await session.execute(text("DROP TABLE genome_tags"))

    with pytest.raises(DBAPIError) as exc_info:
        await catalog.replace_genome_tags(
            (GenomeTag(tag_id=1, tag="zeppelins"),), revision='"fixture"'
        )

    assert not isinstance(exc_info.value, RepositoryConflict)
    assert exc_info.value.orig.__cause__.sqlstate == "42P01"  # type: ignore[union-attr]


async def test_a_replayed_genome_phase_reports_updates_and_the_same_coverage(
    session: AsyncSession, cache: Path
) -> None:
    """Trap 3 through the whole phase rather than through one statement, and
    the coverage report alongside it. Rowcount reports the sum, so without
    `xmax = 0` the second run of an operator's `--phase movielens` would be
    indistinguishable from the first."""
    catalog = await _seed_catalog(session, _genome_cache(cache))
    rows = [
        GenomeVector(
            movie_id=90000501,
            imdb_id="tt99000020",
            tmdb_id=None,
            relevance=(0.5, 0.25) + (0.0,) * (GENOME_TAG_COUNT - 2),
        ),
    ]
    # Straight through the repository: the phase's own batching is covered
    # above, and what this case is about is the second write of one row.
    first = await catalog.upsert_genome_vectors(rows, revision='"fixture"')
    again = await catalog.upsert_genome_vectors(rows, revision='"fixture"')

    assert (first.inserted, first.updated) == (1, 0)
    assert (again.inserted, again.updated) == (0, 1)

    coverage = await catalog.genome_coverage()
    assert coverage.with_vector == 1
    assert coverage.revisions == (('"fixture"', 1),)
    # A bootstrap-only catalog is all skeletons, so the enriched-tier
    # denominator is zero -- which is the state PRD 08 says every operator
    # command must survive, and the report must not divide by it.
    assert coverage.enriched == 0
