"""`LLMRow` -- the one shelf whose *order* is the product, and the one whose
cards can go missing between the write and the render.

**The wrong implementations this file's cases rule out:**

1. **Hydrates in the repository's order.** `TitleRepository.list_by_ids` is a
   single `IN (...)` and promises no order at all -- physical order against
   Postgres, insertion order against the fake -- so a `build` that returned
   what the read returned produces a correctly-populated shelf in the wrong
   sequence, on every generation, forever. For the other nine rows that is a
   defect; here it is the *whole* defect, because a curated row **is** an
   ordering and it is the only judgement the completion was bought for
   (`CuratedRow`'s own docstring says a case must pin it). Every ordering case
   below therefore reads the store's answer back through the port and asserts
   it disagrees with the row's, rather than asserting membership -- `assert
   title_id in {...}` is satisfied by returning the catalog in physical order.
2. **Sorts.** `sorted(...)` by id, by name or by anything else. The fixture's
   curated order is neither the store's order nor its reverse, which is what
   makes a re-sort visible; the ids are UUIDv7, so store order, insertion order
   and id order are one order here and one guard covers all three.
3. **Raises on a title that vanished.** `curated_rows.card_title_ids` is a
   `uuid[]` with no foreign key -- Postgres has no `FOREIGN KEY EACH ELEMENT`
   -- so deleting a title leaves a dangling id in every curated row that
   mentioned it, for up to one generation. A `KeyError` there is a 500 on a
   home screen because one film was merged away overnight.
4. **Truncates at the first missing title** rather than skipping it. The
   fixture puts the vanished id in the *middle* for exactly that reason: a
   shelf that stops at the gap is populated, plausible, and silently short.
5. **Mints its own slug.** A constant `"curated"` collides in `RowCache`, whose
   key is `(user_id, slug)`, so five shelves become one -- and the composer
   breaks score ties on the slug, which is where the stored positional slug
   makes the model's own ordering the tiebreak.
6. **Invents a runtime** from `titles.runtime_minutes`. This row reads no watch
   state, so the honest card is nought seconds into an unknown total
   (ADR-0014); a runtime it did not read is a runtime it does not know.

**And the wrong `CuratedProvider`s the second half of this file rules out:**

7. **Reads the table rather than the household.** `list_for_user(ctx.user.id)`
   is the whole of the scope, and the one predicate `PostgresCuratedRowRepository`
   actually got wrong (`testing-discipline.md`, 2026-08-06). A provider passing
   anything but `ctx.user.id` puts one household's shelves, headings and
   reasons on another's screen, and every fixture that mints a fresh
   `generation_id` per household hides it.
8. **Proposes all of them.** `curation_validate` deliberately caps nothing --
   *"every card in a hundredth row is still a title the household could
   watch"* -- so the `0-5 rows` bound is PRD 06's, is a product bound, and
   lives here. Without it a nine-row generation paints nine shelves.
9. **Takes the wrong five.** `stored[-5:]`, or the five that sort first by
   heading: the model's ordering is the only judgement the completion was
   bought for, so the five that survive the cap are its first five.
10. **Scores them apart.** A per-row decrement is a *second* spelling of the
    model's ordering, and the composer already has the first: it breaks score
    ties on `slug`, and a curated slug is positional and zero-padded to the
    generation's width. Two spellings of one order eventually disagree, and
    `domain/curation.py`'s `SLUG_PREFIX` comment already assumes there is
    only one ("every curated row carries the same base score").
    `BecauseYouWatchedProvider` needs `_SEED_STEP` for the *opposite* reason --
    its slugs carry a seed id, so its tie alphabetises.
11. **Generates.** A provider holding an `LLMClient` would put a paid network
    round trip inside `GET /home`. Asserted structurally on the module's
    imports and its own source, the way
    `test_the_home_service_and_every_provider_hold_no_source_adapter` is --
    `test_no_provider_reaches_a_port_the_context_does_not_carry` cannot see it,
    because `usher.ports.llm` is under `usher.ports` and passes that scan.
"""

import ast
import inspect
import pathlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.unit.rows import NOW, USER, Library
from usher.domain.curation import SLUG_PREFIX, CuratedRow
from usher.domain.ids import new_id
from usher.domain.rows import DisplayHint, RowFamily
from usher.domain.title import Title

# The composer's own ordering, imported rather than re-spelled -- see
# `test_one_score_for_the_whole_generation...`. Private to `services/home.py`
# because nothing in `src/` may sort candidates but the composer; a test that
# asserts the composer's order has to name the composer's order.
from usher.services.home import HomeService, _Candidate, _ranking
from usher.services.rows.cache import RowCache
from usher.services.rows.curated import CURATED_SCORE, MAX_CURATED_ROWS, CuratedProvider, LLMRow

# The instant a generation ran. A fixed literal rather than `datetime.now`:
# nothing in this file reads it, and a stored row whose timestamp moves per run
# is a fixture that cannot be compared against itself.
_GENERATED_AT = datetime(2026, 8, 4, 3, 0, tzinfo=UTC)
_MODEL = "an-invented-model"
_TITLE = "Slow-burn sci-fi for a rainy night"
_REASON = "Because you keep finishing the quiet ones."


