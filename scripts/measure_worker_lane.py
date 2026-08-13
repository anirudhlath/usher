"""Price the worker lane against a local stub, and ask whether the rate limit binds.

**Not a test.** It writes to a real database and opens real sockets. It never
touches `api.themoviedb.org`: the upstream is a stub on `127.0.0.1` that
replays the latency distribution M9's S3 measured over 130,334 live requests
(median 0.0588 s, mean 0.0993 s, p95 0.4267 s). ADR-0005 chose ~25 rps as
courtesy against TMDb's stated ~40 and S3 already drew 86 x 502 from that
server in two bursts of 43, so probing a third party's real ceiling is not
something this harness is allowed to do -- and a stub is the *accurate*
instrument as well as the courteous one, because it isolates the lane from
upstream variance.

    uv run python scripts/measure_worker_lane.py --jobs 400
    uv run python scripts/measure_worker_lane.py --database-url "$USHER_DATABASE_URL"

**The question is not "how many rps".** M9's S3 measured 19.76 rps on three
workers against a bucket configured at 10 rps per process that was *never
binding on any of them* -- so the architecture, not the policy, was the
ceiling. The bar this harness scores is that a **single process** tracks its
configured limit across several settings: set it, and watch throughput follow.
The pre-registered bar is `/var/tmp/w1/BAR.md`, whose sha256 is re-computed at
run time and printed below, so an edit made after a number was seen shows up in
the log.

Two instruments, deliberately, because they fail differently:

* **Requests counted at the stub**, over a steady-state window that drops the
  first `--warmup` seconds. That is the rate the bucket is supposed to bind.
* **Maximum concurrent in-flight requests at the stub**, plus the
  intersection-over-union of the request windows. A count of completed jobs is
  also what a sequential loop produces (CLAUDE.md's fourth evidence rule); an
  observed overlap is not.

Quiet-check: the two-sided idle-sampled CPU drift and the argv-token foreign
process census from `scripts/measure_suggest_tiers.py`, imported rather than
re-derived -- a one-minute load average rises from the run's own work and would
condemn every clean run, and `pgrep -f pytest` counts the shell that mentions
the word.
"""

import argparse
import asyncio
import hashlib
import json
import random
import re
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_suggest_tiers import (
    _CPU_DRIFT_LIMIT,
    _CPU_SETTLE_SECONDS,
    _load_snapshot,
)

from usher.composition import (
    SourceRegistry,
    build_pipeline,
    build_worker,
    metadata_provider,
    unit_of_work,
)
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.users import ensure_default_user
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobRequest

BAR = Path("/var/tmp/w1/BAR.md")  # noqa: S108 -- durable, not tmpfs; CLAUDE.md

# S3's measured HTTP latency, 130,334 requests over 1.98 h against the live
# API (`.claude/rules/tmdb-and-enrichment.md`). A *constant*-latency stub
# cannot show the straggler behaviour that separates a fixed-batch `gather`
# from a continuously-fed pool, and S2's own 0.38% sample priced the median
# correctly and the tail not at all -- so the tail is the part that has to be
# reproduced rather than the mean.
_LATENCY_MEDIAN = 0.0588
_LATENCY_MEAN = 0.0993
_LATENCY_P95 = 0.4267

# The three configured limits the bar names, all at or under ADR-0005's ~25.
_LIMITS: tuple[float, ...] = (5.0, 12.0, 24.0)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: `ln(0.0588)`, so the drawn median is S3's by construction whatever `sigma`
#: is. Only the spread is a choice, and it is `--sigma`.
_LATENCY_MU = -2.834

