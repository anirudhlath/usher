"""people, credits, collections, and the FK titles.collection_id has waited for

Revision ID: fd7c3a5b9e12
Revises: fc6d2b81a794
Create Date: 2026-08-03

**The revision-id convention is nearly exhausted and this note is where that
is recorded.** Ids spell one hex letter per migration in chain order --
`a8a0`, `b3f1`, `c7a2`, `d4c9`, `e5b8`, `f1a7` -- and M6 restarted with a
second cycle carrying the sequence in the *second* character: `fa`, `fb`,
`fc`. M7 continues `fd`, `fe`, `ff`. **`ff` is the last id this convention
can produce**, because hex has no letter after `f` in either position and a
third cycle starting with a digit would sort *before* `fa` under a plain
`ls`. M7 needs five migrations and the second cycle has three left, so the
fourth and fifth cannot follow the rule as written; the remedy is to extend
by a character (`ff` -> `ffa` -> `ffb`), which stays correctly ordered and is
unbounded. Task 38 owns retiring the convention in writing; do not silently
invent a different one here.

**Three tables and one ALTER, and the ALTER is the interesting half.**
`titles.collection_id` has existed since a8a0e10ff464 as a bare nullable UUID
with no foreign key that nothing in `src/` ever writes (PRD 02 says exactly
that). This migration does not create the column -- it creates the table the
column points at, adds the constraint, and adds the index PRD 02 had deferred
to M9.

**Reversible, and a down-then-up round trip has e5b8f2c40d17's hazard.**
`downgrade()` drops `collections` and cannot touch `titles.collection_id`, so
a database that has been down and is coming back up holds links to rows that
no longer exist, and `ADD CONSTRAINT` fails with a bare
`ForeignKeyViolationError` naming one row. That is the failure e5b8f2c40d17
reproduced against real Postgres for `watch_states.episode_id` and
`media_items.episode_id`, and
`_adopt_collection_links_orphaned_by_an_earlier_downgrade` is the same
remedy: apply the column's *own* delete rule to the orphans. Here that rule
is `SET NULL`, so orphans are silently NULLed -- the `media_items` branch. It
is silent because it is safe: a title with no collection link is worth
keeping and nothing else on the row depends on it. There is deliberately no
`watch_states`-style refusal branch, because nothing this migration touches
is user state, and saying so keeps the asymmetry legible rather than looking
like a simplification.

**Two triggers, hand-written, because autogenerate cannot see triggers at
all.** `people` and `collections` are both written by
`INSERT ... ON CONFLICT DO UPDATE` out of a temporary staging table, and
SQLAlchemy's `onupdate=` is a Core-side feature with no effect on raw SQL.
Naming: `trg_<table>_set_updated_at`, matching the five that exist. The
exact-set assertion in tests/integration/test_migrations.py grows from five
to seven in this commit.

`credits` gets **no** trigger and has no `updated_at` column: every write to
it is an insert, because a title's credit set is replaced rather than merged.
See db/models/people.py.

**Every CHECK below was read by eye against the model rather than trusted
from autogenerate.** Autogenerate emits a new table's constraints verbatim
from metadata, which is the easy direction; it is blind to a *changed*
condition and blind to a *missing* one, verified in M1 by loosening a bound
and getting an empty `pass` migration.
`test_every_check_constraint_in_the_models_exists_in_the_database` reads
`pg_constraint` and compares normalised bodies, and the five new constraints
join it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fd7c3a5b9e12"
down_revision: str | Sequence[str] | None = "fc6d2b81a794"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both are written by `INSERT ... ON CONFLICT DO UPDATE` out of a staging
# table, so `onupdate=` never fires for them. `credits` is deliberately
# absent: it has no `updated_at` column at all -- every write to it is an
# insert.
_TRIGGERED_TABLES = ("people", "collections")


def _adopt_collection_links_orphaned_by_an_earlier_downgrade() -> None:
    """NULL any `titles.collection_id` naming a collection that is gone.

    `downgrade()` drops `collections` but cannot touch the column, so a
    database that has been down and is coming back up can hold links to
    collections that no longer exist; adding the foreign key over them fails
    with a bare `ForeignKeyViolationError` naming one row. On a first upgrade
    this is a no-op: nothing has ever written the column.

    The rule applied is the column's own `ON DELETE` behaviour, which is
    `SET NULL` -- the `media_items.episode_id` branch of
    e5b8f2c40d17's version of this function. Silent, because it is safe: an
    unlinked title is worth keeping and nothing else on the row depends on
    the link. There is no `watch_states`-style refusal here because nothing
    this migration touches is user state.
    """
    op.execute("""
        UPDATE titles SET collection_id = NULL
        WHERE collection_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM collections c WHERE c.id = titles.collection_id)
    """)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "collections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("name <> ''", name="ck_collections_name_not_empty"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collections")),
    )
    op.create_index(
        "ix_collections_tmdb_id",
        "collections",
        ["tmdb_id"],
        unique=True,
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.create_table(
        "people",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sort_name", sa.Text(), nullable=False),
        sa.Column("known_for_department", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("name <> ''", name="ck_people_name_not_empty"),
        sa.CheckConstraint("sort_name <> ''", name="ck_people_sort_name_not_empty"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_people")),
    )
    op.create_index(
        "ix_people_tmdb_id",
        "people",
        ["tmdb_id"],
        unique=True,
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.create_table(
        "credits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("person_id", sa.UUID(), nullable=False),
        sa.Column("title_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("cast", "crew", name="creditkind", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column("tmdb_credit_id", sa.Text(), nullable=True),
        sa.Column("character", sa.Text(), nullable=True),
        sa.Column("job", sa.Text(), nullable=True),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("billing_order", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "billing_order IS NULL OR billing_order >= 0",
            name="ck_credits_billing_order_non_negative",
        ),
        sa.CheckConstraint(
            "tmdb_credit_id IS NULL OR tmdb_credit_id <> ''",
            name="ck_credits_tmdb_credit_id_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_credits_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_credits_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credits")),
    )
    op.create_index("ix_credits_title_id", "credits", ["title_id"], unique=False)
    op.create_index("ix_credits_person_id", "credits", ["person_id"], unique=False)
    op.create_index(
        "ix_credits_tmdb_credit_id",
        "credits",
        ["tmdb_credit_id"],
        unique=True,
        postgresql_where=sa.text("tmdb_credit_id IS NOT NULL"),
    )

    # The M1 column finally gets its target, plus the index the referential
    # check needs and PRD 02 had deferred to M9. See this module's docstring.
    _adopt_collection_links_orphaned_by_an_earlier_downgrade()
    op.create_index(
        "ix_titles_collection_id",
        "titles",
        ["collection_id"],
        unique=False,
        postgresql_where=sa.text("collection_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_titles_collection_id_collections"),
        "titles",
        "collections",
        ["collection_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Hand-written: autogenerate cannot see triggers. set_updated_at() itself
    # already exists -- the core schema created it -- and this migration must
    # not drop it on the way back down, since five other triggers reference it.
    for table_name in _TRIGGERED_TABLES:
        op.execute(f"""
            CREATE TRIGGER trg_{table_name}_set_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """)


def downgrade() -> None:
    """Downgrade schema."""
    for table_name in reversed(_TRIGGERED_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_set_updated_at ON {table_name}")

    op.drop_constraint(op.f("fk_titles_collection_id_collections"), "titles", type_="foreignkey")
    op.drop_index(
        "ix_titles_collection_id",
        table_name="titles",
        postgresql_where=sa.text("collection_id IS NOT NULL"),
    )
    op.drop_index(
        "ix_credits_tmdb_credit_id",
        table_name="credits",
        postgresql_where=sa.text("tmdb_credit_id IS NOT NULL"),
    )
    op.drop_index("ix_credits_person_id", table_name="credits")
    op.drop_index("ix_credits_title_id", table_name="credits")
    op.drop_table("credits")
    op.drop_index(
        "ix_people_tmdb_id",
        table_name="people",
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.drop_table("people")
    op.drop_index(
        "ix_collections_tmdb_id",
        table_name="collections",
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.drop_table("collections")
