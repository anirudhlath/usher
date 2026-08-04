"""`EnrichService`'s index enqueue, against real Postgres.

**This file exists for one ordering the unit suite cannot see.** The enqueue
happens after the commit, and the reason is a transaction: a worker claiming
the index job reads `titles` in a *different* one, so a job enqueued before
the commit can run against the pre-enrichment row -- fingerprint the old
text, embed the old text, and then stop matching the stale predicate because
the fingerprint agrees with what it embedded. A permanently stale vector the
backfill will never re-claim, produced by the enqueue that exists to keep it
fresh, with nothing raising anywhere.

`FakeJobQueue` and `FakeTitleRepository` share no transaction, so against
them an enqueue before the commit is *indistinguishable* from one after.
`tests/unit/test_services_enrich.py` asserts the order through a recording
collaborator, which is one more than the plan expected of it and still not
the data consequence. Here the consequence is readable: the enqueue is made
by a publisher-shaped probe that reads `titles` back **on its own
connection**, and a separate connection cannot see an uncommitted write.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.metadata_provider import FakeMetadataProvider
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.sync import PostgresRawPayloadStore
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import Job, JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.events import NullEventPublisher
from usher.ports.ingest import ProviderRef
from usher.ports.jobs import JobQueue, JobRequest
from usher.services.enrich import EnrichService

_TMDB_ID = 90000551
_MARK = "enrich-index-case"


class _ReadsOnItsOwnConnection(JobQueue):
    """Enqueues through the real queue, and records what a *different*
    transaction can see at that instant.

    The whole point: `EnrichService` writes the enriched name inside its own
    transaction, so a second connection reads the pre-enrichment row until
    the commit lands. Recording `enrichment_state` here turns "the enqueue
    came first" from an ordering claim into a value a worker would actually
    have read.
    """

    def __init__(self, inner: JobQueue, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._inner = inner
        self._sessions = sessions
        self.state_visible_elsewhere: str | None = None

    async def enqueue(self, requests: Sequence[JobRequest]) -> int:
        async with self._sessions() as other:
            result = await other.execute(
                text("SELECT enrichment_state FROM titles WHERE tmdb_id = :tmdb_id"),
                {"tmdb_id": _TMDB_ID},
            )
            row = result.scalar_one_or_none()
            self.state_visible_elsewhere = None if row is None else str(row)
        return await self._inner.enqueue(requests)

    async def claim(self, kinds: Sequence[JobKind], *, limit: int = 1) -> list[Job]:
        raise NotImplementedError

    async def complete(self, job_id: uuid.UUID) -> None:
        raise NotImplementedError

    async def fail(self, job_id: uuid.UUID, *, error: str, retryable: bool) -> Job | None:
        raise NotImplementedError

    async def requeue_running(self, *, older_than_seconds: float = 0.0) -> int:
        raise NotImplementedError

    async def depth(self) -> dict[JobKind, int]:
        raise NotImplementedError

    async def parked(self, *, limit: int = 100) -> list[Job]:
        raise NotImplementedError


async def _wipe(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM jobs WHERE kind = 'index'"))
    await session.execute(
        text("DELETE FROM raw_payloads WHERE provider = 'tmdb' AND reference = :id"),
        {"id": str(_TMDB_ID)},
    )
    await session.execute(
        text("DELETE FROM titles WHERE tmdb_id = :tmdb_id OR sort_name = :mark"),
        {"tmdb_id": _TMDB_ID, "mark": _MARK},
    )
    # `DROP TABLE IF EXISTS stg_jobs` stood here until M6's staging tables
    # became `CREATE TEMP TABLE ... ON COMMIT DROP`; the commit below is now
    # what removes it rather than what persists it.
    await session.commit()


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Its own engine, because every case here needs two live connections at
    once -- the suite's usual per-test session is one connection inside one
    rolled-back transaction, which is exactly what this file cannot use.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """This module commits for real, because the ordering it exists to check
    is only visible from a second connection -- which cannot see a rolled-back
    transaction at all. So it cleans up after itself: a leftover `titles` row
    fails an unrelated file on `ix_titles_tmdb_id_kind`. A leftover `stg_jobs`
    used to be the other half of that and no longer can be -- the staging
    tables are temporary and drop at commit.
    """
    async with sessions() as session:
        await _wipe(session)
    yield
    async with sessions() as session:
        await _wipe(session)


async def _seed(session: AsyncSession) -> Title:
    title = Title(
        kind=TitleKind.MOVIE,
        tmdb_id=_TMDB_ID,
        name="The Quiet Vacuum",
        sort_name=_MARK,
        enrichment_state=EnrichmentState.STUB,
    )
    await PostgresTitleRepository(session).add(title)
    await session.commit()
    return title


def _service(session: AsyncSession, queue: JobQueue) -> EnrichService:
    # Seeded under this file's own id rather than reusing the fake's default
    # 90000550, which `test_sse_end_to_end.py` also commits and wipes. Two
    # committing modules sharing a `tmdb_id` fail each other on
    # `ix_titles_tmdb_id_kind`, in whichever ran second.
    provider = FakeMetadataProvider()
    provider.seed(
        ProviderRef(provider="tmdb", value=str(_TMDB_ID), kind=TitleKind.MOVIE),
        {"id": _TMDB_ID, "title": "The Quiet Vacuum", "overview": "A caretaker counts rooms."},
    )
    return EnrichService(
        PostgresTitleRepository(session),
        PostgresEpisodeRepository(session),
        PostgresRawPayloadStore(session),
        provider,
        session.commit,
        NullEventPublisher(),
        queue=queue,
    )


async def test_a_finished_enrichment_leaves_one_index_job_in_the_table(
    sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """The row, read back through SQL rather than off a fake's dict.

    Fails: the absent enqueue -- and it is absent *silently*. An enriched
    title with no index job produces no error, no log line, no failed job and
    no degraded health check; it produces a search result set that is quietly
    wrong.
    """
    async with sessions() as session:
        title = await _seed(session)
        queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0)

        await _service(session, queue).enrich(title.id)
        await session.commit()

    async with sessions() as reader:
        result = await reader.execute(
            text("SELECT key, priority FROM jobs WHERE kind = 'index'"),
        )
        rows = result.all()

    assert [(key, priority) for key, priority in rows] == [
        (str(title.id), int(JobPriority.BACKFILL))
    ]


async def test_the_enqueue_sees_a_committed_title(
    sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """**The case the unit suite structurally cannot write.**

    A second connection is opened at the instant of the enqueue and asked what
    it can see. After the commit it reads `enriched`; before it, it reads
    `stub` -- or nothing at all, if the row itself is new. A worker claiming
    this job runs in exactly that second transaction, so what it can see is
    what it would have fingerprinted.

    Fails: moving the enqueue above `await self._commit()`. That version
    passes every unit case that asserts a *value* and is caught in the unit
    file only by an ordering probe; here it is caught by the consequence,
    which is what a reviewer told to mutation-test should be able to run.
    """
    async with sessions() as session:
        title = await _seed(session)
        probe = _ReadsOnItsOwnConnection(
            PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0), sessions
        )

        await _service(session, probe).enrich(title.id)
        await session.commit()

    assert probe.state_visible_elsewhere == EnrichmentState.ENRICHED.value