#: 🔴 **No two-parameter lognormal reproduces all three of S3's statistics, and
#: the docstring here claimed one did until the harness's own printed
#: comparison said otherwise on the first real run.** It read *"a lognormal
#: fitted on the median and the p95 lands the mean at 0.099 s within a
#: percent"*, from an arithmetic slip: `(ln(0.4267) - mu) / 1.645` is **1.205**,
#: not the 0.9007 written beside it. So the two fits are a genuine choice and
#: each is wrong in a different direction, measured over 20,000 draws:
#:
#: | `sigma` | median | mean | p95 |
#: |---|---|---|---|
#: | S3, live, n = 130,334 | 0.0588 | 0.0993 | 0.4267 |
#: | **0.9** — matches the *mean* | 0.0587 | 0.0882 (-11%) | 0.2585 (**-39%**) |
#: | **1.205** — matches the *p95* | 0.0588 | 0.1214 (+22%) | 0.4267 |
#:
#: The real distribution is more skewed than a lognormal, which is itself worth
#: knowing: S3's own note that *"concurrency does not move the median request;
#: it moves the tail"* is exactly the part a two-parameter fit cannot hold on to.
#: **Both are run and both are reported.** 1.205 is the sterner test of
#: `Settings.job_concurrency`, whose 12 is derived from the p95 -- a heavier
#: tail needs more in flight to hold a given rate -- and 0.9 is the one closest
#: to the per-job wall clock S2 measured end to end. A bar cleared under one and
#: not the other is a finding, not a pass.
#:
#: The default stays at 0.9 because that is what the first run was taken with,
#: and moving an instrument after seeing a number is how a bar stops being one.
_DEFAULT_SIGMA = 0.9


def _delay(chooser: random.Random, sigma: float) -> float:
    """One draw from S3's measured latency distribution. See `_DEFAULT_SIGMA`
    for which two of its three statistics a given `sigma` reproduces, and why
    no value reproduces all three."""
    return chooser.lognormvariate(_LATENCY_MU, sigma)


@dataclass(slots=True)
class _Window:
    """One stub request, as an interval rather than as a tick.

    An interval is what makes "these requests genuinely overlapped" an
    assertion; a count of requests is satisfied by a sequential loop.
    """

    started_at: float
    finished_at: float


@dataclass(slots=True)
class _Stub:
    """A local HTTP/1.1 responder standing in for `api.themoviedb.org/3`.

    Raw asyncio rather than uvicorn: the point of the stub is that its own
    scheduling contributes as little as possible to the number being measured,
    and an ASGI server brings its own concurrency semantics into the middle of
    a concurrency measurement.
    """

    chooser: random.Random
    sigma: float
    windows: list[_Window] = field(default_factory=list)
    in_flight: int = 0
    peak_in_flight: int = 0
    server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                if not head:
                    return
                started = time.perf_counter()
                self.in_flight += 1
                self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
                try:
                    await asyncio.sleep(_delay(self.chooser, self.sigma))
                    # **The requested id is echoed back**, and that is not
                    # decoration: `titles.tmdb_id` carries a unique index per
                    # kind, so a stub answering a constant id makes every
                    # enrichment after the first a `RepositoryConflict` on
                    # `ix_titles_tmdb_id_kind` -- which is a *retryable*
                    # failure, so the lane would measure the backoff path and
                    # report it as throughput. Found by running it.
                    body = _body_for(head)
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                    )
                    await writer.drain()
                finally:
                    self.in_flight -= 1
                    self.windows.append(_Window(started, time.perf_counter()))
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            return
        finally:
            writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


# A TMDb movie detail body, shaped like the real one and carrying no third-party
# data: every value is synthetic. Shape-recorded, value-synthetic, which is the
# rule `.claude/rules/fixtures-and-fakes.md` states for every fixture here.
_SHAPE: dict[str, Any] = {
    "id": 1,
    "title": "A Synthetic Film",
    "original_title": "A Synthetic Film",
    "overview": "Synthetic overview text." * 20,
    "release_date": "1999-01-01",
    "runtime": 101,
    "status": "Released",
    "genres": [{"id": 18, "name": "Drama"}],
    "original_language": "en",
    "spoken_languages": [{"iso_639_1": "en", "english_name": "English"}],
    "origin_country": ["US"],
    "vote_average": 7.1,
    "vote_count": 128,
    "popularity": 3.5,
    "keywords": {"keywords": [{"id": 1, "name": "synthetic"}]},
    "external_ids": {"imdb_id": None},
    "release_dates": {"results": []},
    "images": {"posters": [], "backdrops": [], "logos": []},
    "credits": {"cast": [], "crew": []},
    "belongs_to_collection": None,
}


