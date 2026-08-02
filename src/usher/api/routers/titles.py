"""`GET /titles/{id}` -- PRD 03's read-through, at the boundary.

**This route cannot fail because a source is down**, and that is structural
rather than defensive: `TitleReadService` holds no `SourceAdapter`, so there
is no call to catch. PRD 08: "a degraded subsystem narrows functionality; it
never fails a request local state can answer." What a degraded source does
change is the *width* of the answer -- a copy the nightly sweep retracted is
rendered with `available: false` rather than dropped -- and never the status
code. That is also why M5 ships no RFC 9457 envelope: PRD 07's worked example
of one is `503 source_unavailable`, and this route has no 503 to give a
`code` to.

**It is a `GET` that writes**, once and deliberately: opening an unenriched
title enqueues its `enrich` job at `JobPriority.DEMAND` (PRD 03's demand
promotion). Idempotent, and a second open writes zero rows -- the enqueue
statement's `WHERE jobs.priority < excluded.priority` sees nothing left to
promote. `get_session` commits it as it commits any other request, which is
what makes the write durable rather than a flush that the response outlives.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from usher.api.deps import DefaultUserIdDep, TitleReadServiceDep
from usher.api.dto.title import TitleResponse

router = APIRouter(tags=["titles"])


@router.get("/titles/{title_id}", response_model=TitleResponse)
async def get_title(
    title_id: uuid.UUID, titles: TitleReadServiceDep, user_id: DefaultUserIdDep
) -> TitleResponse:
    detail = await titles.detail(title_id, user_id=user_id)
    if detail is None:
        # FastAPI's default shape, exactly as `GET /admin/sources/{id}/status`
        # ships it. PRD 07's RFC 9457 envelope is M9's, where the first route
        # that must answer "the source is down and I cannot serve this from
        # local state" arrives -- see the M5 plan's "Does a streaming surface
        # force the error envelope?" for why nothing in this milestone does.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="title not found")
    return TitleResponse.of(detail)
