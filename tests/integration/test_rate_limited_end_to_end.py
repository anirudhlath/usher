"""The 429 path end to end, against a stub -- because provoking a real one is
refused, and the refusal is the point rather than a shortcut.

`PortRateLimited.retry_after` reaches `jobs.run_after` through four layers:
`EmbySession.request` translating a status into a port error,
`usher.adapters.http.retry_after_seconds` parsing the header,
`JobWorker._fail` reading the hint off the exception through an `isinstance`,
and `_FAIL`'s `GREATEST(:retry_after_seconds, 0)` adding it as a floor inside
`make_interval`. M9's D9 built that chain and pinned it with **one** case, at
the queue's own boundary (`tests/contract/job_queue_contract.py`), where the
hint is a float a test passes to `fail()`. This file is the second case and it
is the one that starts at an HTTP response: nothing here hands the queue a
number, and the only way `run_after` moves is if every layer above it worked.

**No request in this file leaves the process, and that is a decision rather
than a limitation of the harness.** The only servers this project talks to are
a household's own Emby and the live TMDb API, and hammering either until it
rate-limits is precisely the behaviour M10's outbound gate exists to prevent --
ADR-0005 chose ~25 rps as courtesy against a *stated* ~40 rather than
discovering the ceiling by hitting it. So the 429 comes from `FakeEmbyServer`,
and what this file can honestly claim is exactly half of what PRD 09's
carried-debt entry invites: **the mechanism is verified, the upstream
behaviour is not.** Three live runs corroborate the second half and none of
them is weak -- M9's T2 (393 TMDb requests, no 429, and no `Retry-After` on the
one 400), M9's S3 (130,334 TMDb requests, zero 429s, and no `Retry-After` on
any of 193 non-200s) and M9's H4/H5 (23 Emby requests, no 429, `run_after` NULL
on the only queued row). The missing half cannot be obtained without a server
that rate-limits Usher, which would be evidence that the gate failed.

**Real Postgres, deliberately.** The property under test is interval arithmetic
inside `_FAIL` -- `clock_timestamp() + make_interval(secs => GREATEST(hint, 0)
+ <the jittered term>)` -- and `FakeJobQueue` computes a Python transcription
of it, so a dict arm would be asserting a second implementation of the thing
under test. It is also the only arm on which `run_after` is a *timestamp*
rather than a number a fake chose.

**Both arms in every case, because "it backed off" is satisfied by any
backoff** -- including the ordinary jittered one the queue would have produced
with the hint dropped on the floor, which is exactly the state D9 closed. So
each case fails two jobs: one under a 429 carrying the hint and one under a 429
carrying no header at all, and asserts the second lands strictly sooner. The
whole content of D9 is *which* backoff.

**Two `Retry-After` forms, because RFC 9110 permits two** and
`retry_after_seconds` reaches the HTTP-date one only after `float(value)` has
raised -- the bug that existed in two separate copies of this code before that
helper was shared, and the form that would otherwise turn the one moment an
upstream is explicitly asking for backoff into a `ValueError`. Each case
asserts its own header really is the form it is filed under, because two arms
that are one form spelled twice would run green and cover one path.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.emby_server import USER_ID, FakeEmbyServer
from usher.adapters.emby.adapter import EmbyAdapter
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobRequest
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.events import DeferredEventPublisher
from usher.services.handlers import SourceBinding, SourceResolver, match_handler
from usher.services.jobs import JobScope, JobWorker
from usher.services.matching import MatchService

#: What the stub asks for, in whichever of RFC 9110's two spellings the case is
#: exercising. 120 s is comfortably outside the ordinary schedule below, which
#: is what makes "the hint reached `run_after`" a thing an assertion can be
#: wrong about.
RETRY_AFTER_SECONDS = 120.0

#: The queue's own parameters, named here because every bound below is derived
#: from them rather than written as a literal. The jittered term for a job on
#: its first failure is `BACKOFF_SECONDS * power(2, 0) * (0.5 + random() / 2)`,
#: i.e. a uniform draw from **[15, 30) s** -- so a hinted backoff lands in
#: [135, 150) and an unhinted one in [15, 30), and the two populations cannot
#: overlap. The unhinted half of that is **asserted** rather than merely
#: stated here, on `_Row.drawn`: until it was, a queue giving an unhinted
#: failure no backoff at all satisfied every assertion in this file, because
#: "sooner than the hinted one" is also what zero is.
BACKOFF_SECONDS = 30.0
MAX_ATTEMPTS = 5

#: Seeded on the stub and named by a `match` job each. Two keys rather than one
#: because the second arm has to be the *same* work under a 429 that carries no
#: hint, and a job already backed off past its `run_after` cannot be re-claimed
#: within one pass.
HINTED_KEY = "movie-rate-limited"
PLAIN_KEY = "movie-rate-limited-no-hint"

#: A well-formed MediaBrowser header for the probes below. The stub's identity
#: gate is checked for every path and runs *before* an armed rate limit, so a
#: bare probe would be refused with a 400 and the arming would never be
#: observed -- which is the failure this constant exists to keep out of the
#: premise.
PROBE_IDENTITY = 'MediaBrowser Client="Usher", Device="probe", DeviceId="probe", Version="1"'


def _item_path(external_id: str) -> str:
    """`GET /Users/{user}/Items/{item}`, written out here rather than imported
    from the adapter.

    `tests/fakes/emby_server.py`'s own module docstring requires it: every path
    in that file is spelled independently of the adapter's constants so a typo
    on one side fails rather than cancelling out. The same argument applies to
    a test arming a route on it -- and the failure is *loud* here, because an
    armed path nothing matches means no 429 fires at all, which the premise
    assertions below are what catch.
    """
    return f"/Users/{USER_ID}/Items/{external_id}"


def _is_numeric(value: str) -> bool:
    """Whether `retry_after_seconds` can answer this header without reaching
    its date arm -- i.e. `float(value)` succeeds."""
    try:
        float(value)
    except ValueError:
        return False
    return True


def _retry_after(form: str) -> tuple[str, float, float]:
    """One arm's header value, plus the closed interval the hint it parses to
    has to fall inside.

    **Computed inside the case rather than at collection time**, and that is
    not tidiness: an HTTP-date built when the module is imported is minutes
    stale by the time the case runs, `parsedate_to_datetime` yields an instant
    in the past, `max(0.0, ...)` floors the hint to **zero**, and the arm ends
    up asserting the ordinary backoff while reading as a pass.

    The date form carries a two-second floor rather than an exact number for
    one measured reason: HTTP-date has **one-second resolution**, so formatting
    truncates this instant's fraction and the hint the adapter parses is a
    little under `RETRY_AFTER_SECONDS` rather than exactly it. The integer form
    has no such slack and is asserted exactly.
    """
    if form == "integer":
        return str(int(RETRY_AFTER_SECONDS)), RETRY_AFTER_SECONDS, RETRY_AFTER_SECONDS
    target = datetime.now(UTC) + timedelta(seconds=RETRY_AFTER_SECONDS)
    return format_datetime(target, usegmt=True), RETRY_AFTER_SECONDS - 2.0, RETRY_AFTER_SECONDS


@dataclass(frozen=True, slots=True)
class _Row:
    """One `jobs` row as this file reads it, with `run_after` already resolved
    against the database's own clock -- twice, because the two resolutions
    answer two different questions.

    The interval is computed in SQL rather than in Python: `run_after` is a
    `timestamptz` Postgres wrote from `clock_timestamp()`, and subtracting it
    from a Python `datetime.now()` would compare two clocks -- one of which is
    inside a container -- instead of measuring the interval the statement
    actually chose.

    `seconds` is `run_after - clock_timestamp()`, i.e. **what is left** of the
    backoff when this file reads the row: the same comparison `_CLAIM` makes,
    so it is the number that decides when the job is re-claimable. `drawn` is
    `run_after - updated_at`, i.e. **what `_FAIL` chose**, and it is readable
    only because that statement writes both columns from `clock_timestamp()`
    in one pass and `jobs` is deliberately **not** one of the seven tables
    carrying a `set_updated_at` trigger -- a fact
    `test_migrations.py::test_migration_creates_the_updated_at_triggers`
    pins by name, and one that would otherwise put an unrelated `now()` on
    the second column.

    Two fields rather than one because a bound on the *draw* cannot be spelled
    against `seconds` without going intermittent. The jittered draw's own
    minimum **is** `BACKOFF_SECONDS / 2`, so `BACKOFF_SECONDS / 2 <=
    row.seconds` is falsified by *any* elapsed time at all: the gap between
    `_FAIL` and this read measured **4.0 ms** directly (`drawn - seconds` on
    one run) and is bounded at **< 0.42 s** by six earlier ones, which is the
    most those six can say -- and 0.42 s is the bottom 2.8% of [15, 30), i.e.
    that share of correct queues failing. `drawn` subtracts two instants
    Postgres wrote microseconds apart inside one statement and has no such
    slack.
    """

    status: str
    attempts: int
    last_error: str
    seconds: float
    drawn: float


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> dict[str, _Row]:
    async with sessions() as session:
        result = await session.execute(
            text(
                "SELECT key, status, attempts, last_error, "
                "EXTRACT(EPOCH FROM (run_after - clock_timestamp())) AS seconds, "
                "EXTRACT(EPOCH FROM (run_after - updated_at)) AS drawn FROM jobs"
            )
        )
        return {
            row.key: _Row(
                status=row.status,
                attempts=row.attempts,
                last_error=row.last_error,
                seconds=float(row.seconds),
                drawn=float(row.drawn),
            )
            for row in result
        }


def _hint_in(last_error: str) -> float:
    """The parsed hint `PortRateLimited` carried, read back out of the column
    that stores it.

    `jobs.last_error` holds `str(exc)`, and `PortRateLimited.__init__` renders
    exactly `rate limited, retry_after={value}`. Reading the number back is
    what separates "a 429 reached the queue" from "a 429 reached the queue
    carrying the interval the upstream asked for" -- and until D9 that column
    was the *only* place the hint survived at all, which is what the entry this
    file is about was written to close.
    """
    prefix = "rate limited, retry_after="
    assert last_error.startswith(prefix), last_error
    return float(last_error.removeprefix(prefix))


@pytest.fixture
def source() -> Source:
    """The configured source, **not persisted**.

    Nothing on the path under test reads it back: `match_handler` reaches
    `binding.source` only for a log line and for the `media_items` lookup that
    happens *after* the source has answered, and the 429 arrives before either.
    Writing a row would be fixture that the case cannot fail without.
    """
    return Source(
        kind=SourceKind.EMBY,
        name="Rate Limited Emby",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )


@pytest.fixture
def emby() -> FakeEmbyServer:
    server = FakeEmbyServer()
    for key in (HINTED_KEY, PLAIN_KEY):
        server.add_item(
            SourceItem(
                external_id=key,
                name=f"A Film Nobody Got To Match ({key})",
                kind=SourceItemKind.MOVIE,
                year=2024,
                container="mkv",
            ),
            datetime(2026, 7, 1, tzinfo=UTC),
        )
    return server


@pytest_asyncio.fixture
async def adapter(emby: FakeEmbyServer, source: Source) -> AsyncIterator[EmbyAdapter]:
    """The **shipped** adapter over the stub's transport, and unthrottled.

    `limiter=None` is what a directly-constructed adapter gets, and
    `EmbySession` turns it into a `SourceGate(0.0, ...)` whose `take()` returns
    before it computes an interval -- so no request here waits `1 / rate`. That
    is checked rather than assumed: S3 moved the gate's ownership to the
    composition root, and a test that silently spaced its requests would be a
    slow test nobody diagnoses.
    """
    client = httpx.AsyncClient(transport=emby.transport(), base_url=source.base_url)
    built = EmbyAdapter(
        source,
        SourceCredentials(username=emby.username, password=SecretStr(emby.password)),
        client=client,
    )
    yield built
    await built.aclose()
    await client.aclose()


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Engine-bound sessions that genuinely commit.

    `JobWorker` opens a scope per claim and a scope per job and commits inside
    each, which is the whole reason the claim is durable while the handler
    runs. The suite's shared `session` fixture is one connection inside one
    externally managed transaction, so a worker over it would be committing
    into a transaction that is rolled back afterwards -- and `run_after` would
    then be a value nothing outside that transaction ever saw.
    `test_services_jobs.py` takes the same trade for the same reason, and pays
    for it the same way: this fixture deletes what it wrote, because a `jobs`
    row left behind is four failures in three other files.
    """
    engine = build_engine(postgres_url)
    make = build_session_factory(engine)
    try:
        yield make
    finally:
        async with make() as cleanup:
            await cleanup.execute(text("DELETE FROM jobs"))
            await cleanup.commit()
        await engine.dispose()


