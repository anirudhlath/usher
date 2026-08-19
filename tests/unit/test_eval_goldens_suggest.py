"""The gate's 2,993 typo cases, regenerated rather than restored.

The pure generator is tested here against a hand-built pool. The catalog
reads are `tests/integration/test_eval_goldens_postgres.py`'s -- not the
ledger file beside it, which is the schema and DDL one.
"""

import os
import subprocess
import sys
import uuid
from collections import Counter

import pytest

from usher.eval.errors import EvalRefused
from usher.eval.goldens.suggest import (
    GATE_BANDS,
    GATE_CASES,
    GATE_POOLS,
    GATE_SEED,
    GATE_SHARED_LOWER_NAMES,
    TYPO_CLASSES,
    Frame,
    TypoCase,
    build_typo_cases,
    check_frame,
    mutate,
)

#: Run in a second interpreter under two `PYTHONHASHSEED` values. Deliberately
#: a digest of the whole case tuple rather than a length or a sample: the
#: defect this exists for reorders cases without losing any.
_PROBE = (
    "import hashlib, uuid;"
    "from usher.eval.goldens.suggest import GATE_BANDS, GATE_SEED, build_typo_cases;"
    "pools = {band: [(uuid.UUID(int=n + 1), f'{band}-name-{n}') for n in range(20)]"
    " for band, _low, _high in GATE_BANDS};"
    "print(hashlib.sha256(repr(build_typo_cases(pools, seed=GATE_SEED)).encode()).hexdigest())"
)


def _pool(names: list[str]) -> list[tuple[uuid.UUID, str]]:
    """Stable ids, so a re-run draws the same rows. The catalog reader orders
    by `titles.id`; this mirrors that, which is what makes the RNG's draw
    sequence reproducible at all."""
    return [(uuid.UUID(int=index + 1), name) for index, name in enumerate(names)]


def _gate_frame(**moved: int) -> Frame:
    """The gate's own six numbers, with the named ones nudged.

    A helper rather than six literals because the whole claim under test is
    *one row out*, and a fixture that moves five numbers while asserting
    about one is the fixture that let a `check_frame` ignoring
    `shared_lower_names` survive.
    """
    pools = dict(GATE_POOLS)
    shared = GATE_SHARED_LOWER_NAMES + moved.pop("shared", 0)
    for band, delta in moved.items():
        pools[band] += delta
    return Frame(shared_lower_names=shared, pools=pools)


def test_a_substitution_changes_exactly_one_character() -> None:
    import random

    probe = mutate("Arrival", "substitution", random.Random(GATE_SEED))  # noqa: S311
    assert probe is not None
    assert len(probe) == len("Arrival")
    assert sum(a != b for a, b in zip(probe, "Arrival", strict=True)) == 1


def test_a_substitution_lands_where_the_rng_drew_it_and_not_always_at_one_place() -> None:
    """Position is the whole difference between this class and a no-op.

    Tier 1 is a `lower(name) text_pattern_ops` **prefix** probe, so a
    substitution pinned to index 0 destroys every prefix match there is and
    would drive that arm's recall to zero for reasons that are the generator's
    rather than the index's. An arm spelled `at = 0` changes exactly one
    character, keeps the length, and passes the case above.
    """
    import random

    landed = set()
    for seed in range(12):
        probe = mutate("Interstellar", "substitution", random.Random(seed))  # noqa: S311
        assert probe is not None
        changed = [
            index for index, (a, b) in enumerate(zip(probe, "Interstellar", strict=True)) if a != b
        ]
        assert len(changed) == 1
        landed.add(changed[0])
    assert len(landed) > 1, f"every seed substituted at the same index: {landed}"


def test_a_deletion_declines_on_a_two_character_name_and_otherwise_drops_one() -> None:
    """A two-character name deleted is a one-character name, which is not a
    case about typo tolerance. This decline is the entire reason the gate
    counted 2,993 and not 3,000 -- seven two-character names.

    The length assertion is the second half and it is not decoration: an arm
    deleting *two* characters still declines on `"Up"` and still answers
    non-`None` on `"Alien"`, so "is not None" alone ratifies a two-character
    edit as a single-edit typo.
    """
    import random

    assert mutate("Up", "deletion", random.Random(GATE_SEED)) is None  # noqa: S311
    probe = mutate("Alien", "deletion", random.Random(GATE_SEED))  # noqa: S311
    assert probe is not None
    assert len(probe) == len("Alien") - 1


