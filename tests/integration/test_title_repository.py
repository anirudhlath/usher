import uuid
from typing import cast

import pytest
from sqlalchemy import Table, insert
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.title_repository_contract import TitleRepositoryContract
from usher.db.models.title import TitleRow
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound


@pytest.fixture
def repo(session: AsyncSession) -> PostgresTitleRepository:
    return PostgresTitleRepository(session)


async def test_add_then_get_round_trips_the_domain_model(
    repo: PostgresTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", year=2021, tmdb_id=438631)
    await repo.add(title)
    fetched = await repo.get(title.id)
    assert fetched is not None
    assert fetched.name == "Dune"
    assert fetched.tmdb_id == 438631
    assert fetched.enrichment_state is EnrichmentState.SKELETON


async def test_add_rejects_a_duplicate_id(repo: PostgresTitleRepository) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await repo.add(title)
    with pytest.raises(RepositoryConflict):
        await repo.add(title)


async def test_get_returns_none_for_unknown_id(repo: PostgresTitleRepository) -> None:
    assert await repo.get(new_id()) is None


async def test_get_by_tmdb_id_finds_the_title(repo: PostgresTitleRepository) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
    await repo.add(title)
    found = await repo.get_by_tmdb_id(438631)
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
    ix_titles_tmdb_id is already a live unique partial index (Task 8/9,
    shipped before Task 10) -- so update() can violate it today, not just
    hypothetically. The plan's amendment claim ("nothing in its current
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
# run against the pre-fix title.py first: all four methods below raised a
# raw sqlalchemy.exc.IntegrityError.


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
    will violate ix_titles_tmdb_id whenever it's next flushed. Stands in
    for a different repository's unrelated pending write sharing this
    session: the row that eventually fails to flush has nothing to do with
    the id any method below is asked to look up."""
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
    found = await repo.get_by_tmdb_id(102)
    assert found is not None
    assert found.id == title_id


async def test_get_by_imdb_id_does_not_leak_integrity_error_from_pending_state(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    title_id = await _insert_bypassing_the_identity_map(
        session, name="Dune", sort_name="Dune", imdb_id="tt1160419", tmdb_id=103
    )
    _stage_conflicting_pending_row(session, tmdb_id=103)
    found = await repo.get_by_imdb_id("tt1160419")
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
