"""The security boundary: what a model said, turned into what a household may
see.

**Everything else in M8 is plumbing; this module is the thing standing between
a completion and a screen.**
[ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md) is its
specification, and the three rules it binds are the three sections below.

## Rule 1 -- candidates are addressed by index, and the caller owns the map

`validate_curation` takes `handles: Mapping[int, uuid.UUID]` -- *the pool that
was offered*, not the pool that exists -- and does **no arithmetic on it at
all**. That is the whole of the bound:

- **The map is authoritative about which handles were sent**, so a sparse pool,
  a 1-based prompt (which is what ADR-0028 measured) and a 0-based one are all
  the same code. A `Sequence` parameter would have made the validator guess
  between `pool[i]` and `pool[i - 1]`, and an off-by-one there is a validator
  that silently returns the film next to the one the model chose.
- **`pool[-1]` is legal Python** and denotes the last candidate, so a
  list-backed implementation answers a hallucinated `-1` with a real title.
  There is no negative key in a handle map and no index to wrap.
- A hallucinated identifier is therefore **unrepresentable** rather than
  rejected -- which is the difference between this scheme and a UUID handle
  (well-formed, denotes nothing) or an IMDb one (well-formed, may denote a real
  film the household does not own).

## Rule 2 -- coerce before comparing, and stop there

Measured 2026-08-06 against a local vLLM under `response_format:
{"type": "json_object"}`: the model returned **the correct identifiers with the
wrong JSON type**, `json types seen = {'int': 108}`. `id in set[str]` -- the
obvious spelling -- dropped **108 of 108**; `str(id).strip() in set[str]`
dropped **0 of 108**. Not one id was invented.

What that ships as is the reason rule 3 exists: a generation that called the
model, got a good answer, wrote an `llm_calls` row reading `ok = true` with real
tokens and a real cost, and left the household with no rows -- byte-for-byte
the state of a household whose model had nothing to say, because PRD 08's
degradation table reads *"previous curated rows persist"*.

**So `_handle` accepts exactly two JSON types and refuses the rest**:

- `11` -> `"11"`. The finding above, and the only reason this module exists.
- `" 11 "` -> `"11"`. Models pad.
- `True` -> **refused.** `isinstance(True, int)` is `True` in Python, so an
  `int` branch written without this refusal coerces it to `"True"` -- and a
  bool where a handle was asked for is a shape error, not the index `1`.
- `11.0` -> **refused.** `str(11.0)` is `"11.0"`, which is not `"11"`, and
  closing that gap means `int()` -- but `int(11.5)` is also 11, so a rule that
  accepts the first has to invent an answer for the second.
- `None`, `[]`, `{}` -> **refused.** `str(None)` is `"None"`: a perfectly good
  string that denotes nothing. Refused by *type*, so the count says shape.
- `"04"` -> `"04"`, which is `not_in_pool` rather than `unparseable`. It is a
  well-formed handle naming nothing that was sent, and closing *that* gap would
  again mean the arithmetic this module refuses.

**And coercion is for handles only.** A handle's meaning survives its type --
index 11 is index 11 however it arrives -- and prose's does not. `str(11)` is a
heading that says nothing and `str({"a": 1})` puts this project's own data
structures on a television, so a non-string `title` or `reason` makes the row
unusable instead. Over-coercing is how a validator starts inventing.

## Rule 3 -- zero rows is a failure, not an empty success

The return type is a union of two dataclasses rather than one with a `rows`
field that can be empty, and the asymmetry is the point:

- `CurationKept` **cannot be constructed with no rows.**
- `CurationRejected` **has no `rows` attribute at all**, so
  `for row in outcome.rows` on the failure branch is an error from `mypy`
  before it is an `AttributeError` at runtime.

A caller therefore cannot reach an empty sequence by accident; the only way to
zero rows is to name the failure, and `CurationRejected.error` is what Task 12
writes into `llm_calls.error`.

## The drop vocabulary, and why it is five and not two

ADR-0028 named two, `not_in_pool` and `unparseable`, *"because those two
produce the same empty screen and have opposite fixes"*. That test -- **a
different operator story with a different next step** -- is what the other
three pass:

- **`not_in_pool`** counts a **card**: a well-formed handle naming nothing that
  was sent. *The model is inventing* -- look at the prompt, the temperature,
  the pool.
- **`unparseable`** counts a **card**: a value that could not be a handle at
  all. *The shape is wrong* -- look at `response_format` and the schema. This
  and the one above produce the identical empty screen and have opposite fixes,
  which is ADR-0028's own argument for splitting them.
- **`duplicate`** counts a **card**: a candidate this row already used. *The
  model repeats itself* -- prompt or temperature, and nothing at all is wrong
  with the pool. Folded into either of the two above it would report a fix
  that is not the fix.
- **`row_unusable`** counts a **row**: not an object, no title, a non-string
  title or reason, or prose past the bound. *The row's shape is wrong.*
- **`row_too_short`** counts a **row**: fewer than `min_cards` cards survived.
  *The pool could not answer the question* -- or the cards are being eaten,
  and the three card counts beside this one are what tell those apart. This is
  the second-order effect of the other three and is the number an operator
  reads first.

Two of them count **rows** and three count **cards**, which is why the row ones
carry the prefix: summing across the whole map is meaningless and the names say
so. The map always carries **all five keys**, zeros included -- a reason absent
from a tally is indistinguishable from a reason nobody counts, which is this
module's own subject one level up.

`row_unusable` deliberately folds "unreadable" and "too long" together. They
have the same next step (the row's shape is wrong, fix the schema or the
prompt), and a metric dimension earns members by the fixes they distinguish,
not by the branches that produce them.

## Two things this module deliberately does not do

- **It does not cap the number of rows.** `USHER_CURATION_MAX_ROWS` is a
  product bound, not a safety one -- every card in a hundredth row is still a
  title the household could watch -- and PRD 06 already gives
  `CuratedProvider` a `0-5 rows` budget, which is where a cap belongs. The
  slug's width adapts instead, so the model's ordering survives however many
  rows arrive.
- **It does not sanitise prose.** There is nothing downstream that interprets
  `title` or `reason`: they are `Text` columns and a heading. The defence is a
  **bound** (`MAX_TITLE_CHARS`, `MAX_REASON_CHARS`), enforced here, and a row
  that exceeds it is discarded whole rather than truncated -- a cut heading is
  not what the model said, and a validator that quietly rewrites prose is one
  nobody can reason about. The bounds are chosen, not measured: the shipped
  providers' headings run ~10-40 characters (`More like <name>`) and their
  subtitles ~30-90, so these are an order of magnitude of headroom, and what
  matters is that they are finite.
"""

