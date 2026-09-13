"""Persistence for canonical titles."""

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
    text,
    update,
    values,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from usher.db.models.episode import EpisodeRow
from usher.db.models.source import MediaItemRow
from usher.db.models.title import DERIVED_COLUMNS, TitleRow
from usher.db.models.watch import WatchStateRow
from usher.db.repositories._errors import (
    constraint_name,
    is_row_refusal,
    refusals_as_conflict,
)
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.genres import canonical_genres, genre_spellings
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import (
    BrowseCursorPosition,
    BrowseFacets,
    BrowseSort,
    TitleGenres,
    TitleReference,
    TitleRepository,
)

# The natural-key ladder as one statement: `imdb_id`, then `(kind, tmdb_id)`, then the
# raw id, first hit wins.
_RESOLVE_NATURAL_KEYS = """
SELECT p.ord AS ord,
       COALESCE(
           (SELECT by_imdb.id FROM titles AS by_imdb WHERE by_imdb.imdb_id = p.imdb_id),
           (SELECT by_tmdb.id FROM titles AS by_tmdb
             WHERE by_tmdb.tmdb_id = p.tmdb_id AND by_tmdb.kind = p.kind),
           (SELECT by_raw.id FROM titles AS by_raw WHERE by_raw.id = p.raw_id)
       ) AS id
FROM unnest(
    CAST(:imdb_ids AS text[]),
    CAST(:kinds AS text[]),
    CAST(:tmdb_ids AS integer[]),
    CAST(:raw_ids AS uuid[])
) WITH ORDINALITY AS p(imdb_id, kind, tmdb_id, raw_id, ord)
"""


def _to_domain(row: TitleRow) -> Title:
    # `- DERIVED_COLUMNS`, not a hardcoded name: `Title` is `extra="forbid"`, so an
    # index artefact reaching this dict raises on *every read of every title*, in every
    # entry point.
    return Title.model_validate(
        {
            column.name: getattr(row, column.name)
            for column in TitleRow.__table__.columns
            if column.name not in DERIVED_COLUMNS
        }
    )


# The three bookkeeping columns update() has always excluded, plus every derived column.
_NOT_UPDATABLE = {"id", "created_at", "updated_at"} | DERIVED_COLUMNS


# The four ARRAY(Text) columns -- see the module docstring's note on
# ARRAY(Text) always reading back as a list, never a tuple.
_ARRAY_FIELDS = ("genres", "keywords", "spoken_languages", "origin_countries")


# **One deferral per member of `DERIVED_COLUMNS`, which is what makes `_to_domain`'s
# filter cost nothing.** That filter runs after the row has arrived: every derived
# column it drops was selected, detoasted, serialised and put on the wire first.
_WITHOUT_DERIVED_COLUMNS = (
    defer(TitleRow.search_document, raiseload=True),
    defer(TitleRow.credit_names, raiseload=True),
)


def _to_row(title: Title) -> TitleRow:
    # Emits lists for the four ARRAY columns, not the tuples Title actually types them
    # as.
    data = title.model_dump(exclude={"created_at", "updated_at"})
    for field in _ARRAY_FIELDS:
        data[field] = list(data[field])
    return TitleRow(**data)


def _browse_filters(
    *, genre: str | None, year: int | None, owned: bool | None
) -> list[ColumnElement[bool]]:
    """`browse`'s `WHERE`.

    built once so `browse_facets` can leave exactly one predicate out rather than re-
    read the filters.

    Two copies of a filter set is two chances for a facet to be counted over a
    population the page is not drawn from -- the same argument
    `_WITHOUT_DERIVED_COLUMNS` makes for looping over `DERIVED_COLUMNS`.
    """
    clauses: list[ColumnElement[bool]] = []
    if genre is not None:
        # **`&&` over every spelling of the concept, not `@>` over the one string the
        # client sent** (ADR-0039).
        clauses.append(
            TitleRow.genres.bool_op("&&")(sql_cast(list(genre_spellings(genre)), PG_ARRAY(Text)))
        )
    if year is not None:
        clauses.append(TitleRow.year == year)
    if owned is not None:
        # **`episode_id IS NULL` and `available`, one leg from each of this codebase's
        # two readings of "owned".** The port's docstring carries the argument; the
        # short version is that browse is a title-level screen (so a series' episode
        # files are not this row's copy) and its filter answers "what can I play" (so a
        # retracted copy is not one).
        copy = exists().where(
            MediaItemRow.title_id == TitleRow.id,
            MediaItemRow.episode_id.is_(None),
            MediaItemRow.available.is_(True),
        )
        clauses.append(copy if owned else ~copy)
    return clauses


