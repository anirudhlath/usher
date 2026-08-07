"""What a generation produced, and what it cost.

Two models with almost nothing in common, in one module because they are
written by one service in one transaction and because reading either without
the other is how a cost dashboard ends up reporting spend nobody can attribute
to an outcome ([PRD 10](../../../docs/prd/10-telemetry-and-dashboards.md)'s
dashboard 5).

**`CuratedRow` is the only table in this project whose contents no re-run
reproduces.** `title_neighbors` can be diffed against a fresh computation;
`search_document` has a case asserting the stored value equals a freshly
computed one. A curated row has no oracle and is not even deterministic at a
fixed temperature. Everything defensible about it therefore has to be true at
the moment it is *written* -- which is what
[ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md)'s
validator is, and why these models refuse the states that validator exists to
prevent rather than trusting it to have run.
"""

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from usher.domain.base import DomainModel


class LLMPurpose(StrEnum):
    """`llm_calls.purpose` (PRD 10) -- a closed vocabulary so it stays a
    usable telemetry dimension instead of a cardinality footgun. PRD 10's own
    text marks this open-ended ("curation | query_expansion | ..."): a new
    call site adds a member here and to PRD 10 in the same change, never a
    free-form string.

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
    """One shelf an LLM proposed, after validation, as stored.

    **`card_title_ids` is ordered and that order is the product.** A curated
    row *is* an ordering -- it is the only judgement the completion was bought
    for -- so nothing downstream may re-sort it by popularity, by year or by
    anything else. `LLMRow` hydrates in this order and a case pins it, because
    the hydration path is shared with nine providers that legitimately do
    sort.

    **`min_length=1`, so an empty curated row is not constructible.** This is
    the one place this project's usual rule reverses: a *source* row may
    legitimately build empty and be dropped by the composer, because "the
    household owns nothing in this genre" is a true and useful state. A stored
    curated row with no cards is not a state, it is a validator that ran and
    kept nothing, and persisting one would put a heading with no shelf under
    it on the screen. The row is discarded whole instead -- never padded from
    the pool, which would be a fabricated recommendation wearing a model's
    reason string.

    **`generation_id` is what makes a replacement atomic and a partial write
    visible.** Rows are written per user per generation and read back by the
    newest generation, so a crash between two inserts leaves a short screen
    that is legibly short rather than a mixture of two nights' output.

    **`model_name` is here for `title_embeddings.model_name`'s reason**
    (ADR-0020): it is what makes "these rows were written by a model we no
    longer run" a query rather than something inferred from a date. It is not
    used as an invalidation predicate today -- nothing recomputes curated rows
    on a model change, because regeneration is an operator's job either way --
    and it is recorded so that decision stays available.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    # `curated-1`, `curated-2`, … Minted from the row's position rather than
    # slugified from the model's title, for three reasons: a title is
    # arbitrary text and would need escaping to be a cache key; two
    # generations could produce the same title and collide in `RowCache`,
    # whose key is `(user_id, slug)`; and the composer breaks score ties on
    # `slug`, so a positional slug makes the model's own ordering the
    # tiebreak instead of an alphabetisation of its prose.
    #
    # **Zero-padded to the width of the generation** by
    # `services.curation_validate`, which is the only thing that mints one --
    # so ten rows are `curated-01` … `curated-10` and three are `curated-1` …
    # `curated-3`. Unpadded, the tiebreak above orders
    # `curated-1 < curated-10 < curated-2`, which alphabetises exactly the
    # judgement the completion was bought for. This is the same defect as M8's
    # `m8a` sorting after `m10a`, one subsystem over: an identifier minted by
    # counting and compared as a string sorts wrong at its first two-digit
    # value, so it is padded where it is minted.
    #
    # **The width is a property of the generation, so a curated slug is unique
    # within one generation and is not a stable name across them** -- nine rows
    # mint `curated-1` and ten mint `curated-01`, so the first shelf changes key
    # the night the model returns one more row. Two things follow, and the
    # second is the load-bearing one because it is a premise held somewhere
    # else:
    #
    # - `RowCache`'s `(user_id, slug)` entry for the old width is not
    #   overwritten, it is *orphaned* -- a guaranteed miss and a rebuild, which
    #   is the correct answer arrived at by accident, and the dead entry is
    #   reclaimed by its own TTL like every other.
    # - **It is only harmless because `CuratedRowRepository.replace_for_user`
    #   is delete-then-insert.** An upsert keyed on `(user_id, slug)` would
    #   leave a nine-row generation's `curated-1` … `curated-9` sitting beside
    #   a ten-row one's `curated-01` … `curated-10`, and the household would
    #   get nineteen shelves, nine of them last night's, with nothing in the
    #   schema refusing it. The write path's ordering is what keeps a slug from
    #   ever having to be stable, and Task 15's `CuratedProvider` reads rows
    #   under that guarantee without restating it.
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    # `None` is reachable here and is not reachable from any M7 provider --
    # all nine return a sentence. `test_api_home.py` records this as the first
    # plausible row with nothing to explain, and a model that returns an empty
    # reason should produce a row with no subtitle rather than a row with an
    # empty one.
    reason: str | None = None
    card_title_ids: tuple[uuid.UUID, ...] = Field(min_length=1)
    # The model's own ordering of the rows within one generation. `ge=0`
    # rather than `ge=1` because it indexes the list the model returned.
    position: int = Field(ge=0)
    model_name: str = Field(min_length=1)
    generation_id: uuid.UUID
    generated_at: AwareDatetime