import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from usher.domain.curation import CuratedRow
from usher.domain.ids import new_id

#: The keys the completion is read through. Constants rather than literals so
#: Task 12's prompt and JSON schema are written against the same four strings
#: this reads -- a schema that says `ids` and a validator that reads `item_ids`
#: is a generation that drops 100% of a correct answer, which is the failure
#: this whole module exists to make impossible. PRD 06 step 2's own spelling.
ROWS_KEY = "rows"
TITLE_KEY = "title"
REASON_KEY = "reason"
ITEM_IDS_KEY = "item_ids"

#: `curated-01`, `curated-02`, … The composer breaks score ties on `slug` and
#: every curated row carries the same base score, so this string is what
#: carries the model's row ordering onto the screen. Zero-padded to the width
#: of the generation for the reason this milestone already learned once from
#: `m8a` sorting *after* `m10a`: `sorted(["curated-10", "curated-2"])` puts the
#: tenth row first.
SLUG_PREFIX = "curated"

#: The floor a row has to clear, restating `SeasonalProvider`'s and
#: `RediscoverProvider`'s rather than inventing a second number: *"an empty or
#: two-card row is worse than none"*. A parameter on `validate_curation`, so
#: `USHER_CURATION_MIN_CARDS` can override it without this module reading
#: settings.
DEFAULT_MIN_CARDS = 5

#: See the module docstring's last paragraph. Inclusive bounds.
MAX_TITLE_CHARS = 200
MAX_REASON_CHARS = 1000


