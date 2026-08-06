"""What a generation produced and what it cost, and the states neither may reach.

These two models are the only place in this project where the *write* is the
whole guarantee. `title_neighbors` can be diffed against a fresh computation and
`search_document` has a case asserting the stored value equals a freshly
computed one; a curated row has no oracle and is not deterministic even at a
fixed temperature. So every case below is about a state that must be
unconstructible rather than about a value that must be recomputable.

Three of them assert on the *absence* of something -- `LLMCall` has no
`user_id`, `CuratedRow` has no derivation of `slug` from `title`, `reason` has
no coercion of `""` to `None`. Each absence is a five-line diff somebody would
write while tidying, and each has a specific failure on the other side of it:
a denormalised spend column that dashboard 5's join already answers, two
generations colliding on one `RowCache` key, and a subtitle that cannot tell
"nothing to say" from "said nothing".

**`LLMPurpose`'s two-import identity case is the load-bearing one for the move**
that put the enum in `usher.domain`. `lint-imports`' `hexagonal layering`
contract already refuses `usher.domain -> usher.ports`, so the *layering* needs
no case here (verified by planting the import: 6 kept, 1 broken). What no
contract can see is the other tidy-up -- re-declaring the enum in `ports/llm.py`
instead of re-exporting it, which keeps every import resolving, keeps every
contract kept, and gives the codebase two vocabularies that compare unequal.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from usher.domain.curation import CuratedRow, LLMCall, LLMPurpose

# Chosen so that insertion order is *not* sorted order, and the case that cares
# asserts that premise before asserting the ordering. Minting these with
# `new_id()` would defeat the point: UUIDv7 is monotonic, so three ids minted in
# a row arrive already sorted and a validator that sorted the tuple would
# survive its own test -- the trap that cost M7 five untested orderings.
_CARD_A = uuid.UUID("f0000000-0000-7000-8000-00000000000a")
_CARD_B = uuid.UUID("10000000-0000-7000-8000-00000000000b")
_CARD_C = uuid.UUID("90000000-0000-7000-8000-00000000000c")


def _row(**overrides: object) -> CuratedRow:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "slug": "curated-1",
        "title": "Quiet films for a loud week",
        "reason": "Because you finished Perfect Days",
        "card_title_ids": (_CARD_A, _CARD_B, _CARD_C),
        "position": 0,
        "model_name": "gemma-4-26b-a4b",
        "generation_id": uuid.uuid4(),
        "generated_at": datetime.now(UTC),
    }
    return CuratedRow(**(fields | overrides))


def _call(**overrides: object) -> LLMCall:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "at": datetime.now(UTC),
        "model": "gemma-4-26b-a4b",
        "purpose": LLMPurpose.CURATION,
        "tokens_in": 2924,
        "tokens_out": 316,
        "cost_usd": Decimal("0.0036"),
        "latency_ms": 1995,
        "ok": True,
        "error": None,
        "generation_id": uuid.uuid4(),
    }
    return LLMCall(**(fields | overrides))


# --- CuratedRow ------------------------------------------------------------


def test_a_curated_row_carries_every_field_it_was_given() -> None:
    """Kills dropping any one of the ten fields.

    `extra="forbid"` is what turns a deleted field into a construction failure
    rather than a value silently discarded, so this reads as a smoke test and
    is really the refusal underneath every case below it. The read-backs are
    what catch the other half -- a field that exists, accepts a value, and
    answers with something else.
    """
    generation = uuid.uuid4()
    row = _row(generation_id=generation, position=3, slug="curated-4")
    assert isinstance(row.id, uuid.UUID)
    assert isinstance(row.user_id, uuid.UUID)
    assert row.slug == "curated-4"
    assert row.title == "Quiet films for a loud week"
    assert row.reason == "Because you finished Perfect Days"
    assert row.card_title_ids == (_CARD_A, _CARD_B, _CARD_C)
    assert row.position == 3
    assert row.model_name == "gemma-4-26b-a4b"
    assert row.generation_id == generation
    assert row.generated_at.tzinfo is not None


def test_a_curated_row_with_no_cards_is_not_constructible() -> None:
    """**The one place this project's usual rule reverses**, so it is the one
    place a reader will assume `BuiltRow`'s answer applies and it does not.

    Kills `card_title_ids: tuple[uuid.UUID, ...] = ()` -- which is exactly what
    `BuiltRow.cards` is, one module over, with a docstring arguing for it: an
    empty *source* row is a true state ("the household owns nothing in this
    genre") and `Row.empty()` is a real method because of it.

    A stored curated row with no cards is not a state. It is a validator that
    ran and kept nothing (ADR-0028: a generation that validates to zero rows is
    a failure), and persisting one puts a heading with no shelf under it on the
    screen. The row is discarded whole instead -- never padded from the pool,
    which would be a fabricated recommendation wearing a model's reason string.
    """
    with pytest.raises(ValidationError):
        _row(card_title_ids=())


def test_the_order_the_model_returned_is_the_product_and_survives_construction() -> None:
    """A curated row *is* an ordering -- it is the only judgement the completion
    was bought for.

    Kills a `field_validator` that sorts or dedupes `card_title_ids`, which is
    the shape a "tidy-up" takes on a tuple of ids, and kills
    `card_title_ids: frozenset[uuid.UUID]`.

    The premise assertion is not decoration and is the whole reason this case
    can fail: `new_id()` is a monotonic UUIDv7, so ids minted in a row arrive
    already sorted and a sorting validator would return them unchanged. That
    trap cost M7 five untested orderings. These three ids are fixed and
    deliberately out of order, and the first assertion is what fails loudly if
    someone later swaps them for freshly minted ones.
    """
    ids = (_CARD_A, _CARD_B, _CARD_C)
    assert list(ids) != sorted(ids), "the fixture is pre-sorted, so it cannot see a sort"
    assert _row(card_title_ids=ids).card_title_ids == ids


def test_card_ids_are_a_tuple_even_when_a_list_is_handed_in() -> None:
    """Kills `card_title_ids: list[uuid.UUID]`.

    Two consequences, and the second is the one that bites at runtime. A list
    field makes the model unhashable even though it is frozen -- `DomainModel`'s
    own docstring records that `Title` is the one model in this set that is not
    hashable and why -- and a curated row is a value a cache and a composer both
    hold. And a list is mutable by whoever received it: `row.card_title_ids.
    append(...)` on a row read out of `RowCache` edits the cached object for
    every later reader, silently, with no write anywhere.

    The list input is deliberate: pydantic coerces it, so the annotation is the
    only thing standing between a caller's list and a shared mutable.
    """
    row = _row(card_title_ids=[_CARD_A, _CARD_B])
    assert isinstance(row.card_title_ids, tuple)
    assert hash(row) is not None


def test_a_positional_slug_survives_two_rows_that_chose_the_same_title() -> None:
    """**Kills minting `slug` from the model's prose title.**

    Three separate failures sit behind that one-liner, and the case can only
    exhibit the second: a title is arbitrary text that would need escaping to be
    a cache key; two rows can carry the *same* title and would then collide in
    `RowCache`, whose key is `(user_id, slug)`; and the composer breaks score
    ties on `slug`, so a positional slug makes the model's own ordering the
    tiebreak rather than an alphabetisation of its prose.

    A model asked for five rows really does repeat itself, and the collision is
    silent -- the second row evicts the first from the cache and the screen is
    short by one with nothing logged. So the two rows below share a title on
    purpose.

    `is_required()` is the second half: it kills a computed or defaulted slug,
    which is the same derivation arriving as a `default_factory` instead of a
    validator.
    """
    assert CuratedRow.model_fields["slug"].is_required()
    first = _row(slug="curated-1", position=0, title="More like Perfect Days")
    second = _row(slug="curated-2", position=1, title="More like Perfect Days")
    assert first.title == second.title
    assert first.slug != second.slug


def test_position_starts_at_zero_and_refuses_a_negative() -> None:
    """`position` indexes the list the model returned, so the first row is 0.

    Kills `ge=1`, which is the reflex for anything called a position and which
    would refuse the first row of every generation; and kills dropping the bound
    entirely, which lets a `-1` reach an `ORDER BY position` and put a row above
    the one the model ranked first.
    """
    assert _row(position=0).position == 0
    with pytest.raises(ValidationError):
        _row(position=-1)


def test_a_row_with_nothing_to_explain_carries_none_and_not_an_empty_string() -> None:
    """Kills a validator that normalises `""` to `None`, and kills
    `reason: str = ""`.

    `None` is reachable here and is not reachable from any of M7's nine
    providers -- all nine return a sentence -- so this is the first plausible row
    with nothing to say, and PRD 06's reason is *spoken aloud* by Alfred rather
    than only displayed. The two values render differently and must stay
    distinguishable: `None` is "this row needs no subtitle", `""` is "the model
    returned a subtitle and it was empty", and the second is a prompt bug an
    operator can only see if it survives storage.
    """
    assert CuratedRow.model_fields["reason"].default is None
    assert _row(reason=None).reason is None
    assert _row(reason="").reason == ""


def test_an_empty_slug_title_or_model_name_is_rejected() -> None:
    """Kills dropping `min_length=1` from the three text columns.

    An empty `slug` is the sharpest of the three and has the shape of
    `Job.key`'s: `RowCache` is keyed `(user_id, slug)`, so every curated row of
    a household would collapse onto one entry -- five rows becoming one, with
    the survivor chosen by write order. An empty `title` is a heading with no
    text above a real shelf, and an empty `model_name` is what makes "these rows
    were written by a model we no longer run" unanswerable.
    """
    for blank in ("slug", "title", "model_name"):
        with pytest.raises(ValidationError):
            _row(**{blank: ""})


def test_a_generated_at_without_a_timezone_is_rejected() -> None:
    """Kills `generated_at: datetime`.

    Rows are read back by the *newest* generation, so this column is a
    comparison key rather than a display field. A naive datetime compares
    against an aware one with a `TypeError` and against another naive one in
    whatever the writer's local offset happened to be -- which is how a
    regeneration run either raises at read time or silently loses to the run it
    replaced.
    """
    with pytest.raises(ValidationError):
        _row(generated_at=datetime(2026, 8, 6, 12, 0, 0))


def test_a_curated_row_is_frozen_and_refuses_an_unknown_field() -> None:
    """Kills relaxing `frozen=True` on this model, and kills relaxing
    `extra="forbid"`.

    Both matter more here than on a model nothing caches: a curated row is
    handed to concurrent readers out of `RowCache`, and the field names are
    hand-mapped by a service reading a validated completion, where
    `card_ids=` for `card_title_ids=` would construct a row with no cards --
    except that the previous case makes that unconstructible, which is the two
    refusals working together.
    """
    row = _row()
    with pytest.raises(ValidationError):
        row.slug = "curated-2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _row(card_ids=(_CARD_A,))


def test_evolve_revalidates_a_curated_row_where_model_copy_does_not() -> None:
    """The sanctioned write path, pinned on the constraint most worth keeping.

    Kills `DomainModel.evolve` becoming `model_copy(update=...)`. The second
    half is the demonstration rather than the assertion: `model_copy` hands back
    a `CuratedRow` with no cards that `model_dump()` serializes without
    complaint, so the row the previous case made unconstructible is one method
    call away from being written.
    """
    row = _row()
    assert row.evolve(position=7).position == 7
    with pytest.raises(ValidationError):
        row.evolve(card_title_ids=())
    smuggled = row.model_copy(update={"card_title_ids": ()})
    assert smuggled.card_title_ids == ()
    assert smuggled.model_dump()["card_title_ids"] == ()


# --- LLMCall ---------------------------------------------------------------


def test_an_llm_call_carries_prd_10s_columns() -> None:
    """Kills dropping any one of them, by `extra="forbid"`.

    `generation_id` is the eleventh and is not in PRD 10's list: it is what
    makes dashboard 5's "cost per curated row" a join against `curated_rows`
    rather than a correlation on timestamps.
    """
    generation = uuid.uuid4()
    call = _call(generation_id=generation)
    assert isinstance(call.id, uuid.UUID)
    assert call.at.tzinfo is not None
    assert call.model == "gemma-4-26b-a4b"
    assert call.purpose is LLMPurpose.CURATION
    assert call.tokens_in == 2924
    assert call.tokens_out == 316
    assert call.cost_usd == Decimal("0.0036")
    assert call.latency_ms == 1995
    assert call.ok is True
    assert call.error is None
    assert call.generation_id == generation


def test_the_two_legal_states_of_the_ledger_are_both_constructible() -> None:
    """The control for the two refusals below.

    Without it, `model_post_init` raising unconditionally passes both of them --
    a guard that refuses everything is indistinguishable from a guard that
    refuses the right things, judged only by what it rejects.
    """
    assert _call(ok=True, error=None).error is None
    assert _call(ok=False, error="upstream returned 503").error == "upstream returned 503"


def test_a_successful_call_carries_no_error() -> None:
    """Kills inverting or deleting the first `model_post_init` clause.

    A success carrying an error string reads as a failure in every `WHERE error
    IS NOT NULL` anybody will write against this table, which is the first query
    an operator writes and the one PRD 10's failure panel is.
    """
    with pytest.raises(ValidationError):
        _call(ok=True, error="upstream returned 503")


def test_a_failed_call_must_say_what_went_wrong_and_an_empty_string_does_not() -> None:
    """Kills deleting the second `model_post_init` clause, and kills weakening
    it from `not self.error` to `self.error is None`.

    The second mutation is the one that survives a carelessly written case. A
    failed call whose error is `""` is a row an operator cannot act on in
    exactly the way a `None` is -- it renders as an empty cell rather than as a
    missing one -- and `""` is what a service reaches for when it has an
    exception it could not turn into a sentence. So both spellings of "no
    reason" are refused, and the case asserts both.
    """
    with pytest.raises(ValidationError):
        _call(ok=False, error=None)
    with pytest.raises(ValidationError):
        _call(ok=False, error="")


def test_a_call_that_answered_perfectly_and_kept_nothing_is_a_failure() -> None:
    """**ADR-0028 rule 3, which is why `ok` is not "the HTTP call returned
    200".**

    Kills any reading of `ok` as transport health -- a `model_post_init` clause
    tying `ok = false` to a zero cost or zero output tokens, which is the shape
    "a failed call cost nothing" takes when someone writes it down.

    This is the run that produced the rule: the model was asked over a pool that
    could not answer the question, returned the right identifiers with the wrong
    JSON type, and the obvious `id in set[str]` comparison scored 108 of 108
    out-of-pool. That generation had real token counts, a real cost and a real
    latency, and left the household with no curated rows. The ledger has to be
    able to say the call succeeded and the generation did not -- which means a
    failed row with 316 output tokens and a non-zero cost on it is legal and is
    the interesting case, not a contradiction.
    """
    call = _call(ok=False, error="validated to zero rows", tokens_out=316)
    assert call.ok is False
    assert call.tokens_out == 316
    assert call.cost_usd > 0
    assert call.latency_ms > 0


def test_evolve_re_runs_the_ok_error_agreement() -> None:
    """The path this invariant is actually reached by, and it is not
    construction.

    A generation records its call, then discovers the validator kept nothing,
    then has to say so. That is an `.evolve(ok=False, error=...)` on a row
    already built with `ok=True`, so an invariant checked only at `__init__`
    would be checked on the one path where it cannot fire.

    Kills `DomainModel.evolve` becoming `model_copy(update=...)`, and kills
    moving the agreement into an `__init__` override.
    """
    call = _call(ok=True, error=None)
    assert call.evolve(ok=False, error="validated to zero rows").ok is False
    with pytest.raises(ValidationError):
        call.evolve(ok=False)


def test_cost_is_a_decimal_because_a_month_of_these_is_summed() -> None:
    """Kills `cost_usd: float`.

    $3/Mtok on 1,200 tokens is exactly 0.0036, which binary floating point
    cannot represent -- so the assertion that has teeth is not the `isinstance`
    but the sum. A thousand calls at that price is exactly $3.60 and `float`
    answers 3.60000000000004 -- measured, not asserted -- which is a cost
    dashboard that disagrees with itself depending on how the rows were grouped,
    and a total that never equals the sum of its own monthly subtotals.

    `Decimal` is pinned on the port too (`test_llm_usage_cost_is_decimal_not_
    float`), and this is the end of that chain: `LLMUsage.cost_usd` is what gets
    written here.
    """
    call = _call(cost_usd=Decimal("0.0036"))
    assert isinstance(call.cost_usd, Decimal)
    assert sum((call.cost_usd for _ in range(1000)), Decimal(0)) == Decimal("3.60")


def test_negative_tokens_latency_or_cost_are_all_rejected() -> None:
    """Kills dropping `ge=0` from any one of the four numeric columns, which is
    why each is asserted separately rather than through one representative.

    None of the four has a negative reading. A negative token count or latency
    is a subtraction done in the wrong order against a clock or a counter, and a
    negative cost is a credit -- which would make `SUM(cost_usd)` under-report
    spend by however much of it was recorded backwards, on the one table whose
    entire purpose is to total correctly.
    """
    with pytest.raises(ValidationError):
        _call(tokens_in=-1)
    with pytest.raises(ValidationError):
        _call(tokens_out=-1)
    with pytest.raises(ValidationError):
        _call(latency_ms=-1)
    with pytest.raises(ValidationError):
        _call(cost_usd=Decimal("-0.01"))


def test_an_llm_call_has_no_user_id() -> None:
    """**Deliberate, and specified: PRD 10's column list has none.**

    Kills adding `user_id: uuid.UUID | None = None`, which is the five-line diff
    somebody writes after being asked "which user did this cost belong to". The
    answer is already available and is better: spend is attributed to an outcome
    by joining `curated_rows` on `generation_id`, and that join *is* dashboard
    5's "cost per curated row". A denormalised user on a cost row is a second
    source of truth that the query-expansion purpose -- which produces no rows
    at all -- cannot even fill.

    The second assertion is the load-bearing one: `extra="forbid"` is what makes
    the absence a runtime refusal rather than a field somebody passes anyway and
    has silently dropped.
    """
    assert "user_id" not in LLMCall.model_fields
    with pytest.raises(ValidationError):
        _call(user_id=uuid.uuid4())


def test_a_purpose_that_produces_no_rows_records_no_generation() -> None:
    """The asymmetry between the two models, and it is not an oversight in
    either direction.

    Kills making `LLMCall.generation_id` required -- query expansion is a real
    purpose that produces no curated rows, so a required column would have
    nothing honest to put in it and would get a zero UUID. And kills making
    `CuratedRow.generation_id` optional: a row that cannot name its generation
    cannot be replaced atomically, and a crash between two inserts would leave a
    mixture of two nights' output rather than a legibly short screen.
    """
    assert _call(purpose=LLMPurpose.QUERY_EXPANSION, generation_id=None).generation_id is None
    assert CuratedRow.model_fields["generation_id"].is_required()


def test_an_at_without_a_timezone_is_rejected() -> None:
    """Kills `at: datetime`.

    Every cost dashboard in PRD 10 buckets this column by day. A naive
    timestamp buckets in the writer's local offset, so a deployment that moves
    machines gets a ledger whose midnight moves with it -- and the error is
    invisible, because both halves still sum.
    """
    with pytest.raises(ValidationError):
        _call(at=datetime(2026, 8, 6, 12, 0, 0))


def test_an_llm_call_is_frozen_and_refuses_an_unknown_field() -> None:
    """Kills relaxing either half of `DomainModel`'s config on this model.

    `extra="forbid"` earns its keep here on the field pair this model exists to
    keep honest: `LLMCall(..., ok=False, err="...")` -- the abbreviation -- would
    otherwise construct a failed call with `error=None`, which is the exact row
    `model_post_init` refuses, arriving past it through a typo.
    """
    call = _call()
    with pytest.raises(ValidationError):
        call.ok = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _call(ok=False, err="upstream returned 503")


# --- LLMPurpose ------------------------------------------------------------


def test_the_purpose_vocabulary_is_closed_at_the_two_that_have_call_sites() -> None:
    """Kills adding a member without a call site.

    PRD 10's own text marks this open-ended -- `curation | query_expansion | …`
    -- and an ellipsis in a telemetry dimension is a cardinality footgun: a
    free-form string here makes every `GROUP BY purpose` panel grow a row per
    spelling. An exact set rather than a membership check, so a third member
    cannot arrive without this list moving and someone reading that rule.

    `QUERY_EXPANSION` is itself the member with no emitter until Task 20 adds
    one, which is the exception this vocabulary is allowed and the reason it is
    worth stating: it is declared because PRD 10 names it as a column value, not
    because something writes it today.
    """
    assert set(LLMPurpose) == {LLMPurpose.CURATION, LLMPurpose.QUERY_EXPANSION}
    assert {p.value for p in LLMPurpose} == {"curation", "query_expansion"}


def test_the_purpose_a_port_caller_imports_is_the_one_a_domain_model_types() -> None:
    """**What makes the move to `usher.domain` invisible to every caller, and
    exactly the property a tidy-up would break.**

    The enum moved because `LLMCall.purpose` has to be typed and
    `usher.domain` may not import `usher.ports`; `ports/llm.py` re-exports it so
    every existing `from usher.ports.llm import LLMPurpose` still resolves.

    Kills re-declaring the enum in `ports/llm.py` rather than re-exporting it.
    That mutation is invisible to everything else: both spellings import,
    `test_ports.py`'s vocabulary pin passes against either copy, mypy is happy,
    and `lint-imports` reports every contract kept -- because a duplicate
    definition is not an import. What breaks is at runtime and only under
    comparison: `LLMCall(purpose=ports.LLMPurpose.CURATION).purpose is domain.
    LLMPurpose.CURATION` is `False`, and the `enum_column` that Task 8 will
    store this through would then be writing one enum's values and reading them
    back as another's members.

    `is` rather than `==`: these are `StrEnum` members, so two independent
    declarations compare **equal** on value and would pass an `==` assertion
    while still being different objects with different identities.

    The layering half of this needs no case: `lint-imports`' `hexagonal
    layering` contract already refuses `usher.domain -> usher.ports`, verified
    by planting that import in `domain/curation.py` and watching it report
    6 kept, 1 broken.
    """
    from usher.ports.llm import LLMPurpose as PortLLMPurpose

    assert PortLLMPurpose is LLMPurpose
    assert PortLLMPurpose.CURATION is LLMPurpose.CURATION
    assert "LLMPurpose" in __import__("usher.ports.llm", fromlist=["__all__"]).__all__
