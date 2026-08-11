"""The seeding every provider case shares, and the discipline it enforces.

**A wrong row renders identically to a right one**, so every case in every
`test_rows_*.py` file asserts on **position** and seeds a distractor a broken
implementation would rank first. `assert title_id in {c.title_id for c in
row.cards}` is satisfied by returning the library in physical order, and so is
`assert len(row.cards) > 0`.

**A distractor that varies two things at once is not a distractor**, and this
milestone's own plan wrote one into its headline table -- the ContinueWatching
distractor it specifies sets `played` *and* `position_seconds = 0`, so it
isolates neither half of the `NOT played AND position_seconds > 0` predicate.
`Library.finished` therefore keeps its resume position, and
`Library.never_started` is the separate seed for the other half.

`Library` seeds through the real fakes rather than through dicts, so a case
reads as a household rather than as a fixture, and every id is minted per call
and never sorted on: `watch_states.id` is a UUIDv7, so id order is insertion
order, and a fixture whose insertion order matches its intended answer order is
satisfied by `ORDER BY id`. Group E found six vacuous fixtures that way.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from tests.fakes.collection_repository import FakeCollectionRepository, SeededMediaItem
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.curated_row_repository import FakeCuratedRowRepository
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.person_repository import (
    FakePersonRepository,
    SeededCredit,
    SeededWatchState,
)
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.collection import Collection
from usher.domain.curation import SLUG_PREFIX, CuratedRow
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.people import Credit, CreditKind, Person
from usher.domain.taste import GenreAffinity
from usher.domain.title import Title
from usher.domain.watch import User
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.rows import RowContext

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
USER = User(name="default", is_default=True)
SOURCE = uuid.UUID("00000000-0000-7000-8000-0000000000ff")


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


class Library:
    """One household: a catalog, its copies, its episodes and its watch state.

    Every seeder returns the id it minted, so a case can name the title it
    expects at position 0 without reaching back into the fakes.
    """

    def __init__(
        self,
        *,
        titles: FakeTitleRepository | None = None,
        media_items: FakeMediaItemRepository | None = None,
    ) -> None:
        # Both are injectable so a case can count the *statements* a row costs
        # rather than only the cards it produces. They are wired into three
        # other fakes below (`FakeCreditRepository`, the collections catalog,
        # `available_copies`), which is why a case substitutes them here rather
        # than assigning over the attributes afterwards.
        self.titles = FakeTitleRepository() if titles is None else titles
        self.media_items = FakeMediaItemRepository() if media_items is None else media_items
        self.watch_states = FakeWatchStateRepository()
        # **The fake *copies* the mapping it is constructed with**, so seeding
        # into a dict handed to the constructor reaches nothing -- and
        # `list_recent` then returns `[]` for a television household, which
        # reads exactly like a correct provider finding nothing to say. Written
        # into the fake's own dict as episodes are seeded instead.
        self.episode_series: dict[uuid.UUID, uuid.UUID] = self.watch_states._episode_series
        self.episodes = FakeEpisodeRepository()
        self.neighbors = FakeTitleNeighborRepository()
        self.people = FakePersonRepository()
        # **Wired to the same title store**, which is `FakeCreditRepository`'s
        # own instruction: two independent dicts make a *correct*
        # implementation fail rather than a wrong one pass.
        self.credits = FakeCreditRepository(self.people, self.titles)
        self.collections = FakeCollectionRepository()
        self.curated_rows = FakeCuratedRowRepository()
        # `replace_for_titles` is a replace, so incremental seeding has to hold
        # the accumulated set per title and re-send it. Keeping the accumulator
        # here rather than reaching into the fake's private list is what makes
        # `credit()` go through the port the way a derivation does.
        self._credits: dict[uuid.UUID, list[Credit]] = {}
        # `replace_for_user` is the same shape one table over, and it refuses a
        # batch carrying two `generation_id`s -- so incremental seeding holds
        # the accumulated generation per household and re-sends it.
        self._curated: dict[uuid.UUID, list[CuratedRow]] = {}
        self._generation = new_id()
        self._observed = 0

    # -- the catalog ------------------------------------------------------

    async def title(
        self,
        name: str = "An Invented Title",
        *,
        kind: TitleKind = TitleKind.MOVIE,
        year: int | None = 2024,
        genres: Sequence[str] = (),
        keywords: Sequence[str] = (),
        popularity: float | None = None,
        vote_count: int | None = None,
        owned: bool = True,
        added: datetime | None = None,
        seen: datetime | None = None,
        runtime_minutes: int | None = 120,
    ) -> uuid.UUID:
        title = Title(
            id=new_id(),
            kind=kind,
            name=name,
            sort_name=name.lower(),
            year=year,
            genres=tuple(genres),
            keywords=tuple(keywords),
            popularity=popularity,
            vote_count=vote_count,
            runtime_minutes=runtime_minutes,
            enrichment_state=EnrichmentState.ENRICHED,
        )
        await self.titles.add(title)
        # `FakeCollectionRepository` models `titles.kind` and the catalog's own
        # order because `attach_titles` refuses a series and `list_owned`
        # returns members "in release order" -- both are facts about `titles`
        # that a collection fake cannot invent. Registered here so no case has
        # to remember to, which is how the four vacuous fixtures Group G found
        # were written.
        self.collections.catalog.kinds[title.id] = kind
        self.collections.catalog.order.append(title.id)
        if owned:
            await self.copy(title.id, added=added, seen=seen)
        return title.id

    async def copy(
        self,
        title_id: uuid.UUID,
        *,
        episode_id: uuid.UUID | None = None,
        added: datetime | None = None,
        seen: datetime | None = None,
    ) -> None:
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=SOURCE,
                    external_id=str(new_id()),
                    title_id=title_id,
                    episode_id=episode_id,
                    container=None,
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=added,
                    # `last_seen_at` defaults to the *newest* instant in the
                    # library, which is what makes the RecentlyAdded distractor
                    # buildable: the nightly scan touches this column on every
                    # item every night, so a row ordered by it is the whole
                    # library in scan order.
                    last_seen_at=seen if seen is not None else NOW,
                )
            ]
        )
        self.collections.catalog.media_items.append(
            SeededMediaItem(title_id=title_id, episode_id=episode_id, available=True)
        )
        # The third fake modelling `media_items`, and the one whose read
        # deliberately does *not* bound itself to `episode_id IS NULL`: a
        # series owned only through its episode files is owned here, which is
        # what keeps `GenreAffinityProvider` and `SeasonalProvider` from being
        # films-only on a library that is 89% episodes.
        self.titles.available_copies.setdefault(title_id, []).append(episode_id)

    # -- the series tree --------------------------------------------------

    async def series(self, name: str = "An Invented Series") -> uuid.UUID:
        return await self.title(name, kind=TitleKind.SERIES)

    async def episode(
        self,
        series_id: uuid.UUID,
        *,
        season: int,
        number: int,
        name: str | None = None,
        owned: bool = True,
        played: bool = False,
    ) -> uuid.UUID:
        season_id = new_id()
        await self.episodes.upsert_seasons(
            [Season(id=season_id, title_id=series_id, season_number=season)]
        )
        one = Episode(
            id=new_id(),
            title_id=series_id,
            season_id=season_id,
            season_number=season,
            episode_number=number,
            name=name,
            runtime_minutes=45,
        )
        await self.episodes.upsert_episodes([one])
        self.episode_series[one.id] = series_id
        # **Trap 7's third fake.** `FakePersonRepository._title_of` reproduces
        # `COALESCE(w.title_id, e.title_id)` and reads this map; without it an
        # episode watch state reaches no credits at all, which is precisely the
        # films-only answer `list_recurring_for_user` exists to refuse.
        self.people.household.episode_titles[one.id] = series_id
        if owned:
            await self.copy(series_id, episode_id=one.id)
        if played:
            # Written through the episode fake's own hook *and* as a watch
            # state, because `next_up` reads the first and `list_recent` reads
            # the second -- two fakes modelling one table, and a case that
            # seeded only one would make a correct provider look broken.
            self.episodes.set_watch_state(USER.id, one.id, played=True)
            await self.watched(episode_id=one.id, played=True)
        return one.id

    # -- watch state ------------------------------------------------------

    async def watched(
        self,
        title_id: uuid.UUID | None = None,
        *,
        episode_id: uuid.UUID | None = None,
        played: bool = False,
        position_seconds: int = 0,
        runtime_seconds: int | None = 7200,
        play_count: int = 1,
        at: datetime | None = None,
    ) -> None:
        self._observed += 1
        await self.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=USER.id,
                    title_id=title_id,
                    episode_id=episode_id,
                    position_seconds=position_seconds,
                    runtime_seconds=runtime_seconds,
                    played=played,
                    play_count=play_count,
                    last_played_at=at if at is not None else days_ago(1),
                    observed_at=NOW - timedelta(seconds=10_000 - self._observed),
                )
            ]
        )
        # The same row, in the shape `FakePersonRepository` joins across. Two
        # fakes modelling one table again -- and here the mutually-exclusive
        # `(title_id, episode_id)` pair is the whole point, so it is carried
        # through verbatim rather than collapsed to a series id.
        self.people.household.watch_states.append(
            SeededWatchState(
                user_id=USER.id,
                title_id=title_id,
                episode_id=episode_id,
                played=played,
                last_played_at=at if at is not None else days_ago(1),
            )
        )

    async def in_progress(
        self, title_id: uuid.UUID, *, at: datetime, position_seconds: int = 1800
    ) -> None:
        await self.watched(title_id, played=False, position_seconds=position_seconds, at=at)

    async def finished(self, title_id: uuid.UUID, *, at: datetime, play_count: int = 1) -> None:
        """**The distractor, and it varies exactly one thing.**

        `played = True` with the resume position *kept*, so it isolates the
        `NOT played` half of `list_in_progress`' predicate. The plan's own
        headline seeding sets `played` and `position_seconds = 0` together,
        which isolates neither half -- Group E measured that and this is the
        correction. `never_started` is the separate seed for the other half.
        """
        await self.watched(
            title_id, played=True, position_seconds=5400, play_count=play_count, at=at
        )

    async def never_started(self, title_id: uuid.UUID) -> None:
        """`position_seconds = 0` with `played = False`: the *other* half of the
        predicate, alone."""
        await self.watched(title_id, played=False, position_seconds=0, at=days_ago(0.5))

    # -- people, credits and collections -----------------------------------

    async def person(self, name: str, *, tmdb_id: int | None = None) -> uuid.UUID:
        one = Person(id=new_id(), tmdb_id=tmdb_id, name=name, sort_name=name.lower())
        await self.people.upsert_many([one])
        # `upsert_many` keys anonymous people on their own id and tmdb-keyed
        # ones on the provider id, so the stored id is the minted one either
        # way -- but only because nothing here re-seeds one tmdb_id twice.
        return one.id

    async def credit(
        self,
        person_id: uuid.UUID,
        title_id: uuid.UUID,
        *,
        kind: CreditKind = CreditKind.CAST,
        job: str | None = None,
        character: str | None = None,
        billing_order: int | None = None,
    ) -> None:
        """One credit, written through both fakes that model `credits`.

        `FakeCreditRepository` holds the rows `list_for_person` reads;
        `FakePersonRepository.household.credits` holds what
        `list_recurring_for_user` groups. They are one table in Postgres and a
        case that seeded one would make a *correct* provider look broken from
        whichever side it did not seed.
        """
        one = Credit(
            id=new_id(),
            person_id=person_id,
            title_id=title_id,
            kind=kind,
            tmdb_credit_id=str(new_id()),
            character=character,
            job=job,
            billing_order=billing_order,
        )
        held = self._credits.setdefault(title_id, [])
        held.append(one)
        await self.credits.replace_for_titles(
            [title_id],
            held,
            credit_names={title_id: [self.people.stored(c.person_id).name for c in held]},
        )
        self.people.household.credits.append(
            SeededCredit(
                person_id=person_id,
                title_id=title_id,
                kind=kind,
                job=job,
                character=character,
            )
        )

    async def collection(self, name: str, title_ids: Sequence[uuid.UUID]) -> uuid.UUID:
        one = Collection(id=new_id(), tmdb_id=None, name=name)
        await self.collections.upsert_many([one])
        await self.collections.attach_titles([(title_id, one.id) for title_id in title_ids])
        return one.id

    # -- what a generation left behind -------------------------------------

    async def curated(
        self,
        card_title_ids: Sequence[uuid.UUID],
        *,
        position: int,
        title: str = "A Shelf A Model Named",
        reason: str | None = "Because you keep finishing the quiet ones.",
        slug: str | None = None,
        width: int = 1,
        user_id: uuid.UUID | None = None,
        generation_id: uuid.UUID | None = None,
        generated_at: datetime = NOW,
    ) -> CuratedRow:
        """One stored `curated_rows` record, written through the port.

        **`position` is required and `slug` is derived from it**, because the
        two are one fact in production -- `services.curation_validate` is the
        only thing that mints a curated slug and it mints it from the row's
        index. A seeder that let a case set them independently would let a
        fixture assert an ordering the write path cannot produce.

        `width` is the **generation's** padding width, not this row's: nine
        rows mint `curated-1` and ten mint `curated-01`, so it is a property of
        the batch and a case seeding ten rows passes `width=2` to every one of
        them. That instability is exactly why a curated slug is not a stable
        name across generations, and why `CuratedProvider` may treat one as
        unique only within the generation it read.
        """
        owner = USER.id if user_id is None else user_id
        row = CuratedRow(
            id=new_id(),
            user_id=owner,
            slug=f"{SLUG_PREFIX}-{position + 1:0{width}d}" if slug is None else slug,
            title=title,
            reason=reason,
            card_title_ids=tuple(card_title_ids),
            position=position,
            model_name="an-invented-model",
            generation_id=self._generation if generation_id is None else generation_id,
            generated_at=generated_at,
        )
        held = self._curated.setdefault(owner, [])
        # A second generation for the same household **replaces** the first,
        # which is what `replace_for_user` is: the accumulator is reset rather
        # than appended to, so a case seeding "last night's ten and tonight's
        # nine" gets the write path's own answer instead of a mixture no
        # generation ever produced.
        if held and held[0].generation_id != row.generation_id:
            held = []
            self._curated[owner] = held
        held.append(row)
        await self.curated_rows.replace_for_user(owner, held)
        return row

    def generation(self) -> uuid.UUID:
        """A fresh generation stamp, so a case can seed two nights in order."""
        self._generation = new_id()
        return self._generation

    # -- the context ------------------------------------------------------

    def context(
        self,
        *,
        affinities: Sequence[GenreAffinity] = (),
        now: datetime = NOW,
    ) -> RowContext:
        """The wiring `api/deps.py` builds per request, over this household.

        **`affinities` is taken as the sequence and wrapped here**, because
        every case in every `test_rows_*.py` file is about what a provider does
        with the affinities it is handed and none of them is about the field's
        laziness. The two cases that *are* about it -- the route not reading
        the household's taste to build a context, and a screen the cache can
        answer reading none at all -- build their own callable so it can count.
        """
        held = tuple(affinities)

        async def affinities_of() -> Sequence[GenreAffinity]:
            return held

        return RowContext(
            user=USER,
            # Bound at call time rather than read from the module: a
            # provider whose firing condition is "is it October" and which
            # reads the wall clock is testable only in October, and
            # `SeasonalProvider`'s *entire* behaviour is window boundaries.
            now=lambda: now,
            titles=self.titles,
            media_items=self.media_items,
            watch_states=self.watch_states,
            episodes=self.episodes,
            neighbors=self.neighbors,
            people=self.people,
            credits=self.credits,
            collections=self.collections,
            curated=self.curated_rows,
            affinities=affinities_of,
        )
