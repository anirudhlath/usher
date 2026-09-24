"""The row abstractions: what a provider proposes and what a row builds."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from pydantic import AwareDatetime

from usher.domain.rows import BuiltRow, DisplayHint, RowFamily
from usher.domain.taste import GenreAffinity
from usher.domain.watch import User
from usher.ports.repository import (
    CollectionRepository,
    CreditRepository,
    CuratedRowRepository,
    EpisodeRepository,
    ImageRepository,
    MediaItemRepository,
    PersonRepository,
    TitleNeighborRepository,
    TitleRepository,
    WatchStateRepository,
)


@dataclass(frozen=True, slots=True)
class RowContext:
    """Everything a row may reach, for one request, for one user."""

    user: User
    now: Callable[[], AwareDatetime]
    titles: TitleRepository
    media_items: MediaItemRepository
    watch_states: WatchStateRepository
    episodes: EpisodeRepository
    neighbors: TitleNeighborRepository
    # A field lands here only with the provider that reads it: most providers
    # do not mention these three at all, which is the property this bag
    # exists to have.
    people: PersonRepository
    credits: CreditRepository
    collections: CollectionRepository
    affinities: Callable[[], Awaitable[Sequence[GenreAffinity]]]
    # `list_for_user` answers the newest generation, so `CuratedProvider`
    # never asks which night it is looking at.
    curated: CuratedRowRepository
    # Read by `BaseRow.hydrate` rather than by any single provider, which is
    # why it is a port here and not a mapping the composer computes.
    images: ImageRepository


class Row(ABC):
    """A named, ordered shelf of titles, able to build itself.

    The properties are abstract, not bare class annotations: an annotation is
    a class variable declaration, so a subclass that forgot to set one would
    inherit `None` and fail at render time rather than at instantiation.

    `build` returns a `BuiltRow`, never `BuiltRow | None`. With the optional,
    `usher.home.rows.dropped` cannot tell a provider working correctly on a
    quiet household from a provider that never fired. An empty row is
    `BuiltRow(cards=())`, spelled `empty()` on `BaseRow`.
    """

    @property
    @abstractmethod
    def slug(self) -> str:
        """Stable identifier for this shelf.

        `"continue-watching"`, `"because-you-watched-<seed>"`.

        Unique within one composed screen, and **not** something the composer branches
        on -- a per-seed slug is a value that varies with the catalog.
        """

    @property
    @abstractmethod
    def title(self) -> str:
        """What the shelf is called on screen."""

    @property
    @abstractmethod
    def reason(self) -> str | None:
        """The subtitle, written to be **spoken aloud** rather than merely displayed.

        A real constraint on the field: "Because you watched Dune" is
        speakable, a cosine and a seed id are not.

        `None` for a shelf that needs no explaining. A stored reason passes
        through `None` included, because validation turns a blank one into
        `None` rather than `""`.
        """

    @property
    @abstractmethod
    def family(self) -> RowFamily:
        """The diversity key.

        The composer's constraints -- "no three consecutive similarity rows; cap per
        family" -- are stated in families, so a row that could not name its own would
        make both inexpressible.
        """

    @property
    @abstractmethod
    def display_hint(self) -> DisplayHint:
        """A hint, never a layout.

        A property of the shelf, which is why it is here and not on `RowCard`.
        """

    @property
    @abstractmethod
    def ttl(self) -> timedelta:
        """How long a *built* result may be cached.

        Carried onto `BuiltRow` when the row builds, so the cached artefact
        is self-describing.
        """

    @abstractmethod
    async def build(self, ctx: RowContext) -> BuiltRow:
        """Hydrate this shelf's cards.

        May legitimately return a row with no cards: a seed can vanish
        between `propose` and `build`, and the composer drops empties.
        """


@dataclass(frozen=True, slots=True)
class ScoredRow:
    """One proposal: a row, what it is worth, and whether it is pinned."""

    row: Row
    score: float
    pinned: bool = False


class RowProvider(ABC):
    """Proposes 0..n rows for one context.

    A provider proposes; the composer decides what is shown.
    """

    @property
    @abstractmethod
    def slug_prefix(self) -> str:
        """This provider's stable identifier: `"continue-watching"`, `"because-you-watched"`."""

    @abstractmethod
    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """Return 0..n candidate rows with relevance scores."""


__all__ = ["Row", "RowContext", "RowProvider", "ScoredRow"]
