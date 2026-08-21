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

Every one of the first 28 cases below was watched failing against at least one
of those, planted out of tree at `/var/tmp/e1-t5/` (2026-08-19): 20 wrong
implementations, 20 killed, plus two controls that survive all five gate
steps.

**A review round added 17 more cases and 14 more plants (2026-08-19, out of
tree at `/var/tmp/e1-t5-review/`, 14 killed).** The plant copy was proved to
be the one the run imports before any of them were scored -- a `raise` at its
module scope, and the run reported a collection error naming it -- because a
plant that did not land looks exactly like a check that passed. The wrong
implementations they name:

* a `Fingerprint` that keeps the caller's mapping rather than copying and
  wrapping it -- the question `CursorSpec` settled on the same decorator --
  dies on `..._inputs_cannot_be_mutated_after_it_is_built` and its provenance
  twin, and each half-fix dies on one assertion of them: wrapped without a
  copy on the digest, copied without a wrap on the `TypeError`;
* a `git_sha` that records a clean sha for a dirty tree dies on
  `..._a_dirty_tree_is_marked_and_a_clean_one_is_not`, and the two ways of
  guessing when the tree check itself fails -- reading as clean, reading as
  dirty -- both die on `..._a_tree_check_that_itself_failed_says_so...`;
* the three no-sha events collapsed back into one `"unknown"` dies on
  `..._the_three_events_that_answer_no_sha_answer_three_different_things`
  and on the three cases that pin the literals;
* **`check=True` no longer survives**, which corrects what this docstring
  said above: with one `"unknown"` for all three events it was equivalent,
  and with the three it answers `"unknown:git-timeout"` for a git that
  answered 128 immediately, dying on
  `..._a_directory_that_is_not_a_repository_names_that_event...`;
* a `check_digest` that compares the frame half only -- which is what "but
  `check_frame` already covers it" produces -- dies on the `another-seed` and
  `the-2964-case-set` parameters of `..._is_refused_and_the_input_is_named`,
  and survives the two parameters `check_frame` really does cover, which is
  the parametrisation naming which input went unchecked;
* a hand-transcribed `GATE_DIGEST` that has drifted from the four constants
  dies on `..._is_the_digest_the_gates_own_constants_produce` **and passes**
  `..._is_this_exact_value`; a change to the input mapping's shape dies on the
  second and passes the first. Two claims, two cases, measured both ways;
* `for_suggest`'s `dict(frame.pools)` deleted dies on
  `..._the_gates_own_mappingproxy_pools_digest_as_the_plain_dict_spelling...`
  with `TypeError: Object of type mappingproxy is not JSON serializable`, and
  on nothing else in this file.

The leak scan's new premise was measured in both directions, since a premise
that skips an arm can also swallow the defect the arm exists for: with
`platform.node()` forced to `cachyos` the host arm skips with its reason, and
with a credential leak planted beside that same coincidence the case still
**fails** on the credential assertion rather than skipping.
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
from typing import Any

import pytest

from usher.eval import fingerprint as fingerprint_module
from usher.eval.errors import EvalRefused
from usher.eval.fingerprint import GATE_DIGEST, Fingerprint, check_digest, for_suggest, git_sha
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


def _legitimate_sources() -> dict[str, str]:
    """What `provenance` is *supposed* to carry, read from the same collaborators.

    The leak scan is a substring search, so a needle already inside one of
    these is indistinguishable from one that leaked, and the host arm below
    uses this to say so rather than to fail. Measured on this host 2026-08-19:
    `platform.node()` is `linux-server` and `platform.platform()` is
    `Linux-7.1.3-2-cachyos-x86_64-with-glibc2.43`, so nothing collides here
    and every arm runs -- but a machine named `cachyos` is one `hostnamectl`
    away and is a perfectly ordinary name on this distribution.

    **`git_sha()` is deliberately absent.** Its value comes from git and
    cannot legitimately contain a hostname, so an implementation that put one
    there must fail the arm rather than excuse itself from it. The residual is
    a host named entirely in hex characters colliding with a sha by accident,
    which is a flake this trades for not masking the leak.
    """
    return {
        "platform string": platform.platform(),
        "python version": platform.python_version(),
        "ranx version": ir.library_version(),
    }


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


