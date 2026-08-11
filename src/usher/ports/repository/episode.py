"""Seasons and episodes, which are written as one aggregate under a title.

Implemented by `usher.db.repositories.episode.PostgresEpisodeRepository`.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.episode import Episode, Season
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "EpisodeRepository",
]


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
        """Insert or update, keyed on `(title_id, season_number,
        episode_number)`.

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

        Exists because `upsert_seasons` reports counts rather than ids, and it
        cannot report the caller's: ingest mints a fresh UUIDv7 per sighting,
        and a season the catalog already holds keeps the id it was inserted
        with. So the id an episode's `season_id` must carry is knowable only by
        reading it back.

        **Keyed across titles, not scoped to one.** A batch of 1,000 episodes
        off a walk sorted by creation date routinely spans hundreds of series
        -- an episode arrives the week it airs, not with its siblings -- so a
        per-title signature is one round trip per series in the batch, which at
        999,827 episodes is the same design defect batching exists to avoid.

        Absent keys mean "no such season", never "not asked".
        """

    @abstractmethod
    async def resolve_episodes(
        self, keys: Sequence[tuple[uuid.UUID, int, int]]
    ) -> dict[tuple[uuid.UUID, int, int], uuid.UUID]:
        """`(title_id, season_number, episode_number)` -> episode id, in one
        round trip. 999,827 episodes means this cannot be a lookup per item,
        and -- for the reason `resolve_seasons` states -- not a lookup per
        series either.

        `title_id` is part of the key rather than a separate argument because
        every series has an S01E01: a resolve that dropped it hangs one show's
        episodes off another's, and 32,409 series makes that a certainty.

        Absent keys mean "no such episode under this series", never "not
        asked", so a caller iterates its own probes.
        """

    @abstractmethod
    async def list_by_ids(self, episode_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Episode]:
        """Episodes by their own ids, in one round trip.

        **The read `list_in_progress` leaves its caller needing.** That method
        returns episode watch states *as themselves* -- deliberately, because
        the card resumes a file -- and its docstring hands the roll-up to the
        provider: *"Collapsing to one card per series is the provider's, and is
        decided once, there."* An episode state carries no `title_id`, so
        without this there is no way to reach the series a resume belongs to,
        and `ContinueWatchingProvider` silently drops every episode resume on a
        library where 999,827 of 1,126,674 items are episodes. Trap 7, arriving
        through the one M7 read that does not `COALESCE` its way to a title.

        **One statement for the whole page, never one per state.** The
        alternative in the existing surface is `list_for_title`, which returns
        the entire tree -- 20,000 rows for the measured pathological series, to
        find one episode.

        An id with no episode is simply absent, never a key mapped to `None`:
        a caller drops the card rather than rendering one it cannot open.
        """

    @abstractmethod
    async def next_up(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Episode]:
        """The next episode to watch for each of many series, in one round
        trip.

        **"Next" is the episode immediately after the household's high-water
        mark** -- the greatest `(season_number, episode_number)` among played
        episodes of that series -- not the first gap. A skipped episode stays
        skipped: nothing in PRD 06 or PRD 07 can dismiss a card, so a
        gap-seeking implementation makes one skipped episode this household's
        Next Up tonight and every night after.

        **The mark is a position, not an instant.** Never
        `ORDER BY last_played_at DESC LIMIT 1`: a household that finishes
        season three and rewatches the pilot is not asking for S01E02, and
        `last_played_at` is nullable on nearly every walk-sourced row
        (ADR-0014), which makes a recency-keyed mark arbitrary rather than
        merely wrong.

        **Absent, not null, in three cases**: nothing played (a series never
        started has a *first* episode, not a next one -- and "S01E01 of
        everything unstarted" is the whole unwatched library wearing a
        personalised row's title); the mark is the finale (the series is
        finished; **never wrap to S01E01**); and no episodes at all. A key
        missing from the mapping means "nothing to say", which is the answer
        PRD 06 asks a provider to give.

        **Season 0 is excluded on both sides.** Specials are out-of-band by
        construction, and `(0, n) < (1, 1)` is an artefact of the numbering
        rather than a claim about viewing order -- so one watched special
        must not make this say "continue" about a show nobody has started,
        and a special must never be offered as the next chapter.

        **Reads watch state keyed on `episode_id` only.** A series' own
        `title_id`-keyed row is the whole show, and a source can set it
        (Emby's "mark series watched"); an implementation that reads it has
        no `(season, episode)` to position from and answers from whatever the
        join degenerates to.

        **Only `played` states move the mark.** A walk writes a row for every
        item it sees, so on a full library nearly every episode has a
        `watch_states` row and almost none are played -- without the
        predicate the mark is the finale for every series at once and this
        method goes silent across the whole library.

        **One statement for every series asked about.** A per-series loop
        returns the identical mapping and is the N+1 this method exists to
        prevent -- the same argument `resolve_episodes` makes, and the reason
        `NextUpProvider` must never reach for `list_for_title`, which returns
        the whole tree (20,000 rows for the measured pathological series).
        """

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> tuple[list[Season], list[Episode]]:
        """Everything under one series, seasons then episodes, each ordered by
        its own numbering. Used by enrichment to decide what changed, and by
        the CLI's report."""
