"""`row_provider_settings` -- which row providers an operator has turned off."""

from abc import ABC, abstractmethod
from collections.abc import Mapping

__all__ = ["RowProviderSettingsRepository"]


class RowProviderSettingsRepository(ABC):
    """One row per provider an operator has ever touched; nothing for the rest.

    The whole port is the discipline of never letting "never configured"
    collapse into "explicitly disabled".

    Both methods flush and return, and neither commits.
    """

    @abstractmethod
    async def overrides(self) -> Mapping[str, bool]:
        """Every slug an operator has ever set, and only those.

        A slug missing from the returned mapping has never been touched and
        means *enabled*. A caller reaching for `.get(slug, False)` has
        reintroduced the collapse this port exists to refuse -- the read is
        `slug in overrides()`, and the default the caller applies to a miss
        must be `True`, never `False`.
        """

    @abstractmethod
    async def set_enabled(self, slug: str, *, enabled: bool) -> None:
        """Upsert one provider's override, `enabled` included.

        Writing `True` is not a no-op and not a delete: it is still a
        recorded operator action, kept exactly as a `False` one would be, so
        this table's rows are "everything an operator has touched", never
        "everything an operator has disabled". A second call for the same
        `slug` replaces the stored value; it does not add a row.
        """
