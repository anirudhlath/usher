import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, event, insert, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.contract.title_repository_contract import (
    TitleRepositoryCandidateContract,
    TitleRepositoryContract,
    TitleRepositoryOwnedContract,
)
from usher.db.models.source import MediaItemRow
from usher.db.models.title import TitleRow
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound


@pytest.fixture
def repo(session: AsyncSession) -> PostgresTitleRepository:
    return PostgresTitleRepository(session)


async def test_add_then_get_round_trips_the_domain_model(
    repo: PostgresTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", year=2021, tmdb_id=90000100)
    await repo.add(title)
    fetched = await repo.get(title.id)
    assert fetched is not None
    assert fetched.name == "Dune"
    assert fetched.tmdb_id == 90000100
    assert fetched.enrichment_state is EnrichmentState.SKELETON


async def test_add_rejects_a_duplicate_id(repo: PostgresTitleRepository) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await repo.add(title)
    with pytest.raises(RepositoryConflict):
        await repo.add(title)


async def test_get_returns_none_for_unknown_id(repo: PostgresTitleRepository) -> None:
    assert await repo.get(new_id()) is None


async def test_get_by_tmdb_id_finds_the_title(repo: PostgresTitleRepository) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=90000100)
    await repo.add(title)
    found = await repo.get_by_tmdb_id(90000100, TitleKind.MOVIE)
    assert found is not None and found.id == title.id


async def test_titles_without_provider_ids_are_allowed(
    repo: PostgresTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Home Video 1998", sort_name="Home Video 1998")
    await repo.add(title)
    assert (await repo.get(title.id)) is not None


async def test_update_mutates_an_existing_title(repo: PostgresTitleRepository) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await repo.add(title)
    enriched = title.evolve(enrichment_state=EnrichmentState.ENRICHED)
    await repo.update(enriched)
    fetched = await repo.get(title.id)
    assert fetched is not None
    assert fetched.enrichment_state is EnrichmentState.ENRICHED


async def test_update_rejects_an_unknown_id(repo: PostgresTitleRepository) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    with pytest.raises(RepositoryNotFound):
        await repo.update(title)


async def test_count_by_state_reports_the_catalog(repo: PostgresTitleRepository) -> None:
    for i in range(3):
        await repo.add(Title(kind=TitleKind.MOVIE, name=f"Film {i}", sort_name=f"Film {i}"))
    counts = await repo.count_by_state()
    assert counts[EnrichmentState.SKELETON] == 3
    assert counts[EnrichmentState.ENRICHED] == 0


# --- Regression coverage for the session-poisoning decision the plan's
# "Open question flagged for Group E" left open (see title.py's module
# docstring for the resolution: session.begin_nested() SAVEPOINTs around
# both add()'s and update()'s flush, not session.rollback()). The three
# tests below must keep passing -- that's the property the SAVEPOINT
# exists for. They are not hypothetical: written and run against a naive
# bare try/except flush() (no SAVEPOINT) first, they failed with exactly
# the errors their comments describe -- PendingRollbackError from
# add()/update(), and a raw, uncaught sqlalchemy.exc.IntegrityError
# escaping update() -- before the fix in title.py made them pass.


async def test_add_leaves_the_session_usable_after_a_caught_conflict(
    repo: PostgresTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await repo.add(title)
    with pytest.raises(RepositoryConflict):
        await repo.add(title)
    # Postgres aborts the whole transaction on any statement error until a
    # ROLLBACK. Without a SAVEPOINT, this next, entirely unrelated add()
    # fails with "current transaction is aborted, commands ignored until
    # end of transaction block" instead of succeeding -- a caller that
    # caught RepositoryConflict above and kept using the session (e.g. "try
    # to add, fall back to update on conflict") would see that opaque
    # error, not the RepositoryConflict that actually caused it.
    other = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival")
    await repo.add(other)
    assert await repo.get(other.id) is not None


async def test_update_translates_a_conflicting_provider_id(
    repo: PostgresTitleRepository,
) -> None:
    """update() sets tmdb_id/imdb_id/tvdb_id from the incoming title, and
    ix_titles_tmdb_id_kind is already a live unique partial index (Task 8/9,
    shipped before Task 10; widened from a single-column index to
    (tmdb_id, kind) by ADR-0011) -- so update() can violate it today, not
    just hypothetically. The plan's amendment claim ("nothing in its current
    body raises IntegrityError... it can't yet") does not hold against the
    schema as actually shipped. Left uncaught, that IntegrityError would
    escape PostgresTitleRepository -- the one thing ADR-0009 says must
    never happen, since the only way a caller could then handle it is to
    import sqlalchemy itself.
    """
    first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=1)
    second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tmdb_id=2)
    await repo.add(first)
    await repo.add(second)
    with pytest.raises(RepositoryConflict):
        await repo.update(second.evolve(tmdb_id=1))


