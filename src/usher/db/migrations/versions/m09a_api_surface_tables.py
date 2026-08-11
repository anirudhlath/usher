"""M9's whole schema in one revision: `images`, `search_queries`,
`row_provider_settings`, `title_search_names`, and the two tier-1 prefix
indexes.

Revision ID: m09a
Revises: m08b
Create Date: 2026-08-10

**`m09a`, zero-padded, per the convention `m08a` opened and
`.claude/rules/db-and-sql.md` names in advance** — milestone-prefixed, two
digits, because unpadded `sorted(["m8a", "m9a", "m10a"])` puts `m10a` *first*.
`m08b` is the verified parent: it declares `Revises: m08a`, nothing revises it,
and `tests/unit/test_db_migration_status.py` pinned `code_head_revision() ==
"m08b"` until this file existed.

## One migration, and the chain that was refused

The first drafting pass pre-allocated `m09a`…`m09g` across four task groups on
the theory that a revision id each would let them author in parallel. It does
the **opposite**: every integration test runs `alembic upgrade head`, so a
worktree holding `m09d` cannot migrate until `m09a` to `m09c` merge. That is a
serial spine across four groups, which is the exact thing the split existed to
avoid. Precedent for one migration carrying unrelated tables is `m08a`, which
shipped `curated_rows` and `llm_calls` together — two tables sharing no column,
no foreign key and no lifetime.

So this is M9's only migration on this track, and there is exactly one head.
`m09b` carries the IMDb provenance schema on the other track; **`m09c` is spare
and must be requested, never minted.** Two consumer questions are already known
to be beyond this revision — a cached-derivative table for the image proxy, and
`title_tags` if the genome gate clears its bar — and the answer to either is a
request, not a second head.

## What this revision deliberately does not deliver: behaviour

No domain model, no port ABC, no repository, no service, no route. `images`
gets a table and a SQLAlchemy row and no `Image` domain model; `search_queries`
gets nine columns and no writer; `row_provider_settings` gets a primary key and
no admin route; `title_search_names` gets a shape and stays empty. Every
consumer carries its own behaviour, and each row module's docstring holds the
argument for its own shape rather than repeating it here.

## Enum columns, and the `CREATE TYPE` that does not exist

`usher.db.base.enum_column` compiles `native_enum=False` to `VARCHAR(length)`
with a `values_callable` binding each member's `.value`. Every `sa.Enum` in
every migration in this schema carries `native_enum=False` explicitly, so
**there is no Postgres enum type anywhere in this database** — nothing to
create here and nothing to drop in `downgrade()`. The three new enum columns
are spelled with their literal member values below, because at
`native_enum=False` the member list never reaches the DDL at all: only the
width does. Membership is Pydantic's, exactly as it is for every other enum
column in this schema.

`search_queries.mode` reuses `usher.ports.search.SearchMode` rather than
minting a copy. `usher.db` sits outside the four-layer import contract, so the
import is legal, and `usher/domain/search.py` deliberately declares no
`SearchMode` — a decision this migration honours rather than reverses.

## Two prefix indexes, and the existing one serves neither

`ix_titles_name_lower_year` is **not** the tier-1 index. It is
`Index("ix_titles_name_lower_year", text("lower(name)"), "year")` with the
*default* opclass, which under this database's collation cannot answer
`LIKE 'pre%'`. Measured on `pgvector/pgvector:pg17` at the pre-`m09a` schema:
with `SET enable_seqscan = off`, the plan for `WHERE lower(name) LIKE 'pre%'`
is still `Seq Scan on titles` at cost 1e10 — not merely not-chosen, not
choosable. With `(lower(name) text_pattern_ops)` present the same query is
`Index Scan using ix_titles_name_lower_prefix`, `Index Cond: ((lower(name)
~>=~ 'pre') AND (lower(name) ~<~ 'prf'))`.

Two indexes therefore, not one: one on `titles`, which answers canonical-name
prefixes on day one and joins `_SUSPENDABLE_INDEXES` because `titles` is the
table the bulk loader writes in million-row bursts; one on
`title_search_names`, which is free on an empty table, is what the alias and
people halves will read, and does **not** join that dict because `bulk.py` does
not write that table. Measured cost of the shape on a real 1,271,138-title
catalog: p50 0.6 ms, p95 1.0 ms, max 10 ms, 44 MB, building in 0.559 s
(`.claude/rules/search-and-embeddings.md`).

## The one declared delete rule with no lookup behind it

`images`' three cascades and `title_search_names`' one each get an index, for
the reason M4 added `ix_media_items_episode_id` and M6 added
`ix_title_neighbors_neighbor_id`: Postgres implements a delete rule by finding
referencing rows *by that column*, so a rule without an index scans the child
table on every parent delete.

**`search_queries` is the exception, and it is stated rather than hidden.** It
ships no index beyond its primary key — `genome_tags`' precedent, and PRD 09's
boundary call 9 says an index whose reader is a later milestone is the failure
this table is named in — so `fk_search_queries_clicked_title_id_titles`' SET
NULL scans the table on every title delete. Empty today and the analytics task
owns the measurement; what would reverse the call is that task finding the scan
in a plan, not a reader wanting one.

## CHECK bodies are hand-written and verified by eye

`--autogenerate` is blind to CHECK **bodies** and to triggers and functions
entirely: a loosened bound produces an empty `pass` migration with no warning.
Every CHECK here is written by hand and compared body-for-body against
`Base.metadata` by
`tests/integration/test_migrations.py::test_every_check_constraint_in_the_models_exists_in_the_database`.

`ck_title_search_names_name_within_btree_bound`'s number is imported from
`usher.db.models.search.SEARCH_NAME_MAX_CHARS` — `m08b` imported
`GENOME_TAG_COUNT` for the same reason — so the migration and the model cannot
drift. Its arithmetic is on that constant: Postgres refuses a btree entry over
`BTMaxItemSize`, 2,704 bytes on an 8 kB page, and 512 characters is 2,048 bytes
at UTF-8's four-byte worst case plus 12 bytes of index-tuple and varlena
header, i.e. 2,060 against 2,704.

## No trigger, and that set does not move

`test_migration_creates_the_updated_at_triggers` asserts the `set_updated_at`
trigger set **exactly**, and all four tables here carry none.
`images` is replaced wholesale per owner (`credits`' precedent, which has no
`updated_at` at all for exactly this reason), `search_queries` records
something that already happened, `title_search_names` is replaced per
`(title_id, kind)`, and `row_provider_settings`' one writer sets `updated_at`
explicitly on every statement (`jobs`' precedent).

Reversible in both directions. `downgrade()` drops exactly what `upgrade()`
creates, statement for statement. Verified empty → head → `-1` → head and
`downgrade base` → head against a real `pgvector/pgvector:pg17`.
"""

