"""`BaseRow`'s artwork hook -- the fourth read every shelf makes, and the one
decision on a card that no client could make for itself.

**The wrong implementations these cases rule out:**

1. **Poster and backdrop swapped.** A backdrop in a `portrait` slot is 16:9
   painted into a 2:3 frame: populated, correctly shaped, and wrong on every
   card of every shelf at once, with nothing reporting an error. This is the
   headline plant, and no membership assertion can see it -- both spellings
   answer an id for every card. Every case here that cares asserts *which* id,
   with `assert poster_id != backdrop_id` as its own premise so it cannot pass
   by both being `None`.
2. **One read per card.** `ImageRepository.primary_for_titles` takes a sequence
   precisely so the per-card shape is inexpressible, and a shelf is up to
   thirty cards on a screen composing ten of them. Counted at two lengths and
   asserted **equal**, never `== 1` once -- `== 1` also passes for an
   implementation that answers the first title only.
3. **A read for a shelf with no cards.** `hydrate` returns `()` before it asks
   anything, and a fourth port call that ran anyway would be one statement per
   *dropped* row on every screen.
4. **The first image rather than the flagged one.** `id` is first-sighting
   order, so a fixture that seeds the flagged image first agrees with `ORDER BY
   id` by accident -- the trap `CLAUDE.md` names. The flagged image is seeded
   **second** here and the premise says so.
5. **A logo on a card.** A card paints a poster or a backdrop and never a logo,
   and the `kind` filter is the whole of why: a title whose only artwork is a
   logo carries `artwork=None`, which is the same card a title with no artwork
   at all gets. That is deliberate and is
   [ADR-0032](../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)'s
   SVG ruling arriving here -- *"no logo"* and *"a logo we will not serve"*
   produce the identical action on a card, so there is no discriminator.
6. **A hint the mapping does not cover.** `DisplayHint` is closed at four and
   two of them (`wide`, `square`) have no emitter in `services/rows/` today, so
   a mapping written from the providers rather than from the vocabulary is a
   `KeyError` at render time on the first row that uses one.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

import pytest

from tests.unit.rows import Library, days_ago
from usher.domain.enums import ImageKind
from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext
from usher.services.home import HomeService
from usher.services.rows import ROW_PROVIDERS
from usher.services.rows.base import ARTWORK_FOR_HINT, BaseRow


class _Shelf(BaseRow):
    """The smallest thing that is a row: a hint and a list of ids.

    Hand-rolled rather than borrowed from `services/rows/`, because the subject
    is `BaseRow` and every shipped provider fixes its own hint -- two of the
    four `DisplayHint` members have no emitter at all, so a case written
    through a real provider could not reach them.
    """

    def __init__(self, title_ids: Sequence[uuid.UUID], *, hint: DisplayHint) -> None:
        self._ids = tuple(title_ids)
        self._hint = hint

    @property
    def slug(self) -> str:
        return f"a-shelf-{self._hint.value}"

    @property
    def title(self) -> str:
        return "A Shelf"

    @property
    def reason(self) -> str | None:
        return None

    @property
    def family(self) -> RowFamily:
        return RowFamily.SOURCE

    @property
    def display_hint(self) -> DisplayHint:
        return self._hint

    @property
    def ttl(self) -> timedelta:
        return timedelta(minutes=5)

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        return self._ids


async def test_the_hint_decides_the_kind_and_a_poster_is_not_a_backdrop() -> None:
    """**The headline, and the one wrong implementation a shelf cannot show
    you.** One title, both kinds of artwork, two shelves over it that differ in
    nothing but `display_hint`.

    Under the swap both rows still carry an id for the card, both render, and
    the only symptom is a 16:9 image in a 2:3 slot -- which is why the premise
    is asserted: with `poster_id == backdrop_id` the case would pass against
    any implementation at all, including one that answers a constant.
    """
    library = Library()
    title_id = await library.title("A Film With Both")
    poster_id = await library.artwork(title_id, kind=ImageKind.POSTER)
    backdrop_id = await library.artwork(title_id, kind=ImageKind.BACKDROP)
    ctx = library.context()

    assert poster_id != backdrop_id, "the premise: the two kinds are two rows"

    portrait = await _Shelf([title_id], hint=DisplayHint.PORTRAIT).build(ctx)
    landscape = await _Shelf([title_id], hint=DisplayHint.LANDSCAPE).build(ctx)

    assert portrait.cards[0].artwork == poster_id
    assert landscape.cards[0].artwork == backdrop_id


@pytest.mark.parametrize(
    ("hint", "kind"),
    [
        (DisplayHint.PORTRAIT, ImageKind.POSTER),
        (DisplayHint.SQUARE, ImageKind.POSTER),
        (DisplayHint.LANDSCAPE, ImageKind.BACKDROP),
        (DisplayHint.WIDE, ImageKind.BACKDROP),
    ],
)
async def test_every_hint_in_the_vocabulary_takes_the_kind_it_was_given(
    hint: DisplayHint, kind: ImageKind
) -> None:
    """All four members, and two of them have no emitter in `services/rows/`.

    `wide` and `square` are in ADR-0006's vocabulary and no provider returns
    either today, so a mapping derived from what the providers happen to use
    would be complete-looking and would `KeyError` on the first row that used
    one. Parametrised over the *enum* rather than over the providers.

    Each arm seeds the **other** kind as well, so an implementation ignoring
    `kind` entirely answers the wrong id rather than `None` -- a `None` here
    would be indistinguishable from a title with no artwork.
    """
    library = Library()
    title_id = await library.title("A Film With Both")
    wanted = await library.artwork(title_id, kind=kind)
    other = await library.artwork(
        title_id, kind=ImageKind.BACKDROP if kind is ImageKind.POSTER else ImageKind.POSTER
    )
    ctx = library.context()

    assert wanted != other, "the premise: the two kinds are two rows"

    row = await _Shelf([title_id], hint=hint).build(ctx)

    assert row.cards[0].artwork == wanted


def test_the_hint_to_kind_mapping_is_total_over_the_vocabulary() -> None:
    """`DisplayHint` is closed at four and `ARTWORK_FOR_HINT` has to stay in
    step with it, which is a fact about two vocabularies rather than about any
    row.

    The behavioural case above covers today's four; this one fails the day a
    fifth hint is added without a kind, which is a `KeyError` inside `hydrate`
    -- a 500 on a home screen -- and which no case parametrised over the four
    members that exist can see.
    """
    assert set(ARTWORK_FOR_HINT) == set(DisplayHint)
    assert set(ARTWORK_FOR_HINT.values()) == {ImageKind.POSTER, ImageKind.BACKDROP}


async def test_a_title_with_no_artwork_carries_none_beside_cards_that_have_some() -> None:
    """**The `None` arm, on a shelf where it is not the only answer.**

    A catalog that has never been derived has no artwork at all, so a case
    seeding nothing would pass against an implementation that never reads
    anything. The shelf here is mixed: one title with a poster, one without,
    and the ordering is the row's own, so the `None` sits between two ids.
    """
    library = Library()
    first = await library.title("A Film With A Poster")
    poster_id = await library.artwork(first, kind=ImageKind.POSTER)
    bare = await library.title("A Film Nobody Derived")
    last = await library.title("Another Film With A Poster")
    other_id = await library.artwork(last, kind=ImageKind.POSTER)
    ctx = library.context()

    row = await _Shelf([first, bare, last], hint=DisplayHint.PORTRAIT).build(ctx)

    assert [card.artwork for card in row.cards] == [poster_id, None, other_id]


async def test_a_title_whose_only_artwork_is_a_logo_carries_none() -> None:
    """ADR-0032's SVG ruling, as the state it makes unreachable.

    The proxy refuses `image/svg+xml` -- measured, the CDN ignores the ladder
    entirely for it -- and TMDb publishes some logos as `.svg`. A card is never
    handed one, because a card paints a poster or a backdrop and the `kind`
    filter is what says so. So *"no logo"* and *"a logo we will not serve"* are
    the same card, and there is no discriminator field to tell them apart.

    Kills a hook that reads `list_for_title` and takes the first row, which is
    the obvious implementation and which hands a logo to a portrait shelf.
    """
    library = Library()
    title_id = await library.title("A Film With Only A Logo")
    await library.artwork(title_id, kind=ImageKind.LOGO)
    ctx = library.context()

    row = await _Shelf([title_id], hint=DisplayHint.PORTRAIT).build(ctx)

    assert row.cards[0].artwork is None


async def test_the_flagged_image_wins_over_the_one_that_was_seen_first() -> None:
    """`(is_primary DESC, id)`, and the fixture is arranged so `ORDER BY id`
    disagrees.

    `Image.id` is a UUIDv7, so first-sighting order *is* id order, and a
    fixture seeding the flagged image first would be satisfied by an
    implementation that ignored the flag entirely -- the trap `CLAUDE.md` names
    and which cost M7 five untested orderings. The unflagged image is seeded
    first and the premise says so in terms of the ids themselves.

    `m09c` carries no `sort_order` column, deliberately, so which image is
    primary is the *only* thing a provider re-ranking its posters can move in
    Usher's answer. That makes this the whole of the ordering contract a card
    depends on.
    """
    library = Library()
    title_id = await library.title("A Film With Two Posters")
    first_seen = await library.artwork(title_id, kind=ImageKind.POSTER, is_primary=False)
    flagged = await library.artwork(title_id, kind=ImageKind.POSTER, is_primary=True)
    ctx = library.context()

    assert first_seen < flagged, "the premise: id order and the flag disagree"

    row = await _Shelf([title_id], hint=DisplayHint.PORTRAIT).build(ctx)

    assert row.cards[0].artwork == flagged


async def test_a_whole_shelf_costs_one_artwork_read_whatever_its_length() -> None:
    """**Counted, not timed** -- a timing assertion against an in-memory dict
    measures the dict (`rows-and-genome.md`'s four-reads finding).

    Asserted **equal** at two lengths rather than `== 1` once: `== 1` is also
    what an implementation answering the first title only produces, and two
    lengths giving one number is the claim that the cost does not scale. The
    premise is that the long shelf really did paint every card, so the equality
    is not bought by a shelf that hydrated nothing.
    """
    library = Library()
    one = [await library.title("A Film On Its Own")]
    thirty = [await library.title(f"A Film Numbered {index}") for index in range(30)]
    for title_id in [*one, *thirty]:
        await library.artwork(title_id, kind=ImageKind.POSTER)
    ctx = library.context()

    library.images.reset_calls()
    short = await _Shelf(one, hint=DisplayHint.PORTRAIT).build(ctx)
    for_one = library.images.calls

    library.images.reset_calls()
    long = await _Shelf(thirty, hint=DisplayHint.PORTRAIT).build(ctx)
    for_thirty = library.images.calls

    assert len(long.cards) == 30, "the premise: the long shelf really did hydrate every card"
    assert all(card.artwork is not None for card in long.cards)
    assert len(short.cards) == 1
    assert for_one == 1
    assert for_thirty == for_one


async def test_a_composed_screen_costs_one_artwork_read_per_shelf_and_no_more() -> None:
    """**`+1 per shelf`, counted against fakes rather than timed** -- the honest
    unit for this task's cost, because the home path's p95 is a property of the
    household and not of the composer (the 5,200-copy and 1,277,878-copy
    figures differ by 30x, `rows-and-genome.md`).

    Asserted **equal to the number of shelves the composer actually built**,
    derived from the screen rather than written as a literal, so the case says
    "one per shelf" rather than "some number I measured once". A row the
    composer proposed and did not build costs nothing, and a row that built
    empty costs nothing -- both of which a literal would hide.
    """
    library = Library()
    resuming = await library.title("A Film Half Watched", added=days_ago(200))
    await library.artwork(resuming, kind=ImageKind.BACKDROP)
    await library.in_progress(resuming, at=days_ago(2))
    arrived = await library.title("A Film That Just Arrived", added=days_ago(1))
    await library.artwork(arrived, kind=ImageKind.POSTER)
    ctx = library.context()

    library.images.reset_calls()
    screen = await HomeService(providers=list(ROW_PROVIDERS)).compose(ctx)

    assert len(screen) >= 2, "the premise: the household really composed more than one shelf"
    assert library.images.calls == len(screen)


async def test_a_shelf_with_no_titles_reads_no_artwork_at_all() -> None:
    """`hydrate` returns `()` before it asks anything, and the composer drops
    empty rows -- so a fourth port call made anyway would be one statement per
    *dropped* shelf, on every screen, for a row nobody sees.

    The premise is the second half: the same fixture with one id, and the read
    appears. Without it `0 == 0` is also what an implementation that never
    reads artwork produces.
    """
    library = Library()
    title_id = await library.title("A Film That Is Not On This Shelf")
    await library.artwork(title_id, kind=ImageKind.POSTER)
    ctx = library.context()

    library.images.reset_calls()
    empty = await _Shelf([], hint=DisplayHint.PORTRAIT).build(ctx)

    assert empty.cards == ()
    assert library.images.calls == 0

    await _Shelf([title_id], hint=DisplayHint.PORTRAIT).build(ctx)

    assert library.images.calls == 1
