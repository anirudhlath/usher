"""Admin routes for the bulk catalog bootstrap (PRD 07, PRD 04)."""

from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import BootstrapReportDep, JobQueueDep
from usher.api.dto.bootstrap import BootstrapStatusResponse, BootstrapTriggerResponse
from usher.api.dto.problem import ProblemResponse
from usher.domain.bootstrap import BootstrapPhase
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.jobs import JobRequest
from usher.telemetry import current_traceparent

router = APIRouter(prefix="/admin/bootstrap", tags=["admin"])

#: The trigger's one refusal, and it is FastAPI's rather than this module's: a
#: `{phase}` outside `BootstrapPhase`. Declared rather than left automatic,
#: because the automatic one names `HTTPValidationError` while `api/errors.py`
#: answers an RFC 9457 document carrying the same error list under `errors`.
#: `tests/unit/test_api_openapi.py` holds both halves.
_TRIGGER_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ProblemResponse, "description": "`{phase}` is not a bootstrap phase."},
}


@router.get("/status", response_model=BootstrapStatusResponse)
async def bootstrap_status(report: BootstrapReportDep) -> BootstrapStatusResponse:
    """What every dataset's import has done.

    the catalog's size, the genome's coverage, and whether the stored tag vocabulary can
    name its lanes.

    **Declared before `POST /{phase}` and safe either way.** The two differ by
    method, so no request can match both; the order here is for a reader.

    **One report, two surfaces.** `usher bootstrap-status` prints the same
    `BootstrapReport` this serialises, and the vocabulary verdict crosses the
    wire as a `VocabularyState` member rather than as the CLI's sentence --
    what moves into the report is the *decision*, or this route ends up
    serialising English and a client ends up parsing it.

    ⚠️ **Two aggregate reads, roughly a third of a second on a real
    1.27M-title catalog** (`BootstrapReport`'s docstring carries the
    measurement). That is priced for an admin screen an operator opens on
    purpose. There is no cache, deliberately, and no other route should copy
    this shape.
    """
    return BootstrapStatusResponse.of(report)


@router.post(
    "/{phase}",
    response_model=BootstrapTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_TRIGGER_FAILURES,
)
async def start_bootstrap(phase: BootstrapPhase, queue: JobQueueDep) -> BootstrapTriggerResponse:
    """Ask for one bulk-import phase to run.

    Enqueues `JobKind.BOOTSTRAP` and returns before a byte is read.
    """
    await queue.enqueue(
        [
            JobRequest(
                kind=JobKind.BOOTSTRAP,
                key=phase.value,
                priority=JobPriority.DEMAND,
                traceparent=current_traceparent(),
            )
        ]
    )
    return BootstrapTriggerResponse(kind=JobKind.BOOTSTRAP, key=phase.value)
