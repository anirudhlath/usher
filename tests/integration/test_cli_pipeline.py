"""The CLI's ingest commands, run for real against a live PostgreSQL.

`tests/unit/test_cli.py` covers the argument surface; nothing there
constructs a repository, and a composition root's whole job is construction.
The plan's own Step 3 verified these by hand against `docker compose`
(`sync-status`, `unmatched`, `work --once` on an empty database, all
exiting 0) -- this is that check, automated, because "every command works
before a sync has ever run" is exactly the property an operator needs when
diagnosing why the sync did not happen, and it is the property a wiring
change silently breaks.

**These tests do not use the `session` fixture, and that is the point.**
Each command owns its own engine, session, and transaction -- built from
`Settings` the way the real process builds them -- so what runs here is the
real `build_engine` -> `build_session_factory` -> `_build_pipeline` chain
rather than a hand-assembled one. The cost is that they commit for real
against the session-scoped container instead of rolling back, so
`_clean_slate` deletes what they wrote. It also drops any `stg_*` table:
`usher.db.staging` creates staging tables with DDL, Postgres DDL is
transactional, and a *committing* test is the only kind that can leak one --
which surfaces as schema drift in `test_migrations.py`, a different file
that then fails only in combination.

No test here reaches a network. `_open_adapter` is exercised on the branch
where the credential row is missing, which answers before an adapter is
built; anything that reached `EmbyAdapter` would resolve a hostname.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.cli import _open_adapter, _session_for, _sync_status, _unmatched, _work
from usher.composition import build_pipeline, selected_sources
from usher.config import Settings, get_settings
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.users import DEFAULT_USER_NAME, ensure_default_user
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.ports.ingest import MediaItemUpsert
from usher.ports.jobs import JobRequest


@pytest.fixture
def cli_settings(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """`Settings` as `usher.cli.main` builds them: from the environment,
    through the cached `get_settings()`.

    The CLI reads `get_settings()` and the API reads `app.state.settings`,
    and that asymmetry is deliberate (M3 found `Depends(get_settings)`
    re-reads `os.environ` and failed 13 of 15 tests). Building them the same
    way the CLI does is what makes this a test of the CLI's own root.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def clean_slate(cli_settings: Settings) -> AsyncIterator[None]:
    """Undo what a committing test wrote, in both directions.

    Runs before as well as after: a previous run of this file that died
    between the two would otherwise leave rows that make the next run's
    counts wrong, and a test whose isolation depends on the last one having
    finished cleanly is not isolated.
    """
    await _purge(cli_settings)
    yield
    await _purge(cli_settings)


async def _purge(settings: Settings) -> None:
    async with _session_for(settings) as session:
        for statement in (
            "DELETE FROM jobs",
            "DELETE FROM watch_states",
            "DELETE FROM media_items",
            "DELETE FROM users WHERE name = 'default'",
            "DELETE FROM sources WHERE name LIKE 'cli-%'",
            "DELETE FROM titles WHERE sort_name = 'cli-orphan'",
            "DROP TABLE IF EXISTS stg_jobs",
            "DROP TABLE IF EXISTS stg_media_items",
        ):
            await session.execute(text(statement))
        await session.commit()


