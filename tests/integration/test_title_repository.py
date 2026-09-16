import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import ColumnElement, Select, Table, event, insert, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import InvalidRequestError, MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.contract.title_repository_contract import (
    TitleRepositoryBrowseContract,
    TitleRepositoryCandidateContract,
    TitleRepositoryContract,
    TitleRepositoryGenreSweepContract,
    TitleRepositoryNaturalKeyContract,
    TitleRepositoryOwnedContract,
)
from usher.db.models.source import MediaItemRow
from usher.db.models.title import DERIVED_COLUMNS, TitleRow
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import (
    _RESOLVE_NATURAL_KEYS,
    _WITHOUT_DERIVED_COLUMNS,
    PostgresTitleRepository,
    _browse_order,
)
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import BrowseSort, TitleReference


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


# --- Regression coverage for session poisoning: `session.begin_nested()`
# SAVEPOINTs around both `add()`'s and `update()`'s flush, never
# `session.rollback()` (see title.py's module docstring).


async def test_add_leaves_the_session_usable_after_a_caught_conflict(
    repo: PostgresTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await repo.add(title)
    with pytest.raises(RepositoryConflict):
        await repo.add(title)
    # Postgres aborts the whole transaction on any statement error until a ROLLBACK.
    other = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival")
    await repo.add(other)
    assert await repo.get(other.id) is not None


async def test_update_translates_a_conflicting_provider_id(
    repo: PostgresTitleRepository,
) -> None:
    """update() sets tmdb_id/imdb_id/tvdb_id from the incoming title.

    ix_titles_tmdb_id_kind is a live unique partial index over (tmdb_id, kind), so
    update() can violate it today rather than hypothetically. Left uncaught, that
    IntegrityError would escape PostgresTitleRepository, and the only way a caller could
    then handle it would be to import sqlalchemy itself.
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


async def test_a_caught_conflict_leaves_an_expired_row_and_every_read_refreshes_it(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """The case above reads back a *different* title.

    This one reads back the title the conflict was about, which is the one the SAVEPOINT
    rollback leaves behind.
    """
    first = Title(
        kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=90000201, imdb_id="tt99000201"
    )
    second = Title(
        kind=TitleKind.MOVIE,
        name="Arrival",
        sort_name="Arrival",
        tmdb_id=90000202,
        imdb_id="tt99000202",
    )
    await repo.add(first)
    await repo.add(second)
    with pytest.raises(RepositoryConflict) as conflict:
        await repo.update(second.evolve(imdb_id="tt99000201"))
    assert conflict.value.constraint == "ix_titles_imdb_id"

    # The premise: the conflicted row really is still here, and really is
    # expired. Without this the reads below prove nothing.
    held: list[TitleRow] = [
        row
        for row in session.identity_map.values()
        if isinstance(row, TitleRow) and cast(Any, sa_inspect(row)).identity == (second.id,)
    ]
    assert held, "the SAVEPOINT rollback left no row to be about"
    poisoned = held[0]
    assert cast(Any, sa_inspect(poisoned)).expired is True

    # The hazard, exercised rather than described.
    with pytest.raises(MissingGreenlet):
        # The attribute access *is* the assertion -- an f-string because that is
        # the shape a log line or an exception message reaches it in.
        assert f"{poisoned.name}"

    # And every shipped read refreshes it inside its own `await`.
    assert (await repo.get(second.id)) is not None
    assert (await repo.get_by_tmdb_id(90000202, TitleKind.MOVIE)) is not None
    assert (await repo.get_by_imdb_id("tt99000202")) is not None
    assert len(await repo.list_by_ids([second.id])) == 1
    assert sum((await repo.count_by_state()).values()) == 2


# --- Regression coverage for autoflush leaking storage exceptions past reads -- a
# different door than the session-poisoning tests above, but the same underlying rule:
# no sqlalchemy.exc type may ever escape this class.


async def _insert_bypassing_the_identity_map(session: AsyncSession, **values: object) -> uuid.UUID:
    """Inserts a title through Core, not the ORM (`session.add(...)`).

    `session.get()` serves a row present in the local identity map with no SQL at all,
    never touching the autoflush path these cases are about. Standing in for a row some
    *other* session or process wrote -- the bulk COPY path is exactly this shape -- which
    is realistically how a caller ends up asking this session for an id it has never
    itself loaded.
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
    """Adds, without flushing, a row that violates ix_titles_tmdb_id_kind when it does.

    Under its own unrelated id, always kind=MOVIE so the composite index still fires.
    Stands in for a different repository's unrelated pending write sharing this session:
    the row that eventually fails to flush has nothing to do with the id any method
    below is asked to look up.
    """
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
    """update()'s session.get() has to run inside the try.

    An autoflush it triggers of unrelated pending state would otherwise raise a raw
    IntegrityError instead of the RepositoryConflict every other failure in this method
    produces.
    """
    title_id = await _insert_bypassing_the_identity_map(
        session, name="Dune", sort_name="Dune", tmdb_id=105
    )
    _stage_conflicting_pending_row(session, tmdb_id=105)
    incoming = Title(
        id=title_id, kind=TitleKind.MOVIE, name="Dune 2", sort_name="Dune 2", tmdb_id=105
    )
    with pytest.raises(RepositoryConflict):
        await repo.update(incoming)


@contextmanager
def _capturing_sql(session: AsyncSession) -> Iterator[list[str]]:
    """Every statement this session's connection actually sends, verbatim.

    `before_cursor_execute` is the only place the *emitted* text is visible:
    what a repository builds is a SQLAlchemy construct, and the two are not the
    same claim -- a `defer()` that never reached the statement, or a projection
    that widened, is invisible from the construct's own API and plain in the
    string. Shared by the three cases below rather than re-declared per case.
    """
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
        yield statements
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)


