"""`GET /people/{id}` -- PRD 07's filmography, at the boundary."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import CreditRepositoryDep, PersonRepositoryDep, TitleRepositoryDep
from usher.api.dto.people import PersonResponse
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.errors import ProblemException

router = APIRouter(tags=["people"])

#: What `/openapi.json` says this route answers when it fails. The `422` is
#: declared rather than left to FastAPI, whose automatic one names
#: `HTTPValidationError` while `api/errors.py` answers an RFC 9457 document
#: carrying the same error list under `errors`.
#: `tests/unit/test_api_openapi.py` holds both halves.
_PERSON_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such person."},
    422: {"model": ProblemResponse, "description": "The request was rejected."},
}

# **The route's number, not the port's.** The port's default belongs to
# `PeopleProvider`, which reads the same method for a home-screen row; the day that
# caller wants a different page size, this route must not follow it silently.
FILMOGRAPHY_CREDIT_LIMIT = 50


@router.get(
    "/people/{person_id}",
    response_model=PersonResponse,
    # `groups` is **absent** rather than `[]` when a person has no derived
    # credits (group B's convention for this whole surface), and this flag is
    # the mechanism: `PersonResponse.of` leaves the field unset on that arm.
    # Every other field is passed explicitly on both arms, so nothing else can
    # disappear through it.
    response_model_exclude_unset=True,
    responses=_PERSON_FAILURES,
)
async def get_person(
    person_id: uuid.UUID,
    people: PersonRepositoryDep,
    credits: CreditRepositoryDep,
    titles: TitleRepositoryDep,
) -> PersonResponse:
    """A person and their filmography, grouped by role.

    Groups are `cast` plus one per crew job, ordered with `cast` first and the
    crew labels alphabetically. Within a group, titles are newest first with
    the title id breaking a tie and unknown years last; a title appears once
    per group however many credits put it there, and a person credited in two
    roles on one title appears in both groups.

    At most 50 credits are read, which is this route's own page size and not a
    property of the person: a filmography that comes back at exactly 50
    entries may be truncated, and there is no cursor to page past it.

    Absent rather than empty: a person with no derived credits carries no
    `groups` key at all, because an empty list cannot be told from a
    filmography that has not been derived yet.
    """
    person = await people.get(person_id)
    if person is None:
        # Generic `not_found` rather than a `person_not_found`: RFC 9457's
        # `instance` already carries `/people/{id}`, so a per-resource member
        # would be a second spelling of what the document says.
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="person not found",
        )
    filmography = await credits.list_for_person(person_id, limit=FILMOGRAPHY_CREDIT_LIMIT)
    # One statement for the whole page rather than a `get()` per credit --
    # `list_by_ids`' whole reason -- and it is skipped entirely when there is
    # nothing to hydrate, because `IN ()` is a round trip to learn nothing.
    hydrated = (
        await titles.list_by_ids([credit.title_id for credit in filmography]) if filmography else []
    )
    return PersonResponse.of(person, filmography, hydrated)
