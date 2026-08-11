"""`images` gets the natural key `m09a` was asked for and shipped without.

Revision ID: m09c
Revises: m09a
Create Date: 2026-08-11

## Why this is a second revision and not an edit

`m09a` has merged. Every integration suite builds its schema from it and
`tests/integration/test_migrations.py` pins it, so the three DDL facts group C
asked of it are a **requested** revision rather than a rewrite -- which is the
rule that document states for exactly this case. This is that request, granted.
`m09b` is not skipped by accident: revision ids are allocated per *merge*, and
this one is C2's.

## The one requested fact that could not simply be transcribed

The request was *"a unique key over `(title_id, provider, provider_path)`"*.
`images` has **three** nullable owner columns under
`ck_images_exactly_one_owner`, so that spelling covers title-owned rows and
silently covers nothing else: `UNIQUE (title_id, provider, provider_path)` on
an episode-owned row indexes `(NULL, 'tmdb', '/x.jpg')`, and in SQL a NULL never
collides with a NULL, so every episode still and every person headshot would be
freely duplicable under a constraint whose name says otherwise. **A unique index
that silently exempts two thirds of a table is worse than none**, because the
guarantee `Cache-Control: immutable` rests on would read as present.

So the real key is `(the one owner, provider, provider_path)`, and *the one
owner* is a three-way disjunction rather than a column. Two spellings say that
faithfully and **this migration takes the first**:

1. **Three partial unique indexes, one per owner column**, each predicated on
   its own owner being non-null. Inside each index all three columns are
   non-null -- the owner by the index's own `WHERE`, `provider` and
   `provider_path` by their `NOT NULL` -- so there is no NULL hole to reason
   about, and `ck_images_exactly_one_owner` puts every row in exactly one of
   the three.
2. `UNIQUE (coalesce(title_id, episode_id, person_id), provider,
   provider_path)`. **Rejected**, and not on taste: `coalesce(...)` is non-null
   only *because* `ck_images_exactly_one_owner` says so, so the index would
   borrow its honesty from a constraint on a different column set -- loosen
   that CHECK to `>= 0` some day and this index goes quietly leaky instead of
   loudly wrong. It also gives `ON CONFLICT` one inference target for three
   write models, where a title-scoped upsert could name an expression that
   spans owners it has no business touching.

Spelling 1 costs nothing spelling 2 saves: every row is in exactly one of the
three indexes, so the three together hold one entry per row, the same as one
index would.

**All three arms ship, though M9 writes only the title one.** An index enforcing
a *constraint* is not the `ix_titles_popularity` shape that boundary call
refuses -- its reader is Postgres, on every insert, from the first row. Two of
the three are for a writer this milestone cannot name, which is exactly what
`ck_images_width_positive` already is.

## `remote_url` -> `provider_path`, renamed rather than added beside

PRD 02's sketch said `remote_url` and `m09a` transcribed it.
[ADR-0032](../../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)
then settled the proxy on `{base}{rung}{path}`, and a stored full URL is not a
cosmetic mismatch with that: it bakes a **rung** into the natural key, so the
key that has to survive a re-derivation stops surviving a change to a
deployment constant, and rung selection becomes string surgery on somebody
else's URL. A rename rather than a second column, because two spellings of one
fact is the divergence `titles.credit_names` exists to argue about.

The table is empty on every deployment -- `m09a` shipped no writer at all and
C3 is the first -- so this is a rename of a column and of its CHECK, with no
data migration and nothing to backfill.

## `sort_order`, `NOT NULL`, no default

The read order is `(is_primary DESC, sort_order, id)` and the middle key is the
only one a *second* derivation can move: `id` is first-sighting order, and
`is_primary` is one bit. Without it, `ORDER BY id` and `ORDER BY <the real key>`
agree by accident under UUIDv7 -- the trap that cost M7 five untested orderings
-- and a provider that re-ranks a title's posters could never be reflected.

The column is added with `server_default = '0'` and the default is then
**dropped in the same migration**. The default exists only so `NOT NULL` is
addable at all; leaving it would mean an insert that forgets `sort_order`
silently sorts every image equal, which is the same "keeps the first ordering
forever" defect one layer down.

## What this revision does not do

It does not touch `ck_images_exactly_one_owner`, and would be the wrong place
to. If a key over these columns had turned out unspellable, the correct outcome
was to drop `Cache-Control: immutable` from the proxy, never to weaken the
CHECK so a simpler index would fit.
"""

import sqlalchemy as sa
from alembic import op

revision = "m09c"
down_revision = "m09a"
branch_labels = None
depends_on = None

#: `(index name, owner column)`. Written once and iterated in both directions,
#: so an `upgrade()` that creates three and a `downgrade()` that drops two
#: cannot drift apart -- which is the failure
#: `test_a_full_down_and_up_cycle_restores_every_index` exists to catch and the
#: one a hand-written triple invites.
_OWNER_KEYS = (
    ("uq_images_title_provider_path", "title_id"),
    ("uq_images_episode_provider_path", "episode_id"),
    ("uq_images_person_provider_path", "person_id"),
)


def upgrade() -> None:
    # The CHECK body follows the column automatically -- Postgres stores a
    # parse tree, not the text -- so only the constraint's *name* has to be
    # renamed, and it is renamed rather than left because a constraint called
    # `ck_images_remote_url_not_empty` on a column called `provider_path` is
    # the stale "verified" fact that is worse than none.
    op.alter_column("images", "remote_url", new_column_name="provider_path")
    op.execute(
        "ALTER TABLE images RENAME CONSTRAINT "
        "ck_images_remote_url_not_empty TO ck_images_provider_path_not_empty"
    )

    op.add_column(
        "images", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
    )
    # Dropped immediately: the default is what makes `NOT NULL` addable, and
    # keeping it would let a writer that forgets the column sort every image
    # equal without saying so.
    op.alter_column("images", "sort_order", server_default=None)
    op.create_check_constraint("ck_images_sort_order_non_negative", "images", "sort_order >= 0")

    for index_name, owner in _OWNER_KEYS:
        # Partial on its own owner, so all three indexed columns are non-null
        # *within the index* and there is no NULL hole. `ON CONFLICT` must
        # repeat this predicate verbatim or Postgres cannot infer the index --
        # `db/staging.py`'s first trap, and `repositories/image.py` does.
        op.create_index(
            index_name,
            "images",
            [owner, "provider", "provider_path"],
            unique=True,
            postgresql_where=sa.text(f"{owner} IS NOT NULL"),
        )


def downgrade() -> None:
    for index_name, _ in _OWNER_KEYS:
        op.drop_index(index_name, table_name="images")
    op.drop_constraint("ck_images_sort_order_non_negative", "images", type_="check")
    op.drop_column("images", "sort_order")
    op.execute(
        "ALTER TABLE images RENAME CONSTRAINT "
        "ck_images_provider_path_not_empty TO ck_images_remote_url_not_empty"
    )
    op.alter_column("images", "provider_path", new_column_name="remote_url")
