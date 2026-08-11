"""Admin routes for configured sources (PRD 07).

Four of the five: `POST /admin/sources/{id}/sync` triggers a reconcile, and
there is no reconciler until M5.

`GET /admin/sources/{id}/status` is the endpoint PRD 07's provisional
marker was about. It answers 200 for *every* state a configured source can
be in, including "the credentials are wrong" and "the host is unreachable"
-- those are facts about the source being described, not failures of this
request, and an admin screen has to render them side by side. 404 is
reserved for the one case that really is a failed lookup: no such source.
That is why `SourceAdapter.verify()` returns a `SourceStatus` instead of
raising for an expected failure; this router is the caller that shape
exists for.
"""

import uuid

from fastapi import APIRouter, Response, status

from usher.api.deps import SourceServiceDep
from usher.api.dto.problem import ProblemCode
from usher.api.dto.source import SourceCreateRequest, SourceResponse, SourceStatusResponse
from usher.api.errors import ProblemException
from usher.ports.credentials import SourceCredentials

router = APIRouter(prefix="/admin/sources", tags=["admin"])


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
