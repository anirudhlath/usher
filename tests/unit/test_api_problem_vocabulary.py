"""ADR-0030's `code` vocabulary, encoded rather than written down.

`tests/unit/test_api_problem.py` covers the envelope's *shape* -- A2's
module, A2's cases. This file covers the **vocabulary**: which members exist,
what status each one carries, and the two mechanisms that keep a parallel
fan-out from growing the set by instinct. It is a separate file because the
two have different owners and different failure modes: a shape defect is a
malformed document, a vocabulary defect is a contract nobody agreed to.

**Why a decision record is the source of truth and not this file.** A
vocabulary that lives in a test is a vocabulary the next route's author
edits in the same commit as the route, which is the drift ADR-0030 exists to
stop -- six independent drafters proposed seventeen members against a budget
of four, under two mutually exclusive conventions for the same 404. The
table lives in `docs/prd/decisions/0030-*.md`; every case here reads it, and
a fan-out task needing a member the design did not give it has to amend a
decision record in the same commit. Growth becomes a recorded amendment
rather than silent drift. The same idiom
`tests/unit/test_decision_register.py` uses on `decisions/README.md`.

**Every scan here carries a control, because a scan that globs nothing
passes identically to a scan that passes.** The AST harvest asserts it found
`source_unavailable` -- which `api/routers/playback.py` demonstrably emits --
before any comparison is read out of it; the table parse asserts the same;
the route walk asserts it found `/titles/{title_id}`, because
`include_router` on FastAPI 0.140 appends one opaque `_IncludedRouter` per
router and a one-level walk finds zero of Usher's routes.
"""

import ast
import pathlib
import re
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
_ADR = (
    _REPO
    / "docs"
    / "prd"
    / "decisions"
    / ("0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md")
)

# The table is read out of a delimited region rather than out of the whole
# document, so a second status-bearing table added by a later amendment --
# the declined members, a worked example -- cannot be unioned into the
# vocabulary by a regex that was only ever aimed at one of them. The markers
# are asserted present; a parse that found no region and a parse that found
# an empty one must not look alike.
_TABLE_BEGIN = "<!-- vocabulary:begin -->"
_TABLE_END = "<!-- vocabulary:end -->"
_ROW = re.compile(r"^\|\s*`([a-z][a-z_]*)`\s*\|\s*(\d{3})\s*\|", re.MULTILINE)

# An amendment's own disposition line, in both spellings the record uses.
# `_amendment_statuses` explains why the colon has to be adjacent.
_AMENDMENT_STATUS = re.compile(r"Status of the amendment:\**\s*([A-Z][a-z]+)")

# The three words an amendment may be in. Anything else is a garbled line, and
# a garbled line is how a status stops being findable while still looking like
# one to a reader.
_DISPOSITIONS = frozenset({"Open", "Accepted", "Declined"})

# The one member D4 landed against a real unreachable source, and the one
# ADR-0030 says it may not rename: PRD 07's worked example of this envelope
# is this code, spelled this way. Every scan in this file uses it as the
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
    rather than raising, so a router naming a member ADR-0030 never declared
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


def _declared() -> dict[str, int]:
    """ADR-0030's table, as `{code: status}`."""
    text = _ADR.read_text()
    assert _TABLE_BEGIN in text and _TABLE_END in text, (
        f"ADR-0030 has lost its {_TABLE_BEGIN}/{_TABLE_END} markers, so the vocabulary "
        "cannot be read out of it"
    )
    region = text.split(_TABLE_BEGIN, 1)[1].split(_TABLE_END, 1)[0]
    rows = _ROW.findall(region)
    parsed = {code: int(code_status) for code, code_status in rows}
    assert len(parsed) == len(rows), f"ADR-0030's table names a code twice: {rows}"
    return parsed


