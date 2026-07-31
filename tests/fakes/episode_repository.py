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

from usher.domain.episode import Episode, Season
from usher.ports.repository import BulkWriteResult, EpisodeRepository

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
        self.calls = 0

    def reset_calls(self) -> None:
        self.calls = 0

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
