"""Wire shapes for `/admin/bootstrap` (PRD 07's Admin table, PRD 04).

What is deliberately absent from `BootstrapTriggerResponse` is the whole
design: no percentage, no estimate, no "already running" flag, and no
`ImportRun`. `JobQueue.enqueue` returns a row count that cannot tell a fresh
row from a promotion of one already in flight (`usher.domain.jobs.JobKind`
carries the measured table), so every one of those would be a number this
route cannot honestly produce. Progress belongs to `GET
/admin/bootstrap/status`, which reads the durable `import_runs` checkpoint,
and to the `bootstrap.progress` event.
"""

from pydantic import BaseModel

from usher.domain.jobs import JobKind


class BootstrapTriggerResponse(BaseModel):
    """`POST /admin/bootstrap/{phase}`'s whole body: the enqueued job's
    identity, on the shape `RegenerateResponse` and `SyncTriggerResponse`
    already use for the other two admin triggers.

    `key` is a `BootstrapPhase`'s wire value, so a client that posted
    `/admin/bootstrap/all` reads `all` back and can watch for exactly that
    key. It is never a job id: `(kind, key)` is what the queue deduplicates
    on, and the row's own `id` changes under a promotion.
    """

    kind: JobKind
    key: str
