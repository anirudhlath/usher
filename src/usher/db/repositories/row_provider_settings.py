"""`row_provider_settings`, and the two statements this whole port is.

Implements `RowProviderSettingsRepository` (`usher.ports.repository`). No
partial-index predicate to repeat in the `ON CONFLICT` clause -- `slug_prefix`
is the table's only key and its primary key is the only index on it -- so the
upsert is the plain form every other single-key repository in this package
uses (`taste.py`'s `user_taste`, one row per key, is the closest sibling).

Same session ownership as every other repository here: flushes, never
commits.
"""

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.repository import RowProviderSettingsRepository

_OVERRIDES = "SELECT slug_prefix, enabled FROM row_provider_settings"

# One statement, one writer. `now()` in the VALUES list rather than the
# column's `server_default`, because `server_default` only ever fires on the
# INSERT branch -- an update needs `updated_at` written explicitly on every
# statement (this table carries no `set_updated_at` trigger; see
# `db/models/rows.py`'s module docstring for why), and `excluded.updated_at`
# is what carries the same `now()` value into the DO UPDATE branch.
_SET_ENABLED = """
INSERT INTO row_provider_settings (slug_prefix, enabled, updated_at)
VALUES (:slug_prefix, :enabled, now())
ON CONFLICT (slug_prefix) DO UPDATE SET
    enabled = excluded.enabled,
    updated_at = excluded.updated_at
"""


class PostgresRowProviderSettingsRepository(RowProviderSettingsRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def overrides(self) -> Mapping[str, bool]:
        # no_autoflush: a plain read has no business flushing anything, and
        # this session may be shared with other repositories that have
        # pending, unrelated work.
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(_OVERRIDES))).all()
        return {row.slug_prefix: row.enabled for row in rows}

    async def set_enabled(self, slug: str, *, enabled: bool) -> None:
        await self._session.execute(text(_SET_ENABLED), {"slug_prefix": slug, "enabled": enabled})
