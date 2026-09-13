"""In-memory `MediaItemRepository`."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import AwareDatetime

from usher.domain.ids import new_id
from usher.domain.source import MediaItem
from usher.ports.ingest import (
    AvailabilitySweepRefused,
    MediaItemTarget,
    MediaItemUpsert,
    SweepResult,
)
from usher.ports.repository import (
    AddedTitle,
    BulkWriteResult,
    MediaItemRepository,
    UnmatchedCursorPosition,
)

# Sorts before every real timestamp, so an undated item lands at the end of
# a descending review queue -- Postgres's `NULLS LAST`, spelled for Python.
_UNDATED = datetime.min.replace(tzinfo=UTC)


def _after(entry: MediaItem, boundary: UnmatchedCursorPosition) -> bool:
    """Whether `entry` sorts strictly after `boundary` in the review queue's order.

    `added_at DESC NULLS LAST, id DESC`.
    """
    if boundary.added_at is None:
        # The boundary is inside the undated group, which sorts last, so only
        # the rest of that group can follow it.
        return entry.added_at is None and entry.id < boundary.id
    if entry.added_at is None:
        # Every undated row follows every dated one. This is the arm a row
        # comparison loses.
        return True
    if entry.added_at != boundary.added_at:
        return entry.added_at < boundary.added_at
    return entry.id < boundary.id


def _answers(entry: MediaItem, target: MediaItemTarget) -> bool:
    """Whether this row is the item behind a watch-state target.

    An episode target matches on `episode_id` alone -- the row also carries
    the series' `title_id`, deliberately. A title target additionally
    requires `episode_id IS NULL`, or a series' own watch state would
    resolve to one of its episodes' files.
    """
    if target.episode_id is not None:
        return entry.episode_id == target.episode_id
    return (
        target.title_id is not None
        and entry.title_id == target.title_id
        and entry.episode_id is None
    )


class FakeMediaItemRepository(MediaItemRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[uuid.UUID, str], MediaItem] = {}
        self.calls = 0

    def reset_calls(self) -> None:
        """A test-double affordance, not a port method.

        see `tests/fakes/title_match_repository.py` for why the round-trip count is a
        service property that only a counter can express.
        """
        self.calls = 0

    async def upsert_many(self, rows: Sequence[MediaItemUpsert]) -> BulkWriteResult:
        self.calls += 1
        inserted = updated = 0
        # `{(row.source_id, row.external_id): row for row in rows}` rather
        # than iterating: last-wins deduplication, matching the real one's
        # `SELECT DISTINCT ON (...) ORDER BY ..., id DESC`. See the module
        # docstring -- this is the forgiving half of the pair.
        for row in {(entry.source_id, entry.external_id): entry for entry in rows}.values():
            key = (row.source_id, row.external_id)
            existing = self._items.get(key)
            if existing is None:
                self._items[key] = MediaItem(
                    id=new_id(),
                    source_id=row.source_id,
                    title_id=row.title_id,
                    episode_id=row.episode_id,
                    external_id=row.external_id,
                    container=row.container,
                    video_codec=row.video_codec,
                    audio_codec=row.audio_codec,
                    width=row.width,
                    height=row.height,
                    hdr_format=row.hdr_format,
                    audio_channels=row.audio_channels,
                    file_size_bytes=row.file_size_bytes,
                    runtime_seconds=row.runtime_seconds,
                    added_at=row.added_at,
                    last_seen_at=row.last_seen_at,
                    available=True,
                )
                inserted += 1
                continue
            self._items[key] = existing.evolve(
                # Three COALESCEs, not one.
                title_id=row.title_id if row.title_id is not None else existing.title_id,
                episode_id=row.episode_id if row.episode_id is not None else existing.episode_id,
                container=row.container,
                video_codec=row.video_codec,
                audio_codec=row.audio_codec,
                width=row.width,
                height=row.height,
                hdr_format=row.hdr_format,
                audio_channels=row.audio_channels,
                file_size_bytes=row.file_size_bytes,
                runtime_seconds=row.runtime_seconds,
                added_at=row.added_at if row.added_at is not None else existing.added_at,
                last_seen_at=row.last_seen_at,
                available=True,
            )
            updated += 1
        return BulkWriteResult(inserted=inserted, updated=updated)

    async def mark_unseen_unavailable(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> SweepResult:
        mine = [entry for entry in self._items.values() if entry.source_id == source_id]
        stale = [entry for entry in mine if entry.available and entry.last_seen_at < seen_since]
        # A count comparison, never `len(stale) / len(mine)`: an empty source
        # divides by zero, and `mine` is empty on the very first sync of a
        # source that turned out to hold nothing.
        if stale and len(stale) > len(mine) * max_retract_fraction:
            raise AvailabilitySweepRefused(
                would_retract=len(stale), total=len(mine), ceiling=max_retract_fraction
            )
        for entry in stale:
            self._items[(entry.source_id, entry.external_id)] = entry.evolve(available=False)
        return SweepResult(retracted=len(stale), total=len(mine))

    async def get_by_external_id(self, source_id: uuid.UUID, external_id: str) -> MediaItem | None:
        return self._items.get((source_id, external_id))

    async def resolve_series_titles(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, uuid.UUID]:
        self.calls += 1
        wanted = set(external_ids)
        resolved: dict[str, uuid.UUID] = {}
        for entry in self._items.values():
            # An absent key means "not matched yet"; a key mapped to None
            # would be indistinguishable from a matched series whose title
            # failed to load, so unmatched series are simply left out.
            if (
                entry.source_id == source_id
                and entry.external_id in wanted
                and entry.title_id is not None
            ):
                resolved[entry.external_id] = entry.title_id
        return resolved

    async def resolve_targets(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, MediaItemTarget]:
        self.calls += 1
        wanted = set(external_ids)
        return {
            entry.external_id: MediaItemTarget(title_id=entry.title_id, episode_id=entry.episode_id)
            for entry in self._items.values()
            if entry.source_id == source_id
            and entry.external_id in wanted
            # Same convention as `resolve_series_titles`: absent means "not
            # matched", because a key mapped to a pair of `None`s says the
            # same thing at the cost of a branch in every caller.
            and (entry.title_id is not None or entry.episode_id is not None)
        }

    async def resolve_external_ids(
        self, source_id: uuid.UUID, targets: Sequence[MediaItemTarget]
    ) -> dict[MediaItemTarget, str]:
        self.calls += 1
        resolved: dict[MediaItemTarget, str] = {}
        for target in targets:
            candidates = [
                entry
                for entry in self._items.values()
                if entry.source_id == source_id and _answers(entry, target)
            ]
            if not candidates:
                continue
            # The freshest sighting, then the id -- a total order, spelled the same way
            # the real one spells its `DISTINCT ON (...) ORDER BY ..., last_seen_at
            # DESC, external_id`.
            candidates.sort(key=lambda entry: (-entry.last_seen_at.timestamp(), entry.external_id))
            resolved[target] = candidates[0].external_id
        return resolved

    async def list_for_title(self, title_id: uuid.UUID) -> list[MediaItem]:
        # `episode_id is None` is the same clause the real one spells
        # `episode_id IS NULL`, and it is what bounds the answer: an
        # episode's row carries its series' `title_id` too, so without it a
        # series answers with one entry per episode file.
        copies = [
            entry
            for entry in self._items.values()
            if entry.title_id == title_id and entry.episode_id is None
        ]
        # Available first, then freshest, then a total order on `id`.
        copies.sort(
            key=lambda entry: (not entry.available, -entry.last_seen_at.timestamp(), entry.id)
        )
        return copies

    async def list_for_episode(self, episode_id: uuid.UUID) -> list[MediaItem]:
        # No `title_id` bound here, deliberately: `episode_id` alone already
        # names the row, and reading `title_id` too is the wrong
        # implementation this method's contract case exists to fail --
        # `list_for_title`'s bound is the opposite one, for the opposite
        # reason.
        copies = [entry for entry in self._items.values() if entry.episode_id == episode_id]
        # Same ordering as `list_for_title`, for the same reason -- see that
        # method's comment for the divergence this fake's `list.sort`
        # stability hides from Postgres's `id` tiebreak.
        copies.sort(
            key=lambda entry: (not entry.available, -entry.last_seen_at.timestamp(), entry.id)
        )
        return copies

    async def list_unmatched(
        self, source_id: uuid.UUID | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[MediaItem]:
        matching = [
            entry
            for entry in self._items.values()
            if entry.title_id is None and (source_id is None or entry.source_id == source_id)
        ]
        # `(added_at or _UNDATED, id)` reversed: descending by date, with undated items
        # last and `id` breaking ties, which is `ORDER BY added_at DESC NULLS LAST, id
        # DESC`.
        matching.sort(key=lambda entry: (entry.added_at or _UNDATED, entry.id), reverse=True)
        return matching[offset : offset + limit]

    async def list_unmatched_page(
        self,
        source_id: uuid.UUID | None = None,
        *,
        limit: int,
        after: UnmatchedCursorPosition | None = None,
    ) -> list[MediaItem]:
        # The whole queue in `list_unmatched`'s order, then everything strictly
        # after the boundary. One order, read from the one method that spells
        # it, which is what the contract's "the two forms agree on page one"
        # case asserts from the outside.
        ordered = await self.list_unmatched(source_id, limit=len(self._items), offset=0)
        if after is not None:
            ordered = [entry for entry in ordered if _after(entry, after)]
        return ordered[:limit]

    async def attach_title(
        self, media_item_id: uuid.UUID, *, title_id: uuid.UUID, episode_id: uuid.UUID | None
    ) -> bool:
        for key, entry in self._items.items():
            if entry.id == media_item_id:
                self._items[key] = entry.evolve(title_id=title_id, episode_id=episode_id)
                return True
        return False

    async def owned_title_ids(self, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        # `entry.episode_id is None` is the real one's `episode_id IS NULL`,
        # and the *absence* of an `entry.available` test is the other half of
        # the definition: a retracted copy is still a copy you have (PRD 02's
        # soft delete). Both halves are asserted against Postgres, where the
        # sweep that sets `available = false` actually runs.
        wanted = set(title_ids)
        return {
            entry.title_id
            for entry in self._items.values()
            if entry.title_id in wanted and entry.episode_id is None and entry.title_id is not None
        }

    async def owned_episode_ids(self, episode_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        # No `episode_id is None` bound here, which is the *opposite* of
        # `owned_title_ids` above and is the whole reason the two are separate
        # methods rather than one with a flag.
        wanted = set(episode_ids)
        return {
            entry.episode_id
            for entry in self._items.values()
            if entry.episode_id in wanted and entry.episode_id is not None
        }

    async def list_recently_added(
        self, *, since: AwareDatetime, limit: int = 24
    ) -> list[AddedTitle]:
        # One row per title, keeping the NEWEST contributing file -- an episode's row
        # carries its series' `title_id`, so a series that just landed is one row per
        # episode file and one card.
        newest: dict[uuid.UUID, AddedTitle] = {}
        for entry in self._items.values():
            if not entry.available or entry.title_id is None or entry.added_at is None:
                continue
            if entry.added_at < since:
                continue
            current = newest.get(entry.title_id)
            if current is None or entry.added_at > current.added_at:
                newest[entry.title_id] = AddedTitle(entry.title_id, entry.added_at)
        rows = sorted(newest.values(), key=lambda one: one.title_id, reverse=True)
        rows.sort(key=lambda one: one.added_at, reverse=True)
        return rows[:limit]

    async def count_for_source(self, source_id: uuid.UUID) -> int:
        return sum(1 for entry in self._items.values() if entry.source_id == source_id)
