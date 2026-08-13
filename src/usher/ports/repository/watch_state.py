"""Watch state -- what a user has played, and how recently.

Implemented by
`usher.db.repositories.watch_state.PostgresWatchStateRepository`.
"""

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
    """One title the household finished, with the engagement facts this
    schema actually has.

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
        """Apply inbound watch records, returning how many rows changed.

        Upserts on `(user_id, title_id)` or `(user_id, episode_id)` with
        `origin = source`.

        - `position_seconds` and `played` are always written.
        - `runtime_seconds`, `play_count` and `last_played_at` are written
          **only when not `None`**; `None` leaves the stored value exactly as
          it was.
        - A stored row whose `updated_at` is newer than `observed_at` is left
          alone entirely. PRD 03: "latest `updated_at` wins" -- a client that
          set a resume position thirty seconds ago knows more than a walk
          that started an hour ago, and without this the nightly reconcile
          stomps it.

        **A batch may contain the same target twice**, for the same reason
        `MediaItemRepository.upsert_many` must tolerate it: a walk may yield
        an item more than once. The merge with the latest `observed_at` wins.

        A merge naming neither a `title_id` nor an `episode_id`, or naming
        both, raises `PortDataMalformed` -- `WatchState`'s own model
        validator and the `num_nonnulls(title_id, episode_id) = 1` CHECK say
        the same thing, and a caller must not receive that as a raw storage
        exception. This is not only about the error's type: an
        implementation that splits a batch into a title branch and an
        episode branch would otherwise write a both-targets merge *twice*,
        as two half-rows.
        """

    @abstractmethod
    async def set_from_client(self, write: WatchStateWrite) -> WatchState:
        """Write one user's own report of their progress, and win.

        The other side of the conflict rule `merge_from_source` documents
        above. That method exists to *lose* to a client -- a stored row
        newer than the walk's `observed_at` is left alone -- and this one
        exists to *be* that newer write. `trg_watch_states_set_updated_at`
        is the entire mechanism: it is a `BEFORE UPDATE` trigger that
        assigns `now()` unconditionally, so a client write is automatically
        newer than any walk that started before it. This method therefore
        needs no `observed_at` of its own, and no `COALESCE` against a
        possibly-absent value -- both fields it writes are always given.

        - `origin = WatchStateOrigin.API`, always. `WatchState.origin` has
          no default deliberately -- a sync path that forgets it must fail
          loudly rather than mislabel source-pushed state as
          user-originated -- and this is the path the member was invented
          for.
        - `position_seconds` and `played` are written exactly as given, on
          every call, whether or not `played` is changing.
        - Marking played (`played=True`) advances `play_count` to
          `GREATEST(play_count, 1)` and stamps `last_played_at` to the
          write instant, **once** -- matching Emby's own
          `POST /PlayedItems`, which M3 measured as advancing to 1
          idempotently rather than incrementing
          (`adapters/emby/adapter.py:623-625`). Marking an already-played
          title played again must not advance `play_count` a second time,
          or the write-back round trip diverges on the second press.
        - Marking unplayed (`played=False`) leaves `play_count` and
          `last_played_at` exactly as stored, and does **not** clear
          `position_seconds`. M3's live run found
          `DELETE /Users/{u}/PlayedItems/{item}` destructive well beyond
          its name -- it clears `PlayCount`, `LastPlayedDate` *and* a
          non-zero resume position -- and `EmbyAdapter.push_watch_state`
          already refuses to use it (`adapter.py:614-619`). This method
          must not do at the database what the adapter deliberately
          declines to do at the source.

        A write naming neither a `title_id` nor an `episode_id`, or naming
        both, raises `PortDataMalformed` -- the same answer
        `merge_from_source` gives, for the same reason:
        `num_nonnulls(title_id, episode_id) = 1` is a CHECK
        (`db/models/watch.py:169`) and a caller must not receive it as a
        raw storage exception.

        Returns the stored row rather than a count: the caller is a single
        action route answering with the new state, not a batch walk
        counting what changed.
        """

    @abstractmethod
    async def list_needing_history(
        self, *, limit: int = 500
    ) -> list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]:
        """`(user_id, title_id, episode_id)` for rows that are played but
        whose play count is unknown.

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
        """One user's in-progress states, most recently played first.

        **"In progress" is `NOT played AND position_seconds > 0`**, and both
        halves are load-bearing. Without `NOT played`, a title finished last
        night is the single most recent thing the household did and heads the
        row. Without `position_seconds > 0`, the answer is the entire
        unwatched library in physical order -- which is a populated, plausible
        row that satisfies every `len(cards) > 0` assertion ever written
        about it.

        **A minimum position is deliberately *not* here.** A title abandoned
        at three seconds is in progress by this definition and stays there
        forever, because nothing in PRD 06 or PRD 07 can dismiss a card. The
        floor belongs to `ContinueWatchingProvider`: it is a product tunable,
        the percentage spelling needs the nullable `runtime_seconds` (and so
        evaluates to false for every state whose runtime a source did not
        report), and Postgres uses a partial index whenever the query's
        predicate implies the index's -- so a tighter provider predicate over
        this looser one costs nothing, while a floor baked into an index
        predicate makes every adjustment a migration.

        **Ordered `last_played_at DESC NULLS LAST, id DESC`.** `NULLS LAST` is
        the whole correctness content of that clause: `last_played_at` is
        nullable because a walk's listing frequently cannot determine it
        (ADR-0014), Postgres's default for `DESC` is NULLS FIRST, and the
        obvious spelling therefore ranks every state the system knows least
        about above every state it knows most about. `updated_at` is not a
        tiebreak here: it is trigger-owned and is the *write* instant, which a
        nightly walk makes identical across a million rows because `now()` is
        frozen per transaction.

        **Episode states are returned as themselves, not rolled up to their
        series.** The card resumes a file. Collapsing to one card per series
        is the provider's, and is decided once, there.
        """

    @abstractmethod
    async def list_recent(self, user_id: uuid.UUID, *, limit: int = 20) -> list[RecentWatch]:
        """One user's most recently *finished* titles, one row per title.

        Feeds `BecauseYouWatchedProvider`'s seeds and `TasteService`'s
        centroid. One method rather than two: same predicate, same ordering,
        same dedup, and only `limit` differs (~3 seeds against ~50 to
        average).

        **`played`, not "has a `last_played_at`".** A seed naming a film the
        household abandoned twenty minutes in is a recommendation built on a
        rejection.

        **Watched episodes are rolled up to their series' `title_id`, and this
        is not a convenience.** `title_embeddings` and `title_neighbors` are
        keyed on `titles.id` and an episode has neither, while 999,827 of the
        one measured source's 1,126,674 items are episodes -- so a title-only
        implementation returns an empty list for exactly the household PRD
        06's taste model describes, and both consumers then produce confident
        output from no input.

        **One row per title.** Ten watched episodes of one series are one
        seed, or `BecauseYouWatchedProvider` emits ten identical rows and the
        centroid is one series counted ten times. `last_played_at` and
        `play_count` come from the most recent contributing state.

        Ordered `last_played_at DESC NULLS LAST`, with the same `NULLS`
        argument as `list_in_progress`. `limit` is applied after the dedup: a
        limit pushed inside it keeps whichever titles the dedup's own key
        ordered first, which is not a recency answer at all.
        """

    @abstractmethod
    async def list_rediscoverable(
        self, user_id: uuid.UUID, *, before: AwareDatetime, limit: int = 24
    ) -> list[RecentWatch]:
        """Titles this user finished long ago, most-rewatched first.

        **This is a substitution and it is named as one.** PRD 06 fires
        `RediscoverProvider` on *"Watched > 2 years ago, **rated highly**"*.
        There is no rating column on `watch_states`, no `favorite`, and
        `SourceWatchState` carries neither -- M7 does not invent one, because
        landing a real rating is a source-port change against a field no
        client can set yet. What this schema can express instead is split in
        two, and the split is the whole design:

        - **The filter is `played AND last_played_at < before`.** Both halves
          are needed: `played` excludes an abandonment, which is a rejection
          rather than a fondness, and the cutoff is the entire "> 2 years
          ago".
        - **The engagement proxy is the *ordering*, never the filter**:
          `play_count DESC`. A rewatch is a revealed preference and it is the
          only thing in this table a household writes more than once.

        **`play_count >= 2` as a filter is the tempting version and it is
        wrong.** `list_needing_history`'s own docstring records that `played
        AND play_count = 0` is how "history unknown" is spelled, and that
        Emby's listing reports `PlayCount: 0` for an item played twice -- so
        that filter returns **nothing** on a freshly-walked deployment and an
        arbitrary subset on a half-backfilled one. As an *ordering* the same
        unreliable column degrades gracefully: an un-backfilled household
        gets a correct set ordered by recency within it, a backfilled one
        gets rewatches first, and neither is empty for a reason nobody can
        see.

        **A state that cannot be dated is excluded for free**, because
        `last_played_at < before` is NULL and therefore not true. That is the
        exact mirror of `list_in_progress`, where the same nullability does
        the *wrong* thing for free -- same column, same three-valued logic,
        opposite outcomes.

        **Title-keyed only, so Rediscover is film-only.** This is a scope
        decision and not the call `list_recent` refused: a title-only
        `list_recent` returns an *empty* set for a TV household and the
        centroid is then computed from nothing, which is a correctness
        failure that produces confident output. A title-only
        `list_rediscoverable` returns a correct but film-only row -- and a
        "rediscover" card for a series is an invitation to re-watch sixty
        hours.
        """

    @abstractmethod
    async def played_title_ids(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these titles this user has already played.

        **The mirror of `MediaItemRepository.owned_title_ids`, and shaped like
        it for the same reason**: one statement for a whole shelf, bounded by
        what the caller is holding rather than by the household's history. It
        exists because three providers need to *subtract* -- a "you love
        westerns" shelf made of the four westerns already finished, or a "more
        from this director" shelf made of the films that established the
        affinity, is circular and has nothing to offer.

        **An episode's played state counts for its series**, through
        `COALESCE(ws.title_id, e.title_id)`. Trap 7 again, and the direction
        of the damage is the reverse of `list_recent`'s: this read *excludes*,
        so a title-only implementation returns too few ids and every series
        the household is halfway through is offered back as something new.
        The row is populated, plausible and about things they have seen.

        **`played`, never "has a watch state".** A sync writes a row per item
        it observed, so "has a state" is the owned library and the shelf built
        on it is permanently empty. Note this is the same predicate
        `list_recent` uses and deliberately *not* `list_in_progress`': a
        title abandoned twenty minutes in is not "already seen", and offering
        it again is right.

        No `limit`: the answer is bounded by `title_ids`, and a limit would
        make a partial answer indistinguishable from a full one -- which for
        a set used to subtract means silently showing a title back.

        An empty `title_ids` answers with an empty set without reading
        anything.
        """
