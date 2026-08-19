"""IR scoring. **The only module in this project that imports `ranx`.**

Everything below is arranged around four things measured against ranx 0.3.21
on 2026-08-18, each of which changes the code:

1. `ranx.__version__` does not exist -- `importlib.metadata` is the reader.
2. A **one-element** metric list returns a bare `np.float64`, not a dict.
3. A query in the qrels but missing from the run raises a bare
   `AssertionError`.
4. `Run.from_dict` raises `ValueError: max() iterable argument is empty` when
   *every* query has an empty result dict.

(4) is the load-bearing one: total failure is what the negative control
produces and what tier 1 approaches on short typos, so a harness that crashes
there cannot report the finding it exists to report.
"""

import importlib.metadata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from usher.eval.errors import EvalDependencyMissing, EvalRefused

try:
    from ranx import Qrels, Run, evaluate
except ImportError as exc:  # pragma: no cover - exercised by the CLI preflight
    raise EvalDependencyMissing("ranx") from exc

# The stand-in document for a query that returned nothing at all.
#
# **Not cosmetic.** `Run.from_dict` refuses a run whose every entry is empty
# (measured, see the module docstring), so without this a total wipeout is a
# `ValueError` rather than 0.0. Verified 2026-08-18 to score identically to an
# empty dict in the mixed case: 0.5 either way over the same two queries.
#
# It is deliberately not a UUID, so it can never collide with a real title id
# and satisfy a judgement by accident.
NO_RESULT = "__no_result__"


@dataclass(frozen=True, slots=True)
class Ranking:
    """What one query returned, best first. Empty is a legitimate answer."""

    query_id: str
    ranked_ids: tuple[str, ...]


def library_version() -> str:
    """`ranx.__version__` does not exist. Recorded in every run's provenance
    so a metric that moves can be attributed to a library rather than to the
    system under test."""
    return importlib.metadata.version("ranx")


def score(
    relevant: Mapping[str, str],
    rankings: Sequence[Ranking],
    metrics: Sequence[str],
) -> dict[str, float]:
    """Score `rankings` against one relevant document per query.

    One relevant document is the shape every E1 judgement has: a typo probe
    should find the title it was mutated from. `recall@5` over a single
    relevant document is therefore the gate's own hit rate, which is what
    makes E1's numbers comparable with 2026-08-03's.

    **The denominator is `relevant`, always.** Both directions of a mismatch
    are refused rather than repaired, because the tempting repair for the
    second one -- dropping the judgement instead of adding an empty ranking --
    makes recall *rise* as the system gets worse.
    """
    by_query = {ranking.query_id: ranking for ranking in rankings}
    if len(by_query) != len(rankings):
        raise EvalRefused("two rankings share a query id; scores would silently overwrite")
    unjudged = set(by_query) - set(relevant)
    if unjudged:
        raise EvalRefused(
            f"{len(unjudged)} ranking(s) name a query that is not judged, "
            f"e.g. {sorted(unjudged)[0]!r}"
        )
    unanswered = set(relevant) - set(by_query)
    if unanswered:
        raise EvalRefused(
            f"{len(unanswered)} judged quer(y/ies) have no ranking at all, e.g. "
            f"{sorted(unanswered)[0]!r} -- add an empty ranking; do not drop the "
            "judgement, which would raise the score by shrinking the denominator"
        )

    qrels = Qrels.from_dict({query: {document: 1} for query, document in relevant.items()})
    # The descending score is what carries the ranking. Measured 2026-08-18 and
    # recorded because the code invites the question: **on duplicate-free input**
    # a *constant* score scores identically, because 0.3.21 breaks ties in
    # insertion order and this dict is built in rank order -- confirmed over 400
    # randomised trials across nine metrics with zero differing cases. So on the
    # input this harness is built for, the ordering would be riding on a library
    # internal rather than on anything stated. An *ascending* score, which is the
    # defect an author actually writes, moves MRR to 0.233 and is pinned.
    #
    # **"No assertion in this repository can hold the two apart" was too strong,
    # corrected 2026-08-19 by measurement.** A **duplicate id inside one
    # ranking's `ranked_ids`** separates them, because in this comprehension the
    # last write wins: `("t1", "a", "t1")` builds `{"t1": 1.0, "a": 2.0}`, so a
    # repeated document is scored by its **worst** position and demoted below
    # everything that outranked the later occurrence -- MRR 0.5 here against a
    # constant score's 1.0, and 1.0 against 0.5 for the mirror
    # `("a", "t1", "a")`, with `("t1", "a", "b")` identical as the control.
    #
    # That demotion is this function's behaviour and not a curiosity: with six
    # other documents in the list, one repeat of the *right* answer takes
    # `recall@5` -- the gate's own hit rate -- from **1.0 to 0.0**. It is
    # described and pinned by
    # `test_a_document_id_repeated_inside_one_ranking_is_scored_by_its_worst_position`,
    # deliberately as a description rather than a design. Note the asymmetry the
    # guards above make: duplicate *query* ids across rankings are refused, and
    # duplicate *document* ids within one ranking are not. Whether `Ranking`
    # should refuse them at construction is open and is not decided here.
    run = Run.from_dict(
        {
            query: (
                {
                    document: float(len(by_query[query].ranked_ids) - position)
                    for position, document in enumerate(by_query[query].ranked_ids)
                }
                if by_query[query].ranked_ids
                else {NO_RESULT: 0.0}
            )
            for query in relevant
        }
    )
    raw = evaluate(qrels, run, list(metrics))
    if not isinstance(raw, dict):
        # The one-element-list case. Measured, not defensive.
        return {metrics[0]: float(raw)}
    return {name: float(value) for name, value in raw.items()}
