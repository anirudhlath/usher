"""`GenreAffinityProvider` -- the row that must work with no embedder.

**The wrong implementation these cases rule out is a sentence that is false
out loud.** *"You watch a lot more Drama than your library would suggest"* is
a claim about **lift**, and it is true exactly when lift is what was ranked.
An implementation that ranks by raw watched volume says the same sentence
about the household's most common genre -- which is the *library's* most
common genre -- on every household in the deployment, in a correctly-shaped
row, spoken aloud by Alfred.

Task 23 owns the two distractors the front matter names for this provider
(the household's most common genre, whose lift is ~1.0, and a genre watched
once, whose support is 1): both are filtered by `_MIN_LIFT` and `_MIN_SUPPORT`
before a `GenreAffinity` exists at all, and
`tests/unit/test_services_taste.py` seeds them together. What is left for
*this* file is everything downstream of that list, and the volume-ranking
failure survives into it: the provider is handed a lift-ordered sequence and
can still re-rank it by support.
"""

import pytest

from tests.unit.rows import NOW, Library, days_ago
from usher.domain.enums import TitleKind
from usher.domain.rows import RowFamily
from usher.domain.taste import Centroid, GenreAffinity
from usher.services.rows.genre_affinity import (
    GENRE_AFFINITY_SCORE_CEILING,
    GenreAffinityProvider,
)

pytestmark = pytest.mark.anyio


def _affinity(genre: str, *, lift: float, support: int) -> GenreAffinity:
    return GenreAffinity(genre=genre, lift=lift, support=support)


async def test_the_affinity_row_is_about_lift_and_not_about_volume() -> None:
    """**Six engaged westerns at lift 4.4 against forty engaged dramas at lift
    1.6**, handed over in Task 23's own lift order.

    The wrong implementation re-ranks by `support`, which is the count of
    titles -- volume -- and it produces a populated, hydrated, plausibly
    labelled Drama row first. Nothing about either row's shape distinguishes
    them; only the order does, so the assertion is on the proposed rows' slugs
    *in sequence* and the drama row is asserted not to be first rather than
    asserted absent (it is a real affinity and belongs on the screen).

    The fixture is what makes it non-vacuous: drama has **six times** western's
    support, so a support ranking is not a coin flip.
    """
    library = Library()
    for index in range(6):
        await library.title(f"Western {index}", genres=("Western",), popularity=float(index))
    for index in range(40):
        await library.title(f"Drama {index}", genres=("Drama",), popularity=float(index))

    rows = await GenreAffinityProvider().propose(
        library.context(
            affinities=[
                _affinity("Western", lift=4.4, support=6),
                _affinity("Drama", lift=1.6, support=40),
            ]
        )
    )

    assert [row.row.slug for row in rows] == ["genre-affinity-western", "genre-affinity-drama"]
    assert rows[0].score > rows[1].score


async def test_the_reason_claims_lift_rather_than_volume() -> None:
    """`reason` is written to be spoken (PRD 06's Alfred section), and this
    sentence is *generated from the computation*: it says the household watches
    more of this genre than their library would predict, which is what lift
    means and what a volume ranking would make false on every household at
    once.
    """
    library = Library()
    for index in range(4):
        await library.title(f"Western {index}", genres=("Western",))

    rows = await GenreAffinityProvider().propose(
        library.context(affinities=[_affinity("Western", lift=4.0, support=4)])
    )

    assert rows[0].row.reason == "You watch a lot more Western than your library would suggest."
    assert rows[0].row.title == "More Western"
    assert rows[0].row.family is RowFamily.SOURCE
    assert rows[0].pinned is False


