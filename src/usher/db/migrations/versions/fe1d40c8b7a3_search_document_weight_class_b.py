"""titles.credit_names, and weight class B"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fe1d40c8b7a3"
down_revision: str | Sequence[str] | None = "fd7c3a5b9e12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Weight class B fills the slot fa2b6c1e9d30 reserved for it -- "reserved for cast and
# crew and deliberately absent rather than filled with something else" -- and slots
# between A and C, which is where ts_rank's default weight vector {0.1, 0.2, 0.4, 1.0}
# puts it.
_COLUMN = """
ALTER TABLE titles ADD COLUMN search_document tsvector
GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(name,          '')), 'A')
   || setweight(to_tsvector('english', coalesce(original_name, '')), 'A')
   || setweight(to_tsvector('english', usher_array_text(credit_names)), 'B')
   || setweight(to_tsvector('english', coalesce(overview,      '')), 'C')
   || setweight(to_tsvector('english', coalesce(tagline,       '')), 'C')
   || setweight(to_tsvector('english', usher_array_text(genres)),    'D')
   || setweight(to_tsvector('english', usher_array_text(keywords)),  'D')
) STORED
"""

# fa2b6c1e9d30's expression, restated rather than imported: a migration must
# not import application code that can change under it, and `downgrade()` has
# to restore the definition that revision installed rather than whatever the
# model happens to say today.
_M6_COLUMN = """
ALTER TABLE titles ADD COLUMN search_document tsvector
GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(name,          '')), 'A')
   || setweight(to_tsvector('english', coalesce(original_name, '')), 'A')
   || setweight(to_tsvector('english', coalesce(overview,      '')), 'C')
   || setweight(to_tsvector('english', coalesce(tagline,       '')), 'C')
   || setweight(to_tsvector('english', usher_array_text(genres)),    'D')
   || setweight(to_tsvector('english', usher_array_text(keywords)),  'D')
) STORED
"""


def upgrade() -> None:
    # The column first, populated to '{}' for every existing row by the
    # server_default, so the expression below has something non-NULL to read
    # the moment it exists. `usher_array_text` is STRICT: a NULL here would
    # null the whole document.
    op.add_column(
        "titles",
        sa.Column(
            "credit_names",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # Then the forced rewrite. Drop-and-re-add rather than
    # `UPDATE titles SET id = id`: both force a full rewrite, but the update
    # also leaves 1.27M dead tuples and their WAL behind for autovacuum.
    op.drop_index("ix_titles_search_document", table_name="titles")
    op.execute("ALTER TABLE titles DROP COLUMN search_document")
    # (fa2b6c1e9d30's third step, CREATE OR REPLACE FUNCTION, is deliberately
    # absent -- the wrapper's body is unchanged. See the docstring.)
    op.execute(_COLUMN)
    # `fastupdate=off` is carried across the rewrite deliberately and is
    # spelled out at both call sites rather than shared through a dict: its
    # default pending list cost a measured 231 buffers against 30 -- 7.7x
    # read amplification, invisible in EXPLAIN unless you look at buffers --
    # and an index recreated without it loses that silently.
    op.create_index(
        "ix_titles_search_document",
        "titles",
        ["search_document"],
        postgresql_using="gin",
        postgresql_with={"fastupdate": "off"},
    )


def downgrade() -> None:
    # The mirror order, and it is a forced rewrite in this direction too: a
    # downgrade that dropped only the column would leave every stored document
    # computed under the class-B expression while the schema claimed the M6
    # one. head -> base -> head in tests/integration/test_migrations.py is what
    # exercises it.
    op.drop_index("ix_titles_search_document", table_name="titles")
    op.execute("ALTER TABLE titles DROP COLUMN search_document")
    op.drop_column("titles", "credit_names")
    op.execute(_M6_COLUMN)
    # `fastupdate=off` is carried across the rewrite deliberately and is
    # spelled out at both call sites rather than shared through a dict: its
    # default pending list cost a measured 231 buffers against 30 -- 7.7x
    # read amplification, invisible in EXPLAIN unless you look at buffers --
    # and an index recreated without it loses that silently.
    op.create_index(
        "ix_titles_search_document",
        "titles",
        ["search_document"],
        postgresql_using="gin",
        postgresql_with={"fastupdate": "off"},
    )
