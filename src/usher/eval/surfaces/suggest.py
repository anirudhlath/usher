"""The typo-tolerance surface: PRD 05, ADR-0002, ADR-0031.

Drives the **real** `SearchService.suggest` through the real composition
root. It reimplements no part of either tier -- an eval that reimplements the
thing it measures measures itself.

**Both tiers are measured separately and never averaged.** ADR-0031 ships a
btree exact-prefix probe at p50 0.6 ms with 1.9% typo recall and a trigram +
`levenshtein_less_equal` path at p50 33.6 ms that carries the tolerance.
Neither is a degraded form of the other, so a mean over them describes
neither -- the same argument `SuggestTier` exists for rather than a
`typo_tolerant: bool`.

🔴 **This harness resolves no household, and since M10's J2 that is what keeps
it out of `search_queries`.** `SearchService.suggest` can write one row per
answered keystroke -- `USHER_SEARCH_SUGGEST_ANALYTICS`, off by default, so most
deployments are protected by the switch as well -- and it writes none for a
call carrying no `user_id` whatever the switch says, which is the half that
holds on the deployment that turned it on. That is
PRD 10's *"a search with no household"* exclusion, which the search path has
always had and which nothing shipped could reach until now. `tier_suggester`
below is a third caller of `suggest` that the analytics amendment was not
planned around: it holds a **real** `SearchAnalytics` over a real
`PostgresSearchQueryRepository`, because it builds the real pipeline through
the real composition root, so the only thing standing between
`usher eval suggest --full` and thousands of rows of evaluation traffic wearing
a household's clothes is that this module never names one.

**So do not add one.** A household here would make every bar this harness
scores a contributor to the table those bars are meant to be independent of --
and PRD 10's *"which absence means what"* table would gain a sixth absence
nobody could name. `tests/integration/test_search_analytics.py::
test_a_suggest_with_no_household_writes_no_row_and_the_eval_harness_is_that_caller`
drives this function against a real database and asserts the table stays empty,
with a household-carrying control beside it, because an untested absence is
indistinguishable from an oversight.
"""

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import Settings
from usher.eval.goldens.suggest import TypoCase
from usher.eval.metrics.ir import Ranking

# What a tier looks like to this module: a probe and a limit in, title ids out,
# best first. Narrow on purpose -- it is everything the measurement needs and
# nothing else, so the unit tests need no database and no service graph.
Suggester = Callable[[str, int], Awaitable[list[uuid.UUID]]]


@dataclass(frozen=True, slots=True)
class SurfaceRun:
    """One tier's answers to the whole golden set."""

    relevant: Mapping[str, str]
    rankings: tuple[Ranking, ...]
    latencies_ms: tuple[float, ...]
    strata: Mapping[str, tuple[str, ...]]

    def strata_for(self, query_id: str) -> tuple[str, ...]:
        return self.strata[query_id]


async def rank_cases(
    cases: Sequence[TypoCase], suggester: Suggester, *, limit: int = 5
) -> SurfaceRun:
    """Ask one tier every case, in case order.

    **Errors are not caught.** A tier that is down produces an exception, not
    a run of misses: a zero and an absence are different facts and only one of
    them is a regression. `runner.py` turns the exception into
    `skipped-with-reason`.
    """
    relevant: dict[str, str] = {}
    rankings: list[Ranking] = []
    latencies: list[float] = []
    strata: dict[str, tuple[str, ...]] = {}
    for case in cases:
        started = time.perf_counter()
        hits = await suggester(case.probe, limit)
        latencies.append((time.perf_counter() - started) * 1000.0)
        relevant[case.query_id] = str(case.title_id)
        # Order preserved: neither tier is re-ranked by `SearchService.suggest`
        # (each already ordered its own answer), so reordering here would make
        # MRR a measurement of this module.
        rankings.append(Ranking(case.query_id, tuple(str(hit) for hit in hits)))
        strata[case.query_id] = (
            "all",
            f"band={case.band}",
            f"typo_class={case.typo_class}",
        )
    return SurfaceRun(
        relevant=relevant,
        rankings=tuple(rankings),
        latencies_ms=tuple(latencies),
        strata=strata,
    )


def tier_suggester(session: AsyncSession, settings: Settings, tier: str) -> Suggester:
    """Bind one real tier, through the real composition root.

    Imported here rather than at module scope for the reason `cli.py` imports
    `uvicorn` inside its own branch: nothing about generating goldens should
    pay for building a service graph.
    """
    from usher.composition import build_pipeline
    from usher.ports.search import SuggestTier

    pipeline = build_pipeline(session, settings)
    chosen = SuggestTier(tier)

    async def ask(probe: str, limit: int) -> list[uuid.UUID]:
        results = await pipeline.search.suggest(probe, limit=limit, tier=chosen)
        return [result.title_id for result in results]

    return ask