def _stored(
    card_title_ids: Sequence[uuid.UUID],
    *,
    slug: str = "curated-1",
    title: str = _TITLE,
    reason: str | None = _REASON,
    position: int = 0,
) -> CuratedRow:
    """One shelf as `curation_validate` minted it and `curated_rows` holds it.

    The defaults are the shape production writes -- a positional, zero-padded
    slug and a reason written to be spoken aloud -- so a case that overrides
    one is visibly overriding it.
    """
    return CuratedRow(
        id=new_id(),
        user_id=USER.id,
        slug=slug,
        title=title,
        reason=reason,
        card_title_ids=tuple(card_title_ids),
        position=position,
        model_name=_MODEL,
        generation_id=new_id(),
        generated_at=_GENERATED_AT,
    )


async def _four(library: Library) -> list[uuid.UUID]:
    """Four titles whose insertion order, id order and alphabetical order all
    agree -- so a fixture that disagrees with one disagrees with all three."""
    return [
        await library.title("Aardvark"),
        await library.title("Blue Velvet"),
        await library.title("Crimson Tide"),
        await library.title("Dune"),
    ]


async def test_the_shelf_is_hydrated_in_the_order_the_model_chose() -> None:
    """**The load-bearing property, and the reason `CuratedRow` says a case
    must pin it.**

    A curated row *is* an ordering: it is the only judgement the completion was
    bought for, so nothing downstream may re-sort it. The hydration path is
    shared with nine providers that legitimately *do* sort, which is what makes
    this worth its own case rather than an inherited assurance.

    Kills a `build` that answers in the repository's order and kills one that
    sorts -- by id, by name, or by reversing. The premises are read back
    through the port rather than computed from the ids the case minted, because
    a guard computed from a literal is one no fixture change can falsify.
    """
    library = Library()
    aardvark, blue, crimson, dune = await _four(library)
    wanted = [dune, blue, aardvark, crimson]

    answered = [title.id for title in await library.titles.list_by_ids(list(wanted))]
    assert answered != wanted, "the premise: the store answers in its own order, not the row's"
    assert wanted != list(reversed(answered)), "the premise: nor in the reverse of the store's"

    built = await LLMRow(_stored(wanted)).build(library.context())

    assert [card.title_id for card in built.cards] == wanted


async def test_a_title_that_vanished_since_the_generation_loses_its_card_not_the_shelf() -> None:
    """`card_title_ids` is a `uuid[]` and Postgres has no foreign key over
    array elements, so a merged-away title leaves a dangling id here for up to
    one generation -- `db/models/curation.py` names this case as the place that
    handles it.

    Dropped, never raised: a `KeyError` here is a 500 on a home screen because
    one film went away between two statements of one request. The heading and
    the reason stay, and the survivors keep the model's order.

    The vanished id sits in the **middle**, which is what kills a hydration
    that stops at the first miss -- that shelf is populated, plausible and
    silently short.
    """
    library = Library()
    aardvark, blue, crimson, dune = await _four(library)
    merged_away = new_id()
    wanted = [dune, merged_away, blue, aardvark, crimson]

    built = await LLMRow(_stored(wanted)).build(library.context())

    assert [card.title_id for card in built.cards] == [dune, blue, aardvark, crimson]
    assert built.title == _TITLE
    assert built.reason == _REASON


async def test_a_shelf_whose_every_title_vanished_builds_empty_rather_than_raising() -> None:
    """**Empty is a legal value and a different state from absent** (ADR-0023),
    which is the whole reason `BuiltRow` is constructible with no cards.

    A curated row cannot be *stored* empty -- `CuratedRow.card_title_ids`
    carries `min_length=1`, because a stored row with no cards is a validator
    that kept nothing and would paint a heading with no shelf under it. It can
    still *build* empty, on the day the catalog loses every title it named, and
    the composer is what drops it: "this row built and had nothing to show" and
    "this row was never proposed" are a quiet catalog and a dead provider, and
    `HomeService` has to tell them apart.

    `== row.empty()` rather than only `cards == ()`, and the weaker assertion is
    *not* kept alongside it: `Row.empty()` constructs `cards=()`, so
    `built.cards == ()` is implied by the line below it and cannot fail on its
    own. The stronger form also pins that the losing shelf is the *same value*
    as the empty one -- a `build` that quietly changed the heading or the TTL on
    its way to nothing would pass the weaker assertion.
    """
    library = Library()
    await _four(library)
    row = LLMRow(_stored([new_id(), new_id(), new_id()]))

    built = await row.build(library.context())

    assert built == row.empty()


