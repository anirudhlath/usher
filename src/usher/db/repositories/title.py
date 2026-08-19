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
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Row,
    Text,
    Uuid,
    and_,
    column,
    exists,
    func,
    literal,
    nulls_last,
    or_,
    select,
    update,
    values,
)
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
from usher.domain.genres import canonical_genres, genre_spellings
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import (
    BrowseCursorPosition,
    BrowseFacets,
    BrowseSort,
    TitleGenres,
    TitleRepository,
)


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


# **One deferral per member of `DERIVED_COLUMNS`, which is what makes
# `_to_domain`'s filter cost nothing.** That filter runs after the row has
# arrived: every derived column it drops was selected, detoasted, serialised
# and put on the wire first. `titles` holds a tsvector roughly the size of the
# document it indexes and up to ten cast names per title, and a ranked result
# set, an owned-by-tag shelf and a 200-title candidate pool have no use for
# either -- so the two sets must stay in step, and a derived column added to
# one and not the other is a column paid for on every entity read forever.
# `tests/integration/test_title_repository.py::
# test_no_entity_read_ships_credit_names_over_the_wire` iterates
# `DERIVED_COLUMNS` rather than naming these two, so that is what fails.
#
# **`raiseload` on one and not the other, and the asymmetry is the decision
# rather than an oversight.** `search_document` is a `TSVECTOR`: `Title` is
# `extra="forbid"` and could not carry it if it wanted to, every consumer of it
# is SQL-side, and there is therefore no access that is not a bug -- so raising
# costs nothing and closes an N+1 that would answer correctly and be invisible.
# `credit_names` is a `text[]` with a live, sanctioned reader one method down
# (`credit_names_for`) and a real meaning to a caller, so a future
# `row.credit_names` off a loaded entity is a mistake about *routing* rather
# than a nonsense access -- and `raiseload` would convert it into an
# `InvalidRequestError` inside the nightly curation job, where plain deferral
# costs one small extra query for ten short strings. Prefer the failure that
# degrades. Verified rather than assumed before choosing either: no reader in
# `src/` reaches `credit_names` through a loaded `TitleRow` -- `credit_names_for`
# selects the column explicitly (a column read, which an entity load's options
# do not touch), `people.py` writes it in raw SQL and `search.py` reads it in
# raw SQL -- and the whole suite was additionally run with `raiseload=True`
# here to prove no untested path does either.
_WITHOUT_DERIVED_COLUMNS = (
    defer(TitleRow.search_document, raiseload=True),
    defer(TitleRow.credit_names),
)


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


def _browse_filters(
    *, genre: str | None, year: int | None, owned: bool | None
) -> list[ColumnElement[bool]]:
    """`browse`'s `WHERE`, built once so `browse_facets` can leave exactly one
    predicate out rather than re-read the filters.

    Two copies of a filter set is two chances for a facet to be counted over a
    population the page is not drawn from -- the same argument
    `_WITHOUT_DERIVED_COLUMNS` makes for looping over `DERIVED_COLUMNS`.
    """
    clauses: list[ColumnElement[bool]] = []
    if genre is not None:
        # **`&&` over every spelling of the concept, not `@>` over the one
        # string the client sent** (ADR-0039). `titles.genres` unions two
        # importers' vocabularies -- the IMDb bulk phase writes `Sci-Fi`
        # (20,051 titles) and `EnrichService` writes TMDb's `Science Fiction`
        # (6,223), and **zero** titles carry both -- so exact containment
        # answered half a concept under either spelling and looked entirely
        # right doing it. `genre_spellings` resolves the label to its concepts
        # and the concepts back to every spelling that names them, so the two
        # requests are one query over one population.
        #
        # Identical to `@>` for an unmapped label: the expansion is a
        # one-element array and `a && ARRAY[x]` is `a @> ARRAY[x]` there.
        #
        # `&&` written out for `list_owned_by_tag`'s measured reason: the
        # generic `ARRAY` these columns are declared with raises
        # `NotImplementedError` from `ARRAY.overlap()`/`.contains()`, at
        # statement-build time in the integration run and never at all against
        # the fake.
        clauses.append(
            TitleRow.genres.bool_op("&&")(sql_cast(list(genre_spellings(genre)), PG_ARRAY(Text)))
        )
    if year is not None:
        clauses.append(TitleRow.year == year)
    if owned is not None:
        # **`episode_id IS NULL` and `available`, one leg from each of this
        # codebase's two readings of "owned".** The port's docstring carries
        # the argument; the short version is that browse is a title-level
        # screen (so a series' episode files are not this row's copy) and its
        # filter answers "what can I play" (so a retracted copy is not one).
        # `EXISTS` rather than a join, so a 20,000-episode series costs one
        # probe rather than 20,000 rows.
        copy = exists().where(
            MediaItemRow.title_id == TitleRow.id,
            MediaItemRow.episode_id.is_(None),
            MediaItemRow.available.is_(True),
        )
        clauses.append(copy if owned else ~copy)
    return clauses