def _entity_reads_of_titles(statements: Sequence[str]) -> list[str]:
    """The captured statements that read `titles` as an *entity*.

    That is, those projecting the wide column list `_to_domain` consumes. A filter
    rather than "the only statement", because a session flush or a fixture's own write
    can share the capture window, and a case that indexed `statements[0]` would silently
    start asserting about whichever statement arrived first.
    """
    return [
        statement
        for statement in statements
        if statement.lstrip().startswith("SELECT") and "titles.overview" in statement
    ]


def _projections_over_titles(statement: str) -> list[str]:
    """Every `SELECT <projection> FROM titles` stage, in the order they appear.

    Only stages reading `titles` itself: the ownership subquery selects from
    `media_items` and the exclusion from `watch_states`, so neither is matched
    and the count is the number of times this statement projects the catalog.
    """
    return re.findall(r"SELECT (.+?)\s*\nFROM titles", statement, flags=re.S)


#: Columns no consumer of these three reads touches and every one of them used
#: to carry. `CandidatePoolService` renders `name`, `year` and `genres` into a
#: prompt and keeps `id`; `SearchService` re-orders `list_by_ids`' answer by its
#: own ranking. Named individually rather than as "everything but four" so that
#: a column added to `titles` does not silently join the list.
_COLUMNS_NO_CONSUMER_READS = (
    "overview",
    "tagline",
    "keywords",
    "field_provenance",
    "origin_countries",
    "enrichment_error",
)


