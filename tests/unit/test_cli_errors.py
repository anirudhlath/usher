"""The CLI's error boundary: what an operator sees when the thing that
failed is theirs to fix.

M7's smoke test recorded the finding this module closes: `usher
bootstrap-status` and `usher sync-status` against an unreachable database
printed **60 lines of asyncpg and greenlet internals** and exited 1. The
exit code was right and the presentation was not -- the operator's actual
information, `Connect call failed`, was the last line of a stack whose
first fifty frames are library code they cannot act on.

Two properties are under test here, and they pull in opposite directions,
which is why both are pinned:

1. **A failure the operator can fix is a message.** No stack, one line,
   still exit 1.
2. **A failure the operator cannot fix keeps its stack.** A `TypeError` in
   row composition is a bug, and the traceback is the bug report. A
   boundary that swallows it has traded a wart for a blindfold.

And one that is a security control rather than a presentation choice:
**a settings failure may not echo the value it rejected.** pydantic's
`ValidationError` message carries `input_value=...`, so `USHER_DATABASE_URL`
with the wrong driver printed the whole DSN -- password included -- and a
short `USHER_SECRET_KEY` printed the key. Both fields are `SecretStr`
precisely so that cannot happen. This is the same defect `usher.api.errors`
exists to prevent on the HTTP side, on the surface an operator is *more*
likely to run while pasting output into an issue.
"""

import argparse
import ast
import inspect
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy.exc import OperationalError

from usher import cli as usher_cli
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    RepositoryConflict,
    RepositoryNotFound,
    UsherPortError,
)

# The three `UsherPortError` subclasses that do *not* live in
# `ports/errors.py`. Imported here rather than left to `__subclasses__()`
# finding them by luck of another import: a class nothing has imported is not
# a subclass Python will report, and the case below is an exhaustiveness
# assertion.
from usher.ports.ingest import AvailabilitySweepRefused
from usher.ports.search import FilterNotSupported
from usher.ports.source import SourceNotSupported
from usher.services.reconcile import RETRACTION_ERROR_CODE

# A value that must never appear in anything this module asserts on. Spelled
# once so a leak fails loudly rather than being read past.
_PASSWORD = "sup3rs3cret"

# The minimal argv for every subcommand the parser advertises. A table
# rather than a loop over `choices` alone because three commands need a
# positional; `test_the_argv_table_covers_every_subcommand` is what keeps
# the two in step, so a command added without a row here fails rather than
# silently sitting outside the boundary.
_MINIMAL_ARGV: dict[str, list[str]] = {
    "serve": ["serve"],
    "bootstrap": ["bootstrap"],
    "bootstrap-status": ["bootstrap-status"],
    "sync": ["sync"],
    "sync-status": ["sync-status"],
    "unmatched": ["unmatched"],
    "work": ["work", "--once"],
    "index": ["index"],
    "derive": ["derive"],
    "search": ["search", "dune"],
    "suggest": ["suggest", "du"],
    "similar": ["similar", "--rebuild"],
    "home": ["home"],
    "curate": ["curate"],
    "push": ["push"],
}


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `Settings` that validates, so a case about the *command* failing is
    not accidentally a case about the settings failing."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)


def _raising(exc: BaseException) -> Callable[..., Coroutine[Any, Any, None]]:
    async def run(*args: object, **kwargs: object) -> None:
        raise exc

    return run


