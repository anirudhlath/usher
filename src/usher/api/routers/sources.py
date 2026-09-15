"""Admin routes for configured sources (PRD 07)."""

import uuid
from typing import Any, Final, Literal

from fastapi import APIRouter, Response, status

from usher.api.deps import JobQueueDep, SourceRepositoryDep, SourceServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
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

#: What `/openapi.json` says these routes answer when they fail.
_REJECTED: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ProblemResponse, "description": "The request was rejected."},
}

#: A `404` for a source id no row carries, and nothing else: the status route
#: answers `200` for every state a *configured* source can be in, including a
#: rejected credential and an unreachable host, because those are facts about
#: the source rather than failures of the request.
_SOURCE_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such source."},
    **_REJECTED,
}

#: The sync trigger adds a `409` for a source an operator has disabled --
#: `not_playable`, reused rather than minted, because it says the same thing:
#: stop asking until that state changes.
_SYNC_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    409: {"model": ProblemResponse, "description": "This source is disabled."},
    **_SOURCE_FAILURES,
}

_DISABLED_DETAIL = (
    "this source is disabled; enable it before requesting a sync -- "
    "a disabled source is one an operator has parked, and the worker will "
    "decline to walk it"
)


@router.post(
    "",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_REJECTED,
)
async def create_source(request: SourceCreateRequest, sources: SourceServiceDep) -> SourceResponse:
    source = await sources.register(
        kind=request.kind,
        name=request.name,
        base_url=request.base_url,
        # The password crosses this layer inside the `SecretStr` it was parsed into and
        # is never unwrapped here -- the DTO field and the port's field are the same
        # type, so no `get_secret_value()` call appears anywhere in `api/`.
        credentials=SourceCredentials(username=request.username, password=request.password),
    )
    return SourceResponse.of(source)


@router.get("", response_model=list[SourceResponse])
async def list_sources(sources: SourceServiceDep) -> list[SourceResponse]:
    return [SourceResponse.of(source) for source in await sources.list_sources()]


@router.get("/{source_id}/status", response_model=SourceStatusResponse, responses=_SOURCE_FAILURES)
async def source_status(source_id: uuid.UUID, sources: SourceServiceDep) -> SourceStatusResponse:
    result = await sources.status(source_id)
    if result is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="source not found",
        )
    return SourceStatusResponse.of(result)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_SOURCE_FAILURES)
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
    responses=_SYNC_FAILURES,
)
async def sync_source(
    source_id: uuid.UUID,
    sources: SourceRepositoryDep,
    queue: JobQueueDep,
    kind: Literal["full", "delta"] = "delta",
) -> SyncTriggerResponse:
    """Ask for one source to be walked again.

    Enqueues `JobKind.SYNC` and returns before anything runs.
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
