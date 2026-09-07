"""`CONTRIBUTING.md`'s gate is `CLAUDE.md`'s gate.

The two files are written for different readers -- one a maintainer, one a
stranger -- and the list of commands is the one thing that must not differ
between them.
"""

import pathlib
import re

_ROOT = pathlib.Path(__file__).parents[2]


def _gate_commands(document: str, heading: str) -> list[str]:
    """The `bash` fence under `heading`, one command per line.

    **Trailing `#` comments are stripped, and that is load-bearing.**
    `CLAUDE.md`'s `lint-imports` line carries the architecture-contract count
    as a comment, and comparing raw lines would make this a contract-count
    detector that goes red on an unrelated new contract. The count lives in
    exactly one place on purpose -- it has been wrong there before -- and a
    second copy in `CONTRIBUTING.md` would be a second chance to be wrong.
    """
    after = document.split(heading, 1)[1]
    fence = re.search(r"```bash\n(.*?)```", after, re.DOTALL)
    assert fence is not None, f"no bash fence follows {heading!r}"
    lines = (line.split("#", 1)[0].strip() for line in fence.group(1).splitlines())
    return [line for line in lines if line]


def test_the_contributing_gate_is_the_gate_claude_md_states() -> None:
    """Compared as ordered lists: the order is the order they must be run in.

    The control is the length assertion. A fence parser that found nothing
    compares two empty lists and passes.
    """
    claude = _gate_commands((_ROOT / "CLAUDE.md").read_text(), "### The gate")
    contributing = _gate_commands((_ROOT / "CONTRIBUTING.md").read_text(), "## The gate")

    # Six, not five: the block leads with `uv sync --extra eval`, without which
    # five eval modules abort at collection and `pytest` exits having run
    # nothing. M10's R3 plan text says five; it predates that line.
    assert len(claude) == 6, (
        f"the fence parser found {len(claude)} commands in CLAUDE.md, so the "
        f"comparison below would be vacuous: {claude}"
    )

    assert contributing == claude
