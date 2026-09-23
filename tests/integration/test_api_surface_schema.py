"""`m09a`'s four tables and two indexes, asserted against a real database."""

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
    """A series title plus one episode hanging off it.

    Returns both ids because `images.episode_id` needs the episode and the cascade case
    needs the title above it.
    """
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
        # A provider *path*, not a URL: the image ladder is `{base}{rung}{path}`
        # and a stored URL turns rung selection into string surgery. Each caller
        # of `_image` that needs two distinct rows overrides it, since
        # `uq_images_owner_provider_path` refuses a second row with the same
        # owner, provider and path.
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


# --- `search_queries` carries PRD 10's eleven columns and no twelfth ---------


async def test_search_queries_carries_prd_tens_columns_and_no_others(
    session: AsyncSession,
) -> None:
    """`requested_mode` is wire-only.

    PRD 10 assigns this table *whole* because a half-populated analytics table is worse
    than an empty metric -- a dashboard reading it cannot tell a real zero from a column
    nobody filled -- and the other half of "whole" is that nothing is added to it
    speculatively either.

    The eleven stay closed rather than becoming a floor: `m10c` takes the two suggest
    columns -- `surface` and `tier`, both named in PRD 10 -- and nothing else. No
    `keystroke_index`, no `session_id`, no `debounced` flag; a twelfth column is a red
    here.
    """
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
        "surface",
        "tier",
    }


async def test_search_queries_ships_one_index_beyond_its_primary_key(
    session: AsyncSession,
) -> None:
    """One index beyond the primary key, and its reader exists today.

    An index whose reader is a later milestone is `ix_titles_popularity` again;
    `ix_search_queries_at`'s reader is written out verbatim in PRD 10 -- `DELETE FROM
    search_queries WHERE at < now() - interval '90 days'`, an operator's own SQL.

    That the statement plans onto it is asserted in
    `tests/integration/test_m10_schema.py`; that no *second* index appeared alongside it
    is asserted here.
    """
    result = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
        {"t": "search_queries"},
    )
    assert {row[0] for row in result} == {"pk_search_queries", "ix_search_queries_at"}


# --- the delete rules, read off `pg_constraint` ------------------------------


async def test_the_three_image_foreign_keys_carry_the_delete_rule_they_were_given(
    session: AsyncSession,
) -> None:
    """All three CASCADE, and the reason is not "artwork is cheap".

    It is that `ck_images_exactly_one_owner` makes SET NULL *unavailable*. Nulling the
    one non-null owner column leaves `num_nonnulls(...) = 0`, which the
    CHECK refuses, so the parent delete would fail with a constraint violation naming a
    table the operator never touched. RESTRICT would make deleting a title fail because
    somebody cached a poster for it.

    Read off `pg_constraint`, not off `Base.metadata`: `confdeltype` is what
    Postgres will actually do. `confdeltype::text` is not decoration -- the
    column's type is `"char"`, which asyncpg hands back as `bytes`, so the
    uncast comparison fails against `b'c'`. `c` is CASCADE.
    """
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
    """The asymmetry is the content.

    `clicked_title_id` is SET NULL (`n`) -- a deleted title must not delete the row
    recording what somebody searched for, because the search happened and the
    attribution is a separate fact. `user_id` is RESTRICT (`r`) -- a household's search
    history is user state, the same side of that asymmetry
    `fk_watch_states_episode_id_episodes` already sits on.
    """
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
    """CASCADE, `title_embeddings`' case rather than `watch_states`'.

    A search name protects no user state and is fully re-derivable from the title plus a
    loader.
    """
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
    """Postgres implements ON DELETE CASCADE by finding referencing rows *by that column*.

    So a CASCADE without a lookup index sequentially scans the child table on every
    parent delete.
    """
    probes = [
        ("images", "title_id"),
        ("images", "episode_id"),
        ("images", "person_id"),
        ("title_search_names", "title_id"),
    ]
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    for table, column in probes:
        result = await session.execute(
            text(
                f"EXPLAIN SELECT 1 FROM {table} "  # noqa: S608 -- both are literals above
                f"WHERE {column} = '00000000-0000-0000-0000-000000000000' FOR KEY SHARE"
            )
        )
        plan = "\n".join(row[0] for row in result)
        assert f"Seq Scan on {table}" not in plan, f"{table}.{column}: {plan}"
        assert f"Index Cond: ({column} = " in plan, f"{table}.{column}: {plan}"


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
    """The other half of `= 1`, and what a `num_nonnulls(...) >= 1` spelling lets past.

    An image belonging to both a title and a person is not a poster with two homes, it
    is a row two readers will disagree about.
    """
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
    """Parametrised over all three owner columns.

    A CHECK naming `num_nonnulls(title_id, episode_id, person_id)` is satisfied by
    exactly one of them, and a migration that misspelled one column name would still
    pass a case that only ever exercises `title_id`.
    """
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
    """The constraint has to refuse before the index does.

    Postgres refuses a btree entry over 2,704 bytes on an 8 kB page, and a long alias
    out of IMDb's `title.akas` must be refused by a *named* constraint with a
    classifiable `IntegrityError` rather than by the index at insert time -- an
    index-side refusal carries no constraint name for `constraint_name()` to report, so
    a loader cannot tell it from any other write failure.

    The arithmetic is in the migration docstring; this case pins that one character over
    the bound is refused, which is where the two spellings (`<=` and `<`) differ.
    """
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
    """The premise of the case above: the bound is a bound and not an off-by-one.

    A name of exactly that length goes into the `text_pattern_ops` index without
    Postgres refusing the entry, and a CHECK that let the index refuse first would fail
    here rather than there.
    """
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
    """**Not seeded with ten slugs.** An absent row means enabled.

    Which is what "providers are enabled by registration in code" already means. A
    migration hard-coding the registry would be a second copy of
    `services/rows/__init__.py` with nothing anywhere to detect drift. Reconciliation
    belongs to the admin task.
    """
    result = await session.execute(text("SELECT count(*) FROM row_provider_settings"))
    assert result.scalar_one() == 0


