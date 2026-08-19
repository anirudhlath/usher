"""The fingerprint's two halves, and why conflating them breaks CI.

`inputs` decides comparability. `provenance` decides attribution. A field in
the wrong half is not a cosmetic error: git sha in `inputs` makes every
commit incomparable with every other, so `baseline-invalid` becomes the only
reachable verdict and the eval job gets disabled within a fortnight.

**Every case here was written against a named wrong implementation**, because
the last three modules in this package each shipped a suite that passed
against a broken one. The list, and the case that kills each:

* a digest computed over `provenance` as well as `inputs` -- the spec's own
  bug restored -- dies on `..._a_changed_git_sha_does_not_change_the_digest`,
  and its half-fix (special-case the sha, digest the rest) dies on
  `..._no_provenance_field_of_any_kind_reaches_the_digest`;
* a digest that ignores one input field, so a real catalog change reads as
  comparable, dies on `..._any_one_input_field_moved_on_its_own_...`, one
  parameter per field -- the plan's single-field positive control cannot see
  it;
* a digest over `str(dict)`, or one that sorts only the top level, dies on
  the two key-order cases (flat, and nested inside `pools`);
* a digest over the *values* alone dies on
  `..._reads_the_keys_and_not_only_the_values`;
* a digest that treats an absent field as equal to a present one dies on
  `..._absent_is_not_the_same_catalog_as_one_that_is_present`;
* a digest built on anything `PYTHONHASHSEED` salts -- `hash()`, a `set`
  iterated -- dies on the two-interpreter case, which is the only one that
  can see it, because a baseline is written by one process and compared by
  another;
* a provenance field silently becoming an input (or the reverse) dies on
  `..._compares_the_frame_and_records_the_rest`, which pins both halves as
  exact sets rather than as memberships;
* a digest that is not a pure function of `inputs` -- object identity mixed
  in, a clock, a nonce -- dies on `..._stable_across_two_captures_...`;
* a record that *discards* provenance once it has decided not to compare it
  dies on `..._provenance_still_reaches_the_record`, and one that records a
  plausible constant in place of what the run actually imported dies on
  `..._read_from_the_run_and_not_written_down`.

Every one of the 28 cases below has been watched failing against at least one
of those, planted out of tree at `/var/tmp/e1-t5/` (2026-08-19): 20 wrong
implementations, 20 killed, plus two controls that survive all five gate
steps. The one mutation that survives is `check=True` in `git_sha`, and it is
equivalent rather than uncovered -- see that function's docstring.
"""

import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from usher.eval import fingerprint as fingerprint_module
from usher.eval.fingerprint import Fingerprint, for_suggest, git_sha
from usher.eval.goldens.suggest import (
    GATE_CASES,
    GATE_POOLS,
    GATE_SEED,
    GATE_SHARED_LOWER_NAMES,
    Frame,
)
from usher.eval.metrics import ir

# tests/unit/test_eval_fingerprint.py -> tests/unit -> tests -> repo root. The
# same derivation `test_eval_contract.py` and `test_eval_runner.py` make, and
# for the same reason: the two `git_sha` cases below are about which working
# tree the call is standing in, so the tree has to be named rather than
# assumed to be pytest's cwd.
_ROOT = Path(__file__).resolve().parents[2]

#: The child interpreter's `inputs`, and the parent's. One binding rather than
#: two literals: the case asserts the two processes agree, so a probe that had
#: drifted from the mapping beside it would be comparing two catalogs and
#: calling the difference a hash seed.
_PROBE_INPUTS: dict[str, object] = {
    "surface": "suggest",
    "seed": 20260803,
    "shared_lower_names": 81_054,
    "pools": {"5-7": 2532, "2-4": 432},
}

#: Run in a second interpreter under two `PYTHONHASHSEED` values. `repr` of a
#: dict is insertion-ordered valid Python, so the child builds the *same*
#: mapping in the same written order -- the difference between the two runs is
#: the seed and nothing else.
_PROBE = (
    "from usher.eval.fingerprint import Fingerprint;"
    f"print(Fingerprint(inputs={_PROBE_INPUTS!r}, provenance={{}}).digest)"
)

#: The gate's own frame, which is what `for_suggest` is handed in a real run.
_GATE_FRAME = Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=dict(GATE_POOLS))


