"""The taste profile: a stored centroid plus the library genres it is read against."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import AwareDatetime

__all__ = [
    "LibraryGenres",
    "StoredTaste",
    "TasteRepository",
]


@dataclass(frozen=True, slots=True)
class StoredTaste:
    """One user's cached centroid, and the evidence for its currency."""

    user_id: uuid.UUID
    centroid: tuple[float, ...] | None
    model_name: str
    source_watermark: AwareDatetime | None
    title_count: int
    computed_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class LibraryGenres:
    """The genre baseline: how the household's **owned** shelf is composed.

    A taste question rather than a catalog one, which is why it is here and
    not on `TitleRepository`.

    `tagged_titles` is carried alongside `counts` and must come from the same
    read. `sum(counts.values())` over-counts, because a title carries several
    genres and the shares do not partition; and two statements could disagree,
    letting a share exceed 1 for a genre nobody added -- a plausible number
    rather than a visible fault.

    An untagged title is in neither the counts nor the total. Leaving it in
    the denominator dilutes every share by the tagged fraction and inflates
    every lift uniformly, which on a mostly-skeleton catalog fires the
    minimum-lift floor for everything at once.
    """

    counts: Mapping[str, int]
    tagged_titles: int


class TasteRepository(ABC):
    """`user_taste` — the per-user centroid, invalidated by fingerprint.

    Nothing invalidates this on watch-state change. The nightly walk merges a
    whole library's worth of watch states, so one invalidation per merged row
    is a million invalidations a night for at most one useful recomputation
    per user. The merge path writes nothing here and does not know it exists.

    Derived state carries its fingerprint instead: the stored row holds the
    `max(updated_at)` of the watch states it was computed from, and a demand
    read recomputes when the household's current max differs. Same shape as
    `title_embeddings`' `source_fingerprint`, on a different key.

    Methods flush and return, never commit.
    """

    @abstractmethod
    async def library_genre_counts(self) -> LibraryGenres:
        """How the **owned** library is composed by genre."""

    @abstractmethod
    async def get(self, user_id: uuid.UUID, *, model_name: str) -> StoredTaste | None:
        """The cached row **only if it is not stale**, else `None`.

        The staleness check lives here rather than in the service because the
        predicate is three clauses over two tables including a `max()`
        subquery: a service-side check would be `get()` plus `watermark()`
        plus a comparison -- two round trips and a race between them. `None`
        means "recompute", for any of the three reasons at once: no row, a
        different embedder, or a moved watermark.

        A returned row may carry `centroid=None`. That is a current, readable
        refusal rather than an absence; a caller that treats it as `None`
        recomputes forever.
        """

    @abstractmethod
    async def latest(self, user_id: uuid.UUID) -> StoredTaste | None:
        """The stored row for this household, **whatever model wrote it**.

        Read-only, with no staleness predicate.
        """

    @abstractmethod
    async def put(self, taste: StoredTaste) -> None:
        """Upsert one user's row, refusals included.

        Sets `computed_at` from the value rather than from a server default, so
        the artefact's age is the service's injected clock and a test does not
        have to wait for one.
        """

    @abstractmethod
    async def watermark(self, user_id: uuid.UUID) -> AwareDatetime | None:
        """`max(watch_states.updated_at)` for this user; `None` on an empty history.

        Read *before* the window, never after. A merge landing between the
        window read and the write would otherwise be stamped as included when
        it was not, leaving a stale centroid carrying a watermark that claims
        freshness -- which no later read can detect. Reading it first fails
        the harmless way: one redundant recomputation.

        `updated_at`, not `last_played_at`. The merge touches `updated_at`
        and it carries both an `onupdate` and the table's trigger, so it is
        monotone. A re-merge that raises `play_count` without moving
        `last_played_at` is the rewatch the centroid's weights care about.
        """