async def test_the_row_takes_its_slug_heading_and_reason_from_the_stored_record() -> None:
    """The stored row is the artefact; this class is a renderer for it.

    Kills a slug minted here. A constant -- `"curated"` -- makes five shelves
    one `RowCache` entry, since that key is `(user_id, slug)`, and the household
    sees whichever built first repeated five times. The stored slug is
    positional and zero-padded to the width of *one generation*, which is what
    makes the composer's `slug` tiebreak the model's own ordering rather than an
    alphabetisation of its prose.

    Also kills a heading composed here from the model name or the position.
    """
    library = Library()
    aardvark, _blue, _crimson, _dune = await _four(library)
    stored = _stored([aardvark], slug="curated-07", title="A Heading Only A Model Would Write")

    built = await LLMRow(stored).build(library.context())

    assert built.slug == "curated-07"
    assert built.title == "A Heading Only A Model Would Write"
    assert built.reason == _REASON


async def test_a_row_the_model_gave_no_reason_for_has_no_subtitle_not_an_empty_one() -> None:
    """**The first row in this project that can reach `reason=None`.**

    All nine M7 providers return a sentence, so `BuiltRow.reason`'s null arm is
    a shape the wire promises and nothing reached -- `test_api_home.py` records
    that and names `CuratedProvider` as the first plausible one.
    `curation_validate` turns a blank reason into `None` rather than `""`,
    because an empty string is a subtitle a client renders as a blank line and
    cannot tell from a row that had something to say and said nothing.

    Kills `reason = self._row.reason or ""` here, which would put that blank
    line back one layer below the DTO that refuses it.
    """
    library = Library()
    aardvark, _blue, _crimson, _dune = await _four(library)

    built = await LLMRow(_stored([aardvark], reason=None)).build(library.context())

    assert built.reason is None


async def test_the_row_names_the_curated_family_and_carries_its_own_ttl() -> None:
    """**`RowFamily.CURATED` exists because this row emits it**, which is the
    argument `domain/rows.py` made for not pre-declaring it: a cap on a family
    with no members is a branch nothing can reach.

    The TTL is five minutes and it is not "until regenerated", which is PRD
    06's phrase for the *artefact*'s lifetime and is the wrong reading for a
    cache. The stored row is immutable until a generation replaces it -- and a
    replacement is the only event that matters, because `RowCache` holds the
    whole built row under `(user_id, slug)` and a generation of the same width
    re-uses the same slugs. Nothing invalidates that entry: the curation job
    runs in `usher work`, a different process from the API that holds the cache,
    and cross-process invalidation is M9's. So this number is not "how long the
    row stays fresh", it is **how long a household keeps seeing last night's
    shelf after tonight's replaced it** -- which `POST /admin/rows/regenerate`
    turns into an operator staring at a screen. Five minutes matches
    `RecentlyAddedProvider`'s, the other row whose content moves on an event
    this process never observes.
    """
    library = Library()
    aardvark, _blue, _crimson, _dune = await _four(library)

    built = await LLMRow(_stored([aardvark])).build(library.context())

    assert built.family is RowFamily.CURATED
    assert built.display_hint is DisplayHint.PORTRAIT
    assert built.ttl == timedelta(minutes=5)


async def test_a_copy_retracted_since_the_generation_is_marked_unowned_not_dropped() -> None:
    """The pool is drawn from titles the household **owns**, so an unowned
    curated card means a copy went away between the generation and the render.

    Kills a hydration that filters to owned. PRD 05 requires an unowned result
    to be *"clearly marked"*, not hidden, and dropping it here would shorten the
    shelf for a state the badge already describes -- while `RowCard.owned` is
    exactly the field a client renders it from. Ordered, so the badge is
    asserted per card rather than as a set.
    """
    library = Library()
    owned = await library.title("A Film Still Owned")
    retracted = await library.title("A Film Whose Copy Went Away", owned=False)

    built = await LLMRow(_stored([retracted, owned])).build(library.context())

    assert [(card.title_id, card.owned) for card in built.cards] == [
        (retracted, False),
        (owned, True),
    ]


async def test_the_row_claims_nothing_about_where_the_household_is_in_a_title() -> None:
    """**ADR-0014 at the card.** This row reads no watch state -- the pool is
    unwatched candidates -- so every card carries the honest zero-and-`None`:
    nought seconds into a total this provider did not read.

    Kills filling `runtime_seconds` from `titles.runtime_minutes`, which is the
    tempting spelling and is a different fact: `WatchState.runtime_seconds` is
    what a *source* reported for the file the household holds, and a card
    carrying the catalog's figure instead invites every client to compute a
    progress fraction against a number that never came from their copy. The
    fixture seeds `runtime_minutes=120`, so that substitution is available.

    Also kills a chapter: a curated card is about a title, never an episode, so
    both chapter fields stay `None` and a client's branch is "is this an episode
    card" rather than "is this label meaningful".
    """
    library = Library()
    aardvark, blue, _crimson, _dune = await _four(library)

    built = await LLMRow(_stored([blue, aardvark])).build(library.context())

    assert len(built.cards) == 2, "the fixture stopped producing cards, so this proves nothing"
    for card in built.cards:
        assert card.position_seconds == 0
        assert card.runtime_seconds is None
        assert card.played is False
        assert card.episode_id is None
        assert card.episode_label is None


# -- the provider ---------------------------------------------------------