async def test_the_cards_are_owned_and_unwatched_with_the_best_first() -> None:
    """Two distractors, each varying **one** thing, and each seeded as the
    *best* match in the catalog so it is `cards[0]` under the implementation
    that forgets it.

    - An unowned horror at the highest popularity in the library. A "you love
      horror" shelf of films nobody can play looks perfect and does nothing.
    - An owned horror the household already finished, at the second-highest.
      The row exists to offer something *new*; a shelf of the four westerns
      they already watched is circular.

    A membership assertion on the survivor passes with both of them sitting
    above it.
    """
    library = Library()
    unowned = await library.title("Unowned Best", genres=("Horror",), popularity=99.0, owned=False)
    watched = await library.title("Already Seen", genres=("Horror",), popularity=98.0)
    await library.finished(watched, at=days_ago(30))
    best = await library.title("Owned And New", genres=("Horror",), popularity=50.0)
    also = await library.title("Owned And Newer", genres=("Horror",), popularity=10.0)

    rows = await GenreAffinityProvider().propose(
        library.context(affinities=[_affinity("Horror", lift=3.0, support=5)])
    )
    built = await rows[0].row.build(library.context())

    assert [card.title_id for card in built.cards] == [best, also]
    assert unowned not in {card.title_id for card in built.cards}
    assert watched not in {card.title_id for card in built.cards}


async def test_a_series_watched_only_through_its_episodes_is_not_offered_again() -> None:
    """**Trap 7 in the unwatched filter.** An episode's watch state carries
    `title_id IS NULL`, so a "has the household seen this" check keyed on
    `watch_states.title_id` answers films only -- and every series the
    household is partway through comes back as something new, in a row headed
    "you watch a lot more Horror".

    The distractor is a *film* the household finished, so a films-only
    implementation still drops something and still looks like it is filtering.
    """
    library = Library()
    seen_film = await library.title("A Seen Film", genres=("Horror",), popularity=90.0)
    await library.finished(seen_film, at=days_ago(20))
    series_id = await library.title(
        "A Seen Series", kind=TitleKind.SERIES, genres=("Horror",), popularity=80.0
    )
    await library.episode(series_id, season=1, number=1, played=True)
    fresh = await library.title("Something New", genres=("Horror",), popularity=1.0)

    rows = await GenreAffinityProvider().propose(
        library.context(affinities=[_affinity("Horror", lift=3.0, support=5)])
    )
    built = await rows[0].row.build(library.context())

    assert [card.title_id for card in built.cards] == [fresh]


async def test_a_genre_whose_owned_titles_are_all_watched_builds_empty() -> None:
    """The row is **proposed** -- the affinity is real and the claim is true --
    and builds with no cards, so `HomeService` drops it (PRD 06: *"drops any
    that build empty"*).

    Fails the implementation that pads with watched titles to avoid an empty
    row, which is the popular-titles fallback scoped to one genre. It is also
    why this provider's `propose` reads nothing: proposing on the affinity
    alone and reading cards in `build` is what keeps "this row had nothing to
    show" a different state from "this row was never proposed".

    Note the plan's own table says this provider *"returns nothing when ...
    every card would be a title already watched"*, which contradicts the case
    it specifies two paragraphs later. The case wins; the table is recorded as
    wrong.
    """
    library = Library()
    for index in range(4):
        title_id = await library.title(f"Seen {index}", genres=("Horror",))
        await library.finished(title_id, at=days_ago(30 + index))

    rows = await GenreAffinityProvider().propose(
        library.context(affinities=[_affinity("Horror", lift=3.0, support=4)])
    )
    built = await rows[0].row.build(library.context())

    assert len(rows) == 1
    assert built.cards == ()


async def test_no_more_than_three_affinity_rows_are_proposed() -> None:
    """PRD 06 says 1-3 rows. Task 23's `_MAX_AFFINITY_ROWS` bounds the
    affinities; this pins that the provider does not then emit one row per
    *card set* or re-expand them -- and it is written against a context
    carrying five, because a provider that trusted its input would be correct
    only for as long as the other cap holds.
    """
    library = Library()
    genres = ("Horror", "Western", "Musical", "Noir", "Documentary")
    for genre in genres:
        for index in range(3):
            await library.title(f"{genre} {index}", genres=(genre,))

    rows = await GenreAffinityProvider().propose(
        library.context(
            affinities=[
                _affinity(genre, lift=4.0 - position * 0.1, support=4)
                for position, genre in enumerate(genres)
            ]
        )
    )

    assert [row.row.slug for row in rows] == [
        "genre-affinity-horror",
        "genre-affinity-western",
        "genre-affinity-musical",
    ]


