"""In-memory TitleRepository, for services to be unit-tested against.

Lives outside `tests/unit/` deliberately: from M4 onward, service tests
import this the same way an adapter imports its port, and importing a test
*module* (`tests.unit.test_ports`) would drag in that module's fixtures and
parametrized tests along with it.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository

# Mirrors db/models/title.py's three partial unique indexes exactly, name
# for name -- this is what lets RepositoryConflict.constraint agree
# between this fake and the real, Postgres-backed repository (which reads
# its constraint name from asyncpg's own structured error fields; see
# title.py's _constraint_name). Checked in this fixed order so the fake is
# deterministic when a candidate conflicts on more than one field at once.
#
# tmdb_id's entry carries `kind_scoped=True`: its index is composite
# (tmdb_id, kind), so two rows sharing a tmdb_id across kinds do NOT
# conflict. ADR-0011.
_PROVIDER_ID_CONSTRAINTS: tuple[tuple[str, str, bool], ...] = (
    ("tmdb_id", "ix_titles_tmdb_id_kind", True),
    ("imdb_id", "ix_titles_imdb_id", False),
    ("tvdb_id", "ix_titles_tvdb_id", False),
)


def _provider_id_conflict(candidate: Title, other: Title) -> str | None:
    """The constraint name Postgres's own partial unique index would
    report for the first non-null tmdb_id, imdb_id, or tvdb_id `candidate`
    and `other` (a different row) share -- `None` if they don't conflict.

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


def _conflict(title_id: uuid.UUID, constraint: str) -> RepositoryConflict:
    """Same message shape as the real repository's title.py:_conflict --
    see that function's docstring for why it never claims `title_id`
    itself already exists."""
    return RepositoryConflict(
        f"title {title_id} conflicts with an existing title (constraint: {constraint})",
        constraint=constraint,
    )


