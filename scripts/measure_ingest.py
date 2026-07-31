"""Measure a full reconcile at library scale.

Answers the one question the test suite cannot: what does a walk of a real
library *cost*, in statements and in wall time? **Not a test.** It seeds a
`FakeEmbyServer` with tens of thousands of synthetic items and writes them
to a real database, so it never runs in CI and never runs against a real
catalog -- it truncates the tables it uses between passes.

    docker run -d --name usher-measure -e POSTGRES_USER=usher \\
      -e POSTGRES_PASSWORD=usher -e POSTGRES_DB=usher -p 55432:5432 \\
      pgvector/pgvector:pg17
    export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:55432/usher"
    export USHER_SECRET_KEY="$(openssl rand -hex 32)"
    uv run alembic upgrade head
    uv run python scripts/measure_ingest.py --items 50000

**The number that matters is statements per item.** A correct
implementation is a small constant -- one staged `COPY` plus a handful of
set-based statements per batch, so at a batch size of 1,000 it is well under
0.05. Anything approaching 1.0 means something is per-item, and at 1,126,674
items that is the difference between a walk that finishes overnight and one
that does not.

Two passes, because they answer different questions and the first one
flatters the second. **Pass 1 is a cold catalog**: every movie and series is
new, so `MatchService._create_stub` -- the one call in the pipeline that is
not set-based -- fires once per new *title*. **Pass 2 is the nightly walk**:
everything matches what pass 1 stored, no stub is created, and the count
collapses to the batch-level constant. The shape is the measured library's:
94,438 movies, 32,409 series, 999,827 episodes, so 89% episodes by default
-- and an episode never walks the match ladder at all, which is why the
per-title cost is bounded by 11% of the library rather than by all of it.

`kill -9 "$(cat pidfile)"` does not stop this if you background it: `uv run`
forks a child rather than exec-replacing itself, so kill the whole process
group (or `pgrep -P` the wrapper) or an orphaned writer keeps committing
underneath your next measurement. That contaminated an M2 run.
"""

import argparse
import asyncio
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

import httpx
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes.emby_server import FakeEmbyServer

from usher.adapters.emby.adapter import EmbyAdapter
from usher.config import get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.staging import raw_connection
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind
from usher.ports.credentials import SourceCredentials
from usher.ports.ingest import WatchStateMerge
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService

# The measured deployment: 94,438 movies, 32,409 series, 999,827 episodes of
# 1,126,674 items. Episodes are 88.7%, series 2.9%, movies 8.4%.
EPISODE_SHARE = 0.887
SERIES_SHARE = 0.029
CHANGED_AT = datetime(2026, 7, 1, tzinfo=UTC)


