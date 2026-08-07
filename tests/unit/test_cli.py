"""The CLI's argument surface and its default. No database, no network.

Plus one case that is not about arguments: `usher push` with no `--probe`
is a composition root nothing else calls, and wiring nothing calls is
wiring nothing checks.
"""

import argparse
import asyncio
import contextlib
import dataclasses
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.genome_repository import FakeGenomeRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from tests.fakes.llm_client import FakeLLMClient
from usher.adapters.bulk.movielens import MovieLensGenomeDataset
from usher.cli import (
    PHASES,
    SYNC_KINDS,
    _as_uuid,
    _filters_from,
    _movielens,
    _print_search_answer,
    _report_coverage,
    _run_lanes,
    _search,
    _vocabulary_line,
    build_parser,
    parse_args,
)
from usher.config import Settings
from usher.domain.bootstrap import ImportRunStatus
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.search import SearchResult
from usher.ports.bulk import GENOME_TAG_COUNT, ImdbTitle
from usher.ports.repository import GenomeCoverage
from usher.ports.search import SearchFilters, SearchMode
from usher.services.bootstrap import BootstrapService
from usher.services.search import SearchAnswer


def test_no_arguments_still_means_serve() -> None:
    """The container's CMD is `alembic upgrade head && exec python -m usher`.
    Adding subcommands must not change what that does -- this is the exact
    class of regression that would only show up in a deploy."""
    assert build_parser().parse_args(["serve"]).command == "serve"


def test_bootstrap_defaults_to_all_phases() -> None:
    args = build_parser().parse_args(["bootstrap"])
    assert args.command == "bootstrap"
    assert args.phase == "all"


@pytest.mark.parametrize("phase", PHASES)
def test_every_advertised_phase_parses(phase: str) -> None:
    assert build_parser().parse_args(["bootstrap", "--phase", phase]).phase == phase


def test_an_unknown_phase_is_rejected() -> None:
    """argparse `choices`, not a runtime lookup: a typo must fail before a
    multi-hour import starts, not silently import nothing."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bootstrap", "--phase", "embeddings"])


def test_bootstrap_status_is_its_own_command() -> None:
    assert build_parser().parse_args(["bootstrap-status"]).command == "bootstrap-status"


def test_the_parser_knows_the_ingest_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["sync", "--source", "living-room"]).command == "sync"
    assert parser.parse_args(["sync", "--source", "living-room"]).source == "living-room"
    assert parser.parse_args(["sync-status"]).command == "sync-status"
    assert parser.parse_args(["unmatched"]).command == "unmatched"
    assert parser.parse_args(["work"]).command == "work"


def test_a_sync_with_no_source_means_every_enabled_source() -> None:
    """`--source` is optional on purpose: a nightly cron runs one command for
    a household with two servers. `None` is the "all of them" sentinel, and
    an empty string would be a source name nothing matches."""
    assert build_parser().parse_args(["sync"]).source is None


def test_sync_defaults_to_a_full_walk() -> None:
    """A delta walk has to be asked for. It resumes from a cursor and never
    sweeps (ADR-0015), so defaulting to it would mean an operator who typed
    `usher sync` never retracts anything and never notices."""
    assert build_parser().parse_args(["sync"]).kind == "full"
    assert build_parser().parse_args(["sync", "--kind", "delta"]).kind == "delta"


@pytest.mark.parametrize("kind", SYNC_KINDS)
def test_every_advertised_sync_kind_parses(kind: str) -> None:
    assert build_parser().parse_args(["sync", "--kind", kind]).kind == kind


def test_an_unknown_sync_kind_is_rejected() -> None:
    """`watch_state` is a real `SyncRunKind` and is deliberately not offered:
    it is a lane `sync` always runs after the item walk, not an alternative
    to it. argparse `choices` is what stops `--kind watch_state` reaching
    `ReconcileService`, which would walk `list_items` and label the run
    something the sweep's own lane check then declines to act on."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "--kind", "watch_state"])


def test_sync_requires_a_confirmation_to_disable_the_retraction_guard() -> None:
    """`--allow-full-retraction` is the one flag that can mark a whole
    library unavailable, so it is a flag rather than a config default
    (ADR-0015). Absent, the configured ceiling applies."""
    assert build_parser().parse_args(["sync"]).allow_full_retraction is False
    assert build_parser().parse_args(["sync", "--allow-full-retraction"]).allow_full_retraction


def test_unmatched_pages() -> None:
    args = build_parser().parse_args(["unmatched", "--limit", "5", "--offset", "10"])
    assert (args.limit, args.offset) == (5, 10)