import sqlalchemy as sa
from alembic import op

from usher.db.models.search import SEARCH_NAME_MAX_CHARS

revision = "m09a"
down_revision = "m08b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        # Three nullable owners, exactly one of which is set -- see the CHECK
        # below and `db/models/image.py` for why this is not three tables and
        # not a polymorphic pair. All three CASCADE: SET NULL is *unavailable*
        # under `ck_images_exactly_one_owner` (it would leave zero owners and
        # the parent delete would fail naming a table the operator never
        # touched), and RESTRICT would make deleting a title fail because
        # somebody cached a poster for it.
        sa.Column(
            "title_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("titles.id", ondelete="CASCADE", name="fk_images_title_id_titles"),
            nullable=True,
        ),
        sa.Column(
            "episode_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("episodes.id", ondelete="CASCADE", name="fk_images_episode_id_episodes"),
            nullable=True,
        ),
        sa.Column(
            "person_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("people.id", ondelete="CASCADE", name="fk_images_person_id_people"),
            nullable=True,
        ),
        # `VARCHAR(16)` via `native_enum=False`, this schema's only enum
        # spelling. `backdrop` is the longest member at 8 characters.
        sa.Column(
            "kind",
            sa.Enum(
                "poster",
                "backdrop",
                "logo",
                "still",
                "profile",
                name="imagekind",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("remote_url", sa.Text(), nullable=False),
        # Nullable: a provider that reports no dimensions is ordinary, and a
        # placeholder `0` is a lie a layout engine acts on.
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_images"),
        # `= 1`, not `>= 1`. An image belonging to both a title and a person
        # is a row two readers will disagree about.
        sa.CheckConstraint(
            "num_nonnulls(title_id, episode_id, person_id) = 1",
            name="ck_images_exactly_one_owner",
        ),
        sa.CheckConstraint("provider <> ''", name="ck_images_provider_not_empty"),
        sa.CheckConstraint("remote_url <> ''", name="ck_images_remote_url_not_empty"),
        sa.CheckConstraint("width IS NULL OR width > 0", name="ck_images_width_positive"),
        sa.CheckConstraint("height IS NULL OR height > 0", name="ck_images_height_positive"),
    )
    # The three cascades' own lookups -- Postgres finds referencing rows *by
    # that column*. Deleting any of these three lines is not an equivalent
    # mutation: unlike `m08a`'s `drop_index`, these create indexes no other
    # statement creates, and `tests/integration/test_api_surface_schema.py`
    # probes the planner for each under `enable_seqscan = off`.
    op.create_index("ix_images_title_id", "images", ["title_id"], unique=False)
    op.create_index("ix_images_episode_id", "images", ["episode_id"], unique=False)
    op.create_index("ix_images_person_id", "images", ["person_id"], unique=False)

    op.create_table(
        "search_queries",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        # When the search happened, not when the row was inserted, so no
        # `server_default`. `at` rather than `created_at` because PRD 10's
        # column list says `at` -- `llm_calls`' call, one table over.
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        # RESTRICT: a household's search history is user state, ADR-0010's
        # `fk_watch_states_episode_id_episodes` side of the asymmetry.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_search_queries_user_id_users"),
            nullable=False,
        ),
        sa.Column("query", sa.Text(), nullable=False),
        # `usher.ports.search.SearchMode`; `full_text` is the longest member at
        # 9 characters.
        sa.Column(
            "mode",
            sa.Enum(
                "full_text",
                "semantic",
                "fused",
                name="searchmode",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        # SET NULL: a deleted title must not delete the row recording what
        # somebody searched for.
        sa.Column(
            "clicked_title_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "titles.id",
                ondelete="SET NULL",
                name="fk_search_queries_clicked_title_id_titles",
            ),
            nullable=True,
        ),
        # NOT NULL with no default, `llm_calls.ok`'s precedent: a dashboard
        # must read a real `false` rather than a column nobody filled.
        sa.Column("played", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_search_queries"),
        sa.CheckConstraint("query <> ''", name="ck_search_queries_query_not_empty"),
        sa.CheckConstraint("result_count >= 0", name="ck_search_queries_result_count_non_negative"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_search_queries_latency_ms_non_negative"),
    )
    # No index on `search_queries` beyond its primary key, and the SET NULL
    # scan that costs -- see this migration's docstring.

    op.create_table(
        "row_provider_settings",
        # `RowProvider.slug_prefix` is the natural key: declared rather than
        # derived, bounded at ten. A surrogate id would permit two rows for one
        # provider, a state no admin route could interpret.
        sa.Column("slug_prefix", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("slug_prefix", name="pk_row_provider_settings"),
        sa.CheckConstraint("slug_prefix <> ''", name="ck_row_provider_settings_slug_not_empty"),
    )
    # **Created empty, and not seeded with ten slugs.** An absent row means
    # enabled, which is what "providers are enabled by registration in code"
    # already means. A migration hard-coding the registry is a second copy of
    # `services/rows/__init__.py` with nothing anywhere to detect drift.

    op.create_table(
        "title_search_names",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "title_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "titles.id", ondelete="CASCADE", name="fk_title_search_names_title_id_titles"
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # Two members, `alias` and `person`, each with a named emitter inside
        # M9. There is deliberately no `primary`: a canonical name is served
        # by `ix_titles_name_lower_prefix` on `titles`, so a `primary` row
        # would be the duplication M6's boundary call 3 refused arriving under
        # a new table name.
        sa.Column(
            "kind",
            sa.Enum("alias", "person", name="searchnamekind", native_enum=False, length=16),
            nullable=False,
        ),
        # IMDb `title.akas`' own two. Without them a French and a Brazilian
        # alias for the same film are indistinguishable rows -- a defect the
        # loader cannot repair later without a second migration.
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_title_search_names"),
        sa.CheckConstraint("name <> ''", name="ck_title_search_names_name_not_empty"),
        # The btree bound, with its arithmetic in this migration's docstring.
        # The constant is imported so the CHECK body cannot drift from the
        # model's -- `m08b` imported `GENOME_TAG_COUNT` for the same reason.
        sa.CheckConstraint(
            f"length(name) <= {SEARCH_NAME_MAX_CHARS}",
            name="ck_title_search_names_name_within_btree_bound",
        ),
        # **No unique constraint.** The write is replace-scoped on
        # `(title_id, kind)`, matching `credits`; what would reverse that is a
        # writer that upserts.
    )
    op.create_index(
        "ix_title_search_names_title_id", "title_search_names", ["title_id"], unique=False
    )

    # The two tier-1 prefix indexes. The opclass is inside the `text()`
    # deliberately: `postgresql_ops` keys match columns and not expressions, so
    # the other spelling compiles to a plain `(lower(name))` with no error and
    # builds an index that cannot serve the query it exists for -- measured by
    # compiling both.
    op.create_index(
        "ix_titles_name_lower_prefix",
        "titles",
        [sa.text("lower(name) text_pattern_ops")],
        unique=False,
    )
    op.create_index(
        "ix_title_search_names_name_lower_prefix",
        "title_search_names",
        [sa.text("lower(name) text_pattern_ops")],
        unique=False,
    )


def downgrade() -> None:
    # `ix_titles_name_lower_prefix` is the one artefact here that no
    # `drop_table` takes with it -- it sits on `titles`, which survives -- so
    # this line is load-bearing in the way `ff`'s `create_index` is and
    # `m08a`'s `drop_index` is not. `tests/integration/test_migrations.py`'s
    # `-1` half cannot see it (an index is not a primary key), so
    # `test_a_full_cycle_restores_the_four_tables_and_both_indexes` asserts it
    # by name after one step back.
    op.drop_index("ix_titles_name_lower_prefix", table_name="titles")
    op.drop_table("title_search_names")
    op.drop_table("row_provider_settings")
    op.drop_table("search_queries")
    op.drop_table("images")
