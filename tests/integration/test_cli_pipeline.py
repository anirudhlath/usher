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

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
import pytest_asyncio
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.embedding import FakeEmbedder
from usher.cli import (
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

# The blend these arranged rows claim to have been computed under. A literal,
# never `blend_fingerprint()`: a case that inherits today's fingerprint cannot
# express "this row came from a different blend", which is the whole state the
# column exists to describe.
_FP = "arranged-by-a-test"


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
    """`cli.py:153-154`'s rule, asserted rather than followed.

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
    assert "9 providers, 9 proposed nothing" in out
    assert "screen: 0 rows, 0 cards" in out


async def test_home_prints_a_line_for_a_provider_that_proposed_nothing(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """**An absent provider and a silent one are the two states this milestone
    exists to distinguish**, and a report that drops the silent ones makes them
    indistinguishable from unregistered ones -- which is exactly how a provider
    left out of `ROW_PROVIDERS` survives review.

    Kills a report built by iterating the *proposals* rather than the registry.
    Asserted by name for every one of the nine, because a count is satisfied by
    a report printing one provider nine times.
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
