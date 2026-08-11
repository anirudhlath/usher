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
`_clean_slate` deletes what they wrote. It used to drop `stg_*` tables too
-- `usher.db.staging` created them with DDL, Postgres DDL is transactional,
and a *committing* test was the only kind that could leak one, which
surfaced as schema drift in `test_migrations.py`, a different file that then
failed only in combination. M6's `CREATE TEMP TABLE ... ON COMMIT DROP`
removed the leak, so those two lines are gone rather than kept as a cleanup
that can no longer fire.

No test here reaches a network. `_open_adapter` is exercised on the branch
where the credential row is missing, which answers before an adapter is
built; anything that reached `EmbyAdapter` would resolve a hostname. The
same holds for `usher push --probe`, whose whole job is opening a socket:
the two cases below exercise "nothing to probe" and "no credential to probe
with", both of which answer from local state.
"""

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.embedding import FakeEmbedder
from tests.fakes.llm_client import FakeLLMClient, usage
from usher.cli import (
    _curate,
    _home,
    _open_adapter,
    _push,
    _search,
    _session_for,
    _similar,
    _suggest,
    _sync_status,
    _unmatched,
    _work,
)
from usher.composition import build_pipeline, nothing, selected_sources
from usher.config import Settings, get_settings
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.users import DEFAULT_USER_NAME, ensure_default_user
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert
from usher.ports.jobs import JobRequest
from usher.ports.repository import ScoredNeighbor, TitleEmbeddingUpsert
from usher.ports.search import SearchFilters
from usher.services.curation_validate import (
    ITEM_IDS_KEY,
    REASON_KEY,
    ROWS_KEY,
    TITLE_KEY,
    DropReason,
)

# The blend these arranged rows claim to have been computed under. A literal,
# never `blend_fingerprint()`: a case that inherits today's fingerprint cannot
# express "this row came from a different blend", which is the whole state the
# column exists to describe.
_FP = "arranged-by-a-test"

# The shape every `(thing, close it)` pair in `usher.composition` returns.
AsyncCloser = Callable[[], Awaitable[None]]


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
            # M8's cost ledger, which cascades from nothing and has no
            # `user_id` to scope a delete by -- so a committing curate case
            # has to clear the table.
            "DELETE FROM llm_calls",
            "DELETE FROM watch_states",
            "DELETE FROM media_items",
            "DELETE FROM users WHERE name = 'default'",
            "DELETE FROM sources WHERE name LIKE 'cli-%'",
            "DELETE FROM titles WHERE sort_name = 'cli-orphan'",
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
    """`work --once` against an empty database. It builds every service and a
    handler per kind this deployment can serve -- **two** of the six here,
    since `enrich` and `derive` want a TMDb key, `index` wants the embedding
    extra and `curate` wants `USHER_LLM_ENABLED`, and this fixture configures
    none of the three -- requeues whatever a previous process left `running`,
    claims nothing, and exits. It creates the singleton default user on the
    way, which nothing before M4 ever did and without which
    `watch_states.user_id` has no row to point at."""
    await _work(cli_settings, once=True)
    assert "0 jobs" in capsys.readouterr().out
    async with _session_for(cli_settings) as session:
        stored = (
            await session.execute(
                text("SELECT id FROM users WHERE name = :name"), {"name": DEFAULT_USER_NAME}
            )
        ).scalar_one()
    assert stored is not None


async def test_work_parks_a_curate_job_it_cannot_serve_and_buys_nothing(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    clean_slate: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**`usher work` is the second composition root, and this is the only
    thing that says it builds an `LLMClient` at all.**

    `tests/integration/test_lanes_in_the_server_process.py` makes the same
    claim about `create_app`; the two roots are exactly what
    `usher.composition` exists to keep in step, and "one root gains a
    collaborator and the other keeps answering as though it had none" is the
    drift that module's docstring names. Without this case, deleting
    `_work`'s `llm_client(settings)` leaves the whole suite green and turns
    `USHER_LLM_ENABLED=true` on a split deployment into a queue that grows
    forever.

    Parked rather than completed, and against an **empty** database on
    purpose: `CurationService` refuses an empty candidate pool with
    `PortDataMalformed` *before* the client is touched, so this exercises the
    whole claim/handle/classify path at a cost of nothing and opens no socket
    -- which is the same reason PRD 08's "every command works against an
    empty database" is the rule this file is built around.

    Its own settings rather than the shared `cli_settings` fixture, because
    the one thing under test is a setting the shared fixture does not set.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)
    monkeypatch.setenv("USHER_LLM_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_enabled is True, "the premise: the fixture really turned it on"
    household = new_id()
    async with _session_for(settings) as session:
        await build_pipeline(session, settings).queue.enqueue(
            [JobRequest(kind=JobKind.CURATE, key=str(household), priority=JobPriority.BACKFILL)]
        )
        await session.commit()

    await _work(settings, once=True)

    assert "1 jobs" in capsys.readouterr().out
    async with _session_for(settings) as session:
        status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE kind = 'curate' AND key = :k"),
                {"k": str(household)},
            )
        ).scalar_one()
        # The whole table: `llm_calls` carries no `user_id` -- `generation_id`
        # is its only correlation key -- and `_purge` empties it either side.
        billed = (await session.execute(text("SELECT count(*) FROM llm_calls"))).scalar_one()
    assert status == "parked", "the curate job was never claimed; this root built no LLM client"
    assert int(billed) == 0, "an empty catalog was billed for a completion"
    get_settings.cache_clear()


async def test_work_releases_every_process_resource_it_built(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, clean_slate: None
) -> None:
    """The `usher work` half of `create_app`'s
    `test_the_lifespan_releases_every_process_resource_it_built`, and the two
    are one claim about two roots -- which is what `usher.composition` exists
    to keep in step.

    Measured before writing it: deleting any one of `aclose()`,
    `aclose_model()` or `aclose_client()` from `_work`'s `finally` left
    `tests/unit` and `tests/integration` fully green. `aclose_client()` is
    M8's line and the other two are inherited; all three are pinned here
    rather than only the new one.

    The three factories are substituted for the reason the API case states:
    on this deployment's settings the real ones all answer
    `(None, composition.nothing)`, and `nothing` is one shared module-level
    no-op, so a real run cannot tell "released the thing" from "awaited the
    no-op". `--once` so the loop exits and the `finally` actually runs.
    """
    calls: list[str] = []

    def _factory(name: str) -> Callable[..., Awaitable[tuple[None, AsyncCloser]]]:
        async def _build(*_: object, **__: object) -> tuple[None, AsyncCloser]:
            async def _close() -> None:
                calls.append(name)

            return None, _close

        return _build

    monkeypatch.setattr("usher.cli.metadata_provider", _factory("provider"))
    monkeypatch.setattr("usher.cli.embedder", _factory("embedder"))
    monkeypatch.setattr("usher.cli.llm_client", _factory("client"))

    await _work(cli_settings, once=True)

    assert sorted(calls) == ["client", "embedder", "provider"], (
        "`usher work` built three process-lifetime resources and did not release all three"
    )


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


async def test_push_probe_reports_nothing_to_probe_before_it_opens_anything(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`usher push --probe` against an empty deployment must answer, not
    crash -- the same rule `sync-status` follows: a command an operator can
    only run *after* a working source is no use for diagnosing why the
    source is not working."""
    await _push(cli_settings, source_name=None, probe=True)
    assert "no enabled sources configured" in capsys.readouterr().out
    await _push(cli_settings, source_name="cli-nothing", probe=True)
    assert "no enabled source matched" in capsys.readouterr().out


