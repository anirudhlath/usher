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

    **Not a `WatchState`, and that is structural rather than stylistic.** A
    watched *episode* is rolled up to its series here, and a `WatchState`
    carrying a series' `title_id` alongside an episode's `episode_id` is
    forbidden both by the model validator and by
    `ck_watch_states_exactly_one_target`. Returning one anyway would mean
    lying about which row was read.

    `play_count` is here rather than being left for a second call because it
    is the only engagement signal `watch_states` carries -- there is no
    rating column, and M7 does not invent one -- and every consumer of
    `list_recent` wants to weight by it.
    """

    title_id: uuid.UUID
    last_played_at: AwareDatetime | None
    play_count: int


class WatchStateRepository(ABC):
    """Persistence for watch state, and the inbound merge from a source.

    Same session/transaction ownership as `TitleRepository`: flushes, never
    commits.

    **`merge_from_source` is `COALESCE`-shaped, and that is a correctness
    property rather than an implementation detail.** `WatchStateMerge`'s
    `play_count`/`last_played_at` are `int | None`/`datetime | None`, where
    `None` means "the read that produced this could not determine it" and
    `0`/a datetime are positive claims. A source's *listing* frequently
    cannot determine them -- verified against Emby 4.9.5.0, where a listing
    reports `PlayCount: 0` for an item played twice -- so an implementation
    that wrote `None` through as `0`, or that wrote the DTO's default,
    replaces the household's real play history with zeros on every nightly
    walk, silently. ADR-0014.
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
        """`(user_id.

        title_id, episode_id)` for rows that are played but whose play count is unknown.

        "Unknown" is spelled `played AND play_count = 0`, because
        `watch_states.play_count` is `NOT NULL DEFAULT 0` and a walk that
        could not determine the count leaves the default in place. Slightly
        lossy -- an item genuinely played once whose history a source then
        cleared matches too -- and self-healing, because the backfill's
        single-item read is idempotent and Emby never leaves a played item
        at `PlayCount: 0`. ADR-0014 records the alternative (a nullable
        column) and why it was not taken.

        Bounded by `limit` and ordered oldest-first, because this is the
        queue-filling query for a backfill that costs one upstream request
        per row and must never be handed the whole library.
        """

    @abstractmethod
    async def get_for_title(self, user_id: uuid.UUID, title_id: uuid.UUID) -> WatchState | None:
        """One user's state for one title, or None."""

    @abstractmethod
    async def get_for_episode(self, user_id: uuid.UUID, episode_id: uuid.UUID) -> WatchState | None:
        """One user's state for one episode, or None.

        Not a convenience twin of `get_for_title`: 999,827 of the one
        measured source's 1,126,674 items are episodes, so this is the
        majority read, and it is a genuinely different query -- a different
        unique constraint, and a different branch of `merge_from_source`'s
        SQL. Without it, an implementation whose `COALESCE` is right for
        titles and wrong for episodes has nothing that can tell.
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