async def test_sync_status_works_before_any_sync_has_run(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command that only works after a successful sync is a command an
    operator cannot use to find out why the sync did not happen. Every
    `JobKind` is reported, including the empty ones -- a queue that stops
    reporting a kind is indistinguishable from one reporting zero."""
    await _sync_status(cli_settings)
    printed = capsys.readouterr().out
    for kind in JobKind:
        assert f"queue {kind.value}" in printed
    assert "parked jobs: 0" in printed


async def test_unmatched_reports_an_empty_review_queue(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    await _unmatched(cli_settings, limit=50, offset=0, resolve=None, title=None)
    assert "nothing unmatched" in capsys.readouterr().out


async def test_work_runs_a_pass_over_an_empty_queue(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`work --once` against an empty database. It builds every service, the
    handler for all three kinds it can serve, requeues whatever a previous
    process left `running`, claims nothing, and exits -- and it creates the
    singleton default user on the way, which nothing before M4 ever did and
    without which `watch_states.user_id` has no row to point at."""
    await _work(cli_settings, once=True)
    assert "0 jobs" in capsys.readouterr().out
    async with _session_for(cli_settings) as session:
        stored = (
            await session.execute(
                text("SELECT id FROM users WHERE name = :name"), {"name": DEFAULT_USER_NAME}
            )
        ).scalar_one()
    assert stored is not None


async def test_the_default_user_is_created_once_and_is_stable(
    cli_settings: Settings, clean_slate: None
) -> None:
    """Two runs of the same command must write history to the same user.
    `is_default` is a plain boolean with no partial unique index behind it,
    so "the default user" is a stable *choice* rather than a constraint."""
    async with _session_for(cli_settings) as session:
        first = await ensure_default_user(session)
        await session.commit()
    async with _session_for(cli_settings) as session:
        second = await ensure_default_user(session)
        await session.commit()
        count = (
            await session.execute(
                text("SELECT count(*) FROM users WHERE name = :name"),
                {"name": DEFAULT_USER_NAME},
            )
        ).scalar_one()
    assert first == second
    assert count == 1


async def test_work_completes_a_job_for_an_item_no_source_addresses(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `match` job for an item no configured source addresses completes
    rather than parks (PRD 08 reserves parking for work a human must look
    at), so the worker's loop is exercised end to end without a network
    call: `SourceRegistry.resolve` answers `None` from local state alone.
    """
    async with _session_for(cli_settings) as session:
        pipeline = build_pipeline(session, cli_settings)
        await pipeline.queue.enqueue(
            [JobRequest(kind=JobKind.MATCH, key="nobody-has-this", priority=JobPriority.NEW)]
        )
        await session.commit()
    await _work(cli_settings, once=True)
    assert "1 jobs" in capsys.readouterr().out
    async with _session_for(cli_settings) as session:
        remaining = (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
    assert remaining == 0, "a job for an item nothing addresses is done, not poison"


async def test_unmatched_lists_and_resolves_through_the_real_repository(
    cli_settings: Settings,
    clean_slate: None,
    capsys: pytest.CaptureFixture[str],
    session: AsyncSession,
) -> None:
    """The review queue's two halves, against real SQL. `--resolve` writes
    `title_id` and leaves `episode_id` null, which `attach_title` documents
    as the deliberate act of a human rather than a walk's "I did not look".
    """
    title_id = new_id()
    async with _session_for(cli_settings) as own:
        source = Source(
            kind=SourceKind.EMBY,
            name="cli-source",
            base_url="https://emby.invalid",
            credentials_ref=f"ref-{new_id()}",
            device_id=str(new_id()),
        )
        await PostgresSourceRepository(own).add(source)
        await own.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name, enrichment_state) "
                "VALUES (:id, 'movie', 'cli-orphan', 'cli-orphan', 'skeleton')"
            ),
            {"id": title_id},
        )
        pipeline = build_pipeline(own, cli_settings)
        await pipeline.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id="unmatched-1",
                    title_id=None,
                    episode_id=None,
                    container="mkv",
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=None,
                    last_seen_at=source.created_at,
                )
            ]
        )
        await own.commit()
        stored = await pipeline.media_items.get_by_external_id(source.id, "unmatched-1")
    assert stored is not None

    await _unmatched(cli_settings, limit=50, offset=0, resolve=None, title=None)
    assert "unmatched-1" in capsys.readouterr().out

    await _unmatched(cli_settings, limit=50, offset=0, resolve=str(stored.id), title=str(title_id))
    assert "resolved" in capsys.readouterr().out

    async with _session_for(cli_settings) as own:
        row = (
            await own.execute(
                text("SELECT title_id, episode_id FROM media_items WHERE id = :id"),
                {"id": stored.id},
            )
        ).one()
    assert row.title_id == title_id
    assert row.episode_id is None

    # And it leaves the review queue: `list_unmatched` is `title_id IS NULL`,
    # so a resolution that wrote nothing would still list here.
    await _unmatched(cli_settings, limit=50, offset=0, resolve=None, title=None)
    assert "nothing unmatched" in capsys.readouterr().out


async def test_resolving_an_item_that_does_not_exist_says_so(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    await _unmatched(
        cli_settings, limit=50, offset=0, resolve=str(uuid.uuid4()), title=str(new_id())
    )
    assert "no such media item" in capsys.readouterr().out


async def test_a_disabled_source_is_never_walked(session: AsyncSession) -> None:
    """`enabled` is how an operator parks a server that is being rebuilt.
    Honouring `--source` over the flag would walk it anyway -- and a full
    walk of a half-restored library is exactly the shape ADR-0015's
    retraction guard exists to catch after the fact."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher", secret_key="0" * 32
    )
    pipeline = build_pipeline(session, settings)
    disabled = Source(
        kind=SourceKind.EMBY,
        name="cli-disabled",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
        enabled=False,
    )
    await PostgresSourceRepository(session).add(disabled)
    assert await selected_sources(pipeline, None) == []
    assert await selected_sources(pipeline, "cli-disabled") == []


async def test_a_source_with_no_credential_row_is_skipped_not_crashed(
    session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator running `usher sync` across three sources needs the
    second and third to run when the first's credential row has gone
    missing -- the same reasoning `ReconcileService.reconcile` applies one
    layer down to an unreachable server. Answered from local state, so no
    adapter is built and no hostname is resolved."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher", secret_key="0" * 32
    )
    pipeline = build_pipeline(session, settings)
    source = Source(
        kind=SourceKind.EMBY,
        name="cli-uncredentialed",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    await PostgresSourceRepository(session).add(source)
    assert await _open_adapter(pipeline, source) is None
    assert "no stored credentials" in capsys.readouterr().out


def test_allow_full_retraction_is_the_only_way_past_the_ceiling(session: AsyncSession) -> None:
    """ADR-0015's ceiling reaches the service from `Settings` unless the
    operator passed the flag, and the flag means exactly 1.0 -- "retract
    whatever you find", which is right only for a library that really was
    removed."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher",
        secret_key="0" * 32,
        sync_max_retract_fraction=0.1,
    )
    guarded = build_pipeline(session, settings)
    opened = build_pipeline(session, settings, max_retract_fraction=1.0)
    assert guarded.reconcile._max_retract_fraction == 0.1
    assert opened.reconcile._max_retract_fraction == 1.0