def test_a_fingerprints_inputs_cannot_be_mutated_after_it_is_built() -> None:
    """Both halves: the dict the caller handed over, and the fingerprint's own.

    **The same question `CursorSpec` settled**, on the same decorator by the
    same mechanism (`api/cursor.py:165-171`), and the consequence here is
    larger. A cursor's digest is wrong for one request; this one is written to
    `eval.runs`, committed to `docs/evals/ledger.jsonl` and transcribed into
    `bars.toml`, and `ledger.py` reads `.digest` at two moments with a
    `session.commit()` between them -- so "the two agree" was resting on
    nobody having touched the caller's mapping in between rather than on
    anything in this class.

    The first assertion is the copy and the second is the proxy, and neither
    subsumes the other: a `dict(...)` with no wrap leaves the fingerprint free
    to edit its own record, and a wrap with no copy leaves the caller holding
    the key to it.
    """
    inputs = {"titles": 1_271_138}
    fingerprint = Fingerprint(inputs=inputs, provenance={"git_sha": "abc1234"})
    before = fingerprint.digest

    inputs["titles"] = 1_271_570
    assert fingerprint.digest == before, "the fingerprint kept a reference to the caller's dict"
    with pytest.raises(TypeError):
        fingerprint.inputs["titles"] = 1_271_570  # type: ignore[index]


def test_a_fingerprints_provenance_cannot_be_mutated_after_it_is_built() -> None:
    """The half no digest would notice, which is why it needs its own case.

    `provenance` is never compared, so a mutation here moves no digest -- it
    moves the *record*. `ledger.py` serialises it twice, into `eval.runs` and
    into `docs/evals/ledger.jsonl`, either side of a `session.commit()`, and
    the whole purpose of the half is that a later reader can attribute a moved
    metric to a library upgrade. Two sinks disagreeing about which version was
    installed is worse than neither recording it.
    """
    recorded = {"ranx": "0.3.21"}
    fingerprint = Fingerprint(inputs={"titles": 1_271_138}, provenance=recorded)

    recorded["ranx"] = "0.4.0"
    assert fingerprint.provenance["ranx"] == "0.3.21", "the fingerprint kept the caller's dict"
    with pytest.raises(TypeError):
        fingerprint.provenance["ranx"] = "0.4.0"  # type: ignore[index]


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


def test_the_gates_own_mappingproxy_pools_digest_as_the_plain_dict_spelling_does() -> None:
    """`for_suggest`'s `dict(frame.pools)` is load-bearing and was pinned by
    nothing: measured out of tree, replacing it with `frame.pools` leaves every
    other case in this file green, because no fixture anywhere passes a
    non-`dict` `pools`.

    The state is one line away and mypy-clean -- `Frame.pools` is
    `Mapping[str, int]` and `GATE_POOLS` is a `MappingProxyType`, so
    `Frame(shared_lower_names=..., pools=GATE_POOLS)` type-checks -- and the
    failure is not a wrong number: `TypeError: Object of type mappingproxy is
    not JSON serializable` surfaces at `.digest`, which is reached after the
    catalog reads and the two tier runs, as a crash out of the run rather than
    as a verdict.

    **`Fingerprint.__post_init__` does not subsume this.** That wraps the top
    level and `digest`'s own `dict(self.inputs)` unwraps that same level
    again; this proxy sits one level down, inside the `pools` value, where
    neither reaches it.
    """
    proxied = for_suggest(
        Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=GATE_POOLS),
        case_count=GATE_CASES,
    )
    assert proxied.digest == for_suggest(_GATE_FRAME, case_count=GATE_CASES).digest