async def test_update_leaves_the_session_usable_after_a_caught_conflict(
    repo: PostgresTitleRepository,
) -> None:
    first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=1)
    second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tmdb_id=2)
    await repo.add(first)
    await repo.add(second)
    with pytest.raises(RepositoryConflict):
        await repo.update(second.evolve(tmdb_id=1))
    other = Title(kind=TitleKind.MOVIE, name="Sicario", sort_name="Sicario")
    await repo.add(other)
    assert await repo.get(other.id) is not None


# --- Regression coverage for autoflush leaking storage exceptions past
# reads -- a different door than the session-poisoning tests above, but the
# same underlying rule: no sqlalchemy.exc type may ever escape this class.
# session.get()/session.execute() autoflush by default, so a pre-existing,
# unflushed, invalid row elsewhere on the *shared* session can make a pure
# read raise sqlalchemy.exc.IntegrityError -- get(), get_by_tmdb_id(),
# get_by_imdb_id(), and count_by_state() had no translation at all, and
# update()'s own session.get() lookup ran outside both its try and its
# SAVEPOINT. Not reachable through this repository alone today (add() and
# update() always flush before returning, so nothing of this repository's
# own making is ever left pending) -- reachable the moment a second
# repository (M4's MediaItemRepository/WatchStateRepository) shares this
# session and leaves work pending across a repository boundary. Written and
# run against the pre-fix title.py first: all five tests below failed --
# the four pure reads raised the raw sqlalchemy.exc.IntegrityError directly,
# and the update() case raised it in place of the RepositoryConflict it
# raises after the fix.


async def _insert_bypassing_the_identity_map(session: AsyncSession, **values: object) -> uuid.UUID:
    """Inserts a title through Core, not the ORM (`session.add(...)`) --
    `session.get()`'s documented shortcut ("if the given primary key
    identifier is present in the local identity map... no SQL is emitted")
    would otherwise serve the row straight out of memory and never touch
    the session's autoflush path, which would make every test below pass
    whether or not title.py's fix actually works. Standing in for a row
    some *other* session or process wrote -- M2's bulk COPY path is exactly
    this shape already (see TitleRepository's docstring) -- which is
    realistically how a caller ends up asking this session to look up an id
    it has never itself loaded.
    """
    title_id = new_id()
    # DeclarativeBase.__table__ is typed as the broader FromClause in
    # SQLAlchemy's stubs -- at runtime it is always a concrete Table for a
    # normal declarative model like this one, so the cast is safe (same
    # pattern as tests/unit/test_db_models.py).
    table = cast(Table, TitleRow.__table__)
    await session.execute(insert(table).values(id=title_id, kind=TitleKind.MOVIE, **values))
    return title_id