def _fingerprint(
    *,
    inputs: dict[str, object] | None = None,
    provenance: dict[str, object] | None = None,
) -> Fingerprint:
    """One capture of a catalog, with the named fields nudged.

    A helper rather than literals at each call site because every claim here
    is *one field out*: a fixture that moves two while asserting about one is
    the fixture that lets a digest ignoring a field survive.
    """
    facts: dict[str, object] = {
        "titles": 1_271_138,
        "shared_lower_names": 81_054,
        "pools": {"2-4": 432},
    }
    recorded: dict[str, object] = {"git_sha": "abc1234", "seed": 20260803, "ranx": "0.3.21"}
    facts.update(inputs or {})
    recorded.update(provenance or {})
    return Fingerprint(inputs=facts, provenance=recorded)


def _fields_carrying(fingerprint: Fingerprint, needle: str) -> list[str]:
    """Which halves and keys hold `needle` anywhere in their serialised value.

    It names the **field** and never the value, because this is the sentence a
    CI log gets: a case that proves a credential leaked by printing the
    credential has performed the leak it exists to catch.
    """
    found = []
    for half, mapping in (("inputs", fingerprint.inputs), ("provenance", fingerprint.provenance)):
        for key, value in mapping.items():
            if needle in json.dumps(value, sort_keys=True, default=str):
                found.append(f"{half}.{key}")
    return found


def test_the_digest_is_stable_across_two_captures_of_the_same_catalog() -> None:
    """Two reads of one unchanged catalog have to answer one digest, or the
    baseline is invalid the moment it is written."""
    assert _fingerprint().digest == _fingerprint().digest


def test_the_digest_ignores_key_order() -> None:
    """Two captures that built the mapping in a different order describe the
    same catalog. A digest over `str(dict)` would disagree."""
    one = Fingerprint(inputs={"a": 1, "b": 2}, provenance={})
    two = Fingerprint(inputs={"b": 2, "a": 1}, provenance={})
    assert one.digest == two.digest


def test_the_digest_ignores_the_order_a_nested_mapping_was_built_in() -> None:
    """The case above is satisfied by sorting the *top level* only --
    `json.dumps(dict(sorted(inputs.items())))` -- and `pools` is a nested
    mapping built one band at a time by whichever query answered first. So
    the two captures below differ in nothing a catalog can see, and a
    top-level-only sort calls them two different catalogs.
    """
    one = Fingerprint(inputs={"pools": {"2-4": 432, "5-7": 2532}}, provenance={})
    two = Fingerprint(inputs={"pools": {"5-7": 2532, "2-4": 432}}, provenance={})
    assert one.digest == two.digest


def test_the_digest_reads_the_keys_and_not_only_the_values() -> None:
    """A digest over `sorted(inputs.values())` passes every ordering case
    above and every positive control below, because both are satisfied by a
    function merely *sensitive* to the same numbers. The two captures here
    carry the identical multiset of values under swapped keys: 432 titles in
    a catalog of 81,054 shared names is not 81,054 titles in a catalog of 432.
    """
    one = Fingerprint(inputs={"titles": 432, "shared_lower_names": 81_054}, provenance={})
    two = Fingerprint(inputs={"titles": 81_054, "shared_lower_names": 432}, provenance={})
    assert one.digest != two.digest


def test_a_changed_catalog_input_changes_the_digest() -> None:
    """The positive control. Without it every test here passes for a digest
    that returns a constant."""
    assert _fingerprint(inputs={"titles": 1_271_570}).digest != _fingerprint().digest


@pytest.mark.parametrize(
    "moved",
    [
        {"titles": 1_271_570},
        {"shared_lower_names": 81_055},
        {"pools": {"2-4": 433}},
    ],
    ids=["titles", "shared_lower_names", "pools"],
)
def test_any_one_input_field_moved_on_its_own_changes_the_digest(
    moved: dict[str, object],
) -> None:
    """**The positive control above moves one field, so it cannot see a digest
    that reads only that field.** An implementation digesting `titles` alone
    -- or one that skips `pools` because a nested mapping is awkward to
    serialise -- passes it and reports a bootstrap re-run, an enrichment crawl
    and a re-sampled frame as all comparable with the baseline.

    The two integers and the nested mapping are the three shapes an input
    carries, and each is a parameter rather than an arm of one case so the
    verdict names which field went unread.
    """
    assert _fingerprint(inputs=moved).digest != _fingerprint().digest