def _every_command_raises(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Make every dispatch target fail identically.

    Reached by walking the module rather than by listing the dispatch
    coroutines, for the same reason the argv table is checked against the
    parser: a hand-written list agrees with itself and with nothing else.
    **Count-free deliberately** -- this docstring said "the fourteen
    coroutines" until M8 added a fifteenth command, which is a number nobody
    re-derives and the walk never needed.
    """
    for name, value in vars(usher_cli).items():
        if name.startswith("_") and inspect.iscoroutinefunction(value):
            monkeypatch.setattr(usher_cli, name, _raising(exc))

    def boom(*args: object, **kwargs: object) -> None:
        raise exc

    # `serve` is not a coroutine, and it is the one arm the container's CMD
    # actually runs, so it is inside the boundary or the boundary is not
    # CLI-wide. Patched on the module object because `main` imports uvicorn
    # lazily -- same module, so the attribute swap is visible either way.
    monkeypatch.setattr(uvicorn, "run", boom)


def _refused() -> ConnectionRefusedError:
    """The exact exception the M7 smoke test hit, shape and all: asyncpg lets
    the raw `OSError` out rather than wrapping it, which is why the boundary
    cannot key on a SQLAlchemy type alone."""
    return ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 5432)")


def test_an_unreachable_database_is_a_message_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    monkeypatch.setattr(usher_cli, "_status", _raising(_refused()))

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap-status"])

    message = str(exit_info.value)
    # A `SystemExit` carrying a string exits 1 and prints the string on
    # stderr -- the same shape `_as_uuid` and the semantic-search guard
    # already use, so the boundary is not a second exit convention.
    assert isinstance(exit_info.value.code, str)
    assert "Connect call failed" in message
    # The type name survives because `str(OSError)` alone is
    # `[Errno 111] Connect call failed (...)`, which never says what went
    # wrong -- only where.
    assert "ConnectionRefusedError" in message
    assert "Traceback" not in message
    assert "asyncpg" not in message


def test_the_message_names_the_command_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator runs `usher bootstrap` overnight and finds the output in
    a log the next morning; a bare `[Errno 111]` with no subject is the same
    problem as the traceback, shorter."""
    _configured(monkeypatch)
    monkeypatch.setattr(usher_cli, "_sync_status", _raising(_refused()))

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["sync-status"])

    assert "sync-status" in str(exit_info.value)


def test_the_message_names_the_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stack still exists and the message has to say so, or the boundary
    has removed the only tool for the failure it is most likely to hide."""
    _configured(monkeypatch)
    monkeypatch.setattr(usher_cli, "_status", _raising(_refused()))

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap-status"])

    assert "--traceback" in str(exit_info.value)


def test_the_traceback_flag_lets_the_original_exception_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    refused = _refused()
    monkeypatch.setattr(usher_cli, "_status", _raising(refused))

    with pytest.raises(ConnectionRefusedError) as raised:
        usher_cli.main(["--traceback", "bootstrap-status"])

    # The *same* exception object, not a re-raise of a copy: the point of
    # the flag is the original stack, and `raise exc` would rebind the
    # traceback to the boundary.
    assert raised.value is refused


def test_a_programming_error_keeps_its_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary's hardest requirement, and the reason it enumerates
    families instead of catching `Exception`.

    `AttributeError` here stands for every bug: nothing the operator sets or
    starts makes it go away, so collapsing it to one line moves the cost
    from a wart to a lost bug report. This case is what a later
    `except Exception:` would break.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        usher_cli, "_status", _raising(AttributeError("'NoneType' object has no attribute 'id'"))
    )

    with pytest.raises(AttributeError):
        usher_cli.main(["bootstrap-status"])


def test_an_http_source_that_is_down_is_operator_facing_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDb, Emby and every bulk download fail through httpx, not through
    the driver -- an unreachable Emby is the same class of operator problem
    as an unreachable database and reads the same way."""
    _configured(monkeypatch)
    monkeypatch.setattr(
        usher_cli, "_sync", _raising(httpx.ConnectError("[Errno -2] Name or service not known"))
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["sync"])

    assert "ConnectError" in str(exit_info.value)
    assert "Traceback" not in str(exit_info.value)