def _capture(
    predicate: Callable[[str], bool],
) -> tuple[list[tuple[str, Sequence[object]]], Callable[[], None]]:
    """Record (statement, parameters) for statements matching `predicate`,
    and the callable that stops recording."""
    seen: list[tuple[str, Sequence[object]]] = []

    def record(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if predicate(statement):
            seen.append((statement, cast(Sequence[object], parameters)))

    event.listen(Engine, "before_cursor_execute", record)
    return seen, lambda: event.remove(Engine, "before_cursor_execute", record)


@contextmanager
def counted() -> Iterator[list[str]]:
    """Every statement SQLAlchemy issues. A `COPY` is invisible here --
    `copy_records_to_table` runs on the raw asyncpg connection -- which is
    the point: a `COPY` is one command however many records stream through
    it."""
    seen: list[str] = []

    def record(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def library(count: int) -> list[SourceItem]:
    """`count` items in the measured library's proportions."""
    series_count = max(1, int(count * SERIES_SHARE))
    episode_count = int(count * EPISODE_SHARE)
    movie_count = count - series_count - episode_count
    items = [
        SourceItem(
            external_id=f"series-{index}",
            name=f"Series {index}",
            kind=SourceItemKind.SERIES,
            year=2000 + index % 25,
            provider_ids={"tvdb": str(500_000 + index)},
        )
        for index in range(series_count)
    ]
    items.extend(
        SourceItem(
            external_id=f"movie-{index}",
            name=f"Movie {index}",
            kind=SourceItemKind.MOVIE,
            year=1950 + index % 75,
            provider_ids={"tmdb": str(700_000 + index)},
            container="mkv",
        )
        for index in range(movie_count)
    )
    items.extend(
        SourceItem(
            external_id=f"episode-{index}",
            name=f"Episode {index}",
            kind=SourceItemKind.EPISODE,
            # An episode's own ids, never its series' -- the shape a live
            # Emby episode really has, and the reason an episode must never
            # reach the match ladder.
            provider_ids={"imdb": f"tt{2000000 + index}"},
            container="mkv",
            series_external_id=f"series-{index % series_count}",
            season_number=1 + (index // 500) % 12,
            episode_number=1 + index % 500,
        )
        for index in range(episode_count)
    )
    return items


def _service(session: AsyncSession, *, batch_size: int) -> ReconcileService:
    matching = PostgresTitleMatchRepository(session)
    media_items = PostgresMediaItemRepository(session)
    queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=30.0)
    return ReconcileService(
        ingest=IngestService(
            matcher=MatchService(
                titles=PostgresTitleRepository(session), matching=matching, queue=queue
            ),
            matching=matching,
            media_items=media_items,
            episodes=PostgresEpisodeRepository(session),
            queue=queue,
        ),
        media_items=media_items,
        runs=PostgresSyncRunRepository(session),
        commit=session.commit,
        batch_size=batch_size,
    )


async def _walk(
    factory: async_sessionmaker[AsyncSession],
    source: Source,
    emby: FakeEmbyServer,
    *,
    batch_size: int,
    label: str,
    items: int,
) -> SyncRun:
    async with factory() as session:
        client = httpx.AsyncClient(transport=emby.transport(), base_url=source.base_url)
        adapter = EmbyAdapter(
            source,
            SourceCredentials(username=emby.username, password=SecretStr(emby.password)),
            client=client,
            page_size=200,
        )
        try:
            started = time.perf_counter()
            with counted() as statements:
                run = await _service(session, batch_size=batch_size).reconcile(
                    source, SyncRunKind.FULL, adapter
                )
            elapsed = time.perf_counter() - started
        finally:
            await adapter.aclose()
            await client.aclose()

    # `TitleRepository.add` wraps its INSERT in a SAVEPOINT (it catches
    # `IntegrityError` and must leave the session usable), so a stub really
    # costs three statements, not one. Counting only the INSERT would
    # under-report the one non-set-based path in the pipeline by 3x, which
    # is exactly the sort of flattering arithmetic this script exists to
    # avoid.
    inserts = sum(1 for one in statements if one.lstrip().upper().startswith("INSERT INTO TITLES"))
    stub_cost = sum(
        1
        for one in statements
        if one.lstrip().upper().startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "INSERT INTO TITLES"))
    )
    batch_level = len(statements) - inserts * 3
    print(
        f"\n{label}\n"
        f"  status                 {run.status.value}\n"
        f"  items seen             {run.items_seen}\n"
        f"  matched / unmatched    {run.items_matched} / {run.items_unmatched}\n"
        f"  wall time              {elapsed:.1f} s\n"
        f"  items / second         {run.items_seen / elapsed:,.0f}\n"
        f"  statements             {len(statements)}\n"
        f"  statements per item    {len(statements) / max(items, 1):.4f}\n"
        f"  new titles stubbed     {inserts}  (bounded by new titles, never by items)\n"
        f"  statements per stub    {inserts and (inserts * 3) / inserts:.1f}"
        f"  (SAVEPOINT + INSERT + RELEASE; {stub_cost} savepoint-wrapped in all)\n"
        f"  batch-level statements {batch_level}"
        f" = {batch_level / max(items, 1):.4f} per item"
    )
    return run


