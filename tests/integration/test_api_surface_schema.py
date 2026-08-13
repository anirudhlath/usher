"""`m09a`'s four tables and two indexes, asserted against a real database.

`tests/unit/test_db_models_api_surface.py` owns the *declarations*; this file
owns what Postgres will actually do with them. The same split
`test_curation_schema.py` and `test_search_schema.py` make, and it is the one
that matters here: three of the four properties this migration was written for
-- a CHECK body, a delete rule, and an operator class -- are invisible to
`compare_metadata`, so a model and a migration can agree with each other and
disagree with the database.

**One case per table for existence, deliberately.** A migration that ships
three tables of four passes a check naming only the first, which is the rule
`m08a` needed for two tables and this head needs for four.

**And the index cases carry their own premise.** An index that exists proves
nothing about what it serves: `ix_titles_name_lower_year` is a btree over
`(lower(name), year)` with the *default* opclass and it has been on `titles`
since M1, so "there is a btree on `lower(name)`" was already true before this
migration and `LIKE 'pre%'` still could not use it. The planner probe under
`SET LOCAL enable_seqscan = off` is what tells the two apart, and it was run
against the pre-migration schema first -- with `m09a.upgrade()` still a `pass`,
`EXPLAIN` for that predicate is a `Seq Scan on titles` even with seq scans
disabled, i.e. the existing index is not merely not-chosen, it is not
*choosable*. That measurement is the whole of "two indexes, not one".
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.search import SEARCH_NAME_MAX_CHARS
from usher.db.repositories._errors import constraint_name
from usher.domain.ids import new_id


async def _exists(session: AsyncSession, table: str) -> bool:
    result = await session.execute(
        text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    )
    return bool(result.scalar_one())


async def _primary_key(session: AsyncSession, table: str) -> str | None:
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE contype = 'p' AND conrelid = to_regclass('public.' || :table)"
        ),
        {"table": table},
    )
    return result.scalar_one_or_none()


async def _seed_title(session: AsyncSession, name: str = "A Film") -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text("INSERT INTO titles (id, kind, name, sort_name) VALUES (:id, 'movie', :name, :name)"),
        {"id": title_id, "name": name},
    )
    return title_id


async def _seed_person(session: AsyncSession) -> uuid.UUID:
    person_id = new_id()
    await session.execute(
        text("INSERT INTO people (id, name, sort_name) VALUES (:id, 'A Person', 'Person, A')"),
        {"id": person_id},
    )
    return person_id


async def _seed_episode(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A series title plus one episode hanging off it. Returns both ids
    because `images.episode_id` needs the episode and the cascade case needs
    the title above it."""
    title_id = new_id()
    season_id = new_id()
    episode_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name) "
            "VALUES (:id, 'series', 'A Series', 'A Series')"
        ),
        {"id": title_id},
    )
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": season_id, "title_id": title_id},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :title_id, :season_id, 1, 1)"
        ),
        {"id": episode_id, "title_id": title_id, "season_id": season_id},
    )
    return title_id, episode_id


_INSERT_IMAGE = text(
    "INSERT INTO images "
    "(id, title_id, episode_id, person_id, kind, provider, provider_path, "
    " width, height, language, is_primary) "
    "VALUES (:id, :title_id, :episode_id, :person_id, :kind, :provider, :provider_path, "
    "        :width, :height, :language, :is_primary)"
)


def _image(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": new_id(),
        "title_id": None,
        "episode_id": None,
        "person_id": None,
        "kind": "poster",
        "provider": "tmdb",
        # A provider *path*, not a URL -- `m09c` renamed the column, because
        # ADR-0032's ladder is `{base}{rung}{path}` and a stored URL turns rung
        # selection into string surgery. Each caller of `_image` that needs two
        # distinct rows overrides it, since `uq_images_owner_provider_path` now
        # refuses a second row with the same owner, provider and path.
        "provider_path": "/an-invented-path.jpg",
        "width": 500,
        "height": 750,
        "language": "en",
        "is_primary": True,
    }
    row.update(overrides)
    return row


# --- one case per table, four of them ---------------------------------------


async def test_the_images_table_and_its_primary_key_exist(session: AsyncSession) -> None:
    assert await _exists(session, "images")
    assert await _primary_key(session, "images") == "pk_images"


async def test_the_search_queries_table_and_its_primary_key_exist(session: AsyncSession) -> None:
    assert await _exists(session, "search_queries")
    assert await _primary_key(session, "search_queries") == "pk_search_queries"