def _canonical_facet(rows: Sequence[Row[tuple[str, int]]]) -> dict[str, int]:
    """`browse_facets`' genre counts, one entry per concept rather than one per
    spelling.

    **The collapse is a sum, and its premise is measured rather than assumed.**
    Summing overcounts exactly when one title carries two spellings of one
    concept, and that is zero across all nine alias pairs on the live catalog
    (1,272,866 titles, 2026-08-19) -- `Sci-Fi`/`Science Fiction` 0,
    `Reality-TV`/`Reality` 0, `Fantasy`/`Sci-Fi & Fantasy` 0, and so on. It
    stays zero because `EnrichService` preserves an IMDb label only when the
    provider's vocabulary cannot name its concept, and a concept with no TMDb
    name has exactly one spelling.

    **The exact spelling was measured and declined.** `SELECT canon, count(*)
    FROM (SELECT DISTINCT id, canon ...)` is correct without the premise and
    ran at **1,789 ms** against this query's **199 ms** on the live catalog --
    against a facet block whose B7 bar (p95 <= 200 ms) is already missed at
    330.81 ms.

    **`usher genres --backfill` removes the need for the collapse without
    removing the collapse**, and the asymmetry is deliberate. On a normalised
    catalog every concept has one spelling, so this sum is over a single key
    and the premise is true by construction rather than by measurement. It
    stays because a fresh bootstrap, a partially-swept catalog and a deployment
    that has never run the command all reach this function, and a facet that is
    correct only after an operator's action is not correct.

    A fused label counting under two concepts is not overcounting: `Sci-Fi &
    Fantasy` says two things and is counted under both, which is exactly what
    `?genre=Fantasy` will then return.
    """
    counts: dict[str, int] = {}
    for label, count in rows:
        for canonical in canonical_genres(label):
            counts[canonical] = counts.get(canonical, 0) + count
    return counts


def _browse_order(key: ColumnElement[Any], *, descending: bool) -> tuple[ColumnElement[Any], ...]:
    """`key <dir> NULLS LAST, id ASC` -- browse's total order.

    **Spelled as `nulls_last(...)` rather than written out as
    `(key IS NOT NULL) DESC, key <dir>`, and that is a measurement rather than
    a preference.** The two produce the identical row order; only one of them
    an index can serve. B7 measured `sort=name` at **299.21 ms p50 written out
    against 0.92 ms as `NULLS LAST`, 317x**, on a page proved byte-identical
    (0 mismatched positions over 25) across a real 1,272,367-title catalog --
    because `titles.sort_name` is `NOT NULL`, `ix_titles_sort_name` already
    exists, and **Postgres matches an index by the sort-key *expression***. It
    does not simplify `sort_name IS NOT NULL` to `true` even on a `NOT NULL`
    column, so the written-out form has a leading sort key no index carries and
    the page becomes a 95,000-buffer Parallel Seq Scan. The general form: two
    spellings of one order are two different sort keys, and **a legibility
    decision about SQL text can be a plan decision.**

    **What the written-out form bought, and what pays for it now.** It made
    `_browse_after`'s three arms and this clause visibly the same rule --
    *"the keyset predicate has to agree with this term for term, and two
    spellings of one rule is how they stop agreeing"* -- and that argument is
    about correctness and is right. The cost of taking the fast spelling is
    that the agreement is no longer legible from the two functions side by
    side: `NULLS LAST` is where `key IS NULL` sorts, and you have to know that
    to see that `_browse_after`'s first disjunct is its other half. So the
    agreement is a **test** instead of a reading --
    `tests/integration/test_title_repository.py::
    test_the_shipped_order_is_byte_identical_to_the_written_out_one` compares
    the two orders position for position, for every member of `BrowseSort`,
    over a population carrying NULLs and ties in every key, unpaged and paged.
    That is a strictly stronger guarantee than the legibility was; it is just
    not free, and it is the thing to keep if this clause is ever touched again.

    **What this does not change: the `WHERE`.** `_browse_after` keeps its three
    arms exactly as measured. The row-comparison spelling silently drops the
    unkeyed tail, and none of that is affected by how the `ORDER BY` is
    written.

    **The nullable sorts do not benefit yet and the reason is worth carrying.**
    `year`, `popularity` and `vote_count` have no index at all, so all three
    stay a sequential scan (B7: 235.55 / 229.50 / 231.21 ms) -- but under the
    written-out spelling a `(col DESC NULLS LAST, id)` btree could not have
    been matched even if it existed, so this change is what makes such an index
    *possible* rather than what makes it unnecessary. Recommended, deliberately
    not minted: `ix_titles_popularity` is this schema's precedent for an index
    declared on a guess, unusable, and dropped two milestones later in `ffc`.
    A GIN index on `genres` is a third question and a genuinely open one --
    B7 found none exists, so the lossy-bitmap recheck B3 measured one subsystem
    over would be *created* by adding it.
    """
    ordered = nulls_last(key.desc()) if descending else nulls_last(key.asc())
    # The total order, and the only reason two reads of one unchanged catalog
    # agree about which page a row is on. ADR-0034 refuses to mint a cursor for
    # a keyset that does not end in the primary key.
    return (ordered, TitleRow.id.asc())


