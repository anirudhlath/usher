"""IR scoring. **The only module in this project that imports `ranx`.**"""

import importlib.metadata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from usher.eval.errors import EvalDependencyMissing, EvalRefused

try:
    from ranx import Qrels, Run, evaluate
except ImportError as exc:  # pragma: no cover - exercised by the CLI preflight
    raise EvalDependencyMissing("ranx") from exc

# The stand-in document for a query that returned nothing at all.
NO_RESULT = "__no_result__"


@dataclass(frozen=True, slots=True)
class Ranking:
    """What one query returned, best first. Empty is a legitimate answer."""

    query_id: str
    ranked_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        repeated = sorted({one for one, times in Counter(self.ranked_ids).items() if times > 1})
        if repeated:
            raise EvalRefused(
                f"the ranking for query {self.query_id!r} repeats "
                f"{len(repeated)} document id(s), e.g. {repeated[0]!r}: {repeated} -- "
                "scoring keeps only each id's worst position, so a duplicate is "
                "reported as a miss the surface did not have"
            )


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

    **Four guards, and the fourth is the same failure mode arriving through the
    judgements rather than through the rankings**: a judgement naming
    `NO_RESULT` is satisfied by the substitution this function makes for an
    empty ranking, so the query that returned nothing scores 1.0. Refused, with
    the measurement on the guard itself.
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
    # The fourth guard, and the one whose damage is a *perfect* score rather than a
    # wrong one.
    sentinel = sorted(query for query, document in relevant.items() if document == NO_RESULT)
    if sentinel:
        raise EvalRefused(
            f"{len(sentinel)} judgement(s) name the empty-result sentinel "
            f"{NO_RESULT!r} as the relevant document, e.g. {sentinel[0]!r}: {sentinel} -- "
            "an empty ranking is scored against that same sentinel, so the query "
            "that returned nothing would score a perfect hit"
        )

    qrels = Qrels.from_dict({query: {document: 1} for query, document in relevant.items()})
    # The descending score is what carries the ranking.
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