async def test_push_probe_skips_a_source_whose_credential_row_is_gone(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Answered from local state, so no adapter is built and no hostname is
    resolved -- which is what keeps this file's "no test here reaches a
    network" claim true of a command whose whole job is opening a socket."""
    async with _session_for(cli_settings) as session:
        await PostgresSourceRepository(session).add(
            Source(
                kind=SourceKind.EMBY,
                name="cli-probe-uncredentialed",
                base_url="https://emby.invalid",
                credentials_ref=f"ref-{new_id()}",
                device_id=str(new_id()),
            )
        )
        await session.commit()
    await _push(cli_settings, source_name="cli-probe-uncredentialed", probe=True)
    printed = capsys.readouterr().out
    assert "no stored credentials" in printed
    assert "upgraded=" not in printed


# -- `usher search` / `usher suggest`, against the real indexes ------------
#
# Every title here is synthetic (`tests/fixtures/README.md`'s bands), and each
# carries `sort_name = _SEARCH_MARK` so `_purge_search` can find its own rows
# without a blanket `DELETE FROM titles` that could reach another committing
# file's work.

_SEARCH_MARK = "cli-search"


def _searchable(name: str, *, overview: str | None = None) -> Title:
    return Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=_SEARCH_MARK,
        overview=overview,
        year=2021,
        enrichment_state=EnrichmentState.ENRICHED,
    )


async def _seed_searchable(settings: Settings, titles: Sequence[Title]) -> None:
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        for title in titles:
            await pipeline.titles.add(title)
        await session.commit()


@pytest_asyncio.fixture
async def clean_search(cli_settings: Settings) -> AsyncIterator[None]:
    await _purge_search(cli_settings)
    yield
    await _purge_search(cli_settings)


async def _purge_search(settings: Settings) -> None:
    async with _session_for(settings) as session:
        for statement in (
            "DELETE FROM title_neighbors WHERE title_id IN "
            "(SELECT id FROM titles WHERE sort_name = :mark) "
            "OR neighbor_id IN (SELECT id FROM titles WHERE sort_name = :mark)",
            "DELETE FROM title_embeddings WHERE title_id IN "
            "(SELECT id FROM titles WHERE sort_name = :mark)",
        ):
            await session.execute(text(statement), {"mark": _SEARCH_MARK})
        await session.execute(
            text("DELETE FROM titles WHERE sort_name = :mark"), {"mark": _SEARCH_MARK}
        )
        await session.commit()


