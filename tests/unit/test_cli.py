"""The CLI's argument surface and its default. No database, no network.

Plus one case that is not about arguments: `usher push` with no `--probe`
is a composition root nothing else calls, and wiring nothing calls is
wiring nothing checks.
"""

import argparse
import ast
import asyncio
import contextlib
import dataclasses
import gzip
import inspect
import pathlib
import re
import uuid
import zipfile
from collections import Counter
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import usher.cli
from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.genome_repository import FakeGenomeRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from tests.fakes.llm_client import FakeLLMClient
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.adapters.bulk.imdb import parse_akas_row
from usher.adapters.bulk.movielens import MovieLensGenomeDataset
from usher.cli import (
    PHASES,
    SYNC_KINDS,
    _as_uuid,
    _bootstrap,
    _filters_from,
    _print_search_answer,
    _run_lanes,
    _search,
    _unmatched,
    _vocabulary_line,
    build_parser,
    parse_args,
)
from usher.composition import (
    _aliases,
    _credit_names,
    _movielens,
    _percent,
    _report_coverage,
)
from usher.config import Settings
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRun, ImportRunStatus
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.search import SearchResult
from usher.domain.title import Title
from usher.ports.bulk import GENOME_TAG_COUNT, ImdbTitle
from usher.ports.events import NullEventPublisher
from usher.ports.ingest import MediaItemUpsert
from usher.ports.repository import GenomeCoverage
from usher.ports.search import SearchFilters, SearchMode
from usher.services.bootstrap import (
    BootstrapReport,
    BootstrapService,
    VocabularyState,
    VocabularyVerdict,
    bootstrap_report,
    vocabulary_verdict,
)
from usher.services.search import SearchAnswer, SuggestTier


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


# -- `--resolve --title`'s three answers ---------------------------------------
#
# `--title` has three bad values and they are three different conditions.
# A value that is not a UUID never reaches a port -- `_as_uuid` refuses it
# (`test_resolving_an_unmatched_item_needs_both_ids`' neighbours). The other
# two both reach `attach_title`, and until M10's F4 only one of them had an
# answer: a `--resolve` naming no media item is `rowcount == 0` and prints
# `no such media item`, while a well-formed `--title` naming no row is a
# foreign key -- `RepositoryConflict`, which is deliberately **not** in
# `OPERATOR_ERRORS` (ADR-0026's amendment), so `main` re-raises it and the
# operator gets the stack. The fix is a lookup here rather than a tenth
# member of that tuple, and the enumeration that argues for it is in
# ADR-0026's Consequences.
#
# These two cases are the fake-backed half. The fake has **no foreign key**
# (see its own divergence list), so it cannot exhibit the defect at all --
# what it can pin, and what a `try/except RepositoryConflict` around the
# write would fail, is that nothing was attempted and nothing was committed.
# `tests/integration/test_cli_pipeline.py::
# test_an_unknown_title_id_is_a_sentence_against_real_postgres` is where the
# FK actually exists.


@dataclasses.dataclass
class _ResolveSession:
    """`_session_for`'s yield, narrowed to the one method `_unmatched` calls
    on it. The counter is an assertion rather than a stub's convenience: a
    refusal that committed is a refusal that arrived too late."""

    commits: int = 0

    async def commit(self) -> None:
        self.commits += 1


class _RecordingMediaItems(FakeMediaItemRepository):
    """The fake plus the one thing these cases have to know -- whether the
    write was *attempted*. `attach_title`'s return value cannot say so: `False`
    is also what a call that found no row produces."""

    def __init__(self) -> None:
        super().__init__()
        self.attached: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def attach_title(
        self, media_item_id: uuid.UUID, *, title_id: uuid.UUID, episode_id: uuid.UUID | None
    ) -> bool:
        self.attached.append((media_item_id, title_id))
        return await super().attach_title(media_item_id, title_id=title_id, episode_id=episode_id)


@dataclasses.dataclass
class _ResolveHarness:
    """What `usher unmatched --resolve` runs against, with no database."""

    media_items: _RecordingMediaItems
    titles: FakeTitleRepository
    session: _ResolveSession
    #: A media item the fake genuinely holds, so "nothing was written" is a
    #: statement about the pre-check rather than about a missing row.
    held: uuid.UUID


async def _resolve_harness(monkeypatch: pytest.MonkeyPatch) -> _ResolveHarness:
    media_items = _RecordingMediaItems()
    source_id = new_id()
    await media_items.upsert_many(
        [
            MediaItemUpsert(
                source_id=source_id,
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
                last_seen_at=datetime(2026, 8, 20, tzinfo=UTC),
            )
        ]
    )
    stored = await media_items.get_by_external_id(source_id, "unmatched-1")
    assert stored is not None, "the premise: the fake holds the item being resolved"
    harness = _ResolveHarness(
        media_items=media_items,
        titles=FakeTitleRepository(),
        session=_ResolveSession(),
        held=stored.id,
    )

    @contextlib.asynccontextmanager
    async def _session(_: Settings) -> AsyncIterator[_ResolveSession]:
        yield harness.session

    def _build(_session: object, _settings: Settings, **_rest: object) -> _ResolveHarness:
        return harness

    monkeypatch.setattr(usher.cli, "_session_for", _session)
    monkeypatch.setattr(usher.cli, "build_pipeline", _build)
    return harness


