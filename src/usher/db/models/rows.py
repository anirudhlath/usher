"""`row_provider_settings` — PRD 09's boundary call 9 coming due.

M7 refused this table on the ground that *"a `row_providers` table with nine
rows all reading `enabled = true` is indistinguishable from no table, right up
until an operator finds it and expects toggling it to do something"*, and it
named the admin API as the condition. The admin API is M9's, so the table
ships — **empty**, which is the half of the refusal that survives: an absent
row means enabled, which is exactly what *"providers are enabled by
registration in code"* already means.

**Nine was true when the call was written and is not now.** `row_providers()`
returns **ten** as of `CuratedProvider` (`src/usher/services/rows/__init__.py`),
and PRD 09's counted fact is corrected in the same commit rather than left to
age.

**Not seeded with ten slugs.** A migration hard-coding the registry would be a
second copy of `services/rows/__init__.py` with nothing anywhere to detect
drift — the exact shape `_SUSPENDABLE_INDEXES`' literal `CREATE INDEX` strings
needed a dedicated round-trip case to stop. Reconciliation between the table
and the registry belongs to the admin task, which is also the only thing that
can report a slug in one and not the other.

No `set_updated_at` trigger: `jobs`' precedent, already named in
`test_migration_creates_the_updated_at_triggers`' comment block — this table's
one writer is an admin route that sets `updated_at` explicitly on every
statement, and that trigger set is asserted **exactly**, so a trigger here
would be a failing case in another file.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base


class RowProviderSettingRow(Base):
    """One provider's operator-set override. Three columns and no surrogate
    id.

    **`RowProvider.slug_prefix` is the natural key**, and its own port
    docstring is why: it is *"declared rather than derived"* and *"bounded at
    ten"*, a name a dashboard and an operator already hold. A surrogate id
    would add a column nothing reads while permitting two rows for one
    provider — a state no admin route could interpret — which is the identical
    argument `genome_tags.tag_id` and `title_embeddings.title_id` both make.

    `Text` rather than `String(N)`: a slug prefix is bounded by the registry
    and not by a width anybody measured, and pinning one into the schema would
    make a longer provider name a migration.
    """

    __tablename__ = "row_provider_settings"

    slug_prefix: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # `server_default` so a hand-written `INSERT` cannot leave it NULL, and no
    # `onupdate=` and no trigger: the one writer names this column on every
    # statement. See the module docstring.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The empty string is a slug prefix no provider can have and a row an
        # admin route would render as a nameless toggle.
        CheckConstraint("slug_prefix <> ''", name="ck_row_provider_settings_slug_not_empty"),
        # No index beyond the primary key. The whole read is "the overrides",
        # which is at most ten rows, and the primary key already serves the
        # per-slug lookup an admin route makes.
    )
