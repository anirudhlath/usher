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
  ten provider modules, and `ports/` is precisely the layer where a service
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
use, so `asyncio.gather` over ten providers sharing a request's session is not
a performance choice but a corruption -- and it *usually works*, which is how it
ships. The defence is structural: a row holding repositories has no session to
share, so there is nothing for a `gather` to interleave. That the repositories
underneath share one is the composer's problem, stated once in `HomeService`,
rather than ten providers' problem stated nowhere.

`lint-imports` does **not** cover this. The `db is driven, not driving`
contract forbids `usher.ports -> usher.db`, and no contract in `pyproject.toml`
constrains `usher.ports -> sqlalchemy` at all, because every contract
enumerates `usher.*` modules only. A `from sqlalchemy.ext.asyncio import
AsyncSession` here passes all seven. `test_a_row_context_cannot_reach_a_session`
is the check that does not.

**ADR-0014's site enumeration lives in `usher.domain.rows`**, which lands
first and holds the other of this milestone's two new sites. `RowContext.taste`
was the eighth by that count and has been removed -- see `RowContext` below for
why, and note that the count in `domain/rows.py` moves with it.
"""

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

    **Thirteen fields, and two were removed rather than counted.** Group B built
    `PersonRepository`, `CreditRepository` and `CollectionRepository` and did
    not put them here; nothing in tasks 22-25 read them, so the gap was
    invisible until `FranchiseProvider` and `PeopleProvider` needed them. They
    are added by the task that first reads them, which is what this docstring's
    original promise -- "a field added later is a one-line change every
    existing provider ignores" -- was asserting is cheap. It was: the four
    providers already shipped are untouched by all three, and the nine already
    shipped are untouched by the twelfth.

    The full list:

        user, now, titles, media_items, watch_states, episodes, neighbors,
        people, credits, collections, affinities, curated, images

    **`search` and `taste` were here and are gone, and that is the other half
    of the same promise.** Group I/J found by grep that no provider read
    either, two groups after they were added. A field with no consumer is what
    this project deletes, and the argument is `RowCard.artwork`'s verbatim, one
    layer up: a field that is always absent -- or always `None` -- is a branch
    that never takes its other arm, and the day something fills it every reader
    written against the empty case is already wrong.

    - **`search: SearchIndex`** was here because PRD 06 puts one on the
      context. Nine providers later, none retrieves; every one of them reads a
      *repository* and asks a question with a predicate, which is what a row
      is. Its wiring existed solely to serve this field --
      `composition.search_index` was written to dodge contract six on its
      behalf -- and went with it.
    - **`taste: Centroid | None`** was worse than unread: on the request path
      it was structurally `None`, because `TasteService.centroid` returns
      `None` without an embedder and the route deliberately holds none. So
      every `GET /home` paid a `user_taste` read to fill a field that no
      provider looked at and that could not have carried a value there anyway.

    **The consequence, named rather than hidden: `TasteService.centroid` now
    has no caller in `src/`.** That is a genuinely larger finding than these
    two fields and it is deliberately *not* acted on here. Deleting it means
    deleting `user_taste`, `TasteRepository`, `StoredTaste` and a table in
    migration `ffa` -- a reversal of Group G's Task 22, with PRD 06's whole
    taste section to rewrite -- and doing that silently as a side effect of
    removing a context field would be the opposite of the discipline that found
    it. It belongs to the PRD/verification pass or to M8, whose
    `CuratedProvider` is the first plausible consumer of a taste vector.
    `genre_affinity` is unaffected and is read, via `affinities`.

    **`affinities` is the eleventh, and the plan did not foresee it.** Task
    27 says `GenreAffinityProvider` *"reads `TasteService.genre_affinity(
    user_id)`"* -- a **service** result, and a provider may import only
    `domain/` and `ports/`. Every other route was worse:

    - **A constructor argument on the provider.** Providers are enabled by
      registration in code (boundary call 9), so they are constructed once,
      and this is per-request, per-user data. That is the same argument that
      put the clock on this context rather than on one constructor per
      registered provider.
    - **Recomputing it inside the provider.** Two of `genre_affinity`'s three
      inputs are already here; the third is
      `TasteRepository.library_genre_counts()`. So the provider would need a
      `TasteRepository` field *and* a second copy of the lift arithmetic --
      and the front matter's rule for the seed list applies verbatim: it
      should exist exactly once.
    - **Widening it into a bundle** -- hiding a field rather than declaring
      one.

    So it is a value the composer computes and hands over, rather than a
    repository. **An empty sequence means no genre cleared Task 23's lift and
    support floors**, a real answer and the common one; it is never a stand-in
    for "nothing computed this", because there is no deployment in which
    nothing does -- the signal needs no embedder, which is the whole reason
    Task 23 declines PRD 06's centroid formulation, and the reason
    `GenreAffinityProvider` reads this rather than the centroid that used to
    sit beside it. A deployment without an embedder gets a home screen with
    **fewer rows, not worse rows**.

    **And it is *awaited* rather than held, which is the one shape change this
    dataclass has taken since M7.** It was `Sequence[GenreAffinity]`, computed
    while the dependency graph resolved -- and FastAPI resolves that graph
    before the handler runs, while `HomeService.compose_report` can only look
    in the ~30 s screen cache once it has a context. So every `GET /home`,
    hit or miss, paid `list_recent(50)` + `list_by_ids(50)` + a library-wide
    `unnest(genres) GROUP BY` over 1.27M titles to fill a field that exactly
    one of the ten providers reads, and a cache hit -- most requests -- paid
    the most expensive thing on the path before it could answer for free.
    The two available shapes were this and moving the cache lookup in front of
    the context; this one is chosen because the other puts a *stale-by-design*
    read in a dependency (the entry can expire or be invalidated between the
    lookup and the compose, and the screen composed from the empty affinity
    that follows is then cached for another 30 s -- a silently wrong screen
    bought to save a read), and because a lazy field costs nothing on the miss
    path it was already correct on.

    **A callable here is not a licence for more of them.** The other eleven
    are ports, a user and the clock -- and the clock is a callable for a
    different reason entirely (a fixture has to be able to say what time it
    is), not because reading it costs anything. This is the only field whose
    *value* is the product of three statements, which is the whole of why it
    is the only one deferred and the test of whether a twelfth should be. A
    second lazily-awaited field would want the same argument made again, in
    writing, and a context of callables is a context that has stopped saying
    what a row may reach and started saying what it may run.
    `test_the_route_does_not_read_a_households_taste_until_a_row_asks_for_it`
    pins the deferral and
    `test_a_screen_the_cache_can_answer_reads_no_taste_at_all` pins what it
    bought.

    **`curated` is the twelfth and it arrived with its reader**, which is the
    shape the two deleted fields did not have. M8's Task 15 adds it and
    `CuratedProvider` in one commit; the port
    (`CuratedRowRepository.list_for_user`) and the table had been there since
    Task 9, which is exactly the pull that put `search` here three groups
    before anything retrieved. It is a **repository**, not the rows: a
    provider is constructed once at import (boundary call 9) and a generation
    is per-household and per-request-fresh, so a `Sequence[CuratedRow]` here
    would be `affinities`' shape spent on data the composer has no reason to
    read and every request would pay for it whether or not the provider fired.

    **`images` is the thirteenth and it arrived with its reader too**, in M9's
    Task C6, in the same commit as `RowCard.artwork` and `BaseRow._artwork`.
    It is the first field on this bag whose reader is `BaseRow` rather than a
    named provider, and that is what makes it a *port*: the image a card shows
    is chosen against the **row's** `display_hint` (a poster for
    `portrait`/`square`, a backdrop for `landscape`/`wide`), so the question
    cannot be answered before the rows that would answer it exist. A
    `Mapping[uuid.UUID, uuid.UUID]` computed by the composer would be
    `affinities`' shape spent on a decision the composer cannot make.

    A fourteenth field arriving without a task is drift, and
    `test_every_row_context_field_is_read_by_at_least_one_provider` is what
    now says so -- scanned rather than counted, because the count was correct
    on the day two of the thirteen were decoration.
    """

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
    # The thirteenth, argued above. A `Sequence` rather than a `tuple` for the
    # reason `propose` returns one: a provider must not mutate what the
    # composer handed it, and the composer must not have to copy a list to
    # hand it over -- and a callable rather than the sequence itself, so a
    # screen the cache can answer never pays for it. The composition root owns
    # the memo, because "once per request" is a fact about the request and not
    # about any row.
    affinities: Callable[[], Awaitable[Sequence[GenreAffinity]]]
    # M8's one, and the fourteenth by that same historical count. It lands with
    # `CuratedProvider` (Task 15) rather than with the port and the table it
    # reads (Task 9), which is the discipline `search` and `taste` did not
    # have. `list_for_user` answers the newest generation, so the provider
    # never asks which night it is looking at.
    curated: CuratedRowRepository
    # M9's one, the thirteenth field, and its reader is `BaseRow.hydrate`
    # rather than any single provider -- which is a first for this bag and is
    # the reason it is a *port* here rather than a mapping the composer
    # computes. `affinities` is a value because exactly one provider reads it;
    # artwork is read by every row that renders a card, once per shelf, keyed
    # on that shelf's own `display_hint`. A composer-side read would have to
    # know every proposed row's hint before any of them built, which is the
    # two-phase coupling `RowContext` being frozen exists to refuse.
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
        the field, and it is a real one on M7's nine providers: "Because you
        watched Dune" is speakable and "cosine>0.82 seed=a3f9" is not.
        `None` for a shelf that needs no explaining, and **M8's `LLMRow` is
        the first thing in `src/` to reach that arm** -- it passes the stored
        `reason` through, `None` included, because `curation_validate` turns a
        blank one into `None` rather than `""`."""

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
    than a lookup the composer performs. **The named risk is one incomparable
    scale per registered provider, which makes the composer's sort meaningless
    while looking exactly like a sort.** Group I asserts the observed range
    across all registered providers; this port does not, because a bound
    invented here would be a number nobody measured.

    This paragraph is the **one** copy of that risk, and it carries no count on
    purpose. It shipped as "nine incomparable scales" and stayed nine through
    M8's tenth provider while `services/rows/__init__.py` said ten and
    `test_rows_invariants.py` said nine -- three live copies of one sentence
    disagreeing, in a commit whose message offered *"neither score invariant
    changed a character"* as evidence of stability. A restated number is a
    number that drifts; the count is `len(BASE_SCORES)` and is derived where it
    is asserted.
    """

    row: Row
    score: float
    pinned: bool = False


