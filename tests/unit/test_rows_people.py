"""`PeopleProvider` -- the row about somebody, and the ways it names the wrong
somebody.

Every wrong implementation here returns **a real person the household really
has watched**. That is what makes the row indistinguishable from a correct one
by anything but its ordering, and it is why every case asserts on position.
"""

import pytest

from tests.unit.rows import Library, days_ago
from usher.domain.enums import TitleKind
from usher.domain.people import CreditKind
from usher.domain.rows import RowFamily
from usher.services.rows.people import PEOPLE_SCORE_CEILING, PeopleProvider

pytestmark = pytest.mark.anyio


async def _watched_film(library: Library, name: str, *, at: float, **kwargs: object) -> object:
    title_id = await library.title(name, **kwargs)  # type: ignore[arg-type]
    await library.finished(title_id, at=days_ago(at))
    return title_id


async def test_the_people_row_is_about_the_person_with_four_titles_not_the_one_with_one() -> None:
    """**The front matter's distractor**, seeded so the one-credit person sorts
    first by name *and* is minted first, which is what an implementation with
    no threshold orders by.

    Asserts the proposed rows' person ids in order and asserts the one-credit
    person is in none of them. Under `_MIN_TITLES = 1` the answer is two rows,
    both populated, both about people the household has genuinely seen -- and
    the top one is about a single appearance.
    """
    library = Library()
    passing = await library.person("A Passing Face")
    recurring = await library.person("Zoe Recurring")
    single = await _watched_film(library, "One Film", at=1)
    await library.credit(passing, single)  # type: ignore[arg-type]
    for index in range(4):
        film = await _watched_film(library, f"Recurring Film {index}", at=index + 2)
        await library.credit(recurring, film)  # type: ignore[arg-type]
        await library.title(f"Unwatched With Zoe {index}")

    rows = await PeopleProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"people-{recurring}"]
    assert f"people-{passing}" not in {row.row.slug for row in rows}


async def test_a_person_credited_twice_on_one_film_is_not_recurring() -> None:
    """One film, **two characters** -- which TMDb genuinely emits -- against a
    genuine three-title actor.

    Under `count(*)` the multiply-credited person scores 3; under distinct
    titles they score 1.

    Two seeding decisions, both measured rather than assumed. The credits
    differ by *character* and not by *job*, because the read groups by
    `(person_id, kind, job)` -- Group B measured that the plan's own seeding
    (credits differing by job) lands in separate groups of one row where the
    two counts agree exactly, and the mutation survived. And there are
    **three** of them rather than two, because `_MIN_TITLES` is 3: at two
    credits the wrong count is still below the floor and the row is absent
    either way, which is a case that passes for a reason unrelated to what it
    asserts. Measured too -- the two-credit seeding let the `count(*)`
    mutation survive this file and left the port's own contract case as the
    only cover.
    """
    library = Library()
    double = await library.person("Aaron Doubled")
    genuine = await library.person("Zoe Genuine")
    one_film = await _watched_film(library, "A Single Film", at=1)
    for part in ("A Twin", "The Other Twin", "Their Double"):
        await library.credit(double, one_film, character=part)  # type: ignore[arg-type]
    for index in range(3):
        film = await _watched_film(library, f"Genuine Film {index}", at=index + 2)
        await library.credit(genuine, film)  # type: ignore[arg-type]
        await library.title(f"Unwatched With Zoe {index}")

    rows = await PeopleProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"people-{genuine}"]


async def test_a_recurring_gaffer_does_not_outrank_a_recurring_lead() -> None:
    """Six below-the-line crew credits against three top-billed ones.

    A person credited on six films as a gaffer is recurring under any counting
    rule and means nothing: below the line, crews repeat because studios
    repeat. The qualifying set is cast or the *director* -- the two roles a
    viewer chooses a film for -- and a bare count has no way to express it, so
    it produces a row headed by a name the household has never heard, ranked
    above one they chose.

    The gaffer has **twice** the count, so this cannot pass by accident.
    """
    library = Library()
    gaffer = await library.person("A Busy Gaffer")
    lead = await library.person("Zoe Lead")
    for index in range(6):
        film = await _watched_film(library, f"Crewed Film {index}", at=index + 1)
        await library.credit(gaffer, film, kind=CreditKind.CREW, job="Gaffer")  # type: ignore[arg-type]
        if index < 3:
            await library.credit(lead, film)  # type: ignore[arg-type]
    for index in range(3):
        await library.title(f"Unwatched With Zoe {index}")

    rows = await PeopleProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"people-{lead}"]