# Headings whose alphabetical order is neither the model's order nor its
# reverse, indexed by position. A generation whose headings happened to sort
# into the model's order would ratify a provider that sorted by heading, which
# is wrong implementation 9 and the one a fixture is most likely to hide --
# `curation_validate` puts no constraint on prose at all, so nothing upstream
# makes these agree or disagree.
_HEADINGS = ("Dust", "Bells", "Glass", "Anvils", "Fog", "Cranes", "Embers")

# The order the seeder writes positions in, which is deliberately not
# ascending. `list_for_user` orders by `position`, so a fixture seeded in
# position order makes insertion order, id order (UUIDv7) and the model's
# order one order -- and "the model's first five" would then be satisfied by
# a provider that took the first five of anything.
_SEEDING_ORDER = (3, 0, 5, 1, 6, 2, 4)


async def _generation(
    library: Library,
    count: int,
    *,
    width: int = 1,
    user_id: uuid.UUID | None = None,
) -> list[CuratedRow]:
    """`count` shelves of one card each, as one generation.

    Written in `_SEEDING_ORDER` rather than in position order, so the port's
    `ORDER BY position` is doing real work and every "the model's first five"
    assertion below is a claim about `position` rather than about insertion.
    """
    order = [one for one in _SEEDING_ORDER if one < count]
    assert len(order) == count, (
        f"_SEEDING_ORDER covers {len(_SEEDING_ORDER)} positions, not {count}"
    )
    seeded = []
    for position in order:
        card = await library.title(f"A Card For {_HEADINGS[position]}")
        seeded.append(
            await library.curated(
                [card],
                position=position,
                title=_HEADINGS[position],
                width=width,
                user_id=user_id,
            )
        )
    return seeded


async def test_the_provider_proposes_one_shelf_per_stored_record_over_the_stored_record() -> None:
    """**A provider that is not registered is dead code, and a provider that
    re-derives what it was handed is dead storage.**

    Three shelves in, three proposals out, each an `LLMRow` over its own record
    -- asserted by *building* one rather than by an `isinstance`, so a provider
    that constructed a plausible row of its own from the model name and the
    position fails here rather than passing a type check.

    Nothing is pinned: `ScoredRow.pinned` is PRD 06's *"1 row, always ranked
    first"* and belongs to `ContinueWatchingProvider` alone. A second pinned
    provider would give the composer two rows claiming one position, which is a
    guarantee that quietly becomes a tie.
    """
    library = Library()
    stored = await _generation(library, 3)
    by_position = sorted(stored, key=lambda one: one.position)

    proposed = await CuratedProvider().propose(library.context())

    assert [one.row.slug for one in proposed] == [one.slug for one in by_position]
    assert all(one.pinned is False for one in proposed)
    built = await proposed[0].row.build(library.context())
    assert built.title == by_position[0].title
    assert built.reason == by_position[0].reason
    assert [card.title_id for card in built.cards] == list(by_position[0].card_title_ids)


async def test_a_generation_longer_than_the_budget_is_cut_to_the_models_first_five() -> None:
    """**The row cap is this provider's, and it is a product bound.**

    `curation_validate` deliberately caps nothing -- *"every card in a
    hundredth row is still a title the household could watch"*, so a cap is not
    a safety property of the validator -- and PRD 06 puts the budget here, as
    `CuratedProvider`'s `0-5 rows`. Without it a model that ignored the
    prompt's *"between 3 and 5 rows"* paints nine shelves, and the composer's
    per-family cap of four then decides which of them a household sees by
    `slug`, which is a product decision made by a diversity rule.

    **The five that survive are the model's first five**, because the ordering
    is the only judgement the completion was bought for. The premises are read
    back through the port rather than computed from the literals this file
    passed in: they say that the model's first five are not the last five and
    not the five that sort first by heading, so a `stored[-5:]` and a
    `sorted(stored, key=title)[:5]` both fail on their own assertion rather
    than on a fixture that could not tell them apart.
    """
    library = Library()
    await _generation(library, MAX_CURATED_ROWS + 2)
    stored = await library.curated_rows.list_for_user(USER.id)

    assert [one.position for one in stored] == list(range(MAX_CURATED_ROWS + 2)), (
        "the premise: the port answers in the model's order"
    )
    kept = [one.slug for one in stored[:MAX_CURATED_ROWS]]
    assert [one.slug for one in stored[-MAX_CURATED_ROWS:]] != kept, (
        "the premise: the model's last five are not its first five"
    )
    by_heading = sorted(stored, key=lambda one: one.title)
    assert [one.slug for one in by_heading[:MAX_CURATED_ROWS]] != kept, (
        "the premise: the five that sort first by heading are not the model's first five"
    )

    proposed = await CuratedProvider().propose(library.context())

    assert [one.row.slug for one in proposed] == kept
    assert MAX_CURATED_ROWS == 5, "PRD 06's `CuratedProvider | 0-5 rows`"


