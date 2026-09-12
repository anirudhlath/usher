"""A `SimilarityScope` over one service, and the rebuild job that reads it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from usher.services.similar import NeighborRebuildJob, SimilarityService


def rebuild_job(
    service: SimilarityService, *, period: timedelta = timedelta(hours=24)
) -> NeighborRebuildJob:
    """`NeighborRebuildJob` over a scope that yields `service` and commits
    nothing on exit, which is the shipped scope's shape.

    The default period is the shipped `USHER_SIMILAR_REBUILD_PERIOD_HOURS`;
    no case that drives `run()` directly is about it.
    """

    @asynccontextmanager
    async def scope() -> AsyncIterator[SimilarityService]:
        yield service

    return NeighborRebuildJob(scope, period=period)