async def test_the_row_provider_settings_table_and_its_primary_key_exist(
    session: AsyncSession,
) -> None:
    assert await _exists(session, "row_provider_settings")
    assert await _primary_key(session, "row_provider_settings") == "pk_row_provider_settings"


async def test_the_title_search_names_table_and_its_primary_key_exist(
    session: AsyncSession,
) -> None:
    assert await _exists(session, "title_search_names")
    assert await _primary_key(session, "title_search_names") == "pk_title_search_names"


# --- `search_queries` carries PRD 10's nine columns and no tenth -------------


async def test_search_queries_carries_prd_tens_nine_columns_and_no_tenth(
    session: AsyncSession,
) -> None:
    """`requested_mode` is wire-only. PRD 10 assigns this table to M9 *whole*
    because a half-populated analytics table is worse than an empty metric --
    a dashboard reading it cannot tell a real zero from a column nobody
    filled -- and the other half of "whole" is that nothing is added to it
    speculatively either."""
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'search_queries'"
        )
    )
    assert {row[0] for row in result} == {
        "id",
        "at",
        "user_id",
        "query",
        "mode",
        "result_count",
        "latency_ms",
        "clicked_title_id",
        "played",
    }


async def test_search_queries_ships_no_index_beyond_its_primary_key(
    session: AsyncSession,
) -> None:
    """`genome_tags`' precedent, and `genome_scores`' before it. Its readers
    are PRD 10's dashboards, which do not exist yet; an index whose reader is
    a later milestone is `ix_titles_popularity` again, and it is the failure
    PRD 09's boundary call 9 names, inverted."""
    result = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
        {"t": "search_queries"},
    )
    assert {row[0] for row in result} == {"pk_search_queries"}


# --- the delete rules, read off `pg_constraint` ------------------------------


async def test_the_three_image_foreign_keys_carry_the_delete_rule_they_were_given(
    session: AsyncSession,
) -> None:
    """All three CASCADE, and the reason is not "artwork is cheap" -- it is
    that `ck_images_exactly_one_owner` makes SET NULL *unavailable*. Nulling
    the one non-null owner column leaves `num_nonnulls(...) = 0`, which the
    CHECK refuses, so the parent delete would fail with a constraint violation
    naming a table the operator never touched. RESTRICT would make deleting a
    title fail because somebody cached a poster for it.

    Read off `pg_constraint`, not off `Base.metadata`: `confdeltype` is what
    Postgres will actually do. `confdeltype::text` is not decoration -- the
    column's type is `"char"`, which asyncpg hands back as `bytes`, so the
    uncast comparison fails against `b'c'`. `c` is CASCADE."""
    result = await session.execute(
        text(
            "SELECT conname, confdeltype::text FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid = 'public.images'::regclass"
        )
    )
    assert {name: rule for name, rule in result.all()} == {
        "fk_images_title_id_titles": "c",
        "fk_images_episode_id_episodes": "c",
        "fk_images_person_id_people": "c",
    }


async def test_the_search_queries_foreign_keys_carry_two_different_delete_rules(
    session: AsyncSession,
) -> None:
    """The asymmetry is the content. `clicked_title_id` is SET NULL (`n`) --
    a deleted title must not delete the row recording what somebody searched
    for, because the search happened and the attribution is a separate fact.
    `user_id` is RESTRICT (`r`) -- a household's search history is user state,
    which is the side of ADR-0010's asymmetry
    `fk_watch_states_episode_id_episodes` already sits on."""
    result = await session.execute(
        text(
            "SELECT conname, confdeltype::text FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid = 'public.search_queries'::regclass"
        )
    )
    assert {name: rule for name, rule in result.all()} == {
        "fk_search_queries_clicked_title_id_titles": "n",
        "fk_search_queries_user_id_users": "r",
    }


async def test_the_title_search_names_foreign_key_cascades(session: AsyncSession) -> None:
    """CASCADE, `title_embeddings`' case rather than `watch_states`'. A search
    name protects no user state and is fully re-derivable from the title plus
    a loader."""
    result = await session.execute(
        text(
            "SELECT conname, confdeltype::text FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid = 'public.title_search_names'::regclass"
        )
    )
    assert {name: rule for name, rule in result.all()} == {
        "fk_title_search_names_title_id_titles": "c",
    }


