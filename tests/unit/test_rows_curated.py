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
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from tests.unit.rows import USER, Library
from usher.domain.curation import CuratedRow
from usher.domain.ids import new_id
from usher.domain.rows import DisplayHint, RowFamily
from usher.services.rows.curated import LLMRow

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

    `== row.empty()` rather than only `cards == ()`, so the case also pins that
    the losing shelf is the *same value* as the empty one -- a `build` that
    quietly changed the heading or the TTL on its way to nothing would pass the
    weaker assertion.
    """
    library = Library()
    await _four(library)
    row = LLMRow(_stored([new_id(), new_id(), new_id()]))

    built = await row.build(library.context())

    assert built.cards == ()
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