def test_a_database_error_the_driver_does_wrap_is_operator_facing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the database story: a connect failure arrives as a
    bare `OSError`, but `relation "titles" does not exist` -- an operator who
    skipped `alembic upgrade head` -- arrives wrapped as a
    `SQLAlchemyError`."""
    _configured(monkeypatch)
    monkeypatch.setattr(
        usher_cli,
        "_status",
        _raising(OperationalError("SELECT 1", {}, Exception('relation "titles" does not exist'))),
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap-status"])

    assert 'relation "titles" does not exist' in str(exit_info.value)


def test_a_rejected_setting_is_reported_without_the_value_it_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security case, and it is a live regression rather than a
    hypothetical: `USHER_DATABASE_URL` with the wrong driver made
    `usher bootstrap-status` print

        ... [type=value_error, input_value='mysql://admin:<the password>@db:5432/usher', ...]

    because pydantic's `ValidationError` message carries the input. The
    field is `SecretStr` in `Settings` for exactly this reason, and the CLI
    was the one reader that unwrapped it.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", f"mysql://admin:{_PASSWORD}@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap-status"])

    message = str(exit_info.value)
    assert _PASSWORD not in message
    assert "mysql://" not in message
    # Redaction that also removes the diagnosis is not a fix: the operator
    # still has to learn which setting, and what it should have been.
    assert "database_url" in message
    assert "postgresql+asyncpg" in message


def test_a_rejected_secret_key_is_reported_without_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`USHER_SECRET_KEY` too short printed `input_value='0000'` -- the key
    itself. A weak key is still a key, and it is usually a real one that was
    truncated by a copy-paste rather than a placeholder."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", _PASSWORD)

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap-status"])

    message = str(exit_info.value)
    assert _PASSWORD not in message
    assert "secret_key" in message


def test_the_traceback_flag_does_not_reopen_the_settings_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--traceback` re-raises everything else, and deliberately not this.

    A settings failure's stack is always the same six pydantic frames and
    tells nobody anything; the *only* thing re-raising would add is the
    rejected value. So the one exception the flag does not reopen is the one
    whose stack is worthless and whose message is a credential.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", f"mysql://admin:{_PASSWORD}@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["--traceback", "bootstrap-status"])

    assert _PASSWORD not in str(exit_info.value)


def test_ctrl_c_during_a_long_import_is_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`usher bootstrap` is a multi-hour download that an operator is
    expected to interrupt. Interrupting it printed a `KeyboardInterrupt`
    traceback through `asyncio.run`, which reads as a failure of the run
    rather than as the operator's own decision.

    130 rather than 1: the shell's convention for "killed by SIGINT"
    (128 + 2), so a wrapping script can tell an interrupt from a failure.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(usher_cli, "_bootstrap", _raising(KeyboardInterrupt()))

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap"])

    assert exit_info.value.code == 130
    assert "interrupted" in capsys.readouterr().err
    # An interrupt is not a `KeyboardInterrupt` escaping to the terminal.
    assert "Traceback" not in capsys.readouterr().err


def test_a_deliberate_exit_message_is_not_re_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI already raised `SystemExit` with a written message in three
    places before this boundary existed -- `_as_uuid`, the semantic-search
    guard, `similar`'s cross-argument rule. Those messages were chosen for
    the failure they describe, and the boundary must pass them through
    untouched rather than prefix them with a second explanation.

    Free structurally, because `SystemExit` is a `BaseException` and the
    boundary names only `Exception` subclasses -- pinned anyway, because
    "free structurally" stops being true the moment somebody widens the
    tuple.
    """
    _configured(monkeypatch)
    written = "semantic search is unavailable -- try --mode fused, or run `usher index`"
    monkeypatch.setattr(usher_cli, "_search", _raising(SystemExit(written)))

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["search", "dune"])

    assert str(exit_info.value) == written


