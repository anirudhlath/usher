"""Liveness and readiness.

Readiness is degraded rather than binary, so a dashboard can distinguish
"down" from "running without a source".
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.api.deps import SessionDep
from usher.db.migrations.status import code_head_revision, database_revision

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Checks nothing external by design."""
    return {"status": "ok"}


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
    checks = {"database": await _check_database(session)}
    checks["migrations"] = await _check_migrations(session) if checks["database"] else False

    # `all(checks.values())` on an empty dict is vacuously True -- guard it
    # now, before M3 adds a conditional per-source check that could leave
    # checks empty on some deployments and report "ready" having checked
    # nothing.
    is_ready = bool(checks) and all(checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "degraded", "checks": checks},
    )