def _body_for(head: bytes) -> bytes:
    """The stub's answer, carrying the id the request line asked for."""
    line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
    target = line.split(" ")[1] if " " in line else "/movie/1"
    digits = re.findall(r"/(\d+)", target.split("?")[0])
    return json.dumps({**_SHAPE, "id": int(digits[-1]) if digits else 1}).encode()


def _settings(*, database_url: str, base_url: str, rps: float, **overrides: Any) -> Settings:
    return Settings(
        database_url=database_url,  # type: ignore[arg-type]
        secret_key="0" * 32,  # type: ignore[arg-type]
        tmdb_api_key="0" * 32,  # type: ignore[arg-type]
        tmdb_base_url=base_url,
        tmdb_requests_per_second=rps,
        push_enabled=False,
        worker_enabled=True,
        **overrides,
    )


_SEED = """
INSERT INTO titles (
    id, kind, name, sort_name, tmdb_id, enrichment_state,
    genres, keywords, spoken_languages, origin_countries, credit_names,
    field_provenance, created_at, updated_at
)
SELECT
    CAST(:prefix || lpad(n::text, 12, '0') AS uuid), 'movie',
    'Seeded ' || n, 'seeded ' || n, n, 'skeleton',
    '{}', '{}', '{}', '{}', '{}', '{}'::jsonb, clock_timestamp(), clock_timestamp()
FROM generate_series(1, :count) AS n
RETURNING id
"""


async def _seed(sessions: async_sessionmaker[AsyncSession], count: int) -> list[uuid.UUID]:
    """One skeleton movie per job, and the `enrich` jobs that name them.

    Truncated first: a run that inherited the previous run's queue would be
    measuring a different population from the one it reports.
    """
    prefix = f"{uuid.uuid4().hex[:8]}-0000-7000-8000-"
    async with sessions() as session:
        await session.execute(text("TRUNCATE jobs, raw_payloads, titles CASCADE"))
        rows = (await session.execute(text(_SEED), {"prefix": prefix, "count": count})).scalars()
        ids = [uuid.UUID(str(one)) for one in rows]
        await session.commit()
    return ids


async def _enqueue(sessions: async_sessionmaker[AsyncSession], ids: Sequence[uuid.UUID]) -> int:
    async with sessions() as session:
        pipeline = build_pipeline(session, _bare_settings())
        written = await pipeline.queue.enqueue(
            [
                JobRequest(kind=JobKind.ENRICH, key=str(one), priority=JobPriority.BACKFILL)
                for one in ids
            ]
        )
        await session.commit()
    return written


_BARE: Settings | None = None


def _bare_settings() -> Settings:
    assert _BARE is not None  # noqa: S101 -- harness precondition, not a test
    return _BARE


@dataclass(slots=True)
class Run:
    limit: float
    requests: int
    window_seconds: float
    rps: float
    peak_in_flight: int
    iou: float
    completed: int
    remaining: int
    elapsed: float
    enriched: int
    retried: int


def _steady(windows: Sequence[_Window], warmup: float, started: float) -> tuple[int, float]:
    """Requests and window length after the warm-up, with the denominator.

    From the first request past the warm-up to the last one, so the rate is a
    rate over a window that actually held work rather than over the whole run
    including its own tail.
    """
    kept = [one for one in windows if one.started_at - started >= warmup]
    if len(kept) < 2:
        return len(kept), 0.0
    return len(kept), kept[-1].started_at - kept[0].started_at


def _iou(windows: Sequence[_Window]) -> float:
    """Total overlap as a fraction of the union of the request intervals.

    1.0 would mean every request covered the whole run; 0.0 that no two ever
    ran at once. `JobQueueContract.overlapping()` asks the same question of two
    windows; this is the population form of it.
    """
    if not windows:
        return 0.0
    ordered = sorted(windows, key=lambda one: one.started_at)
    union = 0.0
    covered = 0.0
    edge = ordered[0].started_at
    for one in ordered:
        covered += one.finished_at - one.started_at
        if one.finished_at > edge:
            union += one.finished_at - max(edge, one.started_at)
            edge = one.finished_at
    return 0.0 if union <= 0 else round((covered - union) / union, 4)


