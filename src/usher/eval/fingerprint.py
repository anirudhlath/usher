"""What makes two eval runs comparable, and what merely explains them.

**The single most important element for CI**, because without it eval CI is
disabled within a fortnight: if the catalog drifts -- a bootstrap re-run, an
enrichment crawl landing, an `m09e`-style embedding rebuild -- scores move for
reasons unrelated to the diff and the PR gets blamed.

**Two halves, and the split is a correction to the design spec.** §8.2 lists
the git sha among the fingerprint fields and then says a run whose fingerprint
differs from the baseline's is not comparable. Those cannot both hold: every
commit changes the sha, so every run would be incomparable with every other
and `baseline-invalid` would be the only reachable verdict.

- `inputs` -- the catalog facts the surface actually reads. **Digested, and
  compared.** For suggest that is the sampling frame, because the frame is
  exactly what the measurement is drawn from.
- `provenance` -- git sha, library versions, host. **Recorded, never
  compared.** This is what a later reader needs to attribute a move to a
  library upgrade rather than to the system under test.

**The seed is an input and not provenance**, against the obvious reading of
that list, and `for_suggest` below puts it there: the seed selects which 750
names were drawn, so two runs at different seeds measured different case sets
and are not two measurements of one system. A fact belongs in `provenance`
only when a run that differs in it measured *the same thing*.
"""

import hashlib
import json
import platform
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from usher.eval.goldens.suggest import GATE_SEED, Frame


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One run's provenance, in the two halves that behave differently.

    Frozen, and **not hashable** -- both fields are `Mapping`s, and the
    generated `__hash__` raises `TypeError` on the dict underneath. Stated
    because "frozen therefore hashable" is false here and this repository has
    been bitten by it; `digest` is the identity anything needs.
    """

    inputs: Mapping[str, Any]
    provenance: Mapping[str, Any]

    @property
    def digest(self) -> str:
        """sha256 over `inputs` alone, canonically serialised.

        `sort_keys=True` because two captures that built the mapping in a
        different order describe the same catalog, and a digest over
        `str(dict)` would call them different. It sorts **recursively**, which
        is what `pools` needs -- a nested mapping assembled one band at a time
        is the shape a top-level-only sort gets wrong.

        `json` rather than `repr` or `hash`: `hash()` is salted per process
        (`PYTHONHASHSEED`) and a baseline is written by one run of the harness
        and compared by the next, so a salted digest agrees with itself all
        day and with nothing else.

        A value `json` cannot serialise raises `TypeError` here rather than
        digesting to something plausible -- which includes a nested
        `mappingproxy` (measured 2026-08-19: *Object of type mappingproxy is
        not JSON serializable*), so a caller holding one unwraps it on the way
        in, as `for_suggest` does with `Frame.pools`.
        """
        canonical = json.dumps(dict(self.inputs), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def git_sha() -> str:
    """The working tree's commit, or `"unknown"`.

    Never raises. A run in a tarball with no `.git` is a legitimate run whose
    provenance is simply thinner, and a harness that dies on a missing git is
    a harness that cannot be used in a container.

    The `except` is what holds that promise, over the two families that reach
    it: `OSError` for an image with no git in it, `SubprocessError` for the
    `timeout` expiring. Both are pinned, one parameter each.

    **`check=False` is defence in depth and not load-bearing, which is the
    opposite of what this docstring said until it was measured.**
    `subprocess.CalledProcessError` **subclasses `SubprocessError`**, so
    `check=True` raises on a directory that is not a repository, the same
    `except` catches it, and the caller is handed the same `"unknown"` --
    planted, it survives all 28 cases in `test_eval_fingerprint.py`, which is
    an equivalent mutant reported rather than closed. What `check=False` buys
    is that the ordinary thin case (git answers 128, and it is the common one)
    stays on the return path rather than travelling as an exception, so the
    `except` is left holding only the two events nobody can return from.

    **What it answers on failure is `"unknown"` and never what git said.**
    git's own message on a missing repository names the directory it searched
    (*"not a git repository (or any of the parent directories)"*), and this
    string is written into a report, a baseline file and a CI log.
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
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


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
        inputs={
            "surface": "suggest",
            "seed": seed,
            "case_count": case_count,
            "shared_lower_names": frame.shared_lower_names,
            # `dict(...)` is not cosmetic: `Frame.pools` is a `Mapping` and the
            # gate's own constant is a `MappingProxyType`, which `json.dumps`
            # refuses.
            "pools": dict(frame.pools),
        },
        provenance={
            "git_sha": git_sha(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ranx": ir.library_version(),
        },
    )
