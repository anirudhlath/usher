"""The `code` vocabulary, encoded rather than written down."""

import ast
import pathlib
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI, status
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from usher.api.app import create_app
from usher.api.dto.problem import PROBLEM_EXEMPT_ROUTES, PROBLEM_EXEMPTIONS, ProblemCode
from usher.api.errors import _CODE_FOR_STATUS
from usher.config import Settings

_REPO = pathlib.Path(__file__).parents[2]
_API = _REPO / "src" / "usher" / "api"

# The one member the vocabulary may not rename: PRD 07's worked example of this
# envelope is this code, spelled this way. Every scan in this file uses it as the
# control, because a scan that finds it cannot be a scan that found nothing.
_ANCHOR = "source_unavailable"

# The DSN `tests/unit/test_api_health.py` already uses: nothing listens on
# port 1, so a connection refused there fails exactly as a down database
# does, with no container.
_UNREACHABLE_DSN = "postgresql+asyncpg://usher:usher@127.0.0.1:1/usher"
_SECRET = "0123456789abcdef0123456789abcdef"


def _settings() -> Settings:
    return Settings(database_url=_UNREACHABLE_DSN, secret_key=_SECRET)


def _api_modules() -> list[pathlib.Path]:
    return sorted(_API.rglob("*.py"))


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """Every `APIRoute` the app really serves.

    `include_router` appends one opaque `fastapi.routing._IncludedRouter`
    per router rather than flattening, so the obvious one-level walk finds
    **zero** of Usher's fourteen routes -- an empty list a `for` loop
    iterates happily. Same descent `tests/unit/test_api_problem.py` makes,
    for the same reason.
    """
    found: list[APIRoute] = []

    def descend(routes: Sequence[BaseRoute]) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                descend(inner.routes)
            elif isinstance(route, APIRoute):
                found.append(route)

    descend(app.routes)
    return found


def _member_value(attr: str) -> str:
    """A `ProblemCode.<ATTR>` reference as the string it puts on the wire.

    An attribute the enum does not have is reported as its lower-cased name
    rather than raising, so a router naming a member the vocabulary never declared
    fails the closure comparison **by name** -- `emitted but not declared:
    ['title_not_found']` -- instead of dying with a `KeyError` three frames
    away from anything a reader can act on.
    """
    member = ProblemCode.__members__.get(attr)
    return member.value if member is not None else attr.lower()


def _emitted_codes() -> set[str]:
    """Every code named anywhere under `src/usher/api/`.

    Two harvests, because there are two ways to name one. A
    `ProblemCode.<MEMBER>` attribute access is the sanctioned spelling; a
    string literal passed as `code=` is the one that bypasses the enum, and
    it is harvested precisely so that bypass is not invisible.
    """
    harvested: set[str] = set()
    for path in _api_modules():
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ProblemCode"
            ):
                harvested.add(_member_value(node.attr))
            elif (
                isinstance(node, ast.keyword)
                and node.arg == "code"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                harvested.add(node.value.value)
    return harvested


def _emitted_pairs() -> set[tuple[str, int]]:
    """Every `(code, status)` a `ProblemException` in `src/usher/api/` raises.

    `status_code=status.HTTP_404_NOT_FOUND` is resolved through
    `fastapi.status` rather than pattern-matched on the name, so a route
    that spells the integer directly and a route that spells the constant
    are the same fact here.
    """
    pairs: set[tuple[str, int]] = set()
    for path in _api_modules():
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ProblemException"
            ):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
            code_node, status_node = keywords.get("code"), keywords.get("status_code")
            if not (
                isinstance(code_node, ast.Attribute)
                and isinstance(code_node.value, ast.Name)
                and code_node.value.id == "ProblemCode"
            ):
                continue
            if isinstance(status_node, ast.Constant) and isinstance(status_node.value, int):
                pairs.add((_member_value(code_node.attr), status_node.value))
            elif isinstance(status_node, ast.Attribute):
                resolved = getattr(status, status_node.attr, None)
                if isinstance(resolved, int):
                    pairs.add((_member_value(code_node.attr), resolved))
    return pairs


def _translated_pairs() -> set[tuple[str, int]]:
    """Every `(code, status)` `api/errors.py` answers without a raise site naming it.

    `_CODE_FOR_STATUS` is the other half of `_emitted_pairs`: those statuses
    come from machinery Usher does not control, so no `ProblemException` in
    `src/usher/api/` carries them and an AST harvest cannot see them.
    """
    return {(code.value, status) for status, code in _CODE_FOR_STATUS.items()}