def test_the_shelf_budget_is_never_smaller_than_what_the_prompt_asks_for() -> None:
    """**The one direction in which two numbers in two files fail silently.**

    `curation_prompt.MAX_ROWS` is a *request* -- "return between 3 and 5 rows"
    -- and `MAX_CURATED_ROWS` is a *bound* on what a household is shown. They
    are allowed to be different numbers, and PRD 06 states both as five. What
    is not allowed is the prompt asking for more than the screen will take:
    every night the model obeys, the excess is bought, validated, stored, and
    then discarded here with nothing counting it -- spend with no screen to
    show for it, which is exactly what PRD 10's dashboard 5 exists to make
    visible and exactly what this assertion is cheaper than.

    Deliberately not an equality. A prompt asking for fewer than the budget
    allows is a product choice with no loss in it, and pinning the two numbers
    equal would be a change-detector on a request nobody may tune.
    """
    from usher.services.curation_prompt import MAX_ROWS

    assert MAX_CURATED_ROWS >= MAX_ROWS, (
        f"the prompt asks for up to {MAX_ROWS} shelves and only {MAX_CURATED_ROWS} are shown"
    )


@pytest.mark.parametrize(
    ("generated", "discarded"), [(3, 0), (MAX_CURATED_ROWS, 0), (MAX_CURATED_ROWS + 2, 2)]
)
async def test_the_shelves_the_budget_discards_are_counted(generated: int, discarded: int) -> None:
    """**The one drop on this screen `ProviderReport` structurally cannot see.**

    That report splits `proposed`/`selected`/`built` precisely because PRD 06's
    "drops any that build empty" is otherwise invisible, so the composer's
    family-cap drop of curated row #5 is legible as `proposed 5, selected 4`.
    `ProviderReport.proposed` is `len(provider.propose(ctx))` -- the **post**-cut
    number -- so a seven-shelf generation prints `curated 5` and the two
    bought, validated and stored shelves this provider threw away are recorded
    nowhere at all. Same argument as the one that split that dataclass into
    three fields, applied to the third drop.

    `test_the_shelf_budget_is_never_smaller_than_what_the_prompt_asks_for`
    guards the *prompt* direction, which cannot happen without somebody editing
    a constant. This is the direction that happens at runtime with no code
    change: `curation_validate` deliberately caps nothing, so a model ignoring
    `MIN_ROWS`/`MAX_ROWS` is stored at whatever length it returned.

    **Three arms, and the two zeros are not padding.** An implementation that
    records the count only when it is non-zero passes the third arm alone, and
    a value absent from an export is indistinguishable from a value nobody
    records -- which is the argument `usher.curation.dropped` already rests on
    one layer down. `MAX_CURATED_ROWS` exactly is the boundary, so the middle
    arm is where an off-by-one in the slice would show.

    A local `TracerProvider` rather than the global one, so this case neither
    reads nor leaves state that `tests/unit/test_services_home_sequential.py`
    and `tests/unit/test_telemetry.py` also touch.
    """
    library = Library()
    await _generation(library, generated)
    assert len(await library.curated_rows.list_for_user(USER.id)) == generated, (
        "the premise: the generation really is this long"
    )
    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))

    with tracing.get_tracer(__name__).start_as_current_span("home.compose"):
        proposed = await CuratedProvider().propose(library.context())

    assert len(proposed) == min(generated, MAX_CURATED_ROWS)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, "the harness recorded no span, so this proves nothing"
    assert spans[0].attributes is not None
    assert spans[0].attributes["usher.home.curated.discarded"] == discarded


def test_this_provider_takes_no_constructor_argument() -> None:
    """**The class docstring says so three lines above `__init__`, and for one
    commit `__init__` said otherwise.**

    It shipped as `def __init__(self, *, limit: int = MAX_CURATED_ROWS)` under a
    docstring reading *"It has no constructor argument"* -- the restated-fact
    failure `row_providers`' own docstring spends a paragraph retiring, in a new
    module, at three lines' distance. Nothing ever passed it, so nothing
    observed the contradiction.

    Two facts, and they are different in kind. **No deployment fact** is the one
    the docstring argues: a household with `USHER_LLM_ENABLED=false` and a
    household whose first generation has not run get the same empty answer
    through the same code path, and an `llm_enabled` flag here would make two
    states two branches with one observable outcome. **No tuning dial** is the
    second: eight siblings take a `limit`, and theirs are card counts and
    candidate pool sizes while this one is PRD 06's `0-5 rows` product bound --
    a per-instance override of which is a sixth curated shelf nobody decided to
    paint.

    Asserted on the signature because both defects are additions to it, and
    because a behavioural assertion cannot see an argument nothing passes.
    """
    assert list(inspect.signature(CuratedProvider).parameters) == []


