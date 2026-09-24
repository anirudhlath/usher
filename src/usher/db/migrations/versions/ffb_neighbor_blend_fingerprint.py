"""`title_neighbors` carries the fingerprint of the blend that computed it."""

import sqlalchemy as sa
from alembic import op

revision = "ffb"
down_revision = "ffa"
branch_labels = None
depends_on = None

# M6's three-signal blend, as `blend_fingerprint()` would have serialised it.
_M6_BLEND_FINGERPRINT = "6697a3e1eaca411cbae890e54a4c665a"


def upgrade() -> None:
    op.add_column(
        "title_neighbors",
        sa.Column(
            "blend_fingerprint",
            sa.Text(),
            nullable=False,
            server_default=_M6_BLEND_FINGERPRINT,
        ),
    )
    # Dropped immediately: the default exists only to make the `NOT NULL` add
    # possible over existing rows. Left in place, a future writer that forgot
    # the column would silently mint rows claiming M6's blend.
    op.alter_column("title_neighbors", "blend_fingerprint", server_default=None)


def downgrade() -> None:
    op.drop_column("title_neighbors", "blend_fingerprint")
