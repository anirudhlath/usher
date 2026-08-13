"""`PostgresImageRepository` against the real database.

The shared contract runs here unchanged, plus the four things a dict cannot
express: a foreign key, a column narrower than the field feeding it, a real row
count behind the upsert, and — the one that matters most — **that the key is
`NULLS NOT DISTINCT` and the obvious spelling would not have been**. That last
case is the whole reason `m09c` exists, and it is invisible on the fake arm by
construction: a Python tuple key treats `None` as an ordinary value, so the
careless DDL passes every shared case.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.image_repository_contract import (
    ImageRepositoryContract,
    ImageSeeder,
    image,
)
from usher.db.repositories._errors import constraint_name
from usher.db.repositories.image import PostgresImageRepository
from usher.domain.enums import ImageKind
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict


class PostgresImageSeeder(ImageSeeder):
    """A real `titles` row, because all three of this table's foreign keys are
    real and `fk_images_title_id_titles` is the one every case crosses."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def title(self) -> uuid.UUID:
        title_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name) "
                "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Title', 'An Invented Title')"
            ),
            {"id": title_id},
        )
        return title_id


class TestPostgresImageRepository(ImageRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresImageRepository:
        return PostgresImageRepository(session)

    @pytest.fixture
    def seeder(self, session: AsyncSession) -> PostgresImageSeeder:
        return PostgresImageSeeder(session)


async def _row_count(session: AsyncSession, title_id: uuid.UUID) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM images WHERE title_id = CAST(:id AS uuid)"), {"id": title_id}
    )
    return int(result.scalar_one())


async def test_the_key_is_nulls_not_distinct_and_the_obvious_spelling_would_not_be(
    session: AsyncSession,
) -> None:
    """**The case `m09c` exists for, and the only one that can see it.**

    The request in ADR-0032 reads *"a unique key over `(title_id, provider,
    provider_path)`"*, and the plain transcription of that is inert for two
    owner kinds in three: Postgres defaults a unique constraint to
    `NULLS DISTINCT`, so an episode- or person-owned duplicate indexes
    `(NULL, 'tmdb', '/x.jpg')` and never conflicts with another one. That
    spelling passes review, passes every test M9 writes — because M9 writes
    only title-owned artwork — and is silently missing for exactly the rows
    nobody is looking at.

    So this case does not assert on this repository at all. It asserts on the
    *constraint*, from the two directions that separate the two spellings:

    1. A **person-owned** duplicate is refused. Under `NULLS DISTINCT` it would
       be admitted, and this is the assertion that fails against the careless
       DDL.
    2. Two **different titles** sharing one path are still two rows. Without
       this the case would also pass against something merely stricter — a key
       on `(provider, provider_path)` alone, which would give one title the
       other's poster id.

    Written against raw SQL rather than through the port because the port
    writes title-owned rows only: the arm that the careless spelling breaks is
    the one no method here can reach, which is precisely why it needed a case
    of its own.
    """
    person_id = new_id()
    await session.execute(
        text(
            "INSERT INTO people (id, name, sort_name) "
            "VALUES (CAST(:id AS uuid), 'An Invented Person', 'An Invented Person')"
        ),
        {"id": person_id},
    )
    insert = text(
        "INSERT INTO images "
        "(id, title_id, episode_id, person_id, kind, provider, provider_path, is_primary) "
        "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), NULL, CAST(:person_id AS uuid), "
        "        :kind, 'tmdb', :path, false)"
    )
    row = {
        "id": new_id(),
        "title_id": None,
        "person_id": person_id,
        "kind": "profile",
        "path": "/a-headshot.jpg",
    }
    await session.execute(insert, row)

    with pytest.raises(IntegrityError) as caught:
        # A SAVEPOINT, so the refusal does not poison the session the rest of
        # this case still has work for -- Postgres aborts the whole transaction
        # on any statement error until a rollback.
        async with session.begin_nested():
            await session.execute(insert, {**row, "id": new_id()})
    # **The constraint name, not merely that something raised.** `pk_images`
    # would also be an `IntegrityError`, and the claim here is about which
    # constraint refused the row.
    assert constraint_name(caught.value) == "uq_images_owner_provider_path"

    # The other direction: two different titles may reference one path.
    seeder = PostgresImageSeeder(session)
    first = await seeder.title()
    second = await seeder.title()
    repository = PostgresImageRepository(session)
    await repository.replace_for_titles(
        [first, second], [image(first, "/shared.jpg"), image(second, "/shared.jpg")]
    )
    assert await _row_count(session, first) == 1
    assert await _row_count(session, second) == 1


async def test_the_constraint_is_declared_nulls_not_distinct_in_the_catalog(
    session: AsyncSession,
) -> None:
    """The declaration itself, read back off `pg_constraint`.

    The case above proves the *behaviour*, which is the thing that matters and
    is also the thing a future reader might "simplify" the DDL under while
    leaving green. This one pins the text Postgres will hand to `pg_dump`, so
    the spelling cannot drift without a named failure — the same reason
    `test_both_tier_one_prefix_indexes_declare_the_operator_class` pins a
    `postgresql_ops` key beside a planner probe rather than instead of one.
    """
    definition = (
        await session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'uq_images_owner_provider_path'"
            )
        )
    ).scalar_one()
    assert definition == (
        "UNIQUE NULLS NOT DISTINCT (title_id, episode_id, person_id, provider, provider_path)"
    )


