"""`GET /search` -- the three-valued mode, the two mode fields, and the
rewrite that has to reach the wire.

**The real `SearchService` over scripted ports, never a stubbed service.**
M5's correction, restated by `tests/unit/test_api_home.py`: a stub would make
every case below an assertion about `SearchResponse.of` alone, and the three
mutations this file exists to kill -- deleting `expanded_query`, collapsing
`requested_mode` into `mode`, re-clamping `limit` in the route -- all live in
the seam between the service's answer and the body. Retrieval is held fixed
with a scripted index for `tests/unit/test_services_search.py`'s reason: that
file's fake has no text analysis, so a route case driven through its matching
would be an assertion about a tokenizer nobody shipped.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import ast
import pathlib
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.embedding import FakeEmbedder
from tests.fakes.llm_call_repository import FakeLLMCallRepository
from tests.fakes.llm_client import FakeLLMClient
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import get_default_user_id, get_search_service
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode
from usher.config import Settings
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.search import (
    SearchDocument,
    SearchHit,
    SearchIndex,
    SearchOutcome,
    SearchRequest,
    SuggestIndex,
)
from usher.services.query_expansion import QUERY_KEY, QueryExpansionService
from usher.services.search import SearchService

SECRET_KEY = "0123456789abcdef0123456789abcdef"
UNREACHABLE_DSN = "postgresql+asyncpg://usher:usher@127.0.0.1:1/usher"

# Fixed ids rather than `new_id()`, for the reason
# `tests/unit/test_services_search.py` gives at length: several mutations here
# tie two rows on the blended score, and only a known tiebreak makes the
# wrong order visible rather than a coin flip.
_FIRST = uuid.UUID(int=0x01)
_SECOND = uuid.UUID(int=0x02)

#: The household `get_default_user_id` is overridden to answer with. Fixed,
#: so a case can assert the id that reached the port is the id the
#: dependency resolved rather than merely that some id did.
_VIEWER = uuid.UUID(int=0xA1)

_CATALOG: dict[uuid.UUID, str] = {
    _FIRST: "A Vacuum in Winter",
    _SECOND: "The Quiet Vacuum",
}

# A `ts_rank` lands around 0.06. Realistic magnitudes, for the same reason the
# service's own suite uses them.
_STRONG = 0.06
_WEAK = 0.02

_ROUTER = pathlib.Path(__file__).parents[2] / "src" / "usher" / "api" / "routers" / "search.py"


class _ScriptedIndex(SearchIndex):
    """A `SearchIndex` that answers with exactly what a case scripted, and
    records the `SearchRequest` that crossed the port.

    The record is what makes the `limit` case an observation rather than a
    hope: the only place a re-clamp in the route is visible is the number the
    service handed on.
    """

    def __init__(self, outcome: SearchOutcome) -> None:
        self.outcome = outcome
        self.requests: list[SearchRequest] = []

    async def index_many(self, documents: Sequence[SearchDocument]) -> None:
        return None

    async def remove(self, title_id: uuid.UUID) -> None:
        return None

    async def search(self, request: SearchRequest) -> SearchOutcome:
        self.requests.append(request)
        return self.outcome


class _ScriptedSuggest(SuggestIndex):
    """Present because `SearchService` takes one. `GET /search` never calls
    it -- `GET /search/suggest` is B5's route."""

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        return []


class _Expander:
    """A real `QueryExpansionService` over a scripted client.

    Deliberately not a stub: the acceptance is that a populated
    `expanded_query` means a completion was *bought*, and a stub recording a
    call would make the bought and unbought paths look identical while proving
    nothing about either. `client.calls` is what the blank-query case asserts
    on.
    """

    def __init__(self, *bodies: dict[str, Any] | BaseException) -> None:
        self.client = FakeLLMClient.returning(*bodies)
        self.ledger = FakeLLMCallRepository()
        self.service = QueryExpansionService(
            client=self.client,
            ledger=self.ledger,
            commit=self._commit,
            model="test/asked-1",
        )

    async def _commit(self) -> None:
        return None


