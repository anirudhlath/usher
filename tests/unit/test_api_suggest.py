"""`GET /search/suggest` -- two tiers on one route, the tier that answered, and
the prefix length below which tier 1 does not run.

**The real `SearchService` over two scripted indexes, never a stubbed
service.** M5's correction, restated by `tests/unit/test_api_home.py` and by
`tests/unit/test_api_search.py` one route over: a stub would make every case
here an assertion about `SuggestResponse.of` alone, and the mutation this file
exists to kill -- the tier parameter selecting the same index for both values
-- lives in the seam between the parameter and the collaborator it picks.

**The two doubles disagree on a typo and agree on a prefix, which is the whole
of the two-armed case's teeth.** `FakePrefixSuggestIndex` is `startswith` and
`FakeSuggestIndex` is Levenshtein-over-the-head; a route serving both tiers
from one index answers the same list twice, and only an arm asserting the
*absence* of the typo hit can see it.

**Both doubles record their calls**, because half of what this route does is
*not* reach the database: a `q` below the answering tier's minimum, and a blank
one, are 200s that must issue no query at all. An assertion on the empty
`results` list cannot tell that from a query that ran and matched nothing --
which is the whole cost this route exists to avoid, since at one character it
is 2,707 ms of it.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.search_index import FakePrefixSuggestIndex, FakeSuggestIndex
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import get_search_service, get_visibility_service
from usher.config import Settings
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.search import (
    SearchDocument,
    SearchFilters,
    SearchHit,
    SearchIndex,
    SearchOutcome,
    SearchRequest,
    SuggestIndex,
)
from usher.services.search import SearchService, SuggestTier
from usher.services.visibility import VisibilityService

SECRET_KEY = "0123456789abcdef0123456789abcdef"
UNREACHABLE_DSN = "postgresql+asyncpg://usher:usher@127.0.0.1:1/usher"

_KESTREL = uuid.UUID(int=0x01)
_NAME = "Kestrelbound Vane"

#: Seven characters of prefix and a one-character substitution of the same
#: seven. Both are past the route's minimum, so the two-armed case below is
#: about the tier selector rather than about the length bound.
_TYPED = "kestrel"
_TYPO = "kestrek"


class _DeadIndex(SearchIndex):
    """Present because `SearchService` takes one. `GET /search/suggest` never
    reaches it -- the suggest path has no `SearchIndex` in it at all, which is
    ADR-0021's port split showing up as an object this file never asks
    anything. It raises rather than answering `SearchOutcome()`, because an
    empty answer is what a wrongly-wired route would also produce."""

    async def index_many(self, documents: Sequence[SearchDocument]) -> None:
        return None

    async def remove(self, title_id: uuid.UUID) -> None:
        return None

    async def search(self, request: SearchRequest) -> SearchOutcome:
        raise AssertionError("the suggest route must not reach the search index")

    async def semantic_coverage(self, filters: SearchFilters) -> float:
        # Dead on the same terms as `search`, and it carries a claim of its own
        # rather than only satisfying the ABC: since #16 the coverage probe is
        # bought by a search that is about to expand, and type-ahead has no
        # embed for an expansion to sit in front of. An expansion factored to
        # the top of `SearchService` -- the tidy-looking version -- reaches
        # this line before it reaches anything else.
        raise AssertionError("the suggest route must not measure semantic coverage")


class _RecordingPrefix(FakePrefixSuggestIndex):
    """Tier 1, plus the ledger the not-reached cases assert on."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, int]] = []

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        self.calls.append((prefix, limit))
        return await super().suggest(prefix, limit=limit)


