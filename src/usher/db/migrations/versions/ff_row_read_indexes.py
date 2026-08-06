"""The read surface for M7's nine row providers.

Revision ID: ff
Revises: fe1d40c8b7a3
Create Date: 2026-08-04

Two indexes added, one dropped, and four deliberately not added. Every number
below was produced by `scripts/measure_rows.py --scale 1126674` against
`pgvector/pgvector:pg17` (PostgreSQL 17.10) at the one measured deployment's
proportions -- 126,857 titles, 992,240 episodes, 1,119,097 media items,
1,119,097 watch states, one series holding 20,000 episodes, 951,368 played
states, 3,442 in progress, 336,489 datable. None was read from a summary.

**Both columns of the table are filled, including the ones that flatter
nothing.** `f1a7d3c9e824` is the precedent: it records that the sweep index
takes its `UPDATE` from 173 ms to 102 ms *and*, in the same breath, that it
does not help the guard's `count(*)` at all (87 ms with, 86 ms without). A
docstring recording only the first number would have been true and would
have implied something false.

    statement                                    without        with
    ----------------------------------------  ----------  ----------
    _IN_PROGRESS  (Task 14)                    38.123 ms    0.029 ms
    _RECENT       (Task 14)                   811.145 ms  797.633 ms
    _REDISCOVERABLE (Task 16)                  14.614 ms   13.864 ms
    _RECENTLY_ADDED, with the window           47.639 ms   38.260 ms
    _RECENTLY_ADDED, no window               1167.831 ms 1181.308 ms
    _NEXT_UP      (Task 15, no index added)   141.348 ms  139.993 ms
    mark_unseen_unavailable's UPDATE            2.675 ms    2.560 ms
    list_needing_history                       32.025 ms   34.829 ms

**Read that table with its signs.** Only two rows move at all.

`_IN_PROGRESS` is the whole case for `ix_watch_states_user_recent`:
**38.123 ms to 0.029 ms**, a `Parallel Seq Scan` of 1,119,097 rows replaced
by `Index Scan using ix_watch_states_user_recent` under an `Incremental
Sort` whose `Presorted Key` is `last_played_at`, touching **24 buffers**.
Continue Watching is built on every home screen, at a ~60 s TTL, forever.

`_RECENTLY_ADDED` moves 47.639 to 38.260, which understates it, and the
understatement is worth a paragraph rather than a footnote. The unindexed
plan is a `Gather Merge` over **two extra worker processes**; the indexed
one is a `Bitmap Heap Scan` in a single backend at 15,196 buffers against
22,660. With `max_parallel_workers_per_gather = 0` -- a box already serving
concurrent home screens, which is exactly when this row is built -- the same
statement is **171.0 ms without the index and 16.5 ms with it**. Measured
across four windows, serial:

    window    without      with
    ------  ---------  --------
    1 day    161.5 ms    0.7 ms
    7 days   163.2 ms    4.3 ms
    30 days  171.0 ms   16.5 ms
    90 days  190.3 ms   92.4 ms

The index cannot supply the *order*, only bound the scan: `DISTINCT ON
(title_id)` forces its own `ORDER BY` to lead with `title_id`, which throws
away the `added_at` ordering the index has, so no `LIMIT` is ever pushed
down. That is why the win decays with window size rather than staying flat.

**Everything else in the table is a row where an index changed nothing, and
each is here on purpose:**

- `_RECENT` (811 -> 798 ms) is a `Sort` node by construction, for the same
  reason: its `DISTINCT ON` must order by the rolled-up title id first, so
  the recency order is re-applied above it whatever indexes exist. The index
  bounds the scan and nothing more, and the scan is bounded by the
  household's history rather than by the catalog.
- `_REDISCOVERABLE` (14.6 -> 13.9 ms) *borrows* `ix_watch_states_user_recent`
  -- equality on `user_id` and `played`, a range on `last_played_at`, and a
  btree is bidirectional so a `DESC NULLS LAST` index serves a `<` range.
  Its `ORDER BY play_count DESC` cannot be served by any index this
  milestone adds and remains a `Sort`. That sort is over titles the
  household watched more than two years ago, which is bounded by history and
  not by catalog, so it is a cost worth paying rather than one worth
  indexing around.
- `_NEXT_UP` (141.3 -> 140.0 ms) has **no index added for it at all** and
  both columns are therefore the same number. See below.
- The sweep's `UPDATE` (2.675 -> 2.560 ms) is the row this migration was
  most likely to get wrong, and it came out clean. `ix_media_items_recently_
  added` is partial on `available`, and `mark_unseen_unavailable`'s whole job
  is to set `available = false`, so every row it retracts leaves this index
  and the sweep pays maintenance for it. **Measured: no cost above noise.**
  Had it moved materially the non-partial `(added_at DESC NULLS LAST)` --
  larger, less selective for this read, untouched by the sweep -- was the
  alternative, and that trade would then have been a number rather than a
  preference. The absolute figure is not comparable with `f1a7d3c9e824`'s
  102 ms, whose fixture this script does not reproduce; the *delta* is the
  question and it is what was asked.
- `list_needing_history` (32.0 -> 34.8 ms) is the eighth row and it exists so
  the drop below is honest. It is a `Parallel Seq Scan on watch_states`
  **in both columns**; the 2.8 ms is run-to-run noise on a 32 ms statement,
  not a regression. Losing the narrow index costs it nothing because it never
  used it.

**ix_watch_states_user_played is dropped, not supplemented.** `(user_id,
played, last_played_at DESC NULLS LAST)` is a strict prefix superset, so
nothing the narrow index could serve is lost. All seven shipped
`watch_states` statements were `EXPLAIN`ed at the population above before
this migration was written, and **not one used it**:

    _NEEDING_HISTORY          Parallel Seq Scan on watch_states
                              Filter: (played AND (play_count = 0))
    _get, title branch        Index Scan using ix_watch_states_title_id
                              Index Cond: (title_id = ...)
                              Filter: (user_id = ...)
    _get, episode branch      Index Scan using ix_watch_states_episode_id
                              Index Cond: (episode_id = ...)
                              Filter: (user_id = ...)
    _update(title_id)         Nested Loop -> Index Scan using
                              ix_watch_states_title_id
    _insert(title_id)         Conflict Arbiter: uq_watch_states_user_title
    _update(episode_id)       Nested Loop -> Index Scan using
                              ix_watch_states_episode_id
    _insert(episode_id)       Conflict Arbiter: uq_watch_states_user_episode

**Two of those corrected the plan that specified this migration**, which
predicted the getters and the merge's `UPDATE`s would drive off
`uq_watch_states_user_title`/`uq_watch_states_user_episode`. They do not:
Postgres prefers the single-column `ix_watch_states_title_id` /
`ix_watch_states_episode_id` and applies `user_id` as a *filter*, because on
a household-sized user set the target id is nearly unique on its own. The
conclusion is unchanged -- and the narrow index is unused either way -- but
a drop justified by a grep is how a nightly job loses its plan, so the
correction is recorded rather than smoothed over.

Forced with `enable_seqscan = off`, `_NEEDING_HISTORY` *can* be made to read
the narrow index, as a `Bitmap Index Scan` on `played = true` alone over all
1,119,097 rows at cost 35,153 against the seq scan's 23,103. The planner
declines it, and the wide index can serve that same degenerate scan anyway.

Two indexes where one suffices is a write cost on every merge of every
nightly walk -- up to 1,126,789 states -- for no read. Sizes at this
population: the narrow index 7,792 kB, its replacement 21 MB,
`ix_media_items_recently_added` 24 MB.

**DESC NULLS LAST is spelled out and is not stylistic.** `last_played_at` is
nullable because a walk's listing cannot determine it (ADR-0014), Postgres
defaults a `DESC` sort to NULLS FIRST, and a DESC-NULLS-FIRST btree cannot
supply `ORDER BY last_played_at DESC NULLS LAST` as an ordered scan -- so
the index would serve the filter, the planner would fall back to a full
`Sort`, and Continue Watching would sort the whole per-user set on every
home screen while an index sat there looking like it was helping.

**Indexes this migration deliberately does not add:**

- `titles.genres` GIN. PRD 02's 78.7 ms at 300k rows is a catalog-wide facet
  count for M9's `/browse`. `GenreAffinityProvider`'s affinity half joins
  through `watch_states` and uses `genres` only as a *projection* -- it never
  appears in a predicate, so a GIN index on it cannot be read -- and its
  retrieval half is bounded to owned titles (single-digit thousands) before
  array containment is consulted. A GIN index on `titles` is paid on every
  write forever including the 1.27M-row bootstrap, and an index nothing reads
  is `ix_titles_popularity` again. **Task 28 owns the measurement**: if
  either of `GenreAffinityProvider`'s two statements shows a `Seq Scan on
  titles`, that is a finding against the provider's shape rather than an
  argument for this index.
- `titles.collection_id`. Group B's `fd7c3a5b9e12` owns it, in the same
  migration as the foreign key whose referential action needs it -- the
  precedent M4 set for `ix_media_items_episode_id` and
  `ix_watch_states_episode_id`. Named here so a reviewer who finds it absent
  from the *row-read* migration knows where it went rather than filing it as
  forgotten.
- `titles.release_date` / `episodes.air_date`. `SeasonalProvider`'s window is
  over **subject matter**, not release dates: a Halloween row is horror
  films, and a horror film released in March belongs in it while a romantic
  comedy released in October does not. So the selective predicate is
  `genres`/`keywords`, the candidate set is already bounded to owned titles,
  and the date is a filter on a small set or a display field. The condition
  that reverses this is a *"released in the last N weeks"* provider -- which
  is a different provider from Seasonal, is not among PRD 06's ten, and does
  not exist. This paragraph is the record of why the index does not.
- Anything for `EpisodeRepository.next_up`, and it is the statement that
  looks most like it needs one. The row comparison `(e.season_number,
  e.episode_number) > (m.season_number, m.episode_number)` is pushed down as
  an `Index Cond` against the existing `uq_episodes_title_season_episode`,
  and the watch-state probe binds `ix_watch_states_episode_id`. Measured at
  32,409 series / 999,827 episodes / 200 probed: **15.7 ms**, two bitmap
  index scans and a nested loop, no `Seq Scan on episodes`. The correctly
  hand-expanded `OR` form of that same comparison returns the identical 200
  rows in **134.1 ms** with a `Seq Scan` over every episode in the library,
  because an `OR` is not indexable as a range. Both spellings are correct;
  only the plan can tell them apart, which is why
  `test_next_up_reads_the_episode_key_index_and_does_not_scan_episodes`
  asserts the comparison appears as an `Index Cond` and not merely that the
  index is named somewhere.

**`compare_metadata` is blind to the partial predicate and NOT to the null
ordering, which is half of what the plan for this migration assumed.**
Measured by mutation, both directions:

- Dropping `NULLS LAST` from the *migration* (so the database has plain
  `DESC` and the model has the full clause), and dropping it from the *model*
  (the mirror), each make `test_migration_matches_the_orm_metadata` **fail**.
  Alembic does diff an expression index's rendered ordering.
- Dropping `postgresql_where` from this migration, leaving it on the model,
  makes that same test **pass**. The partial predicate really is invisible to
  it.

So the ORM round-trip covers one of these two clauses and not the other, and
`test_the_row_read_indexes_carry_the_clauses_that_make_them_work` -- which
reads `pg_indexes.indexdef`, i.e. what Postgres will actually do -- is what
covers both. It is the technique M6 built for `_SUSPENDABLE_INDEXES`, and
the reason to keep it for the null ordering too is that its guarantee does
not depend on which clauses a future Alembic happens to render.

**Neither new index joins `_SUSPENDABLE_INDEXES`.** That dict is scoped to
`titles` for the IMDb bootstrap's bulk-load window, and neither of these is
on `titles`, so `test_every_suspendable_index_rebuilds_to_what_the_migration_
built` is unchanged -- asserted rather than assumed, because the whole reason
that case exists is that a new index has to join the dict in the same commit
when it belongs there.

**No new tables, so the trigger set is unchanged** and
`test_migration_creates_the_updated_at_triggers`'s exact-set assertion stays
green. Stated because that case is written as a literal set precisely so a
migration that should have grown it fails loudly.

**These indexes cannot be built CONCURRENTLY**, because `env.py` wraps both
migration modes in `context.begin_transaction()`. That resolved trivially for
`fb4e0a7d2c15`, whose index was built over a table the same migration had
just created; **it does not resolve trivially here.** Both target tables hold
real rows on a real deployment, so `CREATE INDEX` takes a SHARE lock that
blocks writes -- i.e. blocks a nightly walk -- for the duration. Measured at
this population: **552 ms** for `ix_watch_states_user_recent` and **291 ms**
for `ix_media_items_recently_added`. An operator who cannot take even that
window builds both by hand with `CREATE INDEX CONCURRENTLY` and then
`alembic stamp`s this revision; that is an operator escape, not the default,
and it is recorded here rather than discovered at 3am.

**The revision id is two characters and it is the last one.**
`fa2b6c1e9d30`'s convention fixes one hex character per migration and M6's
cycle ended at `fc`, leaving `fd`/`fe`/`ff` for M7's five. `fd7c3a5b9e12` and
`fe1d40c8b7a3` are spent; this is `ff`. The next migration extends by a
character -- `ffa`, then `ffb` -- rather than starting a third cycle with a
digit, which would sort *before* `fa` and lose the only thing the convention
ever bought (`ls` order matching chain order).

Reversible in both directions, and the downgrade restores the dropped index
rather than leaving the schema one index short of where it started.
"""

import sqlalchemy as sa
from alembic import op

revision = "ff"
down_revision = "fe1d40c8b7a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_watch_states_user_played", table_name="watch_states")
    op.create_index(
        "ix_watch_states_user_recent",
        "watch_states",
        ["user_id", "played", sa.text("last_played_at DESC NULLS LAST")],
        unique=False,
    )
    op.create_index(
        "ix_media_items_recently_added",
        "media_items",
        [sa.text("added_at DESC NULLS LAST")],
        unique=False,
        postgresql_where=sa.text("available AND title_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_media_items_recently_added", table_name="media_items")
    op.drop_index("ix_watch_states_user_recent", table_name="watch_states")
    op.create_index("ix_watch_states_user_played", "watch_states", ["user_id", "played"])