async def test_every_curated_shelf_is_scored_alike_so_the_slug_tiebreak_is_the_models_order() -> (
    None
):
    """**One score for the whole generation, and that is a decision.**

    The composer ranks on `(-score, slug)`, and a curated slug is positional
    and zero-padded to the width of its generation -- so the *tie* is the
    model's own ordering, already, exactly once.
    `domain/curation.py`'s `SLUG_PREFIX` comment says so ("every curated row
    carries the same base score"), and this is the case that makes that
    sentence true rather than aspirational. The citation read
    `curation_validate.SLUG_PREFIX` for one commit -- the same commit that
    moved the constant out of the validator, so the drift and its correction
    shipped together.

    Kills a per-row decrement. `BecauseYouWatchedProvider` has one
    (`_SEED_STEP`) and needs it for the *opposite* reason: its slugs carry a
    seed id, so its tie alphabetises "Because you watched Arrival" above
    "Because you watched Zodiac" regardless of which was watched last night.
    Here a decrement would be a second spelling of an order the slug already
    carries, and two spellings of one order are two things that can disagree.

    Seeded at ten shelves so the width is two and the padding is load-bearing:
    unpadded, `curated-10` sorts between `curated-1` and `curated-2` and the
    composer's tiebreak alphabetises the judgement the completion was bought
    for. Five proposals is what makes a decrement observable at all -- a
    one-row generation ties with itself.

    **The ordering is `usher.services.home._ranking` itself, imported.** This
    case re-spelled it as `key=lambda one: (-one.score, one.row.slug)` for one
    commit, which is the exact objection the paragraph above raises against a
    per-row decrement: two spellings of one order are two things that can
    disagree, and a re-spelled composer here would pass against a composer that
    had changed. `_ranking` takes a `_Candidate`, so the pairing this file has
    to build is the one the composer builds -- which is also the shape
    `_Candidate`'s own docstring argues for, a pairing carried rather than
    reconstructed.
    """
    library = Library()
    for position in range(10):
        card = await library.title(f"A Card At {position}")
        await library.curated([card], position=position, width=2)
    stored = await library.curated_rows.list_for_user(USER.id)
    assert [one.slug for one in stored][:3] == ["curated-01", "curated-02", "curated-03"], (
        "the premise: a ten-row generation is zero-padded to a width of two"
    )

    provider = CuratedProvider()
    proposed = await provider.propose(library.context())

    assert len(proposed) > 1, "one proposal ties with itself, so this would prove nothing"
    assert {one.score for one in proposed} == {CURATED_SCORE}
    composed = sorted(
        (_Candidate(provider=provider, proposal=one) for one in proposed), key=_ranking
    )
    assert [one.row.slug for one in composed] == [one.slug for one in stored[:MAX_CURATED_ROWS]]


async def test_a_household_with_no_generation_of_its_own_gets_no_curated_shelves() -> None:
    """**The scope is `ctx.user.id`, and it is the one predicate this
    subsystem's Postgres read actually got wrong** -- deleting the `user_id`
    half of `list_for_user`'s `WHERE` passed all fourteen integration cases,
    because every fixture minted a fresh `generation_id` per household and the
    two predicates were then exactly as selective as one another.

    One layer up the same mistake is `list_for_user(<anything else>)`, and it
    is worse here than in the repository: what crosses is not a count but a
    *screen* -- another household's headings, their reasons, and a shelf of
    films this one has already watched, rendered as a personal recommendation.

    The premise is read back through the port, so a fixture that silently
    stopped storing the other household's generation fails on its own line
    instead of making the assertion below vacuous. The empty-table arm is the
    registry sweep's (`test_every_provider_returns_nothing_against_an_empty_
    database`), which runs this provider against a `Library()` with nothing in
    it at all.
    """
    library = Library()
    somebody_else = new_id()
    await _generation(library, 3, user_id=somebody_else)

    assert await library.curated_rows.list_for_user(somebody_else), (
        "the premise: the other household really does have a generation stored"
    )

    assert await CuratedProvider().propose(library.context()) == []


async def test_a_slug_is_read_not_remembered_so_a_width_change_renames_every_shelf() -> None:
    """**A curated slug is unique within one generation and is not a stable
    name across two**, because the padding width is a property of the
    generation: ten rows mint `curated-01` and nine mint `curated-1`. The one
    copy of that argument is `domain/curation.py`'s `slug` comment, beside the
    `RowCache` key it is about.

    This provider is allowed not to care, and this is the case that says so:
    it reads the slug it was handed and compares it to nothing, so the whole
    of the instability's reach is `RowCache`, where the old width's entry is
    *orphaned* rather than overwritten -- a guaranteed miss, a rebuild, and a
    dead entry its own TTL reclaims. It is harmless only because
    `replace_for_user` is delete-then-insert; an upsert keyed on
    `(user_id, slug)` would leave last night's nine beside tonight's ten and
    paint nineteen shelves.

    Kills a provider that keyed anything on the slug across generations -- a
    memo, a dedupe, a "have I proposed this before". The premise asserts the
    two nights share no slug at all, which is what makes the second read's
    answer a statement about the second generation rather than a coincidence.
    """
    library = Library()
    for position in range(10):
        card = await library.title(f"Night One {position}")
        await library.curated([card], position=position, width=2)
    first = [one.row.slug for one in await CuratedProvider().propose(library.context())]

    library.generation()
    for position in range(9):
        card = await library.title(f"Night Two {position}")
        await library.curated([card], position=position, width=1)
    second = [one.row.slug for one in await CuratedProvider().propose(library.context())]

    assert set(first) & set(second) == set(), (
        "the premise: a width change renames every shelf, so the two nights share no slug"
    )
    assert first == ["curated-01", "curated-02", "curated-03", "curated-04", "curated-05"]
    assert second == ["curated-1", "curated-2", "curated-3", "curated-4", "curated-5"]


