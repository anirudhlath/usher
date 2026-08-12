"""Admin routes for configured sources (PRD 07).

`GET /admin/sources/{id}/status` is the endpoint PRD 07's provisional
marker was about. It answers 200 for *every* state a configured source can
be in, including "the credentials are wrong" and "the host is unreachable"
-- those are facts about the source being described, not failures of this
request, and an admin screen has to render them side by side. 404 is
reserved for the one case that really is a failed lookup: no such source.
That is why the adapter's own `.verify()` returns a `SourceStatus` instead
of raising for an expected failure; this router is the caller that shape
exists for.

`POST /admin/sources/{id}/sync` is M9's E3, and it is the M4 boundary call
this file deferred: "there is no reconciler until M5" was already wrong by
M4, which built the reconcile pipeline and both its lanes -- `usher sync`
delivered the capability three milestones before this route existed to wire
it. **The route enqueues and returns 202. It does not reconcile.** A
synchronous route would be the first request whose honest answer is "the
upstream is down", and M8 already settled that shape for
`POST /admin/rows/regenerate`: generation -- and now a sync -- is a job.
This handler holds no reconcile pipeline and dials no upstream, asserted on
its own imports in `tests/unit/test_api_sources.py` rather than left to
review, the same shape `test_the_home_service_and_every_provider_hold_no_
source_adapter` uses one router over.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Response, status

from usher.api.deps import JobQueueDep, SourceRepositoryDep, SourceServiceDep
from usher.api.dto.problem import ProblemCode
from usher.api.dto.source import (
    SourceCreateRequest,
    SourceResponse,
    SourceStatusResponse,
    SyncTriggerResponse,
)
from usher.api.errors import ProblemException
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.credentials import SourceCredentials
from usher.ports.jobs import JobRequest
from usher.telemetry import current_traceparent

router = APIRouter(prefix="/admin/sources", tags=["admin"])

_DISABLED_DETAIL = (
    "this source is disabled; enable it before requesting a sync -- "
    "a disabled source is one an operator has parked, and the worker will "
    "decline to walk it"
)


@router.post("", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(request: SourceCreateRequest, sources: SourceServiceDep) -> SourceResponse:
    source = await sources.register(
        kind=request.kind,
        name=request.name,
        base_url=request.base_url,
        # The password crosses this layer inside the `SecretStr` it was
        # parsed into and is never unwrapped here -- the DTO field and the
        # port's field are the same type, so no `get_secret_value()` call
        # appears anywhere in `api/`. The only two in the whole path are in
        # `PostgresCredentialStore` (to encrypt) and `EmbySession` (to
        # authenticate).
        credentials=SourceCredentials(username=request.username, password=request.password),
    )
    return SourceResponse.of(source)


@router.get("", response_model=list[SourceResponse])
async def list_sources(sources: SourceServiceDep) -> list[SourceResponse]:
    return [SourceResponse.of(source) for source in await sources.list_sources()]


@router.get("/{source_id}/status", response_model=SourceStatusResponse)
async def source_status(source_id: uuid.UUID, sources: SourceServiceDep) -> SourceStatusResponse:
    result = await sources.status(source_id)
    if result is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="source not found",
        )
    return SourceStatusResponse.of(result)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: uuid.UUID, sources: SourceServiceDep) -> Response:
    if not await sources.remove(source_id):
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="source not found",
        )
    # An explicit empty `Response`, not a bare `return None`: FastAPI
    # serializes a returned `None` into a literal `null` body, which a 204
    # is not allowed to carry.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{source_id}/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_source(
    source_id: uuid.UUID,
    sources: SourceRepositoryDep,
    queue: JobQueueDep,
    kind: Literal["full", "delta"] = "delta",
) -> SyncTriggerResponse:
    """Ask for one source to be walked again. Enqueues `JobKind.SYNC` and
    returns before anything runs.

    **Two refusals, both before anything is enqueued.** 404 for a source id
    that does not exist -- read through `SourceRepository.get`, never through
    `SourceService.status`, which builds an adapter and calls `verify()`
    (`services/sources.py:161`); a lookup that dials the upstream is not a
    lookup. And 409 `not_playable` for a source whose `enabled` is `false`,
    because `enabled` is how an operator parks a source being rebuilt --
    `composition.selected_sources` skips a disabled source even when named
    explicitly, so a 202 here would promise a walk the worker will decline.
    **`not_playable`, not a minted `source_disabled`** -- V1's vocabulary is
    closed at seven (ADR-0030) and this refusal does not clear the bar for an
    eighth: both say RFC 9110 §15.5.10's *"conflict with the current state of
    the target resource, stop asking"*, and a client cannot act on the two
    differently. `detail` carries the sentence that is true of this route;
    `code` carries only the disposition, which `/play` already spells this
    way. See ADR-0030's amendment.

    **The key is `"{source_id}:{kind}"`, and `kind` is the whole of lane
    selection.** `(kind, key)` is unique on the queue, so a bare source id
    would coalesce a requested `full` walk into a pending `delta` one and
    answer 202 for a walk that never happens -- `usher.domain.jobs.JobKind.
    SYNC` has the whole argument. `kind` defaults to `delta`, the cheaper of
    the two lanes an operator reaching for this button is most often asking
    for; `full` is there for the same reason `usher sync --kind full` is.

    **`JobPriority.DEMAND`, because an operator is waiting on this the way a
    client opening an unenriched title is** -- the same rung
    `POST /admin/rows/regenerate` and the demand-promotion route already use.
    A repeat at this priority writes zero rows and is coalesced into whatever
    is already pending or running, `usher.domain.jobs.JobKind` states the
    measured table, and this route's own 202 is identical in every case for
    the same reason that one's is: `enqueue`'s return value cannot tell a
    fresh row from a promoted one, so nothing here is built to answer a
    question it cannot honestly answer.
    """
    source = await sources.get(source_id)
    if source is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="source not found",
        )
    if not source.enabled:
        raise ProblemException(
            status_code=status.HTTP_409_CONFLICT,
            code=ProblemCode.NOT_PLAYABLE,
            detail=_DISABLED_DETAIL,
        )
    key = f"{source_id}:{kind}"
    await queue.enqueue(
        [
            JobRequest(
                kind=JobKind.SYNC,
                key=key,
                priority=JobPriority.DEMAND,
                traceparent=current_traceparent(),
            )
        ]
    )
    return SyncTriggerResponse(kind=JobKind.SYNC, key=key)
