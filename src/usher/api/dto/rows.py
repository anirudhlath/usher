"""Response shape for `POST /admin/rows/regenerate` (PRD 07)."""

from pydantic import BaseModel

from usher.domain.jobs import JobKind


class RegenerateResponse(BaseModel):
    """The enqueued job's identity.

    See the module docstring for what is deliberately absent, and why each of those
    would misstate the queue.
    """

    kind: JobKind
    key: str


class RowProviderResponse(BaseModel):
    """One registered row provider, and whether it composes (PRD 07, E2)."""

    slug: str
    enabled: bool


class RowProviderUpdate(BaseModel):
    """The whole body of `PUT /admin/rows/providers/{slug}`."""

    enabled: bool
