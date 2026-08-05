"""`ix_titles_popularity` goes, because no statement can use it as declared.

Revision ID: ffc
Revises: ffb
Create Date: 2026-08-05

**The milestone's sixth migration against a plan that budgets four.** Task 35's
`ffb` was the planned fifth; this one is conditional in the plan — Task 36 Step
6 says the index is dropped *only if* the measurement says so, and it does.
Task 38 records both.

**M6 recorded "`ix_titles_popularity` is read by nothing in `src/`" and that is
now half wrong, which is why this was measured rather than inherited.** M7's
own Group H added `TitleRepository.list_owned_by_tag`, whose ordering is
`popularity DESC NULLS LAST, vote_count DESC NULLS LAST, id` — so a statement
in `src/` *does* order by `titles.popularity`. The grep claim is refuted. The
index goes anyway, and the reason is sharper than "unread".

**Measured 2026-08-05 against a real `--phase all` catalog: 1,271,570 titles,
291,584 carrying a popularity, 5,200 owned media items.**

The index is declared

    CREATE INDEX ix_titles_popularity ON titles USING btree (popularity DESC)
        WHERE popularity IS NOT NULL

A `DESC` btree orders **NULLS FIRST**. Every consumer in this repository asks
for `DESC NULLS LAST` — and that spelling is load-bearing correctness, not
taste: without it the entire unknown population sorts above every known one,
which is the argument `_SUGGEST` and `list_owned_by_tag` both write out at
length. Those are **different pathkeys**, so the index cannot serve the
ordering any shipped statement asks for. Three plans, same catalog, same
session:

| query | plan |
|---|---|
| `ORDER BY popularity DESC` (the index's own declaration) | `Index Scan using ix_titles_popularity`, cost 0.42..20.97 |
| `ORDER BY popularity DESC NULLS LAST` (what `src/` asks) | `Parallel Seq Scan` + `Sort`, cost 86,142 |
| the same, `enable_seqscan = off` | `Bitmap Index Scan` + **`Sort`** — the bitmap discards ordering, so the sort remains |

So it is not merely unread; **it is unusable as declared**, and has been since
`a8a0e10ff464` created it.

**The obvious repair was measured too, and it does not rescue the index.**
Building `(popularity DESC NULLS LAST) WHERE popularity IS NOT NULL` makes the
favourable query an `Index Scan` again — and leaves `list_owned_by_tag`'s plan
**byte-identical**: `Merge Semi Join` over `pk_titles` and
`ix_media_items_title_id`, then a 2,569-row `top-N heapsort`, 146 ms. That
statement filters by ownership and genre *first* and sorts a couple of thousand
survivors, so a whole-catalog popularity index has nothing to offer it at any
declaration. Fixing the column order would have produced a correct index that
is still never chosen.

**And the justification in `ports/repository.py` names a consumer that has
never existed.** It reads: *"which is what makes `ix_titles_popularity` usable
and gives M4's enrichment queue a real ordering."* There is no such ordering.
The enrichment queue is `jobs`, claimed through `ix_jobs_claim`
(`priority DESC, created_at`); no statement anywhere orders it by
`titles.popularity`. That sentence is corrected in the same commit.

**What it cost, since the plan asked for the number and carried a wrong one.**
The plan's note says ~340 MB. Measured: **9,536 kB** on the `--phase all`
catalog and **8,192 bytes** on a `--phase imdb` one — it is a *partial* index,
so on a bootstrap-only catalog (popularity NULL on every row) it is empty. So
the storage argument for dropping it is weak and is not the reason. The reason
is that it cannot be used. Two smaller costs are real and are the ones worth
recording: it is **not** in `bulk.py`'s `_SUSPENDABLE_INDEXES`, so it is
maintained through the entire 1,271,570-row bootstrap and through
`link_crosswalk`'s whole-catalog `UPDATE` of 291,956 rows, for a reader that
does not exist.

**What would bring it back**, stated so this is a reversible decision rather
than a deletion: a statement whose plan genuinely takes a popularity ordering —
most plausibly an enrichment queue ordered "most popular unenriched first",
which is what the justification above *claimed* existed and which would be a
legitimate M9 change. That index would be `(popularity DESC NULLS LAST)`, not
this one, and it is one `CREATE INDEX` away. Adding it on the strength of the
sentence rather than of a plan is what produced this.

**Downgrade recreates it exactly as it was**, wrong declaration and all: a
downgrade must restore the schema it reversed, not a better one.
"""

from alembic import op

revision = "ffc"
down_revision = "ffb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_titles_popularity", table_name="titles")


def downgrade() -> None:
    op.create_index(
        "ix_titles_popularity",
        "titles",
        ["popularity"],
        unique=False,
        postgresql_where="popularity IS NOT NULL",
        postgresql_using="btree",
        postgresql_ops={"popularity": "DESC"},
    )