def _resolver(source: Source, adapter: EmbyAdapter) -> SourceResolver:
    """Every key resolves to the one configured source.

    The production resolver is `composition.SourceRegistry.bound`, which reads
    `sources.list_all` and `media_items.get_by_external_id` and then builds an
    adapter through the credential store. None of those three is the subject
    here, and reaching them would mean an encrypted credential row and a
    factory that cannot be handed a `MockTransport` -- so this is the injected
    seam `SourceResolver` exists to be, spelled for a single-source household.
    """

    async def resolve(external_id: str) -> SourceBinding | None:
        return SourceBinding(source=source, adapter=adapter)

    return resolve


def _worker(
    sessions: async_sessionmaker[AsyncSession], resolve: SourceResolver, *, batch_size: int
) -> JobWorker:
    """The shipped worker with the shipped `match` handler, one session per
    scope -- `composition.build_worker`'s own shape.

    `max_in_flight=1` so the two jobs settle one after the other: their
    backoffs are compared against each other, and two failures racing to
    `clock_timestamp()` would put the comparison's margin at the mercy of the
    scheduler rather than of the arithmetic under test.
    """

    @asynccontextmanager
    async def scope() -> AsyncIterator[JobScope]:
        async with sessions() as session:
            queue = PostgresJobQueue(
                session, max_attempts=MAX_ATTEMPTS, backoff_seconds=BACKOFF_SECONDS
            )
            handler = match_handler(
                MatchService(
                    titles=PostgresTitleRepository(session),
                    matching=PostgresTitleMatchRepository(session),
                    queue=queue,
                ),
                PostgresMediaItemRepository(session),
                resolve,
            )
            yield JobScope(
                queue=queue,
                commit=session.commit,
                handlers={JobKind.MATCH: handler},
                events=DeferredEventPublisher(NullEventPublisher()),
            )

    return JobWorker(
        scope, {JobKind.MATCH: 1}, max_in_flight=1, batch_size=batch_size, lease_seconds=300.0
    )