async def measure(items: int, batch_size: int) -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "TRUNCATE media_items, episodes, seasons, jobs, sync_runs, "
                    "watch_states, titles, sources CASCADE"
                )
            )
            source = Source(
                kind=SourceKind.EMBY,
                name="Measurement Source",
                base_url="https://emby.invalid",
                credentials_ref=f"ref-{uuid.uuid4()}",
                device_id=str(new_id()),
            )
            await PostgresSourceRepository(session).add(source)
            await session.commit()

        emby = FakeEmbyServer(page_size=200)
        seeded = library(items)
        for item in seeded:
            emby.add_item(item, CHANGED_AT)
        print(
            f"seeded {len(seeded)} items "
            f"({sum(1 for one in seeded if one.kind is SourceItemKind.EPISODE)} episodes, "
            f"{sum(1 for one in seeded if one.kind is SourceItemKind.SERIES)} series, "
            f"{sum(1 for one in seeded if one.kind is SourceItemKind.MOVIE)} movies), "
            f"batch size {batch_size}"
        )

        await _walk(
            factory,
            source,
            emby,
            batch_size=batch_size,
            label="pass 1 -- cold catalog (every title is new)",
            items=items,
        )
        await _walk(
            factory,
            source,
            emby,
            batch_size=batch_size,
            label="pass 2 -- the nightly walk (everything already matches)",
            items=items,
        )
    finally:
        await engine.dispose()


async def _plan_of(session: AsyncSession, statement: str, parameters: Sequence[object]) -> str:
    """`EXPLAIN (ANALYZE)` the statement **as the driver received it**.

    Captured off `before_cursor_execute` rather than transcribed: two
    earlier tasks in this project asserted on the plan of a hand-copied
    lookalike and both were replaced, because a copy drifts from the
    repository it claims to describe and then reads like coverage. What
    the hook hands over is already compiled to asyncpg's `$1` placeholders,
    so it goes back to asyncpg rather than through `text()`.
    """
    driver = await raw_connection(session)
    rows = await driver.fetch("EXPLAIN (ANALYZE, BUFFERS) " + statement, *tuple(parameters))
    return "\n".join("    " + str(row[0]) for row in rows)


