"""`usher set-credentials` -- its argument surface, its dispatch arm, its refusals.

No refusal repeats an argument, because a password can be a legal variable name.
"""

import os
import re

import pytest

from tests.unit.commands import configured, dispatched, merged
from usher.cli import parse_args

#: What a correct invocation names. A refusal repeats none of the three: the
#: command cannot tell which of its arguments an operator got wrong.
SOURCE = "set-credentials-case source"
USERNAME_VAR = "CASE_SOURCE_USERNAME"
PASSWORD_VAR = "CASE_SOURCE_PASSWORD"

#: The values behind those names, exported by every case that needs the
#: command to get past the flag it is not about.
USERNAME_VALUE = "case-username-canary"
PASSWORD_VALUE = "case-password-canary"

#: What an operator types by mistake. Both are legal variable names, which is
#: the half a grammar check cannot see.
PASSWORD = "hunter2"
USERNAME = "someone"

#: Spelled here rather than imported, so the premises below are about the
#: grammar and not about the constant `cli.py` checks against.
LEGAL_NAME = r"[A-Za-z_][A-Za-z0-9_]*"

VALID = [
    "set-credentials",
    "--source",
    SOURCE,
    "--username-env",
    USERNAME_VAR,
    "--password-env",
    PASSWORD_VAR,
]


def _repeated(seen: str, *values: str) -> list[str]:
    """Which of the invocation's arguments, or of the values behind them, `seen` repeats."""
    candidates = (*values, SOURCE, USERNAME_VAR, PASSWORD_VAR, USERNAME_VALUE, PASSWORD_VALUE)
    return [candidate for candidate in candidates if candidate in seen]


def _exported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with both variables set, so each refusal is the flag under test's own."""
    configured(monkeypatch)
    monkeypatch.setenv(USERNAME_VAR, USERNAME_VALUE)
    monkeypatch.setenv(PASSWORD_VAR, PASSWORD_VALUE)


def _argv_with(flag: str, value: str, *, joined: bool = False) -> list[str]:
    """`VALID` with `flag`'s variable name replaced by `value`, as `flag value` or `flag=value`."""
    argv = list(VALID)
    at = argv.index(flag)
    if joined:
        argv[at : at + 2] = [f"{flag}={value}"]
    else:
        argv[at + 1] = value
    return argv


def _tripwire(flag: str) -> str:
    return (
        f"usher set-credentials: error: {flag} is not an argument of this command, and the "
        "value after it is not repeated here because it is probably a credential. A "
        "credential in argv is in your shell history and in `ps` output for every user on "
        f"this box. Export it and pass the variable's NAME: {flag}-env VAR"
    )


def _unrecognised(count: int) -> str:
    return (
        f"usher: error: unrecognized arguments for set-credentials ({count}, not shown -- on "
        "this command an unrecognized value is most likely a password). The only way to name "
        "a credential here is --username-env and --password-env, which take the NAMES of "
        "exported environment variables"
    )


def _not_a_name(flag: str) -> str:
    return (
        f"usher set-credentials: {flag} takes the NAME of an exported environment variable "
        "([A-Za-z_][A-Za-z0-9_]*), and what it was given is not one. It is not repeated here, "
        "because the commonest way to reach this message is to have passed the value itself "
        "-- export it into a variable and name the variable"
    )


def _unset(flag: str) -> str:
    return (
        f"usher set-credentials: the variable {flag} names is not set or is empty; its name "
        "is not repeated here, because a password can look like a variable name"
    )


@pytest.mark.parametrize(
    ("tokens", "flag", "value"),
    [
        (["--password", PASSWORD], "--password", PASSWORD),
        ([f"--password={PASSWORD}"], "--password", PASSWORD),
        (["--username", USERNAME], "--username", USERNAME),
        ([f"--username={USERNAME}"], "--username", USERNAME),
    ],
    ids=["password", "password=", "username", "username="],
)
def test_a_credential_on_the_command_line_is_refused_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch, tokens: list[str], flag: str, value: str
) -> None:
    """`--username` and `--password` are tripwires, refused beside an otherwise valid invocation.

    The message is this case's teeth. Without the tripwires the value would still go
    unrepeated, counted by the unrecognised-argument arm; what that arm cannot say is
    why the value must not have been typed, and so what the operator now has to clean up.
    """
    _exported(monkeypatch)
    argv = [*VALID, *tokens]
    assert any(value in token for token in argv), "the premise: the value really is in argv"

    code, seen = merged(argv)

    assert code == 2
    assert _tripwire(flag) in seen.splitlines()
    assert _repeated(seen, value) == []


@pytest.mark.parametrize(
    ("tokens", "value"),
    [
        (["--pass", PASSWORD], PASSWORD),
        ([f"--pass={PASSWORD}"], PASSWORD),
        (["--user", USERNAME], USERNAME),
        ([f"--user={USERNAME}"], USERNAME),
        (["--password-e", PASSWORD], PASSWORD),
        ([f"--password-e={PASSWORD}"], PASSWORD),
    ],
    ids=["pass", "pass=", "user", "user=", "password-e", "password-e="],
)
def test_an_abbreviated_flag_is_unrecognised_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch, tokens: list[str], value: str
) -> None:
    """`allow_abbrev=False`, so a prefix binds to nothing and is counted like any stray argument.

    With prefix matching on, `--password-e hunter2` would make the password a variable
    name, and `--user=someone`, a prefix of two flags, would be refused as ambiguous by a
    message quoting the whole argument.
    """
    _exported(monkeypatch)

    code, seen = merged([*VALID, *tokens])

    assert code == 2
    assert _unrecognised(len(tokens)) in seen.splitlines()
    assert _repeated(seen, value) == []


