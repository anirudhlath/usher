"""Watch state -- what a user has played, and how recently."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.domain.watch import WatchState
from usher.ports.ingest import WatchStateMerge, WatchStateWrite

__all__ = [
    "RecentWatch",
    "WatchStateRepository",
]


@dataclass(frozen=True, slots=True)
class RecentWatch:
    """One title the household finished, with the engagement facts this schema actually has.

    Not a `WatchState`, structurally: a watched episode is rolled up to its
    series here, and a `WatchState` carrying a series' `title_id` alongside
    an episode's `episode_id` is forbidden by the model validator and by
    `ck_watch_states_exactly_one_target`.

    `play_count` is here rather than left for a second call because it is the
    only engagement signal `watch_states` carries, and every consumer of
    `list_recent` weights by it.
    """

    title_id: uuid.UUID
    last_played_at: AwareDatetime | None
    play_count: int


class WatchStateRepository(ABC):
    """Persistence for watch state, and the inbound merge from a source.

    Same session/transaction ownership as `TitleRepository`: flushes, never
    commits.

    `merge_from_source` must be `COALESCE`-shaped. In `WatchStateMerge`,
    `None` means the read that produced it could not determine the value and
    `0` or a datetime are positive claims. A source's listing frequently
    cannot determine them -- Emby reports `PlayCount: 0` in a listing for an
    item played twice -- so an implementation writing `None` through as `0`,
    or writing the DTO's default, silently replaces the household's real play
    history with zeros on every nightly walk.
    """

    @abstractmethod
    async def merge_from_source(self, merges: Sequence[WatchStateMerge]) -> int:
        """Apply inbound watch records, returning how many rows changed."""

    @abstractmethod
    async def set_from_client(self, write: WatchStateWrite) -> WatchState:
        """Write one user's own report of their progress, and win."""

    @abstractmethod
    async def list_needing_history(
        self, *, limit: int = 500
    ) -> list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]:
        """Targets that are played but whose play count is unknown.

        "Unknown" is spelled `played AND play_count = 0`, because
        `watch_states.play_count` is `NOT NULL DEFAULT 0` and a walk that
        could not determine the count leaves the default in place. Slightly
        lossy -- an item genuinely played once whose history a source then
        cleared matches too -- and self-healing, because the backfill's
        single-item read is idempotent.

        Bounded by `limit` and ordered oldest-first: this fills the queue for
        a backfill that costs one upstream request per row and must never be
        handed the whole library.
        """

    @abstractmethod
    async def get_for_title(self, user_id: uuid.UUID, title_id: uuid.UUID) -> WatchState | None:
        """One user's state for one title, or None."""

    @abstractmethod
    async def get_for_episode(self, user_id: uuid.UUID, episode_id: uuid.UUID) -> WatchState | None:
        """One user's state for one episode, or None.

        Not a convenience twin of `get_for_title`. A library is mostly
        episodes, so this is the majority read, and it is a different query
        -- a different unique constraint, a different branch of
        `merge_from_source`'s SQL. Without it, an implementation whose
        `COALESCE` is right for titles and wrong for episodes passes.
        """

    @abstractmethod
    async def list_in_progress(self, user_id: uuid.UUID, *, limit: int = 20) -> list[WatchState]:
        """One user's in-progress states, most recently played first."""

    @abstractmethod
    async def list_recent(self, user_id: uuid.UUID, *, limit: int = 20) -> list[RecentWatch]:
        """One user's most recently *finished* titles, one row per title."""

    @abstractmethod
    async def list_rediscoverable(
        self, user_id: uuid.UUID, *, before: AwareDatetime, limit: int = 24
    ) -> list[RecentWatch]:
        """Titles this user finished long ago, most-rewatched first."""

    @abstractmethod
    async def played_title_ids(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these titles this user has already played."""
