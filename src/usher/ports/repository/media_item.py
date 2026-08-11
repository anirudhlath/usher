"""Media items -- a source's own view of a title, and the review queue.

Implemented by
`usher.db.repositories.media_item.PostgresMediaItemRepository`.
"""

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
]


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

    **Availability is retracted by exactly one method, and only after a walk
    has provably finished.** `SourceAdapter.list_items`' contract guarantees
    a walk raises rather than truncating, precisely so a caller can tell
    "the library ended" from "the adapter gave up"; that guarantee is worth
    nothing if the sweep runs either way. `mark_unseen_unavailable` is
    therefore a separate call the reconciler makes *after* the walk returns
    normally, never a side effect of `upsert_many`. See ADR-0015.
    """

    @abstractmethod
    async def upsert_many(self, rows: Sequence[MediaItemUpsert]) -> BulkWriteResult:
        """Insert or update media items, keyed on `(source_id, external_id)`.

        Flushes, never commits. Returns inserts and updates separately, so a
        re-sync is visibly a re-sync (`inserted == 0`) rather than
        indistinguishable from a first one -- which is what makes PRD 10's
        "library growth per week" panel a real measurement instead of a
        straight line.

        **A batch may contain the same `(source_id, external_id)` twice.**
        `SourceAdapter.list_items` explicitly permits duplicates within one
        walk, so an implementation must deduplicate rather than assume;
        against Postgres, not doing so raises `CardinalityViolationError`.
        The last such row in `rows` wins, matching a resumed walk's "the
        later page is the fresher read".

        **Never downgrades a matched item to unmatched.** A row already
        carrying a `title_id` (from an earlier match, or from a human
        resolving it in the review queue) keeps it when this is called with
        `title_id=None`. The reverse -- a newly-resolved `title_id` landing
        on a row that had none -- does apply. Without this rule the nightly
        walk erases every manual resolution, silently, the same night it was
        made.

        **Never hard-deletes.** PRD 02: "Soft-delete availability,
        hard-delete nothing." Rows absent from `rows` are not touched by
        this call at all.

        An item that appears in `rows` is `available = true` when this
        returns, because appearing in a walk *is* the evidence of
        availability -- which is how an item that came back comes back.

        A `title_id` or `episode_id` naming a row that does not exist raises
        `RepositoryConflict`, and leaves the session usable for the caller's
        other pending work.
        """

    @abstractmethod
    async def mark_unseen_unavailable(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> SweepResult:
        """Retract availability for everything this source did not show us.

        Sets `available = false` on every row for `source_id` whose
        `last_seen_at` is older than `seen_since` and which is currently
        available. Returns how many were retracted and how many rows the
        source has in total -- the guard's own denominator, reported because
        "3 retracted" and "3 of 4 retracted" want different responses.

        This only ever sets `false`. Restoring an item that came back is
        `upsert_many`'s doing, because appearing in a walk *is* the evidence
        of availability.

        **Raises `AvailabilitySweepRefused` and changes nothing** when the
        retraction would exceed `max_retract_fraction` of the source's
        items. `list_items` raising rather than truncating covers a walk
        that failed; this covers a walk that *succeeded* and returned far
        less than the library holds -- an unmounted drive, a library removed
        by accident, a permissions change on the source's own account.
        Neither the adapter nor Usher can distinguish that from a genuine
        mass deletion, and only one of the two is reversible, so the sweep
        declines.

        `max_retract_fraction` of `1.0` disables the guard, which is what an
        operator deliberately removing a library passes.

        Never deletes. A retracted item keeps its row, its `title_id`, and
        every watch state attached to its title.
        """

    @abstractmethod
    async def get_by_external_id(self, source_id: uuid.UUID, external_id: str) -> MediaItem | None:
        """One item as this source addresses it, or None."""

    @abstractmethod
    async def resolve_series_titles(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, uuid.UUID]:
        """Map series `external_id` -> `title_id` for those already matched.

        Exists because an episode's canonical parent is its series' `Title`,
        and a walk sorted by creation date offers no guarantee that a series
        is seen before its episodes. Batched rather than per-episode: this
        deployment holds 999,827 episodes, so a per-item lookup here is the
        difference between one query per batch and one per episode.

        Absent keys mean "not matched yet", not "no such series" -- the
        caller leaves those episodes unmatched and enqueues a re-match,
        which the next batch or the next run resolves.
        """

    @abstractmethod
    async def resolve_targets(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, MediaItemTarget]:
        """Map each `external_id` to what its row is matched to.

        The read a watch-state walk needs, and the reason it is batched is
        the reason every other read here is: a walk of `watch_state()`
        yields one record per item, and this deployment has 1,126,674 of
        them. One statement per batch, never one per state.

        Absent keys mean "not stored, or stored and not matched to
        anything" -- the same convention `resolve_series_titles` uses, and
        the same response either way (the caller counts the state
        unmatched and moves on, because a watch record with no target is
        exactly what `merge_from_source` raises `PortDataMalformed` for).

        Unlike `resolve_series_titles` this answers for *any* item, and it
        answers with both ids: an episode's row carries its series' title
        **and** its episode, and a caller that saw only the first would
        merge 40 episodes of a show into one watch state on the series.
        """

    @abstractmethod
    async def resolve_external_ids(
        self, source_id: uuid.UUID, targets: Sequence[MediaItemTarget]
    ) -> dict[MediaItemTarget, str]:
        """The inverse: how this source addresses the items behind these
        canonical targets.

        `WatchStateRepository.list_needing_history` answers in canonical
        ids, and `SourceAdapter.get_watch_state` asks in the source's own --
        so without this the history backfill has no way from one to the
        other, and would be a per-row lookup even if it did.

        A `MediaItemTarget` here is watch-state shaped: exactly one of the
        two ids is set. A title-only target matches only a row with **no**
        `episode_id`, or a series' own state would resolve to whichever of
        its episodes the planner reached first.

        A target with two copies on the same source -- a 4K and an HD file
        of one film, which is ordinary -- resolves to one of them
        deterministically: the most recently seen, then the lowest
        `external_id`. Any of them would answer, and picking arbitrarily
        would make a backfill's upstream request depend on the planner.
        Deliberately *not* also keyed on `available`: only a walk sets an
        item available and only the sweep retracts one, so the freshest
        `last_seen_at` is already the available copy in every state
        reachable through this port -- an `available DESC` ahead of it
        would be an ordering key no case could ever fail.

        Absent keys mean this source has no item for that target, which is
        the normal state of a household with two sources.
        """

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> list[MediaItem]:
        """Every copy of one title, across every source. PRD 07's
        `availability` array.

        **Unavailable copies are returned**, with `available = false`, rather
        than filtered: PRD 02 is "soft-delete availability, hard-delete
        nothing", and a client rendering "not on any source" for a film on a
        temporarily unmounted drive is showing a different fact than the one
        stored. The client decides what a retracted copy means.

        **A series' episodes are not its copies, and that is what bounds
        this read.** An episode's row carries its series' `title_id` as well
        as its own `episode_id`, so a read on `title_id` alone answers a
        series with one row per episode file -- 999,827 of the one measured
        source's 1,126,789 items are episodes, and one long-running serial
        would put thousands of entries in a response whose whole content is
        "which sources hold this". Rows with an `episode_id` are therefore
        excluded, exactly as `resolve_external_ids`' title branch excludes
        them and for the same reason. What is left is bounded by copies of
        the title *itself* -- sources times versions, single digits in a
        household -- so nothing here pages. `list_unmatched` is the method on
        this port that needed an `OFFSET`, and it is measured at 388.9 ms at
        offset 1,126,574 for exactly the reason this one must not need one.

        Ordered `available` first, then most recently seen, then by `id` --
        a total order, so a detail screen does not shuffle its badges between
        refreshes. Nothing about a `SELECT` promises an order otherwise.

        Empty is the common answer, not a missing row: the catalog holds
        1,271,138 titles and the one measured source holds 1,126,789 items,
        so the great majority of titles are on no source at all.
        """

    @abstractmethod
    async def list_for_episode(self, episode_id: uuid.UUID) -> list[MediaItem]:
        """Every copy of one **episode**, across every source.
        `list_for_title`'s counterpart, for `POST /episodes/{id}/play`.

        `list_for_title` carries `AND episode_id IS NULL` -- load-bearing and
        measured, 1 row in 0.251 ms against 20,001 rows and 22.901 ms without
        it, on one 20,000-episode series (`.claude/rules/db-and-sql.md`) --
        which is exactly what makes it useless here: an episode's row is
        precisely one of the rows that clause excludes. The alternative,
        `resolve_external_ids` once per configured source, is N statements
        and returns an id with none of the availability facts `/play`'s
        ranking needs.

        Same ordering as `list_for_title`, for the same reason: `available`
        first, then most recently seen, then `id` as a total-order tiebreak,
        so a detail screen does not shuffle its badges between refreshes.

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
        """

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
        """Which of `title_ids` this household has a copy of, on any source.

        **A retracted copy still counts.** PRD 02's availability is a soft
        delete, so `available = false` means "a source is not currently
        reporting it", and a search ranking that flipped when a source went
        down would move results for a reason unconnected to the query. The
        `owned_only` filter in `PostgresSearchIndex` carries the *identical*
        predicate: two definitions of owned is how a filtered list and a
        boosted list stop agreeing.

        **Restricted to a title's own row (`episode_id IS NULL`), and the
        reason is measured.** `IngestService` writes an episode's row with its
        series' `title_id` *and* its own `episode_id`, so an unrestricted read
        of a series is one row per episode file: 20,001 rows / 22.901 ms / 402
        buffers against 1 row / 0.251 ms / 21 buffers, on this project's own
        measurement of `list_for_title`. `resolve_external_ids`' title branch
        carries the identical clause. The bound that buys: a library that
        reported episodes but never their series row reads as not-owned for
        that series.

        One statement however many ids are asked about — the same N+1 this
        port's `list_for_title` would otherwise be used for, and worse, since
        each of those reads is a read of a whole show.
        """

    @abstractmethod
    async def owned_episode_ids(self, episode_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these **episodes** the household has a copy of.

        `owned_title_ids`' twin, and it is a genuinely different question
        rather than a convenience: that one bounds itself to `episode_id IS
        NULL` precisely so a series is one row, so asking it about an episode
        answers about the *series'* own row and would report a missing episode
        file as owned. 999,827 of the one measured source's 1,126,674 items are
        episodes, so this is the majority read of the two.

        `NextUpProvider` is what needs it: *"next up" that cannot be played is
        worse than absent*, and that filter is the provider's rather than
        `next_up`'s, which answers what comes next and not what is available.

        No availability filter, matching `owned_title_ids` exactly — a copy the
        nightly sweep retracted is still a copy you have. One statement however
        many ids are asked about.
        """

    @abstractmethod
    async def list_recently_added(
        self, *, since: AwareDatetime, limit: int = 24
    ) -> list[AddedTitle]:
        """Titles whose newest copy arrived on or after `since`, newest first.

        **One row per title, and `episode_id IS NULL` is deliberately not how
        that is done.** An episode's `MediaItem` carries its series'
        `title_id`, so a series that just landed is one row per episode file
        -- 20,000 for the measured pathological series, one card. Three other
        statements on this port bound that with `episode_id IS NULL`; here
        the same bound is the wrong one, and its cost is already named on
        `owned_title_ids`: *"a library that reported episodes but never their
        series row reads as not-owned for that series."* Recently Added is
        precisely the surface where a newly added series must appear. So the
        dedup is over *all* matched rows and the window is the bound instead.

        `added_at` is the **newest** contributing copy's, because a season
        that landed last night on a two-year-old show is a new arrival.

        **`available` is filtered, and that is the opposite call from
        `owned_title_ids`**, whose comment argues against an availability
        predicate because "a copy the nightly sweep retracted is still a copy
        you have". True of *ownership*, false of *what arrived this week*.
        Two statements, two answers, and the divergence is deliberate.

        **`since` is the caller's, not the statement's.** `now()` is frozen
        per transaction, so a statement spelling its own
        `now() - interval '30 days'` cannot be tested at its boundary -- every
        row a case inserts shares one instant, and "inside the window" and "at
        its edge" become the same fact. `clock_timestamp()` would trade that
        for a nondeterministic test. It is also what lets
        `RecentlyAddedProvider` own the window as a tunable rather than as a
        migration. An item with no `added_at` at all is excluded, by
        three-valued logic rather than by a predicate.

        **No `user_id` and no `source_id`.** Availability is household-wide,
        so this is the one provider whose output is identical for every member
        of the household. Stated because every other statement on this port is
        per-source and the natural instinct is to scope this one too.

        **`added_at` is not immutable, and a reader of the upsert's
        `COALESCE(excluded.added_at, media_items.added_at)` will assume it
        is.** That clause refuses to overwrite with **NULL** only. A source
        that reports a *different* `added_at` -- files genuinely re-copied, or
        a source migration re-deriving its creation date -- wins, every walk,
        and moves the whole library into this window at once. The window and
        the `limit` cap how bad that looks; nothing prevents it. The clause is
        deliberately unchanged, because it is also what lets a source that
        initially could not report `added_at` fill it in later.
        """

    @abstractmethod
    async def count_for_source(self, source_id: uuid.UUID) -> int:
        """How many items this source has, available or not. The sweep's
        denominator, and the CLI's report."""