def _resource_nouns(app: FastAPI) -> set[str]:
    """The collection names the URL space already carries.

    Literal path segments only. A path **parameter** is deliberately
    excluded and that exclusion is the whole rule: a literal segment names a
    collection the server holds (`/titles`, `/episodes`, `/admin/sources`)
    and RFC 9457's `instance` already carries it, so a code that re-spells
    one says nothing a client could not read off the path. A parameter is
    the *value the client supplied*, which is why `ticket_invalid` is a
    legitimate 404 code and `title_not_found` is not.

    Both the plural and the singular, so `images` in the route table catches
    `image_not_found`. Segments under four characters are dropped -- `play`
    stays, two-letter noise would match half the alphabet.
    """
    nouns: set[str] = set()
    for route in _api_routes(app):
        for segment in route.path.split("/"):
            if not segment or segment.startswith("{") or len(segment) < 4:
                continue
            nouns.add(segment)
            nouns.add(segment.removesuffix("s"))
    return nouns


@pytest.fixture
async def readiness_client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def test_the_codes_the_api_emits_are_exactly_the_members_of_the_vocabulary() -> None:
    """The closure, in both directions, and it is the whole mechanism.

    A code `src/usher/api/` emits that `ProblemCode` does not hold is a code no client
    was told about; a member nothing emits is a contract with no behaviour behind it.
    `tests/unit/test_api_openapi.py` holds the stronger half of the same claim: this
    case reads the whole of `src/usher/api/`, which cannot tell a code a *route* can
    reach from one only a helper names, and that one walks each route's own call graph.
    """
    emitted = _emitted_codes()
    assert _ANCHOR in emitted, (
        f"the AST harvest of {_API} found {sorted(emitted)}, which does not include the code "
        "`api/routers/playback.py` demonstrably emits -- the scan is measuring nothing"
    )

    members = {code.value for code in ProblemCode}
    assert emitted - members == set(), (
        f"`src/usher/api/` emits codes the vocabulary does not hold: {sorted(emitted - members)}"
    )
    assert members - emitted == set(), (
        f"`ProblemCode` holds members nothing under {_API} emits: "
        f"{sorted(members - emitted)} -- delete a member no route can produce, rather "
        "than ship a contract with no behaviour behind it."
    )


def test_a_members_name_and_its_wire_string_are_one_thing() -> None:
    """`SOURCE_UNAVAILABLE = "source_unavailable"`, never two halves that drift apart.

    Cheap, and it is what lets every scan in this file report a code the enum lacks by
    its wire spelling: `_member_value` falls back to lower-casing the attribute, which
    is only a faithful reconstruction because this holds.
    """
    for code in ProblemCode:
        assert code.name.lower() == code.value, f"{code.name} puts {code.value!r} on the wire"


def test_no_404_is_spelled_per_resource() -> None:
    """The careless spelling of the convention this vocabulary refuses.

    One generic `not_found`, because RFC 9457's `instance` already carries the path, so
    a per-resource member is a second spelling of what the document already says, it
    grows the vocabulary linearly with the resource count, and every one of those
    members is handled identically by a client. The one candidate for an exception is a
    title that exists with no playable copy, and that is separated by **status**
    (`409 not_playable`) rather than by code. `ticket_invalid` is a 404 and is not an
    exception: it is not a statement about a resource at all.
    """
    offenders = [
        code.value
        for code in ProblemCode
        if "not_found" in code.value and code.value != "not_found"
    ]
    assert offenders == [], (
        f"per-resource 404 codes: {offenders}. ADR-0030 rules for one generic `not_found`; "
        "if a single path really does produce two 404s a client would act on differently, "
        "amend the ADR's table and this case together."
    )


def test_no_404_code_names_a_collection_the_route_table_already_names() -> None:
    """The careful spelling of the same defect, which is why it sits beside the one above.

    A linter catches the careless spelling only -- `title_not_found` dies on a
    `_not_found$` regex while `no_such_title`, `title_missing` and `unknown_episode`
    all sail past it being exactly the same contract. What they have in common is that
    they name a collection the URL space already names, which is what makes them a
    second spelling of `instance`. Scoped to 404 deliberately: `source_unavailable`
    names a source and is a 503, saying which *dependency* is down, and `not_playable`
    names no collection at all.
    """
    app = create_app(_settings())
    nouns = _resource_nouns(app)
    assert {"title", "episode", "source"} <= nouns, (
        f"the route walk derived {sorted(nouns)} and is missing collections the app serves -- "
        "the descent through _IncludedRouter has stopped working and this case is measuring "
        "nothing"
    )

    pairs = _emitted_pairs() | _translated_pairs()
    assert ("not_found", 404) in pairs, (
        f"the status harvest found {sorted(pairs)}, which does not include the 404 "
        "`api/errors.py` translates -- the scan is measuring nothing"
    )

    offenders = {
        code: sorted(noun for noun in nouns if noun in code)
        for code, raised in sorted(pairs)
        if raised == 404 and any(noun in code for noun in nouns)
    }
    assert offenders == {}, (
        f"404 codes naming a collection the path already names: {offenders}. A per-resource "
        "404 is a second spelling of RFC 9457's `instance`."
    )