def test_resolving_an_unmatched_item_needs_both_ids() -> None:
    """`--resolve` names a `MediaItem` and `--title` names the `Title` to
    attach it to. One without the other is a half-written resolution, and
    argparse is where that is refused -- `attach_title` would otherwise be
    called with `title_id=None`, which its own docstring says it *will*
    write, blanking a link rather than creating one."""
    args = parse_args(
        ["unmatched", "--resolve", "0198c6b1-0000-7000-8000-000000000001", "--title", "t"]
    )
    assert args.resolve == "0198c6b1-0000-7000-8000-000000000001"
    assert args.title == "t"
    with pytest.raises(SystemExit):
        parse_args(["unmatched", "--resolve", "x"])
    with pytest.raises(SystemExit):
        parse_args(["unmatched", "--title", "x"])


def test_similar_is_a_read_form_and_a_write_form_of_one_subcommand() -> None:
    """One subcommand for one artefact, exactly as `usher index` has
    `--backfill`. Two subcommands is how those two would have drifted, and
    argparse has no vocabulary for "exactly one of these".

    Both refusals matter and neither is symmetric with the other. **No
    arguments** is a read of nothing -- and it is the spelling an operator
    reaches for when they mean `--rebuild`, so falling through to a rebuild
    would recompute a 250,000-row table by accident. **Both together** is a
    read and a write in one command, where the read would answer from rows the
    write had just replaced.
    """
    read = parse_args(["similar", "0198c6b1-0000-7000-8000-000000000001"])
    assert read.title_id == "0198c6b1-0000-7000-8000-000000000001"
    assert read.rebuild is False
    assert read.limit == 10

    write = parse_args(["similar", "--rebuild"])
    assert write.title_id is None
    assert write.rebuild is True

    with pytest.raises(SystemExit):
        parse_args(["similar"])
    with pytest.raises(SystemExit):
        parse_args(["similar", "0198c6b1-0000-7000-8000-000000000001", "--rebuild"])


def test_work_runs_forever_unless_asked_for_one_pass() -> None:
    """`--once` is what `docker compose exec usher python -m usher work
    --once` needs to exit; without it the command is a daemon."""
    assert build_parser().parse_args(["work"]).once is False
    assert build_parser().parse_args(["work", "--once"]).once is True


def test_push_probes_or_runs_the_lanes() -> None:
    """`usher push --probe` is ADR-0004's caveat as an operator command: it
    reports what *arrived*, never that the handshake succeeded. Bare `usher
    push` runs the same lanes the server does, without the HTTP surface."""
    args = build_parser().parse_args(["push"])
    assert args.source is None
    assert args.probe is False
    args = build_parser().parse_args(["push", "--probe", "--source", "Living Room Emby"])
    assert args.probe is True
    assert args.source == "Living Room Emby"


def test_push_is_not_the_default_command() -> None:
    """`main` treats no arguments as `serve`, because that is exactly what
    the container's CMD runs. A new subcommand must not change it -- the
    failure M4 found was `main` treating `argv is None` as "no arguments at
    all", which made `usher sync-status` silently start the server."""
    assert build_parser().parse_args([]).command is None
    assert build_parser().parse_args(["push"]).command == "push"


async def test_running_the_lanes_in_the_foreground_stops_them_on_the_way_out() -> None:
    """`usher push` with no `--probe` is a daemon, so the two things a test
    can assert about it are that it *stays up* and that it *lets go*:
    Ctrl-C reaches `asyncio.run`, which cancels the task, and the `finally`
    has to stop the lanes, close the TMDb client and dispose the engine on
    the way out.

    Both lanes **on**, against a database that is not there. `start()`
    creates tasks and opens no connection, so this costs nothing and it is
    the only arrangement in which "stopped them" is observable: with the
    lanes off there is nothing to leave running, and the cleanup mutation
    survives. Asserted on the loop's own task set rather than on the
    supervisor, which `_run_lanes` deliberately does not expose.

    Without this case `_run_lanes` is a composition root nothing calls,
    which is what `tests/integration/test_pipeline_deps.py` exists to stop
    happening to the other one.
    """
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        push_enabled=True,
        worker_enabled=True,
    )
    task = asyncio.create_task(_run_lanes(settings))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done(), "usher push returned instead of staying up"
    assert _lane_tasks(), "usher push started no lanes at all"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _lane_tasks() == [], "usher push left its lanes running"


def _lane_tasks() -> list[str]:
    return sorted(
        name
        for task in asyncio.all_tasks()
        if (name := task.get_name()).startswith("usher.lane.") and not task.done()
    )


def test_index_parses_its_two_modes() -> None:
    """`--backfill` writes and the bare form only reads, which is what makes
    `usher index` safe to run on a production box while diagnosing
    something."""
    assert parse_args(["index"]).backfill is False
    assert parse_args(["index", "--backfill", "--limit", "500"]).limit == 500
    assert parse_args(["index", "--backfill"]).limit == 0


