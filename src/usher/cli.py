"""Command-line composition root: `python -m usher <command>`.

The second composition root alongside `api/`. It is the only module allowed
to construct adapters, repositories, and services together, which is why
`pyproject.toml` carries a contract forbidding anything from importing it.

PRD 08 says first run "offers bootstrap through the admin API -- it does not
start a multi-hour download unprompted". The admin API arrives with the rest
of the HTTP surface in M9; this CLI is that trigger until then, and it has
the same property: nothing downloads unless an operator asks.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence

import httpx
from loguru import logger

from usher.adapters.bulk.imdb import IMDbRatingDataset, IMDbTitleDataset
from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.enums import TitleKind
from usher.ports.bulk import ImdbTitle
from usher.ports.repository import BulkCatalogRepository
from usher.services.bootstrap import BootstrapService
from usher.telemetry import configure_telemetry

PHASES = ("imdb", "tmdb-ids", "crosswalk", "all")


def _titles_writer(
    catalog: BulkCatalogRepository,
) -> Callable[[Sequence[ImdbTitle]], Awaitable[int]]:
    """Adapts `upsert_titles`' BulkWriteResult to the `-> int` the service
    wants. The other three repository methods already return `int`, so only
    this one needs a wrapper."""

    async def write(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        return result.inserted + result.updated

    return write


async def _bootstrap(settings: Settings, phase: str) -> None:
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    # One client for every dataset: connection reuse across the whole run, and
    # one place that owns closing it. Each adapter's `aclose` is deliberately
    # a no-op for exactly this reason -- closing a shared client from inside
    # one dataset would break its siblings.
    client = httpx.AsyncClient(timeout=60.0, headers={"User-Agent": settings.bulk_user_agent})
    try:
        async with factory() as session:
            catalog = PostgresBulkCatalogRepository(session)
            service = BootstrapService(
                PostgresImportRunRepository(session), catalog, session.commit
            )
            if phase in ("imdb", "all"):
                # The window wraps both IMDb passes, not each separately: the
                # ratings pass writes to the same table, and rebuilding the two
                # ordering indexes between them would pay the cost twice.
                async with catalog.bulk_load_window():
                    await service.import_dataset(
                        IMDbTitleDataset(
                            client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
                        ),
                        _titles_writer(catalog),
                    )
                    await service.import_dataset(
                        IMDbRatingDataset(
                            client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
                        ),
                        catalog.apply_ratings,
                    )
            if phase in ("tmdb-ids", "all"):
                for kind in (TitleKind.MOVIE, TitleKind.SERIES):
                    await service.import_dataset(
                        TMDbIdDataset(
                            client,
                            settings.bulk_data_dir,
                            kind=kind,
                            batch_size=settings.bulk_batch_size,
                        ),
                        catalog.upsert_tmdb_ids,
                    )
            if phase in ("crosswalk", "all"):
                await service.import_dataset(
                    WikidataCrosswalkDataset(
                        client,
                        user_agent=settings.bulk_user_agent,
                        endpoint=settings.wikidata_endpoint,
                        batch_size=settings.bulk_batch_size,
                    ),
                    catalog.upsert_crosswalk,
                )
                await service.link_crosswalk()
            logger.info("catalog now holds {count} titles", count=await catalog.count_titles())
    finally:
        await client.aclose()
        await engine.dispose()


async def _status(settings: Settings) -> None:
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            runs = await PostgresImportRunRepository(session).list_runs()
            catalog_size = await PostgresBulkCatalogRepository(session).count_titles()
    finally:
        await engine.dispose()
    # Printed, not logged: this is a report an operator asked for, and routing
    # it through the JSON log sink would make it unreadable at a terminal.
    print(f"titles in catalog: {catalog_size}")
    if not runs:
        print("no import has been run yet")
        return
    for run in runs:
        print(
            f"{run.dataset:<24} {run.status.value:<10} "
            f"position={run.position} seen={run.rows_seen} written={run.rows_written}"
            + (f" error={run.error}" if run.error else "")
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usher")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP server (the default with no arguments)")
    bootstrap = sub.add_parser("bootstrap", help="import bulk catalog datasets")
    bootstrap.add_argument("--phase", choices=PHASES, default="all")
    sub.add_parser("bootstrap-status", help="report import progress and catalog size")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    # `argv or ["serve"]`: `python -m usher` with no arguments must keep
    # starting the server, because that is exactly what the container's CMD
    # runs (`alembic upgrade head && exec python -m usher`). Adding
    # subcommands must not change that.
    args = build_parser().parse_args(list(argv) if argv else ["serve"])
    settings = get_settings()
    configure_telemetry(settings)
    if args.command == "bootstrap":
        asyncio.run(_bootstrap(settings, args.phase))
    elif args.command == "bootstrap-status":
        asyncio.run(_status(settings))
    else:
        # Imported here, not at module scope: uvicorn.run blocks, and nothing
        # about the bootstrap path should pay for importing the server.
        import uvicorn

        uvicorn.run(
            "usher.api.app:create_app", factory=True, host=settings.host, port=settings.port
        )
