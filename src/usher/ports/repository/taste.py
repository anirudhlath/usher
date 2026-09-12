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

    Task 23's denominator, and it is a taste question rather than a catalog
    one, which is why it lives on this port rather than on `TitleRepository`.

    **`tagged_titles` is carried alongside `counts` rather than being derivable
    from it, and it must come from the same read.** `sum(counts.values())`
    over-counts: a title carries two to four genres, so the shares deliberately
    do not partition. And two separate statements could disagree -- a title
    landing between them makes `share_library` exceed 1 for a genre nobody
    added, which reads as a plausible number rather than as a fault.

    **An untagged title is in neither the counts nor the total.** `titles.
    genres` is `ARRAY(Text) NOT NULL DEFAULT '{}'` and the skeleton tier is
    largely empty, so leaving untagged titles in the denominator would dilute
    every `share_library` by the tagged fraction and inflate every lift
    uniformly -- which on a mostly-skeleton catalog makes the minimum-lift
    floor fire for everything at once. Excluded from both sides, an untagged
    title changes no answer at all.
    """

    counts: Mapping[str, int]
    tagged_titles: int


class TasteRepository(ABC):
    """`user_taste` — the per-user centroid, invalidated by fingerprint.

    **PRD 06 says the centroid is *"invalidated on watch-state change"* and
    that is trap 5.** The nightly walk merges up to 1,126,789 watch states, so
    one invalidation per merged row is a million invalidations a night for at
    most one useful recomputation per user — the exact fan-out PRD 07 refuses
    for `watchstate.updated`. Nothing publishes anything here. The merge path
    writes nothing to `user_taste` and does not know it exists.

    Instead this is ADR-0020's scheme applied per user
    (`docs/prd/decisions/0020-derived-state-carries-its-fingerprint.md`):
    the stored row carries the `max(updated_at)` of
    the watch states it was computed from, and a demand read recomputes when
    the household's current max differs. Same shape as `title_embeddings`'
    `source_fingerprint`, on a different key.

    Same session ownership as every other repository here: methods flush and
    return, and never commit.
    """

    @abstractmethod
    async def library_genre_counts(self) -> LibraryGenres:
        """How the **owned** library is composed by genre."""

    @abstractmethod
    async def get(self, user_id: uuid.UUID, *, model_name: str) -> StoredTaste | None:
        """The cached row **only if it is not stale**, else `None`.

        The staleness check lives here rather than in the service, which is the
        opposite of where meaning usually goes in this codebase, and it is
        deliberate: the predicate is three clauses over two tables including a
        `max()` subquery, so a service-side check would be `get()` plus
        `watermark()` plus a comparison — two round trips and a race between
        them. `None` means "recompute", and it means it for all three reasons
        at once: no row, a different embedder, or a moved watermark.

        **A returned row may carry `centroid=None`.** That is a current,
        readable *refusal* and not an absence; a caller that treats it as
        `None` has reintroduced the recompute-forever bug the column exists to
        prevent.
        """

    @abstractmethod
    async def latest(self, user_id: uuid.UUID) -> StoredTaste | None:
        """The stored row for this household, **whatever model wrote it** — read-only, and
        no staleness predicate.
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
        """`max(watch_states.updated_at)` for this user; `None` on an empty
        history.

        **Read *before* the window, never after.** A merge landing between the
        window read and the write would otherwise be stamped as included when
        it was not, and the stored centroid would then be stale while carrying
        a watermark claiming freshness — self-certifying staleness, which no
        later read can detect. Reading it first makes the failure the harmless
        direction: one redundant recomputation.

        **`updated_at`, not `last_played_at`.** `updated_at` is what the merge
        touches and it carries both an `onupdate` and the table's trigger, so
        it is monotone and always moves. A re-merge that raises `play_count`
        without moving `last_played_at` is exactly the `completed` ->
        `rewatched` promotion the centroid's weights care about, and a
        `last_played_at` watermark would miss every rewatch.
        """
