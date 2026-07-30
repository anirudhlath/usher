"""Liveness and readiness.

Readiness is degraded rather than binary, so a dashboard can distinguish
"down" from "running without a source".
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text

from usher.api.deps import SessionDep

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Checks nothing external by design."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: SessionDep) -> JSONResponse:
    """Readiness. Reports each dependency separately.

    Returns 503 when degraded rather than 200: no doc pins a status code
    here, so this is a deliberate call, not a plan default. A readiness
    probe's entire contract *is* the status code -- Kubernetes, Docker
    `healthcheck`, and load balancers gate on it and never parse the
    body, so a 200 "degraded" response tells every one of them "keep
    sending traffic here," which is exactly wrong.
    """
    checks: dict[str, bool] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
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
        checks["database"] = False

    # `all(checks.values())` on an empty dict is vacuously True -- guard it
    # now, before M3 adds a conditional per-source check that could leave
    # checks empty on some deployments and report "ready" having checked
    # nothing.
    is_ready = bool(checks) and all(checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "degraded", "checks": checks},
    )
