"""Admin routes for the bulk catalog bootstrap (PRD 07, PRD 04).

`POST /admin/bootstrap/{phase}` is M9's E5, and it is M2's last boundary
call: the bulk importers have been runnable since M2 and only as a separate
process, which is the fact PRD 07 and `ports/events.py` both cite for why
`bootstrap.progress` has never had a producer. **The route enqueues and
returns 202. It imports nothing.** A `--phase all` run is 74.8 s against warm
on-disk dumps and materially longer cold -- `CachedDatasetFile.ensure_local`
keys on the upstream token and IMDb regenerates daily, so a request here can
trigger a 224 MB download -- and `api/deps.py`'s `get_reconcile_service`
already records the general argument: a route that drove a multi-minute walk
inside one request would be committing the request's session repeatedly
before the handler returned.

**There is no refusal here beyond the phase's own type**, and that is a
statement about what can be known at request time rather than an omission.
Every precondition a bootstrap has -- an empty catalog for `credit-names`,
`aliases` and `movielens`, a writable `USHER_BULK_DATA_DIR`, another process
already owning the dataset's `import_runs` row -- is a fact about the instant
the *worker* claims the job, which the queue may hold for the length of
whatever is ahead of it. Checking any of them here would answer a question
about a moment that has passed by the time it matters, so each is checked
where it is true: `composition.run_bootstrap` refuses an empty catalog per
phase, and `BootstrapService._concede_to_other_owner` answers the ownership
race by touching the winner's checkpoint not at all.

This module names no `BulkDataset` and no `BootstrapService`, asserted on its
own imports in `tests/unit/test_api_bootstrap.py` rather than left to review
-- the same shape `test_the_watch_router_and_its_service_hold_no_source_
adapter` uses two routers over.
"""

from fastapi import APIRouter, status

from usher.api.deps import JobQueueDep
from usher.api.dto.bootstrap import BootstrapTriggerResponse
from usher.domain.bootstrap import BootstrapPhase
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.jobs import JobRequest
from usher.telemetry import current_traceparent

router = APIRouter(prefix="/admin/bootstrap", tags=["admin"])


@router.post(
    "/{phase}",
    response_model=BootstrapTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_bootstrap(phase: BootstrapPhase, queue: JobQueueDep) -> BootstrapTriggerResponse:
    """Ask for one bulk-import phase to run. Enqueues `JobKind.BOOTSTRAP` and
    returns before a byte is read.

    **`phase` is a `BootstrapPhase` rather than a `str`**, which is the whole
    of the refusal: FastAPI validates the path parameter against the enum, so
    `/admin/bootstrap/embeddings` is a **422** in V1's envelope carrying
    `validation_failed`, `/openapi.json` describes the real seven-member set,
    and the CLI's `--phase` choices are derived from the same members. A
    membership test inside this function would have had to choose a status
    for itself, and the obvious choice -- 404 -- is wrong: the route exists
    and the client asked it for something outside its vocabulary.

    **The key is the phase's own wire value.** `(kind, key)` is unique, so
    two presses of *imdb* while one is running are one job; `all` and `imdb`
    are two keys and therefore two jobs, which is safe here in a way the
    equivalent would not be for `sync` because every phase is resumable and
    idempotent -- `usher.domain.jobs.JobKind` carries that argument in full.

    **`JobPriority.DEMAND`**, the rung `POST /admin/rows/regenerate` and
    `POST /admin/sources/{id}/sync` already use: an operator is waiting on
    this the way a client opening an unenriched title is. It is also the rung
    that makes the head-of-line cost visible rather than hidden -- PRD 08's
    job-reliability section prices a triggered walk holding the single
    `JobWorker` lane, and a bootstrap is the longest unit of work in this
    system. A repeat at this priority writes zero rows and is coalesced,
    which is why this 202 is identical in every case: `enqueue`'s return
    value cannot tell a fresh row from a promoted one, so nothing here is
    built to answer a question it cannot answer honestly.
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