async def test_a_household_with_no_affinities_proposes_nothing() -> None:
    """No genre cleared Task 23's lift and support floors, which is the common
    answer and is a real one. **Never "the library's most common genres"**,
    which is the popular-titles fallback wearing a taste row's title -- so the
    library here is deliberately full and deliberately tagged.
    """
    library = Library()
    for index in range(10):
        await library.title(f"Drama {index}", genres=("Drama",), popularity=float(index))

    assert await GenreAffinityProvider().propose(library.context(affinities=[])) == []


async def test_a_bigger_lift_outscores_a_smaller_one_and_then_saturates() -> None:
    """The score *is* the lift, up to a saturation point.

    Beyond "three times what the library predicts" the row is not more wanted,
    and an unsaturated lift lets a thin-library artefact -- a lift of fifty
    over a library holding one western -- outscore everything else on the
    screen. Asserted three ways at once, because `_LIFT_SATURATION = 100.0`
    keeps the *ordering* and destroys the *scale*, and an ordering assertion
    alone would ratify it.
    """
    library = Library()
    for genre in ("Horror", "Western", "Musical"):
        for index in range(3):
            await library.title(f"{genre} {index}", genres=(genre,))

    rows = await GenreAffinityProvider().propose(
        library.context(
            affinities=[
                _affinity("Horror", lift=9.0, support=9),
                _affinity("Western", lift=3.0, support=5),
                _affinity("Musical", lift=1.5, support=4),
            ]
        )
    )

    scores = {row.row.slug: row.score for row in rows}
    assert scores["genre-affinity-horror"] == pytest.approx(GENRE_AFFINITY_SCORE_CEILING)
    assert scores["genre-affinity-western"] == pytest.approx(GENRE_AFFINITY_SCORE_CEILING)
    assert scores["genre-affinity-musical"] == pytest.approx(GENRE_AFFINITY_SCORE_CEILING * 0.5)


async def test_the_provider_returns_the_same_rows_with_and_without_a_centroid() -> None:
    """**The reason this provider exists in this shape.** PRD 06 fires it on
    *"taste centroid concentrated in a genre"*; implemented literally it makes
    the most broadly-useful provider the one that never fires, because the
    embedder is optional and off by default (ADR-0022).

    Task 23 corrected that, and this is the provider-level half of the same
    assertion: the rows are identical against a context with a real centroid
    and against the shipped default's `None`. Kills an "improvement" that
    reaches for `TasteService.centroid()` here -- which would silently disable
    the row on most deployments and raise nothing anywhere.
    """
    library = Library()
    for index in range(4):
        await library.title(f"Western {index}", genres=("Western",), popularity=float(index))
    affinities = [_affinity("Western", lift=4.0, support=4)]
    centroid = Centroid(
        user_id=library.context().user.id,
        vector=(0.6, 0.8),
        model_name="fastembed:test",
        title_count=9,
        computed_at=NOW,
    )

    without = await GenreAffinityProvider().propose(library.context(affinities=affinities))
    with_one = await GenreAffinityProvider().propose(
        library.context(affinities=affinities, taste=centroid)
    )

    assert library.context().taste is None
    assert [row.row.slug for row in without] == [row.row.slug for row in with_one]
    assert [row.score for row in without] == [row.score for row in with_one]
    assert [card.title_id for card in (await without[0].row.build(library.context())).cards] == [
        card.title_id
        for card in (await with_one[0].row.build(library.context(taste=centroid))).cards
    ]
