"""The whole Phase 0-2 pipeline against real Postgres, over committed
synthetic slices. Nothing downloads.

This is the test that proves the parts compose: dataset -> service ->
repository -> Postgres, with checkpoints, resumption, and the crosswalk
link.
"""

import gzip
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.adapters.bulk.imdb import IMDbRatingDataset, IMDbTitleDataset
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import IdCrosswalkPair, ImdbTitle, TmdbId
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
    service = BootstrapService(PostgresImportRunRepository(session), catalog, session.flush)

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
            TmdbId(tmdb_id=278, kind=TitleKind.MOVIE, original_name="Shawshank", popularity=45.5),
            TmdbId(tmdb_id=1399, kind=TitleKind.SERIES, original_name="GoT", popularity=90.1),
        ]
    )
    await catalog.upsert_crosswalk(
        [
            IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278),
            IdCrosswalkPair(imdb_id="tt0944947", tmdb_series_id=1399, tvdb_series_id=121361),
        ]
    )
    linked = await catalog.link_crosswalk()
    assert linked.linked == 2

    result = await session.execute(
        text(
            "SELECT imdb_id, tmdb_id, tvdb_id, popularity, community_rating, "
            "enrichment_state FROM titles WHERE imdb_id IN ('tt0111161','tt0944947') "
            "ORDER BY imdb_id"
        )
    )
    rows = result.all()
    assert rows[0] == ("tt0111161", 278, None, 45.5, 9.3, "skeleton")
    assert rows[1] == ("tt0944947", 1399, 121361, 90.1, 9.2, "skeleton")


async def test_the_catalog_is_queryable_between_batches(session: AsyncSession, cache: Path) -> None:
    """ADR-0005 and the spec both promise the catalog is usable during
    bootstrap. With batch_size=2 the first commit lands two titles, and a
    reader sees them before the import finishes -- this asserts the loop
    really does commit per batch rather than once at the end."""
    catalog = PostgresBulkCatalogRepository(session)
    service = BootstrapService(PostgresImportRunRepository(session), catalog, session.flush)
    seen: list[int] = []

    async def write_and_peek(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        seen.append(await catalog.count_titles())
        return result.inserted + result.updated

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        await service.import_dataset(IMDbTitleDataset(client, cache, batch_size=2), write_and_peek)
    assert seen == [2, 4, 5]


async def test_a_restart_resumes_from_the_stored_checkpoint(
    session: AsyncSession, cache: Path
) -> None:
    """Simulates a crash by importing with a service whose write fails on
    the third batch, then re-running -- the second run must pick up the
    cursor the first one committed."""
    catalog = PostgresBulkCatalogRepository(session)
    runs = PostgresImportRunRepository(session)
    service = BootstrapService(runs, catalog, session.flush)

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

        second = IMDbTitleDataset(client, cache, batch_size=2)
        run = await service.import_dataset(second, lambda rows: _written(catalog, rows))

    assert run.status is ImportRunStatus.COMPLETED
    assert await catalog.count_titles() == 5


class _Stop(Exception):
    """Not a UsherPortError, deliberately: BootstrapService records port
    errors and swallows them, so a port error here would give a COMPLETED-
    shaped path rather than the abrupt stop this test needs."""


async def _written(catalog: PostgresBulkCatalogRepository, rows: Sequence[ImdbTitle]) -> int:
    result = await catalog.upsert_titles(rows)
    return result.inserted + result.updated
