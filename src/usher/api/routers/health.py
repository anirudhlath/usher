"""Liveness and readiness."""

from typing import Any, Final

from fastapi import APIRouter, Response
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher import __version__
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
#: that it is a fact `/openapi.json` states rather than one a reader has to
#: infer from its absence.
_DEGRADED: Final[dict[int | str, dict[str, Any]]] = {
    503: {
        "model": ReadinessResponse,
        "description": "At least one readiness check failed; `checks` says which.",
    },
}


@router.get("/health", response_model=LivenessResponse)
async def health() -> LivenessResponse:
    """Liveness.

    Checks nothing external by design.
    """
    return LivenessResponse(status="ok", version=__version__)


async def _check_database(session: AsyncSession) -> bool:
    try:
        await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        # Rolling back here, not just catching: `get_session`'s commit-on-success
        # runs right after this handler returns, and committing a session left mid
        # failed statement raises `PendingRollbackError`.
        logger.warning(f"readiness check failed: database unreachable: {exc}")
        await session.rollback()
        return False


async def _check_migrations(session: AsyncSession) -> bool:
    """PRD 08's "refuses to serve on a schema mismatch rather than guessing".

    `alembic upgrade head` on container start is not itself a mismatch check: a
    stale image running an older migration chain against a newer-than-expected
    database, or the reverse, would otherwise serve happily.

    Only called once `_check_database` has succeeded -- a database that cannot be
    reached cannot have its migration state read, and attempting it would hit the
    `PendingRollbackError` `_check_database`'s own rollback avoids.
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
    """Readiness, reporting each dependency separately.

    **503 when degraded, 200 when ready**, and the status code is the whole
    contract: Kubernetes, Docker `healthcheck` and load balancers gate on it and
    never parse the body. `checks` names the dependency that failed; `lanes` is
    reported and never gated on.
    """
    database_ok = await _check_database(session)
    migrations_ok = await _check_migrations(session) if database_ok else False
    checks = ReadinessChecks(database=database_ok, migrations=migrations_ok)

    # `all(...)` over the model's own fields, not a hand-maintained boolean: a checks
    # dict could end up empty and report ready having checked nothing, a checks *model*
    # cannot, since every field is required.
    is_ready = all(checks.model_dump().values())
    response.status_code = 200 if is_ready else 503
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
