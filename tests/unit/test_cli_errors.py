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
import contextlib
import inspect
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy.exc import DBAPIError, MissingGreenlet, OperationalError

from usher import cli as usher_cli
from usher.config import Settings
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
    "genres": ["genres"],
    "search": ["search", "dune"],
    "suggest": ["suggest", "du"],
    "eval": ["eval"],
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


def test_a_missing_greenlet_keeps_its_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case issue #8 is about, and it is
    `test_a_programming_error_keeps_its_traceback` in the one family that
    reached this boundary for real.

    `MissingGreenlet` is `InvalidRequestError` is `SQLAlchemyError`, so a
    boundary that names `SQLAlchemyError` catches it -- and M9's S3 run
    recorded exactly that: one of three `usher work` daemons died at
    23:26:57Z on 2026-08-11 and the whole of what it left behind was

        usher work: MissingGreenlet: greenlet_spawn has not been called; ...
        (the stack is one flag away: `usher --traceback work`)

    Nothing an operator sets or starts makes IO-outside-a-greenlet go away;
    it is a bug in this project, and the stack is the bug report. The
    boundary's own comment already says what it means to admit -- *"everything
    the driver does wrap"* -- and that is `DBAPIError`, not every error
    SQLAlchemy can raise.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        usher_cli,
        "_work",
        _raising(MissingGreenlet("greenlet_spawn has not been called; can't call await_only()")),
    )

    with pytest.raises(MissingGreenlet):
        usher_cli.main(["work", "--once"])


def test_the_operator_database_family_is_what_the_driver_wraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The membership assertion behind the case above, so the repair cannot be
    undone by widening the tuple back without noticing.

    `DBAPIError` is the driver's half -- a missing table, a dead pool, a
    permission the role does not have. `InvalidRequestError` is this
    project's half: a session used wrong, a statement built wrong, IO in a
    place there is no greenlet to run it in. The tuple may hold the first and
    must not hold anything that catches the second.
    """
    assert DBAPIError in usher_cli.OPERATOR_ERRORS
    assert not any(issubclass(MissingGreenlet, member) for member in usher_cli.OPERATOR_ERRORS)


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


class _AbsentTitle:
    """`TitleRepository.get` for an id no row carries.

    Only the one method `_unmatched` may reach, so a resolution that grew a
    second lookup fails here rather than passing against a stub that answers
    anything.
    """

    async def get(self, title_id: uuid.UUID) -> None:
        return None


class _RefusesTheForeignKey:
    """`PostgresMediaItemRepository.attach_title` against a real Postgres.

    The raise is not invented: `media_items.title_id` carries
    `fk_media_items_title_id_titles`, the `UPDATE` matches the row, asyncpg
    raises `ForeignKeyViolationError`, and the repository translates every
    `IntegrityError` on that statement into this. Reproduced 2026-08-18
    against `pgvector/pgvector:pg17` at `alembic head` by seeding one source
    and one unmatched item and calling `_unmatched` with a well-formed title
    id naming no row -- the traceback ended in exactly this type carrying
    exactly this message.

    **The message names the media item and the media item is the id that was
    fine**, which is the second half of what made the shipped behaviour
    useless at a terminal.
    """

    def __init__(self) -> None:
        self.attempted: list[uuid.UUID] = []

    async def attach_title(
        self, media_item_id: uuid.UUID, *, title_id: uuid.UUID, episode_id: uuid.UUID | None
    ) -> bool:
        self.attempted.append(title_id)
        raise RepositoryConflict(
            f"cannot attach media item {media_item_id}",
            constraint="fk_media_items_title_id_titles",
        )


async def test_resolving_to_a_title_the_catalog_does_not_hold_is_a_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator typo in `--title` used to be a stack, and this module's
    rule 2 does not cover it: the id is well-formed, so `_as_uuid` passes it,
    and the row it names is the operator's mistake rather than a bug in this
    project's code.

    The write is what raised, so the guard has to run **before** it -- the
    order `POST /admin/unmatched/{id}/resolve` already keeps, and for the
    same reason it documents: `attach_title` writes what it is given, so a
    refusal that arrived after the write would be a refusal that had already
    happened. `attempted` is what asserts the order rather than the outcome;
    a lookup added after the call would print the same line.

    Reads like `no such media item`, the other arm of this one branch, rather
    than like `_as_uuid`'s `SystemExit`: two ways to name something that does
    not exist, in one command, would be two exit codes for one operator
    mistake.
    """
    media_items = _RefusesTheForeignKey()
    absent = uuid.UUID("0198c6b1-0000-7000-8000-00000000dead")

    class _Pipeline:
        def __init__(self) -> None:
            self.titles = _AbsentTitle()
            self.media_items = media_items

    class _Session:
        async def commit(self) -> None:
            raise AssertionError("a refused resolution must not commit")

    @contextlib.asynccontextmanager
    async def _session_for(settings: Settings) -> AsyncIterator[object]:
        yield _Session()

    monkeypatch.setattr(usher_cli, "_session_for", _session_for)
    monkeypatch.setattr(usher_cli, "build_pipeline", lambda session, settings: _Pipeline())

    await usher_cli._unmatched(
        Settings(database_url="postgresql+asyncpg://u:p@db:5432/usher", secret_key="s" * 32),
        limit=50,
        offset=0,
        resolve="0198c6b1-0000-7000-8000-000000000001",
        title=str(absent),
    )

    printed = capsys.readouterr().out
    assert str(absent) in printed, printed
    assert "resolved" not in printed, printed
    assert "Traceback" not in printed, printed
    assert media_items.attempted == [], "the write ran before the id it needs was checked"