async def test_a_re_derivation_writes_one_row_and_not_two(session: AsyncSession) -> None:
    """The physical claim behind the id-stability case, and one the in-memory
    dict cannot get wrong by construction: the shared contract asserts the id
    is the same, which a fake satisfies by keying on the tuple. This asserts
    the *row count*, which is what separates "the upsert found it" from "the
    read happened to return the newer of two rows"."""
    seeder = PostgresImageSeeder(session)
    repository = PostgresImageRepository(session)
    title_id = await seeder.title()

    await repository.replace_for_titles([title_id], [image(title_id, "/once.jpg")])
    await repository.replace_for_titles([title_id], [image(title_id, "/once.jpg", is_primary=True)])

    assert await _row_count(session, title_id) == 1


async def test_an_image_for_a_title_that_does_not_exist_is_a_port_error(
    session: AsyncSession,
) -> None:
    """`fk_images_title_id_titles`, translated. The fake enforces no foreign
    key, so this arm is the only one that can see it — and the translation is
    what ADR-0009 requires: a raw `sqlalchemy.exc` reaching a service is the
    one thing that must never cross this boundary.

    The session stays usable afterwards, which is the half a caller depends on:
    `DeriveService` commits a batch of images together with its job checkpoint,
    so a caught conflict must not leave the next unrelated statement raising
    `PendingRollbackError`.
    """
    repository = PostgresImageRepository(session)
    absent = new_id()

    with pytest.raises(RepositoryConflict) as caught:
        await repository.replace_for_titles([absent], [image(absent, "/orphan.jpg")])
    assert caught.value.constraint == "fk_images_title_id_titles"

    # Still usable: a real read runs on the same session.
    assert await repository.get(new_id()) is None


async def test_a_width_the_column_cannot_hold_is_a_port_error_too(
    session: AsyncSession,
) -> None:
    """**Not an `IntegrityError`, and that is the whole point.** `images.width`
    is `integer` and `Image.width` is `Field(gt=0)` with no ceiling, so
    `2**31` is a *validly constructed* domain model this column cannot hold.
    Measured on `curated_rows."position"` and recorded in `_errors.py`: asyncpg
    refuses it client-side before a byte is sent, as a bare
    `sqlalchemy.exc.DBAPIError` with SQLSTATE `22000`, which
    `except IntegrityError` does not catch.

    So this case is what pins the choice of `refusals_as_conflict` over the
    older house `except IntegrityError` for this repository. `constraint` is
    `None`, correctly: a column's declared width refusing a value is not a
    named constraint firing.
    """
    seeder = PostgresImageSeeder(session)
    repository = PostgresImageRepository(session)
    title_id = await seeder.title()

    with pytest.raises(RepositoryConflict) as caught:
        await repository.replace_for_titles(
            [title_id], [image(title_id, "/too-wide.jpg", width=2**31)]
        )
    assert caught.value.constraint is None


async def test_primary_for_titles_answers_a_whole_shelf_in_one_statement(
    session: AsyncSession,
) -> None:
    """The claim the fake counts, asserted here on the artefact that decides
    it: the *plan*. `rows-and-genome.md`'s finding is that an assertion about
    the query text passes against either spelling, so this walks the plan tree
    counting scan nodes on `images` and asserts there is exactly one.

    A loop calling `list_for_title` per card would not appear in this plan at
    all — it would be thirty plans — so the premise is asserted first: the one
    statement really did answer every title in the shelf.
    """
    seeder = PostgresImageSeeder(session)
    repository = PostgresImageRepository(session)
    titles = [await seeder.title() for _ in range(8)]
    await repository.replace_for_titles(
        titles,
        [image(one, f"/card-{index}.jpg", is_primary=True) for index, one in enumerate(titles)],
    )

    found = await repository.primary_for_titles(titles, ImageKind.POSTER)
    assert set(found) == set(titles), "the premise: one call answered the whole shelf"

    plan = (
        await session.execute(
            text(
                "EXPLAIN (FORMAT JSON) SELECT DISTINCT ON (title_id) * FROM images "
                "WHERE title_id = ANY(CAST(:title_ids AS uuid[])) AND kind = :kind "
                "ORDER BY title_id, is_primary DESC, id"
            ),
            {"title_ids": titles, "kind": "poster"},
        )
    ).scalar_one()

    def _scans(node: dict[str, object]) -> int:
        found_here = 1 if node.get("Relation Name") == "images" else 0
        children = node.get("Plans")
        if isinstance(children, list):
            for child in children:
                found_here += _scans(child)
        return found_here

    root = plan[0]["Plan"]
    assert _scans(root) == 1, plan
