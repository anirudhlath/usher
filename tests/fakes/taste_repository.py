"""In-memory `TasteRepository`.

**The staleness predicate is the whole port, so this fake has to model it
rather than store rows.** `get()` is not a dict lookup with a filter bolted on:
it re-evaluates all three disjuncts of `STALE_TASTE` on every call, against a
watermark it computes from a `WatchStateRepository` it is handed. A fake that
merely returned whatever was last `put` would pass every case about *storage*
and none about *invalidation*, which is the only thing this port does.

**Divergences from `PostgresTasteRepository`, stated rather than discovered.**

1. **The watermark comes from a supplied repository, not from a table.** The
   real one runs `max(updated_at)` over `watch_states`; there is no such table
   here, so the fake reads `FakeWatchStateRepository`'s own states. Two fakes
   modelling one table, the arrangement `FakeTitleEmbeddingRepository` and
   `FakeTitleRepository` already carry -- and passing no repository at all is
   meaningful, modelling a household whose history is empty.
2. **`updated_at` is the fake's `observed_at`.** `trg_watch_states_set_updated_
   at` owns that column against Postgres and this fake stores the merge's own
   instant there. Already the seventh divergence recorded on
   `FakeWatchStateRepository`; it means a case that needs the *write* instant
   to differ from the *observation* instant is an integration case.
3. **No `halfvec` quantisation.** Vectors round-trip exactly here and to a
   measured max cosine error of 1.21e-04 against Postgres, which is why the
   unit file asserts to `abs=1e-9` and the integration file to `abs=1e-3`.

`writes` counts `put` calls, which is what makes "a refusal is written once and
re-claimed exactly once" assertable at all -- the outcome that distinguishes a
written refusal from a household recomputed on every read of every home screen
forever is a *count*, not a value.
"""

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime

from usher.ports.repository import (
    LibraryGenres,
    MediaItemRepository,
    StoredTaste,
    TasteRepository,
    TitleRepository,
    WatchStateRepository,
)


class FakeTasteRepository(TasteRepository):
    def __init__(
        self,
        watch_states: WatchStateRepository | None = None,
        *,
        titles: TitleRepository | None = None,
        media_items: MediaItemRepository | None = None,
    ) -> None:
        self.rows: dict[uuid.UUID, StoredTaste] = {}
        self.writes = 0
        self._watch_states = watch_states
        self._titles = titles
        self._media_items = media_items

    def bind(self, watch_states: WatchStateRepository) -> None:
        """Attach the history this fake's watermark is computed from.

        A setter rather than a constructor argument only because the two fakes
        are usually built in the other order; the real repository reads one
        table and has no equivalent.
        """
        self._watch_states = watch_states

    async def get(self, user_id: uuid.UUID, *, model_name: str) -> StoredTaste | None:
        stored = self.rows.get(user_id)
        # Disjunct 1: no row at all.
        if stored is None:
            return None
        # Disjunct 2: a different embedder. The stored vector is from another
        # space and comparing it to a current one is confident nonsense.
        if stored.model_name != model_name:
            return None
        # Disjunct 3: the household's history has moved. `IS DISTINCT FROM`,
        # never `<` -- a deleted state LOWERS the max and a cleared history
        # makes it NULL, and both must invalidate.
        if stored.source_watermark != await self.watermark(user_id):
            return None
        # A row carrying `centroid=None` is returned here, and that is the
        # written refusal rather than an absence. Collapsing it into `None`
        # is the recompute-forever bug the column exists to prevent.
        return stored

    async def put(self, taste: StoredTaste) -> None:
        self.writes += 1
        self.rows[taste.user_id] = taste

    async def library_genre_counts(self) -> LibraryGenres:
        # Fourth divergence: the real one is a join, and this walks two other
        # fakes. `owned_title_ids` is asked rather than reimplemented, so the
        # `episode_id IS NULL` bound and the deliberate *absence* of an
        # availability filter are modelled once, by the fake that owns them,
        # rather than twice and eventually differently.
        if self._titles is None or self._media_items is None:
            return LibraryGenres(counts={}, tagged_titles=0)
        stored = getattr(self._titles, "stored", None)
        catalog = stored() if callable(stored) else []
        owned = await self._media_items.owned_title_ids([title.id for title in catalog])
        counts: dict[str, int] = {}
        tagged = 0
        for title in catalog:
            # An untagged title is in neither the counts nor the total -- not
            # a genre named "". `titles.genres` defaults to `{}` and the
            # skeleton tier is largely empty, so a "" bucket would be the
            # single largest genre in most libraries.
            if title.id not in owned or not title.genres:
                continue
            tagged += 1
            for genre in set(title.genres):
                counts[genre] = counts.get(genre, 0) + 1
        return LibraryGenres(counts=counts, tagged_titles=tagged)

    async def watermark(self, user_id: uuid.UUID) -> AwareDatetime | None:
        if self._watch_states is None:
            return None
        states = getattr(self._watch_states, "_states", {})
        stamps = [state.updated_at for state in states.values() if state.user_id == user_id]
        if not stamps:
            return None
        newest: datetime = max(stamps)
        # Normalised the way asyncpg hands a `timestamptz` back, so a case
        # comparing a stored watermark to a freshly-read one cannot pass here
        # and fail there on a tzinfo mismatch alone.
        return newest.astimezone(UTC) if newest.tzinfo is not None else newest.replace(tzinfo=UTC)


def stored_taste(
    user_id: uuid.UUID,
    *,
    centroid: tuple[float, ...] | None = None,
    model_name: str = "fake:test-384",
    source_watermark: AwareDatetime | None = None,
    title_count: int = 0,
    computed_at: AwareDatetime | None = None,
) -> StoredTaste:
    """A `StoredTaste` with the fields a case does not care about filled in.

    A test-double builder, not a port method. It exists so a contract case can
    say what it is varying and nothing else -- six positional fields is six
    chances to write the wrong one in the wrong slot and have the case still
    pass.
    """
    return StoredTaste(
        user_id=user_id,
        centroid=centroid,
        model_name=model_name,
        source_watermark=source_watermark,
        title_count=title_count,
        computed_at=computed_at if computed_at is not None else datetime(2026, 8, 4, tzinfo=UTC),
    )
