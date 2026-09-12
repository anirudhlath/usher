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

import argparse
import contextlib
import io
import os
import re

import pytest
from pydantic import SecretStr

from tests.unit.commands import configured, dispatched
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


def _settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@127.0.0.1:1/usher"),
        secret_key=SecretStr("0" * 32),
    )


#: A key an operator could plausibly have made with the command this project
#: documents, chosen so that it is a **legal environment variable name**: 64
#: lowercase hex characters beginning with a letter, which is 6/16 = 37.5% of
#: `openssl rand -hex 32`'s output space (measured over 100,000 samples:
#: 37.6%). It is the case a grammar check alone cannot see.
HEX_KEY_THAT_IS_A_LEGAL_NAME = "f72e4ec32beea584456a" + "a" * 44

#: A key that is *not* a legal name: `openssl rand -base64 32`'s alphabet
#: carries `+`, `/` and `=`, none of which a variable name may hold. This is
#: the half the grammar check catches, and the case asserts that premise.
BASE64_KEY = "aB3/xY+9zQ7wE1rT2uI5oP8kL0jH6gF4dS2aZ1xC3v=="


def _merged(argv: list[str]) -> tuple[object, str]:
    """Run `main` and return its exit code beside **everything an operator
    sees**: stdout, stderr and the `SystemExit` string, concatenated.

    The three together, because the leak this file's security cases are about
    was found on the merged stream -- argparse writes its refusals to stderr
    and this command writes its own to `SystemExit`, so a case reading only
    one of them can watch a key go past on the other.
    """
    out, err = io.StringIO(), io.StringIO()
    code: object = 0
    message = ""
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main(argv)
    except SystemExit as exc:
        code = exc.code
        message = "" if isinstance(exc.code, int) or exc.code is None else str(exc.code)
    return code, out.getvalue() + err.getvalue() + message


def test_rotate_secret_takes_a_variable_name_and_never_a_key() -> None:
    """The acceptance criterion, run through the real parser.

    A key passed as an argument is in the shell's history file and in `ps`
    output for every user on the box, and neither is undone by the command
    exiting. `--new-key-env` names the variable instead, so the value is never
    a token argparse sees.

    ⚠️ **This case asserts a *shape*, and a shape assertion is exactly what the
    2026-08-26 abbreviation defect satisfied**: `--new-key <the key>` bound to
    `new_key_env`, so the namespace was this dict with the wrong value in the
    right field. It is kept because the surface is worth pinning, and the cases
    below it are the ones with teeth.
    """
    args = parse_args(["rotate-secret", "--new-key-env", VAR])

    assert vars(args) == {"command": "rotate-secret", "traceback": False, "new_key_env": VAR}
    # `--new-key` is a tripwire rather than an alternative, so it stays out of
    # `--help` -- asserted on the *subparser's* help, because the top-level
    # help lists subcommand names and would satisfy this vacuously.
    rotate_help = _rotate_secret_help()
    assert "--new-key-env" in rotate_help
    assert not re.search(r"--new-key(?!-env)", rotate_help)


def test_a_key_passed_as_new_key_is_refused_and_never_appears_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 **The 2026-08-26 defect, and it was a silent success.**

    `argparse`'s `allow_abbrev` defaults to `True`, so `--new-key` was an
    unambiguous prefix of `--new-key-env`: the operator's key bound to the
    field meant for a variable *name*, and the "is not set" message printed it
    back twice -- once as `$<key>` and once inside a suggested `export
    <key>=...` that invites a paste into the same history the design exists to
    keep it out of.

    `--new-key` is the single most likely thing to type, because the help text
    and every document about this command say *"the new key"*.

    The premise fires first: the key is a token of the invocation, so its
    absence from the output is a claim about a value that was right there.

    🔴 **The last three assertions are the repair, and the sweep is what asked
    for them.** *"Exit 2 and the key is absent"* is satisfied by deleting the
    `--new-key` tripwire entirely: `allow_abbrev=False` then leaves argparse
    saying *"the following arguments are required: --new-key-env"*, which is a
    refusal, is silent about the value, and even contains the string
    `--new-key-env`. Measured -- with the tripwire deleted this file was
    **19 passed**. So the assertions asserted a rejection and this repository
    already knows that *"a rejection is not an assertion: two implementations
    that fail for opposite reasons produce the identical failure value"*.

    What the tripwire actually buys is the **sentence**, and the sentence is
    the point: an operator who has just typed their new key at a shell has put
    it in `~/.bash_history` and in `ps` output, and nothing about exiting 2
    tells them to go and deal with that. So the message is what is pinned.
    """
    configured(monkeypatch)
    argv = ["rotate-secret", "--new-key", NEW_KEY]
    assert NEW_KEY in argv, "the premise: the key really is in this invocation"

    code, seen = _merged(argv)

    assert code == 2
    assert NEW_KEY not in seen
    assert "--new-key-env" in seen, "the refusal has to say what to do instead"
    # The teaching half, and the half a bare "required argument" refusal has
    # none of: *why* the key must not have been there, and therefore what the
    # operator has to clean up now that it has been.
    assert "shell history" in seen
    assert "ps" in seen
    assert "NAME" in seen


def test_an_abbreviation_cannot_bind_a_value_into_the_variable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`allow_abbrev=False` on this subparser, and the two shapes differ.

    Alone, `--new-k <key>` leaves `--new-key-env` unsatisfied and argparse says
    so. Beside a real `--new-key-env`, it becomes an *unrecognized argument* --
    which is the path argparse would print the key on, and the one
    `_parse_without_echoing_unknown_values` exists for. Both are asserted,
    because fixing the first is what creates the second.
    """
    configured(monkeypatch)

    for argv in (
        ["rotate-secret", "--new-k", NEW_KEY],
        ["rotate-secret", "--new-k", NEW_KEY, "--new-key-env", VAR],
        ["rotate-secret", "--newkey", NEW_KEY, "--new-key-env", VAR],
    ):
        assert NEW_KEY in argv
        code, seen = _merged(argv)
        assert code == 2, argv
        assert NEW_KEY not in seen, argv


