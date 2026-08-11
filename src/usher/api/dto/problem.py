"""PRD 07's RFC 9457 problem document -- the *shape*, not the vocabulary.

The envelope was deferred four times, each time on a structural argument that
was tested rather than restated: M3's admin routes had no `code` vocabulary to
name; M5's `GET /events` has no status code left once it has answered `200
text/event-stream`; M7's `GET /home` holds no `SourceAdapter` and so has no
503 to give a `code` to; and M8's `POST /admin/rows/regenerate` enqueues and
returns 202. M9 pays it in **two passes**, and this module is the first.

**This module defines the shape and deliberately does not design the
vocabulary.** `ProblemCode` carries exactly the codes the already-shipped
surface emits plus the one the cursor codec is about to, and the names are
provisional: whether a 404 is generic (`not_found`) or per-resource
(`title_not_found`), what a failed playback ticket answers, and what an
unreachable source is called are **group V's ADR-0030** to settle. Six
independent drafters designing this vocabulary alongside their own routes
proposed seventeen members under two mutually exclusive conventions for the
same status, which is the whole reason the two passes exist. V1 *moves and
freezes* what it finds here; it does not create a second vocabulary
somewhere else.

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
    """The machine-readable `code`, and **the names are provisional.**

    Exactly what the shipped surface emits, plus `INVALID_CURSOR` for the
    opaque cursor codec landing beside this. Nothing else -- a member no
    route emits is a contract with no behaviour behind it, which is the same
    rule `SseEventKind` keeps one module over.

    **Do not add a member here to serve a route you are writing.** Add it,
    and then let ADR-0030 decide whether it survives: the open question this
    enum is deliberately not answering is generic-versus-per-resource
    (`not_found` against `title_not_found`), and answering it per route is
    how a four-code budget became a seventeen-code proposal.
    """

    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    INVALID_CURSOR = "invalid_cursor"


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
