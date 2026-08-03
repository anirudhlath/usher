"""The CLI's argument surface and its default. No database, no network.

Plus one case that is not about arguments: `usher push` with no `--probe`
is a composition root nothing else calls, and wiring nothing calls is
wiring nothing checks.
"""

import argparse
import asyncio
import dataclasses

import pytest

from usher.cli import (
    PHASES,
    SYNC_KINDS,
    _as_uuid,
    _filters_from,
    _run_lanes,
    build_parser,
    parse_args,
)
from usher.config import Settings
from usher.domain.enums import EnrichmentState, TitleKind
from usher.ports.search import SearchFilters, SearchMode


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