@dataclass(frozen=True, slots=True)
class FakeWatchRow:
    """One `watch_states` row, as much of it as `list_unwatched_candidates`
    reads.

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
    """Keyed the same way the real Postgres-backed
    `PostgresTitleRepository` (Task 10) is: by id, with tmdb_id and
    imdb_id as secondary lookups. `add`/`update` mirror the real
    insert-only/update-only split documented on `TitleRepository` — a
    duplicate `add` raises `RepositoryConflict` and a missing-id `update`
    raises `RepositoryNotFound`, the same exceptions the real,
    Postgres-backed repository raises (translated from `IntegrityError`
    and a missing row respectively). A fake that raised anything else
    would defeat the point: a service unit-tested against this one must
    see the same failure shape it would see in production. That includes
    a duplicate tmdb_id/imdb_id/tvdb_id under a *different* id — the real
    repository's unique partial indexes reject that too (see
    `_provider_id_conflict`), so this fake does the same.

    **Where `list_unwatched_candidates` here is more forgiving than the
    statement, on purpose. Seven, each of which the paired
    `tests/integration/test_title_repository.py` run is what actually closes:**

    - **"No ordering at all" is not expressible.** `list.sort` is stable, so
      deleting a key -- or the whole `sort` call -- still answers in insertion
      order here, where the real statement's deleted `ORDER BY` answers in
      heap order. That is why every ordering case in
      `TitleRepositoryCandidateContract` is seeded worst-first: with a
      best-first fixture neither arm would notice.
    - **The roll-up is a mapping handed in, not a `LEFT JOIN episodes`.** The
      real one reaches a series through `episodes.title_id`; this has no
      episodes table, so `episode_series` is seeded by the caller.
      `FakeWatchStateRepository` records the identical divergence for
      `list_recent`, and it is the reason the episode case is load-bearing in
      the integration run and merely available here.
    - **The genre overlap is a Python set intersection.** `&&` on the generic
      `ARRAY(Text)` these columns are declared with is exactly the shape
      `list_owned_by_tag` records raising `NotImplementedError` for its
      sibling `@>` -- an operator that fails at statement-build time against
      Postgres and cannot fail at all against a set.
    - **`NULLS LAST` is a two-part Python key, so the two agree only because
      both were written to.** Postgres defaults a `DESC` sort to NULLS FIRST
      and Python raises on comparing `None`; the tempting repair on either
      side (`or 0`, or the default) is a wrong answer rather than a crash.
    - **No `users` table and no foreign key**, so a `user_id` naming no
      household is accepted here and is a `ForeignKeyViolationError` there --
      which is why the integration arm's `user_id` fixture writes a real row
      rather than minting a bare id.
    - **`media_items.available` is not modelled at all.** `available_copies`
      holds the *available* half by construction, so a retracted copy leaves
      no trace and the statement's `WHERE available` predicate is unobservable
      from here. `test_a_copy_the_source_has_retracted_does_not_rank_as_owned`
      is therefore load-bearing only in the integration run, and the
      corresponding mutation survived the whole suite until that case existed.
    - **`available_copies` cannot tell an episode's copy from a title's**, so
      the divergence from `owned_title_ids` -- no `episode_id IS NULL` bound
      -- is likewise Postgres-only. The list stores an episode id for the
      episode case, which records the caller's intent and changes no answer
      here; only a real `media_items` row carrying **both** ids (the
      production shape, per `ports/ingest.py`'s `MediaItemTarget`) can fail
      against a spurious bound.
    """

    def __init__(self) -> None:
        self._titles: dict[uuid.UUID, Title] = {}
        # `titles.credit_names`. Public because `CreditRepository` is what
        # writes it -- `credit_names` is not a `Title` field (DERIVED_COLUMNS)
        # and `update()` cannot reach it, which is the guarantee rather than
        # the cost -- so `FakeCreditRepository` is handed this dict the way
        # `FakeCollectionRepository` is handed a catalog. A test that writes
        # it directly is standing in for a derivation, not for the port.
        self.credit_names: dict[uuid.UUID, tuple[str, ...]] = {}
        # `media_items`, as much of it as `list_owned_by_tag` reads: a title
        # maps to the episode ids of its available copies, with `None` for a
        # title-level one. Public and seeded directly, the affordance
        # `FakeCollectionRepository.catalog` and `FakePersonRepository.
        # household` already are -- this fake models one table and the read
        # semi-joins another, so the alternative is a fake that answers
        # "unowned" for everything and a contract case that cannot be written.
        #
        # **Episode ids are modelled rather than collapsed to a bool**, and
        # that is the point of the shape: the real statement deliberately does
        # *not* carry `episode_id IS NULL`, so a series owned only through its
        # episodes is owned, and a fake holding a bare set could not tell that
        # implementation from the one that reports every series unowned.
        self.available_copies: dict[uuid.UUID, list[uuid.UUID | None]] = {}
        # `watch_states` and `episodes.title_id`, as much of the two as
        # `list_unwatched_candidates` reads. Public and seeded directly, the
        # same affordance `available_copies` above is and for the same reason:
        # this fake models one table and the read anti-joins two others, so
        # the alternative is a fake that answers "unwatched" for everything
        # and a set of contract cases that cannot be written.
        #
        # A list rather than a dict keyed on the household: the real statement
        # scans `watch_states` and a per-user dict would make the `user_id`
        # predicate structurally true here, which is exactly the divergence
        # that makes a case vacuous.
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
        # PostgresTitleRepository._to_row excludes both from the INSERT, so
        # the database's own server_default assigns them, never whatever
        # the caller's Title happened to carry (a stale retry, a
        # deliberately backdated import, ...). Stamping here, ignoring
        # title.created_at/title.updated_at entirely, is what makes this
        # fake agree -- verified divergence: before this, the fake
        # preserved the caller's values verbatim, including letting
        # update() overwrite created_at, which the real repository can
        # never do (see tests/contract/title_repository_contract.py's
        # test_created_at_is_not_taken_from_the_caller and
        # test_created_at_is_stable_across_updates).
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
        # created_at is carried over from the persisted row, never taken
        # from the incoming title -- same reasoning as add() above. Real
        # Postgres UPDATEs simply never mention the column (title.py's
        # update() explicitly excludes it from the copy loop), so it can't
        # move after insert; updated_at, in contrast, always advances on a
        # real write (the set_updated_at trigger / onupdate=func.now()),
        # so it's re-stamped here too rather than copied from `existing`.
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
        # Ids the store does not hold are simply absent, which is the port's
        # contract rather than this fake being lenient: a title deleted
        # between an index write and a search read is ordinary.
        #
        # Deliberately **not** returned in the order asked for. The real one
        # is a single `IN (...)` and promises no order at all, and a caller
        # that got insertion order from this fake would be relying on
        # something Postgres never said -- the same shape as `list_for_title`'s
        # tiebreak, which this module's siblings already document.
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
                title.popularity is None,
                -(title.popularity or 0.0),
                title.vote_count is None,
                -(title.vote_count or 0),
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
                title.vote_count is None,
                -(title.vote_count or 0),
                title.id,
            )
        )
        return candidates[: max(limit, 0)]

    def _played_title_ids(self, user_id: uuid.UUID) -> set[uuid.UUID]:
        """`COALESCE(ws.title_id, e.title_id)` for this household's played
        rows, as a dict lookup.

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

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        counts: dict[EnrichmentState, int] = dict.fromkeys(EnrichmentState, 0)
        for title in self._titles.values():
            counts[title.enrichment_state] += 1
        return counts
