"""In-memory `RowProviderSettingsRepository`."""

from collections.abc import Mapping

from usher.ports.repository import RowProviderSettingsRepository


class FakeRowProviderSettingsRepository(RowProviderSettingsRepository):
    def __init__(self) -> None:
        self._overrides: dict[str, bool] = {}

    async def overrides(self) -> Mapping[str, bool]:
        return dict(self._overrides)

    async def set_enabled(self, slug: str, *, enabled: bool) -> None:
        self._overrides[slug] = enabled
