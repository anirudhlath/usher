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

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.ids import new_id
from usher.domain.watch import User

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
#
# **All four columns, not just the id**, since M7: `RowContext` carries a
# `User` and not a `user_id`, and `User.id` is `default_factory=new_id` -- so a
# caller that built one from the name alone would compose a screen for a
# household that has never existed. Every read would return nothing and it
# would render as an empty household rather than as a bug, which is this
# milestone's headline failure arriving through a constructor default.
# `scalar_one_or_none()` still takes the first column, so widening this is
# transparent to `ensure_default_user`.
_SELECT_DEFAULT = """
SELECT id, name, is_default, created_at FROM users WHERE is_default OR name = :name
ORDER BY is_default DESC, created_at, id LIMIT 1
"""


async def _resolve(session: AsyncSession, name: str) -> RowMapping:
    """The default user's row, creating it if this is a first run.

    Flushes, never commits -- the same rule every repository in this package
    follows. The caller owns the transaction.
    """
    with session.no_autoflush:
        existing = (await session.execute(text(_SELECT_DEFAULT), {"name": name})).mappings().first()
        if existing is not None:
            return existing
        await session.execute(text(_INSERT), {"id": new_id(), "name": name})
        return (await session.execute(text(_SELECT_DEFAULT), {"name": name})).mappings().one()


async def ensure_default_user(session: AsyncSession, *, name: str = DEFAULT_USER_NAME) -> uuid.UUID:
    """The `is_default` user's id, creating the row if this is a first run."""
    return uuid.UUID(str((await _resolve(session, name))["id"]))


async def default_user(session: AsyncSession, *, name: str = DEFAULT_USER_NAME) -> User:
    """The same row as a domain model, for the one caller that needs the whole
    thing: `RowContext.user`.

    One statement, not two. The alternative -- `ensure_default_user` followed
    by a read of the row it just resolved -- is a second round trip per home
    request for a `created_at` nothing reads, and this way the id and the name
    cannot disagree about which row they came from.
    """
    row = await _resolve(session, name)
    return User(
        id=uuid.UUID(str(row["id"])),
        name=str(row["name"]),
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
    )
