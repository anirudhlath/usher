"""`POST /admin/rows/regenerate` -- PRD 07's admin row action.

**The route enqueues; the worker generates.** PRD 06 states it as a
constraint on the artefact -- *"`LLMRow.build()` only hydrates stored output.
Generation happens in a background job -- never in the request path"* -- and
this is the enqueue site that makes it one. Nothing on this path holds an
`LLMClient` or a `CurationService`, which is asserted structurally in
`tests/unit/test_api_rows.py` rather than left to review: a route that held
either would make a curation failure an HTTP failure and would buy a paid
completion inside a request, at whatever concurrency an admin UI's retry
button produces.

## What the 202 promises, and what it does not

**It promises exactly one thing: this household's `(curate, <user id>)` row is
on the queue, or was already there, at `JobPriority.DEMAND`.** The response
carries that pair and nothing else, because it is the only fact about the row
that a reader can still act on -- see `usher.api.dto.rows` for the columns
deliberately absent and what each of them would misstate.

**It does not promise that a completion will be bought, and there is no return
value here that could.** Four measured behaviours of `_ENQUEUE` -- the first
three in `usher.domain.jobs.JobKind.CURATE`, pinned by
`tests/integration/test_job_queue.py`; the fourth pinned at this route, in
`tests/integration/test_rows_route.py`, because it is a consequence of the
priority this route sends and of no other:

- **A repeat at the same priority writes 0 rows and is coalesced**, which is
  PRD 06's *"one modest completion per user per day"* working. The 202 is the
  same either way, deliberately: an operator pressing the button twice has not
  made a mistake, and `enqueue` cannot distinguish creating a job from
  promoting one -- both answer 1 -- so a status code that varied would be
  varying on a number that means neither thing.
- **A repeat arriving while the generation is `running` is discarded by
  `complete()` regardless of priority.** The requested generation never runs,
  and the caller was told 202. That is the wanted answer for a cost rule
  ("one per day"), and it is a genuine limit on what "accepted" means: a
  client that needs a generation *newer than* one already in flight has to
  arrange that above the queue.
- **A parked job is not un-parked or promoted by asking again**, at any
  priority -- PRD 08's *"re-enqueueing does not un-park... and a parked job's
  priority is not promoted behind their back either"*. A household whose
  candidate pool cannot be served parks and stays parked until an operator
  releases it. This is the shape of "accepted" that delivers nothing, and the
  channel that shows it is `usher sync-status` and PRD 10's
  `usher.jobs.parked{kind="curate"}`, not this response.
- **A repeat does not repoint the trace link, and from this route it never
  can.** `_ENQUEUE` sets `traceparent = COALESCE(excluded.traceparent,
  jobs.traceparent)` *inside* the `DO UPDATE` gated on `jobs.priority <
  excluded.priority`. The statement names that cost and offers exactly one
  escape from it -- *"a demand promotion (M5) raises the priority and
  therefore does write"* -- and **that escape is unavailable here**, because
  `DEMAND` is the top of the scale, so a repeat from this route can never
  promote and therefore never rewrites the link. Every press after the first
  is traced to a span the row does not reference: the worker links back to
  whichever press *created* the row, minutes or hours earlier. That is the
  first bullet seen from telemetry rather than a second defect -- the run that
  happens is the one the first press asked for, so the link it carries is the
  correct one for the work being done -- but it means the trace is not a
  channel for "did the press I just made do anything". Pinned by
  `tests/integration/test_rows_route.py::test_asking_twice_writes_nothing_the_second_time`,
  which already observes the unchanged `updated_at` this follows from.

## `USHER_LLM_ENABLED=false` still answers 202, and the route does not look

A deployment with the LLM disabled registers no `CURATE` handler
(`composition.build_worker` registers it only when `composition.llm_client`
built one), so the job is enqueued and stays `pending`. **That is the right
answer, and the argument is that the setting this route would have to consult
is a fact about *one process*, not about the deployment.** `JobKind.INDEX`
under an embedder and `JobKind.DERIVE` under a metadata provider already work
this way: the enqueue is unconditional and the *claim* is conditional, so work
waits for a process that can run it instead of being refused by one that
cannot. A server started without a key, beside a `usher work` started with
one, is an ordinary deployment -- `USHER_WORKER_ENABLED=false` on the server
is already documented for the one-worker-per-deployment rule -- and a route
that read the `llm_enabled` setting would refuse exactly that shape, on
evidence it does not have. (Spelled there without the attribute-access dot on
purpose: `tests/unit/test_config.py::test_every_setting_is_read_by_something`
scans `src/` for `.<field>`, so prose spelling it that way satisfies that
check on behalf of a reader which, here, does not exist.)

The cost is real and is stated rather than hidden: a deployment that has not
enabled the LLM -- **which is the shipped default** (`usher.config`), so this
is what the button does out of the box rather than something an operator has
to arrange first -- answers 202 to a regeneration and leaves a row nothing will
ever claim. Two things keep that from being silent, and neither is a status
code. It is **one row, not a leak** -- `(kind, key)` is unique, so pressing the
button a hundred times leaves one. And it shows up as
PRD 10's `usher.jobs.queued{kind="curate"}` never returning to zero, which is
the series that gauge exists to carry: `depth()` promises a key per kind for
exactly this reason, because a gauge that stops reporting a series is
indistinguishable from one reporting zero.

## There is no 503 here to give a `code` to

PRD 07's RFC 9457 envelope has one worked example, `503 source_unavailable`,
and it survived `GET /titles/{id}` and `GET /home` because neither holds a
`SourceAdapter` and so neither can reach an upstream at all.
**That is a claim about reads, and it does not transfer here unchanged** --
not because this route writes (`GET /titles/{id}` writes too, once, on
purpose) but because what it writes to is a subsystem that can be down while
the process is up. That subsystem is the job queue, which is Postgres, which
`/health/ready` already reports as 503 for the entire process. Answering 503
here would say "this endpoint is degraded, retry it" about a deployment in
which every endpoint is down. The handler therefore catches nothing -- a
`PortUnavailable` propagates and becomes an ordinary 500 -- and
`tests/unit/test_api_rows.py::test_an_unreachable_queue_is_not_translated_into_a_503`
is what keeps the `except` from being added back.

**The envelope has since landed and this argument is unchanged**, which is
the point of stating it structurally rather than as a wait. ADR-0030
**preserves** it as a standing rule: the vocabulary holds no
`queue_unavailable` and no `database_unavailable` of any spelling, and this
paragraph is the reason recorded there. The 500 is also a 500 by rule rather
than by omission -- ADR-0030 refuses an `internal_error` member because
nothing emits one, measured rather than inferred from the case above, which
asserts a status code and nothing about the body.

## Two things this route deliberately does not do

**It does not invalidate `RowCache`.** The tempting one-liner: the cache is in
this process, the route is in this process, so clear the household's entries.
It would accomplish nothing -- the generation has not happened yet, so the very
next `GET /home` would repopulate the same entries from the *same* stored rows,
and the entry would still be last night's when the worker finally lands the new
one. PRD 06 already records the consequence and its owner: the cache is in the
API process, the curation job runs under `usher work`, and cross-process
invalidation is M9's.

**It takes no request body.** Not a household, not a row count, not a
`force` flag. There is one household in M8 (PRD 01's authentication seam) and
`DefaultUserIdDep` is where that is decided for every route at once; the row
budget is prompt text, which PRD 08's row-weights-are-code rule makes code
rather than a parameter; and a `force` flag would be a request to defeat
`(kind, key)`, which is the deduplication the milestone's cost claim rests on.
"""

