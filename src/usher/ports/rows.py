"""The row abstractions: what a provider proposes and what a row builds.

PRD 06 sketches `Row` and `RowProvider` as a class body with bare annotations
and settles four of their details from the inside of one imagined
implementation outward. This module settles them, which is the move M6 made on
`SearchIndex`'s provisional marker.

**The ABC is here against PRD 06's own sentence, and that sentence supplies the
reason.** It says *"The `Row` ABC lives in the services layer because it has
behaviour and dependencies"* -- which is an argument for the **base class**, not
for the **abstraction**, and separating the two is what makes both correct:

- `Row(ABC)` here, the shape `HomeService` names when it sorts, caps and
  builds. A service must be able to name what it composes without importing
  nine provider modules, and `ports/` is precisely the layer where a service
  names a shape it does not own.
- `BaseRow(Row)` in `services/rows/base.py`, carrying the shared `hydrate()`
  and `empty()`. `hydrate(title_ids)` needs a `TitleRepository`, a
  `MediaItemRepository` and a `WatchStateRepository` to turn ids into cards --
  and a concrete method on a port is a port with a dependency. Every method in
  `ports/search.py`, `ports/source.py` and `ports/repository.py` is abstract
  today; `Row` is not the place to spend that precedent, because shared
  *implementation* is what a concrete subclass is for.

The second reason is mechanical. `test_every_port_abc_is_registered_in_all_
ports` walks `usher.ports.*` with `pkgutil` and is the only thing in this
repository that checks an ABC is an ABC. A `Row` in `services/` is invisible to
it, so PRD 06's placement would put the milestone's two central abstractions
outside both of ADR-0001's checks.

**`RowContext` carries ports and never an `AsyncSession`, and that is checked
rather than commented.** `AsyncSession` is explicitly not safe for concurrent
use, so `asyncio.gather` over nine providers sharing a request's session is not
a performance choice but a corruption -- and it *usually works*, which is how it
ships. The defence is structural: a row holding repositories has no session to
share, so there is nothing for a `gather` to interleave. That the repositories
underneath share one is the composer's problem, stated once in `HomeService`,
rather than nine providers' problem stated nowhere.

`lint-imports` does **not** cover this. The `db is driven, not driving`
contract forbids `usher.ports -> usher.db`, and no contract in `pyproject.toml`
constrains `usher.ports -> sqlalchemy` at all, because every contract
enumerates `usher.*` modules only. A `from sqlalchemy.ext.asyncio import
AsyncSession` here passes all seven. `test_a_row_context_cannot_reach_a_session`
is the check that does not.

**ADR-0014's site enumeration lives in `usher.domain.rows`**, which lands
first and holds the other of this milestone's two new sites. `RowContext.taste`
is the eighth by that count.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from pydantic import AwareDatetime

from usher.domain.rows import BuiltRow, DisplayHint, RowFamily
from usher.domain.taste import Centroid
from usher.domain.watch import User
from usher.ports.repository import (
    EpisodeRepository,
    MediaItemRepository,
    TitleNeighborRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.search import SearchIndex


@dataclass(frozen=True, slots=True)
class RowContext:
    """Everything a row may reach, for one request, for one user.

    A frozen slotted dataclass rather than a `DomainModel`, and the reason is
    what it holds: its fields are ABCs, and pydantic would need
    `arbitrary_types_allowed=True` to accept them -- which switches validation
    off for exactly the fields you would want it on while still charging for
    the model. More to the point, a context is not a *value*; it is a
    request-scoped wiring bundle, which puts it on `SearchRequest`'s side of
    the line `domain/search.py` draws rather than `SearchResult`'s.

    **Frozen has a second effect worth naming.** A provider cannot stash state
    on the context between `propose` and `build`. Two-phase composition with a
    mutable context invites computing in phase one and reading in phase two,
    which makes every `build` silently depend on its own `propose` having run
    -- and the composer is explicitly allowed to build a row it did not just
    propose, out of cache.

    **The clock is injected, and the repository is not consistent about this**
    (`services/enrich.py` takes one; `services/reconcile.py` calls
    `datetime.now(UTC)` inline), so it is argued rather than assumed. It is
    load-bearing for two providers by name: `SeasonalProvider` fires on a
    calendar window, and a provider whose firing condition is "is it October"
    and which reads the wall clock is testable only in October;
    `RediscoverProvider` fires on "watched > 2 years ago", where the only
    alternative is a fixture dated two years back that stops meaning what it
    meant as the calendar moves. It lives on the *context* rather than on each
    provider's constructor because providers are enabled by registration in
    code (boundary call 9) -- they are constructed once, and a per-request
    clock cannot be a constructor argument of a singleton.

    **Nine fields now, twelve by the end of the milestone.** Group B adds
    `people`, `credits` and `collections` when those repositories exist.
    Stating that here is deliberate: a field added later is a one-line change
    every existing provider ignores, and that property is the *reason* this is
    a bag rather than nine per-provider constructor signatures. A thirteenth
    arriving without a task is a finding rather than drift.

    `taste` is `Centroid | None` -- ADR-0014's eighth site. A deployment with
    no embedder has no centroid at all (ADR-0022), and every reader drops the
    signal rather than zeroing it: `GenreAffinityProvider` with no centroid
    proposes *nothing* rather than falling back to the household's
    most-watched genre. A deployment without an embedder gets a home screen
    with **fewer rows, not worse rows**.
    """

    user: User
    now: Callable[[], AwareDatetime]
    titles: TitleRepository
    media_items: MediaItemRepository
    watch_states: WatchStateRepository
    episodes: EpisodeRepository
    neighbors: TitleNeighborRepository
    search: SearchIndex
    taste: Centroid | None


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
        """Stable identifier for this shelf: `"continue-watching"`,
        `"because-you-watched-<seed>"`. Unique within one composed screen,
        and **not** something the composer branches on -- a per-seed slug is
        a value that varies with the catalog."""

    @property
    @abstractmethod
    def title(self) -> str:
        """What the shelf is called on screen."""

    @property
    @abstractmethod
    def reason(self) -> str | None:
        """The subtitle, written to be **spoken aloud** rather than merely
        displayed -- PRD 06's Alfred section states that as a constraint on
        the field, and it is a real one on the nine providers: "Because you
        watched Dune" is speakable and "cosine>0.82 seed=a3f9" is not.
        `None` for a shelf that needs no explaining."""

    @property
    @abstractmethod
    def family(self) -> RowFamily:
        """The diversity key. The composer's constraints -- "no three
        consecutive similarity rows; cap per family" -- are stated in
        families, so a row that could not name its own would make both
        inexpressible."""

    @property
    @abstractmethod
    def display_hint(self) -> DisplayHint:
        """ADR-0006's hint, never a layout. A property of the shelf, which is
        why it is here and not on `RowCard`."""

    @property
    @abstractmethod
    def ttl(self) -> timedelta:
        """How long a *built* result may be cached. Carried onto `BuiltRow`
        when the row builds, so the cached artefact is self-describing --
        ADR-0020's argument on a short-lived derivative."""

    @abstractmethod
    async def build(self, ctx: RowContext) -> BuiltRow:
        """Hydrate this shelf's cards. May legitimately return a row with no
        cards: a seed can vanish between `propose` and `build`, and the
        composer drops empties for exactly that reason (ADR-0023)."""


