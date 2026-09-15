"""The security boundary: what a model said, turned into what a household may see."""

import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from usher.domain.curation import SLUG_PREFIX, CuratedRow
from usher.domain.ids import new_id

#: The keys the completion is read through. Constants rather than literals so
#: the prompt and the JSON schema are written against the same four strings
#: this reads -- a schema that says `ids` and a validator that reads `item_ids`
#: drops 100% of a correct answer. PRD 06 step 2's own spelling.
ROWS_KEY = "rows"
TITLE_KEY = "title"
REASON_KEY = "reason"
ITEM_IDS_KEY = "item_ids"

#: The floor a row has to clear, restating `SeasonalProvider`'s and
#: `RediscoverProvider`'s rather than inventing a second number: *"an empty or
#: two-card row is worse than none"*.
DEFAULT_MIN_CARDS = 5

#: Inclusive bounds.
MAX_TITLE_CHARS = 200
MAX_REASON_CHARS = 1000


class DropReason(StrEnum):
    """`usher.curation.dropped`'s `reason` label.

    Closed, because a metric dimension built from free-form strings is a
    cardinality footgun -- the same argument `LLMPurpose` makes one module over.
    """

    NOT_IN_POOL = "not_in_pool"
    UNPARSEABLE = "unparseable"
    DUPLICATE = "duplicate"
    ROW_UNUSABLE = "row_unusable"
    ROW_TOO_SHORT = "row_too_short"


@dataclass(frozen=True, slots=True)
class CurationKept:
    """A generation that produced something.

    `rows` is never empty -- see `__post_init__`, and `CurationRejected` for the
    other half of why a caller cannot mistake zero rows for a success.
    """

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

    There is no `rows` attribute, deliberately: an empty tuple would be a value
    a caller could iterate without noticing, leaving "no rows" and "nothing to
    say" indistinguishable. `error` is non-empty, which is what
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
    # `str(index)` once, here, rather than per candidate. Building it from the
    # map the caller owns is what makes the bound a property of what was *sent*
    # rather than of what exists.
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

    # The model's ordering is the product, so `position` indexes the list the model
    # returned and a discarded row leaves a gap rather than renumbering the ones after
    # it.
    width = len(str(len(raw_rows)))
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
    """One row, or `None` if it is discarded.

    Discarded whole, never padded from the pool: padding would be a fabricated
    recommendation wearing a model's reason string.
    """
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
        # Two different failures, spelled as one condition: a non-string reason is
        # a schema violation, and an over-long one is not prose a shelf can carry.
        # Both discard the row.
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

    cards = _cards(raw_ids, by_handle=by_handle, dropped=dropped)
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


def _cards(
    raw_ids: list[Any],
    *,
    by_handle: Mapping[str, uuid.UUID],
    dropped: Counter[DropReason],
) -> list[uuid.UUID]:
    """The candidates one row cites, in the order the model cited them.

    Three of the five drop reasons are counted here and nowhere else, and all
    three count *cards* -- which is why this is separable from `_row`, whose own
    two reasons count rows. A shortened list is a legitimate answer: whether
    what survives is enough is `_row`'s decision, not this one's.
    """
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
    return cards


def _handle(value: Any) -> str | None:
    """`str(value).strip()` for the two JSON types that carry a handle, else `None`."""
    if isinstance(value, bool):
        # First, because `isinstance(True, int)` is `True`. A bool where a
        # handle was asked for is a shape failure, not the index `1`.
        return None
    if isinstance(value, int):
        # Deleting this drops every id a model returned as a JSON number, which
        # is every id.
        return str(value).strip()
    if isinstance(value, str):
        return value.strip() or None
    return None


def _prose(value: Any, *, limit: int) -> str | None:
    """The stripped string, or `None` if this row cannot be shown with it.

    Never coerced -- a non-string here is a schema failure, not a value to
    render.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > limit:
        return None
    return stripped


def _tally(dropped: Counter[DropReason]) -> Mapping[DropReason, int]:
    """Every reason, zeros included.

    A reason absent from the map is indistinguishable from a reason nobody
    counts.
    """
    return MappingProxyType({reason: dropped[reason] for reason in DropReason})


def _summary(dropped: Counter[DropReason]) -> str:
    """The non-zero counts, for `llm_calls.error`.

    Numbers and label names only; nothing the model wrote.
    """
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
