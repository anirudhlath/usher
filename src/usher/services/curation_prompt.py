"""The one string that crosses the wire, and every number rendered into it."""

import uuid
from collections.abc import Mapping, Sequence

from usher.domain.title import Title
from usher.ports.repository import RecentWatch
from usher.services.curation_validate import (
    ITEM_IDS_KEY,
    MAX_REASON_CHARS,
    REASON_KEY,
    ROWS_KEY,
    TITLE_KEY,
)

#: PRD 06's *"3-5 rows"*. Code rather than settings, per the module docstring.
#: Not a cap anything enforces -- the validator deliberately does not cap rows
#: either, because every card in a sixth row is still a title the household
#: could watch, and the product bound lives with `CuratedProvider`'s
#: `0-5 rows` budget.
MIN_ROWS = 3
MAX_ROWS = 5

#: What the prompt asks a heading to fit in. A *request*, not a bound --
#: `MAX_TITLE_CHARS` is the validator's 200 and a longer heading is dropped
#: there. This is the width a shelf looks right at, which is a product opinion
#: and belongs in the prompt with the rest of them.
MAX_HEADING_CHARS = 60

# The candidate line, and the reason it is one line.
_SEPARATOR = " - "

#: The example object in the prompt, built from the same four constants the
#: schema and the validator use.
_SHAPE = (
    f'{{"{ROWS_KEY}": [{{"{TITLE_KEY}": "...", "{REASON_KEY}": "...", '
    f'"{ITEM_IDS_KEY}": [4, 17, 2, 39, 8]}}]}}'
)

#: Introduces the history that follows it. **A branch, not framing prose** --
#: the other arm is `_COLD_START`, and which one renders is a fact about the
#: household.
_HISTORY_HEADING = "This household recently finished, most recent first:"

#: The arm taken by a household that has finished nothing, which `history` (in
#: `CurationService`) calls *"the normal state, not an edge case"*. Most
#: fixtures in this project seed no watch history, so this line is the one that
#: actually renders in nearly every test -- which is exactly why it needs a
#: case naming it rather than a case running through it.
_COLD_START = "This household has not finished anything yet."


def build_prompt(candidates: Sequence[Title], history: Sequence[str], *, min_cards: int) -> str:
    """The one string that crosses the wire.

    Ordered context first, instructions last: the rules are what the model is
    answering *with*, and they are the part that must survive a long candidate
    list.
    """
    # Implicit concatenation, so a source line under 100 characters is not also a
    # *rendered* line break in the middle of a sentence.
    lines = [
        "You are choosing what to put on the home screen of one household's "
        "film and television library. Some of the candidates below are in that "
        "library and some are not; a suggestion the household would have to go "
        "and find is welcome.",
        "",
    ]
    if history:
        lines.append(_HISTORY_HEADING)
        lines.extend(history)
    else:
        lines.append(_COLD_START)
    lines += [
        "",
        "Candidates. Choose only from this list, and name each one by the number in front of it:",
    ]
    lines += [
        f"{index}. {described(title)}{_genres(title)}"
        for index, title in enumerate(candidates, start=1)
    ]
    lines += ["", *instructions(len(candidates), min_cards=min_cards)]
    return "\n".join(lines)


def instructions(pool_size: int, *, min_cards: int) -> list[str]:
    """The rules, with the three numbers that have to agree with something else
    rendered rather than written: `pool_size` is the bound the validator
    checks, `min_cards` is the floor it enforces, and `MAX_REASON_CHARS` is the
    length it discards a whole row over."""
    return [
        "Answer with JSON in exactly this shape and nothing else:",
        _SHAPE,
        "",
        f"- Return between {MIN_ROWS} and {MAX_ROWS} rows.",
        f'- "{ITEM_IDS_KEY}": at least {min_cards} candidate numbers, '
        f"each between 1 and {pool_size}. Numbers only -- never a name, "
        "never a year, never a number outside that range, and never the "
        "same number twice in one row.",
        f'- "{TITLE_KEY}": a short shelf heading, at most '
        f"{MAX_HEADING_CHARS} characters. No spoilers.",
        # The number, not only the word "one".
        f'- "{REASON_KEY}": one sentence saying what these have in common, '
        f"at most {MAX_REASON_CHARS} characters.",
        "- Group by something a person would recognise -- a mood, a period, "
        "a theme, a filmmaker -- rather than by one genre, and never by how "
        "popular something is.",
        "- Do not use the same candidate in more than one row.",
    ]


def history_lines(recent: Sequence[RecentWatch], catalog: Mapping[uuid.UUID, Title]) -> list[str]:
    """The household's recent viewing as numbered prompt lines."""
    lines: list[str] = []
    for entry in recent:
        title = catalog.get(entry.title_id)
        if title is None:
            continue
        lines.append(f"{len(lines) + 1}. {described(title)}{_engagement(entry)}")
    return lines


def described(title: Title) -> str:
    """`Name (Year)`, on one line. See `_SEPARATOR` for why the collapse
    matters."""
    year = f" ({title.year})" if title.year is not None else ""
    return f"{one_line(title.name)}{year}"


def _genres(title: Title) -> str:
    return f"{_SEPARATOR}{', '.join(title.genres)}" if title.genres else ""


def _engagement(entry: RecentWatch) -> str:
    """PRD 06's *"recent watch history with ratings"*, with the substitution
    this schema forces: there is no rating column and M7 declined to invent
    one, so the engagement signal `watch_states` actually carries is the
    rewatch. A single viewing says nothing extra and costs tokens to say."""
    return f", watched {entry.play_count} times" if entry.play_count >= 2 else ""


def one_line(value: str) -> str:
    """Every run of whitespace collapsed to one space."""
    return " ".join(value.split())


__all__ = [
    "MAX_HEADING_CHARS",
    "MAX_ROWS",
    "MIN_ROWS",
    "build_prompt",
    "described",
    "history_lines",
    "instructions",
    "one_line",
]