async def _enqueue(sessions: async_sessionmaker[AsyncSession], keys: tuple[str, ...]) -> None:
    async with sessions() as session:
        await PostgresJobQueue(
            session, max_attempts=MAX_ATTEMPTS, backoff_seconds=BACKOFF_SECONDS
        ).enqueue(
            [JobRequest(kind=JobKind.MATCH, key=key, priority=JobPriority.NEW) for key in keys]
        )
        await session.commit()


def _probe(emby: FakeEmbyServer, source: Source, path: str) -> httpx.Response:
    return emby.handle(
        httpx.Request("GET", f"{source.base_url}{path}", headers={"Authorization": PROBE_IDENTITY})
    )


@pytest.mark.parametrize("form", ["integer", "http-date"])
async def test_a_429_from_a_source_defers_the_job_by_the_interval_the_upstream_asked_for(
    emby: FakeEmbyServer,
    source: Source,
    adapter: EmbyAdapter,
    sessions: async_sessionmaker[AsyncSession],
    form: str,
) -> None:
    """A 429 with a `Retry-After` pushes `jobs.run_after` out past the hint,
    and the same job under a 429 with no header lands on the ordinary jittered
    backoff -- which is strictly sooner.

    Both arms, and the second is not decoration. "The job backed off" is what a
    worker that dropped the hint on the floor also produces, and that is the
    state PRD 09's entry describes as the defect D9 closed; only the comparison
    between the two says *which* backoff was chosen.
    """
    header, hint_floor, hint_ceiling = _retry_after(form)
    assert _is_numeric(header) is (form == "integer"), (
        "the two arms have to be RFC 9110's two forms and not one form spelled twice: "
        f"{form!r} produced {header!r}"
    )

    hinted_path, plain_path = _item_path(HINTED_KEY), _item_path(PLAIN_KEY)
    emby.rate_limit(hinted_path, retry_after=header)
    emby.rate_limit(plain_path)

    # The stub's own arm, asserted before the chain and separately from it. At
    # this task's base commit `FakeEmbyServer` could not answer 429 at all, so
    # without these four lines a green run is equally consistent with a stub
    # that never rate-limited and a worker that was never provoked.
    hinted_probe = _probe(emby, source, hinted_path)
    assert hinted_probe.status_code == 429, (
        "the fake did not rate-limit and a chain that was never provoked proves nothing"
    )
    assert hinted_probe.headers.get("Retry-After") == header
    plain_probe = _probe(emby, source, plain_path)
    assert plain_probe.status_code == 429, (
        "the fake did not rate-limit and a chain that was never provoked proves nothing"
    )
    assert "Retry-After" not in plain_probe.headers, (
        "the control arm must carry no hint at all, or 'strictly sooner' is a comparison "
        "between two hinted backoffs"
    )
    # The probes consumed both arms; re-arm for the run under test.
    emby.rate_limit(hinted_path, retry_after=header)
    emby.rate_limit(plain_path)
    # Everything appended to `emby.requests` from here on is the worker's. The
    # slice is what makes the assertion below a statement about the worker at
    # all: `handle` records `f"{method} {path}"` for every request including
    # the two probes above, which sent those exact two lines -- so the same
    # assertion spelled over the whole list is satisfied before the worker
    # exists.
    before = len(emby.requests)

    await _enqueue(sessions, (HINTED_KEY, PLAIN_KEY))
    assert await _worker(sessions, _resolver(source, adapter), batch_size=2).run_once() == 2

    # The worker's own reads were the ones rate-limited, not the handshake in
    # front of them. A limit armed on `/Users/AuthenticateByName` instead
    # satisfies every other assertion in this case: `_authenticate_locked`
    # translates a 429 through the same `retry_after_seconds`, so both jobs
    # fail with the same hint and the same `run_after` -- and never reach the
    # read the case is about.
    worker_requests = emby.requests[before:]
    assert f"GET {hinted_path}" in worker_requests, worker_requests
    assert f"GET {plain_path}" in worker_requests, worker_requests

    # And the other half of that: an armed path the adapter never asks for
    # fires no 429 at all, which leaves both jobs completing rather than
    # failing -- `PostgresJobQueue.complete` is a `DELETE`, so a completed job
    # leaves no row for this to find.
    rows = await _rows(sessions)
    assert set(rows) == {HINTED_KEY, PLAIN_KEY}, (
        f"both jobs must still be on the queue, backed off rather than completed: {sorted(rows)}"
    )
    hinted, plain = rows[HINTED_KEY], rows[PLAIN_KEY]

    # Every layer above `run_after`: the status became a `PortRateLimited`, the
    # header was parsed, and the parsed value is what reached the queue. Both
    # rows, not only the hinted one -- the control has to have failed for the
    # *same* reason for the comparison below to be about the header.
    assert (hinted.status, hinted.attempts) == ("pending", 1)
    assert (plain.status, plain.attempts) == ("pending", 1)
    assert hint_floor <= _hint_in(hinted.last_error) <= hint_ceiling
    assert plain.last_error == "rate limited, retry_after=None"

    # And `run_after` itself. The upper bound is what makes this an assertion
    # about a *floor added to* the jitter rather than about any large number:
    # the hint plus one whole jittered draw is the most `_FAIL` may produce.
    assert hinted.seconds >= RETRY_AFTER_SECONDS, (
        f"the {form} hint did not reach run_after: {hinted.seconds}s"
    )
    assert hinted.seconds < RETRY_AFTER_SECONDS + BACKOFF_SECONDS
    assert plain.seconds < RETRY_AFTER_SECONDS
    assert plain.seconds < hinted.seconds, (
        "the 429 that carried no hint has to land strictly sooner, or the hint is not what "
        f"moved run_after: hinted={hinted.seconds}s plain={plain.seconds}s"
    )
    # The control arm's *own* schedule, not merely "sooner than the hinted
    # one". Without a lower bound the whole file is green against a queue that
    # gives an unhinted failure no backoff at all -- measured: dividing
    # `_FAIL`'s jitter term by ten puts a job retrying a broken upstream at
    # ~2 s instead of ~20 s, the rationed hot loop the jitter exists to
    # prevent, and every other assertion above passes. This is the module
    # comment on `BACKOFF_SECONDS` spelled as a check rather than as prose,
    # and it is asserted on `drawn` rather than on `seconds` for the reason
    # `_Row` gives.
    assert BACKOFF_SECONDS / 2 <= plain.drawn < BACKOFF_SECONDS, (
        f"the unhinted 429 has to land on the ordinary [15, 30) draw: {plain.drawn}s"
    )