class _RecordingFuzzy(FakeSuggestIndex):
    """Tier 2, on the same terms."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, int]] = []

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        self.calls.append((prefix, limit))
        return await super().suggest(prefix, limit=limit)


@dataclass
class _Kit:
    service: SearchService
    prefix_tier: _RecordingPrefix
    fuzzy_tier: _RecordingFuzzy
    titles: FakeTitleRepository = field(default_factory=FakeTitleRepository)

    def calls(self, tier: SuggestTier) -> list[tuple[str, int]]:
        return self.prefix_tier.calls if tier is SuggestTier.PREFIX else self.fuzzy_tier.calls


async def _kit(*, result_limit: int = 50) -> _Kit:
    """One catalog, three readers of it: both tiers and the hydration."""
    titles = FakeTitleRepository()
    prefix_tier = _RecordingPrefix()
    fuzzy_tier = _RecordingFuzzy()
    await titles.add(
        Title(
            id=_KESTREL,
            kind=TitleKind.MOVIE,
            name=_NAME,
            sort_name=_NAME.casefold(),
            year=2019,
        )
    )
    prefix_tier.given(name=_NAME, title_id=_KESTREL)
    fuzzy_tier.given(name=_NAME, title_id=_KESTREL)
    service = SearchService(
        _DeadIndex(),
        prefix_tier,
        fuzzy_tier,
        titles,
        FakeMediaItemRepository(),
        FakeWatchStateRepository(),
        FakeTasteRepository(),
        FakeTitleEmbeddingRepository(),
        result_limit=result_limit,
    )
    return _Kit(service=service, prefix_tier=prefix_tier, fuzzy_tier=fuzzy_tier, titles=titles)


def _settings() -> Settings:
    return Settings(
        database_url=UNREACHABLE_DSN,
        secret_key=SECRET_KEY,
        # Both lanes off: `dependency_overrides` do not reach the lifespan, so
        # a worker lane here would poll a database nothing listens on.
        push_enabled=False,
        worker_enabled=False,
    )


def _app(service: SearchService) -> FastAPI:
    built = create_app(_settings())
    built.dependency_overrides[get_search_service] = lambda: service
    # Since #73 this route promotes the skeletons it offered, so the queue and
    # the catalog are on its path and `UNREACHABLE_DSN` is exactly what the
    # name says. What this route *promotes* is asserted in
    # `test_api_search.py::test_type_ahead_promotes_what_it_offered`; here it
    # only has to not be the real one.
    built.dependency_overrides[get_visibility_service] = lambda: VisibilityService(
        FakeJobQueue(), FakeTitleRepository()
    )
    return built


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
async def kit() -> _Kit:
    return await _kit()


@pytest.fixture
async def client(kit: _Kit) -> AsyncIterator[httpx.AsyncClient]:
    async for connected in _client(_app(kit.service)):
        yield connected


# --- the two tiers ---------------------------------------------------------


async def test_the_prefix_tier_finds_the_prefix_and_only_the_fuzzy_tier_finds_the_typo(
    client: httpx.AsyncClient,
) -> None:
    """**Both arms in one case, because either alone is green against a route
    that serves both tiers from one index.**

    `?tier=prefix` asked for a true prefix finds the title and asked for a
    one-character substitution of that same prefix finds nothing -- that is
    tier 1's 1.9% measured typo recall as a property rather than as a number.
    `?tier=fuzzy` finds it both times, which is the tolerance the trigram +
    `levenshtein_less_equal` path carries and the whole reason two tiers
    exist.

    Fails with 404 before the route exists (observed). Fails on the second
    assertion against a route whose `?tier=` selects the same collaborator for
    both values, which is this task's headline mutation.
    """
    prefix_hit = await client.get("/search/suggest", params={"q": _TYPED, "tier": "prefix"})
    prefix_typo = await client.get("/search/suggest", params={"q": _TYPO, "tier": "prefix"})
    fuzzy_hit = await client.get("/search/suggest", params={"q": _TYPED, "tier": "fuzzy"})
    fuzzy_typo = await client.get("/search/suggest", params={"q": _TYPO, "tier": "fuzzy"})

    assert [one.status_code for one in (prefix_hit, prefix_typo, fuzzy_hit, fuzzy_typo)] == [
        200,
        200,
        200,
        200,
    ]
    assert [one["title_id"] for one in prefix_hit.json()["results"]] == [str(_KESTREL)]
    assert prefix_typo.json()["results"] == []
    assert [one["title_id"] for one in fuzzy_hit.json()["results"]] == [str(_KESTREL)]
    assert [one["title_id"] for one in fuzzy_typo.json()["results"]] == [str(_KESTREL)]


@pytest.mark.parametrize("tier", [tier.value for tier in SuggestTier])
async def test_the_response_says_which_tier_answered(client: httpx.AsyncClient, tier: str) -> None:
    """`requested_mode`'s argument, one route over and minus the degradation.

    Two tiers give **different answers to the same `q`** by design, and
    `?tier=` has a default -- so a client that named no tier is reading an
    answer from a tier it did not choose, and a response that does not say
    which is uninterpretable beside another one. Fails: the echo dropped, or
    hard-coded to whichever tier the author was thinking about.
    """
    response = await client.get("/search/suggest", params={"q": _TYPED, "tier": tier})
    assert response.json()["tier"] == tier


async def test_the_default_tier_is_the_prefix_tier(client: httpx.AsyncClient, kit: _Kit) -> None:
    """The keystroke tier is the default, which is the whole point of the
    split: the trigram path is 33.6 ms p50 and is meant to be debounced behind
    this one.

    Both halves asserted -- the echo *and* which collaborator was actually
    consulted -- because a route that echoed `prefix` while asking tier 2
    passes the first alone.
    """
    response = await client.get("/search/suggest", params={"q": _TYPED})
    assert response.json()["tier"] == "prefix"
    assert kit.prefix_tier.calls == [(_TYPED, 10)]
    assert kit.fuzzy_tier.calls == []


async def test_the_tier_reaches_the_openapi_document_as_an_enum_defaulting_to_prefix(
    client: httpx.AsyncClient,
) -> None:
    """`/openapi.json` describes the vocabulary, so a client generator writes
    two named values rather than a free string. Fails: `tier: str`, which
    accepts `?tier=fuzy` and answers a 200 from whichever branch the `if`
    happened to fall through to.
    """
    document = (await client.get("/openapi.json")).json()
    parameters = {
        one["name"]: one for one in document["paths"]["/search/suggest"]["get"]["parameters"]
    }
    schema = parameters["tier"]["schema"]
    reference = schema.get("$ref") or schema.get("allOf", [{}])[0].get("$ref", "")
    named = document["components"]["schemas"][reference.rsplit("/", 1)[-1]]
    assert named["enum"] == ["prefix", "fuzzy"]
    assert schema["default"] == "prefix"


async def test_an_unknown_tier_is_refused_rather_than_served_by_a_default(
    client: httpx.AsyncClient,
) -> None:
    """A 422 through A2's envelope, and it is the enum doing it. Fails: a
    `tier: str` parameter with an `else` arm -- a typo would then be served
    silently by one tier while the response echoed the other."""
    response = await client.get("/search/suggest", params={"q": _TYPED, "tier": "fuzy"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


# --- the minimum prefix length --------------------------------------------


async def test_a_prefix_below_the_minimum_never_reaches_the_index_at_all(
    client: httpx.AsyncClient, kit: _Kit
) -> None:
    """**The saving is a query not issued, so the assertion is on the port
    call and not on the empty list.**

    An empty `results` is also what a query that ran and matched nothing
    produces, and running it is precisely the 2,707 ms p95 this bound exists
    to avoid. Fails: a bound applied to the *answer* rather than in front of
    the call, which reads identically from the body.

    The prefix used here is a genuine prefix of the seeded title, so the only
    reason the box is empty is the bound.
    """
    response = await client.get("/search/suggest", params={"q": "kes", "tier": "prefix"})
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert kit.prefix_tier.calls == []


async def test_the_minimum_is_four_characters_and_not_the_number_the_index_could_serve(
    client: httpx.AsyncClient,
) -> None:
    """**Every number here is a literal**, which is the point.

    D4's `TICKET_TTL_SECONDS` and B9's `CAST_LIMIT` both shipped a case whose
    fixture was derived from the constant under test -- so widening the
    constant moved the fixture and the expectation together and the case could
    not see it. `kes` (3) and `kest` (4) are both true prefixes of the seeded
    title, so a bound at any other value fails one of these two arms.

    Also pins the reported number, because a route that refused correctly and
    reported `min_query_length: 1` would leave a client unable to implement
    the same rule -- and the whole saving is the request never sent.
    """
    refused = await client.get("/search/suggest", params={"q": "kes", "tier": "prefix"})
    served = await client.get("/search/suggest", params={"q": "kest", "tier": "prefix"})

    assert refused.json()["results"] == []
    assert [one["title_id"] for one in served.json()["results"]] == [str(_KESTREL)]
    assert refused.json()["min_query_length"] == 4
    assert served.json()["min_query_length"] == 4


async def test_the_length_is_measured_after_stripping_so_padding_cannot_buy_a_probe(
    client: httpx.AsyncClient, kit: _Kit
) -> None:
    """Fails: `len(q)` instead of `len(q.strip())`.

    Four spaces and one character is four characters and one character of
    selectivity, and `LIKE '    k%'` is not the cheap query the bound was
    measured for -- leading whitespace contributes nothing to the index range
    condition. The port-call assertion is what sees it: under the wrong
    spelling the query runs and matches nothing, which looks identical in the
    body.
    """
    response = await client.get("/search/suggest", params={"q": "   k", "tier": "prefix"})
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert kit.prefix_tier.calls == []


async def test_the_fuzzy_tier_is_not_held_to_the_prefix_tiers_minimum(
    client: httpx.AsyncClient, kit: _Kit
) -> None:
    """**The asymmetry is deliberate and is a statement about evidence.**

    B3 measured tier 1 per prefix length and nobody has measured tier 2 that
    way, so a four-character bound there would be a refusal with no
    measurement under it. Tier 2's defence is the client's debounce. Fails: a
    single module-level minimum applied to both tiers, which silently takes
    the typo-tolerant tier away from every short query -- exactly the 2-4
    character band ADR-0002's gate failed on and the band a two-tier design
    exists to serve.
    """
    response = await client.get("/search/suggest", params={"q": "k", "tier": "fuzzy"})
    assert response.status_code == 200
    assert response.json()["min_query_length"] == 1
    assert kit.fuzzy_tier.calls == [("k", 10)]


@pytest.mark.parametrize("tier", [tier.value for tier in SuggestTier])
@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_a_blank_q_is_two_hundred_with_no_results_on_both_tiers(
    client: httpx.AsyncClient, kit: _Kit, tier: str, blank: str
) -> None:
    """Not a 422: a search box sends this between keystrokes and on every
    backspace to zero, and rejecting it would put an error on the wire for
    every viewer who selected their query and typed over it. `GET /search`
    makes the identical call one route over.

    **Both tiers, because the two are bounded by different numbers** -- four
    characters and one -- and only the blank case is answered by the same rule
    on both. A spelling that special-cased the blank inside one tier's branch
    would leave the other forwarding whitespace to a `LIKE '%'` over 1.27M
    rows.

    The `calls` assertion is the one with teeth: the service carries its own
    `if not prefix.strip()` guard, so a route that forwarded a blank would
    still answer `results: []` and look correct.
    """
    response = await client.get("/search/suggest", params={"q": blank, "tier": tier})
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert kit.calls(SuggestTier(tier)) == []


# --- the rest of the wire --------------------------------------------------


@pytest.mark.parametrize("tier", [tier.value for tier in SuggestTier])
async def test_the_query_is_echoed_as_typed(client: httpx.AsyncClient, tier: str) -> None:
    """Not stripped, not lower-cased: it is what the pattern was built from,
    and a client rendering "no matches for ..." needs the string the server
    used. Fails: an echo of the normalised form, which would report a query
    the viewer did not type."""
    response = await client.get("/search/suggest", params={"q": "  KesTrel ", "tier": tier})
    assert response.json()["query"] == "  KesTrel "


async def test_a_hydrated_candidate_carries_the_title_fields_a_box_renders(
    client: httpx.AsyncClient,
) -> None:
    """PRD 05 wants unowned results surfaced "clearly marked", and a
    type-ahead row is a result -- a client that had to ask a second question
    per row to render the badge would not render it. Fails: a DTO that carries
    the id and nothing else, which is a box of UUIDs."""
    row = (await client.get("/search/suggest", params={"q": _TYPED})).json()["results"][0]
    assert row == {
        "title_id": str(_KESTREL),
        "kind": "movie",
        "name": _NAME,
        "year": 2019,
        "popularity": None,
        "owned": False,
        "score": 1.0,
    }


async def test_the_limit_reaches_the_tier_that_runs() -> None:
    """Clamped once, at the service, exactly as `GET /search` is: the ceiling
    lives beside the `Settings` field and this route declares only a floor.
    Fails: a `le=` here, which is the same number spelled twice, or a route
    that drops `limit` and always asks for ten."""
    kit = await _kit(result_limit=20)
    async for client in _client(_app(kit.service)):
        await client.get("/search/suggest", params={"q": _TYPED, "limit": 10_000})
    assert kit.prefix_tier.calls == [(_TYPED, 20)]


async def test_a_limit_of_zero_is_refused(client: httpx.AsyncClient) -> None:
    """`ge=1`, the same floor `GET /search` declares. A zero-result box is a
    request nobody meant to make."""
    response = await client.get("/search/suggest", params={"q": _TYPED, "limit": 0})
    assert response.status_code == 422


async def test_the_suggest_route_holds_no_household_and_no_embedder(
    client: httpx.AsyncClient,
) -> None:
    """**A `DefaultUserIdDep` here would be a `SELECT` per keystroke** to
    resolve an id nothing downstream reads -- `SearchService.suggest` runs no
    blend, so there is no watch-state term and no taste term for a household
    to change. And no embedder: `?mode=semantic`'s 422 has no analogue here
    because there is no lane to be missing.

    Asserted on `/openapi.json` rather than on behaviour, because the defect
    is a parameter or a failure response that *exists*: this app points at a
    database nothing listens on, so a household read would 500 rather than
    quietly cost a round trip, and a case asserting a 200 would pass against a
    version that had wired one on a reachable database.
    """
    operation = (await client.get("/openapi.json")).json()["paths"]["/search/suggest"]["get"]
    assert {one["name"] for one in operation["parameters"]} == {"q", "tier", "limit"}
    assert set(operation["responses"]) == {"200", "422"}


# --- the service's own seam ------------------------------------------------


async def test_a_tier_the_service_cannot_serve_is_not_reachable_from_the_route() -> None:
    """**A coverage assertion over the enum, and it needs its own premise.**

    The service selects its collaborator out of a `dict` keyed by
    `SuggestTier`, so a member added to the enum and not to that map is a
    `KeyError` inside a request -- a 500 on a type-ahead box, on the tier
    somebody added and nobody wired. Behaviourally invisible today, because
    both of the two members are wired.

    Fails: a third member of `SuggestTier`, or a map built from a literal pair
    that stopped matching it.
    """
    kit = await _kit()
    wired: dict[SuggestTier, SuggestIndex] = kit.service._tiers
    assert set(SuggestTier), "the enum is empty, so the coverage claim is vacuous"
    assert set(wired) == set(SuggestTier)
    assert len({id(one) for one in wired.values()}) == len(SuggestTier), (
        "two tiers sharing one index is the mutation the two-armed case above kills"
    )
