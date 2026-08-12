"""`credits.source`, `people.imdb_id`, and the dedup key an IMDb load needs.

Revision ID: m09d
Revises: m09c
Create Date: 2026-08-12

The schema half of
[ADR-0036](../../../../docs/prd/decisions/0036-the-imdb-tmdb-provenance-rule.md).

## Why `m09d` and not `m09b`

`m09b` was granted to T4 in the M9 plan and **withdrawn** with it when T3's
size bar failed, so the id was never minted. Its *position* was taken anyway:
`m09c` was minted for the `images` natural key and descends directly from
`m09a`. Reviving `m09b` now would either fork the chain at `m09a` -- two heads,
and `tests/integration/test_migrations.py` runs `alembic upgrade head` for
every one of its cases -- or produce an id that sorts before the revision it
descends from. So this chains off `m09c` with a fresh id, and `alembic heads`
prints exactly one.

## What it does

1. `people.imdb_id` -- `text`, nullable, partial-unique
   `WHERE imdb_id IS NOT NULL`, mirroring `ix_titles_imdb_id`, plus the
   not-empty CHECK every other nullable text identifier in this schema
   carries.
2. `credits.source` -- NOT NULL, backfilled to `'tmdb'`.
3. `ix_credits_source_natural_key` -- the dedup key for every source that is
   **not** TMDb.

## The backfill is a claim about existing rows, and it is checkable

Every row `credits` holds at this revision was written by
`DeriveService.replace_for_titles` out of `raw_payloads`, whose `provider` is
`'tmdb'` on every row (ADR-0016: the cache holds provider responses, and TMDb
is the only metadata provider that exists). No bulk source has ever written to
this table -- M9 Track 2 shipped the names-only design specifically because
`people`/`credits` were not bulk-loaded. So `'tmdb'` is the true value rather
than a convenient one.

**Spelled as three statements rather than as `server_default='tmdb'`.** A
server default would make the column NOT NULL *and* silently supply a value to
any future writer that forgets it -- which is the "unknown provenance" state
this column exists to abolish, wearing a valid value instead of a NULL. The
column is added nullable, backfilled, then set NOT NULL, and no default is
left behind. `domain/people.py::Credit.source` is required for the same reason
on the other side of the boundary.

**Land the column before the volume.** `ALTER TABLE ... SET NOT NULL` scans
the table; `ADD COLUMN` with no default does not rewrite it (PostgreSQL 11+).
At the 2,877,520 rows a fully-enriched deployment holds today that scan is
seconds. After an IMDb load it is 10^7 rows, which is the reason this revision
is not deferred until there is something to dedupe.

## The natural key, and the three spellings that were measured and rejected

`ix_credits_tmdb_credit_id` is partial over `tmdb_credit_id IS NOT NULL`, i.e.
over **none** of an IMDb load, so before this index `credits` could not dedupe
a bulk IMDb import at all. Measured over the 12,638,471 principals rows this
catalog retains from the pinned `title.principals.tsv.gz`
(`"f4422fc329ee8db79fb20dc7e3b64775-93"`, 2026-08-12):

| candidate | distinct | verdict |
|---|---|---|
| `(title_id, ordering)` | 12,638,471 | **UNIQUE** |
| `(title_id, nconst, category, ordering)` | 12,638,471 | UNIQUE, two columns wider |
| `(title_id, nconst, category)` | 12,276,307 | 362,164 collide |
| `(title_id, nconst, kind)` | 11,294,913 | 1,343,558 collide |

The M9 plan proposed `(title_id, person_id, category, ordering)`. It is
correct and redundant: `category` is not a column here at all -- IMDb's 13
categories fold into `CreditKind`'s two -- and `person_id` adds nothing once
`ordering` is in the key. The 1,343,558-row collision on `(title_id,
person_id, kind)` is what rules out any person-based key: a director who also
wrote a film is two crew credits on one title, and 9.3% of the whole file
repeats a person already credited on the same title.

**`NULLS NOT DISTINCT`, which is `m09c`'s precedent one table over.** Every
IMDb row carries an `ordering` -- 0 of 101,170,912 rows in the pinned file
lack one -- so on today's data the clause never fires. It fires for a future
source with no per-title ordering, which a plain UNIQUE would let write
unlimited `(title_id, source, NULL)` rows. The careless spelling is inert
exactly where nobody is looking, which is `m09c`'s finding restated.

**Partial on `source <> 'tmdb'` rather than `source = 'imdb'`**, so the two
unique indexes on this table partition it rather than overlap: this one covers
every source that does not carry its own credit id. TMDb has to be excluded
rather than merely uncovered -- its crew rows legitimately share a NULL
`billing_order` by the dozen, and `NULLS NOT DISTINCT` would read that as a
collision and refuse a derivation that works today.

## What this revision deliberately does not carry

**No `people.source`.** The pair `(tmdb_id, imdb_id)`, both nullable and each
partially unique, already says which source a person came from -- and unlike
an enum it can represent *both*, which is the state ADR-0036's branch (a)
needs. A `source` column here would have to be dropped to merge a person; two
nullable ids do not.

**No `episode_id`.** Unchanged from `fd7c3a5b9e12`: `season.json`'s
`episodes[].crew` and `episodes[].guest_stars` are still both `[]`.

**No CHECK that a non-TMDb row has a `billing_order`.** That is a claim about
sources that do not exist yet, and `NULLS NOT DISTINCT` already turns the
violation into a loud refusal at the second row rather than a silent doubling
at every one.

## Measured cost

Added relation size over `people` + `credits` for a whole-catalog IMDb load,
after `VACUUM (FULL, ANALYZE)`: **3,374,514,176 B (3.375 GB / 3.143 GiB)** for
12,637,249 credits over 3,215,476 people, of which this index is
**628,826,112 B (599.7 MiB)**.
"""

from alembic import op

revision = "m09d"
down_revision = "m09c"
branch_labels = None
depends_on = None

#: Named once, because `upgrade()` creates it and `downgrade()` must not drop
#: a differently-spelled one.
_NATURAL_KEY = "ix_credits_source_natural_key"


def upgrade() -> None:
    op.execute("ALTER TABLE people ADD COLUMN imdb_id text")
    op.execute(
        "ALTER TABLE people ADD CONSTRAINT ck_people_imdb_id_not_empty "
        "CHECK (imdb_id IS NULL OR imdb_id <> '')"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_people_imdb_id ON people (imdb_id) WHERE imdb_id IS NOT NULL"
    )

    # Three statements, and the order is the point: nullable, backfilled, then
    # NOT NULL. Never `server_default`, which would outlive this migration and
    # supply a plausible wrong value to a writer that forgot.
    op.execute("ALTER TABLE credits ADD COLUMN source varchar(8)")
    op.execute("UPDATE credits SET source = 'tmdb'")
    op.execute("ALTER TABLE credits ALTER COLUMN source SET NOT NULL")

    # `op.execute` rather than `op.create_index`: alembic's operation has no
    # parameter for `NULLS NOT DISTINCT`, and a migration that quietly emitted
    # the default would ship the inert spelling this revision exists to avoid.
    op.execute(
        f"CREATE UNIQUE INDEX {_NATURAL_KEY} ON credits (title_id, source, billing_order) "
        "NULLS NOT DISTINCT WHERE source <> 'tmdb'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {_NATURAL_KEY}")
    op.execute("ALTER TABLE credits DROP COLUMN source")
    op.execute("DROP INDEX ix_people_imdb_id")
    op.execute("ALTER TABLE people DROP CONSTRAINT ck_people_imdb_id_not_empty")
    op.execute("ALTER TABLE people DROP COLUMN imdb_id")