from fastapi import APIRouter, status

from usher.api.deps import DefaultUserIdDep, JobQueueDep
from usher.api.dto.rows import RegenerateResponse
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.jobs import JobRequest
from usher.telemetry import current_traceparent

router = APIRouter(prefix="/admin/rows", tags=["admin"])


@router.post(
    "/regenerate",
    response_model=RegenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_rows(queue: JobQueueDep, user_id: DefaultUserIdDep) -> RegenerateResponse:
    """Ask for this household's curated rows to be generated again.

    The key is the household and never a `generation_id` or a timestamp:
    `(kind, key)` is unique, so keying per request would deduplicate nothing
    and would buy one completion per press (`JobKind.CURATE`).

    `enqueue`'s return value is discarded, and that is a decision rather than
    an oversight -- it answers 1 for a job it created *and* for one it merely
    promoted, and 0 for a repeat already at this priority, so it cannot be
    rendered into a response without inviting a reading it does not support.

    `get_session` commits this as it commits any other request, which is what
    makes the row durable rather than a flush the response outlives.
    """
    await queue.enqueue(
        [
            JobRequest(
                kind=JobKind.CURATE,
                key=str(user_id),
                # The same rung `api/routers/titles.py`'s demand promotion uses
                # (`services/titles.py`): a human is waiting on this, so it goes
                # in front of the nightly sweep. Promote-never-demote means it
                # would also lift a `curate` job a *future* background enqueue
                # had left at a lower rung -- future deliberately, because no
                # such producer exists: this is the only site in `src/` that
                # enqueues the kind at all, `usher curate` generates directly
                # rather than enqueueing, and nothing schedules it -- the
                # nightly run is an operator's cron entry invoking that
                # command, the way `usher similar --rebuild` already is. So
                # the promotion is the behaviour an operator pressing this
                # button would be asking for, not one anything can exercise
                # today.
                priority=JobPriority.DEMAND,
                # PRD 10's "why did the title I just opened take 45 seconds",
                # for the one kind whose answer is measured in dollars: the
                # worker's span links back to this request, minutes later.
                traceparent=current_traceparent(),
            )
        ]
    )
    return RegenerateResponse(kind=JobKind.CURATE, key=str(user_id))