async def test_a_recurring_director_qualifies_and_the_row_says_directed_by() -> None:
    """`CreditKind` has to reach the sentence rather than being collapsed into
    a count on the way.

    *"You've watched four films with Denis Villeneuve"* is wrong in a way a
    listener notices; *"directed by"* is the fix. It is also the case that
    keeps the role filter from being "cast only" -- a director is exactly the
    crew role a viewer chooses a film for.
    """
    library = Library()
    director = await library.person("A Real Director")
    for index in range(3):
        film = await _watched_film(library, f"Directed Film {index}", at=index + 1)
        await library.credit(director, film, kind=CreditKind.CREW, job="Director")  # type: ignore[arg-type]
    await library.title("Their Unwatched Film")

    rows = await PeopleProvider().propose(library.context())

    assert rows[0].row.reason == "You've watched 3 films directed by A Real Director."
    assert rows[0].row.title == "More from A Real Director"
    assert rows[0].row.family is RowFamily.SOURCE
    assert rows[0].pinned is False


async def test_a_cast_row_says_with_rather_than_directed_by() -> None:
    """The other half of the same sentence, so a single hard-coded string
    fails whichever one it picked."""
    library = Library()
    actor = await library.person("A Real Actor")
    for index in range(3):
        film = await _watched_film(library, f"Acted Film {index}", at=index + 1)
        await library.credit(actor, film)  # type: ignore[arg-type]
    await library.title("Their Unwatched Film")

    rows = await PeopleProvider().propose(library.context())

    assert rows[0].row.reason == "You've watched 3 films with A Real Actor."


async def test_two_people_at_equal_counts_are_ordered_by_recency_then_id() -> None:
    """Three titles each, one last watched a month ago and one in 2019.

    A row about a director the household was into three years ago is the front
    matter's opening failure with a person's name on it. The 2019 person is
    minted **first**, so id order favours the wrong answer and the counts are
    identical by construction.
    """
    library = Library()
    old = await library.person("Aaron Nostalgia")
    recent = await library.person("Zoe Current")
    for index in range(3):
        film = await _watched_film(library, f"Old Film {index}", at=1100 + index)
        await library.credit(old, film)  # type: ignore[arg-type]
        await library.title(f"Unwatched Old {index}")
    for index in range(3):
        film = await _watched_film(library, f"New Film {index}", at=20 + index)
        await library.credit(recent, film)  # type: ignore[arg-type]
        await library.title(f"Unwatched New {index}")

    rows = await PeopleProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"people-{recent}", f"people-{old}"]
    assert rows[0].score >= rows[1].score


async def test_a_series_watched_only_through_its_episodes_credits_its_people() -> None:
    """**Trap 7.** An episode's watch state carries `title_id IS NULL` and the
    credit hangs off the *series*, so a history read keyed on
    `watch_states.title_id` finds no credits at all for a television
    household -- and this row is then permanently absent on a library that is
    89% episodes, which renders identically to a household with thin history.

    Group B measured the cost exactly: a film-only `list_recurring_for_user`
    passes 11 of 13 contract cases. The distractor here is a film actor with
    **two** titles, below the floor, so a films-only implementation proposes
    nothing at all rather than something wrong -- which is the failure that
    looks like correct behaviour.
    """
    library = Library()
    television = await library.person("A Television Regular")
    film_only = await library.person("A Film Bit-Part")
    for index in range(3):
        series_id = await library.title(f"Series {index}", kind=TitleKind.SERIES)
        await library.episode(series_id, season=1, number=1, played=True)
        await library.credit(television, series_id)
    for index in range(2):
        film = await _watched_film(library, f"Film {index}", at=index + 1)
        await library.credit(film_only, film)  # type: ignore[arg-type]
    await library.title("An Unwatched Series They Are In", kind=TitleKind.SERIES)
    unwatched = await library.title("Another Unwatched Series", kind=TitleKind.SERIES)
    await library.credit(television, unwatched)

    rows = await PeopleProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"people-{television}"]


async def test_the_cards_are_owned_unwatched_titles_crediting_that_person() -> None:
    """Two distractors, each varying exactly one thing and each seeded as a
    credit of the *same* person so neither can be dropped by the person filter:

    - a title they are in that the household has already watched (the three
      that *established* the affinity -- a row made of those is circular)
    - a title they are in that the household does not own

    The survivor is asserted at `cards[0]`, and both distractors are asserted
    absent.
    """
    library = Library()
    actor = await library.person("A Real Actor")
    watched = []
    for index in range(3):
        film = await _watched_film(library, f"Seen Film {index}", at=index + 1)
        await library.credit(actor, film)  # type: ignore[arg-type]
        watched.append(film)
    unowned = await library.title("An Unowned Film", owned=False)
    await library.credit(actor, unowned)
    fresh = await library.title("Something To Watch")
    await library.credit(actor, fresh)

    rows = await PeopleProvider().propose(library.context())
    built = await rows[0].row.build(library.context())

    assert [card.title_id for card in built.cards] == [fresh]
    assert unowned not in {card.title_id for card in built.cards}
    assert not set(watched) & {card.title_id for card in built.cards}


