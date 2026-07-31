"""In-memory `MediaItemRepository`.

**Where this is more forgiving than Postgres, on purpose.** Five places,
each of which the paired `tests/integration/test_media_item_repository.py`
run is what actually closes:

- It is a `dict` keyed on `(source_id, external_id)`, so a duplicate inside
  one batch is silently last-wins. The real one raises
  `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect
  row a second time` unless its staging read is
  `SELECT DISTINCT ON (source_id, external_id)`, so
  `test_upsert_many_tolerates_a_duplicate_within_one_batch` and
  `test_the_last_of_a_duplicated_pair_wins` both pass here for a reason that
  has nothing to do with the code under test.
- No CHECK constraints, so a negative `width` or `file_size_bytes` stores
  happily -- and worse, `MediaItem`'s own pydantic bounds *do* fire on the
  way out of this fake's constructor, so the fake rejects it at a different
  moment and with a different exception type than Postgres does. The real
  path stages a `COPY` that bypasses the ORM entirely and fails at the
  following upsert, as `RepositoryConflict`.
- No foreign keys, so an item can name a `title_id` or `episode_id` no row
  has. The real one raises, and `PostgresMediaItemRepository` translates it.
- Sorting is Python's, not Postgres's, in two ways. `list_unmatched`'s
  NULLS-LAST rule is spelled here as a sentinel and there as literal
  `NULLS LAST`, and Postgres's *default* for `ORDER BY x DESC` is NULLS
  FIRST -- so the two agree only because
  `test_unmatched_items_sort_dated_before_undated` runs against both. And
  `list.sort` is **stable**, so a missing `id` tiebreak is invisible here
  (equal keys keep insertion order) and is a real paging bug against
  Postgres, which makes no such promise. That is why
  `test_the_review_queue_breaks_ties_on_id` asserts the ordering property
  directly instead of paging a large set and hoping the planner reorders.
- No transaction, so nothing here can leave a session poisoned and
  `test_a_caught_conflict_leaves_the_session_usable` is a Postgres-only
  case. A fake cannot express `PendingRollbackError` and pretending
  otherwise would ratify a repository with no SAVEPOINT.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import AwareDatetime

from usher.domain.ids import new_id
from usher.domain.source import MediaItem
from usher.ports.ingest import AvailabilitySweepRefused, MediaItemUpsert, SweepResult
from usher.ports.repository import BulkWriteResult, MediaItemRepository

# Sorts before every real timestamp, so an undated item lands at the end of
# a descending review queue -- Postgres's `NULLS LAST`, spelled for Python.
_UNDATED = datetime.min.replace(tzinfo=UTC)


class FakeMediaItemRepository(MediaItemRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[uuid.UUID, str], MediaItem] = {}
        self.calls = 0

    def reset_calls(self) -> None:
        """A test-double affordance, not a port method -- see
        `tests/fakes/title_match_repository.py` for why the round-trip count
        is a service property that only a counter can express."""
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
                # Three COALESCEs, not one. `title_id`/`episode_id`: never
                # downgrade a matched item to unmatched -- the nightly walk
                # upserts with `title_id=None` long before the match pass has
                # resolved anything, so an unconditional assignment erases
                # every manual review-queue resolution the same night it was
                # made. `added_at`: a source that stops reporting when a file
                # arrived must not erase the answer it gave last night.
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

    async def list_unmatched(
        self, source_id: uuid.UUID | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[MediaItem]:
        matching = [
            entry
            for entry in self._items.values()
            if entry.title_id is None and (source_id is None or entry.source_id == source_id)
        ]
        # `(added_at or _UNDATED, id)` reversed: descending by date, with
        # undated items last and `id` breaking ties, which is
        # `ORDER BY added_at DESC NULLS LAST, id DESC`. Postgres's own
        # default for a DESC sort is NULLS *FIRST*, so the two agree only
        # because both spell it out -- `_UNDATED` here, `NULLS LAST` there.
        # An earlier version of this key led with `added_at is not None`,
        # which read as the load-bearing part and was not: mutating it away
        # left every contract case green, because the sentinel alone already
        # decides the order.
        matching.sort(key=lambda entry: (entry.added_at or _UNDATED, entry.id), reverse=True)
        return matching[offset : offset + limit]

    async def attach_title(
        self, media_item_id: uuid.UUID, *, title_id: uuid.UUID, episode_id: uuid.UUID | None
    ) -> bool:
        for key, entry in self._items.items():
            if entry.id == media_item_id:
                self._items[key] = entry.evolve(title_id=title_id, episode_id=episode_id)
                return True
        return False

    async def count_for_source(self, source_id: uuid.UUID) -> int:
        return sum(1 for entry in self._items.values() if entry.source_id == source_id)
