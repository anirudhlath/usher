"""Liveness and readiness.

Readiness is degraded rather than binary, so a dashboard can distinguish
"down" from "running without a source".
"""

from fastapi import APIRouter
from sqlalchemy import text

from usher.api.deps import SessionDep

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Checks nothing external by design."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: SessionDep) -> dict[str, object]:
    """Readiness. Reports each dependency separately."""
    checks: dict[str, bool] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    return {
        "status": "ready" if all(checks.values()) else "degraded",
        "checks": checks,
    }