class LLMCall(DomainModel):
    """One *attempted* completion, whether or not it worked.

    [PRD 10](../../../docs/prd/10-telemetry-and-dashboards.md)'s ten columns.
    **A cost ledger with only the successes in it understates spend by exactly
    the failures**, which are the calls an operator most wants to see, so
    `record()` is called on both paths and `ok` is the discriminator.

    **Per attempt rather than per completion**, and the difference is a row: a
    call that never reached the endpoint completed nothing and is still one,
    with zeroed tokens and the model this deployment asked for. What is *not* a
    row is a path that attempted nothing -- `CurationService` raises on an
    empty candidate pool before the client is touched, because no completion
    was attempted, none was billed, and a row saying otherwise is spend an
    operator has to explain away.

    **`ok` is not "the HTTP call returned 200".** It is "this generation
    produced something", and the two are allowed to disagree in exactly one
    direction: a call that answered perfectly and validated to zero rows is
    `ok = false` with a reason. That is not bookkeeping fussiness -- it is the
    only signal distinguishing a validator that ate the output from a model
    that had nothing to say, and those produce the identical empty screen.
    [ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md).

    **There is no `user_id`, deliberately and as specified.** PRD 10's column
    list has none: this is a spend ledger, and spend is attributed to an
    outcome by joining `curated_rows` on `generation_id` rather than by
    denormalising a user onto a cost row. The join is what dashboard 5's "cost
    per curated row" is.
    """

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
        """A failed call with no error is a row an operator cannot act on, and
        a successful call carrying one reads as a failure in every `WHERE error
        IS NOT NULL` anybody will write. Enforced here rather than as a CHECK
        alone, because the model is what the service constructs and the CHECK
        would report it one layer too late.

        **`model_validator(mode="after")` and deliberately not
        `model_post_init`, which is what this was.** They differ on exactly one
        input and it is the one the test suite needs:
        `model_construct` **skips a validator and runs a post-init hook**. Five
        existing cases across the repository build an otherwise-unconstructible
        row with `model_construct` precisely so the *database* CHECK is what
        rejects it, proving the constraint is real rather than trusting pydantic
        to have run first -- and the paragraph above promises such a CHECK on
        `llm_calls`. Under `model_post_init` that case is unwritable for this
        one table, because the model refuses to be built wrong even on purpose.
        `WatchState._exactly_one_of_title_or_episode` is the sibling with this
        same "two fields must agree" shape and it is spelled this way; the
        second, smaller reason is that a post-init hook raises a bare
        `ValueError` where every other model here raises `ValidationError`.
        """
        if self.ok and self.error is not None:
            raise ValueError("a successful call carries no error")
        if not self.ok and not self.error:
            raise ValueError("a failed call must say what went wrong")
        return self


__all__ = ["CuratedRow", "LLMCall", "LLMPurpose"]
