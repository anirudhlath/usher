"""Measure a real IMDb load with and without index suspension.

Answers the question PRD 04's Phase 0 left open. **Not a test**: it
downloads the real 214 MiB `title.basics.tsv.gz`, so it never runs in CI.
Run it once, by hand, and record the numbers in PRD 04.

    export USHER_DATABASE_URL=... USHER_SECRET_KEY=...
    uv run alembic upgrade head
    uv run python scripts/measure_bulk_load.py

The database is truncated between passes, so run it against a scratch
database, never a real catalog.
"""

import asyncio
import time
from collections.abc import Sequence

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.adapters.bulk.imdb import IMDbTitleDataset
from usher.config import get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.ports.bulk import ImdbTitle
from usher.services.bootstrap import BootstrapService

_INDEX_SIZES = text("""
    SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid))
    FROM pg_stat_user_indexes WHERE relname = 'titles' ORDER BY indexrelname
""")


async def _load(factory: async_sessionmaker[AsyncSession], *, suspend: bool) -> float:
    async with factory() as session:
        await session.execute(text("TRUNCATE titles, import_runs CASCADE"))
        await session.commit()
        catalog = PostgresBulkCatalogRepository(session)
        service = BootstrapService(PostgresImportRunRepository(session), catalog, session.commit)
        settings = get_settings()

        async def write(rows: Sequence[ImdbTitle]) -> int:
            result = await catalog.upsert_titles(rows)
            return result.inserted + result.updated

        started = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=60.0, headers={"User-Agent": settings.bulk_user_agent}
        ) as client:
            dataset = IMDbTitleDataset(
                client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
            )
            if suspend:
                async with catalog.bulk_load_window():
                    await service.import_dataset(dataset, write)
            else:
                await service.import_dataset(dataset, write)
        elapsed = time.perf_counter() - started
        count = await catalog.count_titles()
        sizes = (await session.execute(_INDEX_SIZES)).all()
    label = "suspended" if suspend else "kept"
    print(f"\nindexes {label}: {elapsed:.1f}s for {count} titles")
    for name, size in sizes:
        print(f"    {name:<28} {size}")
    return elapsed


async def main() -> None:
    engine = build_engine(get_settings().database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        # Suspended first: the second pass then starts from a non-empty
        # `titles`... which would make bulk_load_window decline. TRUNCATE at
        # the top of _load is what keeps both passes comparable.
        with_suspension = await _load(factory, suspend=True)
        without = await _load(factory, suspend=False)
    finally:
        await engine.dispose()
    saved = without - with_suspension
    print(
        f"\nsuspending the two non-unique btrees saved {saved:.1f}s "
        f"({100 * saved / without:.1f}% of {without:.1f}s)"
    )


asyncio.run(main())
