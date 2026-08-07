"""`PostgresGenomeRepository` against the real database.

The shared contract runs here unchanged, plus the three things a dict cannot
express: the `halfvec(1128)` width declaration, the quantisation that
declaration costs, and the schema decisions boundary call 7 rests on -- no
index beyond the primary key, and a `CASCADE` to `titles`.

The seeder writes through a raw `INSERT` rather than through the port,
because this port has no writer and deliberately never will: the writer is
`BulkCatalogRepository.upsert_genome_vectors`, which is a staged, set-based
join and belongs on that port.
"""

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.genome_repository_contract import (
    RELEASE_A,
    WIDTH,
    GenomeRepositoryContract,
    GenomeSeeder,
    lanes,
)
from usher.db.repositories.genome import PostgresGenomeRepository
from usher.domain.ids import new_id


def _literal(relevance: tuple[float, ...]) -> str:
    """pgvector's text input form. `CAST(:x AS halfvec)` below rather than
    `:x::halfvec` -- SQLAlchemy's bind-parameter regex reads a name followed
    by `::` as a Postgres cast and skips the bind entirely, so the literal
    string `:relevance::halfvec` reaches asyncpg and it answers with a syntax
    error at `":"`."""
    return "[" + ",".join(repr(value) for value in relevance) + "]"


class PostgresGenomeSeeder(GenomeSeeder):
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

    async def vector(
        self, title_id: uuid.UUID, relevance: tuple[float, ...], *, revision: str = RELEASE_A
    ) -> None:
        await self._session.execute(
            text(
                "INSERT INTO genome_scores (title_id, relevance, genome_revision) VALUES ("
                "CAST(:id AS uuid), CAST(:relevance AS halfvec), :revision)"
            ),
            {"id": title_id, "relevance": _literal(relevance), "revision": revision},
        )

    async def tags(self, tags: Sequence[tuple[int, str]], *, revision: str = RELEASE_A) -> None:
        # A raw `INSERT` rather than `BulkCatalogRepository.replace_genome_tags`,
        # for the reason the class docstring gives and one more that is
        # specific to this table: that writer refuses a vocabulary with a gap,
        # which is precisely what one contract case has to store.
        for tag_id, tag in tags:
            await self._session.execute(
                text(
                    "INSERT INTO genome_tags (tag_id, tag, genome_revision) "
                    "VALUES (:tag_id, :tag, :revision)"
                ),
                {"tag_id": tag_id, "tag": tag, "revision": revision},
            )


class TestPostgresGenomeRepository(GenomeRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresGenomeRepository:
        return PostgresGenomeRepository(session)

    @pytest.fixture
    def seeder(self, session: AsyncSession) -> PostgresGenomeSeeder:
        return PostgresGenomeSeeder(session)


async def test_a_vector_of_the_wrong_width_is_refused_by_the_column(
    session: AsyncSession,
) -> None:
    """The declaration `halfvec(1128)` is a real constraint and not
    documentation. This is what the fake cannot fail on -- a dict stores
    whatever it is handed -- and it is why `MovieLensGenomeDataset` verifies
    the vocabulary width against `genome-tags.csv` *before* reading a score:
    a release whose vocabulary grew must fail naming both widths, not
    16,376 rows later inside a COPY with a dimension error naming neither
    the dataset nor the release.
    """
    seeder = PostgresGenomeSeeder(session)
    title_id = await seeder.title()
    with pytest.raises(Exception, match="dimensions"):
        await seeder.vector(title_id, (0.5, 0.25))


async def test_deleting_a_title_takes_its_genome_vector_with_it(
    session: AsyncSession,
) -> None:
    """`ON DELETE CASCADE`, and it is the `title_embeddings` case rather than
    the `watch_states` one. ADR-0010 makes `watch_states.title_id` RESTRICT
    because a watch state is *user state* a delete would destroy silently. A
    genome vector is neither user state nor irrecoverable -- it is fully
    re-derivable from the archive plus the title's `imdb_id`. The merge case
    runs the same way: after a repointing merge the loser's vector describes
    a film that is no longer the canonical title, so it should die with the
    loser rather than block the delete or survive attached to nothing.

    Kills a migration written with `RESTRICT` (which would make every title
    merge fail once the genome is loaded) and one with no `ondelete` at all
    (Postgres defaults to `NO ACTION`, i.e. the same refusal).
    """
    seeder = PostgresGenomeSeeder(session)
    title_id = await seeder.title()
    await seeder.vector(title_id, lanes(0.5))

    await session.execute(text("DELETE FROM titles WHERE id = CAST(:id AS uuid)"), {"id": title_id})

    remaining = await session.execute(
        text("SELECT count(*) FROM genome_scores WHERE title_id = CAST(:id AS uuid)"),
        {"id": title_id},
    )
    assert remaining.scalar_one() == 0


async def test_genome_scores_carries_no_index_beyond_its_primary_key(
    session: AsyncSession,
) -> None:
    """Pins boundary call 7's index decision.

    The access pattern is a pair lookup by `title_id`, not a KNN -- nothing
    asks this table for its nearest neighbours -- and an HNSW index cannot
    help a lookup by primary key at all. Measured against a real 15,565-row
    load: `get_pair` is **0.062 ms**, two primary-key probes under a
    `BitmapOr`. An unindexed KNN over the same table is 59.4-66.2 ms, so if a
    future consumer ever wants one this reopens on evidence rather than being
    foreclosed. M6 separately measured a planner-*preferred* index costing
    4.3x for byte-identical recall, and an index nothing reads is
    `ix_titles_popularity` again.

    (The plan's "1.190 ms for a full pairwise cosine" is wrong: a real full
    pairwise self-join measures 384 s. See the migration docstring.)

    Kills a later migration that adds one "for similarity". The 624 kB of
    index inside the measured 45 MB is this primary key.
    """
    result = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'genome_scores' ORDER BY 1")
    )
    assert [row[0] for row in result] == ["pk_genome_scores"]


async def test_the_stored_vector_is_quantised_but_neither_truncated_nor_reordered(
    session: AsyncSession,
) -> None:
    """`halfvec` is lossy and the loss is bounded and directional-only.

    The contract's round-trip case compares with a tolerance so both arms can
    share it; this one states what that tolerance is buying. Every lane comes
    back within half-precision's own resolution, the width is unchanged, and
    the *order* is unchanged -- which is the property no similarity number
    would ever reveal, because a reversed vector is a perfectly well-formed
    vector describing a different film.
    """
    seeder = PostgresGenomeSeeder(session)
    title_id = await seeder.title()
    stored = tuple((index % 97) / 97.0 for index in range(WIDTH))
    await seeder.vector(title_id, stored)

    row = await PostgresGenomeRepository(session).get(title_id)

    assert row is not None
    assert len(row.relevance) == WIDTH
    assert row.relevance != stored, "identical round-trip would mean this is not a halfvec"
    assert row.relevance == pytest.approx(stored, abs=1e-3)