class DropReason(StrEnum):
    """`usher.curation.dropped`'s `reason` label. Closed, because a metric
    dimension built from free-form strings is a cardinality footgun -- the same
    argument `LLMPurpose` makes one module over. The table in this module's
    docstring is why each member earns its place, and what unit each counts.
    """

    NOT_IN_POOL = "not_in_pool"
    UNPARSEABLE = "unparseable"
    DUPLICATE = "duplicate"
    ROW_UNUSABLE = "row_unusable"
    ROW_TOO_SHORT = "row_too_short"


@dataclass(frozen=True, slots=True)
class CurationKept:
    """A generation that produced something. **`rows` is never empty** -- see
    `__post_init__`, and `CurationRejected` for the other half of why a caller
    cannot mistake zero rows for a success."""

    rows: tuple[CuratedRow, ...]
    dropped: Mapping[DropReason, int]

    def __post_init__(self) -> None:
        if not self.rows:
            raise ValueError(
                "a generation that kept zero rows is a failure, not an empty success; "
                "build a CurationRejected"
            )


@dataclass(frozen=True, slots=True)
class CurationRejected:
    """A generation that produced nothing usable.

    **There is no `rows` attribute, deliberately.** An empty tuple here would
    be a value a caller could iterate without noticing, and the whole reason
    rule 3 exists is that "no rows" and "nothing to say" are otherwise
    indistinguishable. `error` is non-empty and is what
    `LLMCall._ok_and_error_must_agree` and `ck_llm_calls_ok_error_agree` both
    demand of a failed call.
    """

    error: str
    dropped: Mapping[DropReason, int]

    def __post_init__(self) -> None:
        if not self.error:
            raise ValueError("a rejected generation must say what went wrong")


CurationOutcome = CurationKept | CurationRejected


def validate_curation(
    payload: Mapping[str, Any],
    *,
    handles: Mapping[int, uuid.UUID],
    user_id: uuid.UUID,
    generation_id: uuid.UUID,
    model_name: str,
    generated_at: datetime,
    min_cards: int = DEFAULT_MIN_CARDS,
) -> CurationOutcome:
    """Turn one parsed completion into rows this household may be shown.

    Pure: a dict in, a map in, rows out. No port, no session, no clock --
    `generated_at` is the caller's, because a validator that read one would be
    a validator that could not be replayed.
    """
    dropped: Counter[DropReason] = Counter()
    # `str(index)` once, here, rather than per candidate: this is the
    # `set[str]` ADR-0028's comparison table is written against, and building
    # it from the map the caller owns is what makes the bound a property of
    # what was *sent* rather than of what exists.
    by_handle = {str(index): title_id for index, title_id in handles.items()}

    raw_rows = payload.get(ROWS_KEY)
    # `list`, not `Sequence`: a `str` is a `Sequence`, so `{"rows": "11"}`
    # under the looser check validates two rows out of a scalar.
    if not isinstance(raw_rows, list):
        return CurationRejected(
            # The type name only. PRD 08: a rejected request never echoes the
            # body it rejected, and this body is a completion written over the
            # household's own watch history.
            error=f"the response carries no {ROWS_KEY!r} list ({type(raw_rows).__name__})",
            dropped=_tally(dropped),
        )

    # The model's ordering is the product, so `position` indexes the list the
    # model returned and a discarded row leaves a gap rather than renumbering
    # the ones after it.
    width = len(str(max(len(raw_rows), 1)))
    kept: list[CuratedRow] = []
    for position, entry in enumerate(raw_rows):
        row = _row(
            entry,
            by_handle=by_handle,
            dropped=dropped,
            min_cards=min_cards,
            user_id=user_id,
            generation_id=generation_id,
            model_name=model_name,
            generated_at=generated_at,
            position=position,
            width=width,
        )
        if row is not None:
            kept.append(row)

    if not kept:
        return CurationRejected(
            error=(
                f"no row survived validation of {len(raw_rows)} returned "
                f"({_summary(dropped) or 'nothing dropped'})"
            ),
            dropped=_tally(dropped),
        )
    return CurationKept(rows=tuple(kept), dropped=_tally(dropped))