def _stage_conflicting_pending_row(session: AsyncSession, tmdb_id: int) -> None:
    """Adds -- without flushing -- a row under its own, unrelated id that
    will violate ix_titles_tmdb_id_kind whenever it's next flushed (always
    kind=MOVIE here, matching every caller's other row, so the composite
    index still fires). Stands in for a different repository's unrelated
    pending write sharing this session: the row that eventually fails to
    flush has nothing to do with the id any method below is asked to look
    up."""
    session.add(
        TitleRow(
            id=new_id(),
            kind=TitleKind.MOVIE,
            name="Pending Dup",
            sort_name="Pending Dup",
            tmdb_id=tmdb_id,
        )
    )


async def test_get_does_not_leak_integrity_error_from_pending_state(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    title_id = await _insert_bypassing_the_identity_map(
        session, name="Dune", sort_name="Dune", tmdb_id=101
    )
    _stage_conflicting_pending_row(session, tmdb_id=101)
    fetched = await repo.get(title_id)
    assert fetched is not None
    assert fetched.name == "Dune"


async def test_get_by_tmdb_id_does_not_leak_integrity_error_from_pending_state(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    title_id = await _insert_bypassing_the_identity_map(
        session, name="Dune", sort_name="Dune", tmdb_id=102
    )
    _stage_conflicting_pending_row(session, tmdb_id=102)
    found = await repo.get_by_tmdb_id(102, TitleKind.MOVIE)
    assert found is not None
    assert found.id == title_id


async def test_get_by_imdb_id_does_not_leak_integrity_error_from_pending_state(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    title_id = await _insert_bypassing_the_identity_map(
        session, name="Dune", sort_name="Dune", imdb_id="tt99000100", tmdb_id=103
    )
    _stage_conflicting_pending_row(session, tmdb_id=103)
    found = await repo.get_by_imdb_id("tt99000100")
    assert found is not None
    assert found.id == title_id


async def test_count_by_state_does_not_leak_integrity_error_from_pending_state(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    await _insert_bypassing_the_identity_map(session, name="Dune", sort_name="Dune", tmdb_id=104)
    _stage_conflicting_pending_row(session, tmdb_id=104)
    counts = await repo.count_by_state()
    assert counts[EnrichmentState.SKELETON] == 1


async def test_update_translates_integrity_error_from_its_own_lookup(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """update()'s session.get() used to run outside the try -- so an
    autoflush it triggered of unrelated pending state raised a raw
    IntegrityError instead of the RepositoryConflict every other failure in
    this method produces."""
    title_id = await _insert_bypassing_the_identity_map(
        session, name="Dune", sort_name="Dune", tmdb_id=105
    )
    _stage_conflicting_pending_row(session, tmdb_id=105)
    incoming = Title(
        id=title_id, kind=TitleKind.MOVIE, name="Dune 2", sort_name="Dune 2", tmdb_id=105
    )
    with pytest.raises(RepositoryConflict):
        await repo.update(incoming)


# --- Regression coverage for update() rewriting unchanged ARRAY columns --
# see tests/unit/test_title_repository.py's
# test_to_row_emits_lists_not_tuples_for_array_columns for the necessary-
# but-not-sufficient type-level pin (no Postgres needed); this is the
# end-to-end proof against real SQLAlchemy unit-of-work.
#
# updated_at cannot be used to detect this: the fixture wraps each test in
# one Postgres transaction (see conftest.py), and Postgres's now() /
# CURRENT_TIMESTAMP is *transaction*-scoped, not statement-scoped -- every
# now() call inside one transaction returns the same value, so updated_at
# looks identical before and after *any* number of updates within a single
# test regardless of whether this bug is fixed. Counting the actual SQL
# statements SQLAlchemy sends is the direct, transaction-timing-independent
# way to prove no UPDATE was issued at all.


async def test_update_does_not_rewrite_unchanged_columns(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """`_to_row` used to emit tuples for the four ARRAY(Text) columns while
    a loaded row always holds lists on read (see title.py's module
    docstring) -- `("a",) != ["a"]` in Python regardless of contents, so
    SQLAlchemy's attribute-history comparison always saw those four columns
    as changed, and update() rewrote them on *every* call, even a call that
    changes nothing at all. That would confound any "changed since?" logic
    M4 builds on updated_at once it reflects real writes (see
    test_migrations.py).
    """
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        genres=("Sci-Fi", "Adventure"),
        keywords=("desert",),
        spoken_languages=("en",),
        origin_countries=("US",),
    )
    await repo.add(title)
    fetched = await repo.get(title.id)
    assert fetched is not None

    statements: list[str] = []

    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    sync_conn = cast(AsyncConnection, session.bind).sync_connection
    assert sync_conn is not None
    event.listen(sync_conn, "before_cursor_execute", _capture)
    try:
        await repo.update(fetched)  # same data just read back -- a true no-op
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)

    assert not any(statement.strip().upper().startswith("UPDATE") for statement in statements), (
        f"update() issued an UPDATE for a no-op call: {statements}"
    )


class TestPostgresTitleRepositoryContract(TitleRepositoryContract):
    """Same shared assertions as tests/unit/test_title_repository_contract.py
    (FakeTitleRepository), now against a real PostgreSQL -- see
    tests/contract/title_repository_contract.py's module docstring. This is
    what actually proves the fake and PostgresTitleRepository agree, rather
    than merely asserting each looks right in isolation.
    """

    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresTitleRepository:
        return PostgresTitleRepository(session)

    @pytest.fixture
    async def collection_id(self, session: AsyncSession) -> uuid.UUID:
        """A real `collections` row, because M7 gave `titles.collection_id`
        a real foreign key (`fd7c3a5b9e12`).

        The contract's default is a bare `new_id()`, which the fake accepts
        because it is a dict and Postgres refuses with a
        `ForeignKeyViolationError` -- so this override is what keeps the
        round-trip case covering the column instead of dropping it. Written
        with raw SQL rather than through a repository because
        `CollectionRepository` is a different port and this file is about
        `TitleRepository`.
        """
        collection_id = new_id()
        await session.execute(
            text(
                "INSERT INTO collections (id, tmdb_id, name) "
                "VALUES (CAST(:id AS uuid), 98000300, 'An Invented Collection')"
            ),
            {"id": collection_id},
        )
        return collection_id


class TestPostgresTitleRepositoryOwned(TitleRepositoryOwnedContract):
    """`list_owned_by_tag` against real Postgres.

    The half with teeth: `@>` on `text[]`, `NULLS LAST` under a descending
    sort, and the `EXISTS` semi-join with no `episode_id IS NULL` bound are
    all Postgres behaviours the fake reproduces in Python and could
    reproduce wrongly. Group E's `ff_row_read_indexes` also names this read
    by name -- *"if either of `GenreAffinityProvider`'s two statements shows
    a `Seq Scan on titles`, that is a finding against the provider's shape"*
    -- which is a claim about a statement that has to exist to be checked.
    """

    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresTitleRepository:
        return PostgresTitleRepository(session)

    @pytest_asyncio.fixture
    async def owning_source_id(self, session: AsyncSession) -> uuid.UUID:
        source = Source(
            kind=SourceKind.EMBY,
            name=f"Owned Contract Source {new_id()}",
            base_url="https://emby.invalid",
            credentials_ref=f"ref-{new_id()}",
            device_id=str(new_id()),
        )
        await PostgresSourceRepository(session).add(source)
        return source.id

    @pytest.fixture
    def own(
        self, session: AsyncSession, owning_source_id: uuid.UUID
    ) -> Callable[..., Awaitable[None]]:
        async def _own(title_id: uuid.UUID, *, episode: bool = False) -> None:
            # A real `media_items` row rather than a flag, because the whole
            # point of the read is the semi-join. `episode_id` is left NULL
            # even for the episode case: `episodes` needs a `seasons` row and
            # a `titles` row and none of that changes what this statement
            # sees, which is that the title has an available copy. What the
            # episode case must *not* do is write a title-level row where the
            # implementation under test would demand one -- so it writes a row
            # that a `episode_id IS NULL` bound would still accept, and the
            # divergence is pinned in the fake's half where it is expressible
            # without three parent rows.
            await session.execute(
                insert(cast(Table, MediaItemRow.__table__)).values(
                    id=new_id(),
                    source_id=owning_source_id,
                    external_id=str(new_id()),
                    title_id=title_id,
                    episode_id=None,
                    available=True,
                    last_seen_at=datetime.now(UTC),
                )
            )

        return _own


class TestPostgresTitleRepositoryCandidates(TitleRepositoryCandidateContract):
    """`list_unwatched_candidates` against real Postgres, which is where its
    three Postgres-shaped halves can fail.

    The `NOT EXISTS` roll-up through `episodes.title_id` is the one that
    matters: the fake reproduces it as a dict lookup, which is naturally the
    right shape, and only a real `LEFT JOIN episodes` can be written the wrong
    way round. The other two are `NULLS LAST` under a descending sort -- whose
    default is the opposite of what the read wants -- and the `&&` operator on
    a generic `ARRAY(Text)` column, which `list_owned_by_tag` already records
    raising `NotImplementedError` for its sibling `@>`.

    Real `users` rows too: `watch_states.user_id` is a foreign key, so the
    fake's bare ids would be a different test.
    """

    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresTitleRepository:
        return PostgresTitleRepository(session)

    @pytest_asyncio.fixture
    async def owning_source_id(self, session: AsyncSession) -> uuid.UUID:
        source = Source(
            kind=SourceKind.EMBY,
            name=f"Candidate Contract Source {new_id()}",
            base_url="https://emby.invalid",
            credentials_ref=f"ref-{new_id()}",
            device_id=str(new_id()),
        )
        await PostgresSourceRepository(session).add(source)
        return source.id

    @pytest_asyncio.fixture
    async def user_id(self, session: AsyncSession) -> uuid.UUID:
        return await _add_user(session)

    @pytest_asyncio.fixture
    async def other_user_id(self, session: AsyncSession) -> uuid.UUID:
        """A second household member, so the read's `user_id` predicate has
        something to exclude. On a single-household deployment a lost
        `WHERE user_id` is invisible."""
        return await _add_user(session)

    @pytest.fixture
    def own(
        self, session: AsyncSession, owning_source_id: uuid.UUID
    ) -> Callable[..., Awaitable[None]]:
        async def _own(
            title_id: uuid.UUID, *, episode: bool = False, available: bool = True
        ) -> None:
            # **`episode=True` writes a real `episode_id`, and
            # `TitleRepositoryOwnedContract.own` deliberately does not.** That
            # fixture leaves it NULL because `episodes` needs a `seasons` row
            # and a `titles` row and it has no helper for either; this class
            # does, so the excuse does not transfer -- and copying it made the
            # case vacuous. Measured: with `episode_id` left NULL, adding
            # `MediaItemRow.episode_id.is_(None)` to the ownership subquery
            # gives **12 passed, 0 failed** here, so the bound the case exists
            # to rule out was unobservable on the only arm that has it.
            #
            # Both ids together is also the production shape rather than a
            # test convenience: `ports/ingest.py`'s `MediaItemTarget` records
            # that an episode's row holds **both**, because `IngestService`
            # writes `title_id` (the series' canonical title) alongside
            # `episode_id` for a client browsing a season. So a semi-join
            # carrying `episode_id IS NULL` reports every series in a real
            # library as unowned, which on 999,827 episodes of 1,126,674 items
            # is most of it.
            #
            # `available=False` writes a real retracted row -- what
            # `mark_unseen_unavailable` leaves behind -- which the fake cannot
            # express and which is the only way the read's own `available`
            # predicate is observable at all.
            await session.execute(
                insert(cast(Table, MediaItemRow.__table__)).values(
                    id=new_id(),
                    source_id=owning_source_id,
                    external_id=str(new_id()),
                    title_id=title_id,
                    episode_id=await _add_episode(session, title_id) if episode else None,
                    available=available,
                    last_seen_at=datetime.now(UTC),
                )
            )

        return _own

    @pytest.fixture
    def watch(self, session: AsyncSession) -> Callable[..., Awaitable[None]]:
        async def _watch(
            user_id: uuid.UUID,
            *,
            title_id: uuid.UUID | None = None,
            episode_id: uuid.UUID | None = None,
            played: bool = True,
        ) -> None:
            # Raw, rather than through `merge_from_source`: that path is a
            # two-statement upsert with its own dedup and its own conflict
            # rule, and a fixture that went through it would be testing that
            # instead. `ck_watch_states_exactly_one_target` still applies,
            # which is what makes a case naming neither target impossible to
            # write by accident.
            await session.execute(
                text(
                    "INSERT INTO watch_states "
                    "  (id, user_id, title_id, episode_id, position_seconds, played, origin) "
                    "VALUES (CAST(:id AS uuid), CAST(:user_id AS uuid), "
                    "        CAST(:title_id AS uuid), CAST(:episode_id AS uuid), "
                    "        :position_seconds, :played, 'source')"
                ),
                {
                    "id": new_id(),
                    "user_id": user_id,
                    "title_id": title_id,
                    "episode_id": episode_id,
                    # A real position on the abandoned case, so "has a state"
                    # and "played" are two different rows rather than two
                    # readings of one blank one.
                    "position_seconds": 0 if played else 720,
                    "played": played,
                },
            )

        return _watch

    @pytest.fixture
    def episode_of(self, session: AsyncSession) -> Callable[[uuid.UUID], Awaitable[uuid.UUID]]:
        async def _episode_of(series_id: uuid.UUID) -> uuid.UUID:
            return await _add_episode(session, series_id)

        return _episode_of


async def _add_user(session: AsyncSession) -> uuid.UUID:
    identifier = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (CAST(:id AS uuid), :name)"),
        {"id": identifier, "name": f"viewer-{identifier}"},
    )
    return identifier


async def _add_episode(session: AsyncSession, series_id: uuid.UUID) -> uuid.UUID:
    """One real episode of `series_id`'s season 1, minting the season once.

    `episodes.season_id` and `episodes.title_id` are both NOT NULL with
    `ON DELETE CASCADE`, so neither can be invented -- the season is what makes
    the watched roll-up a real two-table join rather than a self-join on a
    column that happens to be there.

    **The season is reused rather than re-inserted**, because
    `uq_seasons_title_season_number` refuses a second season 1 for one title
    and both callers here -- `own(episode=True)` and `episode_of` -- can reach
    the same series in one case. `episode_number` counts the rows already
    present for the same reason: `uq_episodes_season_episode_number` refuses a
    duplicate, and a fixture that raised on its second call would fail the case
    for a reason no implementation could cause.
    """
    season_id = (
        await session.execute(
            text("SELECT id FROM seasons WHERE title_id = CAST(:t AS uuid) AND season_number = 1"),
            {"t": series_id},
        )
    ).scalar_one_or_none()
    if season_id is None:
        season_id = new_id()
        await session.execute(
            text(
                "INSERT INTO seasons (id, title_id, season_number) "
                "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), 1)"
            ),
            {"id": season_id, "title_id": series_id},
        )
    number = (
        await session.execute(
            text("SELECT count(*) FROM episodes WHERE season_id = CAST(:s AS uuid)"),
            {"s": season_id},
        )
    ).scalar_one() + 1
    episode_id = new_id()
    await session.execute(
        text(
            "INSERT INTO episodes "
            "  (id, title_id, season_id, season_number, episode_number) "
            "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), "
            "        CAST(:season_id AS uuid), 1, :number)"
        ),
        {"id": episode_id, "title_id": series_id, "season_id": season_id, "number": number},
    )
    return episode_id