async def test_no_entity_read_ships_credit_names_over_the_wire(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """`_to_domain` drops `credit_names` from every row it builds.

    It is in `DERIVED_COLUMNS`, so selecting it means Postgres detoasts up to ten cast
    names per title, serialises them and puts them on the wire for nothing.
    """
    title = Title(
        kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", genres=("Sci-Fi",), year=2021
    )
    await repo.add(title)

    with _capturing_sql(session) as statements:
        await repo.list_by_ids([title.id])
        await repo.list_owned_by_tag(genre="Sci-Fi", limit=20)
        await repo.list_unwatched_candidates(new_id(), genres=("Sci-Fi",), limit=200)

    reads = _entity_reads_of_titles(statements)
    assert len(reads) == 3, (
        "the three entity reads did not all reach the wire, so this case would "
        f"pass on a statement it never saw: {statements}"
    )
    assert "credit_names" in DERIVED_COLUMNS, (
        "the premise: this case is a loop over DERIVED_COLUMNS, so it says "
        "nothing about the column it was written for unless that column is in it"
    )
    for read in reads:
        for column in DERIVED_COLUMNS:
            assert f"titles.{column}" not in read, (
                f"an entity read still selects the derived column {column} and "
                f"drops it in `_to_domain`: {read}"
            )


async def test_an_unloaded_derived_column_refuses_by_name_rather_than_by_greenlet(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """`raiseload` makes the refusal name the attribute rather than a greenlet."""
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        tmdb_id=90000401,
        imdb_id="tt99000401",
    )
    await repo.add(title)
    await session.execute(
        text("UPDATE titles SET credit_names = ARRAY['Timothee Chalamet'] WHERE id = :id"),
        {"id": title.id},
    )
    session.expunge_all()

    loaded = await session.execute(
        select(TitleRow).options(*_WITHOUT_DERIVED_COLUMNS).where(TitleRow.id == title.id)
    )
    row = loaded.scalar_one()
    assert "credit_names" in cast(Any, sa_inspect(row)).unloaded, (
        "the premise: `credit_names` is deferred, so this case says nothing about "
        "the refusal unless the attribute is actually unloaded"
    )

    # Not `MissingGreenlet`. `raiseload=True` decides the read is a bug before
    # SQLAlchemy tries to run it, so the message names `TitleRow.credit_names`
    # instead of naming a greenlet the reader has never heard of.
    with pytest.raises(InvalidRequestError) as refusal:
        assert f"{row.credit_names}"
    assert not isinstance(refusal.value, MissingGreenlet), (
        "a plain `defer()` here answers a mis-routed read with the same undiagnosable "
        f"error issue #8 is about: {refusal.value}"
    )
    assert "credit_names" in str(refusal.value), (
        f"the refusal does not name the attribute that was read: {refusal.value}"
    )

    # The control: the sanctioned reader is a column read and is untouched by
    # what an entity load's options say.
    assert await repo.credit_names_for([title.id]) == {title.id: ("Timothee Chalamet",)}


async def test_the_candidate_pool_ranks_on_a_narrow_projection(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """The sort is over the whole catalog, so the projection entering it must be narrow.

    `list_unwatched_candidates` outer-joins `titles` to a `DISTINCT` over `media_items`,
    anti-joins `watch_states`, sorts on four keys and keeps 200 -- every row entering
    that sort carrying all thirty-one columns, `overview` included, is the defect.
    """
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await repo.add(title)

    with _capturing_sql(session) as statements:
        rows = await repo.list_unwatched_candidates(new_id(), genres=("Western",), limit=200)

    assert [row.id for row in rows] == [title.id], "the premise: the read answered at all"
    reads = _entity_reads_of_titles(statements)
    assert len(reads) == 1, f"expected exactly one entity read to capture: {statements}"
    statement = reads[0]

    projections = _projections_over_titles(statement)
    assert len(projections) == 2, (
        "the catalog is projected once, so the sort is still carrying the whole "
        f"entity: {statement}"
    )
    outer, ranking = projections
    assert "titles.overview" in outer, (
        "the premise: the outer stage is the entity read, so `ranking` below is "
        f"the stage the LIMIT applies to: {statement}"
    )
    for column in _COLUMNS_NO_CONSUMER_READS:
        assert f"titles.{column}" not in ranking, (
            f"the ranking stage still drags titles.{column} through the sort: {ranking}"
        )
    assert statement.rindex("ORDER BY") > statement.rindex("LIMIT"), (
        "nothing re-orders the rows the LIMIT kept, so the answer's order is "
        f"whatever the join emitted: {statement}"
    )


# --- Regression coverage for update() rewriting unchanged ARRAY columns -- see
# tests/unit/test_title_repository.py's
# test_to_row_emits_lists_not_tuples_for_array_columns for the necessary-but-not-
# sufficient type-level pin (no Postgres needed); this is the end-to-end proof against
# real SQLAlchemy unit-of-work.


async def test_update_does_not_rewrite_unchanged_columns(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """`_to_row` emits lists, not tuples, for the four ARRAY(Text) columns.

    A loaded row always holds lists on read (see title.py's module docstring) and
    `("a",) != ["a"]` in Python regardless of contents, so tuples would make SQLAlchemy's
    attribute-history comparison see those four columns as changed on *every* call, even
    one that changes nothing -- confounding any "changed since?" logic built on
    updated_at.
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

    with _capturing_sql(session) as statements:
        await repo.update(fetched)  # same data just read back -- a true no-op

    assert not any(statement.strip().upper().startswith("UPDATE") for statement in statements), (
        f"update() issued an UPDATE for a no-op call: {statements}"
    )


class TestPostgresTitleRepositoryContract(TitleRepositoryContract):
    """The shared contract assertions, now against a real PostgreSQL.

    tests/unit/test_title_repository_contract.py runs them against FakeTitleRepository;
    this is what proves the fake and PostgresTitleRepository agree, rather than merely
    asserting each looks right in isolation.
    """

    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresTitleRepository:
        return PostgresTitleRepository(session)

    @pytest.fixture
    async def collection_id(self, session: AsyncSession) -> uuid.UUID:
        """A real `collections` row, because `titles.collection_id` has a foreign key.

        The contract's default is a bare `new_id()`, which the fake accepts because it
        is a dict and Postgres refuses with a `ForeignKeyViolationError` -- so this
        override is what keeps the round-trip case covering the column instead of
        dropping it. Written with raw SQL rather than through a repository because
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
    reproduce wrongly. `ff_row_read_indexes` names this read too
    -- *"if either of `GenreAffinityProvider`'s two statements shows
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
            # A real `media_items` row rather than a flag, because the whole point of
            # the read is the semi-join.
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
    """`list_unwatched_candidates` against real Postgres, where three halves can fail.

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
        """A second household member, so the read's `user_id` predicate has something to exclude.

        On a single-household deployment a lost `WHERE user_id` is invisible.
        """
        return await _add_user(session)

    @pytest.fixture
    def own(
        self, session: AsyncSession, owning_source_id: uuid.UUID
    ) -> Callable[..., Awaitable[None]]:
        async def _own(
            title_id: uuid.UUID, *, episode: bool = False, available: bool = True
        ) -> None:
            # **`episode=True` writes a real `episode_id`, and
            # `TitleRepositoryOwnedContract.own` deliberately does not.** That fixture
            # leaves it NULL because `episodes` needs a `seasons` row and a `titles` row
            # and it has no helper for either; this class does, so the excuse does not
            # transfer -- and copying it made the case vacuous.
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
            # Raw, rather than through `merge_from_source`: that path is a two-statement
            # upsert with its own dedup and its own conflict rule, and a fixture that
            # went through it would be testing that instead.
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


class TestPostgresTitleRepositoryBrowse(TitleRepositoryBrowseContract):
    """`browse`/`browse_facets` against real Postgres, where four halves can fail.

    The keyset's NULL branch is the one that matters: the natural
    `ROW(...) > ROW(...)` spelling answers **NULL** rather than false for an
    unkeyed boundary, which Python's `None` comparison cannot reproduce
    (it raises instead). `NULLS LAST` under a `DESC` sort is the opposite of
    Postgres's own default; `@>` on a generic `ARRAY(Text)` is the operator
    `list_owned_by_tag` already records raising `NotImplementedError` through
    SQLAlchemy's helper; and the genre facet's `unnest` has no Python
    counterpart at all.

    And this is the arm where `available = false` is a real row rather than an
    absence, which is the only way browse's `available` predicate is
    observable.
    """

    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresTitleRepository:
        return PostgresTitleRepository(session)

    @pytest_asyncio.fixture
    async def owning_source_id(self, session: AsyncSession) -> uuid.UUID:
        source = Source(
            kind=SourceKind.EMBY,
            name=f"Browse Contract Source {new_id()}",
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
        async def _own(
            title_id: uuid.UUID, *, episode: bool = False, available: bool = True
        ) -> None:
            # `episode=True` writes **both** ids, which is the production shape
            # (`ports/ingest.py`'s `MediaItemTarget`) and the only row that can tell
            # browse's `episode_id IS NULL` bound apart from `list_owned_by_tag`'s
            # deliberate absence of one.
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


async def _browse_by_offset(
    session: AsyncSession, *, limit: int, offset: int
) -> list[tuple[uuid.UUID, str]]:
    """`browse(sort=name)`'s page, spelled the way PRD 07 refuses.

    Raw SQL and not a second implementation on the repository: the offset
    spelling exists to be **compared against**, and putting it behind the port
    would be shipping the thing the port is defined not to do. Same `ORDER BY`
    as `PostgresTitleRepository.browse`'s `name` sort, so the only difference
    between the two arms below is how page 2 finds its start.
    """
    rows = await session.execute(
        text(
            "SELECT id, sort_name FROM titles "
            "ORDER BY (sort_name IS NOT NULL) DESC, sort_name ASC, id ASC "
            "LIMIT :limit OFFSET :offset"
        ),
        {"limit": limit, "offset": offset},
    )
    return [(row[0], row[1]) for row in rows.all()]


async def test_offset_duplicates_a_row_a_concurrent_insert_pushed_down_and_the_keyset_does_not(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """PRD 07's own reason for refusing offset paging, exercised rather than asserted."""
    seeded = [
        Title(kind=TitleKind.MOVIE, name=name, sort_name=name.lower())
        for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
    ]
    for one in seeded:
        await repo.add(one)

    keyset_first = await repo.browse(sort=BrowseSort.NAME, limit=3)
    offset_first = await _browse_by_offset(session, limit=3, offset=0)
    assert [one.name for one in keyset_first] == ["Alpha", "Bravo", "Charlie"]
    assert [row[0] for row in offset_first] == [one.id for one in keyset_first], (
        "the premise: the two spellings agree on page 1, so any disagreement "
        "below is about how page 2 resumes and nothing else"
    )

    inserted = Title(kind=TitleKind.MOVIE, name="Bravissimo", sort_name="bravissimo")
    await repo.add(inserted)
    boundary = BrowseSort.position_of(keyset_first[-1], sort=BrowseSort.NAME)
    assert isinstance(boundary.key, str) and inserted.sort_name < boundary.key, (
        "the premise: the new row sorts *before* the cursor, i.e. into the page "
        "the client has already been served -- which is what makes every later "
        "row's offset one larger than it was"
    )

    keyset_second = await repo.browse(sort=BrowseSort.NAME, after=boundary, limit=3)
    offset_second = await _browse_by_offset(session, limit=3, offset=3)

    keyset_served = [one.id for one in keyset_first] + [one.id for one in keyset_second]
    assert keyset_served == [one.id for one in seeded], (
        "the keyset serves the pre-insert population once, in order: "
        f"{[one.name for one in keyset_first + keyset_second]}"
    )

    offset_served = [row[0] for row in offset_first] + [row[0] for row in offset_second]
    repeated = {name for _, name in offset_first} & {name for _, name in offset_second}
    assert repeated == {"charlie"}, (
        "the refutation this case exists for: under `OFFSET 3` the row that was "
        "last on page 1 is first on page 2, because the insert pushed it down. "
        f"Page 1 {[name for _, name in offset_first]}, page 2 "
        f"{[name for _, name in offset_second]}"
    )
    assert len(offset_served) != len(set(offset_served)), "a duplicate, spelled as one"
    assert len(keyset_served) == len(set(keyset_served)), (
        "and the keyset over the identical two requests and the identical "
        "concurrent write has none, which is the comparison and not a second "
        "reading of the assertion four lines up"
    )


#: A browse population carrying, for **every** member of `BrowseSort`, at least
#: one tie and -- where the column is nullable -- at least two NULLs.
_EQUIVALENCE_POPULATION: tuple[tuple[str, str, int | None, float | None, int | None], ...] = (
    # name, sort_name, year, popularity, vote_count
    ("Delta", "delta", 1999, 3.0, 40),
    ("Alpha", "alpha", None, None, None),
    ("Foxtrot", "foxtrot", 2010, None, 5),
    ("Bravo", "bravo", None, 9.0, None),
    ("Echo", "echo", 1999, 1.0, 900),
    ("Charlie", "charlie", 2010, None, None),
    # One row carrying a tie for **every** key at once -- `sort_name` with
    # Delta, `popularity` and `vote_count` with Delta, `year` alone (1999 and
    # 2010 already repeat). Without it three of the four sorts have no tie and
    # their `id` tail is unobservable, which the premise guard below catches.
    ("Delta II", "delta", 1985, 3.0, 40),
)


async def _seed_equivalence_population(repo: PostgresTitleRepository) -> list[Title]:
    seeded = []
    for name, sort_name, year, popularity, vote_count in _EQUIVALENCE_POPULATION:
        one = Title(
            kind=TitleKind.MOVIE,
            name=name,
            sort_name=sort_name,
            year=year,
            tmdb_popularity=popularity,
            tmdb_vote_count=vote_count,
        )
        await repo.add(one)
        seeded.append(one)
    return seeded


@pytest.mark.parametrize("sort", list(BrowseSort))
async def test_the_shipped_order_is_byte_identical_to_the_written_out_one(
    repo: PostgresTitleRepository, session: AsyncSession, sort: BrowseSort
) -> None:
    """The shipped order is byte-identical to the written-out spelling."""
    seeded = await _seed_equivalence_population(repo)
    column, descending = BrowseSort.order_for(sort)
    values = [getattr(one, column) for one in seeded]
    keyed = [value for value in values if value is not None]
    assert len(keyed) - len(set(keyed)) >= 1, (
        f"the premise: {sort} needs a tie, or the `id` tail cannot be observed"
    )
    if len(keyed) < len(values):
        assert len(values) - len(keyed) >= 2, (
            f"the premise: {sort} needs two NULLs, or the unkeyed group has no "
            "internal order to get wrong"
        )

    key = getattr(TitleRow, column)
    reference = await session.execute(
        select(TitleRow.id).order_by(
            # The spelling this replaced, kept here on purpose: it is the
            # order the fast one has to reproduce, and freezing it as a
            # literal is what makes "identical" checkable rather than assumed.
            key.is_not(None).desc(),
            key.desc() if descending else key.asc(),
            TitleRow.id.asc(),
        )
    )
    expected = list(reference.scalars().all())
    assert len(expected) == len(_EQUIVALENCE_POPULATION), "the premise: the fixture is all there"
    assert expected != sorted(expected), (
        "the premise: this sort's answer is not id order, or the comparison is "
        "satisfied by two implementations that both ignore the sort key"
    )

    unpaged = await repo.browse(sort=sort, limit=len(expected) + 5)
    walked = await TitleRepositoryBrowseContract._walk(repo, sort=sort, limit=2)

    mismatched = [
        index
        for index, (shipped, written) in enumerate(
            zip([one.id for one in unpaged], expected, strict=True)
        )
        if shipped != written
    ]
    assert not mismatched, f"{len(mismatched)} mismatched positions of {len(expected)}"
    assert [one.id for one in unpaged] == expected
    assert [one.id for one in walked] == expected, (
        "and the keyset walk too: `_browse_after` was deliberately not touched, "
        "so this is what says it still agrees with a clause that no longer "
        "reads like it"
    )


async def _plan_of(session: AsyncSession, statement: Select[tuple[uuid.UUID]]) -> dict[str, object]:
    """`EXPLAIN (FORMAT JSON)`'s plan tree for a statement, as a dict."""
    compiled = statement.compile(compile_kwargs={"literal_binds": True})
    rows = await session.execute(text(f"EXPLAIN (FORMAT JSON) {compiled}"))
    plan = rows.scalar_one()[0]["Plan"]
    return cast(dict[str, object], plan)


def _plan_nodes(plan: dict[str, object]) -> list[dict[str, object]]:
    children = cast(list[dict[str, object]], plan.get("Plans", []))
    return [plan, *(node for child in children for node in _plan_nodes(child))]


async def test_the_written_out_order_cannot_use_the_index_that_nulls_last_can(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """Not "the index is missing", but "the spelling cannot be matched to the index".

    `titles.sort_name` is `NOT NULL` and `ix_titles_sort_name` is a plain btree on it.
    Postgres nevertheless does **not** simplify `sort_name IS NOT NULL` to `true`, and it
    matches an index by the *sort-key expression* -- so `(sort_name IS NOT NULL) DESC,
    sort_name, id` has a leading key no index carries and `sort_name ASC NULLS LAST, id`
    has one that `ix_titles_sort_name` does. Same rows, same order, different plan.

    `SET LOCAL enable_seqscan = off` is what makes that observable on a fixture of seven
    rows, and it is this file's own idiom: forcing the choice separates *"the planner did
    not pick it"* from *"the planner could not pick it"*. The refused plan comes back at
    cost **1e10**, the disabled-node penalty, which is the signature of the second.
    """
    await _seed_equivalence_population(repo)
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    # `ColumnElement`, because that is what `_browse_order` takes and what
    # `browse` reaches it with -- there through `getattr`, which mypy sees as
    # `Any`, so the cast is where that erasure is made explicit rather than a
    # widening of anything.
    key = cast("ColumnElement[Any]", TitleRow.sort_name)

    shipped = await _plan_of(
        session, select(TitleRow.id).order_by(*_browse_order(key, descending=False)).limit(3)
    )
    written_out = await _plan_of(
        session,
        select(TitleRow.id)
        .order_by(key.is_not(None).desc(), key.asc(), TitleRow.id.asc())
        .limit(3),
    )

    assert "ix_titles_sort_name" in {node.get("Index Name") for node in _plan_nodes(shipped)}, (
        f"the shipped clause did not reach the index: {shipped}"
    )
    assert {node["Node Type"] for node in _plan_nodes(written_out)} >= {"Seq Scan", "Sort"}, (
        "the written-out clause is expected to sort a sequential scan, because "
        f"no index carries its leading key: {written_out}"
    )
    assert "ix_titles_sort_name" not in {
        node.get("Index Name") for node in _plan_nodes(written_out)
    }
    assert cast(float, written_out["Total Cost"]) > 1e9, (
        "and it is *unchoosable* rather than merely not chosen: with sequential "
        "scans disabled the planner still took one, at the disabled-node "
        f"penalty. Cost {written_out['Total Cost']}"
    )
    assert cast(float, shipped["Total Cost"]) < 1e9, (
        "the premise for the line above: the same penalty is not on the shipped "
        "plan, so the comparison is about the sort key and not about the GUC"
    )


class TestPostgresTitleRepositoryGenreSweep(TitleRepositoryGenreSweepContract):
    """`list_genres_page` and `replace_genres` against real Postgres.

    The half with teeth. `replace_genres` is an `UPDATE ... FROM (VALUES ...)`
    whose `IS DISTINCT FROM` guard is what makes a re-run write zero rows, and
    `rowcount` is what reports it -- neither is expressible against a dict,
    which can only compare in Python and count what it decided to compare. The
    keyset walk is the same read `usher index --backfill`'s is and fails the
    same way on `>=`.
    """

    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresTitleRepository:
        return PostgresTitleRepository(session)


class TestPostgresTitleRepositoryNaturalKeys(TitleRepositoryNaturalKeyContract):
    """`resolve_natural_keys` against real Postgres.

    The half with teeth: `WITH ORDINALITY` over four parallel arrays, three
    subquery rungs whose precedence is a `COALESCE`, and `p.kind` compared
    against a `VARCHAR(16)` column -- all of which the fake reproduces with a
    Python scan and could reproduce wrongly. The statement count is one case
    further down and is Postgres-only by construction.
    """

    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresTitleRepository:
        return PostgresTitleRepository(session)


async def test_resolving_natural_keys_costs_one_statement_for_a_whole_batch(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """The N+1 the port refuses, and the only arm that can see it.

    The fake has no round trip to count. Held against a fixed batch count rather than a
    fixed batch: a `media_items` restore carries one reference per linked copy, so a
    lookup per reference is a round trip per copy in the library. The three-rung ladder
    is deliberately in the batch, because a per-rung implementation is the other shape of
    the same defect: three statements a call is not one.
    """
    by_imdb = Title(kind=TitleKind.MOVIE, name="A", sort_name="A", imdb_id="tt99000801")
    by_tmdb = Title(kind=TitleKind.MOVIE, name="B", sort_name="B", tmdb_id=99000802)
    by_raw = Title(kind=TitleKind.MOVIE, name="C", sort_name="C")
    for one in (by_imdb, by_tmdb, by_raw):
        await repo.add(one)

    carried = [
        TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99000801"),
        TitleReference(kind=TitleKind.MOVIE, id=new_id(), tmdb_id=99000802),
        TitleReference(kind=TitleKind.MOVIE, id=by_raw.id),
        TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99000899"),
    ]

    with _capturing_sql(session) as statements:
        answers = await repo.resolve_natural_keys(carried[:1])
        one_key = len(statements)
        statements.clear()
        answers = await repo.resolve_natural_keys(carried)
        whole_batch = len(statements)

    assert one_key == 1, f"one reference cost {one_key} statements: {statements}"
    assert whole_batch == one_key, (
        f"{one_key} statement(s) for one reference, {whole_batch} for four"
    )
    assert answers == {
        carried[0]: by_imdb.id,
        carried[1]: by_tmdb.id,
        carried[2]: by_raw.id,
    }, "the premise: all three rungs really resolved, and the fourth really did not"


async def test_the_ladder_plans_to_the_indexes_it_was_designed_for(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """Each rung is an index probe, not a scan of `titles`."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        str(line)
        for line in (
            await session.execute(
                text("EXPLAIN " + _RESOLVE_NATURAL_KEYS),
                {
                    "imdb_ids": ["tt99000901"],
                    "kinds": [TitleKind.MOVIE.value],
                    "tmdb_ids": [99000902],
                    "raw_ids": [new_id()],
                },
            )
        ).scalars()
    )

    assert "ix_titles_imdb_id" in plan, plan
    assert "ix_titles_tmdb_id_kind" in plan, plan
    assert "pk_titles" in plan, plan
    assert "Seq Scan on titles" not in plan, plan