def _search_actions() -> list[argparse.Action]:
    """Every action the `search` subparser declares, read off the parser.

    Reached through `_subparsers` rather than by rebuilding the flags here,
    because the property the case below asserts is about *this* parser and a
    second list would only ever agree with itself.
    """
    subparsers = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return list(subparsers.choices["search"]._actions)


def test_search_takes_the_modes_the_port_declares() -> None:
    """The wrong implementation: `choices=["full_text", "semantic"]`, which is
    what you get by writing the flag before `FUSED` existed and never
    revisiting it. `usher search --mode fused` then exits 2 with "invalid
    choice" for the mode that is the milestone's whole design (ADR-0002), and
    nothing else in the suite notices.

    Taken from the enum rather than retyped, so a fourth mode is offered the
    day it exists.
    """
    for mode in SearchMode:
        assert parse_args(["search", "q", "--mode", mode.value]).mode == mode.value
    assert parse_args(["search", "q"]).mode == SearchMode.FUSED.value
    with pytest.raises(SystemExit):
        parse_args(["search", "q", "--mode", "vibes"])


def test_search_refuses_an_empty_year_range() -> None:
    """A cross-argument rule argparse cannot express: each bound is
    individually valid, so `--year-from 2020 --year-to 1990` parses cleanly and
    returns nothing -- which reads as "the catalog does not have it" rather
    than as a transposed pair.

    `parser.error`, so it exits 2 with usage on stderr like every other
    argument failure; a `raise` would exit 1 with a traceback.
    """
    assert parse_args(["search", "q", "--year-from", "1990", "--year-to", "2020"]).year_to == 2020
    # A single-year window is a legitimate ask, so the rule is `>` and not `>=`.
    assert parse_args(["search", "q", "--year-from", "1999", "--year-to", "1999"]).year_to == 1999
    with pytest.raises(SystemExit):
        parse_args(["search", "q", "--year-from", "2020", "--year-to", "1990"])


def test_search_refuses_a_limit_of_zero() -> None:
    """Checked here rather than left to `SearchService`'s ceiling, because the
    two failures differ: above `search_result_limit` the service clamps and the
    answer says so, and at zero the operator asked for nothing and meant
    something."""
    assert parse_args(["search", "q", "--limit", "1"]).limit == 1
    with pytest.raises(SystemExit):
        parse_args(["search", "q", "--limit", "0"])


def test_the_filter_flags_are_search_filters_whole_vocabulary() -> None:
    """One flag per `SearchFilters` field, checked against the dataclass rather
    than against a list.

    The wrong implementation is a CLI offering the three filters somebody
    needed on the day. A filter with no flag is a capability the port declares,
    the backend implements, and no operator can reach -- and because
    `SearchFilters` is frozen and slotted, adding a field later without a flag
    is silent. The vocabulary being closed is 🔶 1's settlement: a
    `dict[str, Any]` let two backends invent different keys, and a backend that
    cannot express a filter must raise rather than ignore it, because an
    ignored filter returns *more* results and reads as working.
    """
    declared = {field.name for field in dataclasses.fields(SearchFilters)}
    reachable = {action.dest for action in _search_actions()}
    assert declared <= reachable, f"no flag reaches {sorted(declared - reachable)}"


def test_the_filter_flags_build_the_filters_they_advertise() -> None:
    """The other half, because a parser action named `genres` proves only that
    the *name* exists. `_filters_from` is the one place the flag-to-field
    mapping lives, so a flag wired to the wrong field is visible here and
    nowhere else."""
    args = parse_args(
        [
            "search",
            "vacuum",
            "--kind",
            "movie",
            "--year-from",
            "1990",
            "--year-to",
            "2030",
            "--genre",
            "drama",
            "--genre",
            "sci-fi",
            "--owned-only",
            "--min-enrichment",
            "enriched",
        ]
    )
    assert _filters_from(args) == SearchFilters(
        kinds=(TitleKind.MOVIE,),
        year_from=1990,
        year_to=2030,
        genres=("drama", "sci-fi"),
        owned_only=True,
        min_enrichment=EnrichmentState.ENRICHED,
    )


def test_the_bare_search_carries_no_filters_at_all() -> None:
    """`SearchFilters()` and not a half-populated one: `owned_only=False` and
    empty tuples are the port's own "narrow nothing", and a CLI that sent
    `genres=()` as `genres=("",)` would narrow every search to nothing while
    looking like it passed no filter."""
    assert _filters_from(parse_args(["search", "vacuum"])) == SearchFilters()


def test_suggest_takes_a_prefix_and_a_limit() -> None:
    assert parse_args(["suggest", "quie"]).prefix == "quie"
    assert parse_args(["suggest", "quie", "--limit", "5"]).limit == 5
    assert parse_args(["suggest", "quie"]).limit == 10


def test_suggest_refuses_a_limit_of_zero() -> None:
    """`usher search`'s rule, for the same reason -- a type-ahead box asking
    for nothing is an operator who meant something."""
    with pytest.raises(SystemExit):
        parse_args(["suggest", "quie", "--limit", "0"])