@pytest.fixture
def a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """`composition.embedder` replaced with one that answers.

    The `embedding` extra is not installed in this environment (167 MiB and a
    65 MB ONNX download for a suite that asserts on printed text), so a real
    `usher search --mode fused` here always *degrades* -- which is a genuine
    case and is the one below this. It is not the case that exercises FUSED
    with zero coverage, and the two print different sentences on purpose. The
    fake supplies a vector and nothing else; relevance is never asserted
    against it (`tests/fakes/embedding.py`'s own docstring forbids it).
    """

    async def _fake(settings: Settings, *, report: bool = True) -> tuple[FakeEmbedder, object]:
        return FakeEmbedder(model_name=settings.embedding_model), nothing

    monkeypatch.setattr("usher.cli.embedder", _fake)


async def test_search_reports_semantic_coverage_and_moves_when_something_is_embedded(
    cli_settings: Settings, clean_search: None, a_model: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The milestone's headline failure mode, arriving at the CLI.

    The wrong implementation: a `_search` that prints the rows and stops. A
    FUSED search over a catalog with no vectors returns a perfectly plausible
    ranked list -- no error, no empty result, no log line -- and an operator
    cannot tell it from a working hybrid search. The coverage figure is the
    only thing that says so.

    **Asserts the figure *moves*, not merely that it is printed**, because a
    `_search` that hardcoded `semantic_coverage=0.000` passes the first half
    alone. Coverage is the fraction of the *filtered population* that had a
    vector, so seeding one embedding of two titles has to read 0.500 and not
    1.000 -- the two agree exactly when every returned hit had one, which is
    the case a green test is most likely to have used.
    """
    first, second = _searchable("The Quiet Vacuum"), _searchable("The Second Vacuum")
    await _seed_searchable(cli_settings, [first, second])

    await _search(cli_settings, query="vacuum", mode="fused", limit=5, filters=SearchFilters())
    before = capsys.readouterr().out
    assert "semantic_coverage=0.000" in before, before
    assert "usher index --backfill" in before, before
    assert str(first.id) in before and str(second.id) in before, before

    async with _session_for(cli_settings) as session:
        pipeline = build_pipeline(session, settings=cli_settings)
        vector = (await FakeEmbedder().embed(["The Quiet Vacuum"]))[0]
        await pipeline.embeddings.upsert_many(
            [
                TitleEmbeddingUpsert(
                    title_id=first.id,
                    embedding=tuple(vector),
                    model_name=cli_settings.embedding_model,
                    source_fingerprint="fingerprint",
                )
            ]
        )
        await session.commit()

    await _search(cli_settings, query="vacuum", mode="fused", limit=5, filters=SearchFilters())
    after = capsys.readouterr().out
    assert "semantic_coverage=0.500" in after, after
    assert "usher index --backfill" not in after, after


async def test_search_says_the_deployment_has_no_model_rather_than_no_embeddings(
    cli_settings: Settings, clean_search: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two problems that present identically and need different fixes.

    `USHER_EMBEDDING_ENABLED` is false in this environment, so a FUSED request
    is *degraded* -- served as full-text because there is no model at all --
    and the fix is an extra plus a setting, not `usher index`. A single
    warning for both cases sends an operator to the wrong one half the time,
    which is exactly why `SearchAnswer` carries `requested_mode` beside
    `mode`.
    """
    await _seed_searchable(cli_settings, [_searchable("The Quiet Vacuum")])
    await _search(cli_settings, query="vacuum", mode="fused", limit=5, filters=SearchFilters())
    printed = capsys.readouterr().out
    assert "mode=full_text" in printed, printed
    assert "USHER_EMBEDDING_ENABLED" in printed, printed
    assert "usher index --backfill" not in printed, printed


async def test_semantic_search_without_a_model_refuses_rather_than_narrowing(
    cli_settings: Settings, clean_search: None
) -> None:
    """`--mode semantic` asks the one question full-text cannot answer, so a
    silent narrowing would hand back a plausible answer to a different
    question. `SystemExit` with a sentence naming the way out, the treatment
    `_as_uuid` gives a bad id."""
    await _seed_searchable(cli_settings, [_searchable("The Quiet Vacuum")])
    with pytest.raises(SystemExit, match="embedding model"):
        await _search(
            cli_settings, query="vacuum", mode="semantic", limit=5, filters=SearchFilters()
        )


async def test_search_says_no_match_rather_than_printing_nothing(
    cli_settings: Settings, clean_search: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty stdout is indistinguishable from a command that crashed
    before it printed, and this one still has a coverage line to give."""
    await _search(
        cli_settings,
        query="zzznothingmatchesthis",
        mode="full_text",
        limit=5,
        filters=SearchFilters(),
    )
    printed = capsys.readouterr().out
    assert "no match" in printed
    assert "results=0" in printed


async def test_suggest_finds_a_title_by_a_prefix_of_its_name(
    cli_settings: Settings, clean_search: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The type-ahead path end to end through the real trigram index, and
    with **no model loaded** -- `SuggestIndex` is its own port precisely
    because this tier serves the whole catalog without one."""
    title = _searchable("The Quiet Vacuum")
    await _seed_searchable(cli_settings, [title])
    await _suggest(cli_settings, prefix="The Quiet Vacu", limit=5)
    printed = capsys.readouterr().out
    assert str(title.id) in printed, printed


async def test_every_search_command_prints_and_never_logs(
    cli_settings: Settings, clean_search: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`cli._print_home_report`'s rule, asserted rather than followed.

    **Cited by name rather than by line number since 2026-08-07**: the two
    copies of this citation said `cli.py:153-154`, which had drifted onto an
    `httpx.AsyncClient` construction inside `_bootstrap` -- a line reference
    into a 1,500-line module is a citation that goes stale on the next
    insertion above it.

    With `USHER_LOG_JSON=true` -- the default -- a result routed through
    loguru is a JSON envelope wrapped around a table, per line. The wrong
    implementation is `logger.info` in a command, which looks fine in a dev
    shell with `USHER_LOG_JSON=false` and is unreadable as shipped.

    **`--mode fused` is in this list because a *collaborator* broke the rule,
    not the command.** Found by the operator smoke run rather than by this
    suite: `composition.embedder` reports "no embedding model configured;
    index jobs will not be claimed" once per process, which is exactly right
    for `usher work` and is, for a search, a JSON envelope printed in front of
    the results carrying advice about a lane this process does not run -- and
    duplicating, worse, the warning `_search` prints itself. A version of this
    case driving only `--mode full_text` never reaches the factory at all and
    passes against that.
    """
    await _seed_searchable(cli_settings, [_searchable("The Quiet Vacuum")])
    sink: list[str] = []
    handler = logger.add(sink.append, level="DEBUG")
    try:
        for mode in ("full_text", "fused"):
            await _search(cli_settings, query="vacuum", mode=mode, limit=5, filters=SearchFilters())
        await _suggest(cli_settings, prefix="quiet", limit=5)
        await _similar(cli_settings, title_id=new_id(), limit=5, rebuild=False)
    finally:
        logger.remove(handler)
    assert capsys.readouterr().out, "the commands printed nothing at all"
    assert sink == [], f"a search command logged instead of printing: {sink}"


async def test_similar_says_whether_the_neighbours_were_ever_computed(
    cli_settings: Settings, clean_search: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`title_neighbors` is a batch artefact, so an empty answer has two causes
    and only one is a fact about the title.

    The wrong implementation prints one message for both, and it sends an
    operator to look at the wrong thing exactly half the time: "no neighbours
    for this title" means run nothing, and "no neighbours have ever been
    computed" means run `usher similar --rebuild`. This is the one place in
    M6 where freshness is a whole-artefact age rather than a per-row
    fingerprint (`TitleNeighborRepository.computed_at`), which is why the
    distinction has to be made in the command rather than derived from a row.

    Landed here rather than with the command it tests: `usher similar` shipped
    with Task 21 and both sentences, and neither was ever asserted.
    """
    seed, other = _searchable("The Quiet Vacuum"), _searchable("Vane 4417")
    await _seed_searchable(cli_settings, [seed, other])

    await _similar(cli_settings, title_id=seed.id, limit=5, rebuild=False)
    assert "no neighbours have ever been computed" in capsys.readouterr().out

    # A row for *some other* seed, so the table has an age while this title
    # still has nothing -- which is the state the two messages differ on.
    async with _session_for(cli_settings) as session:
        pipeline = build_pipeline(session, settings=cli_settings)
        await pipeline.neighbors.replace(
            [other.id],
            [ScoredNeighbor(title_id=other.id, neighbor_title_id=seed.id, score=0.5, rank=0)],
            blend_fingerprint=_FP,
        )
        await session.commit()

    await _similar(cli_settings, title_id=seed.id, limit=5, rebuild=False)
    printed = capsys.readouterr().out
    assert "no neighbours for this title" in printed, printed
    assert "have ever been computed" not in printed, printed


async def test_home_composes_a_screen_against_an_empty_database(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """**PRD 08's operator rule, and the arithmetic it is hunting.**

    The failure here is not a missing row -- it is that the taste centroid is a
    mean, and the mean of zero embeddings is `0/0`. An empty household is a
    fact rather than an error, so this exits 0 and prints a report in which
    every provider proposed nothing.

    **Driven against real Postgres rather than as a unit case**, which is a
    correction to this milestone's plan: it specifies
    `tests/unit/test_cli_home.py` with `empty_db`/`seeded_db` fixtures, and no
    such seam exists -- every command coroutine in `usher.cli` takes a
    `Settings` and opens its own engine through `_session_for`. The plan's own
    "every operator command has to work against an empty database" is also
    exactly the claim a fake database cannot make.
    """
    await _home(cli_settings, limit=10, repeat=1)

    out = capsys.readouterr().out
    assert "10 providers, 10 proposed nothing" in out
    assert "screen: 0 rows, 0 cards" in out


async def test_home_prints_a_line_for_a_provider_that_proposed_nothing(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """**An absent provider and a silent one are the two states this milestone
    exists to distinguish**, and a report that drops the silent ones makes them
    indistinguishable from unregistered ones -- which is exactly how a provider
    left out of `ROW_PROVIDERS` survives review.

    Kills a report built by iterating the *proposals* rather than the registry.
    Asserted by name for every one of the **ten**, because a count is satisfied
    by a report printing one provider ten times.

    **A superset passes this case and that is why the tenth had to be added
    deliberately.** M8 registered `CuratedProvider` and every assertion below
    stayed green -- the report simply grew a line nothing looked at -- which is
    the same shape as the report dropping a silent provider, arriving from the
    other direction. The count in
    `test_home_composes_a_screen_against_an_empty_database` above is what
    actually failed, so the two cases are load-bearing together and neither is
    on its own.
    """
    await _home(cli_settings, limit=10, repeat=1)

    lines = capsys.readouterr().out.splitlines()
    for slug in (
        "continue-watching",
        "next-up",
        "recently-added",
        "rediscover",
        "because-you-watched",
        "franchise",
        "genre-affinity",
        "seasonal",
        "people",
        "curated",
    ):
        assert any(line.startswith(slug) and " 0 " in line for line in lines), slug


async def test_home_prints_a_cold_and_a_warm_composition(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """**The only measurement of the cache this milestone has**, because
    `usher.cache.hits`/`.misses` is M9's. A `--repeat` that measured cache hits
    would report a number near zero and mean nothing, so each repeat clears the
    cache and the warm read is timed once, separately, and labelled.

    The threshold line is asserted too: a boundary call that promises a
    measurement and prints no number is a boundary call nobody can act on.
    """
    await _home(cli_settings, limit=10, repeat=3)

    out = capsys.readouterr().out
    assert "compose (cold)" in out
    assert "over 3 run(s)" in out
    assert "compose (warm, from cache)" in out
    assert "p95 > 400 ms" in out
    # **Every repeat is cold**, and this is what says so: without the clear,
    # runs 2 and 3 are screen-cache hits, the last report carries no providers
    # at all, and the table above it is empty -- a measurement that silently
    # became a benchmark of a dict.
    assert "seasonal" in out, "the last run was a cache hit, so --repeat measured the cache"


async def test_home_prints_and_never_logs_its_answer(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The split every command in this module makes: `loguru` output is
    operational and goes to a sink an operator may not be reading; a command's
    answer is stdout, which is what gets piped."""
    await _home(cli_settings, limit=10, repeat=1)

    assert capsys.readouterr().out.strip()


# --------------------------------------------------------------- usher curate
#
# **Driven against real Postgres for the reason `usher home`'s cases are**: the
# command coroutine takes a `Settings` and builds its own engine, its own
# pipeline and its own `CurationService` through `_session_for`, so the three
# arms below are the only place that wiring is executed at all.
#
# The *client* is the one collaborator these substitute, and it is substituted
# rather than pointed at a dead endpoint for two reasons that point the same
# way: nothing in this suite may open a socket, and a scripted response is what
# makes "what did the validator do with a real completion" observable end to
# end. `tests/unit/test_cli_curate.py` covers the arm with no client at all,
# which is the one that must answer before a connection is opened.

_CURATE_MARK = "cli-curate"

#: `curation_validate.DEFAULT_MIN_CARDS`, which is what a kept row needs. Not
#: imported as a bound to compare against -- the fixtures below are *built*
#: from it, so a change to the floor changes what these cases seed rather than
#: silently making them assert a row the validator would now discard.
_CARDS = 5


def _curatable(name: str) -> Title:
    """One candidate. No `media_items` row, deliberately: ownership is a *sort
    key* in `list_unwatched_candidates` and never a filter, so an unowned
    title is an eligible candidate and seeding a library would test the
    ordering instead of the wiring."""
    return Title(kind=TitleKind.MOVIE, name=name, sort_name=_CURATE_MARK, year=2021)


async def _seed_candidates(settings: Settings, count: int) -> list[Title]:
    titles = [_curatable(f"An Invented Film {index}") for index in range(count)]
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        for title in titles:
            await pipeline.titles.add(title)
        await session.commit()
    return titles


def _completion(handles: Sequence[int]) -> dict[str, Any]:
    """One shelf, addressed by handle. Written through the validator's own four
    exported key constants rather than by retyping `"rows"`/`"item_ids"`: a
    fixture saying `ids` and a reader saying `item_ids` is a case that asserts
    a 100% drop and calls it coverage."""
    return {
        ROWS_KEY: [
            {
                TITLE_KEY: "Slow-burn sci-fi for a rainy night",
                REASON_KEY: "Because this household finished three of these.",
                ITEM_IDS_KEY: list(handles),
            }
        ]
    }


@pytest.fixture
def scripted_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLMClient:
    """`composition.llm_client` replaced with one that answers on a script.

    Substituted on `usher.cli` rather than on `usher.composition`, which is
    where `_curate` looks it up -- and returning the fake's own `aclose` so a
    case can see whether the command released the process resource it built.
    """
    client = FakeLLMClient()

    async def _factory(
        settings: Settings, *, report: bool = True
    ) -> tuple[FakeLLMClient, Callable[[], Awaitable[None]]]:
        return client, client.aclose

    monkeypatch.setattr("usher.cli.llm_client", _factory)
    return client


@pytest_asyncio.fixture
async def clean_curation(cli_settings: Settings) -> AsyncIterator[None]:
    await _purge_curation(cli_settings)
    yield
    await _purge_curation(cli_settings)


async def _purge_curation(settings: Settings) -> None:
    async with _session_for(settings) as session:
        for statement in (
            # The ledger carries no `user_id` -- `generation_id` is its only
            # correlation key -- so a committing curate case has to clear the
            # whole table rather than scope a delete by household.
            "DELETE FROM llm_calls",
            # `curated_rows` and the stored taste profile both cascade from
            # `users`, so this one delete reaches everything the run wrote for
            # the household.
            "DELETE FROM users WHERE name = 'default'",
        ):
            await session.execute(text(statement))
        await session.execute(
            text("DELETE FROM titles WHERE sort_name = :mark"), {"mark": _CURATE_MARK}
        )
        await session.commit()


async def test_curate_against_an_empty_database_says_so_and_buys_nothing(
    cli_settings: Settings,
    clean_slate: None,
    clean_curation: None,
    scripted_llm: FakeLLMClient,
) -> None:
    """**PRD 08's operator rule, on the one command in this project that spends
    money.**

    An empty catalog produces an empty candidate pool, and
    `CurationService.generate` refuses it *before the client is touched* --
    which is the whole of why this case can exist in a suite that opens no
    socket. Four things are asserted and the last three are the ones with
    teeth:

    - the operator gets a sentence rather than sixty frames;
    - **the sentence names nothing an operator cannot look up**, which until
      2026-08-07 it did -- see the comment on that assertion;
    - **nothing was attempted**, so `complete_json` was never called. A
      generation for a household with nothing to recommend is a charge with a
      guaranteed empty answer;
    - **nothing was billed.** This is the one path in the milestone that
      writes no `llm_calls` row at all, and the rule the service implements is
      `record()` on every path that *attempted* a call -- a row here would be
      spend an operator has to explain away.
    """
    with pytest.raises(SystemExit) as exit_info:
        await _curate(cli_settings)

    message = str(exit_info.value)
    assert "the candidate pool is empty" in message, message
    assert "Traceback" not in message, message
    # The fact none of the raised messages carries and every operator asks
    # first, on the run where the screen looks unchanged.
    assert "previous rows still stand" in message, message
    # **The sentence's only concrete token was the household's uuid until
    # 2026-08-07**, carried as the raise's `detail`. `build_parser` refuses a
    # `--user` flag on the grounds that a household id is "an id an operator has
    # no way to look up on a deployment that has exactly one", so the command
    # was printing the one token its own parser argues nobody can read.
    # `test_the_empty_pool_message_carries_no_household_id` pins the raise; this
    # asserts the *rendered* sentence, which is the surface that argument is
    # about -- and it matches a uuid shape rather than this run's id because the
    # user row is flushed and never committed, so there is no id to read back.
    assert re.search(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", message) is None, message
    assert scripted_llm.calls == [], "an empty catalog bought a completion"
    assert scripted_llm.closed == 1, "the command did not release the client it built"
    async with _session_for(cli_settings) as session:
        billed = (await session.execute(text("SELECT count(*) FROM llm_calls"))).scalar_one()
    assert int(billed) == 0, "an empty catalog was billed for a completion"


async def test_curate_writes_a_generation_and_reports_what_it_bought(
    cli_settings: Settings,
    clean_slate: None,
    clean_curation: None,
    scripted_llm: FakeLLMClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole path, end to end and through the real wiring: pool → prompt →
    one completion → validate → `replace_for_user` → ledger → one commit.

    **The pool size is asserted as a number larger than what was kept**, which
    is the property `CurationReport` carries it for: a command that summed the
    rows it was handed would print `5` for a generation chosen from ten
    candidates, and the ratio is what an operator reads to decide whether the
    pool is big enough. Asserted as an inequality rather than as `10` because
    this file commits against a session-scoped container and the catalog is not
    this case's to own.

    The two rows the run has to leave behind are checked in the database rather
    than in the output -- a report is what the command *said*, and PRD 10's
    dashboard 5 is `llm_calls JOIN curated_rows USING (generation_id)`.
    """
    await _seed_candidates(cli_settings, 10)
    scripted_llm.responses.append(_completion(range(1, _CARDS + 1)))
    scripted_llm.usages.append(
        usage(
            model="served/actually-2",
            tokens_in=4_812,
            tokens_out=391,
            cost_usd=Decimal("0.00042100"),
            latency_ms=2_314,
        )
    )

    await _curate(cli_settings)

    out = capsys.readouterr().out
    pool = int(re.search(r"pool: (\d+) candidates", out).group(1))  # type: ignore[union-attr]
    assert pool >= 10, out
    assert pool > _CARDS, "the pool size was the kept-card count, not the pool"
    assert f"kept: 1 row, {_CARDS} cards" in out, out
    assert "Slow-burn sci-fi for a rainy night" in out, out
    for reason in DropReason:
        assert reason.value in out, f"{reason.value} is missing from the report: {out}"
    assert "4812 in, 391 out" in out, out
    assert "$0.00042100" in out, out
    assert "served/actually-2" in out, out
    assert len(scripted_llm.calls) == 1, "PRD 06's one completion per household per run"

    async with _session_for(cli_settings) as session:
        rows = (
            await session.execute(
                text("SELECT slug, generation_id FROM curated_rows ORDER BY position")
            )
        ).all()
        ledger = (
            await session.execute(text("SELECT ok, error, generation_id FROM llm_calls"))
        ).all()
    assert [row.slug for row in rows] == ["curated-1"]
    assert len(ledger) == 1
    assert ledger[0].ok is True and ledger[0].error is None
    # One commit covering both writes, so PRD 10's join never sees a screen
    # with no cost attributed to it.
    assert ledger[0].generation_id == rows[0].generation_id


async def test_curate_says_what_it_dropped_when_nothing_survived(
    cli_settings: Settings,
    clean_slate: None,
    clean_curation: None,
    scripted_llm: FakeLLMClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**ADR-0028's rule 3 at a terminal: the call worked, the money is spent,
    and the generation produced nothing.**

    The state that made ADR-0028 necessary is a run whose ledger row reads
    `ok = true` with real tokens and whose household has no rows -- so what
    this command has to say is not "it failed" but *what was dropped and for
    which reason*, which is the only thing distinguishing a validator eating
    the output from a model with nothing to say. `CurationRejected.error` is
    numbers and label names only, which is why it is safe to put on a screen.

    Handles past the end of the pool rather than a malformed body: it is the
    108/108 shape ADR-0028 measured, it leaves the completion perfectly
    well-formed, and it reaches **two** reasons at once -- five cards
    `not_in_pool` and then the row itself `row_too_short` -- which is the
    second-order effect an operator reads first.

    Both database assertions matter and the second is the one a missing commit
    breaks: last night's screen still stands, *and* the spend is on the record
    anyway.

    **And the message may not say "nothing was written", which is what it said
    until 2026-08-07.** The two assertions below are a contradiction unless the
    sentence is right: this case requires `len(ledger) == 1` -- the call was
    billed -- so a message telling the operator nothing was written is telling
    them they were not charged, on the one path in this milestone where the
    money is gone and the screen is unchanged. That is the exact state
    ADR-0028's rule 3 exists to make visible, inverted at the terminal. The
    clause the arm is *for* is the screen one, which is true on all three paths
    and is asserted above.
    """
    await _seed_candidates(cli_settings, 10)
    invented = range(9001, 9001 + _CARDS)
    scripted_llm.responses.append(_completion(invented))

    with pytest.raises(SystemExit) as exit_info:
        await _curate(cli_settings)

    message = str(exit_info.value)
    assert f"{DropReason.NOT_IN_POOL.value}={_CARDS}" in message, message
    assert f"{DropReason.ROW_TOO_SHORT.value}=1" in message, message
    assert "previous rows still stand" in message, message
    assert "nothing was written" not in message, message
    assert "Traceback" not in message, message
    # Nothing the model wrote reaches the screen on this path: the heading was
    # the model's prose and the tally is numbers and label names.
    assert "Slow-burn sci-fi" not in message, message
    assert capsys.readouterr().out == "", "a rejected generation printed a report"

    async with _session_for(cli_settings) as session:
        kept = (await session.execute(text("SELECT count(*) FROM curated_rows"))).scalar_one()
        ledger = (await session.execute(text("SELECT ok, error FROM llm_calls"))).all()
    assert int(kept) == 0, "a generation that validated to nothing replaced the household's rows"
    assert len(ledger) == 1, "the call was billed and the ledger has to say so"
    assert ledger[0].ok is False
    assert f"{DropReason.NOT_IN_POOL.value}={_CARDS}" in ledger[0].error


async def test_curate_says_a_pool_below_the_card_floor_cannot_fill_one_row(
    cli_settings: Settings,
    clean_slate: None,
    clean_curation: None,
    scripted_llm: FakeLLMClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**M9 Task G4, end to end and with nothing between the guard and the
    screen.** `curation_validate._row` discards a row carrying fewer than
    `DEFAULT_MIN_CARDS` *distinct* cards, so a catalog of four cannot produce
    one surviving row however good the completion is -- and before this guard
    the household paid a completion to find that out, every night.

    **The premise is the second arm, and it is why `calls == []` means
    anything.** A fake with nothing scripted, a household that never reached
    the service, and a catalog of zero all produce an empty `calls` list;
    what distinguishes the guard is that the *same* fixture, one title richer,
    buys exactly one completion. So this case seeds four, asserts the refusal
    bought nothing and was billed nothing, then seeds a fifth and asserts the
    call it declined to make a moment ago now happens.

    The scripted response is left in place across both arms deliberately: on
    the first the client is never asked for it, on the second it is, and a
    fixture that only became answerable for the second arm would be asserting
    an empty deque rather than a guard.
    """
    scripted_llm.responses.append(_completion(range(1, _CARDS + 1)))
    seeded = await _seed_candidates(cli_settings, _CARDS - 1)
    async with _session_for(cli_settings) as session:
        household = await ensure_default_user(session)
        await session.commit()
        pool = await build_pipeline(session, cli_settings).titles.list_unwatched_candidates(
            household, limit=cli_settings.curation_pool_size
        )
    assert len(pool) == len(seeded) == _CARDS - 1, (
        "the premise: the pool this command will read holds four candidates, so it is "
        f"the card floor and not the empty-pool guard that refuses ({len(pool)})"
    )

    with pytest.raises(SystemExit) as exit_info:
        await _curate(cli_settings)

    message = str(exit_info.value)
    assert f"{_CARDS - 1} candidates" in message, message
    assert f"at least {_CARDS}" in message, message
    # The empty pool is the other arm of the same guard and has its own
    # sentence; reading this one as that one would hide the count entirely.
    assert "empty" not in message, message
    assert "previous rows still stand" in message, message
    assert "Traceback" not in message, message
    assert re.search(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", message) is None, message
    assert scripted_llm.calls == [], "a pool that cannot fill one row bought a completion"
    assert capsys.readouterr().out == "", "a refused generation printed a report"
    async with _session_for(cli_settings) as session:
        billed = (await session.execute(text("SELECT count(*) FROM llm_calls"))).scalar_one()
    assert int(billed) == 0, "a pool that cannot fill one row was billed for a completion"

    await _seed_candidates(cli_settings, 1)
    await _curate(cli_settings)

    assert len(scripted_llm.calls) == 1, (
        "the premise: this fixture can buy a completion, so the empty list above "
        "is the guard and not the harness"
    )
    assert f"pool: {_CARDS} candidates" in capsys.readouterr().out


async def test_work_parks_a_curate_job_whose_pool_cannot_fill_one_row(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    clean_slate: None,
    clean_curation: None,
    cli_settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**The disposition, asserted rather than described.** With this file's
    seeding helpers, which is why a `usher work` case lives in the `usher
    curate` section.

    `PortDataMalformed` **parks**, and
    `test_work_parks_a_curate_job_it_cannot_serve_and_buys_nothing` above
    asserts that for a catalog of nothing. This asserts it for a catalog of
    four -- the arm the widened inequality added, and the only one where a
    completion was previously bought.

    **Parking is a permanent block, and this pins it as one.** `_ENQUEUE`
    carries `WHERE jobs.status <> 'parked'`, so every later enqueue for this
    household writes **zero** rows until a human releases the job. That is what
    makes *"until a human releases it"* a fact rather than a warning, and it is
    why the disposition had to follow M9 Task G3's verdict rather than a
    preference: G3 measured that ownership is an `ORDER BY` key and not a
    filter, so the pool is `min(catalog_unwatched, USHER_CURATION_POOL_SIZE)`
    and only a *catalog* below the floor reaches here. That is the empty
    catalog's shape -- an operator's problem, not a transient one that the next
    sync fixes -- and a park is right for it. Had the pool honoured an
    ownership claim, this would fire for an ordinary small library and a park
    would be a permanent block on a condition that grows out of itself.

    Its own settings rather than the shared fixture, for the reason the case
    above gives: `USHER_LLM_ENABLED` is what makes this root build a curate
    handler at all, and no socket is opened because the guard answers in front
    of the client.
    """
    seeded = await _seed_candidates(cli_settings, _CARDS - 1)
    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)
    monkeypatch.setenv("USHER_LLM_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_enabled is True, "the premise: the fixture really turned it on"
    household = new_id()
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        pool = await pipeline.titles.list_unwatched_candidates(
            household, limit=settings.curation_pool_size
        )
        assert len(pool) == len(seeded) == _CARDS - 1, (
            "the premise: the pool this handler will read holds four candidates, so it "
            f"is the card floor and not the empty-pool guard that refuses ({len(pool)})"
        )
        await pipeline.queue.enqueue(
            [JobRequest(kind=JobKind.CURATE, key=str(household), priority=JobPriority.BACKFILL)]
        )
        await session.commit()

    await _work(settings, once=True)

    assert "1 jobs" in capsys.readouterr().out
    async with _session_for(settings) as session:
        status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE kind = 'curate' AND key = :k"),
                {"k": str(household)},
            )
        ).scalar_one()
        # The whole table: `llm_calls` carries no `user_id` -- `generation_id`
        # is its only correlation key -- and the purge fixtures empty it either
        # side.
        billed = (await session.execute(text("SELECT count(*) FROM llm_calls"))).scalar_one()
        written = (await session.execute(text("SELECT count(*) FROM curated_rows"))).scalar_one()
    assert status == "parked", "a pool below the card floor did not park"
    assert int(billed) == 0, "a pool that cannot fill one row was billed for a completion"
    assert int(written) == 0

    async with _session_for(settings) as session:
        again = await build_pipeline(session, settings).queue.enqueue(
            [JobRequest(kind=JobKind.CURATE, key=str(household), priority=JobPriority.BACKFILL)]
        )
        await session.commit()
    assert again == 0, "a parked curate job is not the permanent block this case claims"
    get_settings.cache_clear()