def _amendment_statuses() -> dict[str, str]:
    """Every amendment heading in ADR-0030, mapped to its declared status.

    Two spellings are in the document and both are load-bearing, so the
    pattern takes either: `**Status of the amendment: Open.**` and
    `**Status of the amendment:** Accepted, ...`. Prose that merely *names*
    the line (`` a `Status of the amendment` line ``) does not match, because
    the colon has to follow the word directly.

    Keyed by the heading text rather than by position, so a fourth amendment
    landing between two existing ones cannot silently re-point an assertion
    at the wrong section.
    """
    statuses: dict[str, str] = {}
    heading = ""
    for line in _ADR.read_text().splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        found = _AMENDMENT_STATUS.search(line)
        if found is not None:
            statuses[heading] = found.group(1)
    return statuses


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


def test_the_codes_the_api_emits_are_exactly_the_codes_the_decision_records() -> None:
    """The closure, in both directions, and it is the whole mechanism.

    A member the ADR does not name is a code no client was told about; a
    member the ADR names and nothing emits is a contract with no behaviour
    behind it. Both are equalities now.

    **The second was a containment for the length of the fan-out and H2
    closed it, without deleting anything.** ADR-0030 completed the vocabulary
    before the read routes landed, so a member was allowed to sit with no
    emitter for a while; `invalid_cursor` was named as the case and the only
    one. B7's `GET /browse`, E4's `GET /admin/unmatched` and B12's
    `GET /seasons/{id}/episodes` all call `decode_cursor` now, so the member
    has three emitting routes and the deletion obligation ADR-0030's
    Consequences hand H2 is discharged by measurement rather than by edit.
    `tests/unit/test_api_openapi.py::test_every_member_of_the_vocabulary_has_a_route_that_can_emit_it`
    is the stronger half of the same claim: this case reads the whole of
    `src/usher/api/`, which cannot tell a code a *route* can reach from one
    only a helper names, and that one walks each route's own call graph.
    """
    emitted = _emitted_codes()
    assert _ANCHOR in emitted, (
        f"the AST harvest of {_API} found {sorted(emitted)}, which does not include the code "
        "`api/routers/playback.py` demonstrably emits -- the scan is measuring nothing"
    )

    declared = _declared()
    assert _ANCHOR in declared, (
        f"ADR-0030's table parsed as {sorted(declared)}, which does not include the code PRD "
        "07's worked example spells -- the parse is measuring nothing"
    )

    members = {code.value for code in ProblemCode}
    assert declared.keys() - members == set(), (
        f"ADR-0030 declares codes `ProblemCode` does not have: {sorted(declared.keys() - members)}"
    )
    assert members - declared.keys() == set(), (
        f"`ProblemCode` has members ADR-0030 does not declare: "
        f"{sorted(members - declared.keys())} -- amend the ADR in this commit"
    )
    assert emitted - members == set(), (
        f"`src/usher/api/` emits codes the vocabulary does not hold: {sorted(emitted - members)}"
    )
    assert members - emitted == set(), (
        f"`ProblemCode` holds members nothing under {_API} emits: "
        f"{sorted(members - emitted)} -- ADR-0030's Consequences oblige M9 to delete a member "
        "no route can produce, rather than ship a contract with no behaviour behind it."
    )


def test_a_members_name_and_its_wire_string_are_one_thing() -> None:
    """`SOURCE_UNAVAILABLE = "source_unavailable"`, never a member whose
    Python name and wire string can be changed apart.

    Cheap, and it is what lets every scan in this file report a code the
    enum lacks by its wire spelling: `_member_value` falls back to
    lower-casing the attribute, which is only a faithful reconstruction
    because this holds.
    """
    for code in ProblemCode:
        assert code.name.lower() == code.value, f"{code.name} puts {code.value!r} on the wire"


