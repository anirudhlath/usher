"""Liveness and readiness.

Readiness is degraded rather than binary, so a dashboard can distinguish
"down" from "running without a source".
"""

from typing import Any, Final

from fastapi import APIRouter, Response
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.api.deps import LaneSupervisorDep, SessionDep
from usher.api.dto.health import (
    LaneReport,
    LivenessResponse,
    ReadinessChecks,
    ReadinessResponse,
)
from usher.db.migrations.status import code_head_revision, database_revision

router = APIRouter(tags=["meta"])

#: **The one non-2xx in this API that is not a problem document**, declared so
#: that is a fact `/openapi.json` states rather than one a reader has to infer
#: from its absence. This probe's real consumers -- Kubernetes, Docker
#: `healthcheck`, load balancers -- gate on the status code and never parse the
#: body, so the 503 keeps `ReadinessResponse` and reports *which* check failed
#: instead of naming a code. A2 exempted it, ADR-0030 ruled on it, and
#: `tests/unit/test_api_openapi.py`'s exemption tuple asserts this shape rather
#: than skipping the status.
_DEGRADED: Final[dict[int | str, dict[str, Any]]] = {
    503: {
        "model": ReadinessResponse,
        "description": "At least one readiness check failed; `checks` says which.",
    },
}


@router.get("/health", response_model=LivenessResponse)
async def health() -> LivenessResponse:
    """Liveness. Checks nothing external by design."""
    return LivenessResponse(status="ok")


async def _check_database(session: AsyncSession) -> bool:
    try:
        await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        # Rolling back here, not just catching, matters beyond hygiene:
        # get_session's own commit-on-success (see deps.py) runs right
        # after this handler returns, and committing a session left mid a
        # failed statement can raise PendingRollbackError -- verified
        # directly that an explicit rollback here avoids that. Logging the
        # exception is safe: confirmed directly that neither str() nor
        # repr() of a connection failure leaks the DSN password.
        logger.warning(f"readiness check failed: database unreachable: {exc}")
        await session.rollback()
        return False


async def _check_migrations(session: AsyncSession) -> bool:
    """PRD 08: "the app refuses to serve on a schema mismatch rather than
    guessing." `alembic upgrade head && uvicorn ...` (Task 13) runs
    migrations on container start, but is not itself a mismatch check: a
    stale image running an older migration chain against a
    newer-than-expected database (or vice versa) would otherwise serve
    happily. Only called once `_check_database` has already succeeded --
    a database that can't be reached can't have its migration state read
    either, and attempting to would hit the exact PendingRollbackError
    class of bug `_check_database`'s own rollback avoids.
    """
    try:
        db_revision = await database_revision(session)
        code_revision = code_head_revision()
        ok = code_revision is not None and db_revision == code_revision
        if not ok:
            logger.warning(
                "readiness check failed: migration mismatch "
                f"(database at {db_revision!r}, code expects {code_revision!r})"
            )
        return ok
    except Exception as exc:
        logger.warning(f"readiness check failed: could not read migration state: {exc}")
        await session.rollback()
        return False


@router.get("/health/ready", response_model=ReadinessResponse, responses=_DEGRADED)
async def ready(
    session: SessionDep, lanes: LaneSupervisorDep, response: Response
) -> ReadinessResponse:
    """Readiness. Reports each dependency separately.

    Sets the response status to 503 when degraded rather than leaving the
    default 200: no doc pins a status code here, so this is a deliberate
    call, not a plan default. A readiness probe's entire contract *is*
    the status code -- Kubernetes, Docker `healthcheck`, and load
    balancers gate on it and never parse the body, so a 200 "degraded"
    response tells every one of them "keep sending traffic here," which
    is exactly wrong.

    Takes the `Response` object as a parameter and mutates its
    `status_code` rather than constructing a `JSONResponse` directly, so
    FastAPI still runs this handler's return value through
    `response_model` normally instead of the caller being responsible for
    matching that shape by hand (FastAPI's own docs: returning a
    `Response` directly "bypasses automatic data filtering and
    serialization").
    """
    database_ok = await _check_database(session)
    migrations_ok = await _check_migrations(session) if database_ok else False
    checks = ReadinessChecks(database=database_ok, migrations=migrations_ok)

    # all(...) over the model's own fields, not a hand-maintained boolean
    # expression -- guards the same "reported ready having checked
    # nothing" risk a bare dict had, but structurally: a checks dict could
    # accidentally end up empty; a checks *model* can't, since every field
    # is required, so M3 adding a per-source check is a mypy error at
    # every construction site if forgotten, not a silent gap.
    is_ready = all(checks.model_dump().values())
    response.status_code = 200 if is_ready else 503
    # `checks` alone, exactly as before. The lanes are reported below and
    # are deliberately not in this expression -- see `LaneReport`. PRD 08
    # said readiness "reports Postgres, migration state, and per-source
    # connectivity"; the third is corrected to reported-in-the-body, never
    # in the status code, because a 503 for an unreachable Emby takes this
    # process out of a load balancer for a reason restarting it cannot fix.
    # ⚠️ That is now the *whole* argument. This comment used to lead with
    # "one upstream request per 2 s Docker poll against a server measured
    # at 1-5 s per request" -- a figure nobody had ever taken. M10 S1 puts
    # the probe at **0.1253 s**
    # (2026-08-15, `.claude/rules/emby-push-and-ingest.md`) -- 6% of the poll
    # interval per source, which is a cost and not an argument. The
    # load-balancer half was always the load-bearing one and is left standing
    # alone rather than propped up by a number nobody had taken.
    #
    # Free to report and therefore worth reporting: all five of these read
    # in-memory state off the supervisor -- task state for the first three,
    # and for the last two the number `JobWorker.recover()` already returned
    # rather than a `SELECT count(*) ... WHERE status = 'running'` per 2 s
    # poll over a table with no index on that value. So this endpoint still
    # makes **no upstream request and issues no third statement at all**.
    return ReadinessResponse(
        status="ready" if is_ready else "degraded",
        checks=checks,
        lanes=LaneReport(
            push=lanes.running_sources(),
            worker=lanes.worker_running(),
            crashed_sources=lanes.crashed_sources(),
            recovered_claims=lanes.recovered_claims(),
            recovered_at=lanes.recovered_at(),
        ),
    )
