"""`row_provider_settings` -- the writer M7 refused until a route could reach it.

Implemented by
`usher.db.repositories.row_provider_settings.PostgresRowProviderSettingsRepository`.

M7's boundary call 9 refused this table on the ground that a `row_providers`
table with nine rows all reading `enabled = true` is indistinguishable from no
table at all -- "right up until an operator finds it and expects toggling it
to do something". M9 has the route that gives toggling it a meaning (E2); this
port is the writer the refusal named, landing one task ahead of the route that
calls it. `m09a` already shipped the table itself, empty, with no seeded row
(`db/models/rows.py`).

**Keyed on `slug_prefix`, never on the class name.** `services/rows/__init__
.py`'s `BASE_SCORES` is keyed by `Provider.__name__` because it is an internal
ladder that never leaves the process; a settings key is operator-facing, and
the slug is the one identifier that already lives outside the codebase --
`usher.row.build.duration`'s `provider` label and `usher home`'s leftmost
column (`ports/rows.py`'s `slug_prefix` docstring calls the name "declared
rather than derived" for exactly this reason). A class rename must not
silently re-enable a provider somebody turned off.

**Absence means enabled, and the table holds overrides -- not a mirror of the
registry.** `ROW_PROVIDERS` is the registry; a stored row per provider would be
a second one, and boundary call 9's own argument -- "a list a composition root
assembles by hand is a list the tenth provider is forgotten from" -- applies at
least as hard to a list a migration would have to assemble. So `overrides()`
answers only for slugs an operator has actually touched, in either direction:
setting a slug back to `True` is still a recorded action and still a row, not
a delete, because a value that happens to equal the default is still a value
somebody set. The *rendered*, one-row-per-provider list PRD 09 asks for is
E2's job -- the registry left-joined onto this map.

**No `user_id`, on M8's `llm_calls` precedent.** A column this milestone
cannot fill with two different values does not ship.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping

__all__ = ["RowProviderSettingsRepository"]


class RowProviderSettingsRepository(ABC):
    """One row per provider an operator has ever touched; nothing for the rest.

    Two methods, and the whole port is the discipline of never letting
    "never configured" collapse into "explicitly disabled" -- the trap M7's
    boundary call warns about, arriving here as a read rather than as a
    migration seed.

    Same session ownership as every repository in this package: both methods
    flush and return, and neither commits.
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
