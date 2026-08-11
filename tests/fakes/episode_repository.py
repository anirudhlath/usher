"""In-memory `EpisodeRepository`.

**Where this is more forgiving than Postgres, on purpose.** Five places, each
of which the paired `tests/integration/test_episode_repository.py` run is
what actually closes:

- **No foreign keys**, so an episode here can name a `title_id` or a
  `season_id` no row carries. The real one raises and
  `PostgresEpisodeRepository` translates it, which is why
  `test_a_title_id_no_title_carries_is_a_port_error` is a Postgres-only case
  rather than a contract one -- a dict has nothing to violate.
- **It is a `dict` keyed on the natural key**, so a duplicate inside one
  batch is structurally last-wins. The real one raises
  `CardinalityViolationError` unless its staging read is
  `SELECT DISTINCT ON (...)`, so
  `test_a_duplicate_episode_inside_one_batch_is_tolerated` passes here for a
  reason that has nothing to do with the code under test.
- **The `COALESCE` rule is Python's `if value is not None`**, which is
  naturally that shape and is the same three lines whichever field it is
  applied to. In SQL it is one `COALESCE(excluded.x, episodes.x)` per column
  and a forgotten one is invisible until the field it guards is the one a
  walk blanks.
- **No CHECK constraints**: `ck_episodes_season_number_non_negative` and its
  three siblings are enforced here only by `Episode`/`Season`'s own pydantic
  bounds, which fire at a different moment and with a different exception
  type.
- **No transaction**, so a batch that raises part-way cannot leave a session
  poisoned and nothing here exercises the SAVEPOINT.

`calls` and `reset_calls()` are test-double affordances rather than port
methods: `IngestService`'s scale case asserts that a page of 500 episodes
costs a bounded number of *round trips*, and nothing about the answers this
fake returns can express that. The paired integration case counts real
statements instead.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from usher.domain.episode import Episode, Season
from usher.ports.repository import BulkWriteResult, EpisodeCursorPosition, EpisodeRepository

_SeasonKey = tuple[uuid.UUID, int]
_EpisodeKey = tuple[uuid.UUID, int, int]

# Every field a walk may legitimately not know. `title_id`, `season_id` and
# the numbers are absent: they are the key (or, for `season_id`, always
# supplied), so preserving a stored one would make a re-parented episode
# unfixable.
_SEASON_OPTIONAL = ("name", "overview", "air_date", "episode_count", "tmdb_id")
_EPISODE_OPTIONAL = (
    "absolute_number",
    "name",
    "overview",
    "air_date",
    "runtime_minutes",
    "tmdb_id",
    "imdb_id",
)


class FakeEpisodeRepository(EpisodeRepository):
    def __init__(self) -> None:
        self._seasons: dict[_SeasonKey, Season] = {}
        self._episodes: dict[_EpisodeKey, Episode] = {}
        # `next_up` reads watch state, and `EpisodeRepository` has no write
        # path for it. Rather than give the port one it does not want, the
        # subclass writes here through `set_watch_state` and the Postgres
        # subclass merges through `PostgresWatchStateRepository` instead.
        #
        # Keyed on `(user_id, target_id)` where `target_id` is *either* an
        # episode id or a series' `title_id`, which is deliberately the shape
        # of the real `watch_states` table: a row carries one or the other,
        # never both (`ck_watch_states_exactly_one_target`). Keeping them in
        # one map is what leaves `test_a_series_level_watch_state_does_not_
        # finish_the_series` something to catch -- two separate maps would
        # make the mistake it names structurally unspellable here, and a case
        # that cannot fail is not coverage.
        self._watch: dict[tuple[uuid.UUID, uuid.UUID], tuple[bool, datetime | None]] = {}
        self.calls = 0

    def reset_calls(self) -> None:
        self.calls = 0

    def set_watch_state(
        self,
        user_id: uuid.UUID,
        target_id: uuid.UUID,
        *,
        played: bool,
        last_played_at: datetime | None = None,
    ) -> None:
        """A test-double affordance, not a port method -- the same shape
        `reset_calls` is."""
        self._watch[(user_id, target_id)] = (played, last_played_at)

    async def upsert_seasons(self, seasons: Sequence[Season]) -> BulkWriteResult:
        self.calls += 1
        inserted = updated = 0
        # Last-wins deduplication, matching the real one's
        # `SELECT DISTINCT ON (title_id, season_number) ... ORDER BY ...,
        # ordinal DESC`. A batch of episodes from one season names that season
        # once per episode, so this is the common case.
        deduped = {(one.title_id, one.season_number): one for one in seasons}
        for key, incoming in deduped.items():
            stored = self._seasons.get(key)
            if stored is None:
                self._seasons[key] = incoming
                inserted += 1
                continue
            self._seasons[key] = stored.evolve(
                **_kept(incoming, stored, _SEASON_OPTIONAL),
                updated_at=incoming.updated_at,
            )
            updated += 1
        return BulkWriteResult(inserted=inserted, updated=updated)

    async def upsert_episodes(self, episodes: Sequence[Episode]) -> BulkWriteResult:
        self.calls += 1
        inserted = updated = 0
        deduped = {(one.title_id, one.season_number, one.episode_number): one for one in episodes}
        for key, incoming in deduped.items():
            stored = self._episodes.get(key)
            if stored is None:
                self._episodes[key] = incoming
                inserted += 1
                continue
            self._episodes[key] = stored.evolve(
                season_id=incoming.season_id,
                **_kept(incoming, stored, _EPISODE_OPTIONAL),
                updated_at=incoming.updated_at,
            )
            updated += 1
        return BulkWriteResult(inserted=inserted, updated=updated)

    async def resolve_seasons(
        self, keys: Sequence[tuple[uuid.UUID, int]]
    ) -> dict[tuple[uuid.UUID, int], uuid.UUID]:
        self.calls += 1
        found = {}
        for key in keys:
            stored = self._seasons.get(key)
            if stored is not None:
                found[key] = stored.id
        return found

    async def resolve_episodes(
        self, keys: Sequence[tuple[uuid.UUID, int, int]]
    ) -> dict[tuple[uuid.UUID, int, int], uuid.UUID]:
        self.calls += 1
        found = {}
        for key in keys:
            stored = self._episodes.get(key)
            if stored is not None:
                found[key] = stored.id
        return found

    async def list_by_ids(self, episode_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Episode]:
        # One increment whatever the batch, matching the real one statement --
        # which is what lets a provider case hold the call count fixed across
        # three in-progress series and thirty.
        self.calls += 1
        wanted = set(episode_ids)
        return {one.id: one for one in self._episodes.values() if one.id in wanted}

    async def next_up(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Episode]:
        # One increment, whatever the batch: the real one is one statement,
        # and `test_next_up_costs_one_call_however_many_series_are_asked_about`
        # is what makes a per-series loop visible at the unit level at all.
        self.calls += 1
        wanted = set(title_ids)
        if not wanted:
            return {}
        # The high-water mark: the greatest (season, episode) among played
        # episodes, never the most recently played one. Specials excluded.
        marks: dict[uuid.UUID, tuple[int, int]] = {}
        for one in self._episodes.values():
            if one.title_id not in wanted or one.season_number <= 0:
                continue
            state = self._watch.get((user_id, one.id))
            if state is None or not state[0]:
                continue
            position = (one.season_number, one.episode_number)
            if position > marks.get(one.title_id, (-1, -1)):
                marks[one.title_id] = position
        result: dict[uuid.UUID, Episode] = {}
        for one in sorted(
            self._episodes.values(), key=lambda e: (e.season_number, e.episode_number)
        ):
            mark = marks.get(one.title_id)
            if mark is None or one.season_number <= 0:
                continue
            if (one.season_number, one.episode_number) <= mark:
                continue
            result.setdefault(one.title_id, one)
        return result

    async def list_seasons(self, title_id: uuid.UUID) -> list[Season]:
        # One increment, matching the real one statement -- which is what lets
        # the route's own case hold the call count fixed across a series with
        # two seasons and one with twenty-five.
        self.calls += 1
        return sorted(
            (one for one in self._seasons.values() if one.title_id == title_id),
            key=lambda one: one.season_number,
        )

    async def get_season(self, season_id: uuid.UUID) -> Season | None:
        self.calls += 1
        # Keyed on `(title_id, season_number)` here, so a lookup by the row's
        # own id is a scan. That is a divergence from Postgres, where it is a
        # primary-key probe -- it changes the cost and not the answer, which is
        # the shape every entry in this module's docstring has.
        for one in self._seasons.values():
            if one.id == season_id:
                return one
        return None

    async def list_season_episodes(
        self,
        season_id: uuid.UUID,
        *,
        limit: int,
        after: EpisodeCursorPosition | None = None,
    ) -> list[Episode]:
        self.calls += 1
        ordered = sorted(
            (one for one in self._episodes.values() if one.season_id == season_id),
            key=lambda one: (one.episode_number, one.id),
        )
        if after is not None:
            # Python's tuple comparison is lexicographic and strict, which is
            # the two-arm predicate spelled in one expression -- the same
            # relationship `_NEXT_UP`'s row comparison has to its hand-expanded
            # form. ADR-0034's third arm, `key IS NULL`, has no spelling here
            # because `Episode.episode_number` is a non-optional `int`: the
            # unkeyed group it exists for cannot be constructed.
            ordered = [
                one
                for one in ordered
                if (one.episode_number, one.id) > (after.episode_number, after.id)
            ]
        return ordered[:limit]

    async def list_for_title(self, title_id: uuid.UUID) -> tuple[list[Season], list[Episode]]:
        seasons = sorted(
            (one for one in self._seasons.values() if one.title_id == title_id),
            key=lambda one: one.season_number,
        )
        episodes = sorted(
            (one for one in self._episodes.values() if one.title_id == title_id),
            key=lambda one: (one.season_number, one.episode_number),
        )
        return seasons, episodes


def _kept(
    incoming: Season | Episode, stored: Season | Episode, fields: Sequence[str]
) -> dict[str, object]:
    """The `COALESCE` rule, spelled for Python: an incoming `None` means "this
    read did not know", never "blank it". Ingest creates a season or an
    episode from a source's numbers alone and enrichment fills the rest in;
    the next nightly walk must not undo that."""
    return {
        field: (
            value if (value := getattr(incoming, field)) is not None else getattr(stored, field)
        )
        for field in fields
    }