def test_the_gate_digest_is_the_digest_the_gates_own_constants_produce() -> None:
    """`GATE_DIGEST` is computed from `GATE_SEED`, `GATE_CASES`,
    `GATE_SHARED_LOWER_NAMES` and `GATE_POOLS` rather than transcribed, so it
    cannot drift from the four; this is the case that says the computation is
    still the one `for_suggest` performs.

    It pins that the constant is **in force**. It cannot pin its *value* --
    both sides move together if `_suggest_inputs` changes shape -- which is
    the recorded finding this repository already carries, and why the literal
    below is a second case rather than a second assertion here.
    """
    assert for_suggest(_GATE_FRAME, seed=GATE_SEED, case_count=GATE_CASES).digest == GATE_DIGEST


def test_the_gate_digest_is_this_exact_value() -> None:
    """The other claim, and the one the case above is structurally blind to.

    A digest is only a comparability check if it is the *same* string across
    releases: it is written into `eval.runs`, into every `docs/evals/
    ledger.jsonl` line and, by Task 14, into the `source` of every bar in
    `docs/evals/bars.toml`. A change to the input mapping's shape -- one key
    renamed, a separator, a field added -- moves it, and every ledger row
    written before that change silently stops matching every row written
    after. Measured 2026-08-19 on this tree.

    **Moved once, deliberately, and the reason is not a serialisation
    change.** ADR-0040 re-anchored the sampling frame from `vote_count` --
    which had acquired a second writer on a ~38x different scale -- onto
    `imdb_num_votes`, and re-measured the five pools, `shared_lower_names`
    and `case_count` against the restored catalog. Those six numbers are
    `_GATE_INPUTS`, so the digest *should* move: a run over the old frame and
    a run over this one are genuinely not comparable, and a digest that
    survived the re-anchor would be asserting that they are.

    **It cost nothing, because it happened before the first baseline.**
    Checked at the time: `docs/evals/ledger.jsonl` held **0 rows** and no bar
    in `docs/evals/bars.toml` named the old digest, so no recorded run was
    orphaned. A later re-anchor will not be free, and this is the paragraph
    that says so.
    """
    assert GATE_DIGEST == "21678a1e2ed38b8a08700e44e5b249323cd0214a272fb07da77941017c7a369d"


def test_the_gates_own_run_is_comparable_with_the_gates_baseline() -> None:
    """The positive control, and the one that makes every refusal below
    evidence: without it a `check_digest` that refused everything -- or one
    that refused nothing and never returned -- reads the same."""
    fingerprint = for_suggest(_GATE_FRAME, case_count=GATE_CASES)
    assert check_digest(fingerprint) is fingerprint


@pytest.mark.parametrize(
    ("seed", "case_count", "frame", "named", "unnamed"),
    [
        (12_345, GATE_CASES, _GATE_FRAME, "seed", "shared_lower_names"),
        (GATE_SEED, GATE_CASES - 29, _GATE_FRAME, "case_count", "seed"),
        (
            GATE_SEED,
            GATE_CASES,
            Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES + 1, pools=dict(GATE_POOLS)),
            "shared_lower_names",
            "case_count",
        ),
        (
            GATE_SEED,
            GATE_CASES,
            Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools={**GATE_POOLS, "2-4": 433}),
            "pools",
            "seed",
        ),
    ],
    ids=["another-seed", "the-2964-case-set", "one-more-shared-name", "one-more-2-4-title"],
)
def test_a_run_whose_inputs_are_not_the_gates_is_refused_and_the_input_is_named(
    seed: int, case_count: int, frame: Frame, named: str, unnamed: str
) -> None:
    """**The hole this closes is reachable and quiet.** `usher eval suggest
    --full --seed 12345` is a supported invocation (Task 11 ships `--seed`).
    It yields a different digest, passes `check_frame` -- which sees
    `shared_lower_names` and the five pools and *cannot* see `surface`, `seed`
    or `case_count` -- is then judged against bars derived from `GATE_SEED`,
    and is written to `eval.runs` and to a git-committed `ledger.jsonl` with a
    `pass` or a `fail` beside it.

    The first parameter is that invocation. The other three are the remaining
    shapes an input takes, one per case so the verdict names which one went
    unchecked rather than reporting that some did.

    The refusal names the moved input and not the four that held, for
    `check_frame`'s reason: an operator meets this in CI and cannot act on
    "the digest differs".
    """
    with pytest.raises(EvalRefused) as caught:
        check_digest(for_suggest(frame, seed=seed, case_count=case_count))

    message = str(caught.value)
    assert named in message
    assert unnamed not in message