def test_similar_rejects_a_title_id_that_is_not_a_uuid() -> None:
    """`_as_uuid`, the treatment `--resolve`/`--title` already get: a sentence
    naming the argument rather than a `ValueError` traceback out of
    `uuid.UUID`. Parsing succeeds -- argparse has no uuid type -- so the
    refusal has to happen where `main` converts it."""
    assert parse_args(["similar", "not-a-uuid"]).title_id == "not-a-uuid"
    with pytest.raises(SystemExit, match="title id is not a uuid"):
        _as_uuid("not-a-uuid", "title id")


def test_movielens_is_the_last_phase_before_all_and_the_order_is_execution_order() -> None:
    """`--phase all` runs the tuple in order, so this tuple *is* the
    execution order an operator reads it as.

    `movielens` must come after `imdb`: the genome joins to `titles` on
    `imdb_id`, and against an empty catalog the join matches nothing. Kills a
    tidy-up that alphabetises `PHASES`, which would put `crosswalk` and
    `movielens` before `imdb` and produce a phase that downloads 335 MiB,
    writes zero rows, and reports success.
    """
    assert PHASES == ("imdb", "tmdb-ids", "crosswalk", "movielens", "all")


async def test_the_genome_phase_refuses_an_empty_catalog_before_downloading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The worst outcome available in this milestone, refused.

    Against an empty catalog the phase would otherwise download 350,896,731 B,
    stream 18,472,128 rows, write 0, checkpoint COMPLETED, and show green in
    `bootstrap-status` -- and every later `--phase all` would find a completed
    checkpoint at the file's end and do nothing, so the failure would be
    permanent and invisible.

    Three assertions, one per property. **No request of any kind** -- the
    transport fails the test if reached, so this also pins "before the
    download" rather than merely "before the write". **No `ImportRun`** -- a
    FAILED row would be a lie and a COMPLETED one would be worse, and the
    absence of a row is what `bootstrap-status` already renders as "this
    phase has not run". **A message that names the reason and the fix**,
    because PRD 08's rule is that every operator command works against an
    empty database, and "work" means saying why.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the genome phase reached the network: {request.url}")

    catalog = FakeBulkCatalogRepository()
    runs = FakeImportRunRepository()
    service = BootstrapService(runs, catalog, _no_commit)
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        secret_key="0" * 32,
        bulk_data_dir=tmp_path,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        await _movielens(settings, client, catalog, service, _no_commit)

    assert await catalog.count_titles() == 0
    assert await runs.list_runs() == []
    printed = capsys.readouterr().out
    assert "titles is empty" in printed
    assert "--phase imdb" in printed


async def _no_commit() -> None:
    return None


# One movie, a **full-width** vocabulary, and a links row joining it to the
# catalog title seeded below. Every value invented; see `tests/unit/
# test_adapters_bulk_movielens.py` for why the fixture is Python literals
# rather than a committed archive.
#
# Full width rather than the three tags the adapter's own tests use, because
# `_movielens` constructs `MovieLensGenomeDataset` with the production
# `expected_tags` and a narrower vocabulary is refused before a score is read
# -- which is the check that exists precisely so a release whose vocabulary
# moved cannot be stored under `halfvec(1128)`. The first three names are
# spelled out so the lane-order assertions below read as assertions rather
# than as arithmetic.
_TAG_NAMES = (
    "zeppelins",
    "atmospheric",
    "melancholy",
    *(f"invented tag {n}" for n in range(4, GENOME_TAG_COUNT + 1)),
)
_ARCHIVE_MEMBERS = {
    "links.csv": "movieId,imdbId,tmdbId\n90000101,99000101,90000201",
    "genome-tags.csv": "\n".join(
        ["tagId,tag"] + [f"{n},{name}" for n, name in enumerate(_TAG_NAMES, start=1)]
    ),
    "genome-scores.csv": "\n".join(
        ["movieId,tagId,relevance"]
        + [f"90000101,{n},{n / 10000}" for n in range(1, GENOME_TAG_COUNT + 1)]
    ),
}
_SEEDED_TITLE = ImdbTitle(
    imdb_id="tt" + "99000101",
    kind=TitleKind.MOVIE,
    name="An Invented Feature",
    original_name=None,
    year=1994,
    end_year=None,
    runtime_minutes=100,
    genres=("Drama",),
)


