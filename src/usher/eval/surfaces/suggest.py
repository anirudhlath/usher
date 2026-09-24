"""The typo-tolerance surface (PRD 05)."""

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import Settings
from usher.eval.goldens.suggest import TypoCase
from usher.eval.metrics.ir import Ranking

# What a tier looks like to this module: a probe and a limit in, title ids out,
# best first. Narrow on purpose -- everything the scoring needs and nothing
# else, so the unit tests need no database and no service graph.
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
        # MRR a property of this module rather than of the tier.
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