def test_the_parsers_own_exits_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--help` exits 0 and a bad argument exits 2 with usage. Both happen
    before the boundary, and both are what every other CLI does."""
    _configured(monkeypatch)

    with pytest.raises(SystemExit) as helped:
        usher_cli.main(["--help"])
    assert helped.value.code == 0

    with pytest.raises(SystemExit) as refused:
        usher_cli.main(["bootstrap", "--phase", "nonsense"])
    assert refused.value.code == 2


def test_the_argv_table_covers_every_subcommand() -> None:
    """The parametrised case below is only "CLI-wide" if this passes: a new
    subcommand with no row is a command nobody has checked is inside the
    boundary."""
    subparsers = next(
        action
        for action in usher_cli.build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert sorted(_MINIMAL_ARGV) == sorted(subparsers.choices)


@pytest.mark.parametrize("command", sorted(_MINIMAL_ARGV))
def test_every_command_reports_a_dead_database_the_same_way(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding named two commands; the fix is one boundary, so the case
    is every command rather than those two."""
    _configured(monkeypatch)
    _every_command_raises(monkeypatch, _refused())

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(_MINIMAL_ARGV[command])

    message = str(exit_info.value)
    assert "ConnectionRefusedError" in message
    assert "Traceback" not in message


def _function_def(name: str) -> ast.FunctionDef:
    source = Path(usher_cli.__file__).read_text(encoding="utf-8")
    return next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_the_boundary_is_one_try_around_the_whole_dispatch() -> None:
    """One case about this implementation, one about the next.

    The behavioural cases above pass just as well against one `try`/`except`
    pair per arm -- which is the shape that rots, because the *next* command
    is written by copying an arm and not the handler, and M8's `usher curate`
    is the one that arrived and proved it. This asserts the shape instead:
    `main` has exactly one `try`,
    and everything that can fail -- reading the settings, configuring
    telemetry, dispatching -- is inside it.
    """
    main_def = _function_def("main")

    tries = [node for node in main_def.body if isinstance(node, ast.Try)]
    assert len(tries) == 1, "main must have exactly one error boundary"

    guarded = {id(node) for node in ast.walk(tries[0])}
    fallible = [
        node
        for node in ast.walk(main_def)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"get_settings", "configure_telemetry", "_dispatch"}
    ]
    assert {call.func.id for call in fallible if isinstance(call.func, ast.Name)} == {
        "get_settings",
        "configure_telemetry",
        "_dispatch",
    }
    assert all(id(call) in guarded for call in fallible)


def test_the_boundary_catches_families_and_not_exception() -> None:
    """`except Exception` is the change that passes every behavioural case
    in this module except `test_a_programming_error_keeps_its_traceback`,
    and it is what somebody reaches for when a new failure escapes. The
    tuple is named so the intent is legible at the handler."""
    assert Exception not in usher_cli.OPERATOR_ERRORS
    assert BaseException not in usher_cli.OPERATOR_ERRORS
    assert OSError in usher_cli.OPERATOR_ERRORS


def test_the_port_taxonomy_is_split_and_the_base_class_is_not_in_the_tuple() -> None:
    """**The shape of ADR-0026's 2026-08-07 amendment, asserted rather than
    described**, and the assertion that fails on the one-line version of it.

    `OPERATOR_ERRORS + (UsherPortError,)` is what an implementer reaches for
    on reading "an unreachable LLM endpoint should be a sentence", and it
    passes every behavioural case in this module. It also swallows
    `PortDataMalformed` from the embedder (`fastembed` returning a different
    number of vectors than texts, which its own adapter calls the most
    damaging bug available in that milestone) and `RepositoryConflict` from
    `TitleNeighborRepository.replace` (a score outside `[0, 1]` or a
    self-neighbour, which that repository documents as a bug in the blend
    rather than a conflict a retry clears).

    So the line is drawn between the port families about **reaching** an
    upstream and everything else, and both halves are pinned -- a later
    widening to the base class fails here rather than in review.

    **`UsherPortError` has nine subclasses, not the six in
    `ports/errors.py`.** `SourceNotSupported`, `FilterNotSupported` and
    `AvailabilitySweepRefused` live beside the ports whose contract they
    belong to, which is documented at each of them and is easy to miss from
    the taxonomy module -- so the set is read off `__subclasses__()` rather
    than written out, and a tenth member arriving with no decision about it
    fails here instead of defaulting into either half.
    """
    reaching = {PortUnavailable, PortAuthFailed, PortRateLimited}
    everything_else = set(UsherPortError.__subclasses__()) - reaching
    # The six that stay out, named so the count is checkable: three that are
    # about what came back or what we tried to write and carry deliberate
    # bug tripwires, and three that no measured path reaches this boundary
    # with -- `ReconcileService` and `PushService` absorb two of them, and
    # `_TRANSLATORS` covers every `SearchFilters` field, so the third fires
    # only for a field a later milestone forgets.
    #
    # ⚠️ **`AvailabilitySweepRefused` stays out for the *absorbed* half of that
    # sentence and no longer for the other half** (M10 S9): the family has been
    # observed in the field, and it is still unreachable here because
    # `reconcile` promises never to raise it. `_sync` reports it off the run row
    # and exits non-zero -- see the three cases at the end of this module.
    assert everything_else == {
        RepositoryConflict,
        RepositoryNotFound,
        PortDataMalformed,
        SourceNotSupported,
        FilterNotSupported,
        AvailabilitySweepRefused,
    }

    assert UsherPortError not in usher_cli.OPERATOR_ERRORS
    assert not issubclass(UsherPortError, usher_cli.OPERATOR_ERRORS)
    for family in reaching:
        assert issubclass(family, usher_cli.OPERATOR_ERRORS), family
    for family in everything_else:
        assert not issubclass(family, usher_cli.OPERATOR_ERRORS), family


def test_an_unreachable_llm_endpoint_is_a_message_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**ADR-0026's own motivating defect, in the family the ADR did not
    name.**

    `OpenAICompatibleClient` translates every transport failure into a port
    error *before* it crosses the boundary -- which is what the taxonomy is
    for -- so `httpx.HTTPError` can never fire for it and
    `usher curate` against a `USHER_LLM_BASE_URL` with nothing listening used
    to print a stack ending in `PortUnavailable: POST /chat/completions
    failed: ConnectError`. Measured 2026-08-07 by driving
    `OpenAICompatibleClient.complete_json` at a loopback port with nothing
    bound: that is exactly the exception and exactly the message, and
    `isinstance(exc, cli.OPERATOR_ERRORS)` was `False`.

    It is the *most likely* runtime failure of a cron'd `usher curate`, and
    the operator has already been billed for it by the time they read it:
    `CurationService.generate` writes and commits the `llm_calls` row on its
    way out. Being handed a stack on top of that is the wart ADR-0026 was
    written about.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        usher_cli,
        "_curate",
        _raising(PortUnavailable("POST /chat/completions failed: ConnectError")),
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["curate"])

    message = str(exit_info.value)
    assert isinstance(exit_info.value.code, str)
    assert "curate" in message
    assert "POST /chat/completions failed: ConnectError" in message
    assert "Traceback" not in message
    # The flag still reopens it, which is what makes the boundary allowed to
    # drop the stack at all.
    assert "--traceback" in message


def test_a_rejected_credential_and_a_rate_limit_read_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other two thirds of the transport half, and neither is worth a
    stack: `USHER_LLM_API_KEY` is wrong, or the endpoint asked to be backed
    off and a CLI has no backoff schedule to apply."""
    _configured(monkeypatch)

    monkeypatch.setattr(
        usher_cli, "_sync", _raising(PortAuthFailed("TMDb rejected the configured API key"))
    )
    with pytest.raises(SystemExit) as rejected:
        usher_cli.main(["sync"])
    assert "TMDb rejected the configured API key" in str(rejected.value)
    assert "Traceback" not in str(rejected.value)

    monkeypatch.setattr(usher_cli, "_curate", _raising(PortRateLimited(30.0)))
    with pytest.raises(SystemExit) as limited:
        usher_cli.main(["curate"])
    assert "retry_after=30.0" in str(limited.value)
    assert "Traceback" not in str(limited.value)


def test_a_repository_conflict_keeps_its_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The half of the amendment that is a refusal**, and the case that
    fails if somebody later widens the tuple to `UsherPortError`.

    `PostgresTitleNeighborRepository.replace` raises this for a score outside
    `[0, 1]`, a self-neighbour, a negative rank or a title id naming no row --
    all four CHECKs or foreign keys, and all four *a bug in the blend* rather
    than something an operator can start, fix or wait for.
    `usher similar --rebuild` is where it surfaces, and the stack is the bug
    report. Same argument `test_a_programming_error_keeps_its_traceback`
    makes for `AttributeError`, one taxonomy over.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        usher_cli,
        "_similar",
        _raising(RepositoryConflict("a neighbour batch violates the table's own bounds")),
    )

    with pytest.raises(RepositoryConflict):
        usher_cli.main(["similar", "--rebuild"])


def test_a_malformed_upstream_payload_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FastEmbedEmbedder` raises this when it hands back a different number
    of vectors than it was given texts -- title *n*'s vector landing on title
    *m*, which its own adapter calls the most damaging bug available in that
    milestone and which no operator action reaches. `usher search --mode
    semantic` is where it surfaces.

    The commands that *can* answer a `PortDataMalformed` sensibly do it
    themselves, in the arm that knows what the message means:
    `_vocabulary_line` prints it as a status line, `_curate` turns it into a
    sentence about last night's screen. Neither is the boundary's to guess.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        usher_cli,
        "_search",
        _raising(PortDataMalformed("bge-small returned 3 vectors for 4 texts")),
    )

    with pytest.raises(PortDataMalformed):
        usher_cli.main(["search", "dune"])


