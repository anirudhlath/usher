"""`row_provider_settings`, and the two statements this whole port is."""

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.repository import RowProviderSettingsRepository

_OVERRIDES = "SELECT slug_prefix, enabled FROM row_provider_settings"

# One statement, one writer.
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
