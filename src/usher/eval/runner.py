"""generate -> run -> score -> compare -> record.

**Four of the five verdicts here are not failures**, and keeping them apart is
what stops the harness becoming a red everyone learns to ignore -- the failure
mode `prd-maintenance.md` already records against a check nobody trusts.
"""

from collections.abc import Sequence

from usher.eval.bars import BarSet, Judgement
from usher.eval.ledger import ScoreRecord
from usher.eval.metrics.ir import score as score_ir
from usher.eval.surfaces.suggest import SurfaceRun
from usher.eval.verdicts import Verdict

# What every surface reports. `recall@5` over one relevant document is the
# gate's own hit rate, which is what makes E1 comparable with 2026-08-03.
_METRICS = ("recall@5", "mrr")
# ranx's spelling on the left, the ledger's on the right. Two vocabularies,
# and the boundary between them is here so `@` never reaches a column name or
# a Grafana query.
_METRIC_NAMES = {"recall@5": "recall_at_5", "mrr": "mrr"}


def _quantile(ordered: Sequence[float], q: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def score_surface(run: SurfaceRun, *, tier: str, bars: BarSet) -> tuple[ScoreRecord, ...]:
    """Score one tier's run, every stratum separately.

    **Strata are never averaged together.** A mean over the five length bands
    describes none of them -- ADR-0002 measured 27.8% on 2-4 characters
    against 95-100% above 8, and the mean of those two is a number about no
    query anyone types.
    """
    by_query = {ranking.query_id: ranking for ranking in run.rankings}
    strata: dict[str, list[str]] = {}
    for query_id, names in run.strata.items():
        for name in names:
            strata.setdefault(name, []).append(query_id)

    records: list[ScoreRecord] = []
    for stratum, query_ids in sorted(strata.items()):
        relevant = {query_id: run.relevant[query_id] for query_id in query_ids}
        rankings = [by_query[query_id] for query_id in query_ids]
        values = score_ir(relevant, rankings, list(_METRICS))
        for raw_name, value in values.items():
            metric = _METRIC_NAMES[raw_name]
            records.append(_record(bars, tier, metric, stratum, value, len(query_ids)))

    # Latency at "all" only. Per-band latency is a real question and it is
    # `scripts/measure_suggest_tiers.py`'s, which owns the quiet-check a
    # latency claim needs; E1 records one distribution so a catastrophic
    # regression is visible, not so it can be tuned against.
    ordered = sorted(run.latencies_ms)
    for metric, value in (
        ("latency_p50_ms", _quantile(ordered, 0.50)),
        ("latency_p95_ms", _quantile(ordered, 0.95)),
        ("latency_max_ms", ordered[-1] if ordered else 0.0),
    ):
        records.append(_record(bars, tier, metric, "all", value, len(ordered)))
    return tuple(records)


def _record(
    bars: BarSet, tier: str, metric: str, stratum: str, value: float, observations: int
) -> ScoreRecord:
    bar, judgement = bars.judge_with_bar(
        surface="suggest", tier=tier, metric=metric, stratum=stratum, value=value
    )
    return ScoreRecord(
        surface="suggest",
        tier=tier,
        metric=metric,
        stratum=stratum,
        value=float(value),
        observations=observations,
        judgement=judgement,
        bar_kind=None if bar is None else bar.kind,
        bar_low=None if bar is None else bar.low,
        bar_high=None if bar is None else bar.high,
    )


def verdict_for(records: Sequence[ScoreRecord]) -> Verdict:
    """One verdict for a whole run.

    **Any FAIL makes the run FAIL.** Nothing else does: PENDING and UNBARRED
    are statements that no bar was faced, and a run that reported PASS on the
    strength of them would be claiming to have faced one.
    """
    judgements = {record.judgement for record in records}
    if Judgement.FAIL in judgements:
        return Verdict.FAIL
    if Judgement.PASS in judgements:
        return Verdict.PASS
    if Judgement.PENDING in judgements:
        return Verdict.PENDING
    return Verdict.UNBARRED