# -- a failed sync run is a non-zero exit -------------------------------------
#
# The stand-ins below drive `_sync`'s real body -- every other case in this
# module patches the dispatch coroutine, which is what makes them tests of the
# *boundary*. This one is about what the command itself does with a run row it
# was handed, so the body has to run.


def _run(kind: SyncRunKind, status: SyncRunStatus, *, error: str | None = None) -> SyncRun:
    return SyncRun(
        source_id=new_id(),
        kind=kind,
        status=status,
        error=error,
        finished_at=datetime(2026, 8, 19, tzinfo=UTC),
    )


class _StubAdapter:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _sync_against(
    monkeypatch: pytest.MonkeyPatch,
    *,
    walk: SyncRun,
    watch: SyncRun,
    second: tuple[SyncRun, SyncRun] | None = None,
) -> list[_StubAdapter]:
    """Wire `_sync` to given run rows and nothing else.

    Deliberately not a fake pipeline with behaviour: the property under test is
    what the command does with `run.status`, and a stub that could itself fail
    would make a red ambiguous.

    ⚠️ **`second` is not a convenience.** With one source, *"collect the
    failures and exit after the loop"* and *"exit on the first failing source"*
    are the same program -- measured, in S9's own sweep, where moving the
    `raise` inside the loop survived every case in this module. One source
    cannot see the difference and `_sync`'s docstring calls it load-bearing, so
    a second one is what makes the claim checkable.
    """
    _configured(monkeypatch)
    sources = [
        Source(
            kind=SourceKind.EMBY,
            name=name,
            base_url="https://emby.invalid",
            credentials_ref=f"ref-{index}",
            device_id=f"device-{index}",
        )
        for index, name in enumerate(["Shared Emby", "Second Emby"][: 2 if second else 1])
    ]
    adapters = [_StubAdapter() for _ in sources]
    walks = [walk, second[0]] if second else [walk]
    watches = [watch, second[1]] if second else [watch]

    @asynccontextmanager
    async def session_for(_settings: object) -> AsyncIterator[object]:
        class _Session:
            async def commit(self) -> None:
                return None

        yield _Session()

    # Indexed off the adapter the loop is holding rather than off a counter, so
    # a plant that walks one source twice is not silently handed the second
    # source's rows.
    class _Reconcile:
        async def reconcile(self, _source: Source, _kind: object, adapter: object) -> SyncRun:
            return walks[adapters.index(adapter)]  # type: ignore[arg-type]

    class _Watch:
        async def sync(self, _source: Source, adapter: object, **kwargs: object) -> SyncRun:
            return watches[adapters.index(adapter)]  # type: ignore[arg-type]

    class _Pipeline:
        reconcile = _Reconcile()
        watch = _Watch()

    async def selected(*args: object, **kwargs: object) -> list[Source]:
        return sources

    async def default_user_id(*args: object, **kwargs: object) -> uuid.UUID:
        return new_id()

    async def open_adapter(_pipeline: object, source: Source) -> _StubAdapter:
        return adapters[sources.index(source)]

    monkeypatch.setattr(usher_cli, "_session_for", session_for)
    monkeypatch.setattr(usher_cli, "build_pipeline", lambda *a, **k: _Pipeline())
    monkeypatch.setattr(usher_cli, "selected_sources", selected)
    monkeypatch.setattr(usher_cli, "ensure_default_user", default_user_id)
    monkeypatch.setattr(usher_cli, "_open_adapter", open_adapter)
    return adapters