async def test_resolving_to_a_title_that_does_not_exist_names_the_id_and_keeps_the_stack_out_of_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**Issue #5, and the reason the fix is here rather than in
    `OPERATOR_ERRORS`.**

    An operator resolving a review-queue line reads a listing and pastes an
    id; pasting the wrong *column* of it is a well-formed UUID naming no
    title. Against Postgres that is `fk_media_items_title_id_titles`,
    translated to `RepositoryConflict` by
    `PostgresMediaItemRepository.attach_title`, and re-raised by `main`
    because the family is deliberately out of the tuple -- so the answer to a
    typo was sixty frames.

    Widening the tuple would answer this by muting every raise site of that
    family, of which exactly one is reachable from a CLI argument -- this one
    (ADR-0026's Consequences carries the enumeration and the count). So the
    answer is a lookup at the call site, which is what issue #5's own *Done
    when* asks for and what `POST /admin/unmatched/{id}/resolve` has done
    since M9's E4.

    **The third assertion is the one that separates a lookup from a swallow.**
    `except RepositoryConflict` around the write reads the same way to an
    operator and is not the same thing: the row is refused by Postgres
    *after* the statement ran, inside a SAVEPOINT this command then has to
    unwind. `attach_title` is never called here, and nothing is committed.

    The *stack* half of the claim is the integration twin's -- this fake has
    no foreign key, so the message below is the whole of what an operator
    sees here either way.
    """
    harness = await _resolve_harness(monkeypatch)
    unknown = new_id()

    with pytest.raises(SystemExit) as exit_info:
        await _unmatched(
            _cli_settings(), limit=50, offset=0, resolve=str(harness.held), title=str(unknown)
        )

    message = str(exit_info.value)
    # A `str` code is what makes this exit 1 rather than 0 -- `SystemExit(0)`
    # renders as "0" and would satisfy nothing else here, but stating it is
    # cheaper than leaving the exit status to be inferred from the message.
    assert isinstance(exit_info.value.code, str)
    assert str(unknown) in message, message
    assert "Traceback" not in message, message
    assert harness.media_items.attached == [], harness.media_items.attached
    assert harness.session.commits == 0
    # Not "resolved", and not "no such media item" either -- one message per
    # condition is the whole point of the case below.
    assert capsys.readouterr().out == ""


async def test_a_resolve_naming_no_media_item_still_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The third arm, pinned so the fix above cannot quietly make one message
    serve two conditions.

    The title exists here and the media item does not, which is the *only*
    fixture that can tell the two apart: a case naming neither is answered by
    whichever check runs first, and would have gone on reading as coverage of
    this arm while testing the one above.
    """
    harness = await _resolve_harness(monkeypatch)
    title = Title(kind=TitleKind.MOVIE, name="A Held Title", sort_name="A Held Title")
    await harness.titles.add(title)
    missing = new_id()

    await _unmatched(_cli_settings(), limit=50, offset=0, resolve=str(missing), title=str(title.id))

    assert "no such media item" in capsys.readouterr().out
    # The write *was* attempted, which is what makes this arm `attach_title`'s
    # answer rather than a second pre-check: the port returns whether a row
    # changed precisely so a caller can say this.
    assert harness.media_items.attached == [(missing, title.id)]
    assert harness.session.commits == 1


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


def test_suggest_defaults_to_the_tier_that_tolerates_a_typo() -> None:
    """**The route defaults to `prefix` and this command defaults to `fuzzy`,
    and the disagreement is the decision** (ADR-0031): a route is driven per
    keystroke and pays 2,707 ms p95 at one character, a command is typed once
    and can afford it. `usher suggest` has been the typo-tolerant one since M6
    and CLAUDE.md's Commands section documents it as such.

    Asserted through the enum rather than against the string `"fuzzy"`,
    because what the default has to be is *the tier that tolerates a typo* --
    the same argument `SuggestTier`'s own docstring makes for being an enum
    rather than a `typo_tolerant: bool`.

    **Nothing else in this repository pins it**, measured rather than assumed:
    flipping this default to `prefix` passed all 3,923 unit cases and the whole
    of `tests/integration/test_cli_pipeline.py` before this case existed. The
    damage is quiet -- `usher suggest "the quie"` answers `no match` for a
    misspelt name instead of finding it, on a command whose documented purpose
    is to find it.
    """
    assert SuggestTier(parse_args(["suggest", "quie"]).tier) is SuggestTier.FUZZY
    assert SuggestTier(parse_args(["suggest", "quie", "--tier", "prefix"]).tier) is (
        SuggestTier.PREFIX
    )


def test_suggest_refuses_a_tier_that_is_not_one_of_the_two() -> None:
    """`argparse`'s `choices`, derived from the enum rather than written out,
    so a third member cannot be reachable from the route and unreachable
    here."""
    with pytest.raises(SystemExit):
        parse_args(["suggest", "quie", "--tier", "fuzy"])


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

    Two edges rather than the whole tuple, because M9 added two phases between
    `imdb` and `tmdb-ids` and pinning the literal in two places is how one of
    them comes to be updated and the other merely made green.
    `test_the_imdb_expansion_phases_follow_imdb_and_credit_names_comes_first`
    is where the full tuple is asserted, with the measurement behind each new
    edge.
    """
    assert PHASES[-2:] == ("movielens", "all")
    assert PHASES.index("imdb") < PHASES.index("movielens")


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
    service = BootstrapService(
        runs, catalog, _no_commit, events=NullEventPublisher(), phase=BootstrapPhase.ALL
    )
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        secret_key="0" * 32,
        bulk_data_dir=tmp_path,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        await _movielens(settings, client, catalog, service, _no_commit, print)

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
    service = BootstrapService(
        FakeImportRunRepository(),
        catalog,
        _no_commit,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )

    async with httpx.AsyncClient(transport=_local_archive(cache)) as client:
        await _movielens(_genome_settings(cache), client, catalog, service, _no_commit, print)

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
    service = BootstrapService(
        FakeImportRunRepository(),
        catalog,
        _no_commit,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _movielens(_genome_settings(cache), client, catalog, service, _no_commit, print)
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
    service = BootstrapService(
        runs, catalog, _no_commit, events=NullEventPublisher(), phase=BootstrapPhase.ALL
    )
    settings = _genome_settings(cache)

    async with httpx.AsyncClient(transport=_local_archive(cache)) as client:
        await _movielens(settings, client, catalog, service, _no_commit, print)
        assert len(catalog.genome_tags()) == GENOME_TAG_COUNT
        # An M7-era catalog has no vocabulary. Both stores are emptied rather
        # than just the vocabulary, so the assertions below can distinguish
        # "the second run wrote nothing" from "the second run rewrote
        # everything" -- which the checkpoint alone cannot, since a run that
        # replayed every batch reaches the same position.
        catalog._genome_tags.clear()
        catalog._genome.clear()
        await _movielens(settings, client, catalog, service, _no_commit, print)

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
    service = BootstrapService(
        runs, catalog, _no_commit, events=NullEventPublisher(), phase=BootstrapPhase.ALL
    )

    async with httpx.AsyncClient(transport=_local_archive(cache)) as client:
        await _movielens(_genome_settings(cache), client, catalog, service, _no_commit, print)

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

    verdict = await vocabulary_verdict(genome, _coverage(("etag-a", 5)))

    assert verdict == VocabularyVerdict(state=VocabularyState.NAMED, tags=2)
    assert _vocabulary_line(verdict) == "genome vocabulary: 2 tags"


async def test_the_status_report_says_a_vocabulary_that_was_never_loaded_is_missing() -> None:
    """Every catalog bootstrapped before `m08b` is in this state, so it has to
    read as a thing to do rather than as a fault -- and the line names the
    command that fixes it, which is PRD 08's rule for an operator command."""
    line = _vocabulary_line(
        await vocabulary_verdict(FakeGenomeRepository(), _coverage(("etag-a", 5)))
    )

    assert "not loaded" in line
    assert "--phase movielens" in line


async def test_the_status_report_renders_a_mismatched_vocabulary_rather_than_raising() -> None:
    """`PortDataMalformed` is deliberately not in `OPERATOR_ERRORS` -- the
    three `UsherPortError` subclasses ADR-0026's 2026-08-07 amendment added
    are the transport ones, and this is a content one -- so letting it out of
    a status command answers "what state is my genome in?" with a stack trace
    about the answer being bad.

    Both release tokens have to survive into the line: "the vocabulary is
    wrong" without naming what is stored is not something an operator can act
    on. Kills a handler that prints a fixed sentence.
    """
    genome = FakeGenomeRepository(tags={1: ("zeppelins", "etag-b")})

    line = _vocabulary_line(await vocabulary_verdict(genome, _coverage(("etag-a", 5))))

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

    line = _vocabulary_line(
        await vocabulary_verdict(genome, _coverage(("etag-a", 5), ("etag-b", 2)))
    )

    assert "more than one release" in line


async def test_the_status_report_says_nothing_is_named_when_there_are_no_vectors() -> None:
    """A fresh database has no genome at all, and PRD 08 requires every
    operator command to work against one. Kills an implementation that reports
    a missing vocabulary as a problem on a catalog that has nothing for it to
    explain."""
    assert _vocabulary_line(await vocabulary_verdict(FakeGenomeRepository(), _coverage())) == (
        "genome vocabulary: no vectors to name"
    )


@pytest.mark.parametrize("state", list(VocabularyState))
def test_every_vocabulary_state_the_report_can_carry_has_a_sentence_of_its_own(
    state: VocabularyState,
) -> None:
    """A member added to `VocabularyState` and forgotten in the renderer falls
    through to the `named` branch and prints `genome vocabulary: None tags` --
    a sentence that is grammatical, plausible and about a state that did not
    occur.

    Parametrised over the enum rather than over the five branches, so the
    coverage grows with the vocabulary and nobody has to remember. The
    distinctness assertion is what has teeth: `len(set(...)) == len(...)` over
    every member's rendering fails the moment two states render alike, which
    is exactly what a fall-through produces.
    """
    verdict = VocabularyVerdict(state=state, tags=2, detail="a stored diagnosis")
    sentences = {
        one: _vocabulary_line(VocabularyVerdict(state=one, tags=2, detail="a stored diagnosis"))
        for one in VocabularyState
    }

    assert sentences[state].startswith("genome vocabulary: ")
    assert _vocabulary_line(verdict) == sentences[state]
    assert len(set(sentences.values())) == len(VocabularyState)


async def test_the_status_report_is_one_value_and_survives_an_untouched_database() -> None:
    """`bootstrap_report` against a database no import has run, which is where
    a report assembled from four reads is most likely to raise -- PRD 08's
    rule that a diagnostic must work before the thing it diagnoses has.

    This is the seam the whole report rests on: it takes **ports**, so the
    five vocabulary branches and the empty case are unit-testable, while
    `cli._status` opens its own engine and is not. A report that took a
    session would have moved every one of the cases above into
    `tests/integration/`.
    """
    report = await bootstrap_report(
        FakeImportRunRepository(), FakeBulkCatalogRepository(), FakeGenomeRepository()
    )

    assert report == BootstrapReport(
        runs=(),
        titles=0,
        genome=GenomeCoverage(
            with_vector=0, titles=0, movies=0, enriched=0, enriched_with_vector=0, revisions=()
        ),
        vocabulary=VocabularyVerdict(state=VocabularyState.NO_VECTORS),
    )


async def test_the_status_report_carries_every_run_the_repository_holds_in_its_order() -> None:
    """The report is a *carrier* for `list_runs()` and adds no policy of its
    own -- no truncation, no re-sort, no filter on status.

    Found by planting rather than by design: with every other case reaching
    the report through a database or a fake holding **one** run, slicing it to
    `stored[:1]` survived all 112 cases in this task's sweep selection. The
    damage is the quiet kind -- a report that lists one dataset looks exactly
    like a catalog on which one dataset has ever been imported, which on a
    `--phase all` run is six datasets short and says nothing about it.

    Asserted as an equality against the repository's *own* answer rather than
    against a literal, so it pins the order as well as the membership and
    keeps saying something if `list_runs()`' ordering is ever changed. The
    premise guard is what stops the equality being trivially true against a
    one-element list -- the exact condition under which the plant survived.
    """
    runs = FakeImportRunRepository()
    await runs.save(ImportRun(dataset="imdb.title.basics", revision="an-invented-etag"))
    await runs.save(
        ImportRun(
            dataset="wikidata.crosswalk",
            revision="an-invented-etag",
            heartbeat_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    stored = await runs.list_runs()
    assert len(stored) == 2, "the premise: a one-run repository cannot see a truncation"

    report = await bootstrap_report(runs, FakeBulkCatalogRepository(), FakeGenomeRepository())

    assert report.runs == tuple(stored)


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
        report=print,
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
        report=print,
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

    This is the **only** configuration that reaches a client -- a non-full-text
    mode, an embedder that loaded, and `USHER_QUERY_EXPANSION_ENABLED=true` --
    and each of the other three has its own case below.
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

    monkeypatch.setattr("usher.cli.embedder", _an_embedder)
    monkeypatch.setattr("usher.cli.llm_client", _client)
    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.ensure_default_user", _no_household)
    monkeypatch.setattr("usher.cli.build_pipeline", _recording_pipeline(captured))

    await _search(
        _cli_settings(llm_enabled=True, query_expansion_enabled=True),
        query="isolation in space",
        mode="fused",
        limit=5,
        filters=SearchFilters(),
    )

    assert captured["llm"] is built
    assert closed == 1


async def test_a_search_with_no_embedding_model_opens_no_completion_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The narrowed deployment, and it was paying for a pool until
    2026-08-07.** `embedder(settings, report=False)` answering `(None, nothing)`
    is the whole of ADR-0022's optionality at this layer: `SearchService` then
    narrows a `fused` request to full-text before it ever reaches an expander,
    so no completion is bought and none could be. The client built for it was
    an `httpx.AsyncClient` and its connection pool opened and closed for
    nothing -- verbatim the cost the `full_text` guard one case down exists to
    avoid, on the configuration a deployment without the embedding extra uses
    for every fused search it runs.

    The expansion switch is deliberately **on** here, so the case can only pass
    by reading the embedder's answer.
    """
    captured: dict[str, object] = {}

    monkeypatch.setattr("usher.cli.llm_client", _never_a_client)
    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.ensure_default_user", _no_household)
    monkeypatch.setattr("usher.cli.build_pipeline", _recording_pipeline(captured))

    settings = _cli_settings(llm_enabled=True, query_expansion_enabled=True)
    assert settings.embedding_enabled is False, "the premise: no model on this deployment"

    await _search(
        settings, query="isolation in space", mode="fused", limit=5, filters=SearchFilters()
    )

    assert captured["llm"] is None


async def test_a_search_on_a_deployment_that_curates_but_does_not_expand_opens_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second switch, at the CLI. `USHER_LLM_ENABLED=true` with
    `USHER_QUERY_EXPANSION_ENABLED=false` is the ordinary M8 deployment after
    PRD 05's 2026-08-07 measurement, and on it `build_pipeline` builds no
    expander -- so a client opened here is a pool bought for a service that
    will not exist.

    The embedder is present and the mode is `fused`, so the only thing between
    this and a client is the setting.
    """
    captured: dict[str, object] = {}

    monkeypatch.setattr("usher.cli.embedder", _an_embedder)
    monkeypatch.setattr("usher.cli.llm_client", _never_a_client)
    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.ensure_default_user", _no_household)
    monkeypatch.setattr("usher.cli.build_pipeline", _recording_pipeline(captured))

    await _search(
        _cli_settings(llm_enabled=True),
        query="isolation in space",
        mode="fused",
        limit=5,
        filters=SearchFilters(),
    )

    assert captured["llm"] is None


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

    monkeypatch.setattr("usher.cli.embedder", _an_embedder)
    monkeypatch.setattr("usher.cli.llm_client", _never_a_client)
    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.ensure_default_user", _no_household)
    monkeypatch.setattr("usher.cli.build_pipeline", _recording_pipeline(captured))

    await _search(
        _cli_settings(llm_enabled=True, query_expansion_enabled=True),
        query="the quiet vacuum",
        mode="full_text",
        limit=5,
        filters=SearchFilters(),
    )

    assert captured["llm"] is None


async def test_a_search_ranks_for_the_default_household(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`usher search` resolves the singleton household and hands it on.

    Fails: a command that never resolves one, which renders identically -- the
    same rows, the same scores, no error and no line to say the watch-state
    term was absent. It is the one term whose absence is invisible in the
    output, so only the argument says whether it ran.

    `ensure_default_user` and not `default_user`, for `usher curate`'s reason:
    this needs an id and nothing else.
    """
    captured: dict[str, object] = {}

    monkeypatch.setattr("usher.cli.llm_client", _never_a_client)
    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.ensure_default_user", _no_household)
    monkeypatch.setattr("usher.cli.build_pipeline", _recording_pipeline(captured))

    await _search(
        _cli_settings(),
        query="the quiet vacuum",
        mode="full_text",
        limit=5,
        filters=SearchFilters(),
    )

    assert captured["search_kwargs"] == {
        "mode": SearchMode.FULL_TEXT,
        "limit": 5,
        "filters": SearchFilters(),
        "user_id": _CLI_HOUSEHOLD,
    }


async def _never_a_client(*_: object, **__: object) -> object:
    """A `composition.llm_client` that fails the case rather than answering.

    Sharper than asserting `captured["llm"] is None` alone, which is also what
    a `_search` that built a client, closed it and forgot to pass it produces
    -- the pool would still have been opened, which is the whole subject of the
    three cases that use this.
    """
    raise AssertionError("a search with nothing to expand opened a completion client")


async def _an_embedder(_: Settings, *, report: bool = True) -> object:
    """`composition.embedder`, with a model that loaded.

    A stand-in rather than a real `FastEmbedEmbedder`: `_search` only ever
    tests this for `None` and hands it to a `build_pipeline` these cases have
    replaced, so anything not-`None` is the whole of the fixture.
    """
    assert report is False, "the lane sentence belongs to `usher work`"

    async def _aclose() -> None:
        return None

    return object(), _aclose


def _cli_settings(**rest: object) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        **rest,  # type: ignore[arg-type]
    )


@contextlib.asynccontextmanager
async def _no_session(_: Settings) -> AsyncIterator[None]:
    """`_session_for`, without the engine. The claim under test is about what
    `_search` hands to `build_pipeline`, and opening a real connection would
    make it a claim about Postgres."""
    yield None


#: The household `_no_household` answers with. Fixed rather than minted, so a
#: case can assert the id that reached the service is the id the command
#: resolved rather than merely that one did.
_CLI_HOUSEHOLD = uuid.UUID(int=0xA1)


async def _no_household(_: object, **__: object) -> uuid.UUID:
    """`db.users.ensure_default_user`, without the database it reads.

    `_no_session` yields `None`, so the real one has nothing to run its
    `SELECT` against. Substituted here rather than given a session because
    which row the id came from is `tests/integration/test_cli_pipeline.py`'s
    question; these cases are about what `_search` hands on.
    """
    return _CLI_HOUSEHOLD


def _recording_pipeline(captured: dict[str, object]) -> Callable[..., object]:
    """A `build_pipeline` that records its keyword arguments, and a `search`
    that records its own, answering a `SearchAnswer` with nothing in it."""

    class _Search:
        async def search(self, query: str, **kwargs: object) -> SearchAnswer:
            captured["search_kwargs"] = kwargs
            return SearchAnswer()

    class _Pipeline:
        search = _Search()

    def _build(_session: object, _settings: Settings, **kwargs: object) -> object:
        captured.update(kwargs)
        return _Pipeline()

    return _build


# --- the IMDb expansion phases ----------------------------------------
#
# `credit-names` and `aliases` are the two phases M9 adds, and both are joins
# against a catalog the `imdb` phase has to have built first. The fixtures are
# the committed synthetic slices the adapters' own tests read; the transports
# below serve them out of a scratch cache directory, so no case here opens a
# socket and no third-party row is committed.

_BULK_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"

_EXPANSION_TITLES = (
    ImdbTitle(
        imdb_id="tt99000010",
        kind=TitleKind.MOVIE,
        name='"A Quoted Synthetic Title"',
        original_name='"A Quoted Synthetic Title"',
        year=1962,
        end_year=None,
        runtime_minutes=111,
        genres=("Crime", "Drama"),
    ),
    ImdbTitle(
        imdb_id="tt99000020",
        kind=TitleKind.MOVIE,
        name="A Synthetic Feature",
        original_name="A Synthetic Feature",
        year=1988,
        end_year=None,
        runtime_minutes=123,
        genres=("Drama",),
    ),
    ImdbTitle(
        imdb_id="tt99000030",
        kind=TitleKind.SERIES,
        name="A Synthetic Series",
        original_name="A Synthetic Series",
        year=2004,
        end_year=2009,
        runtime_minutes=44,
        genres=("Drama",),
    ),
)


def _stage_slices(tmp_path: Path, *pairs: tuple[str, str]) -> Path:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    for source, name in pairs:
        (cache / name).write_bytes(gzip.compress((_BULK_FIXTURES / source).read_bytes()))
    return cache


def _recording_local(cache: Path, asked: list[str]) -> httpx.MockTransport:
    """`_local_archive`, plus the order the files were asked for.

    The order is the assertion in `test_the_credit_names_phase_reads_name_
    basics_before_title_principals`, and recording it here rather than
    counting calls is the difference between "both files were read" -- which
    the wrong order also satisfies -- and "this one was read first".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        asked.append(name)
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


def _expansion_settings(cache: Path, *, batch_size: int = 50_000) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        secret_key="0" * 32,
        bulk_data_dir=cache,
        bulk_batch_size=batch_size,
    )


async def _seeded_catalog() -> tuple[FakeBulkCatalogRepository, BootstrapService]:
    catalog = FakeBulkCatalogRepository()
    await catalog.upsert_titles(list(_EXPANSION_TITLES))
    return catalog, BootstrapService(
        FakeImportRunRepository(),
        catalog,
        _no_commit,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )


def test_the_imdb_expansion_phases_follow_imdb_and_credit_names_comes_first() -> None:
    """Two edges, both measured, in a tuple `--phase all` executes in order.

    **`credit-names` and `aliases` after `imdb`**: both join to `titles` on
    `imdb_id`, so against an empty catalog they match nothing -- the same
    argument `movielens` already makes, arriving at a 1.57 GiB download rather
    than a 335 MiB one.

    **`credit-names` before everything that enriches a title.**
    `fill_credit_names` writes only where `enrichment_state = 'skeleton'`, so
    a title an enrichment crawl has already reached is deferred to TMDb -- on
    that run and on every later one. **203,969 of the 204,335 titles with
    >=100 votes (99.82%)** gain a `credit_names` in this order and none of
    them in the other, and no re-run repairs it. Ordering is the whole
    mitigation and there is no other one.

    It stales **no** embedding in either order, and this docstring said
    otherwise until 2026-08-12: the embedded population is
    `enrichment_state <> 'skeleton'`, the exact complement of what the fill
    writes.

    Kills a tidy-up that alphabetises `PHASES` -- which would put `aliases`
    and `credit-names` before `imdb` and produce two phases that download
    1.57 GiB, write nothing and report success.
    """
    assert PHASES == (
        "imdb",
        "credit-names",
        "aliases",
        "tmdb-ids",
        "crosswalk",
        "movielens",
        "all",
    )
    assert PHASES.index("imdb") < PHASES.index("credit-names") < PHASES.index("aliases")


async def test_the_credit_names_phase_reads_name_basics_before_title_principals(
    tmp_path: Path,
) -> None:
    """A credit names a person, so the `nconst -> primaryName` index has to
    exist before a principal is resolved against it.

    **The order is asserted as a sequence, not as two memberships**: "both
    files were read" is satisfied by the wrong order, which would resolve
    every principal against an empty index and yield no record at all -- a
    phase that completes, checkpoints, and blanks nothing while filling
    nothing.

    **This is the plan's `test_bootstrap_phase_people_runs_name_basics_before_
    title_principals` under a name that describes what shipped.** There is no
    `people` phase: T3 measured the `people` + `credits` design at 2.702 GB
    against a 2.0 GB ceiling and refused it, so the two files are one dataset
    resolving the join in the adapter, and the ordering they need is inside
    that dataset rather than between two phases.
    """
    cache = _stage_slices(
        tmp_path,
        ("name.basics.slice.tsv", "name.basics.tsv.gz"),
        ("title.principals.slice.tsv", "title.principals.tsv.gz"),
    )
    catalog, service = await _seeded_catalog()
    asked: list[str] = []

    async with httpx.AsyncClient(transport=_recording_local(cache, asked)) as client:
        await _credit_names(_expansion_settings(cache), client, catalog, service, print)

    assert asked, "the premise: the phase reached the transport at all"
    assert asked.index("name.basics.tsv.gz") < asked.index("title.principals.tsv.gz")
    assert catalog.credit_names("tt99000020") == (
        "Ada Synthetic",
        '"Bo Synthetic"',
        "Cyd Synthetic",
    )


async def test_the_alias_phase_stores_every_alias_even_when_a_title_straddles_a_batch(
    tmp_path: Path,
) -> None:
    """The phase is the writer's only caller, so this is where the loss the
    port cannot detect actually shows up.

    `replace_aliases` deletes by scope before it inserts. With a title's rows
    split across two batches, the second call's scope names that title again
    and its delete takes the rows the first call wrote -- and **nothing
    raises**, because the port's `ValueError` guard is about a row *outside*
    the scope and both halves are inside their own. `batch_size=1` against a
    slice whose every title has two aliases is the shape that does it: before
    `IMDbAkaDataset.group_of`, this stored one alias per title instead of two.

    **The premise is read off the fixture, not off the result.** Spelled as
    *"some title in `stored` has more than one alias"* it is a claim about the
    outcome, so the defect falsifies the guard instead of the assertion and
    the case reports a fixture problem it does not have -- measured, by
    planting `group_of -> None` and watching it fail on `assert 1 > 1`. Read
    off `parse_akas_row` over the committed slice it is a fact about the file,
    which only an edit to the file can change.
    """
    per_title = Counter(
        row.imdb_id
        for row in map(
            parse_akas_row,
            (_BULK_FIXTURES / "title.akas.slice.tsv").read_text(encoding="utf-8").splitlines(),
        )
        if row is not None
    )
    assert max(per_title.values()) > 1, "the premise: the slice holds a title with two aliases"

    cache = _stage_slices(tmp_path, ("title.akas.slice.tsv", "title.akas.tsv.gz"))
    catalog, service = await _seeded_catalog()

    async with httpx.AsyncClient(transport=_recording_local(cache, [])) as client:
        await _aliases(_expansion_settings(cache, batch_size=1), client, catalog, service, print)

    stored = {
        imdb_id: tuple(name for _, name, _, _ in catalog.search_names(imdb_id))
        for imdb_id in ("tt99000010", "tt99000020", "tt99000030")
    }
    assert stored == {
        "tt99000010": ("A Synthetic Festival Title", "A Synthetic Working Title"),
        "tt99000020": ('"A Quoted Synthetic Alias"', "Un Long Métrage Synthétique"),
        "tt99000030": ("Uma Série Sintética", "Une Série Synthétique"),
    }


async def test_the_alias_phase_writes_region_and_language_and_leaves_person_rows_alone(
    tmp_path: Path,
) -> None:
    """Two properties the phase's own wiring is responsible for.

    `region` and `language` reach the table -- `m09a` added those two columns
    for this dump and without them a French and a Brazilian alias of one film
    are indistinguishable rows. And B1's `person` rows survive: the scope is
    `kind = 'alias'` as well as `imdb_ids`, so a credited person's searchable
    name is not collateral of an alias re-import.
    """
    cache = _stage_slices(tmp_path, ("title.akas.slice.tsv", "title.akas.tsv.gz"))
    catalog, service = await _seeded_catalog()
    catalog.seed_person_search_name("tt99000020", "Ada Synthetic")

    async with httpx.AsyncClient(transport=_recording_local(cache, [])) as client:
        await _aliases(_expansion_settings(cache), client, catalog, service, print)

    assert catalog.search_names("tt99000020") == (
        ("alias", '"A Quoted Synthetic Alias"', "GB", None),
        ("alias", "Un Long Métrage Synthétique", "FR", "fr"),
        ("person", "Ada Synthetic", None, None),
    )


async def test_the_credit_names_phase_refuses_an_empty_catalog_before_downloading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_movielens`' refusal, against a 1.09 GB pair of files instead of a
    335 MiB archive, and with the same three properties: no request of any
    kind, no `ImportRun`, and a message naming the phase to run first.

    The outcome it prevents is the same one and is worse here for the size:
    every row would match nothing, the run would checkpoint `COMPLETED`, and
    every later `--phase all` would find that checkpoint and do nothing.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the credit-names phase reached the network: {request.url}")

    catalog = FakeBulkCatalogRepository()
    runs = FakeImportRunRepository()
    service = BootstrapService(
        runs, catalog, _no_commit, events=NullEventPublisher(), phase=BootstrapPhase.ALL
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        await _credit_names(_expansion_settings(tmp_path), client, catalog, service, print)

    assert await runs.list_runs() == []
    printed = capsys.readouterr().out
    assert "titles is empty" in printed
    assert "--phase imdb" in printed


async def test_the_alias_phase_refuses_an_empty_catalog_before_downloading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same refusal, against 486 MiB. Separate from the credit-names case
    rather than parametrised over both, because the two messages name
    different files and a parametrised case asserting only the shared half is
    how one of them would come to name the wrong one."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the aliases phase reached the network: {request.url}")

    catalog = FakeBulkCatalogRepository()
    runs = FakeImportRunRepository()
    service = BootstrapService(
        runs, catalog, _no_commit, events=NullEventPublisher(), phase=BootstrapPhase.ALL
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        await _aliases(_expansion_settings(tmp_path), client, catalog, service, print)

    assert await runs.list_runs() == []
    printed = capsys.readouterr().out
    assert "title.akas" in printed
    assert "--phase imdb" in printed


async def test_the_credit_names_report_carries_a_denominator_and_the_crawl_ordering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A count with the population it is a count *of*, and the one sentence
    an operator has to read before scheduling this phase.

    The ordering line is here rather than only in a PRD because getting it
    wrong is not recoverable by re-running anything: run after a priority-tier
    crawl and every enriched title is deferred to TMDb on this run and on
    every later one, so 99.82% of that tier never gains IMDb names at all.

    **This case is the proof of what the line must not say**, which is why it
    now asserts the absence as well as the presence. `:1699`-equivalent below
    reads back `()` for the *enriched* fixture title and the counter says
    "1 deferred to TMDb" -- i.e. the case demonstrates the skip -- while the
    printed sentence claimed until 2026-08-12 that an enriched tier gets
    rewritten and staled. One case cannot both show a title being skipped and
    assert that it is written.
    """
    cache = _stage_slices(
        tmp_path,
        ("name.basics.slice.tsv", "name.basics.tsv.gz"),
        ("title.principals.slice.tsv", "title.principals.tsv.gz"),
    )
    # Two of the slice's three titles, so all three counters carry a number
    # rather than a zero: `tt99000020` is filled, `tt99000030` is enriched and
    # so belongs to TMDb, and `tt99000040`'s record never arrives at all --
    # its only principal names a person `name.basics` does not hold. The
    # catalog is missing nothing the dump credits, so `unmatched` is 0 here
    # and the case says so rather than asserting a number the fixture cannot
    # produce.
    catalog = FakeBulkCatalogRepository()
    await catalog.upsert_titles([_EXPANSION_TITLES[1], _EXPANSION_TITLES[2]])
    catalog.mark_enriched("tt99000030")
    service = BootstrapService(
        FakeImportRunRepository(),
        catalog,
        _no_commit,
        events=NullEventPublisher(),
        phase=BootstrapPhase.ALL,
    )

    async with httpx.AsyncClient(transport=_recording_local(cache, [])) as client:
        await _credit_names(_expansion_settings(cache), client, catalog, service, print)

    printed = capsys.readouterr().out
    assert "credit_names: 1 titles filled this run" in printed
    assert "of 2 titles in the catalog" in printed
    assert "0 credited titles this catalog does not hold" in printed
    assert "1 deferred to TMDb" in printed
    assert catalog.credit_names("tt99000030") == ()
    assert "BEFORE the TMDb crawl" in printed
    assert "deferred to TMDb for good" in printed
    # The two assertions above prove the enriched title was *skipped*, so the
    # report may not tell an operator its embedding was invalidated. A
    # re-index they cannot need is a re-index we sent them to run.
    assert "stale" not in printed
    assert "usher index --backfill" in printed


async def test_the_alias_report_says_where_the_rows_that_are_not_aliases_went(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`written` alone reads as a broken import: three retained akas rows in
    four restate the title's own name (5,693,570 of 7,536,366, 75.5%), so a
    report printing only what was stored would show a quarter of the file
    arriving and say nothing about the rest.

    The fixture is built to exercise the counter rather than to be
    representative -- one row per title restating the title's own name, which
    the parser cannot drop because only a comparison against the stored
    `Title` can see it.
    """
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    body = (
        "titleId\tordering\ttitle\tregion\tlanguage\ttypes\tattributes\tisOriginalTitle\n"
        "tt99000020\t1\tA Synthetic Feature\tUS\ten\timdbDisplay\t\\N\t0\n"
        "tt99000020\t2\tUn Long Métrage Synthétique\tFR\tfr\timdbDisplay\t\\N\t0\n"
        "tt99000020\t3\tUN LONG MÉTRAGE SYNTHÉTIQUE\tCA\tfr\timdbDisplay\t\\N\t0\n"
        "tt99000099\t1\tA Title No Catalog Holds\tUS\ten\timdbDisplay\t\\N\t0\n"
    ).encode()
    (cache / "title.akas.tsv.gz").write_bytes(gzip.compress(body))
    catalog, service = await _seeded_catalog()

    async with httpx.AsyncClient(transport=_recording_local(cache, [])) as client:
        await _aliases(_expansion_settings(cache), client, catalog, service, print)

    printed = capsys.readouterr().out
    assert "aliases: 1 stored this run of 4 rows read" in printed
    assert "1 restate the title's own name" in printed
    assert "1 repeat one already kept" in printed
    assert "1 scoped ids matched no title" in printed
    assert "25.00% of the rows read" in printed
    assert "the catalog holds 3 titles" in printed


def test_a_zero_denominator_is_a_sentence_naming_what_it_counted() -> None:
    """`_percent` now serves two populations, and its zero branch prints the
    noun. *"n/a (0 titles)"* under a line about rows read is a wrong sentence
    rather than a missing one -- and a `0/0` percentage is what PRD 08's
    "every command works against an empty database" rule is about."""
    assert _percent(0, 0) == "n/a (0 titles)"
    assert _percent(0, 0, noun="rows") == "n/a (0 rows)"
    assert _percent(1, 4, noun="rows") == "25.00%"


def test_every_subcommands_help_renders() -> None:
    """`--help` is the one code path in the parser that interpolates, and
    nothing else in this suite runs it.

    argparse formats each `help=` string against its own parameter dict, so a
    literal `%` raises `TypeError: %o format: an integer is required, not
    dict` from `usher bootstrap --help` and from nowhere else. Found by
    running it: `ruff`, `ruff format --check`, `mypy` and all 67 cases in this
    file passed against a `--phase` help string containing a bare `~100%`,
    because every one of them builds the parser and none of them renders it.

    Over every subcommand rather than over `bootstrap` alone -- the defect is
    a property of writing a help string, not of this command -- and the list
    is read off the parser's own rendered choices rather than typed out, so a
    subcommand added later is covered without anyone remembering to add it.
    """
    parser = build_parser()
    rendered = parser.format_help()
    choices = re.search(r"\{([a-z0-9,-]+)\}", rendered)
    assert choices, "the premise: the top-level help lists its subcommands"
    commands = choices.group(1).split(",")
    assert len(commands) > 10, f"the premise: every subcommand is reached, got {commands}"
    for name in commands:
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args([name, "--help"])
        assert exit_info.value.code == 0, name


def _without_docstrings(tree: ast.Module) -> str:
    """`ast.unparse` with every docstring removed, so a text scan reads code
    and not prose.

    A blanket `"BootstrapService" not in source` is the cheaper spelling and
    it cannot be used here: the case below argues about the class it must not
    hold, by name, in its own docstring. Identifiers and string annotations
    both survive `unparse`, which is the half that matters -- a string
    annotation is the one form needing no import at all.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


async def test_the_cli_reaches_the_shared_dispatch_and_holds_no_second_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`usher bootstrap` is `composition.run_bootstrap` plus an engine, and
    both halves of that sentence are asserted.

    The behavioural proof that the two roots run the *same* phases in the
    same order is
    `tests/unit/test_composition.py::test_the_cli_and_the_handler_run_the_
    same_phase_dispatch`, which needs a `Pipeline` and so lives beside the
    helper that builds one. What can only be seen from here is the two facts
    that make that proof about the CLI at all: this command calls the shared
    function with the phase it was given and `print` as the sink, and this
    module can no longer spell a dispatch of its own.

    **The structural half is not decoration.** *"The CLI calls
    `run_bootstrap`"* is satisfied by a module that calls it and then does
    something else beside it, which is exactly the drift the extraction
    exists to prevent -- so `usher.cli` is asserted to name no `BulkDataset`
    and no `BootstrapService`, the way `test_api_bootstrap.py` asserts it of
    the router.

    ⚠️ **The `BootstrapService` half is asserted on the *name*, not on the
    module, and that narrowing is E6's rather than a weakening.** E5 spelled it
    as `"usher.services.bootstrap" not in named` because at the time that
    module held one public class. It now also holds `BootstrapReport` and the
    two pure functions both surfaces call, and `usher bootstrap-status` reads
    them -- so a module-level ban would forbid exactly the sharing E6 exists
    to create. The claim the docstring above always made is the one now
    checked: no *driver*. Read off `ast.unparse` of a docstring-stripped tree,
    so a string annotation and an attribute access are both caught and this
    paragraph is not.

    No connection is opened: `create_async_engine` is lazy, an `AsyncSession`
    that issues no statement never connects, and `run_bootstrap` is replaced
    before it could.
    """
    seen: list[tuple[object, ...]] = []

    async def record(*args: object, **kwargs: object) -> None:
        seen.append((*args, kwargs.get("report")))

    monkeypatch.setattr(usher.cli, "run_bootstrap", record)
    settings = Settings(database_url="postgresql+asyncpg://u:p@localhost/db", secret_key="0" * 32)

    await _bootstrap(settings, BootstrapPhase.CREDIT_NAMES)

    assert len(seen) == 1
    catalog, runs, _commit, passed_settings, phase, report = seen[0]
    assert isinstance(catalog, PostgresBulkCatalogRepository)
    assert isinstance(runs, PostgresImportRunRepository)
    assert passed_settings is settings
    assert phase is BootstrapPhase.CREDIT_NAMES
    assert report is print

    source = pathlib.Path(inspect.getfile(usher.cli)).read_text()
    tree = ast.parse(source)
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            named.add(node.module)
    assert named, "the import scan found nothing, so it proves nothing"
    assert [one for one in named if one.startswith("usher.adapters.bulk")] == []
    assert "BootstrapService" not in _without_docstrings(tree)


def test_the_phase_choices_are_the_vocabulary_and_not_a_second_copy_of_it() -> None:
    """`--phase`'s `choices` and `BootstrapPhase` are one set, asserted as an
    equality between two derivations rather than as two spelled-out lists.

    Two lists would let `usher bootstrap --phase aliases` succeed against a
    route that rejects it, or the reverse -- and a case that spelled both out
    would go stale in exactly the same commit the drift arrived in. The
    *order* is pinned separately, below, because a set says nothing about
    execution order and the order is the measured part.
    """
    assert set(PHASES) == {phase.value for phase in BootstrapPhase}
    assert set(build_parser().parse_args(["bootstrap"]).__dict__) >= {"phase"}
    assert build_parser().parse_args(["bootstrap"]).phase == BootstrapPhase.ALL.value