async def test_every_cascade_in_this_migration_has_an_index_the_lookup_can_use(
    session: AsyncSession,
) -> None:
    """Postgres implements ON DELETE CASCADE by finding referencing rows *by
    that column*, so a CASCADE without a lookup index sequentially scans the
    child table on every parent delete. M4's
    `test_both_new_foreign_keys_have_an_index_the_referential_check_can_use`
    makes the identical argument for `ix_media_items_episode_id` and
    `ix_watch_states_episode_id`, and `ix_title_neighbors_neighbor_id` exists
    for it too.

    `enable_seqscan = off` forces the planner to reveal whether a usable index
    exists *at all*, which is the property being claimed -- an empty table
    would otherwise seq-scan regardless of how many indexes it has, and prove
    nothing.

    **`search_queries` is deliberately absent from this list**, and it is the
    one place in `m09a` where a declared delete rule has no lookup behind it:
    the table ships no index beyond its primary key, so
    `fk_search_queries_clicked_title_id_titles`' SET NULL scans it on every
    title delete. That is the plan's call, recorded in the migration docstring
    rather than quietly repaired here.

    **`images.title_id` has two acceptable answers since `m09c`, and that is a
    measurement rather than a shrug.** `uq_images_owner_provider_path` leads on
    `title_id`, so it can serve this lookup too -- and on the *empty* table this
    fixture builds, the two cost identically (`4.16..9.52` for both) and the
    planner's tie-break is arbitrary: it named the unique constraint the first
    time `m09c` ran against this case. Measured on `pgvector/pgvector:pg17`
    with 200,000 images over 40,000 titles and `ANALYZE` run, which is the
    state a real deployment is in:

    | index | size | chosen for `WHERE title_id = ?` |
    |---|---|---|
    | `ix_images_title_id` | 2,680 kB | **yes**, `Index Scan`, 4 buffers |
    | `uq_images_owner_provider_path` | 13 MB | no |

    The same narrow index is chosen for the real parent `DELETE` and for
    `list_for_title`. So `ix_images_title_id` is not made redundant by `m09c`,
    it is simply indistinguishable from the wider index at zero rows -- and the
    property this case claims is *"a usable index exists at all"*, which both
    satisfy. Naming one of them would be asserting a tie-break.
    """
    probes = [
        ("images", "title_id", {"ix_images_title_id", "uq_images_owner_provider_path"}),
        ("images", "episode_id", {"ix_images_episode_id"}),
        ("images", "person_id", {"ix_images_person_id"}),
        ("title_search_names", "title_id", {"ix_title_search_names_title_id"}),
    ]
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    for table, column, acceptable in probes:
        result = await session.execute(
            text(
                f"EXPLAIN SELECT 1 FROM {table} "  # noqa: S608 -- both are literals above
                f"WHERE {column} = '00000000-0000-0000-0000-000000000000' FOR KEY SHARE"
            )
        )
        plan = "\n".join(row[0] for row in result)
        # A *set*, and three of the four hold exactly one name -- so this is
        # not a blanket "some index was used", which `Seq Scan` would also have
        # to be excluded from by hand. Only the column with two genuine
        # candidates has two.
        assert any(name in plan for name in acceptable), f"{table}.{column}: {plan}"


# --- the CHECK bodies, exercised rather than described -----------------------


async def test_an_image_with_no_owner_is_refused_by_a_named_constraint(
    session: AsyncSession,
) -> None:
    with pytest.raises(IntegrityError) as caught:
        await session.execute(_INSERT_IMAGE, _image())
    assert constraint_name(caught.value) == "ck_images_exactly_one_owner"


async def test_an_image_with_two_owners_is_refused_by_the_same_constraint(
    session: AsyncSession,
) -> None:
    """The other half of `= 1`, and the half a `num_nonnulls(...) >= 1`
    spelling would let through. An image belonging to both a title and a
    person is not a poster with two homes, it is a row two readers will
    disagree about."""
    title_id = await _seed_title(session)
    person_id = await _seed_person(session)
    with pytest.raises(IntegrityError) as caught:
        await session.execute(
            _INSERT_IMAGE, _image(title_id=title_id, person_id=person_id, kind="profile")
        )
    assert constraint_name(caught.value) == "ck_images_exactly_one_owner"


