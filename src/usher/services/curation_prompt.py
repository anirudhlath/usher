"""The one string that crosses the wire, and every number rendered into it.

Pure functions, no port, no clock, no session -- `curation_validate`'s shape,
for `curation_validate`'s reason, arrived at from the other side of the call.
`CurationService` assembles the context (two reads and a candidate pool) and
this module turns it into text.

**Separated because a prompt is the one artefact this milestone produces whose
only real consumer is outside the process.** `.claude/rules/testing-
discipline.md` records what that costs: a sweep that walked `CurationService`'s
control flow caught every mutation damaging something a case read back through
a port -- a handle map, a ledger row, a span, a returned report -- and was
blind to sixteen live mutants in the prompt, because nothing observes a prompt
unless a case opts in by name. Sixteen, in a body that carries a household's
entire viewing history and 200 candidate titles and is the most expensive
single thing this project sends anywhere. A module of pure functions gives the
artefact a consumer *inside* the process: `test_services_curation_prompt.py`
calls these directly, with no household, no fakes, no pool service and no
scripted client between the input and the sentence.

## The numbers here are code, and three of them have to agree with something

PRD 08's *"row weights are code"* verbatim: the prompt text, the row budget and
the heading width are constants rather than settings. Three numbers are *not*
free to differ from a second definition and so are rendered rather than
written:

- **`min_cards`** -- a prompt asking for four cards under a validator demanding
  five drops every row and reports `row_too_short`, a generation that failed
  because two numbers in two files disagreed. One parameter, threaded from
  `CurationService` into both this module and `validate_curation`.
- **`pool_size`** -- ADR-0028's bound, which is a property of what was *sent*.
  Written down three times (the handle map, the JSON schema, this sentence) and
  this is the only one the model reads.
- **`MAX_REASON_CHARS`** -- the validator's own, imported rather than restated,
  because a reason over it does not get truncated, it takes **the whole row**
  with it as `row_unusable`.

`MAX_HEADING_CHARS` is the exception that proves the rule: it is a *request*
and deliberately stricter than the bound `validate_curation` enforces, so
nothing else in this repository states it and the prompt is the only place it
can be observed at all.

## What may not be rendered

**No identifier.** Candidates are addressed by a 1-based integer index and the
map back is held by the service -- ADR-0028's rule 1, and the reason a
hallucinated handle is *unrepresentable* rather than merely rejected. Measured:
a UUID handle costs 3.1x the prompt tokens, 3.0x the output tokens and 3.2x the
latency, and is the least accurate of the three spellings.

**Nothing third-party on a line of its own.** `titles.name` arrives from a
media server or from TMDb, so every rendered name goes through `one_line`.
See `_SEPARATOR`.

`one_line` is public and `query_expansion` imports it, which is the one thing
this module exports to a sibling service rather than to its caller: a viewer's
typed query is third-party text going into a prompt for exactly the reason a
title is, and the two prompts had a copy each. See the function.
"""

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

# The candidate line, and the reason it is one line. `titles.name` is
# third-party text -- it arrives from a media server or from TMDb -- and a
# newline inside one would render a second numbered line into the candidate
# list, i.e. a handle naming a film the household does not own, which is the
# one thing the pool being the contract is *about*. `" ".join(value.split())`
# collapses every kind of whitespace, not just `\n`.
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
    # Implicit concatenation, so a source line under 100 characters is not
    # also a *rendered* line break in the middle of a sentence.
    lines = [
        "You are choosing what to put on the home screen of one household's "
        "own film and television library.",
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
        # The number, not only the word "one". `MAX_HEADING_CHARS` is rendered
        # for the strictly *weaker* case -- the validator's own title limit is
        # 200 and the prompt asks for 60, so a long heading is a cosmetic
        # problem -- while this is the field `validate_curation` drops the
        # entire row over as `row_unusable`. Imported from the validator rather
        # than restated, because a second copy is the `min_cards` failure one
        # field across.
        f'- "{REASON_KEY}": one sentence saying what these have in common, '
        f"at most {MAX_REASON_CHARS} characters.",
        "- Group by something a person would recognise -- a mood, a period, "
        "a theme, a filmmaker -- rather than by one genre, and never by how "
        "popular something is.",
        "- Do not use the same candidate in more than one row.",
    ]


def history_lines(recent: Sequence[RecentWatch], catalog: Mapping[uuid.UUID, Title]) -> list[str]:
    """The household's recent viewing as numbered prompt lines.

    **The recency order is `recent`'s**, never the catalog's:
    `TitleRepository.list_by_ids` is one `IN (...)` and promises no order at
    all, so lines rendered by walking `catalog` describe the household in
    whatever order the store happened to hold -- which reads as a recency claim
    and is not one. That is why the map is a lookup here and never the thing
    iterated.

    1-based, like the candidate list beside it in the same prompt. A history
    numbered from 0 next to candidates numbered from 1 is the off-by-one
    ADR-0028's handle scheme is about, rendered twice into one body.

    A watch state whose title is missing from `catalog` is skipped rather than
    rendered as a blank line, and the numbering counts what was **rendered**,
    so the list has no gaps. Defensive rather than reachable today:
    `watch_states.title_id` is `ondelete="RESTRICT"` and the episode chain
    composes to the same, so a title with history behind it cannot be deleted
    -- but this function takes two arguments that a caller assembles from two
    separate reads, and nothing in its signature makes them agree.
    """
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
    """Every run of whitespace collapsed to one space.

    Not cosmetic: `titles.name` is third-party text and a newline in one would
    render a second numbered line into the candidate list -- a handle naming
    something that was never in the pool, which is the one property the pool
    being the contract exists to guarantee.

    `" ".join(value.split())` and nothing narrower. `str.splitlines()` breaks
    on `\\r` as well as `\\n`, so `replace("\\n", " ")` still leaves a `\\r\\n`
    input rendering two lines -- measured, and recorded in
    `.claude/rules/testing-discipline.md`, where the mutant survived a six-arm
    parametrisation that asserted only the negative *"no line starts with
    `999.`"*. `split()` collapses every whitespace spelling Python recognises
    (`\\r`, `\\t`, `U+2028`, runs of spaces); every narrower spelling collapses
    a proper subset.

    **Public, and shared with `query_expansion`, because this is
    prompt-injection defence for both prompts this project sends.** It shipped
    twice under two names (`_sanitise` was the other) with that measured
    argument spelled out in each -- so narrowing one copy would have left one
    prompt protected and one open, with every case still green: `titles.name`
    arrives from a media server or TMDb, a viewer's typed query arrives from a
    search box, and neither module's cases can see the other's collapse. One
    definition makes that edit unspellable, which is `llm_ledger`'s argument
    about *"a rule spelled twice"* arriving at a defence rather than at a rule.

    **The bar for sharing is the measurement, not the line count.** Both copies
    carried eight lines saying *why* this spelling and not a narrower one, and
    a justification worth writing twice is a decision worth holding once. A
    helper with nothing behind it -- the `_ms` clamp both spenders used to
    carry -- is a different call, and it moved for a different reason (it went
    to `llm_ledger` with the ledger rule it belongs to, not because two copies
    of `max(0, int(x * 1000))` could disagree).
    """
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
