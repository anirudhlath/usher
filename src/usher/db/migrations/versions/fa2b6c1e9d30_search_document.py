"""the search document: extensions, an immutable wrapper, a generated column"""

from collections.abc import Sequence

from alembic import op

revision: str = "fa2b6c1e9d30"
down_revision: str | Sequence[str] | None = "f1a7d3c9e824"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTENSIONS = ("vector", "pg_trgm", "fuzzystrmatch")

# IMMUTABLE is a promise the planner takes on trust. It is honest here and
# only here: array_to_string is STABLE because `anyarray` admits element
# types whose output depends on a GUC (timestamptz and TimeZone), and text
# has no such dependency. Narrowing the signature to text[] is what makes the
# promise true -- do NOT widen it to anyarray to "reuse" it.
_WRAPPER = """
CREATE FUNCTION usher_array_text(text[]) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
    AS $$ SELECT array_to_string($1, ' ') $$
"""

# Verified populated and weighted: a row named `Iron` with genres {autumn,winter} and no
# other populated input stores `'autumn':2 'iron':1A 'winter':3`.
_COLUMN = """
ALTER TABLE titles ADD COLUMN search_document tsvector
GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(name,          '')), 'A')
   || setweight(to_tsvector('english', coalesce(original_name, '')), 'A')
   -- Weight B is reserved for cast and crew and is deliberately absent
   -- rather than filled with something else (boundary call 2). There is no
   -- Person/Credit table, model or port anywhere in src/; ports/metadata.py
   -- defers all of it to M7 by name. When M7 lands Credit, filling this is a
   -- migration plus one forced rewrite, not a redesign.
   || setweight(to_tsvector('english', coalesce(overview,      '')), 'C')
   || setweight(to_tsvector('english', coalesce(tagline,       '')), 'C')
   || setweight(to_tsvector('english', usher_array_text(genres)),    'D')
   || setweight(to_tsvector('english', usher_array_text(keywords)),  'D')
) STORED
"""


def upgrade() -> None:
    for extension in _EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    op.execute(_WRAPPER)
    op.execute(_COLUMN)
    op.create_index(
        "ix_titles_search_document",
        "titles",
        ["search_document"],
        postgresql_using="gin",
        postgresql_with={"fastupdate": "off"},
    )
    op.create_index(
        "ix_titles_name_trgm",
        "titles",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_titles_name_trgm", table_name="titles")
    op.drop_index("ix_titles_search_document", table_name="titles")
    op.execute("ALTER TABLE titles DROP COLUMN search_document")
    # After the column is gone nothing depends on the wrapper, so this is a
    # plain DROP rather than a CASCADE. The three extensions stay -- see the
    # module docstring.
    op.execute("DROP FUNCTION usher_array_text(text[])")