class _RecordingWatchStates(FakeWatchStateRepository):
    """`FakeWatchStateRepository`, recording the household it was asked about.

    The route's household is invisible in a response body -- the same rows come
    back either way -- so the only place it is observable is the argument that
    crossed the port.
    """

    def __init__(self) -> None:
        super().__init__()
        self.households: list[uuid.UUID] = []

    async def played_title_ids(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        self.households.append(user_id)
        return await super().played_title_ids(user_id, title_ids)


async def _service(
    index: SearchIndex,
    *,
    embedder: FakeEmbedder | None = None,
    expander: _Expander | None = None,
    result_limit: int = 50,
    watch_states: _RecordingWatchStates | None = None,
) -> SearchService:
    titles = FakeTitleRepository()
    media_items = FakeMediaItemRepository()
    households = _RecordingWatchStates() if watch_states is None else watch_states
    # No stored centroid and no vectors: the shipped state of a deployment
    # whose worker has never run, which is what every case in this file is
    # about. The taste term is therefore absent from every score below, and
    # `tests/unit/test_services_search.py` owns the arm where it is present.
    taste = FakeTasteRepository()
    embeddings = FakeTitleEmbeddingRepository()
    for title_id, name in _CATALOG.items():
        await titles.add(
            Title(
                id=title_id,
                kind=TitleKind.MOVIE,
                name=name,
                sort_name=name.casefold(),
                year=2019,
            )
        )
    return SearchService(
        index,
        _ScriptedSuggest(),
        _ScriptedSuggest(),
        titles,
        media_items,
        households,
        taste,
        embeddings,
        result_limit=result_limit,
        embedder=embedder,
        expander=None if expander is None else expander.service,
    )


def _settings(**overrides: Any) -> Settings:
    return Settings(
        database_url=UNREACHABLE_DSN,
        secret_key=SECRET_KEY,
        # Both lanes off: `dependency_overrides` do not reach the lifespan, so
        # a worker lane here would poll a database nothing listens on.
        push_enabled=False,
        worker_enabled=False,
        **overrides,
    )


def _app(service: SearchService, *, settings: Settings | None = None) -> FastAPI:
    """The shipped app with two dependencies replaced.

    `get_search_service` and `get_default_user_id` -- the router, the DTO and
    the real `SearchService` all stay on the path a request takes. The second
    is substituted for the same reason as the first and not for a new one:
    `get_default_user_id` runs a `SELECT` (and, on a first run, an `INSERT`),
    and the app in this file points at a database nothing listens on. Which row
    it resolves is `tests/integration/test_pipeline_deps.py`'s question; what
    is asserted here is that whatever it resolves reaches the blend.
    """
    built = create_app(settings or _settings())
    built.dependency_overrides[get_search_service] = lambda: service
    built.dependency_overrides[get_default_user_id] = lambda: _VIEWER
    return built


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
async def hits() -> _ScriptedIndex:
    return _ScriptedIndex(
        SearchOutcome(
            hits=(
                SearchHit(title_id=_FIRST, score=_STRONG),
                SearchHit(title_id=_SECOND, score=_WEAK),
            ),
            semantic_coverage=0.25,
        )
    )


@pytest.fixture
async def client(hits: _ScriptedIndex) -> AsyncIterator[httpx.AsyncClient]:
    """A deployment with no embedder, which is every API-only deployment:
    `create_app`'s lifespan builds a model only when `worker_enabled` and does
    not expose it, and `api/deps.get_search_service` passes `None`
    deliberately."""
    async for connected in _client(_app(await _service(hits))):
        yield connected


async def test_the_route_ranks_for_the_household_the_dependency_resolved(
    hits: _ScriptedIndex,
) -> None:
    """The household reaches the blend, and it comes off `DefaultUserIdDep`
    rather than off the query string.

    Fails: a route that never resolves one, which renders **identically** --
    the same rows in the same order with the same scores, no error and nothing
    in the body to say the watch-state term never ran. That invisibility is the
    whole reason this case asserts on the argument that crossed the port
    instead of on the response.

    Fails equally on a `?user_id=` parameter, which is the other way this could
    have been built: `openapi.json`'s parameter list is asserted below to hold
    exactly the three a client may choose, and a household is not one of them.
    """
    households = _RecordingWatchStates()
    app = _app(await _service(hits, watch_states=households))
    async for connected in _client(app):
        answer = await connected.get("/search", params={"q": "vacuum"})

    assert answer.status_code == 200
    assert len(answer.json()["results"]) == 2, (
        "the premise: the search ranked something, so the household read was reachable"
    )
    assert households.households == [_VIEWER]


async def test_the_household_is_not_a_query_parameter(client: httpx.AsyncClient) -> None:
    """PRD 05 keeps `SearchFilters` a closed vocabulary with no user field, and
    this is that rule where a client could see it: `q`, `mode` and `limit` are
    the whole of what a caller chooses.

    Fails: a `user_id`/`user` query parameter, which would let any caller rank
    a search against any household's watch history -- and which is the shape
    the parameter would have taken if it had gone on `SearchFilters`, since
    every field of that is a flag on `usher search` and a parameter here.
    """
    document = (await client.get("/openapi.json")).json()
    declared = {
        one["name"]
        for one in document["paths"]["/search"]["get"]["parameters"]
        if one["in"] in {"query", "path"}
    }
    assert declared == {"q", "mode", "limit"}


async def test_a_fused_request_served_without_an_embedder_reports_both_modes(
    client: httpx.AsyncClient,
) -> None:
    """**The headline, and the reason `requested_mode` sits beside `mode`.**

    A `fused` request on a deployment with no embedder is served as full text
    and every row of that answer is correct, so without two fields the only
    signal is `semantic_coverage == 0.0` -- which is *also* what a healthy
    fused search over a catalog with no embeddings reports. Two different
    problems with two different fixes (install the extra; run `usher index`)
    presenting identically.

    Fails with 404 before the route exists. After it exists it fails against
    any implementation that reports one field twice, in either direction: the
    positive control below asserts that a `full_text` request reports them
    *equal*, so "always echo `requested_mode`" and "always echo `mode`" are
    both red -- one here, one there.
    """
    response = await client.get("/search", params={"q": "vacuum", "mode": "fused"})
    assert response.status_code == 200, response.text
    degraded = response.json()
    assert degraded["requested_mode"] == "fused"
    assert degraded["mode"] == "full_text"

    # The positive control. Without it, a route that reported `mode` under both
    # names would fail above and a route that reported `requested_mode` under
    # both names would pass.
    undegraded = (await client.get("/search", params={"q": "vacuum", "mode": "full_text"})).json()
    assert undegraded["requested_mode"] == "full_text"
    assert undegraded["mode"] == "full_text"


async def test_a_semantic_request_without_an_embedder_is_a_problem_document(
    client: httpx.AsyncClient,
) -> None:
    """The one failure this route has, in A2's envelope.

    `fused` narrows because a whole lane is left; `semantic` refuses because
    narrowing it is not narrowing -- the caller asked the one question
    full-text cannot answer and would get a plausible answer to a different
    one. The status and the code are argued at the raise site: 503 would say
    *retry* about a state no retry reaches, and `source_unavailable` names a
    media server.

    **The media type is asserted, not only the code.** A route raising a status
    with no member in the vocabulary is handed to FastAPI's default handler and
    answers `{"detail": ...}` at `application/json` -- which carries the right
    status and is indistinguishable from a route that never adopted the
    envelope at all.
    """
    response = await client.get("/search", params={"q": "vacuum", "mode": "semantic"})
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == ProblemCode.VALIDATION_FAILED.value
    assert body["instance"] == "/search"
    # The remedy is the content of the failure: a client that cannot read what
    # to ask for instead has learned only that something went wrong.
    assert "mode=fused" in body["detail"]
    # `instance` is `request.url.path` and never `request.url`, so the query a
    # viewer typed does not come back in the document. M9's `search_queries`
    # makes that a live concern rather than a hypothetical one.
    assert "vacuum" not in response.text


async def test_the_mode_parameter_is_an_enum_on_the_wire_and_semantic_is_not_a_boolean(
    client: httpx.AsyncClient,
) -> None:
    """PRD 07's sketch spells this `?semantic=`, which is a **boolean** and
    cannot express fusion at all.

    Two claims, and the second is the one a route could pass while getting
    wrong. First, `?mode=` reaches `/openapi.json` as an *enum* rather than as
    a bare string -- the reason `api/dto/home.py` reuses the domain
    `DisplayHint` instead of minting a wire twin: a `str` parameter documents
    nothing and accepts `?mode=fuzed` as far as the schema is concerned.

    Second, **`?semantic=` is not accepted**, and "accepted" has to be checked
    behaviourally rather than by absence from the parameter list. FastAPI
    ignores an undeclared query parameter silently, so a client sending
    `?semantic=true` gets a full-text search and no error -- which is correct,
    and is only distinguishable from a route that honoured it by asserting the
    mode that came back.
    """
    schema = (await client.get("/openapi.json")).json()
    parameters = schema["paths"]["/search"]["get"]["parameters"]
    by_name = {parameter["name"]: parameter for parameter in parameters}
    assert set(by_name) == {"q", "mode", "limit"}, sorted(by_name)

    mode = by_name["mode"]["schema"]
    # FastAPI renders an enum default as an `allOf`/`$ref` pair rather than
    # inlining it, so the vocabulary is resolved out of `components` rather
    # than read off the parameter -- a check that only looked for an inline
    # `enum` key would report "not an enum" for the shape that ships.
    reference = mode.get("$ref") or next(
        (member["$ref"] for member in mode.get("allOf", ()) if "$ref" in member), None
    )
    resolved = (
        schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
        if reference is not None
        else mode
    )
    assert resolved.get("enum") == ["full_text", "semantic", "fused"], resolved

    honoured = (await client.get("/search", params={"q": "vacuum", "semantic": "true"})).json()
    assert honoured["requested_mode"] == "full_text"
    assert honoured["mode"] == "full_text"


async def test_an_expanded_query_reaches_the_body_only_when_a_completion_was_bought() -> None:
    """**The mutation is deleting the field**, and only an injected expander
    can see it.

    A case asserting the shipped default's `null` cannot fail: with no LLM
    client and `USHER_QUERY_EXPANSION_ENABLED` false there is nothing to
    expand with, so `expanded_query is None` holds against an implementation
    that never carried the field at all -- and against one that dropped it,
    since `body.get("expanded_query")` is `None` either way. So the populated
    arm buys a real completion through a real `QueryExpansionService`, and the
    control below asserts the key is **present and null** rather than absent on
    the path that embedded the query as typed.
    """
    expander = _Expander({QUERY_KEY: "a claustrophobic film about isolation"})
    hits = _ScriptedIndex(SearchOutcome())
    service = await _service(hits, embedder=FakeEmbedder(), expander=expander)
    async for client in _client(_app(service)):
        body = (await client.get("/search", params={"q": "vacuum", "mode": "fused"})).json()

    assert body["expanded_query"] == "a claustrophobic film about isolation"
    # The premise: a completion really was bought, so this is a rewrite that
    # travelled rather than a string a fixture happened to echo.
    assert len(expander.client.calls) == 1
    # And the rewrite is what was *embedded*, never what was matched: the typed
    # words stay on the lexical lane, so a drifted rewrite cannot take an
    # exact-title search with it.
    assert hits.requests[-1].query == "vacuum"


async def test_the_expanded_query_key_is_present_and_null_when_nothing_was_substituted(
    client: httpx.AsyncClient,
) -> None:
    """The control for the case above, and a claim in its own right.

    `api/dto/title.py` uses an **absent key** for every empty value; this field
    is deliberately present-and-null, because the two answer different
    questions. An absent `images` says "this server has no such capability
    yet"; a null `expanded_query` says "nothing was substituted on *this*
    search", which is a fact about the request that a client decides what to do
    with on every response.
    """
    body = (await client.get("/search", params={"q": "vacuum"})).json()
    assert "expanded_query" in body
    assert body["expanded_query"] is None


async def test_semantic_coverage_is_the_outcomes_number_and_not_one_derived_from_the_hits(
    client: httpx.AsyncClient, hits: _ScriptedIndex
) -> None:
    """Passed through from `SearchOutcome`, never recomputed.

    It is the fraction of the *filtered population* that had a vector, and the
    population is not on the wire. Recomputed from the returned hits it reads
    `1.0` exactly when every hit had one -- which is precisely what a green
    fixture seeds, so the wrong implementation looks healthiest on the tests
    that pass. The scripted outcome says `0.25` while both returned hits came
    out of an index the route cannot ask about vectors at all, so `1.0` and
    `0.0` are both distinguishable from the right answer.
    """
    assert hits.outcome.semantic_coverage == 0.25, "the fixture stopped seeding a distinctive value"
    body = (await client.get("/search", params={"q": "vacuum", "mode": "fused"})).json()
    assert body["semantic_coverage"] == 0.25
    assert len(body["results"]) == 2


async def test_a_blank_query_is_a_200_with_no_results_and_buys_no_completion() -> None:
    """A search box sends one between keystrokes, so it is not a 422.

    Three claims in one request, and the third is the one with money attached.
    `SearchService`'s own `if not query.strip()` guard returns **before** the
    embed, and an expansion sits immediately in front of that embed -- so a
    blank query buys nothing even on a deployment that has turned expansion on.
    Asserted on the client's own call log rather than on the null field, since
    `expanded_query is None` is also what a bought-and-failed completion
    produces.
    """
    expander = _Expander({QUERY_KEY: "a rewrite nobody should pay for"})
    service = await _service(
        _ScriptedIndex(SearchOutcome()), embedder=FakeEmbedder(), expander=expander
    )
    async for client in _client(_app(service)):
        response = await client.get("/search", params={"q": "   ", "mode": "fused"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"] == []
    assert body["expanded_query"] is None
    assert expander.client.calls == []


async def test_the_limit_ceiling_is_the_services_and_this_route_spells_none() -> None:
    """**The mutation is re-clamping in the route**, and it is invisible
    wherever the two ceilings agree.

    `SearchService.search` does `min(limit, self._result_limit)` against
    `settings.search_result_limit`. A route that clamped as well would be the
    same ceiling spelled twice -- and every assertion would pass for as long as
    nobody moved one of them. So the service's ceiling and the app's setting
    are deliberately made to **disagree** here: a route re-clamping from
    `Settings` hands the service `3` and the port sees `3`, while the shipped
    route hands over what the client asked and the port sees the service's
    `5`.

    The structural half is asserted beside it, because the behavioural half
    cannot see a route that read the setting and clamped to something else
    again.
    """
    hits = _ScriptedIndex(SearchOutcome())
    service = await _service(hits, result_limit=5)
    app = _app(service, settings=_settings(search_result_limit=3))
    async for client in _client(app):
        assert (
            await client.get("/search", params={"q": "vacuum", "limit": 10_000})
        ).status_code == 200

    assert hits.requests[-1].limit == 5

    named = {
        node.attr
        for node in ast.walk(ast.parse(_ROUTER.read_text(), str(_ROUTER)))
        if isinstance(node, ast.Attribute)
    }
    assert "search_result_limit" not in named, (
        "the router reads the result ceiling, so it is spelled twice -- "
        "`SearchService.search` already clamps"
    )


async def test_no_source_concept_and_no_credential_reaches_the_body(
    client: httpx.AsyncClient,
) -> None:
    """PRD 07's first line, asserted on the key set rather than on a substring.

    A `SearchResult` carries no source concept to begin with -- a hit names a
    `Title.id`, which is every route a client can call -- so this is a claim
    about the DTO staying that way. The key set is pinned in both directions:
    an added `external_id` fails, and so does a silently dropped
    `expanded_query`.
    """
    body = (await client.get("/search", params={"q": "vacuum"})).json()
    assert set(body) == {
        "query",
        "requested_mode",
        "mode",
        "semantic_coverage",
        "expanded_query",
        "results",
    }
    assert body["query"] == "vacuum"
    assert set(body["results"][0]) == {
        "title_id",
        "kind",
        "name",
        "year",
        "popularity",
        "owned",
        "score",
    }