@pytest.mark.parametrize("present", [432, None], ids=["a-count", "an-explicit-none"])
def test_a_field_that_is_absent_is_not_the_same_catalog_as_one_that_is_present(
    present: object,
) -> None:
    """A field that *did not appear at all* is a different measurement from
    one that did, and the difference is the one a comparison is most likely to
    get wrong: a digest built by reading known keys out of the mapping
    (`inputs.get(name)`), or one that drops `None`s on the way in, calls a
    capture that never read the pools identical to a capture that read them.

    The `None` parameter is the half that is not obvious -- a frame whose pool
    query failed answers `None`, and "the pools are unknown" must not digest
    the same as "there is no pools field".
    """
    absent = Fingerprint(inputs={"titles": 1_271_138}, provenance={})
    carried = Fingerprint(inputs={"titles": 1_271_138, "pools": present}, provenance={})
    assert absent.digest != carried.digest


def test_a_changed_git_sha_does_not_change_the_digest() -> None:
    """**The whole reason this class has two fields.** Every commit changes
    the sha. Digested, that makes each run incomparable with the previous
    one, `baseline-invalid` the only reachable verdict, and the eval job
    noise that someone turns off."""
    assert _fingerprint(provenance={"git_sha": "deadbee"}).digest == _fingerprint().digest


@pytest.mark.parametrize(
    "moved",
    [
        {"git_sha": "deadbee"},
        {"seed": 20260804},
        {"ranx": "0.4.0"},
        {"host": "some-other-box"},
    ],
    ids=["git_sha", "seed", "ranx", "a-field-nobody-recorded-before"],
)
def test_no_provenance_field_of_any_kind_reaches_the_digest(moved: dict[str, object]) -> None:
    """The case above is satisfied by a digest that special-cases the sha and
    keeps the rest -- which is the repair somebody reaches for on being told
    the sha is the problem, and it re-creates the bug one field over: a `ranx`
    upgrade would then invalidate every baseline in the repository.

    The fourth parameter adds a key nothing recorded before, because the half
    a fixed key list cannot see is a *new* provenance field arriving later.
    """
    assert _fingerprint(provenance=moved).digest == _fingerprint().digest


def test_provenance_still_reaches_the_record() -> None:
    """Not compared is not the same as not kept. A metric that moved because
    a library was upgraded is diagnosable only if the version was written
    down."""
    assert _fingerprint().provenance["ranx"] == "0.3.21"


