"""Persistence for canonical titles.

Repositories translate between SQLAlchemy rows and domain models. Nothing
above this layer sees a Row type. Implements the TitleRepository port
(usher.ports.repository) so services can depend on the port instead of
this module directly — see ADR-0009.

Session-poisoning decision (binding on every repository after this one —
see the plan's "Open question flagged for Group E" note on Task 10):
`add()` and `update()` each wrap their flush in `session.begin_nested()`,
a SAVEPOINT, rather than calling `session.rollback()` in the `except`
block. Both close the same hole — Postgres aborts the *entire* transaction
on any statement error until a ROLLBACK, so an uncaught `IntegrityError`
leaves the session unusable for anything else. But `TitleRepository`'s own
docstring already says "the caller owns the session and the transaction:
... committing or rolling back is the caller's call" — a full
`session.rollback()` here would discard whatever else the caller had
pending on this session (e.g. three other repository calls earlier in the
same request), which is exactly the ownership that line rules out. A
SAVEPOINT confines the damage to just this call's own statement(s): on
`IntegrityError` it rolls back to the SAVEPOINT, not to the start of the
transaction, so the caller's other pending work survives and the caller
still decides commit vs. rollback for the transaction as a whole. The cost
is one extra round trip per `add`/`update` call — accepted deliberately,
because the alternative silently breaks the port's documented contract.
Verified directly (`tests/integration/test_title_repository.py`): without
the SAVEPOINT, a caught `RepositoryConflict` leaves the *session* raising
`PendingRollbackError` on the next unrelated call, not just the failed one
— proving this isn't a hypothetical concern.

`update()` needs the identical translation, not just `add()`: the plan's
own amendment claiming "nothing in [update's] current body raises
IntegrityError... it can't yet" no longer holds against the schema this
task actually ships against — `ix_titles_tmdb_id_kind`/`ix_titles_imdb_id`/
`ix_titles_tvdb_id` (unique partial indexes, `db/models/title.py`, shipped
in Task 8/9, before this task; `ix_titles_tmdb_id_kind` widened from a
single-column index by ADR-0011) are exactly the kind of future column the
amendment was waiting for, except they already exist. `update()` sets
tmdb_id/imdb_id/tvdb_id from the incoming `Title`, so retargeting one to a
value another row already holds raises `IntegrityError` today, not
someday. Left uncaught, that would violate the same "db is driven, not
driving" contract `add()`'s translation exists to uphold — confirmed
directly by first writing this without the fix and watching a raw
`sqlalchemy.exc.IntegrityError` escape `update()`.

Autoflush is the second way a storage exception can escape this class,
independent of the SAVEPOINT decision above. `session.get()` and
`session.execute()` both flush any pending, unflushed session state before
running — including state this repository never touched, left behind by
some other call sharing the same `AsyncSession` (see `TitleRepository`'s
docstring for the session-wide precondition this is about). Verified
directly: a pending, invalid row staged on the session (not flushed) made
every one of `get`/`get_by_tmdb_id`/`get_by_imdb_id`/`count_by_state` raise
a raw `sqlalchemy.exc.IntegrityError`, and made `update()`'s own
`session.get()` lookup do the same, before the fix below. The four read
methods now run their query inside `self._session.no_autoflush` — a plain
read has nothing to flush on its own account, so suppressing autoflush
entirely is strictly better there than catching and translating an error
that isn't this call's own conflict to report. `update()`'s lookup instead
moved inside its existing SAVEPOINT + `except IntegrityError` (the same
protection its mutate-and-flush already had), because unlike a pure read,
`update()` already has a conflict-shaped exception to raise if the lookup's
autoflush fails.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Text, exists, func, literal, nulls_last, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from usher.db.models.episode import EpisodeRow
from usher.db.models.source import MediaItemRow
from usher.db.models.title import DERIVED_COLUMNS, TitleRow
from usher.db.models.watch import WatchStateRow
from usher.db.repositories._errors import constraint_name
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository


def _to_domain(row: TitleRow) -> Title:
    # `- DERIVED_COLUMNS`, not a hardcoded name: `Title` is `extra="forbid"`,
    # so an index artefact reaching this dict raises on *every read of every
    # title*, in every entry point. The set is declared on the model so that
    # adding a derived column is one edit in one place and adding an ordinary
    # column still breaks loudly, which is the property the 1:1 rule exists
    # for.
    return Title.model_validate(
        {
            column.name: getattr(row, column.name)
            for column in TitleRow.__table__.columns
            if column.name not in DERIVED_COLUMNS
        }
    )


# The three bookkeeping columns update() has always excluded, plus every
# derived column. `fresh` is a transient row `_to_row` built and never sets a
# generated column, so without this the mutation loop assigns `None`,
# SQLAlchemy puts it in the SET clause, and Postgres answers `column
# "search_document" can only be updated to DEFAULT`. That fires on writes,
# not reads -- which is why the failing test for it was written first.
_NOT_UPDATABLE = {"id", "created_at", "updated_at"} | DERIVED_COLUMNS


# The four ARRAY(Text) columns -- see the module docstring's note on
# ARRAY(Text) always reading back as a list, never a tuple.
_ARRAY_FIELDS = ("genres", "keywords", "spoken_languages", "origin_countries")


def _to_row(title: Title) -> TitleRow:
    # Emits lists for the four ARRAY columns, not the tuples Title actually
    # types them as. update()'s mutate loop setattr()s every column from
    # this onto an already-persistent row loaded from the database, and a
    # loaded row's ARRAY columns are always lists -- never tuples -- on
    # read. SQLAlchemy's attribute-history comparison (what decides whether
    # a column is actually included in the UPDATE's SET clause) uses `==`,
    # and `("a",) != ["a"]` in Python regardless of contents, so emitting
    # tuples here made every update() rewrite all four arrays even when
    # nothing in them changed -- confirmed directly by counting the actual
    # UPDATE statements SQLAlchemy issued for a semantically no-op update()
    # call: before this fix, one was issued touching all four columns;
    # after, none is.
    data = title.model_dump(exclude={"created_at", "updated_at"})
    for field in _ARRAY_FIELDS:
        data[field] = list(data[field])
    return TitleRow(**data)


def _conflict(title_id: uuid.UUID, constraint: str | None) -> RepositoryConflict:
    """Builds an accurate `RepositoryConflict` for `add()`/`update()`
    alike. Deliberately never claims `title_id` itself already exists --
    measured bug this replaced: the message used to read "title {id}
    already exists" unconditionally, which is false whenever the actual
    collision was on a *different* row's tmdb_id/imdb_id/tvdb_id (`id`
    doesn't pre-exist at all in that case; the provider id does). "conflicts
    with an existing title" is true either way -- `title_id`'s own id
    collided, or one of its provider ids did -- and `constraint` carries
    the specific, structured answer for a caller that needs to branch on
    which.
    """
    detail = f" (constraint: {constraint})" if constraint else ""
    return RepositoryConflict(
        f"title {title_id} conflicts with an existing title{detail}", constraint=constraint
    )


class PostgresTitleRepository(TitleRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, title: Title) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_to_row(title))
                await self._session.flush()
        except IntegrityError as exc:
            # Postgres's own unique-violation on a duplicate id (or a
            # duplicate tmdb_id/imdb_id/tvdb_id), translated so callers
            # depend only on usher.ports.errors -- importing sqlalchemy.exc
            # here would break "db is driven, not driving". The flush runs
            # inside a SAVEPOINT (begin_nested) specifically so this catch
            # only unwinds this call's own insert, not the caller's whole
            # transaction -- see the module docstring.
            raise _conflict(title.id, constraint_name(exc)) from exc

    async def update(self, title: Title) -> None:
        # _to_row(title) raises loudly on any field/column mismatch, the same
        # way add() does -- a setattr loop straight off title.model_dump()
        # would not raise, it would just silently skip a would-be column
        # that no longer has a match, undoing the "loud, not silent" point
        # made below.
        fresh = _to_row(title)
        try:
            async with self._session.begin_nested():
                # session.get() lives *inside* the try and the SAVEPOINT,
                # not just the flush below -- it autoflushes by default
                # (SQLAlchemy's load_on_pk_identity, no_autoflush=False),
                # so it can just as easily be the statement that surfaces
                # some *other*, unrelated pending row's IntegrityError on
                # this shared session (verified: session.execute()/
                # session.get() both do). Per the ordering trap already
                # documented below, that flush must happen inside the
                # SAVEPOINT or a caught conflict leaves the outer
                # transaction DEACTIVE for the next unrelated call -- the
                # same reasoning as the mutation loop, just one statement
                # earlier. See TitleRepository's docstring for the
                # session-wide precondition this is a backstop for, not a
                # substitute for.
                row = await self._session.get(TitleRow, title.id)
                if row is None:
                    raise RepositoryNotFound(f"no existing title {title.id} to update")
                # The mutation happens *inside* the SAVEPOINT scope, not
                # before it -- verified directly that mutating `row` first
                # and only wrapping flush() leaves the session's outer
                # transaction DEACTIVE after a caught conflict (a second,
                # unrelated call then raises PendingRollbackError instead
                # of succeeding). SQLAlchemy's rollback-to-SAVEPOINT only
                # cleanly reverts attribute changes it watched happen
                # within its own scope.
                for column in TitleRow.__table__.columns:
                    if column.name not in _NOT_UPDATABLE:
                        setattr(row, column.name, getattr(fresh, column.name))
                await self._session.flush()
        except IntegrityError as exc:
            # Same translation and same SAVEPOINT reasoning as add() -- see
            # the module docstring. Retargeting tmdb_id/imdb_id/tvdb_id to a
            # value another title already holds raises IntegrityError here
            # today (verified), not just in some future schema change.
            raise _conflict(title.id, constraint_name(exc)) from exc

    async def get(self, title_id: uuid.UUID) -> Title | None:
        # no_autoflush: a plain read has no business flushing anything, and
        # by default it would anyway (session.get() autoflushes) -- so a
        # pre-existing, unflushed, invalid row left on this *shared* session
        # by unrelated code could otherwise fail right here, as a raw
        # sqlalchemy.exc.IntegrityError this method has no way to translate
        # meaningfully (it isn't this read's conflict to report). See
        # TitleRepository's docstring for the session-wide precondition
        # this is a backstop for, not a substitute for.
        with self._session.no_autoflush:
            row = await self._session.get(TitleRow, title_id)
        return _to_domain(row) if row else None

    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        # tmdb_id's own type is `int`, not `int | None` -- but a caller
        # holding a genuinely optional value (e.g. Title.tmdb_id itself)
        # can still reach this with None if it ever bypasses mypy at the
        # call site (a stray `# type: ignore`, `cast`, ...). Guarded
        # because `TitleRow.tmdb_id == None` compiles to `IS NULL`,
        # matching whichever null-provider-id title Postgres happens to
        # return first -- not "the title with this id", the opposite of
        # what this method promises. Verified: without this,
        # get_by_tmdb_id(None) returns an arbitrary title instead of None.
        #
        # The kind filter is not optional either. Without it this query can
        # match a movie and a series holding the same tmdb_id, and
        # scalar_one_or_none() then raises a raw
        # sqlalchemy.exc.MultipleResultsFound out of the port -- reproduced
        # directly against tmdb_id=90000550 in both namespaces. ADR-0011.
        if tmdb_id is None:
            return None
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow).where(TitleRow.tmdb_id == tmdb_id, TitleRow.kind == kind)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def resolve_tmdb_ids(
        self, kind: TitleKind, tmdb_ids: Sequence[int]
    ) -> dict[int, uuid.UUID]:
        # No statement at all for an empty batch. A derivation page whose
        # payloads are all one kind reaches the other branch with nothing to
        # ask about, and `tmdb_id = ANY('{}')` is a round trip to learn
        # nothing -- the same guard `list_by_ids` carries.
        if not tmdb_ids:
            return {}
        with self._session.no_autoflush:  # see get()'s comment
            rows = await self._session.execute(
                # Two columns, never the whole row: the caller wants a
                # `Credit.title_id` and a link target, not 31 columns per
                # title. `ix_titles_tmdb_id_kind` serves this directly, and
                # the `kind` predicate is half the key rather than a filter
                # -- ADR-0011, and without it this collapses a movie and a
                # series that share an integer into one arbitrary answer.
                select(TitleRow.tmdb_id, TitleRow.id).where(
                    TitleRow.tmdb_id.in_(set(tmdb_ids)), TitleRow.kind == kind
                )
            )
        # `tmdb_id` is nullable on the row and the predicate above excludes
        # NULL by construction, so the narrowing is a type-checker fact
        # rather than a runtime branch.
        return {tmdb_id: title_id for tmdb_id, title_id in rows.all() if tmdb_id is not None}

    async def credit_names_for(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, ...]]:
        if not title_ids:
            return {}
        with self._session.no_autoflush:  # see get()'s comment
            rows = await self._session.execute(
                # Two columns, not the row: this is read once per backfill
                # page over the enriched tier, and pulling 31 columns per
                # title to reach one array is the shape `list_by_ids` exists
                # to avoid being used for.
                select(TitleRow.id, TitleRow.credit_names).where(TitleRow.id.in_(set(title_ids)))
            )
        # The column is NOT NULL with a server_default, so `or ()` is a
        # type-checker courtesy rather than a live branch -- and it stays,
        # because a NULL here would be the STRICT-wrapper failure and an
        # empty tuple is the right answer to give the composer either way.
        return {title_id: tuple(names or ()) for title_id, names in rows.all()}

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        # See get_by_tmdb_id's comment -- same IS NULL hazard, same guard.
        if imdb_id is None:
            return None
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow).where(TitleRow.imdb_id == imdb_id)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        # One statement for a whole result set. Hydrating 50 hits through
        # `get()` is 50 round trips per search -- the same shape `index_many`
        # was introduced to delete from `SearchIndex`, arriving from the other
        # direction.
        #
        # `defer(search_document, raiseload=True)` for the reason `list_stale`
        # carries it: `titles` holds a tsvector roughly the size of the
        # document it indexes and a ranked result set has no use for it, so
        # without the deferral every search ships one per hit for nothing.
        # `raiseload` turns a stray attribute access into a raise rather than
        # into one extra query per row -- an N+1 that answers correctly and is
        # therefore invisible. `_to_domain` filters `DERIVED_COLUMNS` before
        # touching it, so nothing legitimate trips it.
        #
        # An id naming no row is simply absent from the answer; the port says
        # so, and the caller re-orders by its own ranking anyway.
        if not title_ids:
            # Never an unbounded read: `IN ()` is a syntax error in Postgres
            # and SQLAlchemy renders an always-false expression with a
            # warning, so the empty case is answered here rather than sent.
            return []
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow)
                .options(defer(TitleRow.search_document, raiseload=True))
                .where(TitleRow.id.in_(list(title_ids)))
            )
        return [_to_domain(row) for row in result.scalars().all()]

    async def list_owned_by_tag(
        self,
        *,
        genre: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
    ) -> list[Title]:
        if genre is None and keyword is None:
            # No statement at all. An unpredicated call is "the library
            # ordered by popularity", which is the popular-titles fallback
            # spelled as a query, and the port declines to express it.
            return []
        # **The ownership semi-join is inside the statement**, which is the
        # whole reason this method exists rather than the caller filtering a
        # catalog read: taking the twenty most popular horror films from
        # 1.27M rows and *then* asking which are owned answers nothing on a
        # normal household.
        #
        # `EXISTS` rather than a join, and deliberately **without**
        # `episode_id IS NULL`. Without the bound a series owned through its
        # episode files is owned, which is the answer this read wants and the
        # opposite of the one `owned_title_ids` wants; and `EXISTS`
        # short-circuits, so the 20,000-episode series costs one probe rather
        # than the 20,001 rows a join would multiply out.
        owned = exists().where(
            MediaItemRow.title_id == TitleRow.id,
            MediaItemRow.available.is_(True),
        )
        statement = (
            select(TitleRow).options(defer(TitleRow.search_document, raiseload=True)).where(owned)
        )
        # `@>` written out, because `ARRAY.contains()` raises
        # `NotImplementedError` on the *generic* `ARRAY` these columns are
        # declared with -- only the dialect-specific type implements it, and
        # the model declares the generic one deliberately (it is what makes
        # M2's bulk path and the ORM agree). Measured, not guessed: the
        # obvious spelling fails at statement-build time in the integration
        # run and never at all against the fake.
        #
        # AND, never OR: a window carrying both a genre and a keyword wants
        # the intersection, and the union is a strictly larger, less relevant
        # row that still looks right.
        if genre is not None:
            statement = statement.where(
                TitleRow.genres.bool_op("@>")(sql_cast([genre], PG_ARRAY(Text)))
            )
        if keyword is not None:
            statement = statement.where(
                TitleRow.keywords.bool_op("@>")(sql_cast([keyword], PG_ARRAY(Text)))
            )
        statement = statement.order_by(
            # `nulls_last` spelled out: Postgres defaults a DESC sort to NULLS
            # FIRST, and `titles.popularity` was measured NULL on all
            # 1,271,138 rows of a bootstrap-only catalog -- so the default
            # puts the entire unknown population above every known one.
            nulls_last(TitleRow.popularity.desc()),
            nulls_last(TitleRow.vote_count.desc()),
            TitleRow.id,
        ).limit(limit)
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(statement)
        return [_to_domain(row) for row in result.scalars().all()]

    async def list_unwatched_candidates(
        self,
        user_id: uuid.UUID,
        *,
        genres: Sequence[str] = (),
        limit: int,
    ) -> list[Title]:
        # **Ownership is a LEFT JOIN here and an `EXISTS` in
        # `list_owned_by_tag`, and the difference is where it sits in the
        # statement.** There it is a `WHERE` predicate over an already-narrow
        # candidate set, which Postgres plans as a semi-join and which
        # short-circuits. Here it is a *sort key* over the whole catalog, and
        # a correlated subquery in an `ORDER BY` cannot be turned into a join
        # at all -- it is one SubPlan execution per candidate row, which at
        # 1.27M rows is the difference between a nightly job and a nightly
        # incident. Reasoned from the planner's own rules rather than measured
        # at scale; what is *not* a guess is that the two spellings agree, and
        # the contract suite is what says so on both arms.
        #
        # `DISTINCT`, so a 20,000-episode series contributes one row rather
        # than 20,000 and the join cannot multiply the catalog out. No
        # `episode_id IS NULL` bound, deliberately and exactly as
        # `list_owned_by_tag` has none: a series owned only through its
        # episode files is owned, which is the normal case on a library that
        # is 89% episodes.
        owned_titles = (
            select(MediaItemRow.title_id)
            .where(MediaItemRow.available.is_(True), MediaItemRow.title_id.is_not(None))
            .distinct()
            .subquery("owned_titles")
        )
        owned = owned_titles.c.title_id.is_not(None)
        # The exclusion, and it is `played_title_ids`' predicate: `played`
        # rather than "has a watch state", and `COALESCE(ws.title_id,
        # e.title_id)` so a watched episode takes its series with it. A
        # correlated `NOT EXISTS` is what Postgres plans as an anti-join,
        # which is why this one *is* a subquery where the ownership key above
        # is not.
        watched = (
            select(literal(1))
            .select_from(WatchStateRow)
            .outerjoin(EpisodeRow, EpisodeRow.id == WatchStateRow.episode_id)
            .where(
                WatchStateRow.user_id == user_id,
                WatchStateRow.played.is_(True),
                func.coalesce(WatchStateRow.title_id, EpisodeRow.title_id) == TitleRow.id,
            )
            .exists()
        )
        # `&&` written out for the reason `list_owned_by_tag` writes `@>` out:
        # the generic `ARRAY` these columns are declared with implements
        # neither operator through SQLAlchemy's own helpers, and the model
        # declares the generic one deliberately. An empty `genres` is a real
        # and common argument -- a household with no history has no
        # affinities -- and `genres && '{}'` is false for every row, which
        # leaves the remaining keys deciding the whole order.
        affine = TitleRow.genres.bool_op("&&")(sql_cast(list(genres), PG_ARRAY(Text)))
        statement = (
            select(TitleRow)
            .options(defer(TitleRow.search_document, raiseload=True))
            .outerjoin(owned_titles, owned_titles.c.title_id == TitleRow.id)
            .where(~watched)
            .order_by(
                owned.desc(),
                affine.desc(),
                # `nulls_last` spelled out: Postgres defaults a DESC sort to
                # NULLS FIRST, and on a bootstrap-only catalog every row's
                # `vote_count` can be NULL -- so the default would put the
                # unknown population above the known one and then let the
                # `id` tail decide the pool.
                nulls_last(TitleRow.vote_count.desc()),
                # ADR-0028's stability, and the only reason two reads of one
                # unchanged catalog agree about what index 7 names.
                TitleRow.id,
            )
            .limit(limit)
        )
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(statement)
        return [_to_domain(row) for row in result.scalars().all()]

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow.enrichment_state, func.count()).group_by(TitleRow.enrichment_state)
            )
        counts = dict.fromkeys(EnrichmentState, 0)
        counts.update({EnrichmentState(state): count for state, count in result.all()})
        return counts