def test_a_key_given_to_the_variable_flag_itself_is_refused_without_being_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half `allow_abbrev=False` does not reach: the operator uses the
    **right** flag and passes the key to it.

    Two premises, and the second is the whole reason this case exists.
    """
    configured(monkeypatch)
    # Premise 1: this really is a legal environment variable name, so the
    # grammar check cannot be what refuses it.
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", HEX_KEY_THAT_IS_A_LEGAL_NAME)
    # Premise 2: and it really is a key -- `Settings` accepts it as one, which
    # is exactly the predicate the refusal uses and the reason it can refuse.
    accepted = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@127.0.0.1:1/usher"),
        secret_key=SecretStr(HEX_KEY_THAT_IS_A_LEGAL_NAME),
    ).secret_key
    assert accepted.get_secret_value() == HEX_KEY_THAT_IS_A_LEGAL_NAME

    code, seen = _merged(["rotate-secret", "--new-key-env", HEX_KEY_THAT_IS_A_LEGAL_NAME])

    assert code != 0
    assert HEX_KEY_THAT_IS_A_LEGAL_NAME not in seen
    assert "NAME" in seen


def test_a_name_that_is_not_an_environment_variable_name_is_refused_without_being_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grammar half. `openssl rand -base64 32` carries `+/=` and a
    hyphenated key carries `-`, none of which is a legal name -- so the
    commonest way to reach this refusal is to have passed the key, and
    repeating it is the defect."""
    configured(monkeypatch)
    assert not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", BASE64_KEY), "the premise"

    code, seen = _merged(["rotate-secret", "--new-key-env", BASE64_KEY])

    assert code != 0
    assert BASE64_KEY not in seen
    assert "environment variable" in seen


def test_an_unrecognised_argument_names_its_value_on_every_command_except_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scrub is scoped, and the control is what says so.

    argparse's *"unrecognized arguments: %s"* is how an operator fixes a typo,
    so it is kept verbatim everywhere else. It is suppressed on
    `rotate-secret` alone, where an unrecognised value is most likely a key.
    """
    configured(monkeypatch)

    _, elsewhere = _merged(["backup", "--nonsense", NEW_KEY])
    assert "unrecognized arguments" in elsewhere
    assert NEW_KEY in elsewhere, "the control: other commands still name what they refused"

    _, here = _merged(["rotate-secret", "--nonsense", NEW_KEY, "--new-key-env", VAR])
    assert "unrecognized arguments for rotate-secret" in here
    assert NEW_KEY not in here


def test_prefix_matching_is_off_for_this_command_and_on_for_every_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blast radius of `allow_abbrev=False`, pinned rather than asserted in
    a comment.

    It is set on this subparser only -- measured, because a subparser does
    **not** inherit it from the parser that created it -- so exactly three
    option strings lose prefix matching and nineteen other commands keep it.
    """
    configured(monkeypatch)

    # Off here: an abbreviation of the one real flag no longer parses.
    with pytest.raises(SystemExit):
        parse_args(["rotate-secret", "--new-key-e", VAR])

    # On everywhere else: `usher backup --out` still means `--output`.
    assert parse_args(["backup", "--out", "usher-backup.jsonl.gz"]).output.name == (
        "usher-backup.jsonl.gz"
    )