@pytest.mark.parametrize(
    ("inputs", "named"),
    [
        ({"judge_model": "gemma-4-26b-a4b"}, "judge_model"),
        ({"pools": None}, "pools"),
    ],
    ids=["a-key-the-gate-never-had", "a-key-the-run-did-not-read"],
)
def test_an_input_present_on_one_side_only_is_named_rather_than_merely_unequal(
    inputs: dict[str, object], named: str
) -> None:
    """An absent input and a wrong one are different operator problems, and
    the first is the one a key-by-key comparison is most likely to lose: a
    run whose pool query failed carries `None`, and a surface that grew a
    field carries one the gate never had.

    The second parameter is also the shape E3 will arrive in: a judge model id
    is an input by this module's own ruling, so the first E3 run's fingerprint
    carries a key no suggest baseline has, and it must be refused as
    incomparable rather than digested into a silent mismatch.
    """
    gate = dict(for_suggest(_GATE_FRAME, case_count=GATE_CASES).inputs)
    with pytest.raises(EvalRefused) as caught:
        check_digest(Fingerprint(inputs={**gate, **inputs}, provenance={}))
    assert named in str(caught.value)


def test_two_input_mappings_only_the_serialisation_can_tell_apart_are_still_refused() -> None:
    """The branch a key-by-key diff cannot report on, and it is reachable
    rather than paranoid: `2993.0 == 2993` in Python and `2993.0 != 2993` in
    JSON, so this mapping compares equal to the gate's key by key and digests
    differently -- a `case_count` that reached the fingerprint through a
    division, or through a driver returning `NUMERIC`, is exactly that. A
    refusal that listed what moved would list nothing, which reads as a check
    that fired for no reason.
    """
    gate = dict(for_suggest(_GATE_FRAME, case_count=GATE_CASES).inputs)
    with pytest.raises(EvalRefused, match="every input comparing equal"):
        check_digest(Fingerprint(inputs={**gate, "case_count": float(GATE_CASES)}, provenance={}))


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

    **`needle in json.dumps(value)` cannot tell a leak from a coincidence, and
    on this distribution the coincidence is one hostname away.** A machine
    named `cachyos` -- not an exotic choice under CachyOS -- is a substring of
    its own kernel release, so the host arm would fail with *"the fingerprint
    names the machine it ran on"* while nothing had leaked. That failure gets
    the case deleted as flaky and takes the credential and user assertions
    with it, which are the two that matter. So each arm states its premise
    against the strings the record legitimately carries, and any arm whose
    premise does not hold is left unasserted with the run reported as skipped
    -- **after** the assertions that can still run have run, which is the only
    ordering pytest offers for skipping part of a case.
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

    sources = _legitimate_sources()
    coincidental = []
    for label, needle in (("the machine's name", host), ("the operator's login", user)):
        collides = [source for source, value in sources.items() if needle in value]
        if collides:
            coincidental.append(f"{label} is part of this run's own {' and '.join(collides)}")
            continue
        leaked = _fields_carrying(fingerprint, needle)
        assert not leaked, f"the fingerprint carries {label}, through {leaked}"

    if coincidental:
        pytest.skip(
            "the leak scan is a substring search, so it cannot distinguish a leak "
            "from a coincidence for a needle the record legitimately contains: "
            + "; ".join(coincidental)
            + ". The credential assertion above ran and passed"
        )


