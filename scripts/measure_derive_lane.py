"""Price `derive` at 1, 2, 4 and 8 in flight on one pool. **Zero live requests.**

**Not a test.** It starts a throwaway `pgvector/pgvector:pg17` container, runs
the real Alembic chain into it, seeds a synthetic catalog and throws the whole
container away afterwards. It touches **no** third party: `derive` is pure
Postgres by construction -- a JSONB read and writes through five repositories,
with `DeriveService` holding a `MetadataProvider` only for `to_derivation`,
which is synchronous and opens no socket. The dev database is not touched
either; nothing here connects to `USHER_DATABASE_URL`.

    uv run python scripts/measure_derive_lane.py
    uv run python scripts/measure_derive_lane.py --titles 240 --ladder 1,2,4,8

## The run this exists to be

`usher.services.jobs.KIND_CONCURRENCY[JobKind.DERIVE]` is **4**, and its own
comment says what it is and is not:

> ⚠️ **Not measured** [...] it is derived from a *budget* rather than from a
> throughput: derivation is pure Postgres [...] so its ceiling is what the
> connection pool can serve without starving the API in the in-process lane --
> four of `Settings.db_pool_size`'s twenty. **The measurement that would replace
> it is derive jobs/s against 1, 2, 4 and 8 in flight on one pool; nothing in
> this repository has run it.**

This is that run, spelled from that sentence.

## What is seeded, and why it is synthetic

Each title gets one `raw_payloads` row built from the committed
`tests/fixtures/tmdb/movie.json` -- a **shape-recorded** fixture, per
`.claude/rules/fixtures-and-fakes.md` -- re-keyed to a distinct `tmdb_id` and
given synthetic cast, crew and image arrays at the sizes the shipped mapper
caps them to. Synthetic because the alternative is shipping third-party
metadata, which this repository refuses (`CLAUDE.md`: *"Ship importers, never
data"*), and because the question is a *throughput* one: what matters is that
every job does a realistic amount of work, not that the names are real.

⚠️ **So the absolute jobs/s is a property of this seed and this box, not of any
real catalog**, and the number worth carrying is the **shape of the curve** --
what the second, fourth and eighth in-flight job add. Stated here rather than
discovered by whoever quotes the absolute number at a different catalog.

## One pool, which is the whole point

Every coroutine gets its **own session** (`AsyncSession` is explicitly not
concurrency-safe, which is `.claude/rules/rows-and-genome.md`'s own finding and
why row building is sequential) from **one** `async_sessionmaker` over **one**
engine -- exactly the shape `usher work` has, where the worker opens a scope
per claim and per job against the process's single pool. `Settings.db_pool_size`
defaults to 20 with `db_max_overflow` 10, and the ladder deliberately runs to 8
so the curve is visible on both sides of the shipped 4.

The bar is `/var/tmp/m10-gate/BAR-S7.md`, whose `sha256` is re-computed here and
printed, so an edit made after a number was seen shows up in the log.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import statistics
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_source_latency import _sha256

DEFAULT_BAR = Path("/var/tmp/m10-gate/BAR-S7.md")  # noqa: S108 -- durable, not tmpfs

#: The ladder the `jobs.py` comment names, verbatim: "1, 2, 4 and 8 in flight".
DEFAULT_LADDER: tuple[int, ...] = (1, 2, 4, 8)

_PROVIDER = "tmdb"


@dataclass(frozen=True)
class Rung:
    concurrency: int
    seconds: float
    jobs: int
    latencies: list[float]

    @property
    def rps(self) -> float:
        return self.jobs / self.seconds if self.seconds > 0 else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def mean(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0


def synthetic_payload(base: dict[str, Any], tmdb_id: int) -> dict[str, Any]:
    """The recorded movie shape at a fresh id, with the arrays the mapper reads.

    Sizes are the ones the shipped mapper caps to (`_CAST_LIMIT` is 50) so a
    job does the amount of work a real enriched title would, rather than the
    amount a two-element fixture would.
    """
    payload = copy.deepcopy(base)
    payload["id"] = tmdb_id
    payload["credits"] = {
        "cast": [
            {
                "id": tmdb_id * 1000 + n,
                "name": f"Cast Person {n}",
                "character": f"Character {n}",
                "order": n,
                "known_for_department": "Acting",
                "profile_path": f"/c{tmdb_id}_{n}.jpg",
            }
            for n in range(50)
        ],
        "crew": [
            {
                "id": tmdb_id * 1000 + 500 + n,
                "name": f"Crew Person {n}",
                "job": ["Director", "Producer", "Writer"][n % 3],
                "department": "Directing",
                "known_for_department": "Directing",
                "profile_path": f"/w{tmdb_id}_{n}.jpg",
            }
            for n in range(10)
        ],
    }
    payload["images"] = {
        "posters": [
            {"file_path": f"/p{tmdb_id}_{n}.jpg", "width": 500, "height": 750, "iso_639_1": "en"}
            for n in range(6)
        ],
        "backdrops": [
            {"file_path": f"/b{tmdb_id}_{n}.jpg", "width": 1920, "height": 1080, "iso_639_1": None}
            for n in range(6)
        ],
    }
    payload["belongs_to_collection"] = {
        "id": 9_000_000 + (tmdb_id % 50),
        "name": f"Collection {tmdb_id % 50}",
        "poster_path": f"/col{tmdb_id % 50}.jpg",
    }
    return payload


async def seed(url: str, *, titles: int, fixture: Path, offset: int = 0) -> list[uuid.UUID]:
    """`titles` enriched titles, each with one cached payload. Returns their ids.

    `offset` moves the synthetic `tmdb_id` range, because `ix_titles_tmdb_id_kind`
    is unique and every rung seeds a fresh population -- see `main`'s note on
    why a rung must not derive titles a previous rung already derived.
    """
    from sqlalchemy import text

    from usher.db.base import build_engine

    base = json.loads(fixture.read_text(encoding="utf-8"))
    engine = build_engine(url)
    ids: list[uuid.UUID] = []
    try:
        async with engine.begin() as conn:
            for index in range(titles):
                tmdb_id = 800_000 + offset + index
                title_id = uuid.uuid4()
                ids.append(title_id)
                await conn.execute(
                    text(
                        "INSERT INTO titles (id, kind, name, sort_name, tmdb_id, "
                        "enrichment_state, created_at, updated_at) VALUES "
                        "(:id, 'movie', :name, :sort, :tmdb, 'enriched', now(), now())"
                    ),
                    {
                        "id": title_id,
                        "name": f"Measured Title {index}",
                        "sort": f"measured title {index}",
                        "tmdb": tmdb_id,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO raw_payloads (id, provider, kind, reference, payload, "
                        "fetched_at) VALUES (:id, :provider, 'movie', :pid, "
                        "CAST(:payload AS jsonb), now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "provider": _PROVIDER,
                        "pid": str(tmdb_id),
                        "payload": json.dumps(synthetic_payload(base, tmdb_id)),
                    },
                )
    finally:
        await engine.dispose()
    return ids


async def run_rung(url: str, title_ids: Sequence[uuid.UUID], *, concurrency: int) -> Rung:
    """`concurrency` derive jobs in flight, each on its own session, one pool.

    One engine per rung rather than one for the whole run: a pool that has
    already grown to eight connections is not the pool a deployment at
    concurrency one has, and reusing it would let the first rung subsidise the
    last. Each rung therefore pays its own connection cost, which is the
    comparison the shipped worker actually faces.
    """
    from usher.adapters.tmdb.provider import TmdbMetadataProvider
    from usher.composition import build_derive_service, build_pipeline
    from usher.config import Settings
    from usher.db.base import build_engine, build_session_factory
    from usher.ports.events import NullEventPublisher

    settings = Settings(
        database_url=url,  # type: ignore[arg-type]
        secret_key="x" * 32,  # type: ignore[arg-type]
    )
    engine = build_engine(url, pool_size=settings.db_pool_size)
    sessions = build_session_factory(engine)
    # **`__new__` without `__init__`, and it is safe for a reason that was read
    # rather than assumed.** `DeriveService` holds the provider for
    # `to_derivation` alone -- `build_derive_service`'s own docstring says so,
    # and `test_deriving_makes_no_provider_fetch` asserts it over a provider
    # whose `fetch` raises. `TmdbMetadataProvider.to_derivation` reads **no**
    # instance attribute: it delegates entirely to `mapping.people_and_credits`,
    # `collection_from_payload` and `images_from_payload`, and passes the
    # module-level `PROVIDER_NAME`. So an instance with no `_client`, `_region`
    # or `_today` is complete for this call and cannot reach a socket, which is
    # a stronger guarantee than a stub honouring the same contract.
    #
    # The alternative -- a real `TmdbClient` over a dead transport -- would need
    # an api key in this process for a method that never touches one.
    provider = TmdbMetadataProvider.__new__(TmdbMetadataProvider)
    gate = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    lock = asyncio.Lock()

    async def one(title_id: uuid.UUID) -> None:
        async with gate:
            started = time.monotonic()
            async with sessions() as session:
                pipeline = build_pipeline(
                    session, settings, events=NullEventPublisher(), provider=provider
                )
                service = build_derive_service(pipeline, provider)
                await service.derive(title_id)
            elapsed = time.monotonic() - started
        async with lock:
            latencies.append(elapsed)

    try:
        started = time.monotonic()
        await asyncio.gather(*(one(one_id) for one_id in title_ids))
        elapsed = time.monotonic() - started
    finally:
        await engine.dispose()
    return Rung(concurrency=concurrency, seconds=elapsed, jobs=len(title_ids), latencies=latencies)


async def _probe_one_job(url: str, title_id: uuid.UUID) -> dict[str, int]:
    """Run one derive and count what it actually wrote into each table."""
    from sqlalchemy import text

    from usher.db.base import build_engine

    await run_rung(url, [title_id], concurrency=1)
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            counts = {}
            for table, column in (
                ("people", None),
                ("credits", "title_id"),
                ("collections", None),
                ("images", "title_id"),
            ):
                where = f" WHERE {column} = :id" if column else ""
                row = await conn.execute(
                    text(f"SELECT count(*) FROM {table}{where}"),  # noqa: S608 -- fixed names
                    {"id": title_id} if column else {},
                )
                counts[table] = int(row.scalar_one())
            return counts
    finally:
        await engine.dispose()


def _upgrade_head(url: str) -> None:
    """The real chain, driven the way `tests/integration/conftest.py` drives it.

    🔴 **`config.set_main_option("sqlalchemy.url", ...)` is a silent no-op
    here**, and it cost a run: `alembic/env.py` reads the URL from
    `usher.config.get_settings()` rather than from `alembic.ini` -- deliberately,
    and its own docstring says so -- so the migration ran against whatever
    `USHER_DATABASE_URL` happened to hold, the container stayed empty, and the
    first `INSERT` answered `relation "titles" does not exist`. The env vars are
    what `env.py` reads, so the env vars are what get set.
    """
    import os

    from alembic.command import upgrade
    from alembic.config import Config

    from usher.config import get_settings

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    saved = {key: value for key, value in os.environ.items() if key.startswith(("USHER_", "OTEL_"))}
    for key in saved:
        del os.environ[key]
    os.environ["USHER_DATABASE_URL"] = url
    os.environ["USHER_SECRET_KEY"] = "0" * 32
    get_settings.cache_clear()
    try:
        upgrade(Config(str(ini)), "head")
    finally:
        for key in list(os.environ):
            if key.startswith(("USHER_", "OTEL_")):
                del os.environ[key]
        os.environ.update(saved)
        get_settings.cache_clear()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--titles", type=int, default=240)
    parser.add_argument("--ladder", default=",".join(str(one) for one in DEFAULT_LADDER))
    parser.add_argument("--bar", type=Path, default=DEFAULT_BAR)
    parser.add_argument("--results-out", default="")
    args = parser.parse_args()

    if args.bar.exists():
        print(f"bar: {args.bar} sha256 {_sha256(args.bar)}")
    else:
        raise SystemExit(f"the pre-registered bar {args.bar} does not exist; write it first")

    ladder = tuple(int(one) for one in args.ladder.split(","))
    fixture = Path(__file__).resolve().parent.parent / "tests/fixtures/tmdb/movie.json"

    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "pgvector/pgvector:pg17",
        username="usher",
        password="usher",  # noqa: S106 -- a throwaway container's own credential
        dbname="usher",
    ) as container:
        url = container.get_connection_url().replace("psycopg2", "asyncpg")
        print("throwaway container up; running the real migration chain")
        _upgrade_head(url)

        # **A derivation that derives nothing runs very fast and reads as a
        # throughput.** Before any rung, one job is run and the rows it wrote
        # are counted, so a seed the mapper silently declines fails here rather
        # than becoming the fastest number in the table. `mutation-sweeps.md`'s
        # standing rule, one register over: a run that did not run is not a
        # pass, and a job that did no work is not a job.
        probe_ids = asyncio.run(seed(url, titles=1, fixture=fixture, offset=0))
        written = asyncio.run(_probe_one_job(url, probe_ids[0]))
        print(
            "seed probe: one derive wrote "
            + ", ".join(f"{count} {table}" for table, count in sorted(written.items()))
        )
        if not all(written.values()):
            raise SystemExit(
                f"a derive wrote nothing into {sorted(k for k, v in written.items() if not v)}"
            )

        rungs: list[Rung] = []
        seed_offset = 1000
        for concurrency in ladder:
            # Re-seeded per rung, so every rung derives titles nothing has
            # derived before. `DeriveService`'s write is a *replace*, so a
            # second pass over the same titles is a different -- cheaper --
            # workload than the first, and comparing a warm rung against a
            # cold one would measure the seeding rather than the concurrency.
            title_ids = asyncio.run(
                seed(url, titles=args.titles, fixture=fixture, offset=seed_offset)
            )
            seed_offset += args.titles
            rung = asyncio.run(run_rung(url, title_ids, concurrency=concurrency))
            rungs.append(rung)
            print(
                f"  c={rung.concurrency:>2}: {rung.jobs} jobs in {rung.seconds:7.3f}s "
                f"= {rung.rps:7.2f} jobs/s; per-job median {rung.median * 1000:7.2f} ms, "
                f"mean {rung.mean * 1000:7.2f} ms"
            )

    base = rungs[0]
    print(f"\n{'in flight':>10} {'jobs/s':>9} {'x c=1':>7} {'median ms':>10} {'x c=1':>7}")
    for rung in rungs:
        print(
            f"{rung.concurrency:>10} {rung.rps:>9.2f} {rung.rps / base.rps:>7.2f} "
            f"{rung.median * 1000:>10.2f} {rung.median / base.median:>7.2f}"
        )

    if args.results_out:
        Path(args.results_out).write_text(
            json.dumps(
                [
                    {
                        "concurrency": one.concurrency,
                        "jobs": one.jobs,
                        "seconds": one.seconds,
                        "rps": one.rps,
                        "median_seconds": one.median,
                        "mean_seconds": one.mean,
                        "latencies": one.latencies,
                    }
                    for one in rungs
                ],
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"wrote {len(rungs)} rungs to {args.results_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