def test_a_refused_sweep_is_reported_at_the_boundary_the_operator_actually_watches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that recorded `FAILED` must not leave the command exiting 0.

    🔴 **Measured on a real deployment before this case existed.** The
    operator's own `sync_runs` holds a `full` run from 2026-08-13 that recorded
    `FAILED` with *"refusing to mark 60 of 180 items unavailable in one run
    (33% exceeds the 25% ceiling)"* -- and `usher sync` printed that line and
    **exited 0**. The same table holds ten `watch_state` runs that have failed
    every night since, also at exit 0. A human watching the terminal sees it; a
    cron entry, a CI step and a systemd unit all see success.

    **The plan for this task said the fix is adding `AvailabilitySweepRefused`
    to `OPERATOR_ERRORS`, and that would have changed nothing.**
    `ReconcileService.reconcile` absorbs it into a `FAILED` row by contract and
    its own docstring promises exactly that -- a promise worth keeping, since a
    multi-source sync must not abort on one source's refusal. The exception
    never reaches `main`'s boundary, so the tuple never sees it. What reaches
    the operator is the **run row**, and the exit status is what was lying
    about it.

    Three assertions, and the reporting one is not redundant with the exit:
    a command that exits 1 having printed nothing is a worse outcome than the
    one being fixed, and the numbers are the operator's actual information.

    The positive control is the second case below: the same wiring with both
    runs completing must exit **0**, or "raises SystemExit" is satisfied by a
    command that fails unconditionally.
    """
    # Built the way `ReconcileService._recorded_error` builds it, from the same
    # constant, because this case and the service are two halves of one
    # agreement: the CLI matches a token the service has to have written. A
    # literal here would let either side drift and leave both suites green --
    # `test_a_refused_sweep_records_the_token_the_cli_matches_on` in
    # `tests/integration/test_services_reconcile.py` is the other half, and it
    # drives a real refusal through real Postgres rather than composing a string.
    refusal = (
        f"{RETRACTION_ERROR_CODE}: refusing to mark 60 of 180 items unavailable "
        "in one run (33% exceeds the 25% ceiling); nothing was retracted"
    )
    _sync_against(
        monkeypatch,
        walk=_run(SyncRunKind.FULL, SyncRunStatus.FAILED, error=refusal),
        watch=_run(SyncRunKind.WATCH_STATE, SyncRunStatus.COMPLETED),
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["sync"])

    output = capsys.readouterr()
    combined = output.out + output.err + str(exit_info.value)

    assert exit_info.value.code != 0, "a run that failed must not exit 0"
    assert "60 of 180" in combined and "25% ceiling" in combined, (
        "the two numbers are the operator's information and must survive to the terminal"
    )
    assert "--allow-full-retraction" in combined, (
        "the refusal is the one sync failure with an escape hatch, and nothing "
        "else in the output names it"
    )


def test_a_sync_whose_runs_all_completed_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control for the case above, and it is the one with teeth.

    `pytest.raises(SystemExit)` is satisfied by a command that exits non-zero
    unconditionally, and so is every assertion about the message. This is the
    same wiring with two completed runs, and it must reach the end of `main`
    without raising at all.
    """
    adapters = _sync_against(
        monkeypatch,
        walk=_run(SyncRunKind.FULL, SyncRunStatus.COMPLETED),
        watch=_run(SyncRunKind.WATCH_STATE, SyncRunStatus.COMPLETED),
    )

    usher_cli.main(["sync"])

    assert "completed" in capsys.readouterr().out
    assert [one.closed for one in adapters] == [1], (
        "the premise: the body ran and closed its adapter"
    )


