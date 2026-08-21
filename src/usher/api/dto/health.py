"""Response DTOs for the health endpoints.

Review item 11: a plain `dict[str, object]` return type generates
`{"type": "object"}` in OpenAPI -- no fields, no types, nothing a
generated client can use. PRD 07 promises a typed HTTP contract
("Clients codegen typed models in any language"); these are the first
two of it.
"""

from datetime import datetime

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
    that costs an upstream **socket** per poll -- and because ADR-0018's
    rule is that a handshake is not delivery, so the honest answer to "is
    push healthy" is a ledger and not a probe, at any price.

    `crashed_sources`, `recovered_claims` and `recovered_at` are reported on
    exactly those terms: every one of them is already in memory, none of them
    costs a statement or a socket, and none of them is in the status code.
    """

    # ⚠️ **A comment, not the docstring, and that placement is the finding.**
    # `LaneReport` is a pydantic model, so pydantic emits its class docstring
    # as the JSON-Schema `description` and FastAPI publishes it at
    # `/openapi.json`. A first draft of this correction put the sentences
    # below into the docstring and thereby shipped an internal agent-rules
    # path, a `⚠️` glyph and an internal task id into the public API
    # contract -- measured on `LaneReport.model_json_schema()["description"]`.
    # **A `src/` docstring is not automatically prose: on a model, on a route
    # handler and on a `Field(description=...)` it is a wire artifact.**
    #
    # The correction itself: the paragraph above used to argue that readiness
    # must not probe because a probe costs "1-5 s per request", a figure
    # nobody had ever taken. It is 0.1253 s -- M10 S1, 2026-08-15,
    # `.claude/rules/emby-push-and-ingest.md`. So the price was never the
    # strong half of the argument; the socket and ADR-0018 are, and they are
    # what the docstring now says on its own.
    push: list[str]
    worker: bool

    # The lanes whose task has *finished*, which is not a state a healthy lane
    # reaches -- `PushSupervisor.run` returns only after the failure ceiling
    # and `_guard` catches everything else. Reported beside `push` because
    # neither list alone tells "the lane crashed" from "the lane was never
    # started", and those are different operator actions. Named here first:
    # M10's S10 computed it and nothing in `src/` read it.
    crashed_sources: list[str]

    # How many abandoned claims **this process** has taken back since it
    # started -- the total `JobWorker.recover()` returned, summed, never a
    # fresh query. Three things about it, and each is a decision:
    #
    # `None` means *not probed*, exactly as `SourceStatus.push_available`
    # does, and it is **not `0`**. With `USHER_WORKER_ENABLED=false` beside a
    # `usher work` container -- the split topology PRD 08 prices -- this
    # process never calls `recover()`, so `0` would assert "no orphans" about
    # a question it never asked.
    #
    # It is a **total**, not a rate, because a readiness body is read by a
    # poller with no memory: a rate is what an alert wants and a total is what
    # a probe can honestly carry.
    #
    # ⚠️ **It is per process, so it cannot see a peer's orphans that the peer
    # recovered.** Two workers each report what they took back and the sum is
    # the truth. Stated rather than solved: the deployment-wide number is a
    # `SELECT count(*) FROM jobs WHERE status = 'running' AND updated_at <=
    # clock_timestamp() - ...`, and this endpoint is polled every 2 s against
    # a table with no index on `status = 'running'` (`ix_jobs_claim` is
    # partial on `pending`, `ix_jobs_parked` on `parked`) that M4 measured at
    # 1,126,674 rows. A dashboard reads that from Postgres on its own beat.
    recovered_claims: int | None

    # The instant of the last recovery pass that found something -- `None`
    # while this process has recovered nothing, whether because it never
    # asked or because there was nothing to take back. Deliberately not
    # "when recovery last ran": a probe polled every 2 s would then carry a
    # timestamp that moves on its own and says nothing.
    recovered_at: datetime | None


class ReadinessResponse(BaseModel):
    status: str
    checks: ReadinessChecks
    lanes: LaneReport
