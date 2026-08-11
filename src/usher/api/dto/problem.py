"""PRD 07's RFC 9457 problem document -- the shape, and the frozen vocabulary.

The envelope was deferred four times, each time on a structural argument that
was tested rather than restated: M3's admin routes had no `code` vocabulary to
name; M5's `GET /events` has no status code left once it has answered `200
text/event-stream`; M7's `GET /home` holds no `SourceAdapter` and so has no
503 to give a `code` to; and M8's `POST /admin/rows/regenerate` enqueues and
returns 202. M9 paid it in **two passes**, and this module is where both
landed: the shape first, and then the vocabulary once
`POST /titles/{id}/play` had produced a real `503 source_unavailable` to
design against.

**`ProblemCode` is now closed by
`docs/prd/decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md`,
and the closure is a mechanism rather than a convention.** ADR-0030 carries
the vocabulary as a table; `tests/unit/test_api_problem_vocabulary.py` parses
that table and compares it to `set(ProblemCode)` in both directions, and
AST-walks `src/usher/api/` for every code the surface emits. **A route that
needs a member this enum does not have amends the ADR in the same commit** --
which is what makes growth a recorded amendment rather than the drift that was
measured before the split: six independent drafters designing this vocabulary
alongside their own routes proposed seventeen members under two mutually
exclusive conventions for the same status.

Two facts fall out of the shape and both are load-bearing:

- **`instance` is `request.url.path` and never `request.url`.** A 422 whose
  `instance` carried the query string is the credential leak `api/errors.py`
  exists to stop, through a different field -- and it is about to matter
  more, because M9's search route writes `?q=` to `search_queries`.
- **`status` is derived from one value, never written twice**, so the
  document and the response line cannot disagree. `api/errors.py`'s
  `problem_response` builds the `JSONResponse` with `document.status`, and
  `tests/unit/test_api_problem.py` pins that structurally, because two
  integers that happen to agree today are also what a document built beside
  its response produces.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Self

from pydantic import BaseModel

#: RFC 9457 section 3. Not `application/json`: the media type is how a
#: client tells a problem document from a route's own body without parsing
#: it, and it is the half of the RFC that costs nothing and is most often
#: skipped.
PROBLEM_MEDIA_TYPE: Final = "application/problem+json"

_TYPE_PREFIX: Final = "https://usher.dev/errors/"


class ProblemCode(StrEnum):
    """The machine-readable `code`. **Seven members, closed by ADR-0030.**

    **Do not add a member here to serve a route you are writing.** Amend
    ADR-0030's table in the same commit or the suite is red: the table and
    this enum are compared in both directions, so a member with no row and a
    row with no member are both failures. That is deliberate friction --
    answering a vocabulary question per route is how a four-code benchmark
    became a seventeen-code proposal under two mutually exclusive
    conventions for the same status.

    **A member's Python name lower-cases to its wire string**, always. The
    two are one thing and a case pins it, which is what lets every scan in
    `tests/unit/test_api_problem_vocabulary.py` report a code the enum lacks
    by the spelling a client would have seen.

    **The naming rule for a new member: `<subject>_<state>`.**
    `INVALID_CURSOR` is the one member that does not follow it and it is kept
    rather than renamed -- ADR-0030 ruling 5 has the reason, which is that
    the rename would land in two documents V1 does not own and is otherwise
    taste. It is not a precedent.

    **The three beyond the four-member benchmark, each with the route that
    forces it.** `api/routers/playback.py` is the first route in Usher whose
    honest answer is "the source is down", and the bar applied to each
    candidate was: does an *existing* member already carry this meaning, and
    would a client branch differently on it?

    - `SOURCE_UNAVAILABLE` (503). Unavoidable twice over. PRD 07's worked
      example of this envelope *is* this code, spelled this way, down to
      `"type": "https://usher.dev/errors/source-unavailable"` -- and
      `api/errors.py`'s `_CODE_FOR_STATUS` has no 503 entry, so without a
      member a `503` is handed to FastAPI's default handler and answers
      `{"detail": ...}` with no `code` at all. Measured, not assumed: that
      is the second of the two reds `test_api_playback.py`'s headline case
      was driven through.
    - `NOT_PLAYABLE` (409). Same mechanism -- no 409 in `_CODE_FOR_STATUS`
      either -- and no existing member means "your household holds this and
      no copy of it can be played". `NOT_FOUND` is the nearest and it is
      wrong in the way that matters to a client: it says *retry somewhere
      else*, where this says *stop asking*. ADR-0030 ruling 3 ratifies the
      409 over `200 {"targets": []}`.
    - `TICKET_INVALID` (404). The one that is genuinely arguable, and it
      follows `INVALID_CURSOR` rather than starting a new convention. Both
      are an **opaque codec refusing its own input**; neither is a statement
      about a resource, so neither is a per-resource 404. A client meeting
      `not_found` on `GET /stream/{ticket}` cannot tell "your ticket
      expired, ask `/play` again" -- the whole remedy -- from "there is no
      such route", and telling those apart without parsing prose is the
      entire job of a `code`.

    **There is no `title_not_found`, `episode_not_found` or
    `image_not_found`, and that is ADR-0030 ruling 1 rather than an
    absence.** One generic `NOT_FOUND`: RFC 9457's `instance` already carries
    the resource, a per-resource member grows the vocabulary linearly with
    the resource count, every one of them is handled identically by a
    client, and no path in M9 produces two 404s a client would act on
    differently -- the one candidate, a title with no playable copy, is
    separated by *status* (`409 NOT_PLAYABLE`) rather than by code. Encoded
    twice, against the careless spelling (`title_not_found`) and the careful
    one (`no_such_title`).
    """

    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    INVALID_CURSOR = "invalid_cursor"
    SOURCE_UNAVAILABLE = "source_unavailable"
    NOT_PLAYABLE = "not_playable"
    TICKET_INVALID = "ticket_invalid"


def problem_type(code: ProblemCode) -> str:
    """The `type` URI, derived from the code and never hand-written.

    One function rather than a member-to-URL table, so a code and its type
    cannot drift apart -- PRD 07's worked example is `source_unavailable` ->
    `https://usher.dev/errors/source-unavailable`, and that is the whole
    rule.
    """
    return f"{_TYPE_PREFIX}{code.value.replace('_', '-')}"


def problem_title(code: ProblemCode) -> str:
    """The short human-readable summary, derived from the code for the same
    reason `problem_type` is. PRD 07's example pairs `source_unavailable`
    with `"Source unavailable"`."""
    return code.value.replace("_", " ").capitalize()


#: The two routes whose non-2xx is deliberately **not** a problem document,
#: each with the reason it is exempt. Group H's "every route that can fail
#: declares its problem responses" scan imports this rather than re-deriving
#: it, so a route left alone on purpose does not read as a route somebody
#: forgot.
#:
#: A mapping rather than a bare set because the reason is the point: an
#: exemption with no recorded cause is indistinguishable from an oversight,
#: and `test_every_exemption_names_a_route_and_carries_a_reason` fails on
#: both an unreasoned entry and one naming a route the app does not serve.
#: `PROBLEM_EXEMPT_ROUTES` is derived from it so the two cannot disagree.
#:
#: The exemptions are about what a **handler** answers. A 405 comes from the
#: router before any handler runs, so these routes answer a problem document
#: for one of those like every other route does.
#:
#: **`/health/ready` is exempt by accident today and asserted anyway.** The
#: handler mutates `response.status_code` and raises nothing, so no exception
#: handler can see it -- which means the exemption is currently held by the
#: shape of the code rather than by this mapping. "Held by convention" is the
#: class of safety property `api/errors.py` exists to stop relying on, so
#: `test_the_readiness_probe_stays_exempt_and_answers_its_own_shape` drives
#: the real degraded path and asserts the body carries neither `type` nor
#: `code` -- after asserting the degraded path ran, because "no `code` key" is
#: also what a 404, a route that never ran, or an app with no health router
#: produces.
#:
#: **The set is closed over `create_app()`'s own route table**, by
#: `test_the_exemption_set_is_closed_over_every_route_the_app_serves`, so a
#: route added later that quietly joins the exemption fails rather than
#: passing silently. ADR-0030 records why these two and no others.
PROBLEM_EXEMPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "/health/ready": (
            "Its real consumers -- Kubernetes, Docker healthcheck, load balancers -- gate on "
            "the status code and never parse the body, so the 503 keeps ReadinessResponse, "
            "which reports which check failed rather than naming a code."
        ),
        "/events": (
            "RFC 9457 formats a response body, and once this route has answered 200 "
            "text/event-stream there is no status code left to carry one; its in-stream "
            "failure vocabulary is an SSE event (resync_required) instead. Its 422 for a "
            "malformed ?titles= is answered before the stream starts and is a problem "
            "document like any other."
        ),
    }
)

#: The exempt paths alone, for a caller that only needs the membership test.
PROBLEM_EXEMPT_ROUTES: Final[frozenset[str]] = frozenset(PROBLEM_EXEMPTIONS)


class ProblemResponse(BaseModel):
    """RFC 9457's five members, plus PRD 07's `code`.

    **Named `ProblemResponse`, and not `ProblemDetail`, on purpose.**
    `tests/unit/test_api_dto.py` discovers response models by
    `name.endswith("Response")` and asserts that none of them declares a
    field named like a credential or typed `SecretStr`. This model is
    rendered on the one path in the API that has just been handed a
    rejected request body, so it is exactly the model that scan should
    cover -- renaming it would leave the scan silently.

    `errors` is an RFC 9457 **extension member** (section 3.2) carrying the
    pydantic error list with `input` already stripped by `api/errors.py`.
    Absent rather than null when there is nothing to say, which is the one
    empty-value convention `api/dto/` keeps.
    """

    type: str
    title: str
    status: int
    code: ProblemCode
    detail: str
    instance: str
    errors: list[dict[str, Any]] | None = None

    @classmethod
    def of(
        cls,
        *,
        status: int,
        code: ProblemCode,
        detail: str,
        instance: str,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        """The only sanctioned construction, so `type` and `title` are
        always the derivations rather than whatever a caller typed."""
        return cls(
            type=problem_type(code),
            title=problem_title(code),
            status=status,
            code=code,
            detail=detail,
            instance=instance,
            errors=errors,
        )


__all__ = [
    "PROBLEM_EXEMPTIONS",
    "PROBLEM_EXEMPT_ROUTES",
    "PROBLEM_MEDIA_TYPE",
    "ProblemCode",
    "ProblemResponse",
    "problem_title",
    "problem_type",
]
