"""The gate's 2,993 typo cases, regenerated rather than restored.

The pure generator is tested here against a hand-built pool. The catalog
reads are `tests/integration/test_eval_ledger_postgres.py`'s.
"""

import uuid
from collections import Counter

import pytest

from usher.eval.errors import EvalRefused
from usher.eval.goldens.suggest import (
    GATE_BANDS,
    GATE_SEED,
    TYPO_CLASSES,
    Frame,
    TypoCase,
    build_typo_cases,
    check_frame,
    mutate,
)


def _pool(names: list[str]) -> list[tuple[uuid.UUID, str]]:
    """Stable ids, so a re-run draws the same rows. The catalog reader orders
    by `titles.id`; this mirrors that, which is what makes the RNG's draw
    sequence reproducible at all."""
    return [(uuid.UUID(int=index + 1), name) for index, name in enumerate(names)]


def test_a_substitution_changes_exactly_one_character() -> None:
    import random

    probe = mutate("Arrival", "substitution", random.Random(GATE_SEED))  # noqa: S311
    assert probe is not None
    assert len(probe) == len("Arrival")
    assert sum(a != b for a, b in zip(probe, "Arrival", strict=True)) == 1


def test_a_deletion_declines_on_a_two_character_name() -> None:
    """A two-character name deleted is a one-character name, which is not a
    case about typo tolerance. This decline is the entire reason the gate
    counted 2,993 and not 3,000 -- seven two-character names."""
    import random

    assert mutate("Up", "deletion", random.Random(GATE_SEED)) is None  # noqa: S311
    assert mutate("Alien", "deletion", random.Random(GATE_SEED)) is not None  # noqa: S311


def test_a_transposition_draws_only_from_positions_that_transpose() -> None:
    """Drawing uniformly and declining on a doubled letter produces 2,964
    cases against the gate's 2,993 -- 29 short. Emitting the unmutated name
    instead is worse: it is a guaranteed hit for any index, which would make
    the 2-4 band's measured 0.0% arithmetically impossible. Drawing from the
    valid positions is the only reading that produces both numbers."""
    import random

    probe = mutate("aabb", "transposition", random.Random(1))  # noqa: S311
    assert probe is not None
    assert probe != "aabb"
    assert sorted(probe) == sorted("aabb")


def test_a_transposition_declines_when_every_character_is_the_same() -> None:
    import random

    assert mutate("aaa", "transposition", random.Random(1)) is None  # noqa: S311


def test_a_doubled_letter_lengthens_the_name_by_one() -> None:
    import random

    probe = mutate("Heat", "doubled", random.Random(GATE_SEED))  # noqa: S311
    assert probe is not None
    assert len(probe) == len("Heat") + 1


def test_the_same_seed_and_pool_produce_a_byte_identical_case_set() -> None:
    """Reproducibility is the whole point. Two runs that disagree about the
    case set are not two measurements of one system."""
    pools = {band: _pool([f"{band}-name-{n}" for n in range(20)]) for band, _l, _h in GATE_BANDS}
    first = build_typo_cases(pools, seed=GATE_SEED)
    second = build_typo_cases(pools, seed=GATE_SEED)
    assert first == second
    assert build_typo_cases(pools, seed=GATE_SEED + 1) != first


def test_every_case_carries_the_title_its_probe_must_still_find() -> None:
    pools = {band: _pool(["Solaris", "Stalker", "Ikiru"]) for band, _l, _h in GATE_BANDS}
    cases = build_typo_cases(pools, seed=GATE_SEED)
    assert cases
    by_name = {name: title_id for title_id, name in pools["2-4"]}
    for case in cases:
        assert isinstance(case, TypoCase)
        assert case.typo_class in TYPO_CLASSES
        assert case.title_id == by_name[case.name]
        assert case.probe != case.name


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
    assert len(cases) == 2993
    counts = Counter(case.typo_class for case in cases)
    assert counts["deletion"] == 743
    assert counts["substitution"] == counts["transposition"] == counts["doubled"] == 750


def test_a_frame_that_does_not_reproduce_the_gate_is_refused() -> None:
    """A pool one row out is a different eligible population, and a recall
    figure over a different population is not the gate's however close it
    looks. Refuse rather than report."""
    with pytest.raises(EvalRefused, match="sampling frame"):
        check_frame(Frame(shared_lower_names=1, pools={band: 1 for band, _l, _h in GATE_BANDS}))


def test_the_gates_own_frame_is_accepted() -> None:
    """The positive control. Without it the test above passes for a
    `check_frame` that refuses everything."""
    from usher.eval.goldens.suggest import GATE_POOLS, GATE_SHARED_LOWER_NAMES

    check_frame(Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=dict(GATE_POOLS)))