@pytest.mark.parametrize("values", [[PASSWORD], [PASSWORD, USERNAME]], ids=["one", "two"])
def test_an_unrecognised_value_is_counted_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch, values: list[str]
) -> None:
    """Counted, where argparse's own refusal would be `unrecognized arguments: hunter2`.

    Two sizes, so the count is read off the arguments rather than being a sentence that
    happens to say one.
    """
    _exported(monkeypatch)

    code, seen = merged([*VALID, *values])

    assert code == 2
    assert _unrecognised(len(values)) in seen.splitlines()
    assert _repeated(seen, *values) == []


@pytest.mark.parametrize("flag", ["--username-env", "--password-env"])
@pytest.mark.parametrize("value", ["pa$$w0rd", "+/==", "my-pass"])
def test_a_value_that_is_no_variable_name_is_refused_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch, flag: str, value: str
) -> None:
    """The right flag, given the value itself -- the commonest way to reach this refusal."""
    _exported(monkeypatch)
    assert not re.fullmatch(LEGAL_NAME, value), "the premise: this is no variable name"

    code, seen = merged(_argv_with(flag, value))

    assert code == _not_a_name(flag)
    assert _repeated(seen, value) == []


@pytest.mark.parametrize("flag", ["--username-env", "--password-env"])
def test_a_value_beginning_with_a_hyphen_is_refused_either_way_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """After a space argparse reads `-my-pass` as another flag; after `=` it is the value."""
    _exported(monkeypatch)
    value = "-my-pass"

    code, seen = merged(_argv_with(flag, value))

    assert code == 2
    assert f"usher set-credentials: error: argument {flag}: expected one argument" in (
        seen.splitlines()
    )
    assert _repeated(seen, value) == []

    code, seen = merged(_argv_with(flag, value, joined=True))

    assert code == _not_a_name(flag)
    assert _repeated(seen, value) == []


@pytest.mark.parametrize(
    ("flag", "value"), [("--username-env", USERNAME), ("--password-env", PASSWORD)]
)
def test_a_password_that_is_a_legal_name_is_refused_as_unset_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch, flag: str, value: str
) -> None:
    """The half the grammar check cannot see, and the reason no refusal names a variable.

    `hunter2` is a legal name, so the command can only read it as one, find nothing
    behind it and say so -- without the name, since the name may be the password.
    """
    _exported(monkeypatch)
    monkeypatch.delenv(value, raising=False)
    assert re.fullmatch(LEGAL_NAME, value), "the premise: a legal variable name"
    assert value not in os.environ, "the premise: and nothing is exported under it"

    code, seen = merged(_argv_with(flag, value))

    assert code == _unset(flag)
    assert _repeated(seen, value) == []


@pytest.mark.parametrize("flag", ["--username-env", "--password-env"])
def test_an_empty_variable_is_refused_like_an_unset_one(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """`export CASE_SOURCE_PASSWORD=` is set and empty, and would store an empty login."""
    _exported(monkeypatch)
    variable = VALID[VALID.index(flag) + 1]
    monkeypatch.setenv(variable, "")
    assert os.environ[variable] == "", "the premise: set, and empty"

    code, seen = merged(VALID)

    assert code == _unset(flag)
    assert _repeated(seen) == []


def test_it_has_its_own_dispatch_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_dispatch`'s `else` is `serve`, so a command without an arm of its own starts the server.

    The whole shape is three names. The values stay in the environment until
    `_set_credentials` reads them, so no frame between argparse and the store holds one.
    """
    _exported(monkeypatch)

    calls = dispatched(monkeypatch, arm="_set_credentials", argv=VALID)

    assert calls == [{"source": SOURCE, "username_env": USERNAME_VAR, "password_env": PASSWORD_VAR}]
    assert USERNAME_VALUE not in repr(calls)
    assert PASSWORD_VALUE not in repr(calls)


def test_the_namespace_holds_three_names_and_the_help_hides_the_tripwires() -> None:
    """`dest=argparse.SUPPRESS` and `help=argparse.SUPPRESS` on both tripwires, each pinned.

    A tripwire with a `dest` puts `password=None` in every namespace, and one with help
    text advertises the flag it exists to refuse.
    """
    args = parse_args(VALID)

    assert vars(args) == {
        "command": "set-credentials",
        "traceback": False,
        "source": SOURCE,
        "username_env": USERNAME_VAR,
        "password_env": PASSWORD_VAR,
    }
    shown = _set_credentials_help()
    assert "--username-env" in shown
    assert "--password-env" in shown
    assert not re.search(r"--(username|password)(?!-env)", shown)


def test_the_help_says_source_takes_an_id_or_a_name() -> None:
    """Both spellings, in the metavar and in the help, where an operator with duplicates looks."""
    shown = " ".join(_set_credentials_help().split())

    assert "--source NAME_OR_ID" in shown
    assert (
        "the source's id, as GET /admin/sources lists it, or its name exactly as registered"
    ) in shown


def _set_credentials_help() -> str:
    """`usher set-credentials --help`, read the way an operator reads it."""
    code, seen = merged(["set-credentials", "--help"])
    assert code == 0, seen
    return seen