def test_the_sha_is_the_commit_git_names_for_the_tree_the_process_is_standing_in() -> None:
    """The end-to-end half: a mocked `subprocess.run` cannot see a wrong argv,
    and `git rev-parse --short HEAD` or `git log -1` would answer a
    plausible-looking string that is not the sha a later reader would resolve.

    `monkeypatch.chdir` is not used and the comparison is run in `_ROOT`
    deliberately: `git_sha()` reads the *process's* working tree, which is
    what makes it usable from a container with a bind mount, and pytest is run
    from the repository root.

    **The expected value is spelled out here rather than assumed clean**,
    because this case runs against whatever tree it is invoked on and the
    ordinary state of that tree mid-task is dirty. Both arms are real: git is
    asked for the sha and asked separately whether the tree has moved past it,
    and the answer under test has to be exactly one of the two. That also
    makes this the only case that pins the marker against a *real* `git
    status`; `test_a_dirty_tree_...` builds its own repository to reach both
    states deterministically.
    """
    assert (_ROOT / ".git").exists(), (
        "the premise: this checkout is a git working tree. In a tarball "
        "`git_sha()` correctly answers 'unknown:not-a-repository' and this "
        "case would be comparing two failures"
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
    moved = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert git_sha() == (f"{expected}-dirty" if moved else expected)
    assert "\n" not in git_sha(), "the newline git prints would land in the record"


def test_a_dirty_tree_is_marked_and_a_clean_one_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**`git rev-parse HEAD` reads `.git/HEAD` and consults neither the index
    nor the worktree**, so on its own it records a clean sha for a tree
    carrying uncommitted code -- a sha naming code that did not run.

    That is the ordinary workflow rather than an edge case: this harness
    exists to answer "did my diff move the number", `--full` appends to
    `docs/evals/ledger.jsonl`, and Task 14 takes the project's baseline that
    way, so an unmarked sha there is permanent and wrong in git history.

    Both arms in one case, in a repository built here: the clean arm is what
    makes the dirty arm evidence -- an implementation that appended `-dirty`
    unconditionally passes the dirty arm alone -- and a repository of its own
    is what makes both reachable, since the tree this suite runs in is in
    whichever state the author left it.

    The sha is asserted whole rather than by a suffix, because the point of
    refusing `git describe --dirty` is that what precedes the marker stays a
    sha a reader can hand to `git show`; `v1.0-3-gabc1234-dirty` is not one.
    """
    assert shutil.which("git"), "the premise: git is on PATH"
    repo = tmp_path / "repository"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@example.invalid",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@example.invalid",
    }

    def _git(*argv: str) -> str:
        # S603/S607: a fixed argv of module-level literals, no shell, and the
        # only interpolation is `repo`, which is pytest's own `tmp_path`.
        return subprocess.run(  # noqa: S603
            ["git", *argv],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()

    _git("init", "--quiet")
    (repo / "measured.py").write_text("recall = 0.7314\n", encoding="utf-8")
    _git("add", "measured.py")
    _git("commit", "--quiet", "-m", "the baseline")
    sha = _git("rev-parse", "HEAD")
    monkeypatch.chdir(repo)

    assert git_sha() == sha, "a committed tree must record the bare sha, or the marker says nothing"

    (repo / "measured.py").write_text("recall = 0.4000\n", encoding="utf-8")
    assert git_sha() == f"{sha}-dirty", (
        "the sha names code that did not run, and nothing in the record says so"
    )


@pytest.mark.parametrize(
    "status",
    [subprocess.CompletedProcess(["git"], 128, "", "fatal"), OSError("git status could not run")],
    ids=["git-answered-non-zero", "git-could-not-be-run"],
)
def test_a_tree_check_that_itself_failed_says_so_rather_than_reading_as_clean(
    monkeypatch: pytest.MonkeyPatch, status: object
) -> None:
    """The third answer, and the reason `_tree_is_clean` is tri-state.

    `rev-parse` succeeded, so there is a sha; the tree check did not, so
    nothing was learned about the worktree. **Appending nothing would claim
    clean on evidence nobody obtained**, which is the same defect one level
    down as the single `"unknown"` these refusals were split out of -- and
    appending `-dirty` is the mirror of it, a claim about evidence nobody
    obtained in the other direction, which would put a permanent false marker
    on a baseline taken from a committed tree.
    """
    real = subprocess.run

    def _fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "status" not in argv:
            return real(argv, **kwargs)
        if isinstance(status, BaseException):
            raise status
        assert isinstance(status, subprocess.CompletedProcess)
        return status

    monkeypatch.setattr(subprocess, "run", _fake)
    answer = git_sha()
    assert answer.endswith("-worktree-unknown"), answer
    assert not answer.startswith("unknown:"), "rev-parse answered; there is a sha to record"


def test_a_directory_that_is_not_a_repository_names_that_event_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run in a tarball, or in a container with no `.git`, is a legitimate
    run whose provenance is simply thinner -- and it is the *only* one of the
    three unknowns that is legitimate, which is why it is named apart from
    them rather than sharing a bare `"unknown"`.

    Real git rather than a stub, because a stubbed `subprocess.run` answers
    its scripted `CompletedProcess` whatever the call asked for -- so the
    return code a real missing repository produces, and what the module does
    with it, is only observable here.

    **This is now also the case that covers `check=True`, which was reported
    as an equivalent mutant on 2026-08-19 and is no longer one.**
    `subprocess.CalledProcessError` subclasses `SubprocessError`, so under
    `check=True` this returncode-128 path raises and lands in the
    `SubprocessError` arm -- which answers `"unknown:git-timeout"` about a git
    that answered immediately. With one `"unknown"` for all three events that
    was invisible; measured again with the three, it fails here.

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
    assert answer == "unknown:not-a-repository"
    assert str(tmp_path) not in answer


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (FileNotFoundError("git"), "unknown:no-git"),
        (
            subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=5),
            "unknown:git-timeout",
        ),
    ],
    ids=["no-git-binary-at-all", "a-git-that-never-answered"],
)
def test_a_git_that_cannot_be_run_at_all_names_which_way_it_failed(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: str
) -> None:
    """The two families the `except`s name, one parameter each: `OSError` for
    an image with no git in it, and `SubprocessError` for a `timeout=` that
    expired. A harness that dies on either is a harness that cannot run in a
    container -- and one that reports them as the same event as a tarball
    tells an operator their run was thin when what happened is that their
    image is missing a binary.
    """

    def _refuse(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(subprocess, "run", _refuse)
    assert git_sha() == expected


def test_the_three_events_that_answer_no_sha_answer_three_different_things(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The claim the three cases above cannot make individually, and the one
    the finding is about: each of them pins a literal, and a pair of literals
    that had been made equal would pass both.

    All three are also asserted non-empty, because `provenance` is asserted
    truthy whole (`all(fingerprint.provenance.values())`) and an empty field
    reads in a report as a fact nobody had -- `None` is not available here for
    that reason, so the distinction had to be carried in the string.
    """
    assert shutil.which("git"), "the premise: git is on PATH"
    monkeypatch.chdir(tmp_path)
    not_a_repository = git_sha()

    answers = {not_a_repository}
    for failure in (
        FileNotFoundError("git"),
        subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=5),
    ):

        def _refuse(
            *_args: object, _failure: Exception = failure, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise _failure

        with monkeypatch.context() as patched:
            patched.setattr(subprocess, "run", _refuse)
            answers.add(git_sha())

    assert len(answers) == 3, f"two distinguishable events answer the same string: {answers}"
    assert all(answers), "a falsy provenance value reads as a fact nobody had"
