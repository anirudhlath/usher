"""The row DTOs, and the three things about them that are load-bearing."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.rows import BuiltRow, DisplayHint, RowCard, RowFamily
from usher.domain.taste import Centroid


def _card(**overrides: object) -> RowCard:
    fields: dict[str, object] = {
        "title_id": uuid.uuid4(),
        "kind": TitleKind.MOVIE,
        "name": "Arrival",
        "year": 2016,
        "enrichment_state": EnrichmentState.ENRICHED,
        "owned": True,
        "position_seconds": 1800,
        "runtime_seconds": 6960,
        "played": False,
    }
    return RowCard(**(fields | overrides))


def test_a_card_carries_one_artwork_reference_and_defaults_to_none() -> None:
    """**Boundary call 3's other day.** M7 refused this field rather than shipping it null.

    on the grounds that there was no `Image` table, no `images` column and no
    `poster_path` on `titles`.
    """
    assert RowCard.model_fields["artwork"].annotation == uuid.UUID | None
    assert _card().artwork is None

    chosen = uuid.uuid4()
    assert _card(artwork=chosen).artwork == chosen

    with pytest.raises(ValidationError):
        _card(poster_path="/a.jpg")


def test_a_row_card_carries_the_raw_progress_pair_rather_than_a_fraction() -> None:
    """`watch_states.runtime_seconds` is nullable.

    so a progress *fraction* is best-effort dressed as arithmetic.

    Kills `progress: float`, which divides by `None` or by a COALESCE'd zero,
    and kills `progress: float | None`, which is correct but relocates the
    three-way branch into every client -- the dead-arm problem boundary call
    3 refuses for artwork, one field over.

    `position_seconds=1800, runtime_seconds=None` is two true facts: half an
    hour in, of an unknown total. No client is forced to render a bar it
    cannot size.
    """
    assert "progress" not in RowCard.model_fields
    assert {"position_seconds", "runtime_seconds"} <= set(RowCard.model_fields)
    assert RowCard.model_fields["runtime_seconds"].annotation == int | None


def test_an_unknown_runtime_stays_unknown_on_a_card() -> None:
    """**ADR-0014: absence is not zero.**.

    Kills `runtime_seconds: int = 0`. A zero runtime is not "no progress" --
    it is a divisor that makes every partially-watched title read as
    complete, on every client that computes the fraction the card declined
    to compute for it.
    """
    card = _card(runtime_seconds=None)
    assert card.runtime_seconds is None
    assert card.position_seconds == 1800


def test_the_display_hint_vocabulary_is_adr_0006s_four_and_no_others() -> None:
    """ADR-0006's only concrete client vocabulary.

    "Rows carry a display *hint* (`portrait | landscape | wide | square`) but never a
    layout.".

    Kills a fifth member. The realistic fifth is `HERO` or `GRID_3_COLUMN`,
    and `GRID_3_COLUMN` is a layout wearing a hint's name -- the exact thing
    the ADR's "never a layout" clause exists to refuse, arriving as an
    enum member that no reviewer reads as an architecture change.
    """
    assert {hint.value for hint in DisplayHint} == {"portrait", "landscape", "wide", "square"}


def test_a_display_hint_belongs_to_the_row_and_not_to_a_card() -> None:
    """A hint describes the shelf: a row of portraits is a portrait row.

    Kills moving `display_hint` onto `RowCard`, which lets one row disagree
    with itself about its own shape -- a per-item layout instruction, which
    is what ADR-0006 declines to send, arriving by a second route.
    """
    assert "display_hint" in BuiltRow.model_fields
    assert "display_hint" not in RowCard.model_fields


def test_a_built_row_with_no_cards_is_constructible() -> None:
    """**An empty row and an absent row are different states.**.

    Kills `min_length=1` on `cards`, and kills a `model_validator` that
    raises on an empty tuple. Either one forces `Row.build()` to return
    `BuiltRow | None`, and then "built and had nothing to show" collapses
    into the same `None` as "never proposed" -- which are a quiet household
    and a dead provider respectively, and Group I's metrics have to tell
    them apart.
    """
    row = BuiltRow(
        slug="continue-watching",
        title="Continue Watching",
        family=RowFamily.SOURCE,
        display_hint=DisplayHint.LANDSCAPE,
        ttl=timedelta(seconds=60),
    )
    assert row.cards == ()


def test_a_built_row_carries_its_own_ttl_so_a_cached_row_is_self_describing() -> None:
    """PRD 06 puts `ttl` on the `Row` class.

    It is on the value instead.

    A cache stores a built row, not its producer. With the TTL on the class,
    the cache needs a reference back to the object that built it to know
    when to drop it, and the two disagree after any deploy that changes the
    number: a row written under 60 s judged by 300 s. ADR-0020's argument
    for fingerprints, on a shorter-lived derivative.

    Kills removing the field, and kills defaulting it -- a default TTL is a
    number nobody chose that every row silently inherits.
    """
    assert "ttl" in BuiltRow.model_fields
    assert BuiltRow.model_fields["ttl"].is_required()


def test_the_row_family_vocabulary_is_prd_06s_three_and_no_others() -> None:
    """PRD 06's family table, as a set rather than a `<=`.

    a fourth member fails here and a deleted third one does too.
    """
    assert {family.value for family in RowFamily} == {"source", "similarity", "curated"}


def test_a_built_row_names_its_family_and_there_is_only_one_spelling_of_it() -> None:
    """The diversity constraints are stated in families.

    "no three consecutive similarity rows; cap per family" -- so the composer needs a
    typed key to state them in, and needs exactly one.

    Kills shipping `RowKind` and `RowFamily` as two enums with the same
    members, which is what the plan's task body and its own cross-group
    handoff each asked for separately. Two vocabularies for one concept is
    a second source of truth, and the composer twenty-eight tasks away
    would have to pick one and leave the other reachable.

    Also kills naming the field `kind`: `RowCard.kind` is already a
    `TitleKind` in this same module, so `BuiltRow.kind: RowKind` puts two
    unrelated "kind"s one field apart.
    """
    assert BuiltRow.model_fields["family"].annotation is RowFamily
    assert "kind" not in BuiltRow.model_fields
    import usher.domain.rows as rows_module

    assert not hasattr(rows_module, "RowKind")


def test_a_centroid_records_the_embedder_that_produced_it() -> None:
    """Kills dropping `model_name`.

    A centroid is a derived vector with a `title_embeddings`-shaped
    staleness problem, and `ports/embedding.py` already argues the fix:
    recording runtime-and-checkpoint is what makes swapping the embedder
    invalidate every stored vector through `IS DISTINCT FROM`, instead of
    through a migration somebody has to remember to write. A centroid
    without it is a vector nobody can prove is current.
    """
    assert "model_name" in Centroid.model_fields
    assert Centroid.model_fields["model_name"].is_required()


def test_a_centroid_over_no_titles_is_not_constructible() -> None:
    """ADR-0014 on the taste signal itself.

    Kills `title_count: int = 0`. A centroid averaged over nothing is not
    "neutral taste", it is a point equidistant from every title in the
    catalog, which makes every genre equally "affine" and every seed equally
    close. The honest value for a household that has watched nothing is
    `RowContext.taste = None`, and this constraint is what stops a
    zero-vector stand-in being constructible in the first place.

    The vector is deliberately **non-empty** here. An earlier version of
    this case passed `vector=()` alongside `title_count=0` and mutation
    survived it: `Field(min_length=1)` on the vector raised the same
    `ValidationError`, so the case proved nothing about the count. Two
    constraints, two cases -- the same reason the artwork case carries two
    assertions.
    """
    with pytest.raises(ValidationError):
        Centroid(
            user_id=uuid.uuid4(),
            vector=(0.1, 0.9),
            model_name="fastembed:BAAI/bge-small-en-v1.5",
            title_count=0,
            computed_at=datetime.now(UTC),
        )


def test_a_centroid_with_an_empty_vector_is_not_constructible() -> None:
    """The other half of the refusal above, and the one that matters at the reader.

    Kills `vector: tuple[float, ...] = ()`. An empty vector is not a
    centroid at all, and every whitespace-only document already embeds to
    the *identical* unit vector at cosine 1.0000 from every other one --
    the degenerate cluster `services/index.py` refuses with a NULL. A
    centroid that can be empty is that failure with a user id attached.
    """
    with pytest.raises(ValidationError):
        Centroid(
            user_id=uuid.uuid4(),
            vector=(),
            model_name="fastembed:BAAI/bge-small-en-v1.5",
            title_count=12,
            computed_at=datetime.now(UTC),
        )
