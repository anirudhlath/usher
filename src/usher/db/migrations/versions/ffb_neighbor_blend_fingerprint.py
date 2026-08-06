"""`title_neighbors` carries the fingerprint of the blend that computed it.

Revision ID: ffb
Revises: ffa
Create Date: 2026-08-04

**This is the milestone's fifth migration against a plan that budgets four, and
it is a planned fifth rather than a surprise.** The front matter's rule is that
a fifth is a *finding*, not that it is forbidden; Task 35 names it in advance
with its reason, and Task 38 records it as such. It could not be folded into
`ffa`: that revision belongs to group F, which runs earlier in the serial
order, and by the time this column was needed `ffa` had already been amended
once by group G and applied.

**Why a column at all.** M6 shipped `title_neighbors` with a whole-artefact
`computed_at()` and no fingerprint, and wrote the gap down honestly rather than
dressing it up -- `services/similar.py` called it "the milestone's one
acknowledged gap". The stated reason is correct and does not go away: a
neighbour row goes stale when *some other* title gets an embedding, and no
per-row predicate can decide that.

**But that argument covers one of two staleness causes and is silent about the
other, and M7 made the other one urgent by doing it.** Task 35 re-weighted the
blend and added a fourth signal, so:

- every row written **before** M7 is a three-signal blend at
  `{cosine 0.60, keywords 0.25, genres 0.15}`;
- every row written **after** is a four-signal blend at
  `{cosine 0.45, tags 0.25, keywords 0.20, genres 0.10}`;
- both are in `[0, 1]`, both carry a plausible `rank`, both sit in the same
  table, and **nothing distinguished them.**

That is precisely the state
[ADR-0020](../../../../../docs/prd/decisions/0020-derived-state-carries-its-fingerprint.md)
exists to eliminate: *"Every derived artefact is either fresh by construction,
or carries the fingerprint of the input it was derived from so that staleness
is a query."* `title_neighbors` was the one artefact M6 exempted. This closes
the half that is decidable.

**What it does NOT close, said here rather than left to be inferred.** The
fingerprint answers *"was this row computed under the current meaning?"* It
does **not** answer *"has some other title been embedded since?"* M6's
reasoning for the age-not-fingerprint call is untouched and must not be read as
reversed: that half is genuinely undecidable per row, and it stays exactly
where M6 left it -- with `computed_at()` and an operator. Two causes, one
closed.

**The backfill is a real fingerprint, not a sentinel, and that is the argued
part.** `NOT NULL` needs a value for the rows already there. The tempting
spellings are `''` or `'unknown'`, both of which are merely *different* from
the running fingerprint and would read as "we have no idea what computed this".
We do know: nothing but `SimilarityService.rebuild` has ever written this
table, so every existing row is M6's three-signal blend at
`_NEIGHBORS_PER_TITLE = 25` and `_CANDIDATE_POOL = 100`. That is

    md5('{"candidate_pool":100,"neighbors_per_title":25,'
        '"weights":{"cosine":0.6,"genres":0.15,"keywords":0.25}}')
    = 6697a3e1eaca411cbae890e54a4c665a

which is reproducible by anyone from `blend_fingerprint()`'s own serialisation
rule, and which therefore names the blend rather than merely failing to match
the current one. The `server_default` is dropped immediately afterwards, so
nothing written from here on can acquire a fingerprint it did not earn.

**A REBUILD IS REQUIRED AND NOTHING SCHEDULES IT.** Every row this migration
stamps is now correctly labelled and still computed under the old meaning. Run

    usher similar --rebuild

after upgrading. `usher similar <title id>` says so per title, and
`usher.similarity.neighbors.stale` counts the rows still owed. `CLAUDE.md` has
recorded since M6 that *"nothing runs `usher similar --rebuild` for you"*, and
that is unchanged -- what changes is that an operator can now *see* the debt
instead of having to know about it.

**Rejected, each with its reason, because each is the tempting answer:**

- *Truncate `title_neighbors` here.* An empty table and a stale table are both
  wrong and the empty one is worse: every `SimilarityRow` renders nothing,
  `computed_at()` returns `None`, and an operator who did not read the release
  note sees a feature that regressed rather than one that is out of date.
- *A `similar` job kind that self-schedules on embedding change.* It needs the
  trigger M6 proved undecidable. A job kind whose enqueue predicate cannot be
  written is a queue that either never fires or fires once per embedded title,
  and the second is a full rebuild per enriched title.
- *Make `_WEIGHTS` a `Settings` field so a deployment can keep the old blend.*
  Refused by `similar.py`'s existing comment, and it produces exactly the
  table-half-computed-under-each-definition state this column exists to detect.

**No index on this column, deliberately.** Its two readers are a whole-table
`count(*)` for the gauge and a `title_id`-scoped count for one seed; the first
scans whatever the table is regardless, and the second is already served by the
primary key's leading column. An index here would be write cost on every
rebuild for a query that is either a full count or already covered.
"""

import sqlalchemy as sa
from alembic import op

revision = "ffb"
down_revision = "ffa"
branch_labels = None
depends_on = None

# M6's three-signal blend, as `blend_fingerprint()` would have serialised it.
# A literal rather than an import: a migration must describe the schema at its
# own point in history, and importing the live function would make this
# backfill silently follow every future weight change -- re-labelling M6's rows
# as whatever the newest blend happens to be, which is the exact confusion the
# column exists to end.
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