def _genome_archive(tmp_path: Path, **overrides: str) -> Path:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(cache / "ml-latest.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in (_ARCHIVE_MEMBERS | overrides).items():
            archive.writestr(f"ml-latest/{name}", body)
    return cache


def _local_archive(cache: Path) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


def _genome_settings(cache: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        secret_key="0" * 32,
        bulk_data_dir=cache,
    )


async def test_the_genome_phase_stores_the_tag_vocabulary_beside_the_vectors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loader half of Task 19, driven through the phase rather than
    through the repository, because "loaded by the existing MovieLens phase"
    is the requirement and a repository case cannot see it.

    The vocabulary carries **the same revision the vectors carry**, which is
    what makes the two comparable at all. Against this fixture the two agree
    by luck as well as by construction -- one ETag, never moving -- so the
    case below is the one with teeth about *which* token was used, and this
    one is its control.
    """
    cache = _genome_archive(tmp_path)
    catalog = FakeBulkCatalogRepository()
    await catalog.upsert_titles([_SEEDED_TITLE])
    service = BootstrapService(FakeImportRunRepository(), catalog, _no_commit)

    async with httpx.AsyncClient(transport=_local_archive(cache)) as client:
        await _movielens(_genome_settings(cache), client, catalog, service, _no_commit)

    stored = catalog.genome_tags()
    assert len(stored) == GENOME_TAG_COUNT
    assert stored[:3] == (
        (1, "zeppelins", '"fixture"'),
        (2, "atmospheric", '"fixture"'),
        (3, "melancholy", '"fixture"'),
    )
    assert stored[-1] == (GENOME_TAG_COUNT, _TAG_NAMES[-1], '"fixture"')
    vector_revisions = {revision for revision, _ in (await catalog.genome_coverage()).revisions}
    assert vector_revisions == {'"fixture"'}
    assert f"{GENOME_TAG_COUNT} tags" in capsys.readouterr().out


async def test_the_vocabulary_is_stamped_with_the_token_the_vectors_were_stamped_with(
    tmp_path: Path,
) -> None:
    """The reason `tag_vocabulary` takes a `revision` instead of resolving
    one, arriving at the layer where the damage would be permanent.

    An upstream that re-uploads between two `HEAD`s hands back two tokens for
    one run. `BootstrapService.import_dataset` already takes the caller's own
    resolved value for exactly that reason (its docstring calls out this
    phase by name), and the vocabulary has to travel the same way -- otherwise
    `genome_tags.genome_revision` says B while every vector beside it says A,
    and `GenomeRepository.vocabulary` then refuses forever on a catalog where
    nothing is actually wrong.

    The fixture is the only one in this file whose ETag **moves**: every other
    transport here answers one token, which makes "resolved once" and
    "resolved twice" indistinguishable. Kills
    `replace_genome_tags(..., revision=await dataset.revision())`.
    """
    cache = _genome_archive(tmp_path)
    tokens = iter(['"release-a"', '"release-b"', '"release-c"'])
    served: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        token = next(tokens)
        served.append(token)
        (cache / f"{name}.revision").write_text(token)
        return httpx.Response(200, content=(cache / name).read_bytes(), headers={"etag": token})

    catalog = FakeBulkCatalogRepository()
    await catalog.upsert_titles([_SEEDED_TITLE])
    service = BootstrapService(FakeImportRunRepository(), catalog, _no_commit)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _movielens(_genome_settings(cache), client, catalog, service, _no_commit)
        # The premise, asserted against the transport rather than against the
        # run: one more resolution really does hand back a different token, so
        # an implementation that resolved a second time would have stamped
        # something else. Without this the fixture is indistinguishable from
        # every other one in this file.
        again = await MovieLensGenomeDataset(client, cache).revision()

    assert again != served[0]
    vector_revisions = {revision for revision, _ in (await catalog.genome_coverage()).revisions}
    tag_revisions = {revision for _, _, revision in catalog.genome_tags()}
    assert tag_revisions == vector_revisions == {served[0]}


async def test_a_completed_checkpoint_that_writes_no_vector_still_loads_the_vocabulary(
    tmp_path: Path,
) -> None:
    """**The upgrade path, and the one case that decides where this call
    goes.** A catalog bootstrapped under M7 has a *completed*
    `movielens.genome` checkpoint and no vocabulary at all, because `ffa`
    deliberately did not store one. Re-running the phase resumes from that
    cursor, yields no batch and writes no vector -- and the vocabulary has to
    land anyway. Deleting the write kills this (measured), and so does gating
    it on a **per-run** count of rows written, which is the defect an
    implementer would actually introduce.

    **What this case does not kill is `if run.rows_written:`, and an earlier
    version of this docstring claimed it did.** `ImportRun.rows_written` is
    *cumulative across resumes* -- `PostgresImportRunRepository.start()` keeps
    it when the revision has not moved -- so the second run below inherits the
    first's count, that gate reads truthy, and the vocabulary is written for
    the wrong reason. Measured 2026-08-07: the `rows_written` spelling passes
    all 2,883 unit and all 899 integration cases, this one included. It is
    still not the predicate to ship (`_movielens`' own docstring has the
    argument), but the reason is that the two answers differ only for a
    completed run that never wrote a vector at all -- not anything this case
    can see.

    Modelled by running the phase twice against one catalog: the second run
    resumes from the first's completed cursor, which is the state a re-run
    against an unchanged archive really produces (`_movielens`' own docstring
    records it as measured -- 16,376 runs skipped, nothing written).
    """
    cache = _genome_archive(tmp_path)
    catalog = FakeBulkCatalogRepository()
    await catalog.upsert_titles([_SEEDED_TITLE])
    runs = FakeImportRunRepository()
    service = BootstrapService(runs, catalog, _no_commit)
    settings = _genome_settings(cache)

    async with httpx.AsyncClient(transport=_local_archive(cache)) as client:
        await _movielens(settings, client, catalog, service, _no_commit)
        assert len(catalog.genome_tags()) == GENOME_TAG_COUNT
        # An M7-era catalog has no vocabulary. Both stores are emptied rather
        # than just the vocabulary, so the assertions below can distinguish
        # "the second run wrote nothing" from "the second run rewrote
        # everything" -- which the checkpoint alone cannot, since a run that
        # replayed every batch reaches the same position.
        catalog._genome_tags.clear()
        catalog._genome.clear()
        await _movielens(settings, client, catalog, service, _no_commit)

    checkpoint = await runs.get("movielens.genome")
    assert checkpoint is not None
    assert checkpoint.position == 1
    # The premise: the second run resumed from a completed cursor and yielded
    # no batch, so nothing it wrote can have come from the vector path.
    assert catalog.genome_keys() == set()
    assert len(catalog.genome_tags()) == GENOME_TAG_COUNT


async def test_an_import_that_failed_writes_no_vocabulary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vocabulary explains the vectors, and a failed drain has not finished
    writing them. The run that eventually completes writes it.

    Kills an implementation that loads the vocabulary unconditionally after
    `import_dataset` -- which does not raise, so "after" and "after a success"
    look identical at the call site. The malformed member is `genome-scores`
    rather than `genome-tags`, deliberately: the vocabulary itself parses
    fine, so nothing but the run's status distinguishes the two.
    """
    cache = _genome_archive(
        tmp_path, **{"genome-scores.csv": "movieId,tagId,relevance\n90000101,1,0.5"}
    )
    catalog = FakeBulkCatalogRepository()
    await catalog.upsert_titles([_SEEDED_TITLE])
    runs = FakeImportRunRepository()
    service = BootstrapService(runs, catalog, _no_commit)

    async with httpx.AsyncClient(transport=_local_archive(cache)) as client:
        await _movielens(_genome_settings(cache), client, catalog, service, _no_commit)

    stored_run = await runs.get("movielens.genome")
    assert stored_run is not None and stored_run.status is ImportRunStatus.FAILED
    assert catalog.genome_tags() == ()
    assert "0 tags" in capsys.readouterr().out


def _coverage(*revisions: tuple[str, int]) -> GenomeCoverage:
    return GenomeCoverage(
        with_vector=sum(count for _, count in revisions),
        titles=100,
        movies=100,
        enriched=0,
        enriched_with_vector=0,
        revisions=revisions,
    )


async def test_the_status_report_says_when_the_vocabulary_names_the_stored_vectors() -> None:
    """The ordinary answer, and the control the three refusal branches below
    need: without it, `return "genome vocabulary: not loaded"` unconditionally
    passes every one of them."""
    genome = FakeGenomeRepository(tags={1: ("zeppelins", "etag-a"), 2: ("atmospheric", "etag-a")})

    assert await _vocabulary_line(genome, _coverage(("etag-a", 5))) == "genome vocabulary: 2 tags"


async def test_the_status_report_says_a_vocabulary_that_was_never_loaded_is_missing() -> None:
    """Every catalog bootstrapped before `m08b` is in this state, so it has to
    read as a thing to do rather than as a fault -- and the line names the
    command that fixes it, which is PRD 08's rule for an operator command."""
    line = await _vocabulary_line(FakeGenomeRepository(), _coverage(("etag-a", 5)))

    assert "not loaded" in line
    assert "--phase movielens" in line


async def test_the_status_report_renders_a_mismatched_vocabulary_rather_than_raising() -> None:
    """`PortDataMalformed` is a `UsherPortError` and `UsherPortError` is not
    in `OPERATOR_ERRORS`, so letting it out of a status command answers "what
    state is my genome in?" with a stack trace about the answer being bad.

    Both release tokens have to survive into the line: "the vocabulary is
    wrong" without naming what is stored is not something an operator can act
    on. Kills a handler that prints a fixed sentence.
    """
    genome = FakeGenomeRepository(tags={1: ("zeppelins", "etag-b")})

    line = await _vocabulary_line(genome, _coverage(("etag-a", 5)))

    assert "etag-a" in line
    assert "etag-b" in line


async def test_the_status_report_declines_to_judge_a_vocabulary_against_mixed_vectors() -> None:
    """With `genome_scores` holding two releases there is no single revision
    to ask for, and asking for either would report the *vocabulary* as wrong
    when what is wrong is the vectors -- which `_report_coverage`'s MIXED
    RELEASES line already says, in the phase that produced them.

    Kills `coverage.revisions[0][0]`, which is a perfectly good release token
    and the wrong question.
    """
    genome = FakeGenomeRepository(tags={1: ("zeppelins", "etag-a")})

    line = await _vocabulary_line(genome, _coverage(("etag-a", 5), ("etag-b", 2)))

    assert "more than one release" in line


async def test_the_status_report_says_nothing_is_named_when_there_are_no_vectors() -> None:
    """A fresh database has no genome at all, and PRD 08 requires every
    operator command to work against one. Kills an implementation that reports
    a missing vocabulary as a problem on a catalog that has nothing for it to
    explain."""
    assert await _vocabulary_line(FakeGenomeRepository(), _coverage()) == (
        "genome vocabulary: no vectors to name"
    )


def test_the_coverage_report_survives_an_enriched_tier_of_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bootstrap-only catalog is all skeletons, which is exactly the state
    PRD 08 says every operator command must survive. The enriched-tier
    fraction is the one that matters and it is the one whose denominator is
    zero on a fresh database. Kills a report that divides."""
    _report_coverage(
        GenomeCoverage(
            with_vector=16376,
            titles=1271138,
            movies=899828,
            enriched=0,
            enriched_with_vector=0,
            revisions=(("an-invented-etag", 16376),),
        ),
        unmatched=0,
        tags=GENOME_TAG_COUNT,
    )
    printed = capsys.readouterr().out
    assert "1.29% of 1271138 titles" in printed
    assert "1.82% of 899828 movies" in printed
    assert "n/a (0 titles) of the enriched tier" in printed
    # One release is the normal case; a line reading "revisions: 1" is noise.
    assert "MIXED RELEASES" not in printed


def test_the_coverage_report_names_every_release_when_there_is_more_than_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two releases in one table is a correctness problem
    `GenomeRepository.get_pair` is already refusing to blend across, and a
    killed re-import against a new upload is how it happens. Kills a report
    that prints the breakdown unconditionally (noise on every normal run,
    which trains an operator to skip the line) and one that never prints it
    (the condition becomes invisible)."""
    _report_coverage(
        GenomeCoverage(
            with_vector=3,
            titles=10,
            movies=8,
            enriched=2,
            enriched_with_vector=1,
            revisions=(("an-invented-etag-a", 1), ("an-invented-etag-b", 2)),
        ),
        unmatched=4,
        tags=GENOME_TAG_COUNT,
    )
    printed = capsys.readouterr().out
    assert "3 vectors stored (4 unmatched)" in printed
    assert "MIXED RELEASES" in printed
    assert "an-invented-etag-a: 1" in printed
    assert "an-invented-etag-b: 2" in printed


def test_the_parser_knows_the_home_command() -> None:
    args = parse_args(["home"])
    assert args.command == "home"
    assert args.limit == 10
    assert args.repeat == 1


def test_home_refuses_a_limit_below_one() -> None:
    """Beside the identical checks `search` and `suggest` already carry. A zero
    limit composes a screen and prints nothing, which reads as a broken
    catalog rather than as an argument the operator got wrong."""
    with pytest.raises(SystemExit):
        parse_args(["home", "--limit", "0"])


def test_home_refuses_a_repeat_below_one() -> None:
    """A zero repeat times nothing and then reports a p95 over an empty list,
    which is either a crash or a fabricated number depending on how the
    percentile is spelled."""
    with pytest.raises(SystemExit):
        parse_args(["home", "--repeat", "0"])


def test_home_has_no_cross_argument_rule_and_that_is_deliberate() -> None:
    """`usher similar` needs one (`bool(title_id) == bool(rebuild)`) because
    its two arguments select between two *different operations*, one of which
    rewrites the whole neighbour table. `usher home` has one operation and two
    scalars, so every combination of them is meaningful.

    Written down as a case rather than as an absence, because a reader
    comparing this command to its template will look for the rule and should
    find the reason it is not there.
    """
    both = parse_args(["home", "--limit", "3", "--repeat", "5"])
    assert (both.limit, both.repeat) == (3, 5)


# --- `usher search`, query expansion --------------------------------------
#
# `_print_search_answer` is a pure function over a `SearchAnswer` -- the split
# `_print_curation_report` already makes -- so the report an operator reads can
# be driven without a database. `_search`'s two cases below drive the
# composition root itself, because "the client was built" and "the client
# reached the pipeline" are different claims and only the second is the one
# that decides whether a search on the *only* user-facing surface ever expands.


def _answer(**changes: object) -> SearchAnswer:
    return dataclasses.replace(
        SearchAnswer(
            results=(
                SearchResult(
                    title_id=uuid.UUID(int=0x11),
                    kind=TitleKind.MOVIE,
                    name="The Quiet Vacuum",
                    year=2019,
                    popularity=None,
                    owned=False,
                    score=0.5,
                ),
            ),
            requested_mode=SearchMode.FUSED,
            mode=SearchMode.FUSED,
            semantic_coverage=1.0,
        ),
        **changes,  # type: ignore[arg-type]
    )


def test_an_expanded_query_is_printed_and_printed_before_the_results(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**"Reported, never silently substituted", at the one surface a person
    reads.** A span attribute is not a report and a `llm_calls` row is not
    either: both are for an operator with a query console, and the person who
    typed the search has neither. Without this line a viewer searches for one
    thing, gets results for another, and has nothing to tell a good expansion
    from a bad one.

    Before the results, because it is the question they are the answer to. The
    ordering assertion is what fails if the line drifts below the summary,
    where it reads as a footnote about a search already scrolled past.
    """
    _print_search_answer(_answer(expanded_query="a crew alone in orbit"))

    out = capsys.readouterr().out
    assert "a crew alone in orbit" in out, out
    assert out.index("a crew alone in orbit") < out.index("The Quiet Vacuum"), out


def test_a_search_that_bought_no_completion_prints_no_expansion_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shipped default is `USHER_LLM_ENABLED=false`, so `expanded_query` is
    `None` on every search of every default deployment -- and a report that
    echoed the typed query there would print a line about a rewrite nobody
    bought, on every run, forever. The rest of the report is asserted too, so
    "print nothing at all" is not a pass.
    """
    _print_search_answer(_answer())

    out = capsys.readouterr().out
    assert "expanded" not in out, out
    assert "The Quiet Vacuum" in out, out
    assert "mode=fused" in out, out


async def test_a_semantic_search_hands_the_completion_client_to_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The wiring that decides whether the only user-facing search surface
    ever expands anything.** `build_pipeline` builds the expander, and it can
    only do so from a client -- so a `_search` that opened one and forgot to
    pass it would leak a connection pool per run and expand nothing, with no
    error and a report that correctly says no expansion happened.

    The client is released on the way out, in the same `finally` as the
    embedder: one `httpx.AsyncClient` per command, closed however the command
    ends.
    """
    built = FakeLLMClient()
    closed = 0
    captured: dict[str, object] = {}

    async def _closer() -> None:
        nonlocal closed
        closed += 1

    async def _client(_: Settings, *, report: bool = True) -> object:
        assert report is False, "the lane sentence belongs to `usher work`"
        return built, _closer

    monkeypatch.setattr("usher.cli.llm_client", _client)
    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.build_pipeline", _recording_pipeline(captured))

    await _search(
        _cli_settings(), query="isolation in space", mode="fused", limit=5, filters=SearchFilters()
    )

    assert captured["llm"] is built
    assert closed == 1


async def test_a_full_text_search_opens_no_completion_client_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call sits in front of the embed, so a mode with no embed has nothing
    to expand for -- and a client built anyway is an `httpx.AsyncClient` and a
    connection pool opened once per run of the mode a deployment with no
    embedding extra uses for *everything*.

    Not a correctness claim about spend: `SearchService` would not call an
    expander on this path either (`test_a_full_text_search_buys_no_completion`
    is where that lives). This is the resource half, and it is the half only
    the composition root can get wrong.
    """
    captured: dict[str, object] = {}

    async def _never(*_: object, **__: object) -> object:
        raise AssertionError("a full-text search opened a completion client")

    monkeypatch.setattr("usher.cli.llm_client", _never)
    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.build_pipeline", _recording_pipeline(captured))

    await _search(
        _cli_settings(),
        query="the quiet vacuum",
        mode="full_text",
        limit=5,
        filters=SearchFilters(),
    )

    assert captured["llm"] is None


def _cli_settings() -> Settings:
    return Settings(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher", secret_key="0" * 32)


@contextlib.asynccontextmanager
async def _no_session(_: Settings) -> AsyncIterator[None]:
    """`_session_for`, without the engine. The claim under test is about what
    `_search` hands to `build_pipeline`, and opening a real connection would
    make it a claim about Postgres."""
    yield None


def _recording_pipeline(captured: dict[str, object]) -> Callable[..., object]:
    """A `build_pipeline` that records its keyword arguments and answers a
    `SearchAnswer` with nothing in it."""

    class _Search:
        async def search(self, query: str, **_: object) -> SearchAnswer:
            return SearchAnswer()

    class _Pipeline:
        search = _Search()

    def _build(_session: object, _settings: Settings, **kwargs: object) -> object:
        captured.update(kwargs)
        return _Pipeline()

    return _build
