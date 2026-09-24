"""Media items -- a source's own view of a title, and the review queue."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.domain.source import MediaItem
from usher.ports.ingest import MediaItemTarget, MediaItemUpsert, SweepResult
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "AddedTitle",
    "MediaItemRepository",
    "UnmatchedCursorPosition",
]


@dataclass(frozen=True, slots=True)
class UnmatchedCursorPosition:
    """One item's place in the review queue's order: when it arrived, and its id."""

    added_at: AwareDatetime | None
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AddedTitle:
    """One title the household has a copy of, and when that copy arrived.

    Not a `MediaItem`: a title with twenty thousand episode files is *one*
    row here, so no single item's identity is the honest answer. `added_at`
    is the newest contributing file's, because a season that landed last
    night on a show whose pilot has been on disk for two years is a new
    arrival.
    """

    title_id: uuid.UUID
    added_at: AwareDatetime


class MediaItemRepository(ABC):
    """Persistence for "this title is available on that source".

    Same session/transaction ownership as `TitleRepository`: every method
    flushes so conflicts surface immediately, none commits.

    Availability is retracted by exactly one method, and only after a walk
    has provably finished. `SourceAdapter.list_items` guarantees a walk
    raises rather than truncating so a caller can tell "the library ended"
    from "the adapter gave up", and that guarantee is worth nothing if the
    sweep runs either way. `mark_unseen_unavailable` is therefore a separate
    call made after the walk returns normally, never a side effect of
    `upsert_many`.
    """

    @abstractmethod
    async def upsert_many(self, rows: Sequence[MediaItemUpsert]) -> BulkWriteResult:
        """Insert or update media items, keyed on `(source_id, external_id)`."""

    @abstractmethod
    async def mark_unseen_unavailable(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> SweepResult:
        """Retract availability for everything this source did not show us."""

    @abstractmethod
    async def get_by_external_id(self, source_id: uuid.UUID, external_id: str) -> MediaItem | None:
        """One item as this source addresses it, or None."""

    @abstractmethod
    async def resolve_series_titles(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, uuid.UUID]:
        """Map series `external_id` -> `title_id` for those already matched.

        An episode's canonical parent is its series' `Title`, and a walk
        sorted by creation date gives no guarantee a series is seen before
        its episodes. Batched rather than per-episode: a library is mostly
        episodes, so a per-item lookup here is one query per episode.

        Absent keys mean "not matched yet", not "no such series" -- the
        caller leaves those episodes unmatched and enqueues a re-match,
        which the next batch or the next run resolves.
        """

    @abstractmethod
    async def resolve_targets(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, MediaItemTarget]:
        """Map each `external_id` to what its row is matched to.

        The read a watch-state walk needs. A walk of `watch_state()` yields
        one record per item, so this is one statement per batch, never one
        per state.

        Absent keys mean "not stored, or stored and not matched to anything"
        -- the same convention `resolve_series_titles` uses, and the same
        response either way: the caller counts the state unmatched and moves
        on.

        Unlike `resolve_series_titles` this answers for any item, and with
        both ids: an episode's row carries its series' title *and* its
        episode, and a caller seeing only the first would merge every episode
        of a show into one watch state on the series.
        """

    @abstractmethod
    async def resolve_external_ids(
        self, source_id: uuid.UUID, targets: Sequence[MediaItemTarget]
    ) -> dict[MediaItemTarget, str]:
        """The inverse: how this source addresses the items behind these canonical targets."""

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> list[MediaItem]:
        """Every copy of one title, across every source.

        PRD 07's `availability` array.
        """

    @abstractmethod
    async def list_for_episode(self, episode_id: uuid.UUID) -> list[MediaItem]:
        """Every copy of one **episode**, across every source.

        `list_for_title`'s counterpart, for `POST /episodes/{id}/play`.

        `list_for_title` carries a load-bearing `AND episode_id IS NULL`,
        which is what makes it useless here: an episode's row is one of the
        rows that clause excludes. The alternative, `resolve_external_ids`
        per configured source, is N statements returning an id with none of
        the availability facts `/play`'s ranking needs.

        Same ordering as `list_for_title` -- `available` first, then most
        recently seen, then `id` as a total-order tiebreak -- so a detail
        screen does not shuffle its badges between refreshes.

        Empty is the ordinary answer for an episode with no copy on any
        configured source, not a missing row.
        """

    @abstractmethod
    async def list_unmatched(
        self, source_id: uuid.UUID | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[MediaItem]:
        """The review queue (PRD 02: "Unmatched items are never dropped").

        Ordered by `added_at` descending with `id` as a tiebreak, so paging
        is stable across calls -- an unstable order silently shows an
        operator the same item twice and hides another. `added_at` is
        nullable and sorts last, because an item a source cannot date is
        less interesting than one it dated yesterday, not more.

        The `OFFSET` costs linear time per page and quadratic to drain, which
        is why `list_unmatched_page` exists beside this. This form is kept for
        `usher unmatched`, whose `--offset` is an operator typing a number at
        a terminal rather than a client following a cursor. Both orders are
        one definition, asserted by a contract case that walks the first page
        of each and requires them to agree.
        """

    @abstractmethod
    async def list_unmatched_page(
        self,
        source_id: uuid.UUID | None = None,
        *,
        limit: int,
        after: UnmatchedCursorPosition | None = None,
    ) -> list[MediaItem]:
        """One keyset page of the review queue, resumed from `after`."""

    @abstractmethod
    async def attach_title(
        self, media_item_id: uuid.UUID, *, title_id: uuid.UUID, episode_id: uuid.UUID | None
    ) -> bool:
        """Resolve one item to a title, by hand or by a later match pass.

        Returns whether a row changed, so a caller can answer 404 rather
        than claim to have resolved something that does not exist.

        Unlike `upsert_many` this *does* write what it is given, including a
        `None` `episode_id`: it is the deliberate act of a human or of a
        re-match, not a walk's incidental "I did not look".
        """

    @abstractmethod
    async def owned_title_ids(self, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of `title_ids` this household has a copy of, on any source."""

    @abstractmethod
    async def owned_episode_ids(self, episode_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these **episodes** the household has a copy of.

        `owned_title_ids`' twin, and a genuinely different question: that one
        bounds itself to `episode_id IS NULL` so a series is one row, so
        asking it about an episode answers about the series' row and reports
        a missing episode file as owned.

        `NextUpProvider` needs it, because a "next up" that cannot be played
        is worse than absent -- and that filter is the provider's, not
        `next_up`'s, which answers what comes next and not what is available.

        No availability filter, matching `owned_title_ids`: a copy the nightly
        sweep retracted is still a copy you have. One statement however many
        ids are asked about.
        """

    @abstractmethod
    async def list_recently_added(
        self, *, since: AwareDatetime, limit: int = 24
    ) -> list[AddedTitle]:
        """Titles whose newest copy arrived on or after `since`, newest first."""

    @abstractmethod
    async def count_for_source(self, source_id: uuid.UUID) -> int:
        """How many items this source has, available or not.

        The sweep's denominator, and the CLI's report.
        """
