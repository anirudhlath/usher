"""`PostgresCollectionRepository` against the real database.

The shared contract runs here unchanged, plus what a dict cannot express: a
foreign key, and the `IS DISTINCT FROM` guard being observable at all.

The guard's *cost* is invisible in both halves -- neither run measures the
tsvector recompute or the GIN write it exists to avoid. What both can see is
the returned count, which is why the port promises **changed** rather than
touched, and why the contract asserts the first call's count as well as the
second's.
"""

import uuid
from datetime import date
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.collection_repository_contract import (
    CollectionRepositoryContract,
    CollectionSeeder,
    collection,
)
from usher.db.repositories.collection import PostgresCollectionRepository
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict


class PostgresCollectionSeeder(CollectionSeeder):
    """Real `titles` and `media_items` rows.

    `media_items` needs a `source_id`, so one source row is created lazily and
    reused -- `uq_media_items_source_external` is per (source, external id),
    so one source can hold every item this suite writes.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._source_id: uuid.UUID | None = None
        self._external = 0

    async def _title(self, kind: str) -> uuid.UUID:
        title_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name) "
                "VALUES (CAST(:id AS uuid), :kind, 'An Invented Title', 'An Invented Title')"
            ),
            {"id": title_id, "kind": kind},
        )
        return title_id

    async def movie(self) -> uuid.UUID:
        return await self._title("movie")

    async def series(self) -> uuid.UUID:
        return await self._title("series")

    async def _source(self) -> uuid.UUID:
        if self._source_id is None:
            self._source_id = new_id()
            await self._session.execute(
                # `credentials_ref` and `device_id` are both NOT NULL -- a
                # source names its credential row and its durable device even
                # when nothing in this file ever reads either.
                text(
                    "INSERT INTO sources "
                    "(id, kind, name, base_url, credentials_ref, device_id, enabled) "
                    "VALUES (CAST(:id AS uuid), 'emby', 'An Invented Source', "
                    "'https://source.invalid', :credentials_ref, :device_id, true)"
                ),
                {
                    "id": self._source_id,
                    "credentials_ref": f"source/{self._source_id}",
                    "device_id": f"device-{self._source_id}",
                },
            )
        return self._source_id

    async def own(
        self, title_id: uuid.UUID, *, available: bool = True, as_episode: bool = False
    ) -> None:
        source_id = await self._source()
        self._external += 1
        episode_id: uuid.UUID | None = None
        if as_episode:
            # A real episode row, because `media_items.episode_id` is a
            # foreign key. Its season is needed for the same reason one level
            # down: `episodes.season_id` is NOT NULL.
            season_id = new_id()
            await self._session.execute(
                text(
                    "INSERT INTO seasons (id, title_id, season_number) "
                    "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), 1)"
                ),
                {"id": season_id, "title_id": title_id},
            )
            episode_id = new_id()
            await self._session.execute(
                text(
                    "INSERT INTO episodes "
                    "(id, title_id, season_id, season_number, episode_number) "
                    "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), "
                    "CAST(:season_id AS uuid), 1, 1)"
                ),
                {"id": episode_id, "title_id": title_id, "season_id": season_id},
            )
        await self._session.execute(
            text(
                "INSERT INTO media_items "
                "(id, source_id, external_id, title_id, episode_id, available, last_seen_at) "
                "VALUES (CAST(:id AS uuid), CAST(:source_id AS uuid), :external_id, "
                "CAST(:title_id AS uuid), CAST(:episode_id AS uuid), :available, now())"
            ),
            {
                "id": new_id(),
                "source_id": source_id,
                "external_id": f"item-{self._external}",
                "title_id": title_id,
                "episode_id": episode_id,
                "available": available,
            },
        )

    async def collection_of(self, title_id: uuid.UUID) -> uuid.UUID | None:
        stored = (
            await self._session.execute(
                text("SELECT collection_id FROM titles WHERE id = CAST(:id AS uuid)"),
                {"id": title_id},
            )
        ).scalar_one()
        return cast(uuid.UUID | None, stored)


class TestPostgresCollectionRepository(CollectionRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresCollectionRepository:
        return PostgresCollectionRepository(session)

    @pytest.fixture
    def seeder(self, session: AsyncSession) -> PostgresCollectionSeeder:
        return PostgresCollectionSeeder(session)

    async def test_upsert_reports_inserts_and_updates_separately(
        self, repository: PostgresCollectionRepository
    ) -> None:
        """`xmax = 0` again, and the fake cannot express it: it computes the
        split from dict membership, which *is* the answer rather than a
        measurement of it. A mixed batch, so both arms fire in one statement.
        """
        await repository.upsert_many([collection(98_000_030, "Already Here")])
        mixed = await repository.upsert_many(
            [collection(98_000_030, "Already Here"), collection(98_000_031, "Brand New")]
        )
        assert (mixed.inserted, mixed.updated) == (1, 1)

    async def test_a_link_to_no_collection_is_a_port_error(
        self, repository: PostgresCollectionRepository, seeder: PostgresCollectionSeeder
    ) -> None:
        """`fk_titles_collection_id_collections`. Postgres-only: the fake has
        no foreign keys, so it cannot raise here at all.

        This is what `resolve_tmdb_ids`' "absent means no such collection"
        rule protects against -- a resolve that minted an id would land here.
        """
        movie_id = await seeder.movie()
        with pytest.raises(RepositoryConflict):
            await repository.attach_titles([(movie_id, new_id())])

    async def test_a_link_to_no_title_is_not_an_error(
        self, repository: PostgresCollectionRepository
    ) -> None:
        """An `UPDATE` that matches nothing is not a failure, and the port
        says so: treating it as one would make a concurrent title merge fail a
        whole derivation.

        The count is what distinguishes "not an error" from "silently did
        something", so it is asserted rather than the absence of a raise.
        """
        await repository.upsert_many([collection(98_000_032, "An Invented Collection")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_032]))[98_000_032]
        assert await repository.attach_titles([(new_id(), collection_id)]) == 0

    async def test_the_member_list_is_ordered_by_release_date(
        self, repository: PostgresCollectionRepository, seeder: PostgresCollectionSeeder
    ) -> None:
        """`array_agg(... ORDER BY m.release_date NULLS LAST, m.year NULLS
        LAST, m.title_id)`, which the fake cannot express -- it has no release
        date and falls back to insertion order, so the contract asserts on the
        member *set* rather than its sequence.

        A franchise row renders in release order or it renders wrong, and the
        wrong implementation this kills is a bare `array_agg` whose order
        Postgres does not promise. Seeded out of order so insertion order and
        release order disagree.
        """
        await repository.upsert_many([collection(98_000_033, "An Invented Collection")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_033]))[98_000_033]

        later = await seeder.movie()
        earlier = await seeder.movie()
        for title_id, release_date in (
            (later, date(2019, 1, 1)),
            (earlier, date(2001, 1, 1)),
        ):
            await repository._session.execute(
                text("UPDATE titles SET release_date = :date WHERE id = CAST(:id AS uuid)"),
                {"date": release_date, "id": title_id},
            )
            await repository.attach_titles([(title_id, collection_id)])
            await seeder.own(title_id)

        listed = await repository.list_owned()
        assert [one for one in listed[0].title_ids] == [earlier, later]
