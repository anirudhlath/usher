"""Behaviour every `TasteRepository` implementation must satisfy.

**This suite is almost entirely about one predicate**, because that is almost
all this port does. `get()` is not a lookup: it is `STALE_TASTE` evaluated over
two tables, and an implementation that stored and returned rows faithfully
while getting any one of its three disjuncts wrong would pass every case about
*storage* and serve a confidently wrong centroid forever.

**Three of the cases exist to separate `IS DISTINCT FROM` from `<`**, and only
the first of the three is the obvious one:

- a **newer** watch state raises `max(updated_at)`. Both spellings catch it,
  and a suite holding only this case is green against the bug.
- a **deleted** watch state *lowers* it. `<` never looks backwards, so it goes
  on serving a centroid computed over a row that no longer exists -- for a
  household that unwatched something, forever.
- a **cleared** history makes the subquery `NULL`, and `stored < NULL` is
  `NULL`, which is not true. So `<` never recomputes for a household whose
  history was wiped, which is the same failure with the same cause and a
  different shape.

Subclass and provide `repository`, `user_id` and `other_user_id` (which must
name users that actually exist, for an implementation with foreign keys), plus
the three history hooks below. `WatchStateRepository` has no delete method --
deliberately, PRD 02 hard-deletes nothing through a port -- so the hooks reach
past it, and each arm reaches past it in its own way.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usher.ports.repository import StoredTaste, TasteRepository

EARLIER = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
LATER = EARLIER + timedelta(days=3)
COMPUTED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
MODEL = "fake:test-384"

# 384 lanes, because that is what `halfvec(384)` accepts and a shorter vector
# would have to be padded by one arm's seeder and not the other's -- the
# padding would then be the suite's behaviour rather than the port's. Group F
# hit the identical constraint on `halfvec(1128)`.
_DIMENSION = 384


def _vector(lead: float) -> tuple[float, ...]:
    """A vector distinguishable from another by its first lane alone.

    Deliberately not a unit vector and deliberately not planted: nothing in
    this suite computes a cosine. `tests/unit/test_services_taste.py` owns
    every angle; this file owns storage and staleness, and a vector here only
    has to round-trip recognisably.
    """
    return (lead,) + (0.0,) * (_DIMENSION - 1)


class TasteRepositoryContract:
    async def add_history(self, user_id: uuid.UUID, *, at: datetime) -> uuid.UUID:
        """Write one watch state for `user_id` whose stored `updated_at` is
        `at`, and return something `drop_history` can remove."""
        raise NotImplementedError

    async def drop_history(self, handle: uuid.UUID) -> None:
        """Remove the state `add_history` returned."""
        raise NotImplementedError

    async def add_title(self, genres: tuple[str, ...], *, owned: bool) -> uuid.UUID:
        """A catalog title carrying `genres`, with or without an owned copy.

        `owned=False` is not decoration: the whole point of the baseline is
        that it is the household's **choice set**, so a suite that could only
        create owned titles could not tell the owned library from the catalog
        -- which is the distinction the baseline decision turns on.
        """
        raise NotImplementedError

    async def add_owned_copy(self, title_id: uuid.UUID) -> None:
        """A second title-level media item for `title_id`.

        A title owned on three sources is owned once, and only a *second* copy
        can show that -- which the first draft of this suite got wrong by
        asking for zero of them.
        """
        raise NotImplementedError

    async def add_owned_episode_copy(self, title_id: uuid.UUID, *, copies: int) -> None:
        """`copies` media items for `title_id` that each name an **episode**.

        The bound `owned_title_ids` spells `episode_id IS NULL`, and this is
        what makes its absence visible.
        """
        raise NotImplementedError

    def stored(
        self,
        user_id: uuid.UUID,
        *,
        centroid: tuple[float, ...] | None,
        watermark: datetime | None,
        model_name: str = MODEL,
        title_count: int = 12,
    ) -> StoredTaste:
        return StoredTaste(
            user_id=user_id,
            centroid=centroid,
            model_name=model_name,
            source_watermark=watermark,
            title_count=title_count,
            computed_at=COMPUTED_AT,
        )

    # -- the watermark -----------------------------------------------------

    async def test_the_watermark_is_none_for_a_household_with_no_history(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """`max()` over an empty set, and `None` is the honest answer.

        It is also why `user_taste.source_watermark` is nullable against the
        plan's `NOT NULL`: with nothing to write, such a household's refusal
        could not be stored at all, so it would be the one recomputed on every
        read of every home screen forever.
        """
        assert await repository.watermark(user_id) is None

    async def test_the_watermark_is_the_newest_update_and_not_the_oldest(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """`max`, seeded out of order so a `min` and a "whatever came last"
        both answer differently."""
        await self.add_history(user_id, at=LATER)
        await self.add_history(user_id, at=EARLIER)

        assert await repository.watermark(user_id) == LATER

    async def test_the_watermark_is_scoped_to_one_user(
        self, repository: TasteRepository, user_id: uuid.UUID, other_user_id: uuid.UUID
    ) -> None:
        """Without the scope every household in the deployment shares one
        watermark, so one member watching anything invalidates everybody's
        centroid -- a recompute storm that looks exactly like a working cache
        from the outside."""
        await self.add_history(other_user_id, at=LATER)

        assert await repository.watermark(user_id) is None

    # -- storage -----------------------------------------------------------

    async def test_an_unwritten_household_has_no_stored_centroid(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        assert await repository.get(user_id, model_name=MODEL) is None

    async def test_a_stored_centroid_round_trips_and_stays_readable(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """The cache actually caching. Nothing has moved, so nothing is
        stale."""
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))

        found = await repository.get(user_id, model_name=MODEL)

        assert found is not None
        assert found.centroid is not None
        assert found.centroid[0] == pytest.approx(0.5, abs=1e-3)
        assert found.title_count == 12
        assert found.source_watermark == EARLIER

    async def test_a_second_put_replaces_rather_than_duplicating(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """The primary key *is* the user id, so two rows for one household is
        a state no consumer could interpret -- and an implementation that
        inserted rather than upserted would raise here rather than answer
        ambiguously, which is the failure worth having."""
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.25), watermark=EARLIER))
        await repository.put(self.stored(user_id, centroid=_vector(0.75), watermark=EARLIER))

        found = await repository.get(user_id, model_name=MODEL)

        assert found is not None
        assert found.centroid is not None
        assert found.centroid[0] == pytest.approx(0.75, abs=1e-3)

    async def test_a_refusal_is_readable_and_carries_a_null_centroid(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """**The written refusal.** Kills the implementation that treats
        "stored with no centroid" as "not stored".

        The distinction is invisible in the value and total in the cost: under
        that implementation a household below the minimum is recomputed on
        every read of every home screen forever, and the title that lifts it
        over the minimum does not re-claim the centroid *once* -- it re-claims
        it always. `title_embeddings` writes a NULL vector for a degenerate
        document for exactly this reason, and this project has already shipped
        the missing-refusal bug once, in the watch-history repair.
        """
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=None, watermark=EARLIER, title_count=3))

        found = await repository.get(user_id, model_name=MODEL)

        assert found is not None
        assert found.centroid is None
        assert found.title_count == 3

    async def test_a_refusal_for_a_household_with_no_history_is_readable(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """The `NULL` watermark, both stored and computed, and the reason the
        column is nullable.

        `NULL IS DISTINCT FROM NULL` is **false**, so a refusal written for a
        household that has watched nothing reads as current. Under the plan's
        `NOT NULL` there would be no value to write at all, and that household
        -- the emptiest and cheapest to recompute, but also the one every fresh
        install starts as -- would be the single case the cache never covers.
        """
        await repository.put(self.stored(user_id, centroid=None, watermark=None, title_count=0))

        found = await repository.get(user_id, model_name=MODEL)

        assert found is not None
        assert found.centroid is None
        assert found.source_watermark is None

    # -- the unpredicated read ---------------------------------------------

    async def test_latest_is_none_for_a_household_with_nothing_stored(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """The absent arm. `None` here means *nothing was ever computed*, which
        a ranking caller reads as "no term" -- and which is the shipped state
        of every deployment whose worker has not run."""
        assert await repository.latest(user_id) is None

    async def test_latest_answers_a_row_written_under_another_model(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """**The whole reason this method exists.**

        A request holds no embedder, so it has no `model_name` to ask `get()`
        with -- and `get()` would answer `None` for every name but the one that
        happens to be stored. `latest` answers the row and lets the caller read
        the name off it, which is what turns "a worker computed a centroid" into
        "a request can serve one".

        Kills the implementation that spells this as `get(user_id,
        model_name=<anything>)`: against the deployment's *own* model it looks
        right, and against a stored row written by any other checkpoint --
        which includes every deployment that has swapped one -- it silently
        answers nothing at all.
        """
        await self.add_history(user_id, at=EARLIER)
        await repository.put(
            self.stored(
                user_id,
                centroid=_vector(0.5),
                watermark=EARLIER,
                model_name="fake:some-other-checkpoint",
            )
        )

        found = await repository.latest(user_id)

        assert found is not None
        assert found.model_name == "fake:some-other-checkpoint"
        assert found.centroid is not None
        assert found.centroid[0] == pytest.approx(0.5, abs=1e-3)

    async def test_latest_answers_a_written_refusal_rather_than_raising(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """The second arm, and it is the state `StoredTaste`'s docstring exists
        to make representable: a household below the minimum has a **row** whose
        centroid is NULL.

        Answered, never raised, and never collapsed into "no row": a ranking
        caller reads it as "no term", which is the same answer -- but an
        implementation that raised would 500 a search for the emptiest
        household in the deployment, and one that could not represent it at all
        is the recompute-forever bug the column exists to prevent.
        """
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=None, watermark=EARLIER, title_count=3))

        found = await repository.latest(user_id)

        assert found is not None
        assert found.centroid is None
        assert found.title_count == 3

    async def test_latest_answers_a_row_that_get_calls_stale(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """**No staleness predicate**, and the two reads disagreeing here is the
        design rather than a leak.

        `STALE_TASTE` answers *"should I recompute?"*, and a caller with no
        embedder cannot act on it. Worse, the watch state that moves the
        watermark is the same watch state the centroid was computed from -- so a
        `latest` that inherited the predicate would withhold the term from
        exactly the households that watch things, which is all of them.

        `get()` is asserted alongside, because "both answer the row" is what a
        `latest` implemented as `return await self.get(...)` produces and this
        case would otherwise ratify it.
        """
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))
        await self.add_history(user_id, at=LATER)

        assert await repository.get(user_id, model_name=MODEL) is None
        found = await repository.latest(user_id)
        assert found is not None
        assert found.source_watermark == EARLIER

    async def test_latest_is_scoped_to_one_household(
        self, repository: TasteRepository, user_id: uuid.UUID, other_user_id: uuid.UUID
    ) -> None:
        """An unscoped read hands one household's taste to another, and the
        result renders perfectly: a populated, correctly-shaped, 384-lane unit
        vector that ranks this member's search by somebody else's viewing."""
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))

        assert await repository.latest(other_user_id) is None

    async def test_latest_never_writes(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """Read-only, asserted as a *state* rather than as a signature.

        The boundary this holds is that a request path cannot mint a
        `user_taste` row under a model it does not have — `TasteService.
        centroid` writes its refusals, and a request doing the same would stamp
        an absent model onto the household's cache and then invalidate it on
        every read.
        """
        assert await repository.latest(user_id) is None
        assert await repository.latest(user_id) is None

        assert await repository.get(user_id, model_name=MODEL) is None

    # -- the three staleness disjuncts -------------------------------------

    async def test_a_different_model_name_makes_the_stored_centroid_unreadable(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """Kills the implementation that stores the vector and forgets the
        model, so a checkpoint swap serves vectors from a different space at
        full confidence -- populated, correctly-shaped, and meaningless."""
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))

        assert await repository.get(user_id, model_name="fake:other-checkpoint") is None

    async def test_a_newer_watch_state_makes_the_stored_centroid_unreadable(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """The requirement itself, and the only one of the three a `<`
        watermark comparison also satisfies."""
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))
        await self.add_history(user_id, at=LATER)

        assert await repository.get(user_id, model_name=MODEL) is None

    async def test_a_deleted_watch_state_makes_the_stored_centroid_unreadable(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """**Kills the `<` spelling, which only ever looks forward.**

        The household unwatched something; the max fell back to the older
        state. Under `<` the stored watermark is now *greater* than the
        household's max, the comparison is false, and the centroid computed
        over a row that no longer exists is served forever.
        """
        await self.add_history(user_id, at=EARLIER)
        newest = await self.add_history(user_id, at=LATER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=LATER))

        await self.drop_history(newest)

        assert await repository.get(user_id, model_name=MODEL) is None

    async def test_a_cleared_history_makes_the_stored_centroid_unreadable(
        self, repository: TasteRepository, user_id: uuid.UUID
    ) -> None:
        """**Kills the `<` spelling again, through `NULL` rather than through
        an ordering** -- a different failure with the same cause, which is why
        both cases exist.

        With no states left the subquery is `NULL`, and `stored < NULL` is
        `NULL`, which is not true. So a `<` implementation never recomputes for
        a household whose history was cleared: it serves that household's old
        taste for the life of the deployment.
        """
        only = await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))

        await self.drop_history(only)

        assert await repository.get(user_id, model_name=MODEL) is None

    async def test_another_households_watching_does_not_invalidate_this_one(
        self, repository: TasteRepository, user_id: uuid.UUID, other_user_id: uuid.UUID
    ) -> None:
        """The scope, from the staleness side rather than the watermark side.

        An unscoped subquery makes every centroid in the deployment stale the
        moment any member watches anything -- which reads as a cache that
        works (it stores, it returns) and never hits.
        """
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))

        await self.add_history(other_user_id, at=LATER)

        assert await repository.get(user_id, model_name=MODEL) is not None

    async def test_one_households_centroid_is_not_returned_for_another(
        self, repository: TasteRepository, user_id: uuid.UUID, other_user_id: uuid.UUID
    ) -> None:
        await self.add_history(user_id, at=EARLIER)
        await repository.put(self.stored(user_id, centroid=_vector(0.5), watermark=EARLIER))

        assert await repository.get(other_user_id, model_name=MODEL) is None

    # -- the genre baseline ------------------------------------------------

    async def test_the_baseline_counts_owned_titles_and_not_the_catalog(
        self, repository: TasteRepository
    ) -> None:
        """**The decision the whole affinity rests on.**

        A household cannot watch what it does not own, so the baseline is the
        owned shelf rather than the 1.27M-row catalog. Against a global
        baseline a household that owns nothing but horror reads as an
        overwhelming horror affinity — and the library made that choice, so the
        row's reason string would be word-for-word false.
        """
        await self.add_title(("horror",), owned=True)
        await self.add_title(("horror",), owned=True)
        await self.add_title(("drama",), owned=False)
        await self.add_title(("drama",), owned=False)
        await self.add_title(("drama",), owned=False)

        library = await repository.library_genre_counts()

        assert library.counts == {"horror": 2}
        assert library.tagged_titles == 2

    async def test_a_title_carrying_two_genres_counts_once_under_each(
        self, repository: TasteRepository
    ) -> None:
        """The shares deliberately do not partition, so `tagged_titles` is
        **not** `sum(counts.values())` and must not be derived from it.

        Dividing by a title's genre count to force a partition would make a
        two-genre title contribute half the evidence of a one-genre title,
        which is a statement about TMDb's tagging density rather than about the
        household.
        """
        await self.add_title(("horror", "comedy"), owned=True)
        await self.add_title(("horror",), owned=True)

        library = await repository.library_genre_counts()

        assert library.counts == {"horror": 2, "comedy": 1}
        assert library.tagged_titles == 2

    async def test_an_untagged_owned_title_is_in_neither_the_counts_nor_the_total(
        self, repository: TasteRepository
    ) -> None:
        """`titles.genres` is `ARRAY(Text) NOT NULL DEFAULT '{}'` and the
        skeleton tier is largely empty, so untagged titles are most of a real
        library.

        Left in the denominator they divide every `share_library` by the tagged
        fraction and inflate every lift uniformly — which on a mostly-skeleton
        catalog makes the minimum-lift floor fire for everything at once. And a
        `""` bucket would be the single largest "genre" in most deployments.
        """
        await self.add_title(("horror",), owned=True)
        for _ in range(9):
            await self.add_title((), owned=True)

        library = await repository.library_genre_counts()

        assert library.counts == {"horror": 1}
        assert library.tagged_titles == 1
        assert "" not in library.counts

    async def test_a_title_owned_many_times_over_counts_once(
        self, repository: TasteRepository
    ) -> None:
        """A title owned on three sources is owned once.

        Fails the join-plus-no-dedup spelling, which counts it three times —
        inflating `tagged_titles` and every genre that title carries, unequally,
        by however many copies the household happens to hold.
        """
        title_id = await self.add_title(("horror",), owned=True)
        await self.add_owned_copy(title_id)
        await self.add_owned_copy(title_id)
        await self.add_title(("horror",), owned=True)

        library = await repository.library_genre_counts()

        assert library.counts == {"horror": 2}
        assert library.tagged_titles == 2

    async def test_an_episode_copy_does_not_make_its_series_count_many_times(
        self, repository: TasteRepository
    ) -> None:
        """**`episode_id IS NULL`, and it is the whole bound.**

        An episode's `MediaItem` carries its *series'* `title_id`, so without
        the clause one 20,000-episode series decides the entire genre baseline
        on its own — and the resulting affinities are populated, plausible and
        about one show.
        """
        series_id = await self.add_title(("drama",), owned=True)
        await self.add_owned_episode_copy(series_id, copies=12)
        await self.add_title(("horror",), owned=True)

        library = await repository.library_genre_counts()

        assert library.counts == {"drama": 1, "horror": 1}
        assert library.tagged_titles == 2

    async def test_a_series_owned_only_through_its_episodes_is_not_in_the_baseline(
        self, repository: TasteRepository
    ) -> None:
        """The **cost** of `episode_id IS NULL`, pinned as behaviour rather
        than left as a comment.

        `owned_title_ids` states it exactly — *"a library that reported
        episodes but never their series row reads as not-owned for that
        series"* — and the baseline inherits that definition on purpose: one
        definition of "owned", or the affinity's denominator and every other
        ownership question in the codebase drift apart silently.

        Without this case an `EXISTS` spelling with the clause dropped is an
        equivalent mutant: `EXISTS` already answers once per title, so the
        clause changes nothing at all unless a series has episode copies and no
        title-level copy of its own.
        """
        series_id = await self.add_title(("drama",), owned=False)
        await self.add_owned_episode_copy(series_id, copies=6)
        await self.add_title(("horror",), owned=True)

        library = await repository.library_genre_counts()

        assert library.counts == {"horror": 1}
        assert library.tagged_titles == 1

    async def test_an_empty_catalog_has_an_empty_baseline_rather_than_raising(
        self, repository: TasteRepository
    ) -> None:
        """The denominator a caller must not divide by. `TasteService` checks
        `tagged_titles == 0` and returns no affinities; a naive caller divides
        and raises in the request path."""
        library = await repository.library_genre_counts()

        assert library.counts == {}
        assert library.tagged_titles == 0