@pytest.mark.parametrize("owner", ["title_id", "episode_id", "person_id"])
async def test_an_image_round_trips_through_each_owner_column(
    session: AsyncSession, owner: str
) -> None:
    """Parametrised over all three, because a CHECK naming
    `num_nonnulls(title_id, episode_id, person_id)` is satisfied by exactly
    one of them and a migration that misspelled one column name would still
    pass a case that only ever exercises `title_id`."""
    if owner == "person_id":
        owner_id = await _seed_person(session)
    elif owner == "episode_id":
        _, owner_id = await _seed_episode(session)
    else:
        owner_id = await _seed_title(session)

    row = _image(**{owner: owner_id})
    await session.execute(_INSERT_IMAGE, row)
    stored = await session.execute(
        text(f"SELECT {owner}, kind, provider, is_primary FROM images WHERE id = :id"),  # noqa: S608
        {"id": row["id"]},
    )
    assert stored.one() == (owner_id, "poster", "tmdb", True)


async def test_deleting_a_title_cascades_into_its_images(session: AsyncSession) -> None:
    title_id = await _seed_title(session)
    row = _image(title_id=title_id)
    await session.execute(_INSERT_IMAGE, row)

    await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": title_id})
    remaining = await session.execute(
        text("SELECT count(*) FROM images WHERE id = :id"), {"id": row["id"]}
    )
    assert remaining.scalar_one() == 0


async def test_a_search_name_longer_than_the_btree_bound_is_refused_by_a_constraint(
    session: AsyncSession,
) -> None:
    """The ordering-of-two-refusals argument
    `test_the_genome_tag_id_column_is_wide_enough_that_a_constraint_refuses_it_first`
    already makes for `genome_tags.tag_id`, arriving at a text column.

    Postgres refuses a btree entry over 2,704 bytes on an 8 kB page, and a
    long alias out of IMDb's `title.akas` must be refused by a *named*
    constraint with a classifiable `IntegrityError` rather than by the index
    at insert time -- an index-side refusal carries no constraint name for
    `constraint_name()` to report, so a loader cannot tell it from any other
    write failure.

    The arithmetic is in the migration docstring; this case pins that one
    character over the bound is refused, which is where the two spellings
    (`<=` and `<`) differ."""
    title_id = await _seed_title(session)
    with pytest.raises(IntegrityError) as caught:
        await session.execute(
            text(
                "INSERT INTO title_search_names (id, title_id, name, kind, region, language) "
                "VALUES (:id, :title_id, :name, 'alias', 'FR', 'fr')"
            ),
            {"id": new_id(), "title_id": title_id, "name": "a" * (SEARCH_NAME_MAX_CHARS + 1)},
        )
    assert constraint_name(caught.value) == "ck_title_search_names_name_within_btree_bound"


async def test_a_search_name_at_exactly_the_bound_is_stored_and_indexed(
    session: AsyncSession,
) -> None:
    """The premise of the case above: the bound is a bound and not an
    off-by-one, and -- the half that matters -- a name of exactly that length
    goes into the `text_pattern_ops` index without Postgres refusing the entry.
    A CHECK that let the index refuse first would fail here rather than
    there."""
    title_id = await _seed_title(session)
    row_id = new_id()
    await session.execute(
        text(
            "INSERT INTO title_search_names (id, title_id, name, kind, region, language) "
            "VALUES (:id, :title_id, :name, 'person', NULL, NULL)"
        ),
        {"id": row_id, "title_id": title_id, "name": "b" * SEARCH_NAME_MAX_CHARS},
    )
    stored = await session.execute(
        text("SELECT length(name), kind, region, language FROM title_search_names WHERE id = :id"),
        {"id": row_id},
    )
    assert stored.one() == (SEARCH_NAME_MAX_CHARS, "person", None, None)


async def test_row_provider_settings_is_created_empty(session: AsyncSession) -> None:
    """**Not seeded with ten slugs.** An absent row means enabled, which is
    what "providers are enabled by registration in code" already means. A
    migration hard-coding the registry would be a second copy of
    `services/rows/__init__.py` with nothing anywhere to detect drift --
    the exact shape `_SUSPENDABLE_INDEXES`' literal strings needed a dedicated
    round-trip case to stop. Reconciliation belongs to the admin task."""
    result = await session.execute(text("SELECT count(*) FROM row_provider_settings"))
    assert result.scalar_one() == 0


async def test_a_row_provider_setting_round_trips_on_its_natural_key(
    session: AsyncSession,
) -> None:
    """`RowProvider.slug_prefix` is the natural key -- "declared rather than
    derived" and "bounded at ten", which its own port docstring says. A
    surrogate id would permit two rows for one provider, a state no admin
    route could interpret."""
    await session.execute(
        text(
            "INSERT INTO row_provider_settings (slug_prefix, enabled, updated_at) "
            "VALUES ('genre-affinity', false, '2026-08-10T00:00:00Z')"
        )
    )
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO row_provider_settings (slug_prefix, enabled, updated_at) "
                "VALUES ('genre-affinity', true, '2026-08-10T00:00:00Z')"
            )
        )


