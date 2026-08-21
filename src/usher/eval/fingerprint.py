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

**Two modules own the comparison and neither owns all of it**, so do not read
this one as owning it whole. `goldens/suggest.py::check_frame` owns the
*catalog* half -- `shared_lower_names` and the five pools -- and answers "is
this the population the gate drew from?". `check_digest` below owns the whole
of `inputs`, which is that half plus `surface`, `seed` and `case_count`: the
three `check_frame` structurally cannot see, and `usher eval suggest --full
--seed 12345` is a supported invocation that moves one of them straight past
it into a ledger row carrying a `pass`. A `--full` run owes both calls.

**The instrument is a third category, and it is ruled on here so E3 does not
have to guess.** A judge model id, its prompt hash and its temperature are
neither the system under test nor the population sampled from -- they are the
ruler. They go in **`inputs`**: "the system did not change" argues provenance,
but the rule above asks whether a run that differs in it measured the same
thing, and a re-ruled measurement is not one. The design spec's insistence
that a judge is untrusted until calibrated says the same, one step earlier --
an uncalibrated swap of the instrument cannot be assumed to preserve the
scale, so a run across it is not comparable and must say so rather than be
compared and blamed.

**E2's first pairing, recorded now because it is the one that looks like a
counterexample.** An embedding *model name* is **provenance** and embedding
*coverage* -- how many titles carry a vector at all -- is an **input**: the
model is the system under test, so digesting its name would make every
deliberate swap `baseline-invalid` and hide the very move the eval exists to
measure, while coverage is a property of the population the measurement is
drawn from. `m09e` widening `halfvec(384)` to `halfvec(1024)` reads like a
model change that *must* invalidate a baseline, and it does -- but through the
input rather than through the provenance field, because it deleted every
embedding row, and that consequence is exactly what the coverage count
reports.
"""

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
        in, as `_suggest_inputs` does with `Frame.pools`. `__post_init__`'s
        wrap does not cover that: it wraps the **top level**, and `dict(...)`
        here unwraps that same top level again, so a proxy nested one deep is
        reached by neither.
        """
        canonical = json.dumps(dict(self.inputs), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _tree_is_clean() -> bool | None:
    """Whether the working tree still matches `HEAD` -- or `None` for "git
    would not say", which is a third answer and not a quiet "yes".

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
    """The commit the code that ran came from, marked when the tree has moved
    past it -- or one of three named `"unknown:…"` answers.

    Never raises, and never answers anything falsy. A run in a tarball with no
    `.git` is a legitimate run whose provenance is simply thinner, and a
    harness that dies on a missing git is a harness that cannot be used in a
    container.

    **`git rev-parse HEAD` reads `.git/HEAD` and consults neither the index
    nor the worktree**, so on its own it records a *clean* sha for a dirty
    tree -- naming code that did not run. That is not an edge case here: the
    stated use for this harness is "did my diff move the number", `--full`
    writes `docs/evals/ledger.jsonl`, and Task 14 takes its baseline that way,
    so the ordinary workflow is the one that would have recorded it. Hence the
    `-dirty` suffix, and hence `_tree_is_clean`'s third answer: when the tree
    check itself fails while `rev-parse` succeeded, the sha carries
    `-worktree-unknown` rather than silently reading as clean. Appending
    nothing is the claim nobody made; appending `-dirty` is a claim about
    evidence that was never obtained, and collapsing an unknown into a known
    is the very defect the three refusals below split apart.

    **Three refusals rather than one `"unknown"`**, because they are three
    distinguishable events and only the last is the legitimate thin run the
    paragraph above argues for:

    * `"unknown:no-git"` -- `OSError`, an image with no git in it;
    * `"unknown:git-timeout"` -- `SubprocessError`, the `timeout` expiring;
    * `"unknown:not-a-repository"` -- returncode 128, the tarball.

    `None` is not among the options: `test_eval_fingerprint.py`'s
    `all(fingerprint.provenance.values())` refuses a provenance field that is
    empty, because an empty field reads in a report as a fact nobody had.

    **`check=False` is load-bearing again, which reverses what this docstring
    said before the three refusals existed.** `subprocess.CalledProcessError`
    subclasses `SubprocessError`, so under `check=True` a directory that is
    not a repository raises, the `SubprocessError` arm catches it, and the
    caller is handed `"unknown:git-timeout"` for a git that answered
    immediately. Measured 2026-08-19: planted, it now fails two cases --
    `test_a_directory_that_is_not_a_repository_names_that_event_...` and
    `test_the_three_events_that_answer_no_sha_answer_three_different_things`.
    With one `"unknown"` for all three events it survived every case in the
    file and was reported as an equivalent mutant, which it was; splitting the
    answers is what made it observable.

    **What it answers on failure never echoes what git said.** git's own
    message on a missing repository names the directory it searched (*"not a
    git repository (or any of the parent directories)"*), and this string is
    written into a report, a baseline file and a CI log.
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


#: The gate's own `inputs`, and the digest of them. **Computed from
#: `GATE_SEED`, `GATE_CASES`, `GATE_SHARED_LOWER_NAMES` and `GATE_POOLS`
#: rather than transcribed**, so it cannot drift from the four constants it is
#: about; the literal value is pinned by a case, which is the other claim and
#: needs its own.
#:
#: Cheap enough to compute at import: five keys, one `json.dumps`, one
#: sha256, and no git, no `ranx` and no catalog -- `for_suggest` is
#: deliberately not used here, because its `provenance` half would drag a
#: subprocess and the optional extra into importing this module.
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
    """The gate's compared inputs, reproduced or refused.

    **The half `check_frame` cannot see.** That function checks six catalog
    numbers; this checks the whole of `inputs`, so it is also the only thing
    between `usher eval suggest --full --seed 12345` and a ledger row that
    reads `pass` against bars derived from `GATE_SEED`. A different seed draws
    a different 750 names, which is a different measurement -- it is not a
    worse one, and it is not the diff's fault either, so this refuses the way
    `check_frame` refuses (`EvalRefused` -> `baseline-invalid`, exit 0) rather
    than failing.

    **Owed by whichever task builds the runner**, which is where the call
    goes: beside `check_frame`, on the `--full` path only. A quick run samples
    the case list, so its `case_count` is right to differ and there is no
    baseline for it to be compared against.

    **The refusal names the input that moved**, for `check_frame`'s reason --
    an operator meets this in CI, and "the digest differs" tells them nothing
    they can act on when five keys could have moved.

    Two of those five are `check_frame`'s own, and on the runner's path it
    answers first and answers better: it reports the drift *per band* where
    this reports two five-entry mappings. They are kept here anyway because
    this function is also reachable on its own and a check that silently
    ignores two of the five inputs it claims to compare is the defect this
    module exists about. The three only this can see are `surface`, `seed` and
    `case_count`.
    """
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
