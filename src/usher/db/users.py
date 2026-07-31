"""The singleton default user, for the composition roots that need one.

PRD 01 leaves authentication as a seam and `usher.domain.watch.User` says
what stands in it until then: "a singleton default user (`is_default=True`)".
Nothing had ever created that row, because nothing before M4 wrote a
`watch_state` -- and `watch_states.user_id` is a real foreign key, so the
watch-state lane is unrunnable without it.

**Deliberately not a repository port.** A port exists so `services/` can
depend on behaviour without depending on `db/` (ADR-0009), and no service
needs this: `WatchStateSyncService` takes a `user_id` per call precisely
because deciding *which* user a source's history belongs to is M5's
question, not a service's. The two composition roots are the only callers,
and they are already allowed to import `db/`. Adding an ABC, a fake, and a
contract suite for one `SELECT` would be a port with nothing on the other
side of it.

Replaced, not extended, when real authentication lands: at that point
"which user" comes from a request rather than from a row flagged
`is_default`, and this module's whole reason to exist goes with it.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.ids import new_id

DEFAULT_USER_NAME = "default"

# `ON CONFLICT (name) DO NOTHING` and then a re-read, rather than
# `RETURNING id` alone: two processes racing to run their first sync would
# otherwise have the loser insert nothing and read `None` from `RETURNING`,
# and a caller that took that at face value would hand `WatchStateMerge` a
# null `user_id`. The winner's row is the answer for both.
_INSERT = """
INSERT INTO users (id, name, is_default) VALUES (:id, :name, true)
ON CONFLICT (name) DO NOTHING
"""

# `ORDER BY is_default DESC, created_at, id` rather than a bare `LIMIT 1`:
# nothing constrains `is_default` to one row (it is a plain boolean column,
# not a partial unique index), so "the default user" has to be a *stable*
# choice or two runs of the same command could write history to two
# different users. The `name` half of the predicate is what makes this
# terminate when a user already exists under that name without the flag --
# the insert below conflicts on `name`, so without it the second read would
# find nothing and the caller would be handed no id at all.
_SELECT_DEFAULT = """
SELECT id FROM users WHERE is_default OR name = :name
ORDER BY is_default DESC, created_at, id LIMIT 1
"""


async def ensure_default_user(session: AsyncSession, *, name: str = DEFAULT_USER_NAME) -> uuid.UUID:
    """The `is_default` user's id, creating the row if this is a first run.

    Flushes, never commits -- the same rule every repository in this package
    follows. The caller owns the transaction.
    """
    with session.no_autoflush:
        existing = (
            await session.execute(text(_SELECT_DEFAULT), {"name": name})
        ).scalar_one_or_none()
        if existing is not None:
            return uuid.UUID(str(existing))
        await session.execute(text(_INSERT), {"id": new_id(), "name": name})
        found = (await session.execute(text(_SELECT_DEFAULT), {"name": name})).scalar_one()
    return uuid.UUID(str(found))
