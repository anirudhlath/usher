"""`usher rotate-secret` -- its argument surface, its dispatch arm, its report.

The same split `test_cli_backup.py` and `test_cli_restore.py` make: every
command coroutine in `usher.cli` takes a `Settings` and builds its own engine
through `_session_for`, so what the rotation does against a real schema lives
in `tests/integration/test_rotation.py`. What is here needs no database --
the parser, the environment read, the key validation and
`_print_rotation_report`, which is a pure function over a `RotationReport`.

**The `_dispatch` arm is the case this file exists for.**
`.claude/rules/config-cli-and-deployment.md` records the measurement:
`_dispatch`'s `else` is `serve`, so a subcommand that parses and has no arm
of its own does not fail -- it starts uvicorn -- and
`test_every_command_reports_a_dead_database_the_same_way` cannot see it.

**And the security cases are the point of the command.** Two claims are
asserted here rather than argued: that the new key never reaches `argv`, by
running the real parser and greping the namespace; and that neither a
rejected key nor a stored credential reaches a message, by seeding a canary
and asserting its *presence* somewhere first.
"""

import os

import pytest
from pydantic import SecretStr

from usher.cli import _new_secret_key, _print_rotation_report, build_parser, main, parse_args
from usher.config import Settings
from usher.services.rotation import RotationReport

# The variable this command's help text, PRD 08 and CLAUDE.md all name.
VAR = "USHER_NEW_SECRET_KEY"

# A new key that `Settings` would accept: 40 characters, distinctive enough
# that finding it anywhere is unambiguous.
NEW_KEY = "z9-rotation-canary-" + "n" * 21

# The value shipped in documentation as a placeholder, spelled as a literal
# rather than imported from `usher.config._PLACEHOLDER_SECRET_KEY`: what this
# case is about is the string an operator copies out of a setup guide, and a
# test that imports the constant the code compares against cannot fail when
# the comparison is removed *and* the constant moves with it. It is exactly
# 32 characters, so `min_length` alone does not refuse it -- which is what
# makes it a case about the validator rather than about the bound.
PLACEHOLDER = "change-me-to-a-long-random-string"


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)


def _settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@127.0.0.1:1/usher"),
        secret_key=SecretStr("0" * 32),
    )


def test_rotate_secret_takes_a_variable_name_and_never_a_key() -> None:
    """The acceptance criterion, run through the real parser.

    A key passed as an argument is in the shell's history file and in `ps`
    output for every user on the box, and neither is undone by the command
    exiting. `--new-key-env` names the variable instead, so the value is never
    a token argparse sees.
    """
    argv = ["rotate-secret", "--new-key-env", VAR]
    args = parse_args(argv)

    assert vars(args) == {"command": "rotate-secret", "traceback": False, "new_key_env": VAR}
    # The surface offers nothing that *could* carry a key.
    assert "--new-key" not in build_parser().format_help()


def test_the_key_itself_is_absent_from_argv_and_from_the_parsed_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same claim as a *value* question rather than a shape one.

    The premise fires first: the key really is in the environment under that
    name, so the two absences below are about a value that exists in this
    process and could have been picked up.
    """
    monkeypatch.setenv(VAR, NEW_KEY)
    assert os.environ[VAR] == NEW_KEY, "the premise: the key is in this process's environment"

    argv = ["rotate-secret", "--new-key-env", VAR]
    args = parse_args(argv)

    assert not [token for token in argv if NEW_KEY in token]
    assert not [name for name, value in vars(args).items() if NEW_KEY in repr(value)]
    # And the parser resolves the name to a name, not to the value behind it:
    # an implementation that helpfully read `os.environ` at parse time would
    # pass both assertions above only by accident of spelling.
    assert args.new_key_env == VAR


def test_the_variable_name_is_required() -> None:
    """No default, because this command rewrites every stored credential in
    the deployment: a bare `usher rotate-secret` must not pick up a variable
    left over in the shell from a previous run."""
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["rotate-secret"])
    assert exit_info.value.code == 2


def test_rotate_secret_dispatches_to_rotate_and_not_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`_dispatch`'s `else` arm is `serve`**, so a subcommand with no arm of
    its own silently starts the HTTP server and looks like it worked.

    The argument is asserted as well as the call, and here that is a security
    assertion rather than only a wiring one: what crosses `_dispatch` is the
    **variable name**, so a frame summary of this call site cannot print a
    key.
    """
    _configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)
    ran: list[str] = []

    async def _record(settings: Settings, *, new_key_env: str) -> None:
        ran.append(new_key_env)

    def _served(*_: object, **__: object) -> None:
        raise AssertionError("usher rotate-secret started the HTTP server")

    monkeypatch.setattr("usher.cli._rotate", _record)
    monkeypatch.setattr("uvicorn.run", _served)

    main(["rotate-secret", "--new-key-env", VAR])

    assert ran == [VAR]
    assert NEW_KEY not in repr(ran)