def test_a_transposition_draws_only_from_positions_that_transpose() -> None:
    """Drawing uniformly and declining on a doubled letter produces 2,964
    cases against the gate's 2,993 -- 29 short. Emitting the unmutated name
    instead is worse: it is a guaranteed hit for any index, which would make
    the 2-4 band's measured 0.0% arithmetically impossible. Drawing from the
    valid positions is the simplest reading that produces both numbers --
    rejection sampling reaches 2,993 too and emits nothing unmutated, so
    "simplest" rather than "only"."""
    import random

    probe = mutate("aabb", "transposition", random.Random(1))  # noqa: S311
    assert probe is not None
    assert probe != "aabb"
    assert sorted(probe) == sorted("aabb")


def test_a_transposition_declines_when_every_character_is_the_same() -> None:
    import random

    assert mutate("aaa", "transposition", random.Random(1)) is None  # noqa: S311


def test_a_doubled_letter_repeats_a_character_of_the_name() -> None:
    """`len(probe) == len(name) + 1` is satisfied by inserting a *random*
    letter, which is a materially different edit: `doubled` measured 95.5% in
    ADR-0002 and an arbitrary insertion is not what that number is about.
    The four spellings below are every position `"Heat"` admits.
    """
    import random

    probe = mutate("Heat", "doubled", random.Random(GATE_SEED))  # noqa: S311
    assert probe is not None
    assert probe in {"HHeat", "Heeat", "Heaat", "Heatt"}


def test_the_same_seed_and_pool_produce_a_byte_identical_case_set() -> None:
    """Reproducibility is the whole point. Two runs that disagree about the
    case set are not two measurements of one system."""
    pools = {band: _pool([f"{band}-name-{n}" for n in range(20)]) for band, _l, _h in GATE_BANDS}
    first = build_typo_cases(pools, seed=GATE_SEED)
    second = build_typo_cases(pools, seed=GATE_SEED)
    assert first == second
    assert build_typo_cases(pools, seed=GATE_SEED + 1) != first


