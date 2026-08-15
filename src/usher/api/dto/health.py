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
    """The checks readiness **gates on**, and nothing else.

    `ready` is `all(self.model_dump().values())`, so every field added here
    becomes part of the status code automatically. That is the property
    that makes this model the right place for `database` and `migrations`
    and the wrong place for anything about a source -- see `LaneReport`.
    """

    database: bool
    migrations: bool


class LaneReport(BaseModel):
    """Which background lanes this process is running.

    **Reported, never gated on.** `ReadinessResponse.status` and its HTTP
    code are computed from `checks` alone, and this is deliberately its own
    model rather than two more booleans inside `ReadinessChecks`: `ready`'s
    `all(checks.model_dump().values())` would then take this process out of
    a load balancer because Emby is unreachable, which restarting it cannot
    fix and which PRD 08's own failure table says leaves the catalog fully
    browsable. That inverts the very argument M1's liveness/readiness split
    is built on -- liveness stays off the database because restarting does
    not fix Postgres either.

    `push` is the **names of the sources whose lanes are running**, which is
    a fact about this process, not a probe: it says a lane exists, never
    that its socket is healthy. Whether a channel is *delivering* is
    grounded in a message ledger and is reported by
    `GET /admin/sources/{id}/status`'s `push_available`, because a
    readiness endpoint Docker polls every 2 s must not answer a question
    that costs an upstream **socket** per poll. ⚠️ The *latency* half of
    that argument is weaker than it used to read. What this used to cite as
    "1-5 s per request" was never a number anybody took; the cheapest
    upstream probe is 0.1253 s (M10 S1, 2026-08-15 --
    `.claude/rules/emby-push-and-ingest.md`). What carries the decision is
    ADR-0018 -- a handshake is not delivery, so the honest answer to "is
    push healthy" is a ledger and not a probe, at any price.
    """

    push: list[str]
    worker: bool


class ReadinessResponse(BaseModel):
    status: str
    checks: ReadinessChecks
    lanes: LaneReport
