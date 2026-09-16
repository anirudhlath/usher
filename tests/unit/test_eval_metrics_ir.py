"""The IR adapter, pinned to arithmetic worked out by hand."""

import importlib.metadata
import math

import pytest
import ranx

from usher.eval.errors import EvalRefused
from usher.eval.metrics.ir import NO_RESULT, Ranking, library_version, score

_RELEVANT = {"q1": "t1", "q2": "t2", "q3": "t3"}
_RANKINGS = (
    Ranking("q1", ("t1", "a", "b", "c", "d")),
    Ranking("q2", ("a", "b", "c", "t2", "d")),
    Ranking("q3", ("a", "b", "c", "d", "e")),
)


def test_recall_and_mrr_match_the_hand_computed_control() -> None:
    scores = score(_RELEVANT, _RANKINGS, ["recall@5", "mrr"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)
    assert math.isclose(scores["mrr"], (1 + 0.25) / 3, rel_tol=1e-9)


def test_a_single_metric_still_returns_a_mapping() -> None:
    """`evaluate` returns a bare `np.float64` for a one-element metric list.

    Two or more returns a dict, so a caller subscripting the result would crash
    on exactly the one-metric call, which is the one a quick run makes.
    """
    scores = score(_RELEVANT, _RANKINGS, ["recall@5"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)


def test_every_value_is_a_builtin_float() -> None:
    """`ranx` hands back `np.float64`, which `json.dumps` and asyncpg both refuse.

    The ledger writes to both sinks, so the cast belongs here rather than at each.
    """
    for value in score(_RELEVANT, _RANKINGS, ["recall@5", "mrr"]).values():
        assert type(value) is float


def test_a_query_that_returned_nothing_scores_zero_rather_than_vanishing() -> None:
    """The denominator is the case count, always.

    A run that dropped empty-result queries would report recall over the cases that
    worked -- which rises as the system gets worse.
    """
    scores = score(_RELEVANT, (_RANKINGS[0], _RANKINGS[1], Ranking("q3", ())), ["recall@5"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)


def test_a_total_wipeout_scores_zero_rather_than_crashing() -> None:
    """`Run.from_dict` raises when *every* query has an empty result dict.

    That is the negative control's own output, and where tier 1 heads on short
    typos, so the harness must express it; `NO_RESULT` is what makes it 0.0.
    """
    nothing = tuple(Ranking(query_id, ()) for query_id in _RELEVANT)
    scores = score(_RELEVANT, nothing, ["recall@5", "mrr"])
    assert scores["recall@5"] == 0.0
    assert scores["mrr"] == 0.0


def test_the_sentinel_cannot_be_mistaken_for_a_title() -> None:
    """Every real document id is a UUID string, and the sentinel is not one.

    A fact about the sentinel only: it says the sentinel cannot *be* a title
    id, not that the ids arriving in `relevant` are title ids, which
    `test_a_judgement_naming_the_empty_result_sentinel_is_refused` closes. The
    pattern has two branches because CPython has worded this `ValueError` both
    ways across versions.
    """
    import uuid

    with pytest.raises(ValueError, match=r"badly formed|invalid"):
        uuid.UUID(NO_RESULT)


def test_a_ranking_for_an_unjudged_query_is_refused() -> None:
    """A bare `AssertionError` is what ranx raises when the query id sets disagree.

    Caught here so the operator gets a refusal naming the surface instead of an
    assertion from a dependency.
    """
    with pytest.raises(EvalRefused, match="not judged"):
        score(_RELEVANT, (*_RANKINGS, Ranking("q4", ("a",))), ["recall@5"])


def test_a_judged_query_with_no_ranking_at_all_is_refused() -> None:
    """The dangerous direction, because the tempting repair is silent.

    ranx crashes here, which is the *good* failure; dropping the qrels entry
    instead makes recall rise over a shrinking denominator. The refusal carries
    the reason so nobody reaches for that repair.
    """
    with pytest.raises(EvalRefused, match="no ranking"):
        score(_RELEVANT, _RANKINGS[:2], ["recall@5"])


def test_two_rankings_for_one_query_are_refused_rather_than_overwriting() -> None:
    """A duplicate query id does damage that reads as a number, not as a crash.

    ranx never sees it: the two mismatch guards above pass, because a duplicate
    leaves the key sets equal, and the last write into `by_query` simply wins.
    The run is then scored over queries that only look complete.
    """
    with pytest.raises(EvalRefused, match="share a query id"):
        score(_RELEVANT, (*_RANKINGS, Ranking("q1", ())), ["recall@5"])


def test_a_judgement_naming_the_empty_result_sentinel_is_refused() -> None:
    """The fourth refusal, and the only one whose damage is a **perfect score**."""
    with pytest.raises(EvalRefused) as caught:
        score({"q1": NO_RESULT}, (Ranking("q1", ()),), ["recall@5", "mrr"])

    assert "q1" in str(caught.value), (
        f"the refusal names no query, so a traceback cannot say which judgement "
        f"produced it: {caught.value}"
    )
    assert NO_RESULT in str(caught.value), (
        f"the refusal does not name the sentinel, so a caller has nothing to "
        f"grep the goldens for: {caught.value}"
    )

    # The control that says the guard is about the *sentinel* and not about an
    # empty ranking: the identical call with a real relevant id is scored, and
    # scored as the total miss it is.
    scored = score({"q1": "t1"}, (Ranking("q1", ()),), ["recall@5", "mrr"])
    assert scored == {"recall@5": 0.0, "mrr": 0.0}


def test_a_document_id_repeated_inside_one_ranking_is_refused_at_construction() -> None:
    """The fifth refusal, and the only one raised by a **DTO** rather than by `score`."""
    with pytest.raises(EvalRefused) as caught:
        Ranking("q1", ("t1", "a", "t1"))

    assert "q1" in str(caught.value), (
        f"the refusal names no query, so a traceback cannot say which surface "
        f"produced it: {caught.value}"
    )
    assert "t1" in str(caught.value), (
        f"the refusal names no repeated id, so a caller with a long ranking "
        f"has nothing to grep for: {caught.value}"
    )

    # The control that says the guard is about repetition and not about
    # `ranked_ids` at all: the same length, the same ids minus the repeat.
    assert Ranking("q1", ("t1", "a", "b")).ranked_ids == ("t1", "a", "b")


def test_the_demotion_the_guard_prevents_is_still_reachable_with_the_guard_suspended() -> None:
    """The demotion the refusal above prevents, exhibited with the guard bypassed."""
    one = {"q1": "t1"}

    def _unguarded(ranked: tuple[str, ...]) -> Ranking:
        raw = Ranking.__new__(Ranking)
        object.__setattr__(raw, "query_id", "q1")
        object.__setattr__(raw, "ranked_ids", ranked)
        return raw

    # The premise, and what makes the numbers below statements about `score`
    # rather than about the bypass: a helper that dropped, reordered or
    # re-typed `ranked_ids` fails here rather than four assertions later.
    assert _unguarded(("t1", "a", "b")) == Ranking("q1", ("t1", "a", "b")), (
        "the premise: on input the guard permits, the bypass and the "
        "constructor build the same object. Without it a helper that dropped, "
        "reordered or re-typed `ranked_ids` would produce every number below "
        "for a reason that has nothing to do with the demotion"
    )

    def mrr(ranked: tuple[str, ...]) -> float:
        return score(one, (_unguarded(ranked),), ["mrr"])["mrr"]

    def recall(ranked: tuple[str, ...]) -> float:
        return score(one, (_unguarded(ranked),), ["recall@5"])["recall@5"]

    assert mrr(("t1", "a", "b")) == 1.0
    assert mrr(("t1", "a", "t1")) == 0.5

    assert mrr(("a", "t1", "b")) == 0.5
    assert mrr(("a", "t1", "a")) == 1.0

    assert recall(("t1", "a", "b", "c", "d", "e", "f")) == 1.0
    assert recall(("t1", "a", "b", "c", "d", "e", "t1")) == 0.0


def test_the_one_metric_branch_is_keyed_by_what_was_asked_for_and_casts_it() -> None:
    """Two claims about the one-metric branch the recall@5 case cannot make.

    The **key**: written as the literal `"recall@5"` rather than read from
    `metrics`, the branch answers the right number under the wrong name for a
    call that asked for `mrr`. The **cast**:
    `test_every_value_is_a_builtin_float` asks for two metrics and never
    reaches this branch, so dropping `float()` here leaks an `np.float64` on
    the one-metric call.
    """
    scores = score(_RELEVANT, _RANKINGS, ["mrr"])
    assert set(scores) == {"mrr"}
    assert math.isclose(scores["mrr"], (1 + 0.25) / 3, rel_tol=1e-9)
    assert type(scores["mrr"]) is float


def test_the_library_version_is_read_from_the_installed_distribution() -> None:
    """The version comes from the installed distribution, not from the module.

    `ranx` exposes no `__version__`, and the first assertion pins that premise
    rather than trusting it: the day it grows one, somebody will simplify the
    metadata read away. The second compares against `metadata(...)["Version"]`
    rather than a pinned literal, which would fail on every upgrade of a number
    the run is supposed to *report* rather than pin.
    """
    assert not hasattr(ranx, "__version__"), (
        "ranx now exposes __version__; the metadata read is still correct, but "
        "the measurement both docstrings rest on has moved and should be re-stated"
    )
    version = library_version()
    assert version
    assert version == importlib.metadata.metadata("ranx")["Version"]