async def test_a_row_provider_setting_round_trips_on_its_natural_key(
    session: AsyncSession,
) -> None:
    """`RowProvider.slug_prefix` is the natural key.

    "Declared rather than derived" and "bounded at ten", which its own port docstring
    says. A surrogate id would permit two rows for one provider, a state no admin route
    could interpret.
    """
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
    """Asserted off `pg_indexes.indexdef`, which is what Postgres actually built.

    Rather than off `Base.metadata`, because an opclass is exactly the kind of thing
    `compare_metadata` does not diff on an expression index.

    One index goes on `titles`, which is what answers canonical-name prefixes on day
    one; one goes on `title_search_names`, which is free on an empty table and is what
    the alias and people halves will read.
    """
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
    """The premise of the case below, stated as its own assertion rather than left implicit.

    `ix_titles_name_lower_year` is on `titles` and is
    `Index("ix_titles_name_lower_year", text("lower(name)"), "year")` -- a btree over
    `lower(name)` with no opclass named, which under this database's collation is
    `text_ops`. So "there is already a btree on `lower(name)`" is true and is not the
    same index, which is the thing the planner probe below exists to prove rather than
    assert.
    """
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_titles_name_lower_year'")
    )
    definition = str(result.scalar_one())
    assert "lower(name)" in definition, definition
    assert "text_pattern_ops" not in definition, definition


async def test_the_tier_one_index_serves_a_prefix_the_existing_index_cannot(
    session: AsyncSession,
) -> None:
    """The case with teeth, and the one that makes "two indexes, not one" a fact.

    An index-exists assertion is a membership assertion, and a membership assertion is
    not a relevance test. `enable_seqscan = off` forces the planner to reveal whether a
    usable index exists at all.

    Without `ix_titles_name_lower_prefix`, the plan for this predicate is `Seq Scan on
    titles` even with seq scans disabled: `ix_titles_name_lower_year` is not merely
    not-chosen for a `LIKE 'pre%'`, it is not choosable. That is what the default
    opclass costs under a non-`C` collation, and it is why this is a second index rather
    than a rename.
    """
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
    """The alias and people halves read this one.

    Free on an empty table today, and asserted now because the task that fills it is not
    the task that would notice the index was never usable.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await session.execute(
        text("EXPLAIN SELECT title_id FROM title_search_names WHERE lower(name) LIKE 'pre%'")
    )
    plan = "\n".join(row[0] for row in result)
    assert "ix_title_search_names_name_lower_prefix" in plan, plan


# The down/up cycle for this head lives in
# `test_migrations.py::test_a_full_down_and_up_cycle_restores_every_index`, not here.