async def test_every_proposed_shelf_carries_the_providers_own_slug_prefix() -> None:
    """**The property that makes `usher.row.build.duration`'s `provider` label
    provably about the rows it measures**, and the one member of the registry
    for which the registry's own sweep cannot check it: that sweep seeds a rich
    household with no watch states, and a curated generation is not something a
    household *has* -- it is something a nightly job left, so `_populated()`
    has none and `CuratedProvider` correctly proposes nothing there.

    The prefix is `domain/curation.py`'s `SLUG_PREFIX`, imported rather than
    restated, because `services.curation_validate` is the only thing that mints
    a curated slug and a provider may not import it (it is `usher.services.*`
    outside `usher.services.rows`, which
    `test_no_provider_reaches_a_port_the_context_does_not_carry` forbids). Two
    copies of that string would drift into a dashboard panel labelled `curated`
    charting nothing, beside `shelf-3` rows nobody can find, with no error
    anywhere.
    """
    library = Library()
    await _generation(library, 3)
    provider = CuratedProvider()

    proposed = await provider.propose(library.context())

    assert provider.slug_prefix == SLUG_PREFIX
    assert len(proposed) == 3, "the sweep saw no proposals, so it proves nothing"
    for one in proposed:
        assert one.row.slug.startswith(provider.slug_prefix), (
            f"a curated shelf proposed {one.row.slug!r} under the prefix {provider.slug_prefix!r}"
        )


def test_the_curated_module_holds_no_llm_client_and_cannot_complete_anything() -> None:
    """**PRD 06 states this as a constraint on the class** -- *"`LLMRow.build()`
    only hydrates stored output. Generation happens in a background job --
    never in the request path"* -- and "it did not raise" is also what a
    provider that swallowed everything produces, so it is asserted
    structurally.

    `test_no_provider_reaches_a_port_the_context_does_not_carry` does **not**
    cover this: `usher.ports.llm` is under `usher.ports`, so an `LLMClient` on
    this module passes that scan whole. Same shape as
    `test_the_home_service_and_every_provider_hold_no_source_adapter` one file
    over, and the same two misses it learned: the scan walks `ast.Import` as
    well as `ast.ImportFrom`, because `import usher.ports.llm` is invisible to
    an ImportFrom-only walk, and the name check reads the **source text**,
    because a string annotation needs no import at all.

    What would ship without it is a paid network round trip inside `GET /home`,
    behind a 30 s cache, once per household per miss -- and it would work.

    **The name scan runs over the module with its docstrings removed**, which
    is not fastidiousness: this module's own prose argues at length about the
    `LLMClient` it must not hold, so a raw `"LLMClient" not in source` is an
    assertion that fails on the *explanation* and would be "fixed" by deleting
    the sentence. `ast.unparse` of a docstring-stripped tree keeps every
    identifier and every string annotation and drops only the prose.
    """
    source = pathlib.Path(inspect.getfile(CuratedProvider)).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert imported, "the import scan found nothing, so it proves nothing"
    for name in imported:
        assert "llm" not in name.split("."), f"the curated row module imports {name}"

    code = ast.unparse(_without_prose(tree))
    assert "CuratedProvider" in code, "the prose strip took the module with it"
    for forbidden in ("LLMClient", "complete_json", "LLMUsage"):
        assert forbidden not in code, f"the curated row module names {forbidden}"