def test_the_case_set_is_identical_in_two_processes_with_different_hash_seeds() -> None:
    """**The property the whole design rests on, and it was pinned in prose.**

    E1 exists to be comparable with a run taken on 2026-08-03 by a different
    process on a different day. The case above compares two calls inside one
    interpreter, so anything salted by `PYTHONHASHSEED` -- a `set` iterated,
    a `frozenset` of bands, a dict keyed on something unhashable-turned-tuple
    -- agrees with itself and disagrees with every other run of the harness.
    Two interpreters, two seeds, one expected digest.

    Behavioural rather than an AST assertion on purpose: the property is
    cross-process reproducibility, not a spelling, and a structural check
    pointed at `build_typo_cases` would miss hash-order dependence introduced
    anywhere else on the path.

    **Two things it deliberately does not catch, so it is not read as
    standing in for them.** It does not catch `for band in pools:` -- dict
    iteration is insertion-ordered and `read_pools` inserts in `GATE_BANDS`
    order, so that spelling is benign today. And it does not catch any of the
    *deterministic* reorderings: a fresh generator per band, bands reversed,
    classes reversed. Those move both processes identically and are pinned by
    `test_the_bands_and_classes_come_out_in_the_order_the_rng_was_consumed`
    and `test_one_generator_spans_every_band_rather_than_restarting`.

    The environment is the running one with `PYTHONHASHSEED` overridden
    rather than a hand-built dict: under `uv run` the child needs the
    parent's `VIRTUAL_ENV`/`PYTHONPATH` resolution. `check=True` and the
    empty-digest assertion are both load-bearing -- a child that failed to
    import prints nothing, and without them this compares `""` against `""`
    and calls it a pass.
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
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        )
        digests.add(result.stdout.strip())
    assert len(digests) == 1, f"the case set is PYTHONHASHSEED-dependent: {digests}"
    assert digests != {""}, "the probe printed nothing; this run proved nothing"


def test_the_bands_and_classes_come_out_in_the_order_the_rng_was_consumed() -> None:
    """**Nothing else in this file asserts `case.band` at all.**

    A generator stamping every case `"2-4"` passed the whole file, and band is
    the axis the gate is scored on -- 0.75 on 2-4 against 0.90 on 8+ -- riding
    into the scorer's strata through `TypoCase.query_id`. The case below it
    cannot see band either, because its pools give all five bands identical
    names and identical ids, so its `by_name` lookup is satisfied by fixture
    construction.

    Order rather than membership, because `GATE_BANDS` order and
    `TYPO_CLASSES` order are both draw order: one `random.Random(seed)` spans
    the whole run, so reversing either produces a different 750 names from
    the same seed with in-process determinism entirely intact. Kills the
    constant-band, reversed-band and reversed-class spellings.
    """
    pools = {band: _pool([f"{band}-name-{n}" for n in range(20)]) for band, _l, _h in GATE_BANDS}
    cases = build_typo_cases(pools, seed=GATE_SEED)
    visited: list[str] = []
    for case in cases:
        if not visited or visited[-1] != case.band:
            visited.append(case.band)
    assert visited == [band for band, _l, _h in GATE_BANDS]
    assert tuple(case.typo_class for case in cases[:4]) == TYPO_CLASSES


def test_one_generator_spans_every_band_rather_than_restarting() -> None:
    """**The reproducibility case cannot see this and no reproducibility case
    can.** It compares two calls into the same function, so a `random.Random(
    seed)` built freshly *per band* moves both sides together and stays green
    -- while drawing the identical positions in all five bands, i.e. a
    different 750 names from the gate's with in-process determinism fully
    intact. What gives it away is that the five bands then agree about which
    row positions were drawn.

    Note the 200-row pools: every other pool in this file is at most 150, so
    `chooser.sample(rows, min(150, len(rows)))` clamps and no other case has
    ever exercised an actual sample. Here it draws 150 of 200, which is both
    the shape the real catalog has and the first time the sample is
    load-bearing.
    """
    pools = {
        band: _pool([f"{band}-name-{n:03d}" for n in range(200)]) for band, _l, _h in GATE_BANDS
    }
    drawn: dict[str, list[str]] = {}
    for case in build_typo_cases(pools, seed=GATE_SEED):
        seq = drawn.setdefault(case.band, [])
        position = case.name.rsplit("-", 1)[1]
        if not seq or seq[-1] != position:
            seq.append(position)
    assert len(drawn) == len(GATE_BANDS)
    assert len({tuple(seq) for seq in drawn.values()}) == len(GATE_BANDS)


def test_every_case_carries_the_title_its_probe_must_still_find() -> None:
    """The pools are distinct per band on purpose. With one pool repeated
    under five keys -- identical names, identical ids -- `by_name[case.name]`
    matches whatever band the case claims to be in, and the lookup is
    satisfied by how the fixture was built rather than by what the generator
    did with it."""
    pools = {
        band: _pool([f"{band} Solaris", f"{band} Stalker", f"{band} Ikiru"])
        for band, _l, _h in GATE_BANDS
    }
    cases = build_typo_cases(pools, seed=GATE_SEED)
    assert cases
    by_name = {name: (band, title_id) for band, rows in pools.items() for title_id, name in rows}
    for case in cases:
        assert isinstance(case, TypoCase)
        assert case.typo_class in TYPO_CLASSES
        assert (case.band, case.title_id) == by_name[case.name]
        assert case.probe != case.name


def test_the_query_id_carries_the_band_and_class_the_strata_are_scored_on() -> None:
    """`query_id` is what reaches the IR run, and a scorer that has to
    re-join to the case list to learn a query's band is a scorer that can get
    the join wrong. Uniqueness is the other half: `EvalRefused` covers "two
    rankings sharing a query id" as a harness bug, which is only detectable
    if the generator does not mint duplicates itself."""
    pools = {band: _pool([f"{band} Solaris", f"{band} Stalker"]) for band, _l, _h in GATE_BANDS}
    cases = build_typo_cases(pools, seed=GATE_SEED)
    assert cases
    for case in cases:
        assert case.query_id == f"{case.band}|{case.typo_class}|{case.title_id}"
    assert len({case.query_id for case in cases}) == len(cases)


def test_the_case_count_arithmetic_reproduces_the_gates_2993() -> None:
    """**Run before this plan was written, and it is the strongest evidence
    the port is faithful.** Five bands x 150 names x four classes is 3,000;
    the gate recorded 2,993, and the seven missing are two-character names
    that admit no deletion. Against a synthetic pool whose 2-4 band holds
    exactly seven two-character names, this generator produces **2,993** --
    750 substitutions, 750 transpositions, 750 doubles and **743** deletions.

    Note the transposition arm stays at 750: `"ab"` transposes to `"ba"`,
    which is why the seven declines are deletions alone. A generator whose
    transposition arm also declined would give 2,986 and would not be this
    procedure.

    Asserted against `GATE_CASES` and not against the literal `2993`.
    `GATE_CASES` had no reader anywhere in `src/` or `tests/` while this line
    hardcoded the number it holds, and this repository's recorded finding is
    that when a constant's whole purpose is being the same object as another
    one, equality is not the assertion -- the binding is.
    """
    pools = {
        "2-4": _pool(["ab" if n < 7 else f"name{n:04d}" for n in range(150)]),
        **{
            band: _pool([f"{band}-name-{n:04d}" for n in range(150)])
            for band, _low, _high in GATE_BANDS
            if band != "2-4"
        },
    }
    cases = build_typo_cases(pools, seed=GATE_SEED)
    assert len(cases) == GATE_CASES
    counts = Counter(case.typo_class for case in cases)
    assert counts["deletion"] == 743
    assert counts["substitution"] == counts["transposition"] == counts["doubled"] == 750


@pytest.mark.parametrize("moved", ["shared", *(band for band, _l, _h in GATE_BANDS)])
def test_any_one_of_the_six_frame_numbers_one_row_out_is_refused(moved: str) -> None:
    """Six numbers and all six have to land, so all six get an arm.

    The single case this replaced moved **all six at once** -- and 431 rows
    out on the 2-4 pool, not one -- so a `check_frame` ignoring
    `shared_lower_names`, or ignoring the pools entirely, or comparing only
    the 2-4 pool, all still refused it and survived. The docstring's claim is
    *a pool one row out*; only a frame that is one row out states it.
    """
    with pytest.raises(EvalRefused, match="sampling frame"):
        check_frame(_gate_frame(**{moved: 1}))


def test_the_refusal_names_the_number_that_moved_and_not_the_five_that_did_not() -> None:
    """An operator meets this in CI as `baseline-invalid`. Two five-entry
    dicts printed beside each other leave six numbers to diff by eye, for a
    check whose thesis is that one row is enough."""
    with pytest.raises(EvalRefused) as caught:
        check_frame(_gate_frame(**{"8-11": 1}))
    message = str(caught.value)
    assert "8-11: expected 7178, observed 7179" in message
    assert "12-19" not in message
    assert "shared_lower_names" not in message


def test_a_band_missing_from_the_frame_is_named_rather_than_only_unequal() -> None:
    """An absent band and a wrong band are different operator problems. Read
    off a dict inequality they are one message; read off the drift the
    missing one arrives as `observed None`, which names it."""
    pools = {band: count for band, count in GATE_POOLS.items() if band != "20+"}
    with pytest.raises(EvalRefused) as caught:
        check_frame(Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=pools))
    assert "20+: expected 17887, observed None" in str(caught.value)


def test_a_band_the_gate_never_had_is_refused() -> None:
    """The equality this check replaced compared two whole dicts, so a sixth
    band was a difference. Reporting per-expected-number would have dropped
    that silently -- a rewrite that quietly narrows a check is how a check
    stops existing."""
    pools = {**GATE_POOLS, "40+": 12}
    with pytest.raises(EvalRefused, match="sampling frame"):
        check_frame(Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=pools))


def test_the_gates_recorded_pool_sizes_cannot_be_edited_in_place() -> None:
    """`Mapping[str, int]` on a plain dict is documentation, not protection,
    and this is the constant the whole comparability story rests on:
    `GATE_POOLS["2-4"] = 433` is one line, silent and process-wide, and it
    moves the number `check_frame` refuses against. Immutability only --
    `Frame` still is not hashable, because `mappingproxy` delegates
    `__hash__` to the dict it wraps and that is `None`."""
    with pytest.raises(TypeError):
        GATE_POOLS["2-4"] = 433  # type: ignore[index]
    assert GATE_POOLS["2-4"] == 432


def test_the_gates_own_frame_is_accepted() -> None:
    """The positive control. Without it the cases above pass for a
    `check_frame` that refuses everything."""
    check_frame(Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=dict(GATE_POOLS)))
