"""In-memory `PersonRepository`.

**Where this is more forgiving than Postgres, on purpose.** Six places, each
of which the paired `tests/integration/test_person_repository.py` run is what
actually closes:

- **No foreign keys**, so `list_recurring_for_user` here reads dictionaries a
  test seeded rather than a join Postgres planned. The real one's arm through
  `episodes` is the half a fake cannot express structurally, which is why
  `test_an_episode_watch_state_reaches_its_series_credits` is fully meaningful
  only in the integration run -- here it passes if this module's own
  `_title_of` reproduces the coalesce. A divergence that makes a case vacuous
  is worse than one that makes it strict, so it is named first. It is
  reproduced rather than shortcut deliberately: a fake that stored the answer
  would make the case decorative on both sides.
- **It is a `dict` keyed on `tmdb_id`**, so a duplicate inside one batch is
  structurally last-wins. The real one raises `CardinalityViolationError`
  unless its staging read is `SELECT DISTINCT ON (tmdb_id)`.
- **A `None` `tmdb_id` is keyed on the person's own id**, not on `None`, which
  is this fake's easiest bug and what
  `test_two_people_with_no_tmdb_id_are_two_people` exists for. In Postgres the
  same property comes free from the unique index being *partial*.
- **The `COALESCE` rule is Python's `if value is not None`**, naturally that
  shape. In SQL it is one `COALESCE(excluded.x, people.x)` per column and a
  forgotten one is invisible until the field it guards is the one a pass
  blanks.
- **No CHECK constraints**: `ck_people_name_not_empty` and its sibling are
  enforced here only by `Person`'s pydantic bounds, which fire at a different
  moment with a different exception type.
- **`xmax = 0` has no analogue.** `inserted`/`updated` are computed from dict
  membership, which *is* the answer rather than a measurement of it. The real
  repository can only tell the two apart through `RETURNING (xmax = 0)`, and
  an implementation returning `(len(rows), 0)` passes every run here.

`calls` and `reset_calls()` are test-double affordances rather than port
methods, matching `FakeEpisodeRepository`: a case asserting a bounded number
of *round trips* cannot express that through the answers this fake returns.

`credits`, `watch_states` and `episode_titles` are the same kind of
affordance. They hold what `list_recurring_for_user` joins across in
Postgres, and `FakePersonHistorySeeder` is the only thing that writes them --
the port itself never does.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from usher.domain.people import CreditKind, Person
from usher.ports.repository import BulkWriteResult, PersonRepository, RecurringPerson

# Every field a derivation pass may legitimately not know. `name` and
# `sort_name` are absent deliberately: both are NOT NULL and always supplied,
# so preserving a stored one would make a corrected name unfixable -- which is
# `season_id`'s exception one table over.
_OPTIONAL = ("known_for_department",)


@dataclass(frozen=True, slots=True)
class SeededCredit:
    person_id: uuid.UUID
    title_id: uuid.UUID
    kind: CreditKind
    job: str | None
    # Not read by the grouping -- the real statement groups on
    # `(person_id, name, kind, job)` -- and modelled anyway, because that is
    # exactly why it matters: several credits differing only by `character`
    # land in ONE group, which is the only seeding that can tell
    # `count(*)` from `count(DISTINCT title_id)`.
    character: str | None = None


@dataclass(frozen=True, slots=True)
class SeededWatchState:
    """One `watch_states` row, with the CHECK the real table carries.

    `title_id` and `episode_id` are mutually exclusive --
    `ck_watch_states_exactly_one_target`, and
    `WatchStateSyncService._watch_target` collapses the pair with the
    **episode** winning, so an episode's row really is
    `(episode_id = ..., title_id = NULL)`.
    """

    user_id: uuid.UUID
    title_id: uuid.UUID | None
    episode_id: uuid.UUID | None
    played: bool


@dataclass
class _Household:
    credits: list[SeededCredit] = field(default_factory=list)
    watch_states: list[SeededWatchState] = field(default_factory=list)
    episode_titles: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)


class FakePersonRepository(PersonRepository):
    def __init__(self) -> None:
        self._by_tmdb_id: dict[int, Person] = {}
        self._anonymous: dict[uuid.UUID, Person] = {}
        self.household = _Household()
        self.calls = 0

    def reset_calls(self) -> None:
        self.calls = 0

    def stored(self, person_id: uuid.UUID) -> Person:
        for person in (*self._by_tmdb_id.values(), *self._anonymous.values()):
            if person.id == person_id:
                return person
        raise KeyError(person_id)

    async def upsert_many(self, people: Sequence[Person]) -> BulkWriteResult:
        self.calls += 1
        inserted = updated = 0
        # Last-wins deduplication, matching the real one's
        # `SELECT DISTINCT ON (tmdb_id) ... ORDER BY tmdb_id, ordinal DESC`.
        # One derivation pass spans many titles and a working actor is on
        # several of them, so this is the common case rather than the odd one.
        deduped: dict[int, Person] = {}
        anonymous: list[Person] = []
        for incoming in people:
            if incoming.tmdb_id is None:
                # Keyed on the person's own id, never on `None`: the unique
                # index is partial, so two people with no provider id are two
                # rows and there is no identity to compare them by.
                anonymous.append(incoming)
            else:
                deduped[incoming.tmdb_id] = incoming

        for tmdb_id, incoming in deduped.items():
            existing = self._by_tmdb_id.get(tmdb_id)
            if existing is None:
                self._by_tmdb_id[tmdb_id] = incoming
                inserted += 1
                continue
            changes: dict[str, object] = {
                "name": incoming.name,
                "sort_name": incoming.sort_name,
            }
            for name in _OPTIONAL:
                value = getattr(incoming, name)
                if value is not None:
                    changes[name] = value
            self._by_tmdb_id[tmdb_id] = existing.evolve(**changes)
            updated += 1

        for incoming in anonymous:
            self._anonymous[incoming.id] = incoming
            inserted += 1

        return BulkWriteResult(inserted=inserted, updated=updated)

    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        self.calls += 1
        # Absent means "no such person", never "not asked" -- so this is a
        # comprehension over what is stored rather than over what was asked.
        return {
            tmdb_id: self._by_tmdb_id[tmdb_id].id
            for tmdb_id in dict.fromkeys(tmdb_ids)
            if tmdb_id in self._by_tmdb_id
        }

    def _title_of(self, watch_state: SeededWatchState) -> uuid.UUID | None:
        """`coalesce(w.title_id, e.title_id)`, reproduced rather than
        shortcut.

        An episode's watch state carries `title_id IS NULL`; the series is on
        `episodes.title_id`. Storing the series id on the watch state instead
        would make `test_an_episode_watch_state_reaches_its_series_credits`
        decorative here as well as strict there.
        """
        if watch_state.title_id is not None:
            return watch_state.title_id
        if watch_state.episode_id is None:
            return None
        return self.household.episode_titles.get(watch_state.episode_id)

    async def list_recurring_for_user(
        self, user_id: uuid.UUID, *, min_titles: int = 2, limit: int = 10
    ) -> list[RecurringPerson]:
        self.calls += 1
        watched: set[uuid.UUID] = set()
        for state in self.household.watch_states:
            if state.user_id != user_id or not state.played:
                continue
            title_id = self._title_of(state)
            if title_id is not None:
                watched.add(title_id)

        # Grouped by (person, kind, job) exactly as the real statement's
        # GROUP BY is, and counting **distinct titles** rather than credits: a
        # person credited twice on one film reads as two titles otherwise, and
        # a one-film person out-ranks a four-film one.
        grouped: dict[tuple[uuid.UUID, CreditKind, str | None], set[uuid.UUID]] = {}
        for one in self.household.credits:
            if one.title_id not in watched:
                continue
            grouped.setdefault((one.person_id, one.kind, one.job), set()).add(one.title_id)

        rows = [
            RecurringPerson(
                person_id=person_id,
                name=self.stored(person_id).name,
                kind=kind,
                job=job,
                watched_title_count=len(titles),
            )
            for (person_id, kind, job), titles in grouped.items()
            if len(titles) >= min_titles
        ]
        # Ties break on person_id so two reads of one catalog agree.
        rows.sort(key=lambda row: (-row.watched_title_count, row.person_id))
        return rows[:limit]