async def scale(rows: int) -> None:
    """The four scale risks Groups A-E flagged and could not measure, at the
    scale that makes them real.

    Each seeds its own population directly (a `generate_series` insert, not a
    walk -- the point is the read, not how the rows got there) and then plans
    the statement the *repository* issued.
    """
    settings = get_settings()
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(
                text("TRUNCATE media_items, watch_states, jobs, users, titles, sources CASCADE")
            )
            source = Source(
                kind=SourceKind.EMBY,
                name="Scale Source",
                base_url="https://emby.invalid",
                credentials_ref=f"ref-{uuid.uuid4()}",
                device_id=str(new_id()),
            )
            await PostgresSourceRepository(session).add(source)
            await session.execute(
                text(
                    "INSERT INTO media_items "
                    "(id, source_id, external_id, last_seen_at, available) "
                    "SELECT gen_random_uuid(), :source_id, 'item-' || g, "
                    "  now() - (g % 7) * interval '1 day', true "
                    "FROM generate_series(1, :rows) g"
                ),
                {"source_id": source.id, "rows": rows},
            )
            await session.execute(text("ANALYZE media_items"))
            await session.commit()
            print(f"seeded {rows} media_items on one source, all unmatched\n")

            media_items = PostgresMediaItemRepository(session)

            print("1. list_unmatched pages with OFFSET")
            for offset in (0, rows // 2, max(rows - 100, 0)):
                started = time.perf_counter()
                page = await media_items.list_unmatched(source.id, limit=100, offset=offset)
                print(
                    f"    offset {offset:>9}: {(time.perf_counter() - started) * 1000:8.1f} ms"
                    f"  ({len(page)} rows)"
                )

            print("\n2. the availability sweep")
            captured, stop = _capture(
                lambda one: "count(*) FILTER" in one or "SET available = false" in one
            )
            try:
                await media_items.mark_unseen_unavailable(
                    source.id, seen_since=datetime.now(UTC), max_retract_fraction=1.0
                )
            finally:
                stop()
            for statement, parameters in captured:
                print(await _plan_of(session, statement, parameters))
            await session.rollback()

            print("\n3. merge_from_source at watch-state scale")
            user_id = new_id()
            await session.execute(
                text("INSERT INTO users (id, name) VALUES (:id, 'scale')"), {"id": user_id}
            )
            await session.execute(
                text(
                    "INSERT INTO titles (id, kind, name, sort_name, enrichment_state) "
                    "SELECT gen_random_uuid(), 'movie', 'T' || g, 'T' || g, 'skeleton' "
                    "FROM generate_series(1, :rows) g"
                ),
                {"rows": rows},
            )
            await session.execute(
                text(
                    "INSERT INTO watch_states "
                    "(id, user_id, title_id, position_seconds, played, play_count, origin, "
                    " updated_at) "
                    "SELECT gen_random_uuid(), :user_id, id, 10, false, 0, 'source', "
                    "  now() FROM titles"
                ),
                {"user_id": user_id},
            )
            await session.execute(text("ANALYZE watch_states"))
            await session.commit()
            # A realistic batch, not one row: `WatchStateSyncService` merges
            # `sync_batch_size` states at a time, and a one-row staging table
            # is exactly the shape that flatters a nested loop. The flagged
            # risk was that the join goes hash + seq scan once the staged
            # side is big enough to be worth one.
            targets = [
                row[0]
                for row in (await session.execute(text("SELECT id FROM titles LIMIT 1000"))).all()
            ]
            captured, stop = _capture(lambda one: "UPDATE watch_states" in one)
            try:
                await PostgresWatchStateRepository(session).merge_from_source(
                    [
                        WatchStateMerge(
                            user_id=user_id,
                            title_id=target,
                            episode_id=None,
                            position_seconds=99,
                            played=True,
                            runtime_seconds=None,
                            observed_at=datetime.now(UTC),
                            play_count=None,
                            last_played_at=None,
                        )
                        for target in targets
                    ]
                )
            finally:
                stop()
            print(f"    watch_states rows: {rows}; merged batch: {len(targets)}")
            print(await _plan_of(session, *captured[0]))
            await session.rollback()

            print("\n4. the claim scan behind a wall of backed-off jobs")
            await session.execute(
                text(
                    "INSERT INTO jobs (id, kind, key, priority, status, attempts, run_after, "
                    "  created_at, updated_at) "
                    "SELECT gen_random_uuid(), 'match', 'k' || g, 20, 'pending', 1, "
                    "  now() + interval '1 hour', now(), now() "
                    "FROM generate_series(1, :rows) g"
                ),
                {"rows": rows},
            )
            await session.execute(
                text(
                    "INSERT INTO jobs (id, kind, key, priority, status, attempts, created_at, "
                    "  updated_at) VALUES (gen_random_uuid(), 'match', 'runnable', 20, "
                    "  'pending', 0, now(), now())"
                )
            )
            await session.execute(text("ANALYZE jobs"))
            await session.commit()
            queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=30.0)
            captured, stop = _capture(lambda one: "FOR UPDATE SKIP LOCKED" in one)
            try:
                started = time.perf_counter()
                claimed = await queue.claim([JobKind.MATCH], limit=20)
                elapsed = time.perf_counter() - started
            finally:
                stop()
            print(
                f"    {rows} backed-off + 1 runnable; claimed {len(claimed)} "
                f"in {elapsed * 1000:.1f} ms"
            )
            print(await _plan_of(session, *captured[0]))
            await session.rollback()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=50_000)
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument(
        "--scale",
        type=int,
        default=None,
        metavar="ROWS",
        help="skip the walk; probe the flagged scale risks at ROWS rows instead",
    )
    args = parser.parse_args()
    if args.scale is not None:
        asyncio.run(scale(args.scale))
        return
    asyncio.run(measure(args.items, args.batch_size))


if __name__ == "__main__":
    main()