def test_every_code_carries_one_status_everywhere_it_is_raised() -> None:
    """The stability rule, encoded: the status for a given code never changes.

    It is the half of the contract a client's `switch` rests on -- a code
    that means 404 on one route and 409 on another is two codes wearing one
    name, and nothing but agreement between the raise sites keeps them
    together. Both spellings of a raise are covered: a route naming its own
    code through `ProblemException`, and `api/errors.py`'s
    `_CODE_FOR_STATUS`, which translates the statuses raised by machinery
    Usher does not control.
    """
    pairs = _emitted_pairs()
    assert (_ANCHOR, 503) in pairs, (
        f"the ProblemException harvest found {sorted(pairs)}, which does not include the 503 "
        "`api/routers/playback.py` demonstrably raises -- the scan is measuring nothing"
    )

    raised: dict[str, set[int]] = {}
    for code, code_status in pairs:
        raised.setdefault(code, set()).add(code_status)

    disagreements = {code: sorted(seen) for code, seen in raised.items() if len(seen) > 1}
    assert disagreements == {}, f"raised with more than one status: {disagreements}"
    for translated_status, code in _CODE_FOR_STATUS.items():
        seen = raised.get(code.value, {translated_status})
        assert seen == {translated_status}, (
            f"_CODE_FOR_STATUS answers {code.value} for {translated_status}, which "
            f"`ProblemException` raises with {sorted(seen)}"
        )


def test_the_status_translation_table_covers_only_what_usher_does_not_raise_itself() -> None:
    """The table covers only statuses raised before any Usher handler runs.

    Starlette's router raises 404 for an unrouted path and 405 for a method a route
    does not have, and FastAPI raises 422 for a rejected request. Every status Usher's
    own code raises names its code at the raise site through `ProblemException`, so an
    entry for 409 or 503 would be a member of a lookup nothing looks up -- and a guess
    about intent, since a later 503 that is not "the source is down" would silently
    answer `source_unavailable`. The cost is real and named rather than hidden: a route
    raising a bare `HTTPException(503)` is handed to FastAPI's default handler and
    silently opts out of the envelope.
    """
    assert set(_CODE_FOR_STATUS) == {404, 405, 422}, (
        f"_CODE_FOR_STATUS covers {sorted(_CODE_FOR_STATUS)}; ADR-0030 scopes it to the "
        "statuses Starlette and FastAPI raise before any Usher handler runs"
    )


async def test_the_readiness_probe_stays_exempt_and_answers_its_own_shape(
    readiness_client: httpx.AsyncClient,
) -> None:
    """`/health/ready`'s 503 is not a problem document.

    The mechanism exempts it **by accident** today -- the route mutates
    `response.status_code` and raises nothing, so no exception handler can see it, and
    "held by convention" is the class of safety property `api/errors.py` exists to stop
    relying on. The degraded assertions come first and they are the point: "no `code`
    key in the body" is also what a 404, a route that never ran, or an app built
    without the health router produces, so the absence claim is worth nothing until the
    degraded path is proved to have run.
    """
    response = await readiness_client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] is False

    assert response.headers["content-type"] == "application/json"
    assert "type" not in body, body
    assert "code" not in body, body


def test_the_exemption_set_is_closed_over_every_route_the_app_serves() -> None:
    """Exactly two routes are exempt, and every other path is not.

    `tests/unit/test_api_problem.py` asserts the two by name, which is a
    claim about the two; this is a claim about the **set**, taken over
    `create_app()`'s own route table, so a route added later that quietly
    joins the exemption fails rather than passing silently.

    Two rather than one: `/health/ready`'s consumers gate on the status code and never
    parse the body, and `GET /events` has no status code left once it has answered
    `200 text/event-stream`.
    """
    app = create_app(_settings())
    served = {route.path for route in _api_routes(app)}
    assert "/titles/{title_id}" in served, (
        "the descent through _IncludedRouter stopped working; this case is measuring nothing"
    )

    exempt = set(PROBLEM_EXEMPT_ROUTES)
    assert exempt == set(PROBLEM_EXEMPTIONS), "the derived set and the reasoned map disagree"
    assert exempt == {"/health/ready", "/events"}, sorted(exempt)
    assert exempt <= served, f"exempt paths the app does not serve: {sorted(exempt - served)}"
    assert served - exempt == served - {"/health/ready", "/events"}