def _canonical_facet(rows: Sequence[Row[tuple[str, int]]]) -> dict[str, int]:
    """`browse_facets`' genre counts, one entry per concept rather than one per spelling."""
    counts: dict[str, int] = {}
    for label, count in rows:
        for canonical in canonical_genres(label):
            counts[canonical] = counts.get(canonical, 0) + count
    return counts


def _browse_order(key: ColumnElement[Any], *, descending: bool) -> tuple[ColumnElement[Any], ...]:
    """`key <dir> NULLS LAST, id ASC` -- browse's total order."""
    ordered = nulls_last(key.desc()) if descending else nulls_last(key.asc())
    # The total order, and the only reason two reads of one unchanged catalog
    # agree about which page a row is on. ADR-0034 refuses to mint a cursor for
    # a keyset that does not end in the primary key.
    return (ordered, TitleRow.id.asc())


def _browse_after(
    key: ColumnElement[object], *, descending: bool, after: BrowseCursorPosition
) -> ColumnElement[bool]:
    """ADR-0034's keyset predicate, for the order `_browse_order` builds.

    NULLs last, then the key, then `id`.
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
    """Builds an accurate `RepositoryConflict` for `add()`/`update()` alike.

    Deliberately never claims `title_id` itself already exists -- measured bug this
    replaced: the message used to read "title {id} already exists" unconditionally,
    which is false whenever the actual collision was on a *different* row's
    tmdb_id/imdb_id/tvdb_id (`id` doesn't pre-exist at all in that case; the provider id
    does). "conflicts with an existing title" is true either way -- `title_id`'s own id
    collided, or one of its provider ids did -- and `constraint` carries the specific,
    structured answer for a caller that needs to branch on which.
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
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9
            # (ADR-0044).** `titles` carries `tmdb_id`/`tvdb_id` (`integer`, unbounded
            # above on `Title`), `original_language` (`varchar(16)`) and
            # `content_rating` (`varchar(32)`) -- and `usher.domain` declares no
            # `max_length` anywhere, so nothing above this layer bounds either string.
            if not is_row_refusal(exc):
                raise
            # Postgres's own unique-violation on a duplicate id (or a duplicate
            # tmdb_id/imdb_id/tvdb_id), translated so callers depend only on
            # usher.ports.errors -- importing sqlalchemy.exc here would break "db is
            # driven, not driving".
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
                # session.get() lives *inside* the try and the SAVEPOINT, not just the
                # flush below -- it autoflushes by default (SQLAlchemy's
                # load_on_pk_identity, no_autoflush=False), so it can just as easily be
                # the statement that surfaces some *other*, unrelated pending row's
                # IntegrityError on this shared session (verified: session.execute()/
                row = await self._session.get(TitleRow, title.id)
                if row is None:
                    raise RepositoryNotFound(f"no existing title {title.id} to update")
                # The mutation happens *inside* the SAVEPOINT scope, not before it --
                # verified directly that mutating `row` first and only wrapping flush()
                # leaves the session's outer transaction DEACTIVE after a caught
                # conflict (a second, unrelated call then raises PendingRollbackError
                # instead of succeeding).
                for column in TitleRow.__table__.columns:
                    if column.name not in _NOT_UPDATABLE:
                        setattr(row, column.name, getattr(fresh, column.name))
                await self._session.flush()
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9
            # (ADR-0044).** The same four columns as `add`, on the path TMDb enrichment
            # actually takes.
            if not is_row_refusal(exc):
                raise
            # Same translation and same SAVEPOINT reasoning as add() -- see
            # the module docstring. Retargeting tmdb_id/imdb_id/tvdb_id to a
            # value another title already holds raises IntegrityError here
            # today (verified), not just in some future schema change.
            raise _conflict(title.id, constraint_name(exc)) from exc

    async def get(self, title_id: uuid.UUID) -> Title | None:
        # no_autoflush: a plain read has no business flushing anything, and by default
        # it would anyway (session.get() autoflushes) -- so a pre-existing, unflushed,
        # invalid row left on this *shared* session by unrelated code could otherwise
        # fail right here, as a raw sqlalchemy.exc.IntegrityError this method has no way
        # to translate meaningfully (it isn't this read's conflict to report).
        with self._session.no_autoflush:
            row = await self._session.get(TitleRow, title_id)
        return _to_domain(row) if row else None

    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        # tmdb_id's own type is `int`, not `int | None` -- but a caller holding a
        # genuinely optional value (e.g.
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
                # Two columns, never the whole row: the caller wants a `Credit.title_id`
                # and a link target, not 31 columns per title.
                select(TitleRow.tmdb_id, TitleRow.id).where(
                    TitleRow.tmdb_id.in_(set(tmdb_ids)), TitleRow.kind == kind
                )
            )
        # `tmdb_id` is nullable on the row and the predicate above excludes
        # NULL by construction, so the narrowing is a type-checker fact
        # rather than a runtime branch.
        return {tmdb_id: title_id for tmdb_id, title_id in rows.all() if tmdb_id is not None}

    async def resolve_natural_keys(
        self, references: Sequence[TitleReference]
    ) -> dict[TitleReference, uuid.UUID]:
        # No statement at all for an empty batch -- `resolve_tmdb_ids`' guard,
        # for its reason: an empty `unnest` is a round trip to learn nothing.
        if not references:
            return {}
        # Deduplicated before the bind, not after: a household's watch states name the
        # same title once per state, so the probe list is the distinct set and the
        # answer is re-expanded by the caller.
        unique = list(dict.fromkeys(references))
        with self._session.no_autoflush:  # see get()'s comment
            rows = (
                await self._session.execute(
                    text(_RESOLVE_NATURAL_KEYS),
                    {
                        "imdb_ids": [one.imdb_id for one in unique],
                        "kinds": [one.kind.value for one in unique],
                        "tmdb_ids": [one.tmdb_id for one in unique],
                        "raw_ids": [one.id for one in unique],
                    },
                )
            ).all()
        # A reference the target does not hold comes back with a NULL `id`
        # (every rung of the `COALESCE` answered nothing), and is dropped rather than
        # mapped to `None`: the port says absent means "not held", and
        # `usher.db.backup_identity.resolve_titles` is what turns that into a
        # named refusal a caller can count.
        return {unique[row.ord - 1]: row.id for row in rows if row.id is not None}

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
        # One statement for a whole result set.
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
        # **The ownership semi-join is inside the statement**, which is the whole reason
        # this method exists rather than the caller filtering a catalog read: taking the
        # twenty most popular horror films from 1.27M rows and *then* asking which are
        # owned answers nothing on a normal household.
        owned = exists().where(
            MediaItemRow.title_id == TitleRow.id,
            MediaItemRow.available.is_(True),
        )
        statement = select(TitleRow).options(*_WITHOUT_DERIVED_COLUMNS).where(owned)
        # `@>` written out, because `ARRAY.contains()` raises `NotImplementedError` on
        # the *generic* `ARRAY` these columns are declared with -- only the dialect-
        # specific type implements it, and the model declares the generic one
        # deliberately (it is what makes M2's bulk path and the ORM agree).
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
            # FIRST, and `titles.tmdb_popularity` was measured NULL on all
            # 1,271,138 rows of a bootstrap-only catalog -- so the default
            # puts the entire unknown population above every known one.
            nulls_last(TitleRow.tmdb_popularity.desc()),
            nulls_last(TitleRow.tmdb_vote_count.desc()),
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
        # **Ownership is a LEFT JOIN here and an `EXISTS` in `list_owned_by_tag`, and
        # the difference is where it sits in the statement.** There it is a `WHERE`
        # predicate over an already-narrow candidate set, which Postgres plans as a
        # semi-join and which short-circuits.
        owned_titles = (
            select(MediaItemRow.title_id)
            .where(MediaItemRow.available.is_(True), MediaItemRow.title_id.is_not(None))
            .distinct()
            .subquery("owned_titles")
        )
        owned = owned_titles.c.title_id.is_not(None)
        # The exclusion, and it is `played_title_ids`' predicate: `played` rather than
        # "has a watch state", and `COALESCE(ws.title_id, e.title_id)` so a watched
        # episode takes its series with it.
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
        # `&&` written out for the reason `list_owned_by_tag` writes `@>` out: the
        # generic `ARRAY` these columns are declared with implements neither operator
        # through SQLAlchemy's own helpers, and the model declares the generic one
        # deliberately.
        affine = TitleRow.genres.bool_op("&&")(sql_cast(list(genres), PG_ARRAY(Text)))
        # **The sort is over the whole catalog, so what enters it is the key and not the
        # row.** This statement outer-joins 1,271,138 titles to a `DISTINCT` over
        # `media_items`, anti-joins `watch_states`, orders on four expressions and keeps
        # `limit` of them -- and selecting the entity here put thirty-two of the table's
        # thirty-three columns into that sort's working set, `overview`, `keywords` and
        ranked = (
            select(
                TitleRow.id.label("id"),
                owned.label("owned"),
                affine.label("affine"),
                TitleRow.tmdb_vote_count.label("tmdb_vote_count"),
            )
            .outerjoin(owned_titles, owned_titles.c.title_id == TitleRow.id)
            .where(~watched)
            .order_by(
                owned.desc(),
                affine.desc(),
                # `nulls_last` spelled out: Postgres defaults a DESC sort to
                # NULLS FIRST, and on a bootstrap-only catalog every row's
                # `tmdb_vote_count` can be NULL -- so the default would put the
                # unknown population above the known one and then let the
                # `id` tail decide the pool.
                nulls_last(TitleRow.tmdb_vote_count.desc()),
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
            # measurement rather than the caution it was written as.
            .order_by(
                ranked.c.owned.desc(),
                ranked.c.affine.desc(),
                nulls_last(ranked.c.tmdb_vote_count.desc()),
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
        # **`genre=None` and `year=None` are each facet dropping its own predicate**,
        # and the two calls are what make that visible: the genre facet keeps `year` and
        # `owned`, the year facet keeps `genre` and `owned`.
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
        # `count_by_state`'s "never a sparse dict", narrowed to the keys the request
        # itself named because a genre vocabulary is open.
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
        # **No staging table, and that is a decision rather than an omission.**
        # `usher.db.staging` exists for `COPY`-sized batches and costs DDL inside the
        # transaction; this write is an `UPDATE` keyed on the primary key, so there is
        # no conflict target, no `ON CONFLICT` predicate to repeat, and none of the
        # three traps `db/repositories/bulk.py` is built around.
        if not rows:
            return 0
        source = values(
            column("id", Uuid),
            column("genres", PG_ARRAY(Text)),
            name="new_genres",
        ).data([(row.id, list(row.genres)) for row in rows])
        # **`refusals_as_conflict`, added by M10's F9 (ADR-0044).** This statement is a
        # bare parameterised `UPDATE` over a `VALUES` join -- it computes nothing
        # server-side, so class 22 here can only be about a value the caller handed in,
        # which is the precondition `_errors.py:66-75` states for `is_row_refusal`'s own
        # claim.
        async with refusals_as_conflict(
            self._session, "a genre batch violates the catalog's own bounds"
        ):
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
        # `rowcount` is what the `WHERE` matched, and `IS DISTINCT FROM` is *in* the
        # `WHERE` -- so this is rows **changed**, never rows touched.
        return cast(CursorResult[Any], result).rowcount
