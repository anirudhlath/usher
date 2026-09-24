"""`GET /home` -- the server-composed screen."""

from fastapi import APIRouter, Request, Response

from usher.api.caching import conditional_response
from usher.api.deps import HomeServiceDep, RowContextDep
from usher.api.dto.home import HomeResponse
from usher.services.home import _SCREEN_TTL

router = APIRouter(tags=["home"])


@router.get("/home", response_model=HomeResponse)
async def get_home(request: Request, home: HomeServiceDep, ctx: RowContextDep) -> Response:
    """Compose this household's screen, and answer it conditionally.

    Carries a strong `ETag` over the body and `Cache-Control: private, max-age=`
    the screen's TTL; a matching `If-None-Match` is answered 304.
    """
    body = HomeResponse.of(await home.compose(ctx))
    return conditional_response(request, body, ttl=_SCREEN_TTL)
