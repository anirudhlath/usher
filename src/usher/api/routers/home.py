"""`GET /home` -- ADR-0006's route, and the first client-facing one since M5."""

from fastapi import APIRouter, Request, Response

from usher.api.caching import conditional_response
from usher.api.deps import HomeServiceDep, RowContextDep
from usher.api.dto.home import HomeResponse
from usher.services.home import _SCREEN_TTL

router = APIRouter(tags=["home"])


@router.get("/home", response_model=HomeResponse)
async def get_home(request: Request, home: HomeServiceDep, ctx: RowContextDep) -> Response:
    """Compose this household's screen, and answer it conditionally.

    The context is a dependency rather than something built here, because it is
    thirteen request-scoped values and `tests/integration/test_pipeline_deps.py`
    is the only thing that resolves the graph FastAPI actually builds --
    annotating one of them without `Depends` is a `FastAPIError` at *route
    registration*, which a unit test that overrides this route's service never
    sees.

    Returns a `Response` rather than a `HomeResponse`, always -- FastAPI
    passes a `Response` instance through untouched (`response_model` still
    describes the 200 shape for `/openapi.json`), and that is what lets the
    same bytes `conditional_response` hashes be the bytes actually sent: a
    second, independent serialisation through FastAPI's own encoder is exactly
    the correctness hazard the caching module's docstring warns about.
    """
    body = HomeResponse.of(await home.compose(ctx))
    return conditional_response(request, body, ttl=_SCREEN_TTL)
