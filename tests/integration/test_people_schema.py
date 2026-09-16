"""Everything about these three tables that only real Postgres can answer."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.ids import new_id


async def _indexdef(session: AsyncSession, name: str) -> str | None:
    return (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": name}
        )
    ).scalar_one_or_none()


async def _title(
    session: AsyncSession,
    *,
    kind: str = "movie",
    collection_id: uuid.UUID | None = None,
) -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, collection_id) "
            "VALUES (CAST(:id AS uuid), :kind, 'The Quiet Vacuum', 'The Quiet Vacuum', "
            "CAST(:collection_id AS uuid))"
        ),
        {"id": title_id, "kind": kind, "collection_id": collection_id},
    )
    return title_id


async def _person(session: AsyncSession, *, tmdb_id: int | None = None) -> uuid.UUID:
    person_id = new_id()
    await session.execute(
        text(
            "INSERT INTO people (id, tmdb_id, name, sort_name) "
            "VALUES (CAST(:id AS uuid), :tmdb_id, :name, :name)"
        ),
        {"id": person_id, "tmdb_id": tmdb_id, "name": "Someone Invented"},
    )
    return person_id


async def _collection(session: AsyncSession, *, tmdb_id: int = 98_000_100) -> uuid.UUID:
    collection_id = new_id()
    await session.execute(
        text(
            "INSERT INTO collections (id, tmdb_id, name) "
            "VALUES (CAST(:id AS uuid), :tmdb_id, 'An Invented Collection')"
        ),
        {"id": collection_id, "tmdb_id": tmdb_id},
    )
    return collection_id


async def _credit(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    title_id: uuid.UUID,
    tmdb_credit_id: str | None = None,
    billing_order: int | None = 0,
) -> uuid.UUID:
    credit_id = new_id()
    await session.execute(
        text(
            "INSERT INTO credits "
            "(id, person_id, title_id, kind, source, tmdb_credit_id, billing_order) "
            "VALUES (CAST(:id AS uuid), CAST(:person_id AS uuid), CAST(:title_id AS uuid), "
            "'cast', 'tmdb', :tmdb_credit_id, :billing_order)"
        ),
        {
            "id": credit_id,
            "person_id": person_id,
            "title_id": title_id,
            "tmdb_credit_id": tmdb_credit_id,
            "billing_order": billing_order,
        },
    )
    return credit_id


async def test_two_people_who_share_a_name_are_two_rows(session: AsyncSession) -> None:
    """Rules out deduping people by `name`, which collapses two directors who share one.

    There is deliberately no unique constraint on `name`, and this is the case that
    would fail if somebody added one for "cleanliness".
    """
    for tmdb_id in (93_000_010, 93_000_011):
        await session.execute(
            text(
                "INSERT INTO people (id, tmdb_id, name, sort_name) "
                "VALUES (CAST(:id AS uuid), :tmdb_id, :name, :name)"
            ),
            {"id": new_id(), "tmdb_id": tmdb_id, "name": "Another Invention"},
        )
    count = await session.execute(
        text("SELECT count(*) FROM people WHERE name = 'Another Invention'")
    )
    assert count.scalar_one() == 2


async def test_the_tmdb_id_index_is_unique_and_partial(session: AsyncSession) -> None:
    """Partial for `ix_titles_imdb_id`'s reason: NULL never collides with NULL.

    An explicit WHERE is what lets Postgres use the index for lookups that already
    filter IS NOT NULL, and what obliges an `ON CONFLICT` against it to repeat the
    predicate. Read off `pg_indexes.indexdef` rather than through behaviour, because a
    non-partial unique index passes every behavioural case here.
    """
    definition = await _indexdef(session, "ix_people_tmdb_id")
    assert definition is not None
    assert "UNIQUE" in definition
    assert "WHERE (tmdb_id IS NOT NULL)" in definition


async def test_two_people_with_no_tmdb_id_do_not_collide(session: AsyncSession) -> None:
    """The partial index's other half: two people with no `tmdb_id` are two rows.

    A derivation that writes a person without a `tmdb_id` must not be blocked by the one
    before it.
    """
    for _ in range(2):
        await session.execute(
            text(
                "INSERT INTO people (id, name, sort_name) "
                "VALUES (CAST(:id AS uuid), 'Nameless', 'Nameless')"
            ),
            {"id": new_id()},
        )
    count = await session.execute(
        text("SELECT count(*) FROM people WHERE tmdb_id IS NULL AND name = 'Nameless'")
    )
    assert count.scalar_one() == 2


async def test_a_duplicated_credit_id_is_refused(session: AsyncSession) -> None:
    """The natural key is unique, so a bug in the delete's scope raises.

    Rules out `credit_id` stored as a plain non-unique column, which would double a
    title's cast on every derivation pass instead of failing.
    """
    person_id = await _person(session, tmdb_id=93_000_020)
    first_title = await _title(session)
    second_title = await _title(session)
    await _credit(session, person_id=person_id, title_id=first_title, tmdb_credit_id="9" * 24)
    with pytest.raises(IntegrityError):
        await _credit(session, person_id=person_id, title_id=second_title, tmdb_credit_id="9" * 24)


async def test_two_credits_with_no_tmdb_credit_id_do_not_collide(
    session: AsyncSession,
) -> None:
    """The partial half of that same index.

    A future non-TMDb derivation has no credit ObjectId at all, and the constraint must
    not be what blocks it: a provider identifier is never identity.
    """
    person_id = await _person(session, tmdb_id=93_000_021)
    title_id = await _title(session)
    await _credit(session, person_id=person_id, title_id=title_id, tmdb_credit_id=None)
    await _credit(session, person_id=person_id, title_id=title_id, tmdb_credit_id=None)
    count = await session.execute(
        text("SELECT count(*) FROM credits WHERE title_id = CAST(:id AS uuid)"),
        {"id": title_id},
    )
    assert count.scalar_one() == 2


async def test_deleting_a_collection_nulls_its_titles_rather_than_deleting_them(
    session: AsyncSession,
) -> None:
    """`ON DELETE SET NULL`: the title is worth keeping and merely loses its link.

    CASCADE would delete the films in the collection, and RESTRICT would refuse every
    collection delete, since a collection with no members is never written. The next
    derivation re-attaches the title.
    """
    collection_id = await _collection(session)
    title_id = await _title(session, collection_id=collection_id)

    await session.execute(
        text("DELETE FROM collections WHERE id = CAST(:id AS uuid)"), {"id": collection_id}
    )

    row = await session.execute(
        text("SELECT collection_id FROM titles WHERE id = CAST(:id AS uuid)"), {"id": title_id}
    )
    assert row.scalar_one() is None


async def test_deleting_a_title_takes_its_credits_with_it(session: AsyncSession) -> None:
    """CASCADE, deliberately the opposite of `watch_states.title_id`'s RESTRICT.

    After a repointing merge the loser's credits duplicate the winner's and are wrong,
    so they die with the loser rather than block the delete. RESTRICT would make
    deleting any enriched title fail, which is nearly always.
    """
    person_id = await _person(session, tmdb_id=93_000_030)
    title_id = await _title(session)
    await _credit(session, person_id=person_id, title_id=title_id)

    await session.execute(text("DELETE FROM titles WHERE id = CAST(:id AS uuid)"), {"id": title_id})

    count = await session.execute(
        text("SELECT count(*) FROM credits WHERE title_id = CAST(:id AS uuid)"),
        {"id": title_id},
    )
    assert count.scalar_one() == 0


async def test_deleting_a_person_takes_their_credits_with_it(session: AsyncSession) -> None:
    """`seasons.title_id`'s argument verbatim: a credit protects nothing.

    No user state, and fully re-derivable from a cached payload in one pass.
    """
    person_id = await _person(session, tmdb_id=93_000_031)
    title_id = await _title(session)
    await _credit(session, person_id=person_id, title_id=title_id)

    await session.execute(
        text("DELETE FROM people WHERE id = CAST(:id AS uuid)"), {"id": person_id}
    )

    count = await session.execute(
        text("SELECT count(*) FROM credits WHERE person_id = CAST(:id AS uuid)"),
        {"id": person_id},
    )
    assert count.scalar_one() == 0


async def test_the_new_foreign_keys_carry_the_delete_rule_they_were_given(
    session: AsyncSession,
) -> None:
    """Read back off `pg_constraint`, not off `Base.metadata`.

    `confdeltype` is what Postgres will actually do: `c` is CASCADE, `n` is SET NULL.
    The cast to `text` is not decoration — the column's type is `"char"`, which asyncpg
    hands back as `bytes`, so the uncast comparison fails against `b'c'`.
    """
    result = await session.execute(
        text(
            "SELECT conname, confdeltype::text FROM pg_constraint "
            "WHERE contype = 'f' AND conname IN "
            "('fk_credits_person_id_people', 'fk_credits_title_id_titles', "
            "'fk_titles_collection_id_collections')"
        )
    )
    assert {name: rule for name, rule in result.all()} == {
        "fk_credits_person_id_people": "c",
        "fk_credits_title_id_titles": "c",
        "fk_titles_collection_id_collections": "n",
    }


async def test_the_collection_id_index_exists_and_is_partial(session: AsyncSession) -> None:
    """The index is the whole of `FranchiseProvider`'s read, and of `collections`' SET NULL.

    Partial because `collection_id` is NULL on every series row — `belongs_to_collection`
    is movies-only — and on most movie rows, so there is nothing to place "last" inside
    the index at all.
    """
    definition = await _indexdef(session, "ix_titles_collection_id")
    assert definition is not None
    assert "WHERE (collection_id IS NOT NULL)" in definition


async def test_a_credit_cannot_name_a_title_that_does_not_exist(
    session: AsyncSession,
) -> None:
    """Postgres-only: the fake is a dict and has no foreign key to violate."""
    person_id = await _person(session, tmdb_id=93_000_040)
    with pytest.raises(IntegrityError):
        await _credit(session, person_id=person_id, title_id=new_id())


@pytest.mark.parametrize(
    "statement,params",
    [
        (
            "INSERT INTO people (id, name, sort_name) "
            "VALUES (CAST(:id AS uuid), '', 'Someone Invented')",
            {},
        ),
        (
            "INSERT INTO people (id, name, sort_name) "
            "VALUES (CAST(:id AS uuid), 'Someone Invented', '')",
            {},
        ),
        (
            "INSERT INTO collections (id, tmdb_id, name) VALUES (CAST(:id AS uuid), 98000200, '')",
            {},
        ),
    ],
)
async def test_the_check_constraints_mirror_the_pydantic_bounds(
    session: AsyncSession, statement: str, params: dict[str, object]
) -> None:
    """The bulk COPY path constructs no pydantic model, so the bound has to be a CHECK.

    Every CHECK here mirrors a `Field(...)` in `usher/domain/people.py`, and
    `test_every_check_constraint_in_the_models_exists_in_the_database` keeps the two
    from drifting: alembic is blind to a changed CHECK body and to a missing one.
    """
    with pytest.raises(DBAPIError):
        await session.execute(text(statement), {"id": new_id(), **params})


@pytest.mark.parametrize("billing_order,tmdb_credit_id", [(-1, None), (0, "")])
async def test_the_credit_check_constraints_mirror_the_pydantic_bounds(
    session: AsyncSession, billing_order: int, tmdb_credit_id: str | None
) -> None:
    """The credit bounds are CHECKs too, since the staged path never constructs the model."""
    person_id = await _person(session, tmdb_id=93_000_050)
    title_id = await _title(session)
    with pytest.raises(DBAPIError):
        await _credit(
            session,
            person_id=person_id,
            title_id=title_id,
            tmdb_credit_id=tmdb_credit_id,
            billing_order=billing_order,
        )
