"""`GET /home` -- ADR-0006's route, and the first client-facing one since M5.

**Why a route at all, when M6 and three milestones before it declined one.**
The default in this project is CLI-only: M6 built `SearchService`,
`PostgresSearchIndex`, RRF fusion and the ranking blend and added no HTTP route,
because `usher search` prints exactly what the route would return. Composition
is the first capability where that stops being true. ADR-0006's central claim --
*"One request paints a screen, which is what makes the home screen feel instant
over a slow link"* -- is a property of a *request boundary*, and no CLI can
exhibit it. The command still ships (`usher home`), because PRD 08 requires
every operator command to work against an empty database and a route is a poor
place to discover that composition divides by zero on a household that has
watched nothing -- but here the CLI is the proxy and the route is the
deliverable, which is the reverse of M6.

`row.invalidated` is the other half: PRD 07's SSE table assigns it to M7 by
name, and `ports/events.py` said the member was absent *"because nothing
composes a row until M7"*. An invalidation event with no row to invalidate is
an event with no consumer.

**This route cannot fail because a source is down**, and that is structural
rather than defensive: nothing on this path holds a `SourceAdapter`, so there is
no call to catch. PRD 08: *"a degraded subsystem narrows functionality; it never
fails a request local state can answer."* Every input is local -- watch state,
media items, `title_neighbors`, `user_taste`, credits, collections -- and PRD
08's degradation table already says *"LLM call fails -> Home composes without
them"*. So there is no 503 here, which is why M5's deferral of PRD 07's RFC 9457
envelope survives this milestone intact: that envelope's own worked example is
`503 source_unavailable`, and this route has none to give a `code` to.
`POST /titles/{id}/play` is still the named trigger, in M9.

**It also never loads an embedding model**, and that one is not cosmetic:
`create_app`'s lifespan builds the embedder **only when `worker_enabled`**, so a
route that reached for one would work in development and 500 in exactly the
push-only deployment PRD 08 describes. Every similarity input here is a
*precomputed* artefact -- `title_neighbors` is a stored table, `user_taste` a
stored row -- and computing those needs a model where reading them does not.
Same property `usher index` already has.

**An empty screen is a 200 with no rows.** Not a 404 -- `/home` is a screen
rather than a resource, and a screen with nothing on it is a fact about the
household. And deliberately not padded with a generic row: a "popular titles"
fallback on a household that has watched nothing produces a screen that *looks*
personalised and is not, which is the failure this milestone exists to refuse.
`rows: []` is distinguishable; a generic row is not.

**Still M9's, so this is a route and not a land grab:** the RFC 9457 envelope,
`usher.http.server.duration`, `usher.cache.hits`/`.misses`, HTTP cache headers,
and pagination. The whole screen comes back in one response with no cursor,
which is what ADR-0006 specifies and what PRD 07's own endpoint table shows
(`/browse` carries a cursor; `/home` does not).
"""

from fastapi import APIRouter

from usher.api.deps import HomeServiceDep, RowContextDep
from usher.api.dto.home import HomeResponse

router = APIRouter(tags=["home"])


@router.get("/home", response_model=HomeResponse)
async def get_home(home: HomeServiceDep, ctx: RowContextDep) -> HomeResponse:
    """Compose this household's screen.

    The context is a dependency rather than something built here, because it is
    thirteen request-scoped values and `tests/integration/test_pipeline_deps.py`
    is the only thing that resolves the graph FastAPI actually builds --
    annotating one of them without `Depends` is a `FastAPIError` at *route
    registration*, which a unit test that overrides this route's service never
    sees.
    """
    return HomeResponse.of(await home.compose(ctx))