def test_the_recorded_provenance_is_read_from_the_run_and_not_written_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case above pins that `provenance` survives construction; it cannot
    see a field whose value was *invented*, and a recorded constant is worse
    than an absent record because a reader believes it.

    `"ranx": "0.3.21"` written as a literal is the shape: it is what the
    library reports today, so every assertion comparing it against the
    installed version agrees, and it goes on agreeing after an upgrade -- at
    which point the one thing this half exists for, attributing a moved metric
    to a library rather than to the system under test, quietly says the wrong
    thing. So both fields are asked for through the collaborator that answers
    them, with the collaborator made to answer something no literal would be.
    """
    monkeypatch.setattr(ir, "library_version", lambda: "9.9.9-probe")
    monkeypatch.setattr(fingerprint_module, "git_sha", lambda: "0f0f0f0-probe")

    recorded = for_suggest(_GATE_FRAME, case_count=GATE_CASES).provenance
    assert recorded["ranx"] == "9.9.9-probe"
    assert recorded["git_sha"] == "0f0f0f0-probe"


def test_the_digest_is_the_same_in_two_processes_with_different_hash_seeds() -> None:
    """**A baseline is written by one process and compared by another**, which
    is the property every other case here is blind to: a digest over anything
    `PYTHONHASHSEED` salts -- `hash()`, a `set` iterated, a `frozenset` of
    band names -- agrees with itself all day inside one interpreter and with
    no other run of the harness. Two interpreters, two seeds, and the parent's
    own answer, so this pins agreement rather than merely internal consistency.

    The digest's *shape* is asserted here rather than in its own case because
    it is the same claim: 64 lowercase hex characters is what a sha256 answers
    and is what a `hash()`, a truncation or a `repr` does not.

    `check=True` and the non-empty assertion are both load-bearing -- a child
    that failed to import prints nothing, and without them this compares `""`
    against `""` and calls it a pass. The environment is the running one with
    `PYTHONHASHSEED` overridden, because under `uv run` the child needs the
    parent's `VIRTUAL_ENV`/`PYTHONPATH` resolution.
    """
    digests = set()
    for seed in ("0", "1"):
        # S603: a fixed argv built from `sys.executable` and a module-level
        # literal, no shell and no external input. Asserting determinism
        # inside one process is exactly what cannot see this.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=_ROOT,
        )
        digests.add(result.stdout.strip())
    assert digests != {""}, "the probe printed nothing; this run proved nothing"
    assert len(digests) == 1, f"the digest is PYTHONHASHSEED-dependent: {digests}"

    here = Fingerprint(inputs=_PROBE_INPUTS, provenance={"git_sha": "abc1234"}).digest
    assert digests == {here}, (
        "the parent and the child disagree about one catalog, so a baseline "
        "written by one run of the harness cannot be compared by the next"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", here), (
        f"the digest is not 64 lowercase hex characters, so it is neither a "
        f"sha256 nor anything a baseline file can be compared on: {here!r}"
    )


def test_the_suggest_fingerprint_compares_the_frame_and_records_the_rest() -> None:
    """**The partition is the deliverable, so it is pinned as two exact sets.**

    A membership assertion (`"git_sha" in provenance`) is satisfied by a
    fingerprint that also digests it, and that is the defect this whole module
    exists to prevent -- so the halves are asserted whole, and asserted
    disjoint, which is what a field *migrating* looks like rather than a field
    appearing.

    `seed` is an input rather than provenance, deliberately and against the
    obvious reading of "a seed is provenance": the seed selects which 750
    names were drawn, so two runs at different seeds measured different case
    sets and are not two measurements of one system. `case_count` is an input
    for the same reason -- 2,993 against 2,964 is a different measurement over
    one frame, which has happened once already.

    An embedding backfill moves `title_embeddings` and touches nothing in
    here, which is the point: it must not invalidate a suggest baseline it has
    no bearing on.
    """
    fingerprint = for_suggest(_GATE_FRAME, case_count=GATE_CASES)

    assert set(fingerprint.inputs) == {
        "surface",
        "seed",
        "case_count",
        "shared_lower_names",
        "pools",
    }
    assert set(fingerprint.provenance) == {"git_sha", "python", "platform", "ranx"}
    assert set(fingerprint.inputs).isdisjoint(fingerprint.provenance)
    assert all(fingerprint.provenance.values()), (
        f"a provenance field was recorded empty, which reads in a report as a "
        f"fact nobody had: {dict(fingerprint.provenance)}"
    )


@pytest.mark.parametrize(
    ("seed", "case_count", "frame"),
    [
        (GATE_SEED + 1, GATE_CASES, _GATE_FRAME),
        (GATE_SEED, GATE_CASES - 29, _GATE_FRAME),
        (
            GATE_SEED,
            GATE_CASES,
            Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES + 1, pools=dict(GATE_POOLS)),
        ),
        (
            GATE_SEED,
            GATE_CASES,
            Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools={**GATE_POOLS, "2-4": 433}),
        ),
    ],
    ids=[
        "another-seed",
        "the-2964-case-set",
        "one-more-shared-name",
        "one-more-title-in-the-2-4-band",
    ],
)
def test_any_one_number_of_the_suggest_frame_moving_changes_its_digest(
    seed: int, case_count: int, frame: Frame
) -> None:
    """Each of the four numbers `for_suggest` calls an input has to reach the
    digest, or the run it describes is compared against a baseline drawn from
    a different catalog.

    The second parameter is the case count the transposition arm really
    produced -- 2,993 - 29 -- rather than a round number, because that is the
    difference this field exists to notice.
    """
    moved = for_suggest(frame, seed=seed, case_count=case_count)
    assert moved.digest != for_suggest(_GATE_FRAME, case_count=GATE_CASES).digest


def test_the_suggest_fingerprint_carries_no_credential_no_host_and_no_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fingerprint is written into a report, a baseline file and a CI log,
    so anything that rides along in it is published. `Settings`' four secrets
    are `SecretStr` and this module never reads them -- what it does read is
    the environment and the machine, through `platform`, and both are one
    line away from carrying a credential or naming the box.

    The env sentinel is what an `os.environ` capture would drag in; the
    hostname and the login name are what a `platform.node()` or a
    `getpass.getuser()` would. Measured on this host 2026-08-19:
    `platform.platform()` answers
    `Linux-7.1.3-2-cachyos-x86_64-with-glibc2.43` and holds neither.
    """
    sentinel = "usher-eval-must-not-travel-9f2c"
    monkeypatch.setenv("USHER_SECRET_KEY", sentinel)
    monkeypatch.setenv(
        "USHER_DATABASE_URL", f"postgresql+asyncpg://usher:{sentinel}@localhost:5432/usher"
    )
    fingerprint = for_suggest(_GATE_FRAME, case_count=GATE_CASES)

    found_python = _fields_carrying(fingerprint, platform.python_version())
    assert found_python == ["provenance.python"], (
        "the premise: this scan can find a string that really is in the record. "
        "A `_fields_carrying` that answered `[]` for everything -- a mis-spelled "
        "serialisation, a half that stopped being iterated -- passes all three "
        "assertions below exactly like a fingerprint that leaks nothing"
    )
    leaked_credential = _fields_carrying(fingerprint, sentinel)
    assert not leaked_credential, (
        f"a credential from the environment reached the record, through {leaked_credential}"
    )

    host = platform.node()
    user = getpass.getuser()
    assert host and user, (
        "the premise: this host has a name and a login to leak. Without both, "
        "the two assertions below are searching for the empty string, which "
        "every field contains, so they would fail rather than pass -- but the "
        "premise says so in one line instead of two confusing ones"
    )
    leaked_host = _fields_carrying(fingerprint, host)
    assert not leaked_host, f"the fingerprint names the machine it ran on, through {leaked_host}"

    leaked_user = _fields_carrying(fingerprint, user)
    assert not leaked_user, f"the fingerprint names the operator, through {leaked_user}"