async def _drain(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    seconds: float,
) -> tuple[int, float]:
    """Run the worker lane for `seconds`, exactly as `usher work` runs it.

    Returns (jobs completed, wall clock). **The one API-coupled function in
    this file** -- everything above and below it is the measurement, so the
    before/after really is one instrument.
    """
    provider, aclose = await metadata_provider(settings)
    ran_total = 0
    started = time.perf_counter()
    async with sessions() as session:
        user_id = await ensure_default_user(session)
        await session.commit()
    registry = SourceRegistry()
    work = unit_of_work(sessions, settings, events=NullEventPublisher(), provider=provider)
    try:
        worker = build_worker(
            work,
            settings,
            provider=provider,
            embedder=None,
            client=None,
            registry=registry,
            user_id=user_id,
        )
        await worker.recover()
        while time.perf_counter() - started < seconds:
            ran = await worker.run_once()
            ran_total += ran
            if ran == 0:
                break
    finally:
        await registry.aclose()
        await aclose()
    return ran_total, time.perf_counter() - started


async def _outcome(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    """Jobs left, titles actually enriched, and jobs carrying an attempt.

    **The premise guard on every rate above it.** A request counted at the stub
    that produced no enriched title is a request the lane made on a failure
    path, and a rate computed over those is a measurement of the backoff
    schedule. Found by running it: a stub answering a constant TMDb id made
    every enrichment after the first a `RepositoryConflict`, and the run
    reported a perfectly plausible rps.
    """
    async with sessions() as session:
        left = int((await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one())
        enriched = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM titles WHERE enrichment_state = 'enriched'")
                )
            ).scalar_one()
        )
        retried = int(
            (
                await session.execute(text("SELECT count(*) FROM jobs WHERE attempts > 0"))
            ).scalar_one()
        )
    return left, enriched, retried


async def _one(
    database_url: str,
    *,
    limit: float,
    jobs: int,
    seconds: float,
    warmup: float,
    chooser: random.Random,
    sigma: float,
) -> Run:
    stub = _Stub(chooser=chooser, sigma=sigma)
    port = await stub.start()
    settings = _settings(database_url=database_url, base_url=f"http://127.0.0.1:{port}", rps=limit)
    engine = build_engine(
        database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow
    )
    sessions = build_session_factory(engine)
    try:
        ids = await _seed(sessions, jobs)
        await _enqueue(sessions, ids)
        started = time.perf_counter()
        completed, elapsed = await _drain(sessions, settings, seconds=seconds)
        left, enriched, retried = await _outcome(sessions)
    finally:
        await engine.dispose()
        await stub.stop()
    requests, window = _steady(stub.windows, warmup, started)
    return Run(
        limit=limit,
        requests=requests,
        window_seconds=round(window, 3),
        rps=round(requests / window, 3) if window > 0 else 0.0,
        peak_in_flight=stub.peak_in_flight,
        iou=_iou(stub.windows),
        completed=completed,
        remaining=left,
        elapsed=round(elapsed, 3),
        enriched=enriched,
        retried=retried,
    )


def _report(runs: Sequence[Run], *, label: str) -> None:
    print(f"\n== {label} ==")
    print(
        f"{'limit rps':>10} {'measured':>10} {'ratio':>7} {'requests':>9} "
        f"{'window s':>9} {'peak':>5} {'iou':>7} {'done':>6} {'left':>6} "
        f"{'enriched':>9} {'retried':>8}"
    )
    for one in runs:
        ratio = one.rps / one.limit if one.limit else 0.0
        verdict = "BIND" if 0.85 <= ratio <= 1.05 else "----"
        print(
            f"{one.limit:>10.1f} {one.rps:>10.3f} {ratio:>7.3f} {one.requests:>9} "
            f"{one.window_seconds:>9.3f} {one.peak_in_flight:>5} {one.iou:>7.4f} "
            f"{one.completed:>6} {one.remaining:>6} {one.enriched:>9} {one.retried:>8}  {verdict}"
        )


