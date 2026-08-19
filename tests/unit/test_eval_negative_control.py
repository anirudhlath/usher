"""Proof the harness can fail. Without this, every green run is unfalsifiable.

Two controls, and the positive one fires first: a harness where *everything*
collapses is as broken as one where nothing does.
"""

import uuid

from usher.eval.metrics.ir import Ranking, score
from usher.eval.runner import rotate_labels

_CASES = tuple(str(uuid.UUID(int=n)) for n in range(1, 21))
_RELEVANT = {f"q{n}": _CASES[n] for n in range(20)}
_PERFECT = tuple(Ranking(f"q{n}", (_CASES[n],)) for n in range(20))


def test_the_positive_control_scores_perfectly() -> None:
    """Fired first. A control that collapses an *undegraded* run is measuring
    the harness, not the system -- and it would make the negative control
    below pass for the wrong reason."""
    assert score(_RELEVANT, _PERFECT, ["recall@5"])["recall@5"] == 1.0


def test_rotating_the_labels_collapses_recall_below_any_bar() -> None:
    """The negative control. Every case is judged against its neighbour's
    answer, so nothing can hit."""
    degraded = rotate_labels(_PERFECT)
    assert score(_RELEVANT, degraded, ["recall@5"])["recall@5"] == 0.0


def test_rotating_the_labels_collapses_mrr_too() -> None:
    degraded = rotate_labels(_PERFECT)
    assert score(_RELEVANT, degraded, ["mrr"])["mrr"] == 0.0


def test_shuffling_within_k_would_not_have_been_a_control() -> None:
    """**Measured, and it is why the control is a rotation.** recall@5 over
    one relevant document is order-insensitive within k -- a control built on
    shuffling the top five would pass every run, on a green harness and on a
    broken one alike."""
    reversed_top5 = tuple(
        Ranking(f"q{n}", tuple(reversed((_CASES[n], "a", "b", "c", "d")))) for n in range(20)
    )
    assert score(_RELEVANT, reversed_top5, ["recall@5"])["recall@5"] == 1.0
    # MRR *is* order-sensitive, which is why both are reported and never blended.
    assert score(_RELEVANT, reversed_top5, ["mrr"])["mrr"] < 1.0


def test_a_rotation_preserves_the_case_count() -> None:
    """The denominator must not move. A control that also shrank the case set
    would collapse the score for two reasons and diagnose neither."""
    assert len(rotate_labels(_PERFECT)) == len(_PERFECT)
