"""`images` gets the natural key `m09a` was asked for and shipped without.

Revision ID: m09c
Revises: m09a
Create Date: 2026-08-11

## Why this is a second revision and not an edit

`m09a` has merged. Every integration suite builds its schema from it and
`tests/integration/test_migrations.py` pins it, so the DDL group C asked of it
is a **requested** revision rather than a rewrite — which is the rule that
document states for exactly this case, and which `m09a`'s own docstring
restates as *"`m09c` is spare and must be requested, never minted"*. The
request is written out in
[ADR-0032](../../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)'s
Decision, and this is it, granted.

## The spelling is not the obvious one, and the obvious one fails in silence

The plain reading of *"a unique key over `(title_id, provider,
provider_path)`"* is:

```sql
ALTER TABLE images ADD CONSTRAINT ... UNIQUE (title_id, provider, provider_path);
```

`images` has **three** nullable owner columns under
`ck_images_exactly_one_owner`, and Postgres defaults a unique constraint to
`NULLS DISTINCT`. So that constraint covers title-owned rows and **nothing
else**: an episode- or person-owned duplicate indexes `(NULL, 'tmdb',
'/x.jpg')`, and a NULL never collides with a NULL.

Measured on a throwaway `pgvector/pgvector:pg17` (PostgreSQL **17.10**, the
version this project deploys) with `images`' owner columns and its CHECK
reproduced and its foreign keys omitted — C1's measurement in ADR-0032,
re-run independently here before this migration was written:

| spelling | title-owned duplicate | person-owned duplicate |
|---|---|---|
| `UNIQUE (title_id, provider, provider_path)` | rejected ✅ | **admitted** 🔴 |
| the same, `NULLS NOT DISTINCT`, over all three owners | rejected ✅ | rejected ✅ |

"Person-owned duplicate" means two rows sharing one `person_id`, one
`provider` and one `provider_path`, so `title_id IS NULL` on both; the obvious
spelling **admitted 2 rows where 1 is correct**.

This is the careless-versus-careful pattern in DDL. The wrong version passes
review, passes any test that only inserts title-owned rows — which is every
test M9 writes, because M9 writes no episode still and no person headshot — and
is inert for precisely the two owner kinds nobody is looking at.

**`NULLS NOT DISTINCT` needs no help from `ck_images_exactly_one_owner`**, and
that is why it is preferred over the other faithful spelling,
`UNIQUE (coalesce(title_id, episode_id, person_id), provider, provider_path)`:
the `coalesce` is non-null only *because* that CHECK says so, so the index
would borrow its honesty from a constraint on a different column set, and
loosening the CHECK some day would make it quietly leaky rather than loudly
wrong. It also needs no `WHERE` repeated in every writer's `ON CONFLICT`, which
three partial unique indexes would (that is the PostgreSQL < 15 fallback, and
this project is on 17).

**It is not merely stricter.** Verified in the same run: two *different* titles
referencing one path are still two rows, which is right — the same artwork can
legitimately be referenced twice.

**Declared as a constraint rather than as `Index(..., unique=True)`**, so
`pg_get_constraintdef` reports it back as
`UNIQUE NULLS NOT DISTINCT (title_id, episode_id, person_id, provider,
provider_path)` and it survives a schema dump.

## What the key buys, which is a property of the *write*

`ON CONFLICT (title_id, episode_id, person_id, provider, provider_path) DO
UPDATE` infers this constraint and **returns the id the row was first inserted
with** — measured in the same run, the same UUID before and after a re-derive
that changed `kind`, `width`, `height`, `language` and `is_primary`. That is
the entire property `Cache-Control: immutable` rests on, and until this landed
ADR-0032's header had nothing underneath it.

## `remote_url` → `provider_path`, renamed rather than added beside

ADR-0032 explicitly leaves this to C2: *"either is implementable; only the
first is cheap, and this ADR does not decide it for C2."* Decided here, on that
argument. The ladder is `{base}{rung}{path}`, so with a full URL stored,
choosing a rung means finding and replacing the `/t/p/{size}` segment of a URL
this project did not mint — on every request — and a CDN-base change becomes a
data migration across 1.27M titles.

A rename rather than a second column, because two spellings of one fact is the
divergence `titles.credit_names` exists to argue about. The table is empty on
every deployment — `m09a` shipped no writer at all and C3 is the first — so
there is nothing to backfill and no `USING` clause to get wrong.

## What this revision deliberately does not carry

**`sort_order`.** Group C's preamble asked for it, and ADR-0032's request drops
it on purpose: *"it belongs to whoever reads images rather than to the proxy,
and bundling it into this request would hide it."* This revision is authorised
for the key, so the read order is `(is_primary DESC, id)` and
`ImageRepository`'s docstring states what that costs rather than leaving it to
be discovered.

**Any change to `ck_images_exactly_one_owner`.** If a key over these columns
had turned out unspellable, the correct outcome was to drop `Cache-Control:
immutable` from the proxy — never to weaken the CHECK so a simpler index would
fit.

**Any change to the three owner indexes**, and this was checked rather than
assumed -- the first check was wrong and the test caught it.
`uq_images_owner_provider_path` leads on `title_id`, so it can serve the
CASCADE lookup `ix_images_title_id` exists for, which raises the fair question
of whether the narrow index still earns its keep. A probe on a throwaway table
said the narrow one was still chosen; that did not transfer, and
`test_every_cascade_in_this_migration_has_an_index_the_lookup_can_use` failed
on the real schema naming `uq_images_owner_provider_path` instead. **On an
empty table the two cost identically** (`4.16..9.52` each) and the planner's
tie-break is arbitrary, which is all that failure was.

Re-measured on the state a deployment is actually in -- 200,000 images over
40,000 titles, `ANALYZE` run:

| index | size | chosen for `WHERE title_id = ?` |
|---|---|---|
| `ix_images_title_id` | 2,680 kB | **yes**, `Index Scan`, 4 buffers |
| `uq_images_owner_provider_path` | 13 MB | no |

The same narrow index is chosen for the real parent `DELETE` and for
`list_for_title`. So nothing is dropped: `ix_images_title_id` is five times
smaller and is what the planner reaches for once there are statistics. The
probe now accepts either name for that one column, because at zero rows it is
asserting a tie-break rather than the existence of a usable index.
"""