@dataclass(frozen=True, slots=True)
class ScoredRow:
    """One proposal: a row, what it is worth, and whether it is pinned.

    It carries the `Row` itself rather than a `row_slug: str`. The slug form
    needs a `dict[str, Row]` on the composer -- a lookup table, a second
    source of truth, and a `KeyError` waiting for the first provider that
    proposes two rows under one slug, which `FranchiseProvider` at one row per
    franchise is well placed to be. Carrying the object is also why this
    dataclass is in `ports/` and not in `domain/rows.py` where the plan's file
    structure put it: `usher.domain` may not import `usher.ports`, and
    `lint-imports` reports `6 kept, 1 broken` on the attempt.

    **`pinned` is the spelling of PRD 06's "1 row, always ranked first".**
    Three were open -- a `RowFamily` member, a flag here, or a composer-side
    constant -- and this is the one Group A picked, so Group I honours it.

    - Not a family member: a family is the key the "cap per family" rule
      *counts*, and Continue Watching is a `SOURCE` row like Recently Added.
      A one-member family invented to express a pin puts a positional
      guarantee inside a rule about crowding.
    - Not a slug comparison, ever: `because-you-watched-<seed>` is minted per
      seed, so a slug-keyed rule couples the composer to the catalog through
      a string literal no type checker reads.
    - Not a score. "Always ranked first" is a **positional** guarantee, and
      scores are minted per provider from unrelated signals with nothing
      normalising them -- so a guarantee expressed as "a score high enough to
      win" is one another provider's arithmetic can silently take away, on a
      screen that still looks fine. ADR-0023 records it.

    **`score` is a module constant per provider, not configuration**, and the
    argument is not PRD 08's. PRD 08 retracted blend weights as settings
    because a weight change leaves an artefact half computed under each
    meaning -- and a row score has no artefact, being computed per request and
    cached for ~30 s. Two other reasons hold instead: a configurable score set
    can *reorder* Continue Watching, i.e. a TOML file that can break a
    specification; and a score only decides ordering among proposals, after
    which diversity and the top-N cap reshape the result, so an operator
    turning the dial would watch a screen change for reasons the dial does not
    explain. PRD 08's config table listed "row weights" after its own prose
    had retracted them; M7 is the milestone that makes that concretely wrong,
    and it is corrected with this file.

    **What is deliberately not constrained: the scale.** Whether a provider
    modulates its base score per proposal (a fresher seed ranking higher) is
    left open, and this port permits it because `score` is a float here rather
    than a lookup the composer performs. The named risk is nine incomparable
    scales, which makes the composer's sort meaningless while looking exactly
    like a sort. Group I asserts the observed range across all registered
    providers; this port does not, because a bound invented here would be a
    number nobody measured.
    """

    row: Row
    score: float
    pinned: bool = False


class RowProvider(ABC):
    """Proposes 0..n rows for one context. Does not decide what is shown.

    See [ADR-0023](../../../docs/prd/decisions/0023-a-provider-proposes-it-does-not-decide.md).
    """

    @abstractmethod
    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """Return 0..n candidate rows with relevance scores.

        **An empty sequence is a first-class answer, not a degraded one.**
        PRD 06: *"A provider returns nothing when it has nothing to say."* The
        failure this shape exists to refuse is not a provider that crashes --
        it is one that, finding no signal, falls back to "popular titles",
        producing a home screen that looks personalised on a household that
        has watched nothing. An empty row and an absent row are different
        states; a *generic* row is neither, and it is the one that survives
        review.

        A port cannot prevent that. What it can do is refuse to **ask** for
        it: there is no `limit`, no `min_results` and no `fallback` parameter
        here, because a signature carrying `min_results` has already decided
        the fallback exists, and every implementer reads that as the
        requirement it looks like.

        `Sequence`, not PRD 06's `list`, for the reason `SearchIndex.
        index_many` takes one: a caller must not mutate a provider's return,
        and a provider that returns its own cached list finds that out the
        hard way.
        """


__all__ = ["Row", "RowContext", "RowProvider", "ScoredRow"]
