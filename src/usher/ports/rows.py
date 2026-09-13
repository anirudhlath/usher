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
    # Group B's three, landing with the providers that read them.
    # `FranchiseProvider` reads `collections.list_owned`; `PeopleProvider`
    # reads `people.list_recurring_for_user` for its rows and
    # `credits.list_for_person` for their cards. The other seven providers do
    # not mention them, which is the property this bag exists to have.
    people: PersonRepository
    credits: CreditRepository
    collections: CollectionRepository
    # The thirteenth, argued above.
    affinities: Callable[[], Awaitable[Sequence[GenreAffinity]]]
    # M8's one, and the fourteenth by that same historical count. It lands with
    # `CuratedProvider` (Task 15) rather than with the port and the table it
    # reads (Task 9), which is the discipline `search` and `taste` did not
    # have. `list_for_user` answers the newest generation, so the provider
    # never asks which night it is looking at.
    curated: CuratedRowRepository
    # M9's one, the thirteenth field, and its reader is `BaseRow.hydrate` rather than
    # any single provider -- which is a first for this bag and is the reason it is a
    # *port* here rather than a mapping the composer computes.
    images: ImageRepository


class Row(ABC):
    """A named, ordered shelf of titles, able to build itself.

    The six properties are abstract rather than bare class annotations,
    which is what PRD 06's sketch spells them as. A bare annotation is a
    *class variable declaration*: every subclass that forgot to set one would
    inherit `None` and fail at render time rather than at instantiation, which
    is exactly the failure ADR-0001 chose ABCs to avoid.

    `build` returns a `BuiltRow`, never `BuiltRow | None`. With the optional,
    the composer's drop-empties step is two predicates over two states that
    have already been merged before it runs -- and `usher.home.rows.dropped`
    then cannot count "a provider working correctly on a quiet household"
    separately from "a provider that never fired". Returning an empty
    `BuiltRow` is expressible because `BuiltRow(cards=())` is constructible;
    `empty()` on `services/rows/base.py:BaseRow` is the shared spelling of it.
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

        PRD 06's Alfred section states that as a constraint on the field, and it is a
        real one on M7's nine providers: "Because you watched Dune" is speakable and
        "cosine>0.82 seed=a3f9" is not.

        `None` for a shelf that needs no explaining, and **M8's `LLMRow` is the first
        thing in `src/` to reach that arm** -- it passes the stored `reason` through,
        `None` included, because `curation_validate` turns a blank one into `None`
        rather than `""`.
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
        """ADR-0006's hint, never a layout.

        A property of the shelf, which is why it is here and not on `RowCard`.
        """

    @property
    @abstractmethod
    def ttl(self) -> timedelta:
        """How long a *built* result may be cached.

        Carried onto `BuiltRow` when the row builds, so the cached artefact is self-
        describing -- ADR-0020's argument on a short-lived derivative.
        """

    @abstractmethod
    async def build(self, ctx: RowContext) -> BuiltRow:
        """Hydrate this shelf's cards.

        May legitimately return a row with no cards: a seed can vanish between `propose`
        and `build`, and the composer drops empties for exactly that reason (ADR-0023).
        """


@dataclass(frozen=True, slots=True)
class ScoredRow:
    """One proposal: a row, what it is worth, and whether it is pinned."""

    row: Row
    score: float
    pinned: bool = False


class RowProvider(ABC):
    """Proposes 0..n rows for one context.

    Does not decide what is shown.

    See [ADR-0023](../../../docs/prd/decisions/0023-a-provider-proposes-it-does-not-decide.md).
    """

    @property
    @abstractmethod
    def slug_prefix(self) -> str:
        """This provider's stable identifier: `"continue-watching"`, `"because-you- watched"`."""

    @abstractmethod
    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """Return 0..n candidate rows with relevance scores."""


__all__ = ["Row", "RowContext", "RowProvider", "ScoredRow"]