def _without_prose(tree: ast.Module) -> ast.Module:
    """`tree` with every docstring removed, so a name scan reads code only."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


# -- what a family of shelves costs to hydrate ------------------------------


class _CountingTitles(FakeTitleRepository):
    """`FakeTitleRepository` with a tally on the catalog read `hydrate` makes."""

    def __init__(self) -> None:
        super().__init__()
        self.catalog_reads = 0
        self.asked_for: list[list[uuid.UUID]] = []

    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        self.catalog_reads += 1
        self.asked_for.append(list(title_ids))
        return await super().list_by_ids(title_ids)


class _CountingCopies(FakeMediaItemRepository):
    """`FakeMediaItemRepository` with a tally on the ownership read."""

    def __init__(self) -> None:
        super().__init__()
        self.ownership_reads = 0

    async def owned_title_ids(self, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        self.ownership_reads += 1
        return await super().owned_title_ids(title_ids)


async def _counted() -> tuple[Library, _CountingTitles, _CountingCopies]:
    catalog = _CountingTitles()
    copies = _CountingCopies()
    return Library(titles=catalog, media_items=copies), catalog, copies


async def _shelf(library: Library, position: int, cards: int) -> CuratedRow:
    """One shelf of `cards` distinct titles, seeded at `position`."""
    return await library.curated(
        [await library.title(f"{_HEADINGS[position]} {index}") for index in range(cards)],
        position=position,
        title=_HEADINGS[position],
    )


async def test_a_family_of_shelves_hydrates_in_two_statements_rather_than_two_per_shelf() -> None:
    """**One generation, one read of each kind -- the finding, as a count.**

    `propose` returns up to five shelves from a *single* `list_for_user`, so
    every card id in the family is already in hand before anything builds; and
    `HomeService._MAX_PER_FAMILY` is 4, so four of them are built. Each
    `BaseRow.build` issued its own `list_by_ids` and its own `owned_title_ids`
    -- **eight round trips for the ~22 distinct ids one generation names**, all
    of them inside one request, on a screen whose whole budget is 400 ms.

    Measured before the shared read landed: `catalog_reads == 4` and
    `ownership_reads == 4`.

    **The count is not the whole assertion, and the other half is the one that
    could go silently wrong.** A shared read hands every shelf the *union*, so
    the failure mode a count cannot see is a shelf rendering the family's cards
    instead of its own -- twenty-two cards on a four-card shelf, ordered by
    whatever the union was read in, which is a populated row nobody would call
    broken by looking at it. So each row's cards are asserted to be exactly its
    own stored ids, in its own stored order, which is the property this whole
    module exists for: the ordering *is* the artefact.
    """
    library, catalog, copies = await _counted()
    stored = [await _shelf(library, position, cards=4) for position in range(4)]
    by_position = sorted(stored, key=lambda one: one.position)
    ctx = library.context()

    proposed = await CuratedProvider().propose(ctx)
    built = [await one.row.build(ctx) for one in proposed]

    assert catalog.catalog_reads == 1, "the family read the catalog once per shelf"
    assert copies.ownership_reads == 1, "the family read ownership once per shelf"
    assert [row.slug for row in built] == [one.slug for one in by_position]
    for row, one in zip(built, by_position, strict=True):
        assert [card.title_id for card in row.cards] == list(one.card_title_ids), (
            "a shelf rendered the family's cards rather than its own"
        )


async def test_a_shelf_the_composer_never_builds_costs_nothing() -> None:
    """**At build time, not at propose time**, which is the difference between
    a shared read and a read for rows nobody sees.

    `propose` is the cheap phase (`services/home.py`), and the composer builds
    at most `_MAX_PER_FAMILY` of the five shelves this provider may return --
    so a family read taken while proposing would hydrate a shelf the per-family
    cap is about to discard, every time a generation produced five. That is the
    one-phase design ADR-0023 rejected, arriving inside one provider.

    The premise is the second half: the same fixture, one shelf built, and the
    reads appear. Without it `0 == 0` is also what a provider that never reads
    anything produces.
    """
    library, catalog, copies = await _counted()
    for position in range(5):
        await _shelf(library, position, cards=4)
    ctx = library.context()

    proposed = await CuratedProvider().propose(ctx)

    assert len(proposed) == MAX_CURATED_ROWS
    assert catalog.catalog_reads == 0, "proposing hydrated shelves nobody has asked for"
    assert copies.ownership_reads == 0

    await proposed[0].row.build(ctx)

    assert catalog.catalog_reads == 1
    assert copies.ownership_reads == 1


async def test_a_shelf_served_from_the_row_cache_reads_nothing_at_all() -> None:
    """**A cached row skips the shared read, because it skips `build`.**

    `HomeService._build` returns `RowCache.get_row`'s hit *before* touching the
    row, so the family memo is never consulted -- it is per-`propose`, and a
    second composition mints new `LLMRow`s over a fresh one. That has to stay
    true in both directions: a cache hit must not pay for a read, and a memo
    must not keep a shelf alive past its own TTL.

    The clock steps 60 s: past `_SCREEN_TTL` (30 s), so the screen is
    recomposed and every provider re-proposes, and well inside `LLMRow`'s
    5-minute row TTL, so the shelves themselves are hits. That gap is the only
    state in which this question is askable at all -- `RowCache.invalidate`
    drops both halves together, and a shorter step is a screen hit that never
    reaches a provider.
    """
    library, catalog, copies = await _counted()
    for position in range(4):
        await _shelf(library, position, cards=4)
    ctx = library.context()
    clock = NOW
    cache = RowCache(clock=lambda: clock)
    service = HomeService(providers=[CuratedProvider()], cache=cache)

    first = await service.compose(ctx)

    assert catalog.catalog_reads == 1
    assert copies.ownership_reads == 1

    clock = NOW + timedelta(seconds=60)
    second = await service.compose(ctx)

    assert catalog.catalog_reads == 1, "a shelf served from the row cache was hydrated again"
    assert copies.ownership_reads == 1
    # The premise, both halves: the screen really did expire (so this is not a
    # screen hit that never reached the provider), and the rows really were
    # served from the cache rather than rebuilt.
    assert cache.get_row(USER.id, first[0].slug) is not None
    assert [row.slug for row in second] == [row.slug for row in first]
    assert [[card.title_id for card in row.cards] for row in second] == [
        [card.title_id for card in row.cards] for row in first
    ]