# --- the two tier-1 prefix indexes, and the premise ---------------------------


@pytest.mark.parametrize(
    ("index_name", "table"),
    [
        ("ix_titles_name_lower_prefix", "titles"),
        ("ix_title_search_names_name_lower_prefix", "title_search_names"),
    ],
)
async def test_both_tier_one_indexes_carry_text_pattern_ops(
    session: AsyncSession, index_name: str, table: str
) -> None:
    """Asserted off `pg_indexes.indexdef` -- what Postgres actually built --
    rather than off `Base.metadata`, because an opclass is exactly the kind of
    thing `compare_metadata` does not diff on an expression index.

    Measured on a real 1,271,138-title catalog: p50 0.6 ms, p95 1.0 ms, max
    10 ms, 44 MB, building in 0.559 s
    (`.claude/rules/search-and-embeddings.md`). One index goes on `titles`,
    which is what answers canonical-name prefixes on day one; one goes on
    `title_search_names`, which is free on an empty table and is what the
    alias and people halves will read."""
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name AND tablename = :table"),
        {"name": index_name, "table": table},
    )
    definition = result.scalar_one_or_none()
    assert definition is not None, f"{index_name} is declared on the model and not in the database"
    assert "text_pattern_ops" in str(definition), definition
    assert "lower(name)" in str(definition), definition


async def test_the_existing_lower_name_index_has_the_default_opclass(
    session: AsyncSession,
) -> None:
    """The premise of the case below, stated as its own assertion rather than
    left implicit. `ix_titles_name_lower_year` has been on `titles` since M1
    and is `Index("ix_titles_name_lower_year", text("lower(name)"), "year")`
    -- a btree over `lower(name)` with no opclass named, which under this
    database's collation is `text_ops`. So "there is already a btree on
    `lower(name)`" is true and is not the same index, which is the thing the
    planner probe below exists to prove rather than assert."""
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_titles_name_lower_year'")
    )
    definition = str(result.scalar_one())
    assert "lower(name)" in definition, definition
    assert "text_pattern_ops" not in definition, definition


async def test_the_tier_one_index_serves_a_prefix_the_existing_index_cannot(
    session: AsyncSession,
) -> None:
    """The case with teeth, and the one that makes "two indexes, not one" a
    measurement instead of a claim.

    An index-exists assertion is a membership assertion, and a membership
    assertion is not a relevance test. `enable_seqscan = off` forces the
    planner to reveal whether a usable index exists at all -- the same
    discipline
    `test_both_new_foreign_keys_have_an_index_the_referential_check_can_use`
    applies to a referential lookup.

    **Run against the pre-migration schema first, with `m09a.upgrade()` still
    a `pass`:** the plan for this exact predicate was `Seq Scan on titles`
    even with seq scans disabled, so `ix_titles_name_lower_year` is not merely
    not-chosen for a `LIKE 'pre%'`, it is not choosable. That is what the
    default opclass costs under a non-`C` collation, and it is why
    `ix_titles_name_lower_prefix` is a second index rather than a rename."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await session.execute(
        text("EXPLAIN SELECT id FROM titles WHERE lower(name) LIKE 'pre%'")
    )
    plan = "\n".join(row[0] for row in result)
    assert "ix_titles_name_lower_prefix" in plan, plan
    assert "ix_titles_name_lower_year" not in plan, plan


async def test_the_tier_one_index_on_the_narrow_table_serves_the_same_prefix(
    session: AsyncSession,
) -> None:
    """The alias and people halves read this one. Free on an empty table
    today, and asserted now because the task that fills it is not the task
    that would notice the index was never usable."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await session.execute(
        text("EXPLAIN SELECT title_id FROM title_search_names WHERE lower(name) LIKE 'pre%'")
    )
    plan = "\n".join(row[0] for row in result)
    assert "ix_title_search_names_name_lower_prefix" in plan, plan


# The down/up cycle for this head lives in
# `test_migrations.py::test_a_full_down_and_up_cycle_restores_every_index`,
# not here. That case owns the one `-1` re-point M9 gets, it already builds a
# throwaway database and compares the whole index set across `base`/`head`, and
# a second copy of that harness in this file would be a second scratch database
# per run asserting the same five names.