def _throwaway_postgres() -> tuple[str, Callable[[], None]]:
    """A container and its schema, built **outside** the event loop.

    `db/migrations/env.py` calls `asyncio.run` itself, so migrating from
    inside a running loop raises `RuntimeError: asyncio.run() cannot be called
    from a running event loop` -- which is why this is a synchronous function
    called before `asyncio.run`, not a step of the async run below.
    """
    from testcontainers.community.postgres import PostgresContainer
    from tests.integration.conftest import _upgrade_head

    container = PostgresContainer(
        "pgvector/pgvector:pg17",
        username="usher",
        password="usher",  # noqa: S106 -- a throwaway container, torn down below
        dbname="usher",
    )
    container.start()
    url = container.get_connection_url().replace("psycopg2", "asyncpg")
    _upgrade_head(url)
    print(f"postgres: throwaway container at {url.rsplit('@', 1)[-1]}")
    return url, container.stop


async def run(args: argparse.Namespace, database_url: str) -> None:
    global _BARE
    print(f"bar: {BAR} sha256={_sha256(BAR) if BAR.exists() else 'MISSING'}")
    # Settle first: the opening sample must be taken under the same condition
    # as the closing one, and starting a container leaves the box in its own
    # wake for several seconds.
    time.sleep(_CPU_SETTLE_SECONDS)
    before = _load_snapshot()
    opening = float(before["cpu_busy"])
    foreign = int(before["processes"]["pytest"])
    print(f"quiet: opening cpu busy {opening}, foreign pytest {foreign}, load {before['loadavg']}")

    _BARE = _settings(database_url=database_url, base_url="http://127.0.0.1:1", rps=1.0)

    chooser = random.Random(args.seed)  # noqa: S311 -- a latency draw, not a secret
    shape = random.Random(args.seed)  # noqa: S311 -- a latency draw, not a secret
    drawn = sorted(_delay(shape, args.sigma) for _ in range(20_000))
    print(
        f"stub latency at sigma={args.sigma}: "
        f"median {statistics.median(drawn):.4f} (S3 {_LATENCY_MEDIAN}), "
        f"mean {statistics.fmean(drawn):.4f} (S3 {_LATENCY_MEAN}), "
        f"p95 {drawn[int(0.95 * len(drawn))]:.4f} (S3 {_LATENCY_P95})"
    )

    runs: list[Run] = []
    for limit in _LIMITS:
        runs.append(
            await _one(
                database_url,
                limit=limit,
                jobs=args.jobs,
                seconds=args.seconds,
                warmup=args.warmup,
                chooser=chooser,
                sigma=args.sigma,
            )
        )
        _report(runs[-1:], label=f"limit {limit}")

    _report(runs, label=args.label)
    time.sleep(_CPU_SETTLE_SECONDS)
    after = _load_snapshot()
    closing = float(after["cpu_busy"])
    foreign = max(foreign, int(after["processes"]["pytest"]))
    drift = round(closing - opening, 4)
    print(f"quiet: closing cpu busy {closing}, drift {drift} (limit +-{_CPU_DRIFT_LIMIT})")
    if abs(drift) > _CPU_DRIFT_LIMIT or foreign:
        print("QUIET CHECK FAILED -- discard this run and repeat it")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--jobs", type=int, default=600)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=20260812)
    parser.add_argument("--sigma", type=float, default=_DEFAULT_SIGMA)
    parser.add_argument("--label", default="all limits")
    args = parser.parse_args()
    stop = None
    database_url = args.database_url
    if database_url is None:
        database_url, stop = _throwaway_postgres()
    try:
        asyncio.run(run(args, database_url))
    finally:
        if stop is not None:
            stop()


if __name__ == "__main__":
    main()
