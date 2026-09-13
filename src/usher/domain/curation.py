"""What a generation produced, and what it cost."""

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from usher.domain.base import DomainModel

# : `curated-01`, `curated-02`, … The composer breaks score ties on `slug` and : every
# curated row carries the same base score : (`services.rows.curated.CURATED_SCORE`), so
# this string is what carries the : model's row ordering onto the screen.
SLUG_PREFIX = "curated"


class LLMPurpose(StrEnum):
    """`llm_calls.purpose` (PRD 10).

    a closed vocabulary so it stays a usable telemetry dimension instead of a
    cardinality footgun.

    PRD 10's own text marks this open-ended ("curation | query_expansion | ..."): a new
    call site adds a member here and to PRD 10 in the same change, never a free-form
    string.

    **Declared here in M8 rather than in `ports/llm.py`, where M1 put it, and
    the move is forced by the layering rather than chosen.** `LLMCall` below
    is a domain model and `usher.domain` may not import `usher.ports` -- so
    the enum had to be in the lower layer for the column to be typed at all.
    That is the right place on the merits too: this is a column in *this*
    project's own table, not a parameter of somebody else's API, and the
    adapter is careful never to send it anywhere. `usher.ports.llm`
    re-exports it, so every existing import still resolves and
    `test_ports.py`'s vocabulary pin is unmoved.
    """

    CURATION = "curation"
    QUERY_EXPANSION = "query_expansion"


class CuratedRow(DomainModel):
    """One shelf an LLM proposed, after validation, as stored."""

    id: uuid.UUID
    user_id: uuid.UUID
    # `curated-1`, `curated-2`, … Minted from the row's position rather than slugified
    # from the model's title, for three reasons: a title is arbitrary text and would
    # need escaping to be a cache key; two generations could produce the same title and
    # collide in `RowCache`, whose key is `(user_id, slug)`; and the composer breaks
    # score ties on `slug`, so a positional slug makes the model's own ordering the
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    # `None` is reachable here and is not reachable from any M7 provider --
    # all nine return a sentence -- so `LLMRow`, which hands this field through
    # unchanged, is the first thing in `src/` to put a null `reason` on the
    # wire. A model that returns an empty reason should produce a row with no
    # subtitle rather than a row with an empty one.
    reason: str | None = None
    card_title_ids: tuple[uuid.UUID, ...] = Field(min_length=1)
    # The model's own ordering of the rows within one generation. `ge=0`
    # rather than `ge=1` because it indexes the list the model returned.
    position: int = Field(ge=0)
    model_name: str = Field(min_length=1)
    generation_id: uuid.UUID
    generated_at: AwareDatetime


class LLMCall(DomainModel):
    """One *attempted* completion, whether or not it worked."""

    id: uuid.UUID
    at: AwareDatetime
    model: str = Field(min_length=1)
    purpose: LLMPurpose
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    # `Decimal`, never `float`, and pinned on the port as well
    # (`test_llm_usage_cost_is_decimal_not_float`). This number is summed over
    # a month, and $3/Mtok on 1,200 tokens is exactly 0.0036 -- a value binary
    # floating point cannot represent.
    cost_usd: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    ok: bool
    # Present exactly when `ok` is false, enforced below. `str | None` rather
    # than a code: an operator reads this, and the vocabulary of things that
    # can go wrong here spans an upstream, a parser and a validator.
    error: str | None = None
    # The generation this call belongs to, so PRD 10's "cost per curated row"
    # is a join rather than a correlation on timestamps. `None` for a purpose
    # that produces no rows at all -- query expansion is one.
    generation_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _ok_and_error_must_agree(self) -> Self:
        """A failed call with no error is a row an operator cannot act on.

        and a successful call carrying one reads as a failure in every `WHERE error IS
        NOT NULL` anybody will write.

        Enforced here rather than as a CHECK alone, because the model is what the
        service constructs and the CHECK would report it one layer too late.
        """
        if self.ok and self.error is not None:
            raise ValueError("a successful call carries no error")
        if not self.ok and not self.error:
            raise ValueError("a failed call must say what went wrong")
        return self


__all__ = ["SLUG_PREFIX", "CuratedRow", "LLMCall", "LLMPurpose"]