def test_an_unset_variable_is_a_sentence_naming_it_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commonest way to get this wrong is to forget the `export`, and the
    message has to be the fix rather than a `KeyError`."""
    monkeypatch.delenv(VAR, raising=False)

    with pytest.raises(SystemExit) as exit_info:
        _new_secret_key(_settings(), VAR)

    message = str(exit_info.value)
    assert VAR in message
    assert "Traceback" not in message
    assert ".env" in message, "the message has to warn against the one place it must not go"


def test_an_empty_variable_is_refused_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """`export USHER_NEW_SECRET_KEY=` is set-and-empty, which `os.environ.get`
    answers with `""` rather than `None`.

    `Settings` has a validator for exactly this shape one namespace over
    (*"not set" is not "set to the empty string"*), and an empty key here
    would otherwise reach pydantic as a `min_length` failure -- a correct
    refusal with a message about characters, for an operator whose real
    mistake was a truncated shell variable.
    """
    monkeypatch.setenv(VAR, "")

    with pytest.raises(SystemExit) as exit_info:
        _new_secret_key(_settings(), VAR)

    assert "is not set" in str(exit_info.value)


def test_a_key_shorter_than_settings_would_accept_is_refused_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotation to a key `Settings` would refuse is a rotation that bricks
    the next start, so the refusal has to arrive here rather than from
    pydantic at the next boot with every credential already re-encrypted.

    The premise is that the short value really was read: a `_new_secret_key`
    that ignored the environment would refuse this for the *absent-variable*
    reason and pass a naive assertion on `SystemExit` alone.
    """
    short = "too-short-to-be-a-key"
    monkeypatch.setenv(VAR, short)
    assert len(short) < 32, "the premise: this is below Settings.secret_key's own bound"

    with pytest.raises(SystemExit) as exit_info:
        _new_secret_key(_settings(), VAR)

    message = str(exit_info.value)
    assert "secret_key" in message
    assert "is not set" not in message, "refused for the wrong reason"
    assert short not in message, "the rejected key reached the message"
    assert "values are not shown" in message


def test_the_documented_placeholder_is_refused_even_though_it_is_long_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`min_length` is not the whole of `Settings.secret_key`'s rules, and the
    placeholder is the half a length check cannot see: it is exactly 32
    characters, so this case is about `_reject_placeholder_secret_key` and
    nothing else."""
    monkeypatch.setenv(VAR, PLACEHOLDER)
    assert len(PLACEHOLDER) >= 32, "the premise: min_length alone would accept this"

    with pytest.raises(SystemExit) as exit_info:
        _new_secret_key(_settings(), VAR)

    message = str(exit_info.value)
    assert "secret_key" in message
    assert PLACEHOLDER not in message


def test_a_key_settings_accepts_comes_back_as_the_secret_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the three refusals above. Without it, *"a bad key is
    refused"* is satisfied by a function that refuses everything."""
    monkeypatch.setenv(VAR, NEW_KEY)

    key = _new_secret_key(_settings(), VAR)

    assert isinstance(key, SecretStr)
    assert key.get_secret_value() == NEW_KEY
    # `SecretStr` and not `str`, so it cannot reach a log line by accident:
    # this is the type the whole of `CLAUDE.md`'s secrets rule rests on.
    assert NEW_KEY not in repr(key)


def test_the_report_prints_three_counts_and_names_every_refused_ref(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No cap on the refused list, unlike `_print_restore_report`'s
    `_REFUSALS_NAMED`: a restore can refuse 14,166 rows and this table holds
    one row per configured source."""
    report = RotationReport(rotated=("ref-a",), already=("ref-b", "ref-c"), refused=("ref-d",))

    _print_rotation_report(report, new_key_env=VAR)

    out = capsys.readouterr().out
    assert "rotated     1" in out
    assert "already     2" in out
    assert "refused     1" in out
    assert "refused: ref-d" in out
    # The ticket sentence, on every run: rotating invalidates every
    # outstanding playback ticket, and an operator watching a dashboard for
    # the next minute should know why.
    assert "/play" in out
    # And the next step, which is the one an operator forgets.
    assert VAR in out


def test_a_run_that_rotated_nothing_does_not_tell_anyone_to_restart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control for the line above. A no-op rerun printing *"set
    USHER_SECRET_KEY and restart"* would send an operator to change a key that
    is already the right one."""
    _print_rotation_report(
        RotationReport(rotated=(), already=("ref-b",), refused=()), new_key_env=VAR
    )

    out = capsys.readouterr().out
    assert "restart" not in out
    assert "already     1" in out


def test_a_refused_row_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_sync`'s and `_restore`'s precedent: the refused refs are on stdout
    for a human, and cron, CI and a systemd unit read the exit code."""
    _configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)

    async def _refusing(*_: object, **__: object) -> RotationReport:
        return RotationReport(rotated=(), already=(), refused=("ref-d",))

    monkeypatch.setattr("usher.services.rotation.RotationService.rotate", _refusing)
    monkeypatch.setattr("usher.cli._session_for", _no_session)

    with pytest.raises(SystemExit) as exit_info:
        main(["rotate-secret", "--new-key-env", VAR])

    message = str(exit_info.value)
    assert "re-entered" in message
    assert exit_info.value.code != 0


def test_a_run_with_nothing_refused_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the case above, and the one that would catch a command
    that exits non-zero on every run."""
    _configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)

    async def _clean(*_: object, **__: object) -> RotationReport:
        return RotationReport(rotated=("ref-a",), already=(), refused=())

    monkeypatch.setattr("usher.services.rotation.RotationService.rotate", _clean)
    monkeypatch.setattr("usher.cli._session_for", _no_session)

    main(["rotate-secret", "--new-key-env", VAR])


class _NoSession:
    """Stands in for `_session_for` so these cases build no engine.

    `commit` is the only attribute `_rotate` reads off the session, and
    `PostgresCredentialRotationStore` is constructed with it and then never
    asked anything, because `RotationService.rotate` is what is patched.
    """

    async def commit(self) -> None:  # pragma: no cover - never reached
        raise AssertionError("a patched rotation committed")


def _no_session(_settings_arg: Settings) -> "_SessionContext":
    return _SessionContext()


class _SessionContext:
    async def __aenter__(self) -> _NoSession:
        return _NoSession()

    async def __aexit__(self, *_: object) -> None:
        return None
