"""Seasons and episodes, which are written as one aggregate under a title."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from usher.domain.episode import Episode, Season
from usher.ports.repository._references import EpisodeReference
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "EpisodeCursorPosition",
    "EpisodeRepository",
]


@dataclass(frozen=True, slots=True)
class EpisodeCursorPosition:
    """One episode's place in a season's order: its number, and its id."""

    episode_number: int
    id: uuid.UUID


class EpisodeRepository(ABC):
    """Persistence for the season/episode hierarchy under a series `Title`.

    Seasons and episodes are one aggregate here rather than two ports: an
    episode cannot exist without its season, both arrive from the same
    provider payload, and every write is a batch over one series.

    Same session/transaction ownership as `TitleRepository`: every method
    flushes so conflicts surface immediately, none commits.
    """

    @abstractmethod
    async def upsert_seasons(self, seasons: Sequence[Season]) -> BulkWriteResult:
        """Insert or update, keyed on `(title_id, season_number)`.

        Never overwrites a non-null field with a null one, for the same reason
        `upsert_episodes` does not: ingest can create a season from a source's
        own number alone and enrichment fills the rest in.

        A `title_id` no title carries raises `RepositoryConflict` rather than a
        raw storage error, and leaves the session usable for the caller's other
        pending work.

        A batch may contain the same `(title_id, season_number)` twice -- a
        walk yields episodes, and a whole season's worth of them name the same
        season -- so an implementation deduplicates rather than assuming. The
        last such row wins.
        """

    @abstractmethod
    async def upsert_episodes(self, episodes: Sequence[Episode]) -> BulkWriteResult:
        """Insert or update, keyed on `(title_id, season_number, episode_number)`.

        Never overwrites a non-null field with a null one: ingest creates an
        episode from a source's own numbers alone (no name, no air date) and
        enrichment fills the rest in, and the next nightly walk must not blank
        what enrichment wrote. Same `COALESCE` rule
        `MediaItemRepository.upsert_many` applies to `title_id`, for the same
        reason.

        A `title_id` or `season_id` naming a row that does not exist raises
        `RepositoryConflict`.

        Tolerates a duplicate within one batch, as `upsert_seasons` does.
        """

    @abstractmethod
    async def resolve_seasons(
        self, keys: Sequence[tuple[uuid.UUID, int]]
    ) -> dict[tuple[uuid.UUID, int], uuid.UUID]:
        """`(title_id, season_number)` -> season id, in one round trip.

        `upsert_seasons` reports counts, not ids, and cannot report the
        caller's: ingest mints a fresh UUIDv7 per sighting while a season the
        catalog already holds keeps the id it was inserted with. So the id an
        episode's `season_id` must carry is knowable only by reading it back.

        Keyed across titles, not scoped to one. A batch off a walk sorted by
        creation date routinely spans hundreds of series -- an episode arrives
        the week it airs, not with its siblings -- so a per-title signature is
        one round trip per series in the batch.

        Absent keys mean "no such season", never "not asked".
        """

    @abstractmethod
    async def resolve_episodes(
        self, keys: Sequence[tuple[uuid.UUID, int, int]]
    ) -> dict[tuple[uuid.UUID, int, int], uuid.UUID]:
        """`(title_id, season_number, episode_number)` -> episode id, in one round trip.

        A million episodes means this cannot be a lookup per item, and -- for
        the reason `resolve_seasons` states -- not one per series either.

        `title_id` is part of the key rather than a separate argument because
        every series has an S01E01: a resolve that dropped it hangs one show's
        episodes off another's.

        Absent keys mean "no such episode under this series", never "not
        asked", so a caller iterates its own probes.
        """

    @abstractmethod
    async def resolve_natural_keys(
        self, references: Sequence[EpisodeReference]
    ) -> dict[EpisodeReference, uuid.UUID]:
        """`EpisodeReference` -> the id **this** catalog holds it under, in one round trip."""

    @abstractmethod
    async def list_by_ids(self, episode_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Episode]:
        """Episodes by their own ids, in one round trip.

        The read `list_in_progress` leaves its caller needing. It returns
        episode watch states as themselves, and an episode state carries no
        `title_id`, so without this there is no way to reach the series a
        resume belongs to and a continue-watching row silently drops every
        episode resume on a library that is mostly episodes.

        One statement for the whole page, never one per state: the
        alternative, `list_for_title`, returns an entire series tree to find
        one episode.

        An id with no episode is simply absent, never a key mapped to `None`:
        a caller drops the card rather than rendering one it cannot open.
        """

    @abstractmethod
    async def next_up(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Episode]:
        """The next episode to watch for each of many series, in one round trip."""

    @abstractmethod
    async def list_seasons(self, title_id: uuid.UUID) -> list[Season]:
        """One series' seasons, ordered by `season_number`."""

    @abstractmethod
    async def get_season(self, season_id: uuid.UUID) -> Season | None:
        """One season by its own id, or `None`.

        `None` and not an empty `Season`: the route answers `404` for a season
        that does not exist and `200` with an empty list for one that exists
        and holds nothing, and it can only tell those apart if this read does.
        The empty case is real, not a defect -- a season block TMDb declines
        to serve arrives as the same 200-with-the-key-absent as one the show
        does not have, leaving a `Season` row with no episodes.
        """

    @abstractmethod
    async def list_season_episodes(
        self,
        season_id: uuid.UUID,
        *,
        limit: int,
        after: EpisodeCursorPosition | None = None,
    ) -> list[Episode]:
        """One page of one season's episodes, ordered by `(episode_number, id)`.

        Keyset-resumed from `after`.
        """

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> tuple[list[Season], list[Episode]]:
        """Everything under one series, seasons then episodes, each ordered by its own numbering.

        Used by enrichment to decide what changed, and by the CLI's report.

        No route may use this. It returns the whole tree, tens of thousands
        of rows for a long-running series, so the response length is a
        property of the show rather than of the request. `list_seasons` and
        `list_season_episodes` are the bounded reads a route takes instead.
        """
