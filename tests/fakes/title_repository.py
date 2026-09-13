"""In-memory TitleRepository, for services to be unit-tested against."""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

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

# Mirrors db/models/title.py's three partial unique indexes exactly, name for name --
# this is what lets RepositoryConflict.constraint agree between this fake and the real,
# Postgres-backed repository (which reads its constraint name from asyncpg's own
# structured error fields; see title.py's _constraint_name).
_PROVIDER_ID_CONSTRAINTS: tuple[tuple[str, str, bool], ...] = (
    ("tmdb_id", "ix_titles_tmdb_id_kind", True),
    ("imdb_id", "ix_titles_imdb_id", False),
    ("tvdb_id", "ix_titles_tvdb_id", False),
)


def _provider_id_conflict(candidate: Title, other: Title) -> str | None:
    """The constraint name Postgres's own partial unique index would report for the first.

    non-null tmdb_id, imdb_id, or tvdb_id `candidate` and `other` (a different row)
    share -- `None` if they don't conflict.

    Mirrors `db/models/title.py`'s three partial unique indexes
    (`ix_titles_tmdb_id_kind`/`ix_titles_imdb_id`/`ix_titles_tvdb_id` —
    unique only where the column `IS NOT NULL`, so many rows may share a
    null provider id) — without this, the fake would let a service add or
    update two rows onto the same TMDb/IMDb/TVDB title in a unit test,
    while the real, Postgres-backed repository rejects the identical call
    with `RepositoryConflict`. That divergence would only surface in
    production, which is exactly what a fake exists to prevent.
    """
    for field, constraint, kind_scoped in _PROVIDER_ID_CONSTRAINTS:
        value = getattr(candidate, field)
        if value is None or value != getattr(other, field):
            continue
        if kind_scoped and candidate.kind is not other.kind:
            continue
        return constraint
    return None


def resolve_title_reference(
    wanted: TitleReference, stored: Iterable[TitleReference]
) -> uuid.UUID | None:
    """`usher.db.backup_identity.RESOLUTION_ORDER`, in Python.

    `imdb_id`, then `(kind, tmdb_id)`, then the raw id, first hit wins.

    **One definition, imported by `FakeEpisodeRepository` rather than
    re-spelled there.** Both fakes resolve a `TitleReference` -- the episode
    one because `resolve_natural_keys` there does the series and the episode
    in a single statement against Postgres, so a fake resolving only the
    numbers would answer a question the port does not ask. Two copies of a
    three-rung ladder are two chances for one to lose a rung, and the
    divergence would be invisible: the *title* contract cases would still
    pass, and only an episode case seeded through the rung that went missing
    could see it.

    Case-exact on `imdb_id` (`==`, never `casefold`), and `kind`-scoped on
    `tmdb_id` (ADR-0011) -- both of which the Postgres arm gets from
    Postgres's own `=` over `text` and from the composite join predicate.
    """
    if wanted.imdb_id is not None:
        for one in stored:
            if one.imdb_id == wanted.imdb_id:
                return one.id
    if wanted.tmdb_id is not None:
        for one in stored:
            if one.tmdb_id == wanted.tmdb_id and one.kind is wanted.kind:
                return one.id
    for one in stored:
        if one.id == wanted.id:
            return one.id
    return None


def _conflict(title_id: uuid.UUID, constraint: str) -> RepositoryConflict:
    """Same message shape as the real repository's title.py:_conflict.

    see that function's docstring for why it never claims `title_id` itself already
    exists.
    """
    return RepositoryConflict(
        f"title {title_id} conflicts with an existing title (constraint: {constraint})",
        constraint=constraint,
    )


@dataclass(frozen=True, slots=True)
class FakeWatchRow:
    """One `watch_states` row, as much of it as `list_unwatched_candidates` reads.

    **Both targets are modelled rather than collapsed to a title id**, for
    `available_copies`' reason one table over: the real statement rolls a
    watched episode up through `episodes.title_id`, and a fake holding
    already-rolled-up title ids could not tell that implementation from the
    one that answers films-only on a library that is 89% episodes.

    `played` is a field rather than a filter applied on the way in, because
    "has a watch state" is the wrong predicate this read has to rule out and a
    store holding only played rows could not express it.
    """

    user_id: uuid.UUID
    title_id: uuid.UUID | None
    episode_id: uuid.UUID | None
    played: bool