class RowProvider(ABC):
    """Proposes 0..n rows for one context. Does not decide what is shown.

    See [ADR-0023](../../../docs/prd/decisions/0023-a-provider-proposes-it-does-not-decide.md).
    """

    @property
    @abstractmethod
    def slug_prefix(self) -> str:
        """This provider's stable identifier: `"continue-watching"`,
        `"because-you-watched"`.

        **Declared rather than derived, because two things outside the
        codebase read it.** It is the `provider` label on
        `usher.row.build.duration` and the leftmost column of `usher home`'s
        report, so it is a name a dashboard and an operator hold -- and
        deriving it from `type(self).__name__` would make a class rename a
        silent dashboard break with no schema anywhere to notice in. PRD 07's
        rule for the wire ("internal refactors don't break clients; wire
        changes are deliberate") applied to telemetry.

        **Bounded at ten, and that is the point.** The tempting label is the
        *row's* slug -- but `BecauseYouWatchedProvider` mints one per seed
        (`because-you-watched-<seed>`) and `CuratedProvider` one per shelf per
        generation (`curated-01`, `curated-02`, …), so a slug label's
        cardinality is the household's watch history, and in time the catalog
        and every shelf a model has ever proposed. A label whose cardinality
        grows with either is a metrics-backend outage rather than a dashboard.

        Every row a provider proposes carries a slug that **starts with this
        string**, which is what makes the label provably about the rows it
        measures rather than merely alongside them;
        `tests/unit/test_rows_invariants.py` asserts it over the registry.
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