from alembic import op

revision = "m09c"
down_revision = "m09a"
branch_labels = None
depends_on = None

#: The owner triple plus the two provider columns, in one place, because
#: `upgrade()` names it and `downgrade()` must not name a different one.
_KEY_COLUMNS = ("title_id", "episode_id", "person_id", "provider", "provider_path")


def upgrade() -> None:
    # The CHECK body follows the column automatically -- Postgres stores a
    # parse tree, not the text -- so only the constraint's *name* has to move,
    # and it is moved rather than left because a constraint called
    # `ck_images_remote_url_not_empty` on a column called `provider_path` is
    # the stale "verified" fact `prd-maintenance.md` calls worse than none.
    op.alter_column("images", "remote_url", new_column_name="provider_path")
    op.execute(
        "ALTER TABLE images RENAME CONSTRAINT "
        "ck_images_remote_url_not_empty TO ck_images_provider_path_not_empty"
    )

    # `op.execute`, not `op.create_unique_constraint`: alembic's operation has
    # no parameter for `NULLS NOT DISTINCT`, and the whole finding above is
    # that the spelling without it is inert for two owner kinds in three. A
    # migration that quietly emitted the default would be the exact defect this
    # revision exists to fix, arriving through the tooling.
    op.execute(
        "ALTER TABLE images ADD CONSTRAINT uq_images_owner_provider_path "
        f"UNIQUE NULLS NOT DISTINCT ({', '.join(_KEY_COLUMNS)})"
    )


def downgrade() -> None:
    op.drop_constraint("uq_images_owner_provider_path", "images", type_="unique")
    op.execute(
        "ALTER TABLE images RENAME CONSTRAINT "
        "ck_images_provider_path_not_empty TO ck_images_remote_url_not_empty"
    )
    op.alter_column("images", "provider_path", new_column_name="remote_url")
