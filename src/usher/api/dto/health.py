"""Response DTOs for the health endpoints.

Review item 11: a plain `dict[str, object]` return type generates
`{"type": "object"}` in OpenAPI -- no fields, no types, nothing a
generated client can use. PRD 07 promises a typed HTTP contract
("Clients codegen typed models in any language"); these are the first
two of it.
"""

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: str


class ReadinessChecks(BaseModel):
    database: bool
    migrations: bool


class ReadinessResponse(BaseModel):
    status: str
    checks: ReadinessChecks