class FakeTitleRepository(TitleRepository):
    """Keyed the same way the real Postgres-backed `PostgresTitleRepository` (Task 10) is.

    by id, with tmdb_id and imdb_id as secondary lookups.
    """

    def __init__(self) -> None:
        self._titles: dict[uuid.UUID, Title] = {}
        # `titles.credit_names`.
        self.credit_names: dict[uuid.UUID, tuple[str, ...]] = {}
        # `media_items`, as much of it as `list_owned_by_tag` reads: a title maps to the
        # episode ids of its available copies, with `None` for a title-level one.
        self.available_copies: dict[uuid.UUID, list[uuid.UUID | None]] = {}
        # `watch_states` and `episodes.title_id`, as much of the two as
        # `list_unwatched_candidates` reads.
        self.watch_states: list[FakeWatchRow] = []
        # `episodes.title_id` -- the roll-up's other side, and the reason
        # `FakeWatchStateRepository` takes an `episode_series` mapping too. An
        # episode absent from this mapping is one whose series row is gone,
        # which is the state the real statement's `COALESCE` resolves to NULL.
        self.episode_series: dict[uuid.UUID, uuid.UUID] = {}

    async def add(self, title: Title) -> None:
        if title.id in self._titles:
            # "pk_titles" -- the real repository's own primary key
            # constraint name (db/base.py's naming convention: "pk_%(table_name)s").
            raise _conflict(title.id, "pk_titles")
        for other in self._titles.values():
            constraint = _provider_id_conflict(title, other)
            if constraint is not None:
                raise _conflict(title.id, constraint)
        # Postgres is the authoritative clock for created_at/updated_at --
        # PostgresTitleRepository._to_row excludes both from the INSERT, so the
        # database's own server_default assigns them, never whatever the caller's Title
        # happened to carry (a stale retry, a deliberately backdated import, ...).
        now = datetime.now(UTC)
        self._titles[title.id] = title.evolve(created_at=now, updated_at=now)

    async def update(self, title: Title) -> None:
        existing = self._titles.get(title.id)
        if existing is None:
            raise RepositoryNotFound(f"no title {title.id} to update")
        others = (t for tid, t in self._titles.items() if tid != title.id)
        for other in others:
            constraint = _provider_id_conflict(title, other)
            if constraint is not None:
                raise _conflict(title.id, constraint)
        # created_at is carried over from the persisted row, never taken from the
        # incoming title -- same reasoning as add() above.
        self._titles[title.id] = title.evolve(
            created_at=existing.created_at, updated_at=datetime.now(UTC)
        )

    async def get(self, title_id: uuid.UUID) -> Title | None:
        return self._titles.get(title_id)

    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        # Same guard, same reason, as PostgresTitleRepository.get_by_tmdb_id:
        # `title.tmdb_id == None` would match the first title with a null
        # tmdb_id instead of finding nothing, mirroring Postgres's own
        # `IS NULL` behaviour for the same comparison -- see that method's
        # comment. The `kind` filter mirrors ix_titles_tmdb_id_kind.
        if tmdb_id is None:
            return None
        for title in self._titles.values():
            if title.tmdb_id == tmdb_id and title.kind is kind:
                return title
        return None

    async def resolve_tmdb_ids(
        self, kind: TitleKind, tmdb_ids: Sequence[int]
    ) -> dict[int, uuid.UUID]:
        # The `kind` filter mirrors ix_titles_tmdb_id_kind and is half the
        # key, not a narrowing: 26,968 measured TMDb ids are live in both
        # spaces. An id this store does not hold is simply absent -- the
        # port's contract, because `raw_payloads` outlives `titles`.
        wanted = set(tmdb_ids)
        return {
            title.tmdb_id: title.id
            for title in self._titles.values()
            if title.tmdb_id in wanted and title.kind is kind and title.tmdb_id is not None
        }

    async def resolve_natural_keys(
        self, references: Sequence[TitleReference]
    ) -> dict[TitleReference, uuid.UUID]:
        # The ladder is `resolve_title_reference` -- one definition, shared with
        # `FakeEpisodeRepository`, which resolves the same references because its own
        # `resolve_natural_keys` does the series and the episode in one statement
        # against Postgres.
        stored = tuple(
            TitleReference(
                kind=title.kind, id=title.id, imdb_id=title.imdb_id, tmdb_id=title.tmdb_id
            )
            for title in self._titles.values()
        )
        found = {}
        for reference in references:
            resolved = resolve_title_reference(reference, stored)
            if resolved is not None:
                found[reference] = resolved
        return found

    async def credit_names_for(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, ...]]:
        # An empty tuple for a title that exists and has no credits, absent
        # for one that does not exist at all. The two are different answers
        # and the composer's positional assembly depends on the difference.
        return {
            title_id: self.credit_names.get(title_id, ())
            for title_id in title_ids
            if title_id in self._titles
        }

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        if imdb_id is None:
            return None
        for title in self._titles.values():
            if title.imdb_id == imdb_id:
                return title
        return None

    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        # Ids the store does not hold are simply absent, which is the port's contract
        # rather than this fake being lenient: a title deleted between an index write
        # and a search read is ordinary.
        wanted = set(title_ids)
        return [title for title in self._titles.values() if title.id in wanted]

    def stored(self) -> list[Title]:
        """Every title held, for `FakeTitleMatchRepository` to read through.

        Not a port method. The two fakes model *one* table -- a real
        `TitleRepository.add` flushes, so the row is visible to the next
        `TitleMatchRepository` read on the same session -- and keeping two
        independent dicts made a correct `MatchService` fail on the second
        walk of a series it had itself stubbed: the ladder missed, the
        re-create conflicted, and nothing could look the winner up. See that
        fake's own docstring.
        """
        return list(self._titles.values())

    async def list_owned_by_tag(
        self,
        *,
        genre: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
    ) -> list[Title]:
        if genre is None and keyword is None:
            # The port's refusal, reproduced rather than inherited: an
            # unpredicated call is the popular-titles fallback as a query.
            return []
        matching = [
            title
            for title in self._titles.values()
            if self.available_copies.get(title.id)
            and (genre is None or genre in title.genres)
            and (keyword is None or keyword in title.keywords)
        ]
        # `NULLS LAST` under a descending sort, spelled as a two-part key --
        # the tempting `key=lambda t: t.popularity` raises on a None and the
        # tempting repair `or 0.0` sorts an unknown above a genuinely
        # unpopular title, which is the wrong answer rather than a crash.
        matching.sort(
            key=lambda title: (
                title.tmdb_popularity is None,
                -(title.tmdb_popularity or 0.0),
                title.tmdb_vote_count is None,
                -(title.tmdb_vote_count or 0),
                title.id,
            )
        )
        return matching[: max(limit, 0)]

    async def list_unwatched_candidates(
        self,
        user_id: uuid.UUID,
        *,
        genres: Sequence[str] = (),
        limit: int,
    ) -> list[Title]:
        affine = set(genres)
        seen = self._played_title_ids(user_id)
        candidates = [title for title in self._titles.values() if title.id not in seen]
        # The port's four keys, in order. `NULLS LAST` under a descending
        # sort is spelled as a two-part key for `list_owned_by_tag`'s reason:
        # `-(vote_count or 0)` sorts an unknown count above a genuinely
        # unpopular title, which is a wrong answer rather than a crash.
        candidates.sort(
            key=lambda title: (
                not self.available_copies.get(title.id),
                not affine.intersection(title.genres),
                title.tmdb_vote_count is None,
                -(title.tmdb_vote_count or 0),
                title.id,
            )
        )
        return candidates[: max(limit, 0)]

    def _played_title_ids(self, user_id: uuid.UUID) -> set[uuid.UUID]:
        """`COALESCE(ws.title_id.

        e.title_id)` for this household's played rows, as a dict lookup.

        An episode this fake has no `episode_series` entry for resolves to
        `None` and is dropped, which is what the real statement's `COALESCE`
        does with an episode whose series row is gone.
        """
        played: set[uuid.UUID] = set()
        for row in self.watch_states:
            if row.user_id != user_id or not row.played:
                continue
            title_id = row.title_id
            if title_id is None and row.episode_id is not None:
                title_id = self.episode_series.get(row.episode_id)
            if title_id is not None:
                played.add(title_id)
        return played

    def _owns_a_title_level_copy(self, title_id: uuid.UUID) -> bool:
        """`browse`'s `owned`: an **available, title-level** copy.

        `available_copies` stores `None` for a title-level copy and an episode
        id for an episode one, so `episode_id IS NULL` -- which browse carries
        and `list_owned_by_tag` deliberately does not -- is one of the two
        readings this fake *can* tell apart. `available` is the other, and it
        cannot: a retracted copy leaves no trace here at all.
        """
        return any(copy is None for copy in self.available_copies.get(title_id, []))

    def _browse_matches(
        self, title: Title, *, genre: str | None, year: int | None, owned: bool | None
    ) -> bool:
        """`browse`'s `WHERE`.

        shared with `browse_facets` so a facet is the same population minus one
        predicate rather than a second reading of the filters.

        The genre leg is the `&&`-over-every-spelling of ADR-0039, in Python:
        `titles.genres` unions two importers' vocabularies and the label the
        client sent is written in one of them, so plain membership answered
        half a concept. For any label outside the alias table the expansion is
        a one-element set and this collapses to the test it replaced.
        """
        if genre is not None and not set(genre_spellings(genre)) & set(title.genres):
            return False
        if year is not None and title.year != year:
            return False
        return not (owned is not None and self._owns_a_title_level_copy(title.id) is not owned)

    @staticmethod
    def _browse_ordered(rows: list[Title], *, column: str, descending: bool) -> list[Title]:
        """`(key IS NOT NULL) DESC, key <dir>, id`, in Python.

        Two lists rather than one sort key, because a descending sort over a
        `str` key cannot be spelled by negating it and `reverse=True` would
        also reverse the `id` tail. Python's sort is stable and `reverse` does
        **not** reorder ties, so sorting by `id` first and by the key second
        leaves ties in ascending id order under either direction -- which is
        the tail the real statement spells `TitleRow.id.asc()`.
        """
        keyed = sorted(
            (one for one in rows if getattr(one, column) is not None), key=lambda o: o.id
        )
        keyed.sort(key=lambda one: getattr(one, column), reverse=descending)
        unkeyed = sorted((one for one in rows if getattr(one, column) is None), key=lambda o: o.id)
        # NULLs last, which is the opposite of Postgres's own `DESC` default
        # and the reason the real statement spells the leg out.
        return keyed + unkeyed

    @staticmethod
    def _browse_after(
        title: Title, after: BrowseCursorPosition, *, column: str, descending: bool
    ) -> bool:
        """The keyset predicate, arm for arm with the Postgres one.

        The `after.key is None` branch is what the SQL spells
        `key IS NOT DISTINCT FROM :after_key`: a boundary inside the unkeyed
        group can only be followed by the rest of that group, and comparing
        the key at all would answer "unknown" there -- which in SQL is not
        true, so every remaining unkeyed row would be dropped.
        """
        key = getattr(title, column)
        if after.key is None:
            return bool(key is None and title.id > after.id)
        if key is None:
            # NULLs sort last, so every unkeyed row follows every keyed one.
            return True
        if key == after.key:
            return bool(title.id > after.id)
        return bool(key < after.key if descending else key > after.key)

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
        column, descending = BrowseSort.order_for(sort)
        matching = [
            title
            for title in self._titles.values()
            if self._browse_matches(title, genre=genre, year=year, owned=owned)
        ]
        ordered = self._browse_ordered(matching, column=column, descending=descending)
        if after is not None:
            ordered = [
                title
                for title in ordered
                if self._browse_after(title, after, column=column, descending=descending)
            ]
        return ordered[: max(limit, 0)]

    async def browse_facets(
        self,
        *,
        genre: str | None = None,
        year: int | None = None,
        owned: bool | None = None,
    ) -> BrowseFacets:
        genres: dict[str, int] = {}
        for title in self._titles.values():
            # `genre=None`: the genre facet drops its **own** predicate and
            # keeps the other two.
            if self._browse_matches(title, genre=None, year=year, owned=owned):
                # One entry per **concept**, not per spelling (ADR-0039), and the
                # increment is per raw label rather than per title so this is the same
                # arithmetic as the Postgres arm's `GROUP BY` and `_canonical_facet` --
                # summing, with the measured premise that no title carries two spellings
                # of one concept.
                for name in title.genres:
                    for canonical in canonical_genres(name):
                        genres[canonical] = genres.get(canonical, 0) + 1
        years: dict[int, int] = {}
        for title in self._titles.values():
            if title.year is not None and self._browse_matches(
                title, genre=genre, year=None, owned=owned
            ):
                years[title.year] = years.get(title.year, 0) + 1
        # A value the request named is present at zero rather than absent --
        # `count_by_state`'s "never a sparse dict", narrowed to the keys the
        # request itself supplied because a genre vocabulary is open. Under the
        # concept's key, never the spelling the client sent.
        if genre is not None:
            for canonical in canonical_genres(genre):
                genres.setdefault(canonical, 0)
        if year is not None:
            years.setdefault(year, 0)
        return BrowseFacets(genres=genres, years=years)

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        counts: dict[EnrichmentState, int] = dict.fromkeys(EnrichmentState, 0)
        for title in self._titles.values():
            counts[title.enrichment_state] += 1
        return counts

    async def list_genres_page(
        self, *, limit: int = 1000, after: uuid.UUID | None = None
    ) -> list[TitleGenres]:
        # `ORDER BY id` explicitly, because a dict preserves insertion order
        # and the real read preserves id order -- and a sweep asserted against
        # insertion order here would be relying on something Postgres never
        # said. Same divergence `list_by_ids` documents, in the direction that
        # matters for a keyset cursor.
        ordered = sorted(self._titles.values(), key=lambda title: title.id)
        return [
            TitleGenres(id=title.id, genres=title.genres)
            for title in ordered
            if after is None or title.id > after
        ][:limit]

    async def replace_genres(self, rows: Sequence[TitleGenres]) -> int:
        # The real statement's `IS DISTINCT FROM` guard, in Python, so the
        # contract case about a re-run writing zero rows means the same thing
        # on both arms. `updated_at` is re-stamped for `update()`'s reason:
        # `titles` carries a `set_updated_at` trigger and every real write
        # moves the column.
        written = 0
        for row in rows:
            existing = self._titles.get(row.id)
            if existing is None or existing.genres == row.genres:
                continue
            self._titles[row.id] = existing.evolve(genres=row.genres, updated_at=datetime.now(UTC))
            written += 1
        return written
