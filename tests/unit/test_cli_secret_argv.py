"""No parser refusal quotes an argument of a command line naming a secret command.

The secret commands are `rotate-secret` and `set-credentials`; every other keeps argparse's words.
"""

import pytest

from tests.unit.commands import merged

#: What an operator types where it does not belong, spelled to be found in any output.
CANARY = "argv-canary-hunter2"

#: Spelled out rather than imported, so these cases are about the sentence an
#: operator reads and not about the constant `cli.py` prints.
NOT_SHOWN = (
    "an argument was refused and is not shown (on a command line naming rotate-secret or "
    "set-credentials, a refused value is most likely a key or a password) -- a secret is "
    "named only with --new-key-env, --username-env or --password-env, which take the NAMES "
    "of exported environment variables"
)

SET_CREDENTIALS = [
    "set-credentials",
    "--source",
    "a source",
    "--username-env",
    "CASE_USERNAME",
    "--password-env",
    "CASE_PASSWORD",
]
ROTATE_SECRET = ["rotate-secret", "--new-key-env", "USHER_NEW_SECRET_KEY"]


@pytest.mark.parametrize(
    ("argv", "prog"),
    [
        (["--password", CANARY, *SET_CREDENTIALS], "usher"),
        ([f"--traceback={CANARY}", *SET_CREDENTIALS], "usher"),
        ([*SET_CREDENTIALS, f"-h={CANARY}"], "usher set-credentials"),
        ([*SET_CREDENTIALS, f"-h-{CANARY}"], "usher set-credentials"),
        (["--new-key", CANARY, *ROTATE_SECRET], "usher"),
        ([f"--traceback={CANARY}", *ROTATE_SECRET], "usher"),
        ([*ROTATE_SECRET, f"-h={CANARY}"], "usher rotate-secret"),
        ([*ROTATE_SECRET, f"-h-{CANARY}"], "usher rotate-secret"),
        (["backup", "--nonsense", CANARY, "set-credentials"], "usher"),
    ],
    ids=[
        "set-credentials, a value before the subcommand",
        "set-credentials, a value on a top-level flag",
        "set-credentials, -h=",
        "set-credentials, -h-",
        "rotate-secret, a value before the subcommand",
        "rotate-secret, a value on a top-level flag",
        "rotate-secret, -h=",
        "rotate-secret, -h-",
        "a secret command named after another",
    ],
)
def test_a_refusal_on_a_secret_commands_line_quotes_no_argument(argv: list[str], prog: str) -> None:
    """Every path that reached argparse's own quoting, the top-level parser's and a subparser's.

    argparse reads a value before the subcommand as the subcommand, and quotes it as
    an invalid choice. `-h-<value>` is why the scrub cannot look for a whole argv token
    in the message: argparse quotes the suffix `-<value>`, which is not one.
    """
    assert any(CANARY in token for token in argv), "the premise: the value is in argv"

    code, seen = merged(argv)

    assert CANARY not in seen
    assert code == 2
    assert f"{prog}: error: {NOT_SHOWN}" in seen.splitlines()


@pytest.mark.parametrize(
    ("argv", "line"),
    [
        (["serv"], "usher: error: argument command: invalid choice: 'serv' (choose from "),
        (
            ["--password", CANARY, "backup"],
            f"usher: error: argument command: invalid choice: '{CANARY}' (choose from ",
        ),
        (
            ["backup", f"-h={CANARY}"],
            f"usher backup: error: argument -h/--help: ignored explicit argument '{CANARY}'",
        ),
    ],
    ids=["a misspelt command", "a value before another command", "-h= on another command"],
)
def test_every_other_command_keeps_argparses_words(argv: list[str], line: str) -> None:
    """The scrub is the secret commands' alone, so a scrub applied everywhere fails here.

    Each line is a prefix only where argparse goes on to list every subcommand.
    """
    code, seen = merged(argv)

    assert code == 2
    assert [shown for shown in seen.splitlines() if shown.startswith(line)] != [], seen


@pytest.mark.parametrize(
    ("argv", "line"),
    [
        (
            ["set-credentials", "--source", "a source"],
            "usher set-credentials: error: the following arguments are required: "
            "--username-env, --password-env",
        ),
        (
            ["rotate-secret"],
            "usher rotate-secret: error: the following arguments are required: --new-key-env",
        ),
        (
            ["rotate-secret", "--new-key-env"],
            "usher rotate-secret: error: argument --new-key-env: expected one argument",
        ),
    ],
    ids=["required, set-credentials", "required, rotate-secret", "expected one argument"],
)
def test_a_refusal_made_of_the_parsers_own_literals_is_kept(argv: list[str], line: str) -> None:
    """These name only flags, so a scrub that replaced them would cost the operator for nothing."""
    code, seen = merged(argv)

    assert code == 2
    assert line in seen.splitlines()
