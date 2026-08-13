"""In-memory `RowProviderSettingsRepository`.

**Divergences from `PostgresRowProviderSettingsRepository`, stated rather than
discovered.**

1. **No CHECK.** Postgres refuses an empty `slug_prefix` with
   `ck_row_provider_settings_slug_not_empty`; this fake stores whatever string
   it is handed. Nothing in this milestone calls `set_enabled` with a slug
   that did not come from the registry, so no case needs the refusal here, and
   modelling it would be a second copy of a constraint that already lives in
   the migration.
2. **No `updated_at`.** The column exists for an operator reading the table
   directly; no port method returns it, so there is nothing for a fake to get
   wrong by omitting it.
3. **No transaction.** `overrides()` and `set_enabled()` both act on the same
   plain `dict` with no flush/commit distinction -- a write is visible to
   every subsequent call on this instance immediately. `PostgresRowProviderSettingsRepository`'s
   equivalent, real distinction (flushed but not yet committed, invisible to a
   second session) is Postgres-only and pinned in
   `tests/integration/test_row_provider_settings_repository.py`.
"""

from collections.abc import Mapping

from usher.ports.repository import RowProviderSettingsRepository


class FakeRowProviderSettingsRepository(RowProviderSettingsRepository):
    def __init__(self) -> None:
        self._overrides: dict[str, bool] = {}

    async def overrides(self) -> Mapping[str, bool]:
        return dict(self._overrides)

    async def set_enabled(self, slug: str, *, enabled: bool) -> None:
        self._overrides[slug] = enabled
