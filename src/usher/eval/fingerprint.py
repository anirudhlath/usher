"""What makes two eval runs comparable, and what merely explains them."""

import hashlib
import json
import platform
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from usher.eval.errors import EvalRefused
from usher.eval.goldens.suggest import (
    GATE_CASES,
    GATE_POOLS,
    GATE_SEED,
    GATE_SHARED_LOWER_NAMES,
    Frame,
)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One run's provenance, in the two halves that behave differently.

    Frozen, and **not hashable** -- both fields are `Mapping`s, and the
    generated `__hash__` raises `TypeError`. Still true now that the field
    underneath is a `mappingproxy` rather than a `dict`: a proxy delegates
    `__hash__` to the mapping it wraps, which is `None`, so the message even
    keeps naming the dict (measured 2026-08-19: *unhashable type: 'dict'*).
    Stated because "frozen therefore hashable" is false here and this
    repository has been bitten by it; `digest` is the identity anything needs.

    **Both mappings are copied, then wrapped**, which is `CursorSpec`'s shape
    (`api/cursor.py`) for `CursorSpec`'s reason, and it matters more here. A
    cursor's digest is wrong for one request; this digest is written to
    `eval.runs`, committed to `docs/evals/ledger.jsonl` and transcribed into
    `bars.toml`, and the ledger reads it at two moments with a
    `session.commit()` between them -- so "two reads agree" was resting on
    nobody having touched the caller's dict in between. The copy stops the
    caller mutating the mapping it handed over; the proxy stops this instance
    mutating its own.
    """

    inputs: Mapping[str, Any]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    @property
    def digest(self) -> str:
        """Sha256 over `inputs` alone, canonically serialised."""
        canonical = json.dumps(dict(self.inputs), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _tree_is_clean() -> bool | None:
    """Whether the working tree still matches `HEAD`.

    or `None` for "git would not say", which is a third answer and not a quiet "yes".

    A second bounded call rather than `git describe --dirty`, which with any
    tag in the repository answers `v1.0-3-gabc1234-dirty`: not a sha a reader
    can hand to `git show`, in the one field they will want to.

    `--untracked-files=no` is a deliberate floor rather than a proof. An
    untracked file *can* be code that ran -- a new module nobody has `git
    add`-ed yet -- and this cannot see it; what it buys is that a stray
    `.log`, a `__pycache__` or an editor swapfile does not mark every run in
    the repository as dirty, which is how a marker stops being read.
    """
    try:
        # S607: `git` rather than an absolute path, for `git_sha`'s reason.
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return not result.stdout.strip()


def git_sha() -> str:
    """The commit the code that ran came from, marked when the tree has moved past it.

    or one of three named `"unknown:…"` answers.
    """
    try:
        # S607: `git` rather than an absolute path, so it is found the way an
        # operator's own shell finds it. Nothing suppresses S603 beside it --
        # the argv is a list literal with no external input, so ruff does not
        # raise S603 here at all and a directive for it is `RUF100`.
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return "unknown:no-git"
    except subprocess.SubprocessError:
        return "unknown:git-timeout"
    if result.returncode != 0:
        return "unknown:not-a-repository"
    sha = result.stdout.strip()
    clean = _tree_is_clean()
    if clean is None:
        return f"{sha}-worktree-unknown"
    return sha if clean else f"{sha}-dirty"


def _suggest_inputs(frame: Frame, *, seed: int, case_count: int) -> dict[str, Any]:
    """The suggest surface's compared half, in one place.

    A function rather than a literal inside `for_suggest`, because
    `GATE_DIGEST` below is computed from these same five keys and a second
    spelling of them is a second thing to keep in step -- with the drift
    landing on the constant that decides comparability.
    """
    return {
        "surface": "suggest",
        "seed": seed,
        "case_count": case_count,
        "shared_lower_names": frame.shared_lower_names,
        # `dict(...)` is not cosmetic: `Frame.pools` is a `Mapping` and the
        # gate's own constant is a `MappingProxyType`, which `json.dumps`
        # refuses. `Fingerprint.__post_init__` does not cover this -- it wraps
        # the top level, and this proxy would sit one level down inside it.
        "pools": dict(frame.pools),
    }


def for_suggest(frame: Frame, *, seed: int = GATE_SEED, case_count: int) -> Fingerprint:
    """The suggest surface's fingerprint.

    **`inputs` is the sampling frame and nothing else**, because the frame is
    what a suggest measurement is drawn from. That keeps an embedding
    backfill -- which changes `title_embeddings` and touches nothing suggest
    reads -- from invalidating a suggest baseline it has no bearing on.

    `case_count` rides in `inputs` too: 2,993 against 2,964 is a different
    measurement over the same frame, and that difference has happened once
    already (the transposition arm).

    Nothing here reads `Settings` or the environment, and `provenance` names
    the machine only through `platform.platform()`, which carries neither the
    hostname nor the login name (measured on this host 2026-08-19:
    `Linux-7.1.3-2-cachyos-x86_64-with-glibc2.43`). A fingerprint is published
    -- into a report, a baseline file and a CI log -- so a field added here is
    a field disclosed.
    """
    from usher.eval.metrics import ir  # local: keeps the ranx import lazy

    return Fingerprint(
        inputs=_suggest_inputs(frame, seed=seed, case_count=case_count),
        provenance={
            "git_sha": git_sha(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ranx": ir.library_version(),
        },
    )


# : The gate's own `inputs`, and the digest of them.
_GATE_INPUTS: Mapping[str, Any] = MappingProxyType(
    _suggest_inputs(
        Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=dict(GATE_POOLS)),
        seed=GATE_SEED,
        case_count=GATE_CASES,
    )
)
GATE_DIGEST = Fingerprint(inputs=_GATE_INPUTS, provenance={}).digest

#: Reported in place of a value for a key one side does not carry at all, so
#: "the pools were never read" is legible in the refusal as something other
#: than a `None` somebody wrote down.
_ABSENT = "<absent>"


def check_digest(observed: Fingerprint) -> Fingerprint:
    """The gate's compared inputs, reproduced or refused."""
    if observed.digest == GATE_DIGEST:
        return observed
    inputs = dict(observed.inputs)
    names = [*_GATE_INPUTS, *(name for name in inputs if name not in _GATE_INPUTS)]
    drift = {
        name: (_GATE_INPUTS.get(name, _ABSENT), inputs.get(name, _ABSENT))
        for name in names
        if _GATE_INPUTS.get(name, _ABSENT) != inputs.get(name, _ABSENT)
    }
    if not drift:
        # Reachable, and not a paranoid branch: `2993.0 == 2993` in Python and
        # `2993.0 != 2993` in JSON, so a `case_count` that arrived through a
        # division or as a `NUMERIC` compares equal to the gate's key by key
        # and digests differently. Refusing with an empty list of what moved
        # would be a refusal naming nothing.
        raise EvalRefused(
            f"the run's inputs digest is not the gate's -- {observed.digest} against "
            f"{GATE_DIGEST}, with every input comparing equal, so the two mappings "
            f"differ in a way only the serialisation can see (a bool against an int, "
            f"a float against an int). This run is not comparable with the baseline."
        )
    raise EvalRefused(
        "the run's inputs are not the gate's -- "
        + ", ".join(
            f"{name}: expected {want!r}, observed {got!r}" for name, (want, got) in drift.items()
        )
        + ". The baseline was measured over a different input, so comparing the two "
        "numbers would be comparing two measurements."
    )