def test_a_source_that_failed_does_not_stop_the_next_source_being_walked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit is collected after the loop, and one source cannot end the run.

    🔴 **Nothing pinned this until S9's own sweep said so.** Moving the `raise`
    inside the loop -- the obvious spelling, and the one somebody reaches for
    when adding the exit -- survived every case in this module, because each
    wired exactly **one** source and the two programs are then identical. The
    claim was load-bearing and lived only in `_sync`'s docstring, which is the
    shape `.claude/rules/testing-discipline.md` calls a deleted guard.

    It matters because it is the same property `ReconcileService.reconcile`
    swallows the exception for in the first place: a household with a laptop
    that is asleep and a NAS that is up must still get the NAS walked. Exiting
    on the first failure would put that back one layer up, having removed it
    one layer down.

    The premise is asserted before the conclusion: **both** adapters must have
    been opened and closed, or "the second source was walked" is a claim about
    a loop that never reached it.
    """
    adapters = _sync_against(
        monkeypatch,
        walk=_run(SyncRunKind.FULL, SyncRunStatus.FAILED, error="source went away mid-walk"),
        watch=_run(SyncRunKind.WATCH_STATE, SyncRunStatus.COMPLETED),
        second=(
            _run(SyncRunKind.FULL, SyncRunStatus.COMPLETED),
            _run(SyncRunKind.WATCH_STATE, SyncRunStatus.COMPLETED),
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["sync"])

    output = capsys.readouterr().out
    assert [one.closed for one in adapters] == [1, 1], (
        "the premise: both sources were opened and both adapters released"
    )
    assert "Second Emby: full completed" in output, (
        "the second source is walked even though the first one failed"
    )
    assert exit_info.value.code != 0, "and the run still ends non-zero"


def test_a_failed_watch_lane_is_a_non_zero_exit_without_the_retraction_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lane that has actually been failing here, and the hint's negative arm.

    Ten of this deployment's thirteen `watch_state` runs have recorded `FAILED`
    and none has ever completed, every one of them at exit 0 -- so the change is
    about a failed run rather than about a refused sweep, and the `full` arm
    above would pass against a command that special-cased retraction alone.

    And the flag is **absent** here, which is the half that keeps the hint
    honest: `--allow-full-retraction` does nothing for a read timeout, and an
    escape hatch offered for every failure is one an operator learns to paste
    without reading.
    """
    _sync_against(
        monkeypatch,
        walk=_run(SyncRunKind.FULL, SyncRunStatus.COMPLETED),
        watch=_run(
            SyncRunKind.WATCH_STATE,
            SyncRunStatus.FAILED,
            error="GET /Users/{user_id}/Items failed: ReadTimeout after 30.0s (read budget)",
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["sync"])

    combined = capsys.readouterr().out + str(exit_info.value)
    assert exit_info.value.code != 0
    assert "watch_state" in combined and "ReadTimeout" in combined
    assert "--allow-full-retraction" not in combined, (
        "the hint belongs to the one failure it resolves, not to every failure"
    )