def _browse_after(
    key: ColumnElement[object], *, descending: bool, after: BrowseCursorPosition
) -> ColumnElement[bool]:
    """ADR-0034's keyset predicate, for the order `_browse_order` builds:
    NULLs last, then the key, then `id`.

    **Not the row comparison the ADR first carried, and that is a
    measurement.** `((k IS NOT NULL), k, id) > ((:ak IS NOT NULL), :ak, :aid)`
    reads well and is wrong for a nullable key: Postgres evaluates a row
    comparison element-wise and answers **NULL**, not false, when the first
    differing pair involves one. Measured on `pgvector/pgvector:pg17` over
    five rows of which three have a NULL key, resuming from the first of
    those: the row form returns the two *keyed* rows and neither remaining
    unkeyed one -- so a page walk drops the whole tail of the unkeyed group
    while every page it served looked full. ADR-0034 now carries the table.

    So the NULL boundary is its own branch, which is what
    `IS NOT DISTINCT FROM` would spell in one expression. The two branches
    together are still one rule -- everything strictly after `after` in the
    order above -- and the `ORDER BY` is built from the same `key` and the
    same `descending`.

    Strict `>` on the `id` tail: relaxed to `>=` the walk re-serves its
    boundary row at every page break.
    """
    if after.key is None:
        # The boundary is inside the unkeyed group, which sorts last, so only
        # the rest of that group can follow it.
        return and_(key.is_(None), TitleRow.id > after.id)
    later = key < after.key if descending else key > after.key
    return or_(
        # Every unkeyed row follows every keyed one -- NULLS LAST. This is the
        # leg a row comparison loses.
        key.is_(None),
        later,
        and_(key == after.key, TitleRow.id > after.id),
    )


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
        # `_WITHOUT_DERIVED_COLUMNS` for the reason `list_stale` carries its
        # own deferral: `titles` holds a tsvector roughly the size of the
        # document it indexes, and a ranked result set has no use for it or for
        # the cast names beside it, so without the deferrals every search ships
        # both per hit for nothing. `_to_domain` filters `DERIVED_COLUMNS`
        # before touching either, so nothing legitimate trips the `raiseload`.
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
                .options(*_WITHOUT_DERIVED_COLUMNS)
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
        statement = select(TitleRow).options(*_WITHOUT_DERIVED_COLUMNS).where(owned)
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
        # **The sort is over the whole catalog, so what enters it is the key
        # and not the row.** This statement outer-joins 1,271,138 titles to a
        # `DISTINCT` over `media_items`, anti-joins `watch_states`, orders on
        # four expressions and keeps `limit` of them -- and selecting the
        # entity here put thirty-two of the table's thirty-three columns into
        # that sort's working set, `overview`, `keywords` and
        # `field_provenance` included, to answer a caller that reads `name`,
        # `year`, `genres` and `id`. Ranking on
        # `titles.id` and joining the entity back onto the survivors is the
        # same answer over ~200 rows of payload instead of the catalog's text.
        #
        # **The three sort keys are projected alongside the id rather than
        # recomputed outside**, which is what keeps the order *identical*
        # rather than merely similar: the outer `ORDER BY` reads back the same
        # values the ranking stage sorted on, so there is no second evaluation
        # to disagree. Re-stating the expressions outside would also mean
        # re-joining `owned_titles`, i.e. a second `DISTINCT` over
        # `media_items` -- paying twice for the thing this change is about.
        ranked = (
            select(
                TitleRow.id.label("id"),
                owned.label("owned"),
                affine.label("affine"),
                TitleRow.vote_count.label("vote_count"),
            )
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
            .subquery("ranked")
        )
        statement = (
            select(TitleRow)
            .options(*_WITHOUT_DERIVED_COLUMNS)
            # An inner join, and it neither adds nor drops a row: `ranked.id`
            # is `titles.id`, so it is unique and every value in it named a
            # title a moment ago. The ownership join stays a `LEFT JOIN` inside
            # `ranked` because there it is a *key* -- an inner join there would
            # silently make the pool the library.
            .join(ranked, ranked.c.id == TitleRow.id)
            # **Repeated, because a join promises no order** -- and this is a
            # measurement rather than the caution it was written as. Deleting
            # these four keys fails **nine of the thirteen** candidate contract
            # cases on the Postgres arm and the shape case beside them, so on
            # this fixture the join really does emit the survivors in an order
            # of its own. What the fixture cannot promise is that it always
            # will, at any row count and under any plan, which is why the shape
            # case asserts the clause is *there* rather than only asserting the
            # order it produces.
            .order_by(
                ranked.c.owned.desc(),
                ranked.c.affine.desc(),
                nulls_last(ranked.c.vote_count.desc()),
                ranked.c.id,
            )
        )
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(statement)
        return [_to_domain(row) for row in result.scalars().all()]

    async def browse(
        self,
        *,
        sort: BrowseSort,
        genre: str | None = None,
        year: int | None = None,
        owned: bool | None = None,
        after: BrowseCursorPosition | None = None,
        limit: int,
    ) -> list[Title]:
        # `order_for` raises `FilterNotSupported` before a statement is built,
        # which is the whole of the port's "an ignored sort answers with more
        # rows in some other order" argument.
        column, descending = BrowseSort.order_for(sort)
        key: ColumnElement[object] = getattr(TitleRow, column)
        statement = (
            select(TitleRow)
            .options(*_WITHOUT_DERIVED_COLUMNS)
            .where(*_browse_filters(genre=genre, year=year, owned=owned))
        )
        if after is not None:
            statement = statement.where(_browse_after(key, descending=descending, after=after))
        # `NULLS LAST` rather than the `(key IS NOT NULL) DESC` this shipped as
        # -- identical order, 317x on `sort=name`, because only one of the two
        # is a sort key an index can serve. `_browse_order` carries the
        # measurement and what the legibility it replaced was worth.
        statement = statement.order_by(*_browse_order(key, descending=descending)).limit(limit)
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(statement)
        return [_to_domain(row) for row in result.scalars().all()]

    async def browse_facets(
        self,
        *,
        genre: str | None = None,
        year: int | None = None,
        owned: bool | None = None,
    ) -> BrowseFacets:
        # **`genre=None` and `year=None` are each facet dropping its own
        # predicate**, and the two calls are what make that visible: the genre
        # facet keeps `year` and `owned`, the year facet keeps `genre` and
        # `owned`. Folded back onto itself, a facet counts the page already on
        # screen and looks correct on every request that does not use it.
        #
        # `unnest` inside a subquery rather than beside the `GROUP BY`: a
        # set-returning function is legal in a target list and a `GROUP BY`
        # over its output needs somewhere to name it. A title whose `genres`
        # is `'{}'` unnests to no rows and is therefore in no bucket, which is
        # the same statement the `years` read makes with `IS NOT NULL`.
        unnested = (
            select(func.unnest(TitleRow.genres).label("genre"))
            .where(*_browse_filters(genre=None, year=year, owned=owned))
            .subquery("browse_genres")
        )
        with self._session.no_autoflush:  # see get()'s comment
            genre_rows = await self._session.execute(
                select(unnested.c.genre, func.count()).group_by(unnested.c.genre)
            )
            year_rows = await self._session.execute(
                select(TitleRow.year, func.count())
                .where(
                    TitleRow.year.is_not(None),
                    *_browse_filters(genre=genre, year=None, owned=owned),
                )
                .group_by(TitleRow.year)
            )
        genres = _canonical_facet(genre_rows.all())
        years = {value: count for value, count in year_rows.all()}
        # `count_by_state`'s "never a sparse dict", narrowed to the keys the
        # request itself named because a genre vocabulary is open. A `GROUP BY`
        # returns only the values with rows, and an absent facet is
        # indistinguishable from a filter the client did not send.
        #
        # Under the concept's own key, never the spelling the client sent: a
        # client that filtered on `Sci-Fi` and got `{"Sci-Fi": 0}` back beside a
        # map written in canonical labels has an entry that is both absent and
        # present depending on how it looks.
        if genre is not None:
            for canonical in canonical_genres(genre):
                genres.setdefault(canonical, 0)
        if year is not None:
            years.setdefault(year, 0)
        return BrowseFacets(genres=genres, years=years)

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow.enrichment_state, func.count()).group_by(TitleRow.enrichment_state)
            )
        counts = dict.fromkeys(EnrichmentState, 0)
        counts.update({EnrichmentState(state): count for state, count in result.all()})
        return counts

    async def list_genres_page(
        self, *, limit: int = 1000, after: uuid.UUID | None = None
    ) -> list[TitleGenres]:
        # A two-column projection off `pk_titles`, so the whole walk is an
        # index scan handing back `uuid` + `text[]` and nothing else. No
        # `_WITHOUT_DERIVED_COLUMNS` and no `_to_domain`: this is not an
        # entity read, which is the point of `TitleGenres` existing.
        statement = select(TitleRow.id, TitleRow.genres).order_by(TitleRow.id).limit(limit)
        if after is not None:
            statement = statement.where(TitleRow.id > after)
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(statement)
        # `ARRAY(Text)` always reads back as a `list`, never a tuple -- the
        # module docstring's note, and the reason `_to_row` emits lists. The
        # tuple is built here so a caller comparing against
        # `canonicalise_genres`' output compares values rather than container
        # types.
        return [TitleGenres(id=row.id, genres=tuple(row.genres)) for row in result.all()]

    async def replace_genres(self, rows: Sequence[TitleGenres]) -> int:
        # **No staging table, and that is a decision rather than an
        # omission.** `usher.db.staging` exists for `COPY`-sized batches and
        # costs DDL inside the transaction; this write is an `UPDATE` keyed
        # on the primary key, so there is no conflict target, no `ON
        # CONFLICT` predicate to repeat, and none of the three traps
        # `db/repositories/bulk.py` is built around. A `VALUES` join is the
        # whole statement.
        #
        # **`IS DISTINCT FROM` is the load-bearing clause**, not the `WHERE
        # id =`. Without it every row named is rewritten: `rowcount` becomes
        # the batch size, a second sweep over a normalised catalog reports
        # work it did not do, and 1.15M dead row versions are produced --
        # each of which also re-evaluates the `search_document` generated
        # column and its GIN index -- for no state change at all. Same
        # argument, same shape, as `_ENQUEUE`'s `AND jobs.priority <
        # excluded.priority`.
        #
        # An empty batch returns before touching the session: `UPDATE ...
        # FROM (VALUES)` with no rows is a syntax error rather than a no-op.
        if not rows:
            return 0
        source = values(
            column("id", Uuid),
            column("genres", PG_ARRAY(Text)),
            name="new_genres",
        ).data([(row.id, list(row.genres)) for row in rows])
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                update(TitleRow)
                .where(TitleRow.id == source.c.id)
                .where(TitleRow.genres.is_distinct_from(source.c.genres))
                .values(genres=source.c.genres)
                # The ORM would otherwise try to synchronise the session's
                # identity map against a multi-row UPDATE it cannot match
                # rows for. Nothing above this call holds a `TitleRow` for
                # these ids -- the sweep reads a projection.
                .execution_options(synchronize_session=False)
            )
        # `rowcount` is what the `WHERE` matched, and `IS DISTINCT FROM` is
        # *in* the `WHERE` -- so this is rows **changed**, never rows touched.
        # The cast is what `bulk.py:_rowcount` and
        # `PostgresCollectionRepository.link_title` both record: `rowcount`
        # lives on `CursorResult`, not on the `Result[Any]` that
        # `session.execute` is annotated to return.
        return cast(CursorResult[Any], result).rowcount
