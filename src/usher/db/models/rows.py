"""`row_provider_settings` — PRD 09's boundary call 9 coming due."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base


class RowProviderSettingRow(Base):
    """One provider's operator-set override.

    **`RowProvider.slug_prefix` is the natural key** -- declared rather than
    derived, bounded at ten, a name a dashboard and an operator already hold. A
    surrogate id would add a column nothing reads while permitting two rows for
    one provider, a state no admin route could interpret.

    `Text` rather than `String(N)`: a slug prefix is bounded by the registry and
    not by any width, and pinning one into the schema would make a longer
    provider name a migration.
    """

    __tablename__ = "row_provider_settings"

    slug_prefix: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # `server_default` so a hand-written `INSERT` cannot leave it NULL, and no
    # `onupdate=` and no trigger: the one writer names this column on every
    # statement.
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
