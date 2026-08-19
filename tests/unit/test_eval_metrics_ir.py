"""The IR adapter, pinned to arithmetic worked out by hand.

Three queries, one relevant document each: at rank 1, at rank 4, and absent.
    recall@5 = 2/3      = 0.666667
    MRR      = (1 + 1/4 + 0)/3 = 0.416667
Confirmed against ranx 0.3.21 on 2026-08-18. A library upgrade that moves
either number fails here rather than silently moving a bar.

This file imports `ranx` itself, in one case and deliberately: it is the
second file a library swap touches, because the premise that
`library_version()` exists at all -- that `ranx` exposes no `__version__` --
is a claim about the library and is worth asserting rather than trusting.
`src/` confinement is unaffected; `grep -rn "ranx" src/` finds
`metrics/ir.py` and nothing else.
"""

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
    """Measured 2026-08-18: `evaluate(qrels, run, ["recall@5"])` -- a
    one-element list -- returns a bare `np.float64`, not a dict. Two or more
    returns a dict. A caller subscripting the result would crash on exactly
    the one-metric call, which is the cheapest call and therefore the one a
    quick run makes."""
    scores = score(_RELEVANT, _RANKINGS, ["recall@5"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)


def test_every_value_is_a_builtin_float() -> None:
    """`ranx` hands back `np.float64`, which `json.dumps` cannot serialise
    and asyncpg will not bind. The ledger writes both, so the cast belongs
    here rather than at each of the two sinks."""
    for value in score(_RELEVANT, _RANKINGS, ["recall@5", "mrr"]).values():
        assert type(value) is float


def test_a_query_that_returned_nothing_scores_zero_rather_than_vanishing() -> None:
    """The denominator is the case count, always. A run that dropped
    empty-result queries would report recall over the cases that worked --
    which rises as the system gets worse."""
    scores = score(_RELEVANT, (_RANKINGS[0], _RANKINGS[1], Ranking("q3", ())), ["recall@5"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)


def test_a_total_wipeout_scores_zero_rather_than_crashing() -> None:
    """Measured 2026-08-18: `Run.from_dict` raises
    `ValueError: max() iterable argument is empty` when *every* query has an
    empty result dict. That is exactly the negative control's output and
    exactly where tier 1 heads on short typos, so the harness must be able to
    express it. The `NO_RESULT` sentinel is what makes it 0.0."""
    nothing = tuple(Ranking(query_id, ()) for query_id in _RELEVANT)
    scores = score(_RELEVANT, nothing, ["recall@5", "mrr"])
    assert scores["recall@5"] == 0.0
    assert scores["mrr"] == 0.0


def test_the_sentinel_cannot_be_mistaken_for_a_title() -> None:
    """Every real document id is a UUID string. The sentinel is not one, so
    it can never accidentally satisfy a judgement.

    The pattern is a raw string because the `|` is a real alternation and
    `ruff`'s RUF043 refuses the plain spelling -- CPython has worded this
    `ValueError` both ways across versions, so the two-branch pattern is the
    point rather than an accident.
    """
    import uuid

    with pytest.raises(ValueError, match=r"badly formed|invalid"):
        uuid.UUID(NO_RESULT)


def test_a_ranking_for_an_unjudged_query_is_refused() -> None:
    """Measured 2026-08-18: ranx raises a bare `AssertionError` reading
    'Qrels and Run query ids do not match'. Caught here so the operator gets
    a refusal naming the surface instead of an assertion from a dependency."""
    with pytest.raises(EvalRefused, match="not judged"):
        score(_RELEVANT, (*_RANKINGS, Ranking("q4", ("a",))), ["recall@5"])


def test_a_judged_query_with_no_ranking_at_all_is_refused() -> None:
    """The dangerous direction. ranx crashes here, which is the *good*
    failure -- but the tempting repair is to drop the qrels entry instead,
    which makes recall rise over a shrinking denominator. Refuse with the
    reason so nobody reaches for that repair."""
    with pytest.raises(EvalRefused, match="no ranking"):
        score(_RELEVANT, _RANKINGS[:2], ["recall@5"])


def test_two_rankings_for_one_query_are_refused_rather_than_overwriting() -> None:
    """The third refusal, and the only one whose damage is a *number* rather
    than a crash.

    ranx never sees this one: the two mismatch guards above both pass, because
    a duplicate leaves the key sets equal. The last write into `by_query`
    simply wins, and the run is scored over three queries that look complete.
    Measured 2026-08-18 with the guard deleted -- the eight cases above all
    pass, and `score` answers **recall@5 = 0.333333, mrr = 0.083333**, which
    is a believable number about a run that never happened.
    """
    with pytest.raises(EvalRefused, match="share a query id"):
        score(_RELEVANT, (*_RANKINGS, Ranking("q1", ())), ["recall@5"])


def test_a_document_id_repeated_inside_one_ranking_is_refused_at_construction() -> None:
    """The fourth refusal, and the only one raised by a **DTO** rather than by
    `score`.

    It was pinned as a *description* until 2026-08-19 -- the demotion below was
    this function's measured behaviour and nothing refused it -- and the case
    below is what that description has become. `Ranking.__post_init__` now
    refuses it, following `SearchRequest.__post_init__`'s precedent one port
    over: a DTO buildable in a state no implementation can serve pushes the
    failure onto whoever notices first, and here that was nobody.

    Three things asserted rather than one, because *that it raised* is the
    weakest possible check on a refusal and it is the one everybody writes:

    * **`EvalRefused`, not `ValueError`.** It is the same event as `score`'s
      three guards -- a harness invariant violated -- so it stays in one
      taxonomy and `runner.py` keeps a single `except`. A bare `ValueError`
      would be caught by nothing that catches its siblings.
    * **The message names the query and the offending id**, because a traceback
      has to point at the surface with the dedupe bug rather than at the
      scorer. `pytest.raises(EvalRefused)` alone is satisfied by a message
      naming neither.
    * **The refusal is at construction, not at scoring.** The `with` block
      wraps `Ranking(...)` and nothing else, so a guard moved into `score`
      would fail here -- which matters because `score`'s duplicate-*query*-id
      guard stays exactly where it is. The two are different events (the
      collection, versus one ranking's contents) and merging them blurs both.
    """
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
    """The evidence for the refusal above, kept rather than deleted with the
    behaviour it describes.

    A guard is only demonstrably load-bearing where something suspends it --
    the reason this repository's `llm_calls` CHECK is proved by
    `model_construct` cases rather than by ordinary ones. `_unguarded` is that
    suspension: `Ranking.__new__` plus `object.__setattr__` skips
    `__post_init__` on a frozen, slotted dataclass, so the scoring path the
    guard defends is still reachable and still measurable.

    `score` builds its run as a descending-score dict comprehension in rank
    order, so a repeated id's **last** write wins and the document is scored by
    its **worst** position: `("t1", "a", "t1")` becomes `{"t1": 1.0, "a": 2.0}`
    and the document listed first is ranked second.

    **Every arm carries its own duplicate-free control**, and they are the
    point rather than padding: without them each assertion is satisfied by a
    `score` that mishandles the *length* of the ranking, or the metric, or the
    single-query shape. The pairs differ in exactly one position, which is why
    both arms go through `_unguarded` -- routing the control through the real
    constructor would make each pair differ in two things at once.

    The third pair is the damage rather than the mechanism, and it is what
    bought the guard. Six other documents and one repeat of the right answer at
    the end puts the relevant title at rank 7 of 7, and `recall@5` -- the gate's
    own hit rate, the number E1 exists to compare against 2026-08-03's -- reads
    **0.0** for a ranking that opened with the correct answer. It is a total
    miss reported for a system that found the title first, so the error
    *depresses* the harness's own headline.

    Note the second pair: the repeat *raises* the score, because what gets
    demoted is an irrelevant document. So this is not "duplicates lower the
    number"; it is "a repeat is scored by its worst position", and only a pair
    that moves the number in both directions says so. Over 200 randomised
    trials permitting duplicates (`random.Random(20260819)`, lists of 3-10
    drawn with replacement from 12 documents, each against its own
    first-occurrence-wins control over nine metrics) **79 differed**.

    Measured 2026-08-19, which also refuted this module's own claim that no
    assertion here could tell a descending score from a constant one: a
    constant score answers **1.0** to the first arm's 0.5 and **0.5** to the
    second's 1.0, because a constant score cannot be overwritten into a
    different order. That refutation is now historical -- the guard makes every
    input `score` can be *handed* duplicate-free, so through the public path the
    two are indistinguishable again (400 randomised duplicate-free trials across
    nine metrics, zero differing). The **ascending** spelling, which is the
    defect an author actually writes, is still separated at MRR 0.233.
    """
    one = {"q1": "t1"}

    def unguarded(ranked: tuple[str, ...]) -> Ranking:
        raw = Ranking.__new__(Ranking)
        object.__setattr__(raw, "query_id", "q1")
        object.__setattr__(raw, "ranked_ids", ranked)
        return raw

    # The premise, and it is what makes the numbers below statements about
    # `score` rather than about the bypass. Planted and watched to fail on its
    # own line, per the standing rule that a guard nothing can falsify is a
    # deleted guard: `ranked_ids=ranked[::-1]` inside the helper fails here
    # rather than four assertions later.
    assert unguarded(("t1", "a", "b")) == Ranking("q1", ("t1", "a", "b")), (
        "the premise: on input the guard permits, the bypass and the "
        "constructor build the same object. Without it a helper that dropped, "
        "reordered or re-typed `ranked_ids` would produce every number below "
        "for a reason that has nothing to do with the demotion"
    )

    def mrr(ranked: tuple[str, ...]) -> float:
        return score(one, (unguarded(ranked),), ["mrr"])["mrr"]

    def recall(ranked: tuple[str, ...]) -> float:
        return score(one, (unguarded(ranked),), ["recall@5"])["recall@5"]

    assert mrr(("t1", "a", "b")) == 1.0
    assert mrr(("t1", "a", "t1")) == 0.5

    assert mrr(("a", "t1", "b")) == 0.5
    assert mrr(("a", "t1", "a")) == 1.0

    assert recall(("t1", "a", "b", "c", "d", "e", "f")) == 1.0
    assert recall(("t1", "a", "b", "c", "d", "e", "t1")) == 0.0


def test_the_one_metric_branch_is_keyed_by_what_was_asked_for_and_casts_it() -> None:
    """Two claims about behaviour 2's branch that the recall@5 case cannot
    make, both measured 2026-08-18 as survivors of it.

    The **key**: written as the literal `"recall@5"` rather than read from
    `metrics`, the branch answers `{'recall@5': 0.4166...}` for a call that
    asked for `mrr` -- the right number under the wrong name, and a `KeyError`
    at the caller. The one case above asks for recall@5, so it agrees.

    The **cast**: `test_every_value_is_a_builtin_float` asks for two metrics
    and therefore never reaches this branch at all, so dropping `float()` here
    leaks an `np.float64` on the *one-metric* call -- the cheapest call in the
    harness, and the one a quick run makes.
    """
    scores = score(_RELEVANT, _RANKINGS, ["mrr"])
    assert set(scores) == {"mrr"}
    assert math.isclose(scores["mrr"], (1 + 0.25) / 3, rel_tol=1e-9)
    assert type(scores["mrr"]) is float


def test_the_library_version_is_read_from_the_installed_distribution() -> None:
    """`library_version()` was called by nothing until this case, so both wrong
    spellings of it survived the file: `ranx.__version__` (an `AttributeError`
    in the middle of writing a run's provenance) and a version read for the
    wrong distribution, which answered **2.5.1** -- numpy's -- as the IR
    library's.

    The first assertion is the premise the function exists for, asserted
    rather than trusted: the day `ranx` grows a `__version__`, this docstring
    and the one on `library_version` become claims about a fact that moved,
    and somebody will simplify the metadata read away.

    The second compares against `metadata(...)["Version"]` rather than
    against a pinned literal. `>=0.3.21` is the declared floor, so a literal
    here would fail on every upgrade -- which is the opposite defect, a change
    detector on a number the run is supposed to *report* rather than pin.
    """
    assert not hasattr(ranx, "__version__"), (
        "ranx now exposes __version__; the metadata read is still correct, but "
        "the measurement both docstrings rest on has moved and should be re-stated"
    )
    version = library_version()
    assert version
    assert version == importlib.metadata.metadata("ranx")["Version"]
