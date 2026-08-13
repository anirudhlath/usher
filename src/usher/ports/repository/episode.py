"""Seasons and episodes, which are written as one aggregate under a title.

Implemented by `usher.db.repositories.episode.PostgresEpisodeRepository`.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from usher.domain.episode import Episode, Season
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "EpisodeCursorPosition",
    "EpisodeRepository",
]


@dataclass(frozen=True, slots=True)
class EpisodeCursorPosition:
    """One episode's place in a season's order: its number, and its id.

    **Typed values, never a cursor.**
    [ADR-0034](../../../docs/prd/decisions/0034-the-cursor-carries-a-position.md)
    holds that no port takes an opaque cursor -- the base64 lives in
    `usher.api.cursor`, and a port that accepted one would have to decode it,
    which means knowing the sort vocabulary of the layer above. So
    `GET /seasons/{id}/episodes` decodes, builds one of these, and hands it
    down. `BrowseCursorPosition` is the same shape for `browse`, and the two
    are deliberately separate types rather than one generic: they carry
    different keys and neither route can serve the other's cursor.

    **`episode_number` is `int` and not `int | None`, and that is the whole of
    why this keyset is simpler than `browse`'s.** ADR-0034's predicate has
    three arms because `titles.year`, `titles.popularity` and
    `titles.vote_count` are nullable, so a page boundary can land *inside* the
    unkeyed group and the walk has to resume from it. `episodes.episode_number`
    is `nullable=False` with `ck_episodes_episode_number_non_negative` beside
    it (`db/models/episode.py:86`), so the unkeyed group is provably empty and
    the `IS NOT NULL` leg is unreachable rather than forgotten. This annotation
    is that fact spelled where a type checker can hold it: a caller cannot
    construct the position the missing leg would have been for.

    `id` is the UUIDv7 primary key, which is what makes the keyset a total
    order (ADR-0003, and `CursorSpec` refuses a keyset that does not end in
    one). It is not redundant merely because
    `uq_episodes_title_season_episode` already makes `episode_number` unique
    within a season: the codec's rule is structural, and a route that dropped
    it would have to argue the uniqueness case again at every call site.
    """

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
    async def list_seasons(self, title_id: uuid.UUID) -> list[Season]:
        """One series' seasons, ordered by `season_number`. **Unpaged, and
        that is a measurement rather than an oversight**: the one deployment
        measured holds 32,409 series at a median of 9 seasons, and a client
        renders all of them at once. A cursor over a nine-row answer is a
        second round trip for a screen that is already complete.

        **Season 0 is included, and this is the one place this port diverges
        from `next_up`.** Specials are out-of-band for *"what do I watch
        next"* and perfectly ordinary in *"show me this series"* -- TMDb
        numbers them as season 0 and Emby emits `ParentIndexNumber: 0`, so
        excluding them here would hide a shelf of rows the catalog holds.
        `test_season_zero_is_a_season_of_the_series_and_never_a_next_episode`
        pins both halves in one case so that "fixing" either to match the
        other fails.

        **A title with no seasons answers `[]`, and so does an id no title
        carries.** This read is scoped to `seasons` and cannot tell the two
        apart; a movie having no seasons is a fact about the title, so
        `GET /series/{id}/seasons` asks `TitleRepository` first and reserves
        `404` for the id that does not exist at all.

        Not `list_for_title`, which answers the same question and returns the
        **whole tree** with it -- 20,001 rows / 22.901 ms / 402 buffers for the
        one measured pathological series. That method exists for enrichment's
        change detection and the CLI's report; no route may use it.
        """

    @abstractmethod
    async def get_season(self, season_id: uuid.UUID) -> Season | None:
        """One season by its own id, or `None`.

        `None` and not an empty `Season`: `GET /seasons/{id}/episodes` answers
        `404` for a season that does not exist and `200` with an empty list
        for one that exists and holds nothing, and the route can only tell
        those apart if this read does. The second is a real state rather than
        a defect -- since M9's T1 an `append_to_response` season block that
        TMDb declines to serve is the *same 200 with the key absent* as one
        the show does not have, so a listed season whose block never arrived
        leaves a `Season` row with no episodes.
        """

    @abstractmethod
    async def list_season_episodes(
        self,
        season_id: uuid.UUID,
        *,
        limit: int,
        after: EpisodeCursorPosition | None = None,
    ) -> list[Episode]:
        """One page of one season's episodes, ordered by `(episode_number,
        id)`, keyset-resumed from `after`.

        **Scoped to a season, not to a series.** Every series has an S01E01,
        and a read that forgot the scope answers with the whole tree in
        physical order -- which satisfies every membership assertion a caller
        could write. `test_a_seasons_episodes_page_excludes_another_seasons`
        asserts position and seeds the distractor for that reason.

        **The keyset is ADR-0034's, minus one arm it can prove empty.** That
        record's predicate has three arms because a nullable sort column puts
        a page boundary inside an unkeyed group and the walk has to resume
        from it -- and a row comparison over `(key IS NOT NULL, key, id)`
        evaluates to NULL rather than false there, dropping the whole unkeyed
        tail with every page still full. Here `episodes.episode_number` and
        `episodes.season_number` are `nullable=False`
        (`db/models/episode.py:85-86`), so the unkeyed group is provably empty
        and `EpisodeCursorPosition.episode_number` is typed `int` rather than
        `int | None` to hold that at the type level. Named because *"we did
        not need the `IS NOT NULL` leg"* and *"we forgot it"* look identical
        in a diff.

        The comparison is **strict** on the id tail: relaxed to `>=` the walk
        re-serves its boundary row at every page break, which a test whose
        pages do not abut cannot see.

        `after` is a typed position and never a cursor -- ADR-0034's first
        decision, and the reason the base64 lives in `usher.api.cursor`. The
        route decodes, builds one of these, and hands it down.

        **One statement per page, never one per episode.** The N+1 that
        `resolve_episodes` and `next_up` both exist to prevent is the same one
        arriving at a route, and this is where a paged screen would meet it.
        """

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> tuple[list[Season], list[Episode]]:
        """Everything under one series, seasons then episodes, each ordered by
        its own numbering. Used by enrichment to decide what changed, and by
        the CLI's report.

        **No route may use this.** It returns the whole tree -- 20,001 rows /
        22.901 ms / 402 buffers for the one measured pathological series -- so
        the response length is a property of the show rather than of the
        request. `list_seasons` and `list_season_episodes` are the bounded
        reads a route takes instead.
        """