def _rotate_secret_help() -> str:
    """The `rotate-secret` subparser's own help.

    Not `build_parser().format_help()`, which lists subcommand *names* and no
    option of any of them -- so an assertion about `--new-key` against it
    passes whatever the subparser declares.
    """
    subparsers = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    parser = subparsers.choices["rotate-secret"]
    return str(parser.format_help())


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
    """The argument is asserted as well as the call, and here that is a
    security assertion rather than only a wiring one: what crosses `_dispatch`
    is the **variable name**, so a frame summary of this call site cannot
    print a key."""
    configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)

    calls = dispatched(monkeypatch, arm="_rotate", argv=["rotate-secret", "--new-key-env", VAR])

    assert [kwargs["new_key_env"] for _, kwargs in calls] == [VAR]
    assert NEW_KEY not in repr(calls)


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
    for a human, and cron, CI and a systemd unit read the exit code.

    ⚠️ **The fixture rotates a row as well as refusing one, and that is
    load-bearing since M10's K8.** It refused a single row out of a single row
    until then, which is the *saturated* count — and `_rotation_refusal` now
    reads that as *"the old key is wrong"* and deliberately does not say
    "re-entered". This case is about the **exit code**, which is non-zero on
    both arms, so it keeps its subject and its assertion by naming a report
    only the partial arm can produce.
    `test_a_run_that_refused_every_row_blames_the_old_key_and_not_the_
    credentials` asserts the exit code on the other arm, so nothing this case
    used to cover is now uncovered.
    """
    configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)

    async def _refusing(*_: object, **__: object) -> RotationReport:
        return RotationReport(rotated=("ref-a",), already=(), refused=("ref-d",))

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
    configured(monkeypatch)
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


def test_a_run_that_refused_every_row_blames_the_old_key_and_not_the_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K8's drill measured the state this message is for, and measured that
    the message was wrong in it.

    With `USHER_SECRET_KEY` already changed to the new key -- the `.env`-first
    mistake, and the likeliest operator error this command has -- `cli._rotate`
    builds `old_cipher` from that same key, both ciphers are one cipher, and
    every row still on the previous key opens under neither. The rows are
    **intact**; the run wrote nothing.

    Today's sentence tells that operator their credentials *"must be
    re-entered"*, and an operator who obeys it re-types every credential in the
    deployment for a problem they do not have. So the saturated count gets its
    own diagnosis: a counter whose saturation implies a different cause than
    its partial values needs the saturated case named, or the message written
    for the partial case is the one that gets acted on.
    """
    configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)

    async def _all_refused(*_: object, **__: object) -> RotationReport:
        return RotationReport(rotated=(), already=(), refused=("ref-a", "ref-b", "ref-c"))

    monkeypatch.setattr("usher.services.rotation.RotationService.rotate", _all_refused)
    monkeypatch.setattr("usher.cli._session_for", _no_session)

    with pytest.raises(SystemExit) as exit_info:
        main(["rotate-secret", "--new-key-env", VAR])

    message = str(exit_info.value)
    assert exit_info.value.code != 0
    # It names the cause an operator can act on, and the setting to act on.
    assert "USHER_SECRET_KEY" in message
    # It says plainly that nothing was lost, which is the half that stops the
    # damage: an operator reading "refused" as "corrupt" re-types everything.
    assert "nothing was written" in message.lower()
    assert "no credential was lost" in message.lower()
    # 🔴 And it must NOT advise the destructive recovery. This is the whole
    # point of splitting the two diagnoses.
    assert "re-entered" not in message
    assert "re-register" not in message
    assert "POST /admin/sources" not in message


def test_a_run_that_refused_only_some_rows_still_says_to_re_register_those(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control, and it is what keeps the case above a statement about the
    *saturated* count rather than about the message being removed.

    A run that rotated some rows and refused others proves the old key was
    right, so a row it could not open really is unreadable and really does have
    to be re-entered. Today's sentence is correct here and is kept verbatim.
    """
    configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)

    async def _partly_refused(*_: object, **__: object) -> RotationReport:
        return RotationReport(rotated=("ref-a", "ref-b"), already=(), refused=("ref-c",))

    monkeypatch.setattr("usher.services.rotation.RotationService.rotate", _partly_refused)
    monkeypatch.setattr("usher.cli._session_for", _no_session)

    with pytest.raises(SystemExit) as exit_info:
        main(["rotate-secret", "--new-key-env", VAR])

    message = str(exit_info.value)
    assert exit_info.value.code != 0
    assert "re-entered" in message
    assert "POST /admin/sources" in message
    # And it does not claim nothing was written, because two rows were.
    assert "nothing was written" not in message.lower()


def test_a_run_where_every_row_was_already_rotated_and_one_refused_is_not_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary the predicate has to get right.

    "Every row refused" is `len(refused) == report.rows`, not `not rotated`. A
    second run over a table one earlier run had finished reports its rows as
    `already` rather than `rotated` -- so a predicate spelled `if not
    report.rotated` would call this saturated and tell an operator their
    intact, already-rotated credentials were fine when one of them is not.
    """
    configured(monkeypatch)
    monkeypatch.setenv(VAR, NEW_KEY)

    async def _already_and_one_refused(*_: object, **__: object) -> RotationReport:
        return RotationReport(rotated=(), already=("ref-a", "ref-b"), refused=("ref-c",))

    monkeypatch.setattr("usher.services.rotation.RotationService.rotate", _already_and_one_refused)
    monkeypatch.setattr("usher.cli._session_for", _no_session)

    with pytest.raises(SystemExit) as exit_info:
        main(["rotate-secret", "--new-key-env", VAR])

    message = str(exit_info.value)
    assert "re-entered" in message, "an old key that opened two rows is not the wrong key"
    assert "no credential was lost" not in message.lower()