def _row(
    entry: Any,
    *,
    by_handle: Mapping[str, uuid.UUID],
    dropped: Counter[DropReason],
    min_cards: int,
    user_id: uuid.UUID,
    generation_id: uuid.UUID,
    model_name: str,
    generated_at: datetime,
    position: int,
    width: int,
) -> CuratedRow | None:
    """One row, or `None` if it is discarded -- **whole, and never padded from
    the pool**, which would be a fabricated recommendation wearing a model's
    reason string (ADR-0014, ADR-0028)."""
    if not isinstance(entry, Mapping):
        dropped[DropReason.ROW_UNUSABLE] += 1
        return None

    title = _prose(entry.get(TITLE_KEY), limit=MAX_TITLE_CHARS)
    if title is None:
        dropped[DropReason.ROW_UNUSABLE] += 1
        return None

    raw_reason = entry.get(REASON_KEY)
    reason: str | None = None
    if raw_reason is not None:
        # A blank reason is the schema's own optionality answered badly and
        # becomes no subtitle; a *non-string* one is a schema violation, and
        # blanking it silently would be the same class of defect this module
        # exists for -- a violation that looks exactly like having nothing to
        # say.
        if not isinstance(raw_reason, str) or len(raw_reason.strip()) > MAX_REASON_CHARS:
            dropped[DropReason.ROW_UNUSABLE] += 1
            return None
        reason = raw_reason.strip() or None

    raw_ids = entry.get(ITEM_IDS_KEY)
    if not isinstance(raw_ids, list):
        # `list` for the reason above: `{"item_ids": "114"}` under a `Sequence`
        # check becomes handles `1`, `1`, `4`, one of which may be real.
        dropped[DropReason.ROW_UNUSABLE] += 1
        return None

    cards: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw in raw_ids:
        handle = _handle(raw)
        if handle is None:
            dropped[DropReason.UNPARSEABLE] += 1
            continue
        title_id = by_handle.get(handle)
        if title_id is None:
            dropped[DropReason.NOT_IN_POOL] += 1
            continue
        # On the resolved title rather than on the handle string: two handles
        # naming one title is not a pool `CandidatePoolService` builds today,
        # and a validator that relies on that is trusting its caller for a
        # property the screen depends on.
        if title_id in seen:
            dropped[DropReason.DUPLICATE] += 1
            continue
        seen.add(title_id)
        cards.append(title_id)

    if len(cards) < min_cards:
        dropped[DropReason.ROW_TOO_SHORT] += 1
        return None

    return CuratedRow(
        id=new_id(),
        user_id=user_id,
        slug=f"{SLUG_PREFIX}-{position + 1:0{width}d}",
        title=title,
        reason=reason,
        card_title_ids=tuple(cards),
        position=position,
        model_name=model_name,
        generation_id=generation_id,
        generated_at=generated_at,
    )


def _handle(value: Any) -> str | None:
    """`str(value).strip()` for the two JSON types that can carry a handle, and
    `None` for everything else. The module docstring's table is the argument."""
    if isinstance(value, bool):
        # First, because `isinstance(True, int)` is `True`. A bool where a
        # handle was asked for is a shape failure, not the index `1`.
        return None
    if isinstance(value, int):
        # **The 108/108 line.** Deleting it drops every id a provider returned
        # as a JSON number -- which is every id, on the arm that was measured.
        return str(value).strip()
    if isinstance(value, str):
        return value.strip() or None
    return None


def _prose(value: Any, *, limit: int) -> str | None:
    """The stripped string, or `None` if this is not prose this row can be
    shown with. Never coerced: see the module docstring."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > limit:
        return None
    return stripped


def _tally(dropped: Counter[DropReason]) -> Mapping[DropReason, int]:
    """Every reason, zeros included -- a reason absent from the map is
    indistinguishable from a reason nobody counts."""
    return {reason: dropped[reason] for reason in DropReason}


def _summary(dropped: Counter[DropReason]) -> str:
    """The non-zero counts, for `llm_calls.error`. Numbers and label names
    only; nothing the model wrote."""
    return ", ".join(
        f"{reason.value}={dropped[reason]}" for reason in DropReason if dropped[reason]
    )


__all__ = [
    "DEFAULT_MIN_CARDS",
    "ITEM_IDS_KEY",
    "MAX_REASON_CHARS",
    "MAX_TITLE_CHARS",
    "REASON_KEY",
    "ROWS_KEY",
    "SLUG_PREFIX",
    "TITLE_KEY",
    "CurationKept",
    "CurationOutcome",
    "CurationRejected",
    "DropReason",
    "validate_curation",
]