async def test_a_person_whose_other_films_are_all_owned_and_watched_builds_empty() -> None:
    """The row is proposed -- the person really does recur -- and builds with
    no cards, so `HomeService` drops it. Fails the implementation that pads
    with the watched titles to avoid an empty row, which is the circular shelf
    the case above rules out arriving as a fallback.
    """
    library = Library()
    actor = await library.person("A Real Actor")
    for index in range(3):
        film = await _watched_film(library, f"Seen Film {index}", at=index + 1)
        await library.credit(actor, film)  # type: ignore[arg-type]

    rows = await PeopleProvider().propose(library.context())
    built = await rows[0].row.build(library.context())

    assert len(rows) == 1
    assert built.cards == ()


async def test_no_more_than_two_people_rows_are_proposed() -> None:
    """0-2 rows, PRD 06's figure. A household with a dozen recurring faces
    would otherwise claim most of a ten-row screen."""
    library = Library()
    for person_index in range(6):
        who = await library.person(f"Person {person_index:02d}")
        for index in range(3):
            film = await _watched_film(library, f"Film {person_index}-{index}", at=index + 1)
            await library.credit(who, film)  # type: ignore[arg-type]

    rows = await PeopleProvider().propose(library.context())

    assert len(rows) == 2


async def test_people_costs_one_history_statement_regardless_of_history_size() -> None:
    """The N+1 available here is worse than `NextUpProvider`'s: a
    per-engaged-title credit fetch is fifty queries to find two people, and it
    returns exactly the right answer -- which is why no assertion about the
    row's contents can see it.

    Held fixed the way M4's ingest cases hold it: the same call count against
    a household with five engaged titles and one with fifty, with the *people*
    count held constant so the bound being measured is history size.
    """

    async def household(films: int) -> int:
        library = Library()
        who = await library.person("A Real Actor")
        for index in range(films):
            film = await _watched_film(library, f"Film {index}", at=index + 1)
            await library.credit(who, film)  # type: ignore[arg-type]
        await library.title("Something To Watch")
        library.people.reset_calls()
        await PeopleProvider().propose(library.context())
        return library.people.calls

    assert await household(5) == await household(50) == 1


async def test_a_household_that_has_watched_nothing_proposes_no_people_row() -> None:
    """A fully credited, fully owned catalog and no history at all. **Never
    "people who appear a lot in your library"**, which is a fact about the
    catalog wearing a personalised row's title."""
    library = Library()
    who = await library.person("A Prolific Actor")
    for index in range(8):
        film = await library.title(f"Owned Film {index}")
        await library.credit(who, film)

    assert await PeopleProvider().propose(library.context()) == []


async def test_an_empty_credits_table_names_the_command_that_fixes_it() -> None:
    """`credits` is empty until `usher derive` has run, and a provider that
    silently never fires is indistinguishable from a household with thin
    history -- the same shape `BecauseYouWatchedProvider` and
    `FranchiseProvider` use, for the same reason.

    The household has watched plenty, so "nothing to say" here is a statement
    about the derivation rather than about the person.
    """
    library = Library()
    for index in range(6):
        await _watched_film(library, f"Watched Film {index}", at=index + 1)

    messages: list[str] = []
    from loguru import logger

    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        rows = await PeopleProvider().propose(library.context())
    finally:
        logger.remove(sink)

    assert rows == []
    assert any("usher derive" in message for message in messages)


async def test_a_larger_body_of_work_outscores_a_smaller_one_and_saturates() -> None:
    """Six watched titles is a stronger claim than three, and beyond six it is
    not a stronger want. Asserted as an ordering *and* a ceiling, because
    `_SATURATION = 1` keeps the ordering and destroys the scale."""
    library = Library()
    deep = await library.person("Zoe Deep")
    shallow = await library.person("Aaron Shallow")
    for index in range(6):
        film = await _watched_film(library, f"Deep Film {index}", at=index + 1)
        await library.credit(deep, film)  # type: ignore[arg-type]
    for index in range(3):
        film = await _watched_film(library, f"Shallow Film {index}", at=index + 20)
        await library.credit(shallow, film)  # type: ignore[arg-type]
    await library.title("Something Unwatched")

    rows = await PeopleProvider().propose(library.context())

    scores = {row.row.slug: row.score for row in rows}
    assert scores[f"people-{deep}"] == pytest.approx(PEOPLE_SCORE_CEILING)
    assert scores[f"people-{shallow}"] == pytest.approx(PEOPLE_SCORE_CEILING * 0.5)


async def test_the_underived_warning_is_said_once_per_process_not_once_per_propose() -> None:
    """The people half of CLAUDE.md's "a per-process fact logged in a per-pass
    function" finding. `test_rows_franchise.py`'s twin carries the arithmetic.

    Three passes on **one** provider instance, because the providers are
    module-level singletons and a single pass cannot tell "once" from "once per
    pass".
    """
    library = Library()
    for index in range(6):
        await _watched_film(library, f"Watched Film {index}", at=index + 1)

    messages: list[str] = []
    from loguru import logger

    sink = logger.add(messages.append, level="WARNING", format="{message}")
    provider = PeopleProvider()
    try:
        for _ in range(3):
            assert await provider.propose(library.context()) == []
    finally:
        logger.remove(sink)

    assert len([m for m in messages if "usher derive" in m]) == 1