def test_no_404_is_spelled_per_resource() -> None:
    """The careless spelling of the convention ADR-0030 refused.

    One generic `not_found`, because RFC 9457's `instance` already carries
    the path -- PRD 07's own worked example is
    `"instance": "/titles/01936f2a-.../play"` -- so a per-resource member is
    a second spelling of what the document already says, it grows the
    vocabulary linearly with the resource count, and every one of those
    members is handled identically by a client. The one candidate for an
    exception is a title that exists with no playable copy, and D4 separates
    that by **status** (`409 not_playable`) rather than by code, which
    leaves no path in M9 producing two client-distinguishable 404s.

    `ticket_invalid` is a 404 and is not an exception to this: it is not a
    statement about a resource at all. See the case below.
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
    """The careful spelling of the same defect, and the reason this case
    exists beside the one above.

    A linter catches the careless spelling only -- `title_not_found` dies on
    a `_not_found$` regex and `no_such_title`, `title_missing` and
    `unknown_episode` all sail past it while being exactly the same
    contract. What they have in common is not the suffix; it is that they
    name a collection the URL space already names, which is what makes them
    a second spelling of `instance`.

    Scoped to 404 deliberately. `source_unavailable` names a source and is a
    503: it says which *dependency* is down, which is not a re-spelling of
    the resource the client addressed. `not_playable` names no collection at
    all.
    """
    app = create_app(_settings())
    nouns = _resource_nouns(app)
    assert {"title", "episode", "source"} <= nouns, (
        f"the route walk derived {sorted(nouns)} and is missing collections the app serves -- "
        "the descent through _IncludedRouter has stopped working and this case is measuring "
        "nothing"
    )

    declared = _declared()
    offenders = {
        code: sorted(noun for noun in nouns if noun in code)
        for code, code_status in declared.items()
        if code_status == 404 and any(noun in code for noun in nouns)
    }
    assert offenders == {}, (
        f"404 codes naming a collection the path already names: {offenders}. A per-resource "
        "404 is a second spelling of RFC 9457's `instance`; see ADR-0030."
    )


def test_every_code_carries_one_status_everywhere_it_is_raised() -> None:
    """The stability rule, encoded: the status for a given code never
    changes.

    It is the half of the contract a client's `switch` rests on -- a code
    that means 404 on one route and 409 on another is two codes wearing one
    name, and nothing but agreement between the raise sites keeps them
    together. Both spellings of a raise are covered: a route naming its own
    code through `ProblemException`, and `api/errors.py`'s
    `_CODE_FOR_STATUS`, which translates the statuses raised by machinery
    Usher does not control.
    """
    declared = _declared()
    pairs = _emitted_pairs()
    assert (_ANCHOR, 503) in pairs, (
        f"the ProblemException harvest found {sorted(pairs)}, which does not include the 503 "
        "`api/routers/playback.py` demonstrably raises -- the scan is measuring nothing"
    )

    disagreements = {
        (code, raised): declared[code]
        for code, raised in sorted(pairs)
        if code in declared and declared[code] != raised
    }
    assert disagreements == {}, f"raised with a status ADR-0030 does not give it: {disagreements}"
    for raised_status, code in _CODE_FOR_STATUS.items():
        assert declared.get(code.value) == raised_status, (
            f"_CODE_FOR_STATUS maps {raised_status} to {code.value}, which ADR-0030 gives "
            f"{declared.get(code.value)}"
        )


def test_the_status_translation_table_covers_only_what_usher_does_not_raise_itself() -> None:
    """D4 left open whether 503 and 409 belong in `_CODE_FOR_STATUS`.
    ADR-0030 says they do not, and this is that answer encoded.

    The table exists for statuses raised by machinery Usher does not
    control: Starlette's router raises 404 for an unrouted path and 405 for
    a method a route does not have, and FastAPI raises 422 for a rejected
    request. Every status Usher's own code raises names its code at the
    raise site through `ProblemException` -- so an entry for 409 or 503
    would be a member of a lookup nothing looks up, and worse, it would be a
    guess about intent: a later 503 that is not "the source is down" would
    silently answer `source_unavailable`.

    The cost of leaving them out is real and is named rather than hidden: a
    route raising a bare `HTTPException(503)` is handed to FastAPI's default
    handler and silently opts out of the envelope. Group H's "every route
    that can fail declares its problem responses" scan is what closes that,
    and `test_a_status_with_no_code_in_the_vocabulary_is_left_alone` in
    `tests/unit/test_api_errors.py` is what keeps the delegation deliberate.
    """
    assert set(_CODE_FOR_STATUS) == {404, 405, 422}, (
        f"_CODE_FOR_STATUS covers {sorted(_CODE_FOR_STATUS)}; ADR-0030 scopes it to the "
        "statuses Starlette and FastAPI raise before any Usher handler runs"
    )


def test_the_image_proxys_amendment_is_no_longer_open() -> None:
    """ADR-0030's image amendment has an answer, and `Open` is not one.

    **Why a case rather than a reading.** This record's own text says that a
    request left open while the table states an answer flatly is *"how an
    unanswered question quietly becomes an answered one"* -- it happened to
    the sibling amendment, which was open in one bullet and settled precedent
    in two other files. `GET /images/{image_id}`'s second upstream arm is the
    one M10's spec picked up out of `08-operations.md` as an open defect. So
    the disposition is asserted rather than left to whoever reads the section
    next.

    **The positive control is the whole reason this is trustworthy.** A regex
    that matches nothing passes exactly like one that passes, and this one is
    aimed at prose. So: the scan must find status lines at all, and it must
    find the **accepted** `not_playable` amendment reading `Accepted` --
    a section this file does not otherwise touch and whose disposition has
    been settled since M9. Without that anchor, a renamed heading, a reworded
    status line or a moved section all read as "no longer open".

    **Scoped to the image amendment on purpose.** The `?mode=semantic`
    amendment beside it is a different question with a stronger case for
    minting by this record's own reading, and it is deliberately left `Open`;
    a case asserting *every* amendment is answered would either fail today or
    press the next reader into answering both at once, which is the fan-out
    ADR-0030 exists to prevent.
    """
    statuses = _amendment_statuses()
    assert statuses, (
        f"the amendment scan found no `Status of the amendment:` lines in {_ADR.name} -- "
        "the parse is measuring nothing"
    )

    anchors = [heading for heading in statuses if "not_playable" in heading]
    assert len(anchors) == 1, (
        f"the scan found {len(anchors)} amendments naming `not_playable`, expected the one "
        f"accepted in M9: {sorted(statuses)}"
    )
    assert statuses[anchors[0]] == "Accepted", (
        f"the accepted amendment reads {statuses[anchors[0]]!r}, so the scan is not reading "
        "the dispositions it thinks it is"
    )

    unknown = {
        heading: status for heading, status in statuses.items() if status not in _DISPOSITIONS
    }
    assert unknown == {}, (
        f"amendments whose status is not one of {sorted(_DISPOSITIONS)}: {unknown}"
    )

    image = [heading for heading in statuses if "/images/{image_id}" in heading]
    assert len(image) == 1, (
        f"the scan found {len(image)} amendments naming `GET /images/{{image_id}}`: "
        f"{sorted(statuses)}"
    )
    assert statuses[image[0]] != "Open", (
        "ADR-0030's image amendment is still `Open`. Its residual `PortDataMalformed` arm has "
        "been measured against the live CDN; the record has to say `Accepted` or `Declined` "
        "and carry the rate, or the vocabulary table and `08-operations.md` go on stating an "
        "answer to a question this record calls unanswered."
    )


async def test_the_readiness_probe_stays_exempt_and_answers_its_own_shape(
    readiness_client: httpx.AsyncClient,
) -> None:
    """`/health/ready`'s 503 is not a problem document, and the mechanism
    exempts it **by accident** today -- the route mutates
    `response.status_code` and raises nothing, so no exception handler can
    see it. "Held by convention" is the class of safety property
    `api/errors.py` was written to stop relying on, so it is asserted.

    **The degraded assertions come first and they are the point.** "No
    `code` key in the body" is also what a 404, a route that never ran, or
    an app built without the health router produces, so the absence claim is
    worth nothing until the degraded path is proved to have run.
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

    Two rather than one: `/health/ready`'s consumers gate on the status code
    and never parse the body, and `GET /events` has no status code left once
    it has answered `200 text/event-stream`. The second is one of PRD 07's
    four deferrals that ADR-0030 **preserves** as a standing rule rather
    than discharging.
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
