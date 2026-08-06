"""`PostgresTasteRepository` against the real database.

The shared contract runs here unchanged, plus the four things a dict cannot
express: `halfvec(384)`'s quantisation, the `NULL`-vs-`NULL` three-valued logic
the whole invalidation rests on, the `CASCADE` to `users`, and the fact that
`user_taste` adds no `updated_at` trigger.

**The tolerance here is `abs=1e-3` and in `tests/unit/test_services_taste.py`
it is `abs=1e-9`.** Both are stated in both files so nobody "fixes" one to
match the other: the gap is `halfvec(384)`'s measured max round-trip cosine
error of 1.21e-04, which exists only where a vector crosses this boundary.

The history hooks write through raw `INSERT`/`DELETE` rather than through
`WatchStateRepository`, and each for its own reason. The insert, because
`trg_watch_states_set_updated_at` is a `BEFORE UPDATE` trigger that assigns
`now()` unconditionally -- so an `UPDATE` cannot set `updated_at` to a chosen
instant and an `INSERT` is the only way to own that column, which is the same
route `tests/integration/test_services_watch_sync.py` takes. The delete,
because `WatchStateRepository` has no delete method: PRD 02 hard-deletes
nothing through a port, so there is no port call that expresses the household
that unwatched something.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.taste_repository_contract import (
    COMPUTED_AT,
    EARLIER,
    MODEL,
    TasteRepositoryContract,
    _vector,
)
from usher.db.repositories.taste import PostgresTasteRepository
from usher.domain.ids import new_id
from usher.ports.repository import StoredTaste

USER = uuid.UUID("00000000-0000-7000-8000-00000000000a")
OTHER_USER = uuid.UUID("00000000-0000-7000-8000-00000000000b")
SOURCE = uuid.UUID("00000000-0000-7000-8000-0000000000ff")


async def _seed_users(session: AsyncSession) -> None:
    for user_id, name in ((USER, "Primary"), (OTHER_USER, "Other")):
        await session.execute(
            text("INSERT INTO users (id, name) VALUES (CAST(:id AS uuid), :name)"),
            {"id": user_id, "name": name},
        )


async def _seed_title(session: AsyncSession) -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name) "
            "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Title', 'An Invented Title')"
        ),
        {"id": title_id},
    )
    return title_id


class TestPostgresTasteRepository(TasteRepositoryContract):
    @pytest.fixture(autouse=True)
    async def _schema(self, session: AsyncSession) -> None:
        self._session = session
        await _seed_users(session)
        await session.execute(
            text(
                "INSERT INTO sources "
                "(id, kind, name, base_url, credentials_ref, device_id) VALUES ("
                "CAST(:id AS uuid), 'emby', 'An Invented Source', "
                "'https://source.invalid', 'an-invented-ref', 'an-invented-device')"
            ),
            {"id": SOURCE},
        )

    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresTasteRepository:
        return PostgresTasteRepository(session)

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return USER

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:
        return OTHER_USER

    async def add_history(self, user_id: uuid.UUID, *, at: datetime) -> uuid.UUID:
        title_id = await _seed_title(self._session)
        state_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO watch_states "
                "(id, user_id, title_id, position_seconds, played, play_count, "
                " last_played_at, updated_at, origin) VALUES ("
                "CAST(:id AS uuid), CAST(:user_id AS uuid), CAST(:title_id AS uuid), "
                "60, true, 1, CAST(:at AS timestamptz), CAST(:at AS timestamptz), 'source')"
            ),
            {"id": state_id, "user_id": user_id, "title_id": title_id, "at": at},
        )
        return state_id

    async def drop_history(self, handle: uuid.UUID) -> None:
        await self._session.execute(
            text("DELETE FROM watch_states WHERE id = CAST(:id AS uuid)"), {"id": handle}
        )

    async def add_title(self, genres: tuple[str, ...], *, owned: bool) -> uuid.UUID:
        title_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name, genres) VALUES ("
                "CAST(:id AS uuid), 'movie', 'An Invented Title', 'An Invented Title', "
                "CAST(:genres AS text[]))"
            ),
            {"id": title_id, "genres": list(genres)},
        )
        if owned:
            await self._copy(title_id, episode_id=None)
        return title_id

    async def add_owned_copy(self, title_id: uuid.UUID) -> None:
        await self._copy(title_id, episode_id=None)

    async def add_owned_episode_copy(self, title_id: uuid.UUID, *, copies: int) -> None:
        # A real `episodes` row per copy, because `media_items.episode_id` is a
        # foreign key -- the fake has no such constraint, which is the third
        # thing only this arm can express.
        for number in range(copies):
            season_id = new_id()
            episode_id = new_id()
            await self._session.execute(
                text(
                    "INSERT INTO seasons (id, title_id, season_number) VALUES ("
                    "CAST(:id AS uuid), CAST(:title_id AS uuid), :n)"
                ),
                {"id": season_id, "title_id": title_id, "n": number + 1},
            )
            await self._session.execute(
                text(
                    "INSERT INTO episodes "
                    "(id, title_id, season_id, season_number, episode_number) VALUES ("
                    "CAST(:id AS uuid), CAST(:title_id AS uuid), CAST(:season_id AS uuid), "
                    ":n, 1)"
                ),
                {
                    "id": episode_id,
                    "title_id": title_id,
                    "season_id": season_id,
                    "n": number + 1,
                },
            )
            await self._copy(title_id, episode_id=episode_id)

    async def _copy(self, title_id: uuid.UUID, *, episode_id: uuid.UUID | None) -> None:
        await self._session.execute(
            text(
                "INSERT INTO media_items "
                "(id, source_id, title_id, episode_id, external_id, last_seen_at) VALUES ("
                "CAST(:id AS uuid), CAST(:source_id AS uuid), CAST(:title_id AS uuid), "
                "CAST(:episode_id AS uuid), :external_id, now())"
            ),
            {
                "id": new_id(),
                "source_id": SOURCE,
                "title_id": title_id,
                "episode_id": episode_id,
                "external_id": str(new_id()),
            },
        )


async def test_a_stored_vector_survives_the_halfvec_round_trip_to_a_thousandth(
    session: AsyncSession,
) -> None:
    """`halfvec(384)`'s quantisation, measured rather than assumed.

    The published figure is a max cosine error of 1.21e-04 over 1,000 real
    vectors -- three orders of magnitude below the useful signal. This asserts
    the per-lane consequence, which is what a reader of a centroid actually
    holds, and it is the whole reason this file's tolerance is 1e-3 where the
    unit file's is 1e-9.
    """
    await _seed_users(session)
    repository = PostgresTasteRepository(session)
    lanes = tuple(0.001 * lane for lane in range(384))
    await repository.put(
        StoredTaste(
            user_id=USER,
            centroid=lanes,
            model_name=MODEL,
            source_watermark=None,
            title_count=7,
            computed_at=COMPUTED_AT,
        )
    )

    found = await repository.get(USER, model_name=MODEL)

    assert found is not None
    assert found.centroid is not None
    # A `list[float]`, never a `HalfVector` -- pgvector 0.8.6's
    # `HALFVEC.result_processor` returns the former, so code written for
    # `.to_list()` is an `AttributeError` at the first read. Group F hit this
    # on `genome_scores` and it is recorded there.
    assert len(found.centroid) == 384
    for expected, actual in zip(lanes, found.centroid, strict=True):
        assert actual == pytest.approx(expected, abs=1e-3)


async def test_a_bare_text_read_would_hand_the_centroid_back_as_a_string(
    session: AsyncSession,
) -> None:
    """**The `.columns()` declaration on `_GET` is load-bearing and its absence
    is silent.**

    asyncpg has no codec for a pgvector type and a `text()` construct carries
    no type information, so the read gets the extension's *text output form*:
    `tuple(row.centroid)` then yields one-character strings and raises nothing
    at all. This case pins the failure directly, so that removing the
    declaration is a red test rather than a centroid of 2,000 punctuation
    marks flowing into a cosine.
    """
    await _seed_users(session)
    await PostgresTasteRepository(session).put(
        StoredTaste(
            user_id=USER,
            centroid=_vector(0.5),
            model_name=MODEL,
            source_watermark=None,
            title_count=7,
            computed_at=COMPUTED_AT,
        )
    )

    undeclared = (
        await session.execute(
            text("SELECT centroid FROM user_taste WHERE user_id = CAST(:id AS uuid)"),
            {"id": USER},
        )
    ).one()

    assert isinstance(undeclared.centroid, str)


async def test_deleting_a_user_takes_their_centroid_with_it(session: AsyncSession) -> None:
    """`ON DELETE CASCADE`, and it is `title_embeddings`' call rather than
    `watch_states`'.

    ADR-0010 makes `watch_states.user_id` protect state a delete would destroy
    irrecoverably. A centroid is neither user state nor irrecoverable -- it is
    a mean over rows that are themselves cascading away -- so it dies with the
    user rather than blocking the delete or surviving attached to nothing.
    """
    await _seed_users(session)
    await PostgresTasteRepository(session).put(
        StoredTaste(
            user_id=USER,
            centroid=_vector(0.5),
            model_name=MODEL,
            source_watermark=None,
            title_count=7,
            computed_at=COMPUTED_AT,
        )
    )

    await session.execute(text("DELETE FROM users WHERE id = CAST(:id AS uuid)"), {"id": USER})

    remaining = (await session.execute(text("SELECT count(*) AS n FROM user_taste"))).one()
    assert remaining.n == 0


async def test_user_taste_carries_no_updated_at_trigger(session: AsyncSession) -> None:
    """One writer, one statement, which sets `computed_at` in its own
    `ON CONFLICT DO UPDATE` -- `title_embeddings`' precedent.

    Mechanically required as well as argued:
    `test_migration_creates_the_updated_at_triggers` asserts the trigger set
    *exactly*, so a trigger here would be a failing case in another file. This
    is the same fact from the side that would notice it first.
    """
    triggers = (
        await session.execute(
            text(
                "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'user_taste' AND NOT t.tgisinternal"
            )
        )
    ).all()

    assert triggers == []


async def test_the_watermark_is_the_max_updated_at_across_a_real_history(
    session: AsyncSession,
) -> None:
    """The aggregate against the real table, including the column the trigger
    owns.

    `updated_at` rather than `last_played_at` is the whole point: a re-merge
    that raises `play_count` without moving `last_played_at` is exactly the
    `completed` -> `rewatched` promotion the centroid's weights care about, and
    a `last_played_at` watermark would miss every rewatch. Seeded so the two
    columns disagree, which is what makes the choice observable.
    """
    await _seed_users(session)
    suite = TestPostgresTasteRepository()
    suite._session = session
    newest = EARLIER + timedelta(days=9)
    await suite.add_history(USER, at=EARLIER)
    await suite.add_history(USER, at=newest)
    # A third state whose *viewing* is the most recent and whose *write* is
    # the oldest: a `last_played_at` watermark would answer with this row.
    title_id = await _seed_title(session)
    await session.execute(
        text(
            "INSERT INTO watch_states "
            "(id, user_id, title_id, position_seconds, played, play_count, "
            " last_played_at, updated_at, origin) VALUES ("
            "CAST(:id AS uuid), CAST(:user_id AS uuid), CAST(:title_id AS uuid), "
            "60, true, 2, CAST(:played AS timestamptz), CAST(:written AS timestamptz), "
            "'source')"
        ),
        {
            "id": new_id(),
            "user_id": USER,
            "title_id": title_id,
            "played": datetime(2026, 8, 1, tzinfo=UTC),
            "written": EARLIER - timedelta(days=1),
        },
    )

    assert await PostgresTasteRepository(session).watermark(USER) == newest
