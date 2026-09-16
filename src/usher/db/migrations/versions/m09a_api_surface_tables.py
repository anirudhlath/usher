"""`images`, `search_queries`, `row_provider_settings` and `title_search_names`."""

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
        # Three nullable owners, exactly one of which is set -- see the CHECK below and
        # `db/models/image.py` for why this is not three tables and not a polymorphic
        # pair.
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
        # RESTRICT: a household's search history is user state, the same side of
        # the asymmetry `fk_watch_states_episode_id_episodes` sits on.
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
        # The btree bound `SEARCH_NAME_MAX_CHARS` states, imported rather than
        # spelled out so the CHECK body cannot drift from the model's.
        sa.CheckConstraint(
            f"length(name) <= {SEARCH_NAME_MAX_CHARS}",
            name="ck_title_search_names_name_within_btree_bound",
        ),
        # **No unique constraint.** The write is replace-scoped on
        # `(title_id, kind)`, matching `credits`, so nothing here upserts.
    )
    op.create_index(
        "ix_title_search_names_title_id", "title_search_names", ["title_id"], unique=False
    )

    # The two tier-1 prefix indexes. The opclass is inside the `text()`
    # deliberately: `postgresql_ops` keys match columns and not expressions, so
    # the other spelling compiles to a plain `(lower(name))` with no error and
    # builds an index that cannot serve the query it exists for.
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
    # `ix_titles_name_lower_prefix` is the one artefact here that no `drop_table` takes
    # with it -- it sits on `titles`, which survives -- so this line is load-bearing in
    # the way `ff`'s `create_index` is and `m08a`'s `drop_index` is not.
    op.drop_index("ix_titles_name_lower_prefix", table_name="titles")
    op.drop_table("title_search_names")
    op.drop_table("row_provider_settings")
    op.drop_table("search_queries")
    op.drop_table("images")
