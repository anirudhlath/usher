"""The CLI's argument surface and its default. No database, no network.

Plus one case that is not about arguments: `usher push` with no `--probe`
is a composition root nothing else calls, and wiring nothing calls is
wiring nothing checks.
"""

import asyncio

import pytest

from usher.cli import PHASES, SYNC_KINDS, _run_lanes, build_parser, parse_args
from usher.config import Settings


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
