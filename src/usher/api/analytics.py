"""The outcome half of `search_queries`, at the boundary.

PRD 10 gives that table nine columns and says it cannot ship until every one
of them has a named writer. F2 wrote the seven a search already knows. The
other two -- `clicked_title_id` and `played` -- are things a *client* does
afterwards, so their writers are routes, and this module is the one line of
code all three of them share.

**Three call sites, one function, because the interesting part is the
absorption rather than the call.** `GET /titles/{id}` reports the click,
`POST /titles/{id}/play` and `POST /episodes/{id}/play` report the play, and
each does it with one `await` and no branching of its own. Spelled inline the
`except` below would exist three times, and a route that grew a fourth
attribution would be the one that forgot it.

**An analytics write must never fail the request it rides on**, which is PRD
08's *"a degraded subsystem narrows functionality; it never fails a request
local state can answer"* applied to the narrowest possible subsystem: the
household asked for a title, the title is right there, and whether Usher
managed to note down where they came from changes nothing about the answer.
`SearchService._record_search` makes the identical call one layer down, and
this is the same catch for the same reason -- **`except UsherPortError` and
deliberately not `except Exception`**: a `RepositoryConflict` means the store
refused the row and the household still gets its title, while a `TypeError`
out of here is a bug in Usher and a bug absorbed into a log line is billed as
an outage.

The catch is defence in depth rather than a path either shipped caller can
reach: `record_outcome`'s only refusal is a `clicked_title_id` naming no
title, the click writer names the title whose row it has just read, and the
play writer names none at all. It is exercised by injecting a repository that
raises, which is what makes a guard against a promise nobody breaks testable
at all.

**Nothing here logs the `search_id` or the title.** The failure is legible
without them -- the exception says what the store refused and the row that was
lost is one row -- and `search_queries` is household state whose home is that
table rather than a log line, which is the argument `_record_search` makes
about the query text one layer down.
"""

import uuid

from loguru import logger

from usher.ports.errors import UsherPortError
from usher.ports.repository import SearchQueryRepository

__all__ = ["record_search_outcome"]


async def record_search_outcome(
    queries: SearchQueryRepository,
    query_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID,
    clicked_title_id: uuid.UUID | None,
    played: bool,
) -> None:
    """Attribute one search to what the client just did, or do nothing.

    `query_id` is `None` for a request that carried no `?search_id=` **and**
    for one whose value was not a UUID at all -- `deps.get_search_id` collapses
    those two before they arrive here, because a client is not owed a 422 for
    a piece of optional telemetry it attached to a resource that exists. A
    `query_id` that is a real UUID and names no row is a no-op one layer
    further down, in the `UPDATE` itself.

    `clicked_title_id` and `played` are passed straight through and are never
    both meaningful in one call: **the click writer names a title and passes
    `played=False`; the play writer passes `played=True` and no title.** That
    split is the whole design and it is stated on the port -- a single writer
    setting both would make `clicked_title_id` mean *"the last thing this
    household did"* rather than *"which result it opened"*.
    """
    if query_id is None:
        return
    try:
        await queries.record_outcome(
            query_id, user_id=user_id, clicked_title_id=clicked_title_id, played=played
        )
    except UsherPortError as exc:
        logger.error(
            "a search outcome was refused; this search keeps the attribution it had: {error}",
            error=str(exc) or type(exc).__name__,
        )
