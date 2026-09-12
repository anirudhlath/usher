"""PRD 07's RFC 9457 problem document -- the shape, and the frozen vocabulary."""

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
    """The machine-readable `code`. **Seven members, closed by ADR-0030.**"""

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


# : The two routes whose non-2xx is deliberately **not** a problem document, : each with
# the reason it is exempt.
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