def test_the_sha_is_the_commit_git_names_for_the_tree_the_process_is_standing_in() -> None:
    """The end-to-end half: a mocked `subprocess.run` cannot see a wrong argv,
    and `git rev-parse --short HEAD` or `git log -1` would answer a
    plausible-looking string that is not the sha a later reader would resolve.

    `monkeypatch.chdir` is not used and the comparison is run in `_ROOT`
    deliberately: `git_sha()` reads the *process's* working tree, which is
    what makes it usable from a container with a bind mount, and pytest is run
    from the repository root.
    """
    assert (_ROOT / ".git").exists(), (
        "the premise: this checkout is a git working tree. In a tarball "
        "`git_sha()` correctly answers 'unknown' and this case would be "
        "comparing two failures"
    )
    # S607: `git` rather than an absolute path, matching the call under test.
    # The argv is a list literal with no external input, so S603 does not fire
    # and a directive for it would be `RUF100`.
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert git_sha() == expected
    assert "\n" not in git_sha(), "the newline git prints would land in the record"


def test_a_directory_that_is_not_a_repository_is_unknown_rather_than_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run in a tarball, or in a container with no `.git`, is a legitimate
    run whose provenance is simply thinner.

    Real git rather than a stub, because a stubbed `subprocess.run` answers
    its scripted `CompletedProcess` whatever the call asked for -- so the
    return code a real missing repository produces, and what the module does
    with it, is only observable here.

    **It is not the cover for `check=True`, and nothing is: that mutation is
    equivalent, measured 2026-08-19.** `subprocess.CalledProcessError`
    subclasses `SubprocessError`, so `check=True` raises, `git_sha`'s own
    `except` catches it, and the answer is the same `"unknown"` -- planted, it
    survives all 28 cases in this file. Written down here as well as in the
    module because a survivor is a claim about the code, and the next reader
    of that flag will otherwise assume a case is holding it.

    The second assertion is the other half of the same failure: git says
    *"fatal: not a git repository (or any of the parent directories)"* on
    stderr and names the directory it looked in, so an implementation that
    reports what it was told publishes a filesystem path into the record.
    """
    assert shutil.which("git"), (
        "the premise: git is on PATH, so the refusal below is a repository "
        "that is absent rather than a binary that is"
    )
    monkeypatch.chdir(tmp_path)

    answer = git_sha()
    assert answer == "unknown"
    assert str(tmp_path) not in answer


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("git"),
        subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=5),
    ],
    ids=["no-git-binary-at-all", "a-git-that-never-answered"],
)
def test_a_git_that_cannot_be_run_at_all_is_unknown_rather_than_a_crash(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """The two families the `except` names, one parameter each: `OSError` for
    an image with no git in it, and `SubprocessError` for a `timeout=` that
    expired. Both are the same event -- provenance nobody could read -- and a
    harness that dies on either is a harness that cannot run in a container.
    """

    def _refuse(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(subprocess, "run", _refuse)
    assert git_sha() == "unknown"
