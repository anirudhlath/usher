import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import ColumnElement, Select, Table, event, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.contract.title_repository_contract import (
    TitleRepositoryBrowseContract,
    TitleRepositoryCandidateContract,
    TitleRepositoryContract,
    TitleRepositoryOwnedContract,
)
from usher.db.models.source import MediaItemRow
from usher.db.models.title import DERIVED_COLUMNS, TitleRow
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository, _browse_order
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import BrowseSort


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


@contextmanager
def _capturing_sql(session: AsyncSession) -> Iterator[list[str]]:
    """Every statement this session's connection actually sends, verbatim.

    `before_cursor_execute` is the only place the *emitted* text is visible:
    what a repository builds is a SQLAlchemy construct, and the two are not the
    same claim -- a `defer()` that never reached the statement, or a projection
    that widened, is invisible from the construct's own API and plain in the
    string. Shared by the three cases below rather than re-declared per case,
    which is how the first of them shipped.
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
    """The captured statements that read `titles` as an *entity* -- i.e. that
    project the wide column list `_to_domain` consumes.

    A filter rather than "the only statement", because a session flush or a
    fixture's own write can share the capture window, and a case that indexed
    `statements[0]` would silently start asserting about whichever statement
    arrived first.
    """
    return [
        statement
        for statement in statements
        if statement.lstrip().startswith("SELECT") and "titles.overview" in statement
    ]


def _projections_over_titles(statement: str) -> list[str]:
    """Every `SELECT <projection> FROM titles` stage in one statement, in the
    order they appear in the text.

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
    """`credit_names` is in `DERIVED_COLUMNS`, so `_to_domain` drops it from
    every row it builds -- after Postgres has detoasted up to ten cast names
    per title, serialised them and put them on the wire.

    **The three reads that select the whole entity are the whole population**,
    and they are asserted together because the deferral is per statement: two
    of them shipped one `defer()` and the third the same one, so a fix applied
    to the read that prompted it would leave the other two paying.

    `search_document` is asserted beside it although that half already held.
    Not decoration: the two are one `options()` call, and a rewrite that drops
    the deferral drops both -- pinning only the new one would let the tsvector
    come back with nothing to notice.

    Verified by reading every consumer rather than by this case alone: nothing
    reaches `credit_names` through a loaded `TitleRow`. `credit_names_for`
    selects the column explicitly (a column read, unaffected by an entity
    load's options), `db/repositories/people.py` writes it in raw SQL and
    `db/repositories/search.py` reads it in raw SQL inside the document
    fingerprint.
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


async def test_the_candidate_pool_ranks_on_a_narrow_projection(
    repo: PostgresTitleRepository, session: AsyncSession
) -> None:
    """**The sort is over the whole catalog and the projection it carried was
    the whole row.** `list_unwatched_candidates` outer-joins 1,271,138 titles
    to a `DISTINCT` over `media_items`, anti-joins `watch_states`, sorts on
    four keys and keeps 200 -- and every row entering that sort carried all
    thirty-one columns, including `overview`, `keywords` and
    `field_provenance`, so the sort's working set is the catalog's text rather
    than its keys. Its consumers read four fields.

    The shape asserted here is: rank on `titles.id` plus the sort keys, then
    join the entity back onto the ~200 survivors. Three assertions, and each
    names a different way the rewrite can be wrong:

    - **Two stages project `titles`, not one.** Before, there is one stage and
      it is the wide one; a rewrite that merely reordered the clauses still has
      one.
    - **The ranking stage names none of the columns nobody reads.** A "narrow"
      projection that kept the entity is the defect wearing the fix's shape.
    - **The final `ORDER BY` follows the `LIMIT` in the text**, which is what
      says the surviving rows are re-ordered rather than handed back in
      whatever order the join produced. Measured rather than assumed: deleting
      that clause fails this case *and* nine of the thirteen cases in
      `TitleRepositoryCandidateContract` on this arm, so the fixture is not
      resting on luck today. It is asserted here anyway because what those nine
      observe is one planner's output order at four rows, and ADR-0028's
      stability is a claim about 1,271,138.

    The ordering *contract* is unchanged and stays where it lives -- thirteen
    positional cases on both arms. This case is about the shape those cases
    cannot see.
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

    with _capturing_sql(session) as statements:
        await repo.update(fetched)  # same data just read back -- a true no-op

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


