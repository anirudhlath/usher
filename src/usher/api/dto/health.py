"""Response DTOs for the health endpoints."""

from datetime import datetime

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    """Liveness, and the one fact an operator needs during an incident.

    **`version` is here and not on `ReadinessChecks`**, for the reason that
    model's own docstring gives: `ready` is
    `all(self.model_dump().values())`, so every field added there becomes part
    of the status code, and a version string is not a check.

    ⚠️ **Publishing it on an unauthenticated probe is a real objection and it
    is answered rather than skipped.** A version string is a free answer to
    "which CVEs apply". Three things settle it, and none of them is "nobody
    will look": this repository is public and MIT, so the tag, the changelog
    and the lockfile are already readable by the same person;
    `/openapi.json` is served unauthenticated and describes the whole surface,
    which is a strictly larger disclosure; and what it buys is the one cheap
    way to tell which image is actually running, which is exactly what an
    incident needs. **An operator who disagrees can drop `/health` at the
    reverse proxy** -- one location block, and it costs them nothing, because
    the compose healthcheck targets `/health/ready`.
    """

    status: str
    version: str


class ReadinessChecks(BaseModel):
    """The checks readiness **gates on**, and nothing else.

    `ready` is `all(self.model_dump().values())`, so every field added here
    becomes part of the status code automatically. That is the property
    that makes this model the right place for `database` and `migrations`
    and the wrong place for anything about a source -- see `LaneReport`.
    """

    database: bool
    migrations: bool


class LaneReport(BaseModel):
    """Which background lanes this process is running."""

    # ⚠️ **A comment, not the docstring, and that placement is the finding.**
    # `LaneReport` is a pydantic model, so pydantic emits its class docstring as the
    # JSON-Schema `description` and FastAPI publishes it at `/openapi.json`.
    push: list[str]
    worker: bool

    # The lanes whose task has *finished*, which is not a state a healthy lane reaches
    # -- `PushSupervisor.run` returns only after the failure ceiling and `_guard`
    # catches everything else.
    crashed_sources: list[str]

    # How many abandoned claims **this process** has taken back since it started -- the
    # total `JobWorker.recover()` returned, summed, never a fresh query.
    recovered_claims: int | None

    # The instant of the last recovery pass that found something -- `None`
    # while this process has recovered nothing, whether because it never
    # asked or because there was nothing to take back. Deliberately not
    # "when recovery last ran": a probe polled every 2 s would then carry a
    # timestamp that moves on its own and says nothing.
    recovered_at: datetime | None


class ReadinessResponse(BaseModel):
    status: str
    checks: ReadinessChecks
    lanes: LaneReport
