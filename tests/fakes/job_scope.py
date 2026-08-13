"""A `JobScope` factory over fakes, for cases that are not about the scope.

`JobWorker` takes a factory rather than a queue and a commit since M9's W1:
`AsyncSession` is not concurrency-safe, so concurrent jobs need a session, a
commit, a handler set and an event buffer each, and the worker opens one scope
per claim and one per job. Most cases in this suite are about something else
entirely -- a span, a metric, a handler -- and would otherwise each carry six
lines of context-manager boilerplate.

**What this helper cannot say, stated where a reader will meet it.** Every
scope it opens shares one `FakeJobQueue`, because the fake *is* the store: one
dict behind one event loop, with no second session to model. So "each job got
its own session" is not a property expressible here at all, and it is asserted
in `tests/integration/test_services_jobs.py` against a real engine, where two
concurrent jobs' connections can be read back and compared. What *is* per-scope
here is what is per-session in production: the commit callable, the handler map
and the `DeferredEventPublisher`.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager

from usher.domain.jobs import Job, JobKind
from usher.ports.events import EventPublisher, NullEventPublisher
from usher.ports.jobs import JobQueue
from usher.services.events import DeferredEventPublisher
from usher.services.jobs import JobScope, JobWorker


async def _no_commit() -> None:
    return None


def worker_over(
    queue: JobQueue,
    handlers: Mapping[JobKind, Callable[[Job], Awaitable[None]]],
    *,
    commit: Callable[[], Awaitable[None]] | None = None,
    events: EventPublisher | None = None,
    concurrency: int = 1,
    batch_size: int = 20,
) -> JobWorker:
    """A worker whose scopes are built from the arguments given.

    `concurrency` defaults to **1** here and to four in
    `tests/unit/test_services_jobs.py`'s own fixture, deliberately: this helper
    serves cases about spans and metrics whose assertions are about one job, and
    a pool would add scheduling to a case that has nothing to say about it. The
    concurrency map is keyed by exactly `handlers`, which is what `JobWorker`
    claims -- so a case cannot enqueue a kind the worker was not given.
    """
    settled = _no_commit if commit is None else commit
    buffer = DeferredEventPublisher(NullEventPublisher() if events is None else events)

    @asynccontextmanager
    async def _scope() -> AsyncIterator[JobScope]:
        yield JobScope(queue=queue, commit=settled, handlers=dict(handlers), events=buffer)

    return JobWorker(
        _scope,
        dict.fromkeys(handlers, concurrency),
        max_in_flight=concurrency,
        batch_size=batch_size,
    )
