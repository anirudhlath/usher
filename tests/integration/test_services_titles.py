"""`TitleReadService` against real Postgres, for the things its port fakes
structurally cannot express.

**`FakeJobQueue.enqueue` reports a re-enqueue as a row written, and Postgres
does not.** The fake takes the update branch and adds one to its count
whatever it wrote; the real `_ENQUEUE`'s conflict clause carries
`AND jobs.priority < excluded.priority`, so re-enqueueing an already-promoted
job matches nothing and answers **0**. That is the divergence, and it is the
one the read path stands on: `_promote` returns whether an enqueue was
*attempted*, and a version that returned "a row changed" passes every unit
case in `tests/unit/test_services_titles.py` and then reports `promoted =
False` for every second open of the same stub in production. Measured both
ways -- the mutation survives the unit file and fails here.

**And the promotion clause itself is SQL.** "Opening a stub raises `NEW` to
`DEMAND`" and "opening a parked title leaves it parked and at its old
priority" are one `ON CONFLICT ... WHERE` against the real queue and two
Python branches in the fake, so only this run says anything about the
statement M4 wrote.

Foreign keys are the third: `media_items.title_id`, `media_items.source_id`
and `watch_states.user_id` are all real here and are dict entries there.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import ensure_default_user
from usher.domain.enums import EnrichmentState, HdrFormat, SourceKind, TitleKind
from usher.domain.jobs import JobKind, JobPriority, JobStatus
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.jobs import JobRequest
from usher.services.titles import TitleReadService

SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def user_id(session: AsyncSession) -> uuid.UUID:
    return await ensure_default_user(session)


@pytest.fixture
def queue(session: AsyncSession) -> PostgresJobQueue:
    return PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0)


@pytest.fixture
def service(session: AsyncSession, queue: PostgresJobQueue) -> TitleReadService:
    return TitleReadService(
        PostgresTitleRepository(session),
        PostgresMediaItemRepository(session),
        PostgresSourceRepository(session),
        PostgresWatchStateRepository(session),
        queue,
    )


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> AsyncIterator[Source]:
    row = Source(
        kind=SourceKind.EMBY,
        name="Living Room Emby",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{uuid.uuid4()}",
        device_id=str(uuid.uuid4()),
    )
    await PostgresSourceRepository(session).add(row)
    yield row


async def _seed_title(
    session: AsyncSession, state: EnrichmentState, *, kind: TitleKind = TitleKind.MOVIE
) -> Title:
    title = Title(
        kind=kind, name="Example Movie", sort_name="Example Movie", enrichment_state=state
    )
    await PostgresTitleRepository(session).add(title)
    return title


async def _seed_copy(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    title_id: uuid.UUID,
    external_id: str,
    episode_id: uuid.UUID | None = None,
) -> None:
    await PostgresMediaItemRepository(session).upsert_many(
        [
            MediaItemUpsert(
                source_id=source_id,
                external_id=external_id,
                title_id=title_id,
                episode_id=episode_id,
                container="mkv",
                video_codec="hevc",
                audio_codec="truehd",
                width=3840,
                height=2160,
                hdr_format=HdrFormat.DOLBY_VISION,
                audio_channels=8,
                file_size_bytes=68_719_476_736,
                runtime_seconds=9360,
                added_at=None,
                last_seen_at=SEEN_AT,
            )
        ]
    )


async def test_a_second_open_still_reports_a_promotion(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """The property the fake cannot see, and the reason this file exists.

    `PostgresJobQueue.enqueue` answers **0** for the second open -- the
    promotion clause's `AND jobs.priority < excluded.priority` finds nothing
    left to raise, which M4 recorded as the honest number. So `_promote`
    reports "this read asked for the front of the queue", not "a row
    changed": the alternative tells a client that the second open of an
    already-promoted title declined to promote it, which is both false and
    exactly backwards. `FakeJobQueue` counts that same re-enqueue as one row
    written, so the unit suite ratifies either spelling.
    """
    title = await _seed_title(session, EnrichmentState.STUB)

    first = await service.detail(title.id, user_id=user_id)
    second = await service.detail(title.id, user_id=user_id)

    assert first is not None and second is not None
    assert (first.promoted, second.promoted) == (True, True)
    rows_written = await PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0).enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.DEMAND)]
    )
    assert rows_written == 0, "the premise: a re-enqueue at the same priority writes nothing"


async def test_opening_a_stub_raises_a_queued_job_to_demand(
    service: TitleReadService, queue: PostgresJobQueue, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """The promotion clause, exercised as SQL rather than as a `max()`.

    A nightly walk enqueues `enrich` at `NEW`; a client then opens the title.
    The stored row has to move to `DEMAND` in place -- not duplicate, and not
    stay at `NEW`.
    """
    title = await _seed_title(session, EnrichmentState.SKELETON)
    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.NEW)]
    )

    await service.detail(title.id, user_id=user_id)

    claimed = await queue.claim([JobKind.ENRICH], limit=10)
    assert [(job.key, job.priority) for job in claimed] == [(str(title.id), JobPriority.DEMAND)]


async def test_opening_a_parked_title_leaves_it_parked_at_its_own_priority(
    service: TitleReadService, queue: PostgresJobQueue, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """PRD 08's "re-enqueueing does not un-park, and a parked job's priority
    is not promoted behind their back either" -- both halves, against the
    statement that actually enforces them."""
    title = await _seed_title(session, EnrichmentState.STUB)
    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.NEW)]
    )
    claimed = await queue.claim([JobKind.ENRICH], limit=1)
    await queue.fail(claimed[0].id, error="TMDb answered 404", retryable=False)

    detail = await service.detail(title.id, user_id=user_id)

    assert detail is not None
    parked = await queue.parked()
    assert [(job.status, job.priority) for job in parked] == [(JobStatus.PARKED, JobPriority.NEW)]
    assert await queue.claim([JobKind.ENRICH], limit=10) == []


async def test_availability_spans_two_sources_and_keeps_a_retracted_copy(
    service: TitleReadService, session: AsyncSession, source: Source, user_id: uuid.UUID
) -> None:
    """Two real `sources` rows and two real foreign keys.

    The retracted copy is the point: PRD 08's rule says a degraded source
    narrows the answer rather than failing it, and what "narrowed" means on
    the wire is a copy still present with `available = false`. The sweep that
    produced it is a real `UPDATE`, and the ordering that puts it last is
    Postgres's, not a Python `sort`.
    """
    other = Source(
        kind=SourceKind.EMBY,
        name="Loft Emby",
        base_url="https://emby2.invalid",
        credentials_ref=f"ref-{uuid.uuid4()}",
        device_id=str(uuid.uuid4()),
    )
    await PostgresSourceRepository(session).add(other)
    title = await _seed_title(session, EnrichmentState.ENRICHED)
    await _seed_copy(session, source_id=source.id, title_id=title.id, external_id="mine")
    await _seed_copy(session, source_id=other.id, title_id=title.id, external_id="theirs")
    await PostgresMediaItemRepository(session).mark_unseen_unavailable(
        other.id, seen_since=datetime(2026, 8, 2, tzinfo=UTC), max_retract_fraction=1.0
    )

    detail = await service.detail(title.id, user_id=user_id)

    assert detail is not None
    assert [(copy.source_name, copy.available) for copy in detail.availability] == [
        ("Living Room Emby", True),
        ("Loft Emby", False),
    ]


async def test_an_episodes_watch_state_does_not_leak_onto_its_series(
    service: TitleReadService, session: AsyncSession, source: Source, user_id: uuid.UUID
) -> None:
    """`watch_states` has a `num_nonnulls(title_id, episode_id) = 1` CHECK, so
    an episode's state and its series' state are separate rows that a dict
    cannot keep apart by constraint -- and `get_for_title` on a series must
    not pick up whichever of its episodes the planner reached first. Same
    asymmetry `resolve_external_ids`' title branch needed `episode_id IS
    NULL` for, and the same one `list_for_title` now needs it for.
    """
    series = await _seed_title(session, EnrichmentState.ENRICHED, kind=TitleKind.SERIES)
    season, episode = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :t, 1)"),
        {"id": season, "t": series.id},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :t, :s, 1, 1)"
        ),
        {"id": episode, "t": series.id, "s": season},
    )
    await _seed_copy(session, source_id=source.id, title_id=series.id, external_id="series-1")
    await _seed_copy(
        session,
        source_id=source.id,
        title_id=series.id,
        external_id="episode-1",
        episode_id=episode,
    )
    await PostgresWatchStateRepository(session).merge_from_source(
        [
            WatchStateMerge(
                user_id=user_id,
                title_id=None,
                episode_id=episode,
                position_seconds=1840,
                played=False,
                runtime_seconds=2700,
                observed_at=SEEN_AT,
            )
        ]
    )

    detail = await service.detail(series.id, user_id=user_id)

    assert detail is not None
    assert detail.watch_state is None, "an episode's progress is not the series' progress"
    assert [copy.external_id for copy in detail.availability] == ["series-1"]


async def test_a_read_of_a_title_with_no_source_row_answers_rather_than_raising(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """The catalog is 1,271,138 titles against one source's 1,126,789 items,
    89% of them episodes, so "on no source" is the majority state and has to
    be a normal 200-shaped answer rather than an absence."""
    title = await _seed_title(session, EnrichmentState.SKELETON)
    detail = await service.detail(title.id, user_id=user_id)
    assert detail is not None
    assert detail.availability == ()
    assert detail.watch_state is None