class TestPostgresTitleRepositoryBrowse(TitleRepositoryBrowseContract):
    """`browse`/`browse_facets` against real Postgres, which is where four of
    this read's halves can fail and the fake's cannot.

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
            # (`ports/ingest.py`'s `MediaItemTarget`) and the only row that can
            # tell browse's `episode_id IS NULL` bound apart from
            # `list_owned_by_tag`'s deliberate absence of one. The candidate
            # arm's fixture records at length why leaving `episode_id` NULL
            # here would make the case vacuous.
            #
            # `available=False` writes a real retracted row -- what
            # `mark_unseen_unavailable` leaves behind -- which the fake cannot
            # express at all.
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
    """**PRD 07's own reason for refusing offset paging, measured instead of
    asserted.**

    *"Offset paging is not offered -- it degrades badly over a 1.3M-row catalog
    and produces duplicates under concurrent writes."* The first clause was
    measured in M4 (`list_unmatched`'s `OFFSET` at 43.7 ms / 388.9 ms). The
    second was not, and ADR-0034's *Uncertainty* section says so in as many
    words: it *"needs a real database with a row inserted between page 1 and
    page 2 -- which needs a repository that exposes a wire-paged read, and none
    does yet. It must ride with group B's first paged route."* This is that
    read, so this is that case.

    Both arms page the same table with the same `ORDER BY`, and a row is
    committed between the two requests -- an ordinary concurrent write, not a
    contrived one. The keyset resumes from a *position* and is unaffected; the
    offset resumes from a *count* and the count moved under it.

    **Three premises, because without them the case is a coincidence.** The
    inserted row must sort into the page already served (a row after the
    cursor is a page-2 row under both spellings); the two spellings must agree
    on page 1 (or the disagreement below is about the `ORDER BY` rather than
    about how page 2 resumes); and the offset arm's duplicate is asserted *as
    a duplicate*, by name, rather than inferred from a length.

    **What this measures is a duplicate and not a drop, which is exactly what
    PRD 07 claims.** An insert grows the population by one, so the window that
    slid by one still reaches the last row: nothing here is lost, `Charlie` is
    simply served twice. The mirror defect — a row *never* served — needs a
    concurrent **delete**, which is a different write and is not claimed by
    the sentence this case exists to verify.
    """
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
#: one tie and — where the column is nullable — at least two NULLs. Both are
#: needed for the equivalence below to be about anything: a fixture with no
#: ties cannot see the `id` tail move and one with no NULLs cannot see the
#: NULLS-LAST leg move, which are the only two ways the two spellings could
#: disagree. Seeded in this order, which is id order and is no sort's answer.
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
    # 2010 already repeat). Without it three of the four sorts had no tie and
    # their `id` tail was unobservable, which is what the premise guard below
    # caught on this fixture's first run rather than on some later one.
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
            popularity=popularity,
            vote_count=vote_count,
        )
        await repo.add(one)
        seeded.append(one)
    return seeded


@pytest.mark.parametrize("sort", list(BrowseSort))
async def test_the_shipped_order_is_byte_identical_to_the_written_out_one(
    repo: PostgresTitleRepository, session: AsyncSession, sort: BrowseSort
) -> None:
    """**The guarantee that replaced a legibility argument, and it is stronger
    than what it replaced.**

    `browse` used to spell its `ORDER BY` as `(key IS NOT NULL) DESC, key
    <dir>, id` — written out, so that a reader could see it and
    `_browse_after`'s three arms were term for term the same rule. B7 measured
    what that costs: **299.21 ms p50 against 0.92 ms, 317x**, on `sort=name`
    over a real 1,272,367-title catalog, because an index is matched by the
    *sort-key expression* and no index carries `sort_name IS NOT NULL`. The
    clause is now `key <dir> NULLS LAST, id`.

    The two are the same order **by an argument**, and an argument is what this
    case replaces. It runs both spellings over one population and compares them
    position for position — unpaged, and again as a keyset walk, because the
    `WHERE` predicate was not touched and has to keep agreeing with a clause
    that no longer looks like it.

    The reference is built from `BrowseSort.order_for`, so it cannot drift on
    *which* column or direction a sort means; only the spelling under test is
    the shipped object's. Every premise is asserted, because an equivalence
    over a fixture with no NULLs and no ties is an equivalence about nothing.
    """
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
    """**Why the clause changed: not "the index is missing", but "the spelling
    cannot be matched to the index that is there".**

    `titles.sort_name` is `NOT NULL` and `ix_titles_sort_name` is a plain btree
    on it. Postgres 17 nevertheless does **not** simplify
    `sort_name IS NOT NULL` to `true`, and it matches an index by the *sort-key
    expression* — so `(sort_name IS NOT NULL) DESC, sort_name, id` has a
    leading key no index carries and `sort_name ASC NULLS LAST, id` has one
    that `ix_titles_sort_name` does. Same rows, same order, different plan.

    `SET LOCAL enable_seqscan = off` is what makes that observable on a
    fixture of seven rows, and it is this file's own idiom rather than a new
    one: `m09a`'s prefix indexes are pinned the same way, because forcing the
    choice separates *"the planner did not pick it"* from *"the planner could
    not pick it"*. The refused plan comes back at cost **1e10**, which is the
    disabled-node penalty and is the signature of the second.

    B7's numbers on a real catalog: 299.21 ms p50 -> 0.92 ms, **317x**, 51x
    under its own 50 ms bar, and byte-identical on 25 of 25 positions.
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
