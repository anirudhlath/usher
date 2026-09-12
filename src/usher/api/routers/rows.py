"""PRD 07's admin row actions: `POST /admin/rows/regenerate` (M8), and `GET`/`PUT
/admin/rows/providers` (M9).
"""

from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import (
    DefaultUserIdDep,
    JobQueueDep,
    RowCacheDep,
    RowProviderSettingsRepositoryDep,
)
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.rows import RegenerateResponse, RowProviderResponse, RowProviderUpdate
from usher.api.errors import ProblemException
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.jobs import JobRequest
from usher.services.rows import ROW_PROVIDERS, row_provider_settings
from usher.telemetry import current_traceparent

router = APIRouter(prefix="/admin/rows", tags=["admin"])

# : What `/openapi.json` says the toggle answers when it fails.
_TOGGLE_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No provider is registered under that slug."},
    422: {"model": ProblemResponse, "description": "The request was rejected."},
}


@router.post(
    "/regenerate",
    response_model=RegenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_rows(queue: JobQueueDep, user_id: DefaultUserIdDep) -> RegenerateResponse:
    """Ask for this household's curated rows to be generated again.

    The key is the household and never a `generation_id` or a timestamp:
    `(kind, key)` is unique, so keying per request would deduplicate nothing
    and would buy one completion per press (`JobKind.CURATE`).

    `enqueue`'s return value is discarded, and that is a decision rather than
    an oversight -- it answers 1 for a job it created *and* for one it merely
    promoted, and 0 for a repeat already at this priority, so it cannot be
    rendered into a response without inviting a reading it does not support.

    `get_session` commits this as it commits any other request, which is what
    makes the row durable rather than a flush the response outlives.
    """
    await queue.enqueue(
        [
            JobRequest(
                kind=JobKind.CURATE,
                key=str(user_id),
                # The same rung `api/routers/titles.py`'s demand promotion uses
                # (`services/titles.py`): a human is waiting on this, so it goes in
                # front of the nightly sweep.
                priority=JobPriority.DEMAND,
                # PRD 10's "why did the title I just opened take 45 seconds",
                # for the one kind whose answer is measured in dollars: the
                # worker's span links back to this request, minutes later.
                traceparent=current_traceparent(),
            )
        ]
    )
    return RegenerateResponse(kind=JobKind.CURATE, key=str(user_id))


@router.get("/providers", response_model=list[RowProviderResponse])
async def list_row_providers(
    provider_settings: RowProviderSettingsRepositoryDep,
) -> list[RowProviderResponse]:
    """Every registered row provider, and whether it composes.

    **Derived from `ROW_PROVIDERS`, never from a literal and never from the
    table.** The registry is the set of providers that exist -- a provider that
    is not registered is dead code (boundary call 9) -- and
    `row_provider_settings` holds only what an operator has touched, so a
    listing read off the *table* would answer nothing on a fresh install and
    would grow a row at a time as somebody clicked. The join is a **left** one
    in `services/rows/__init__.py`, which is also where the default lives:
    absence means enabled, and this endpoint is where a caller would otherwise
    be tempted to spell that for itself.

    In registry order rather than sorted by slug, so an operator's screen is
    the same order as `usher home`'s report and does not reshuffle when a
    provider is renamed.
    """
    return [
        RowProviderResponse(slug=one.slug, enabled=one.enabled)
        for one in row_provider_settings(await provider_settings.overrides())
    ]


@router.put("/providers/{slug}", response_model=RowProviderResponse, responses=_TOGGLE_FAILURES)
async def set_row_provider_enabled(
    slug: str,
    update: RowProviderUpdate,
    provider_settings: RowProviderSettingsRepositoryDep,
    cache: RowCacheDep,
) -> RowProviderResponse:
    """Switch one provider on or off for this deployment."""
    if slug not in {provider.slug_prefix for provider in ROW_PROVIDERS}:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="no row provider is registered under that slug",
        )
    await provider_settings.set_enabled(slug, enabled=update.enabled)
    cache.clear()
    # Built from the slug the registry matched and the value just written,
    # rather than re-read: `set_enabled` flushes without committing, so a
    # read-back would answer out of this request's own uncommitted transaction
    # and could only ever agree with itself. What proves the write landed is
    # the *next* request's `GET`, which is the assertion both test files make.
    return RowProviderResponse(slug=slug, enabled=update.enabled)
