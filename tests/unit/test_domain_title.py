import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind
from usher.domain.title import Title


def test_title_requires_only_kind_and_name() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert title.name == "Dune"
    assert title.enrichment_state is EnrichmentState.SKELETON
    assert title.tmdb_id is None
    assert title.genres == ()


def test_sort_name_may_differ_from_name() -> None:
    """`sort_name` is a distinct field, not a mirror of name.

    An article-first display name sorts under a different letter than it displays under;
    the rest of this file sets the two equal for brevity, which is not a rule.
    """
    title = Title(kind=TitleKind.MOVIE, name="The Matrix", sort_name="Matrix, The")
    assert title.name == "The Matrix"
    assert title.sort_name == "Matrix, The"


def test_title_generates_its_own_identity() -> None:
    a = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    b = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert a.id != b.id
    assert a.id.version == 7


def test_title_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Title(kind="documentary", name="X", sort_name="X")


def test_title_accepts_provider_ids_as_attributes() -> None:
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        tmdb_id=90000100,
        imdb_id="tt99000100",
    )
    assert title.tmdb_id == 90000100
    assert title.imdb_id == "tt99000100"


def test_title_is_immutable() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    with pytest.raises(ValidationError):
        title.name = "Other"  # type: ignore[misc]  # verifying the runtime rejection frozen=True enforces


# --- extra="forbid" -------------------------------------------------------


def test_extra_fields_are_rejected() -> None:
    """Adapters hand-map dozens of provider fields onto Title by keyword.

    A typo'd field name must fail loudly at construction instead of being silently
    discarded — the same standard `usher.config.Settings` holds.
    """
    with pytest.raises(ValidationError):
        Title(
            kind=TitleKind.MOVIE,
            name="Dune",
            sort_name="Dune",
            tmbd_id=999,  # type: ignore[call-arg]  # deliberate typo of tmdb_id
        )


# --- evolve() vs model_copy(update=) ---------------------------------------


def test_evolve_returns_a_changed_validated_copy() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    updated = title.evolve(name="Dune (2021)")
    assert updated.name == "Dune (2021)"
    assert updated.id == title.id
    assert title.name == "Dune"  # original is untouched


def test_evolve_rejects_what_model_copy_would_silently_accept() -> None:
    """`model_copy(update=...)` applies a change with no validation at all.

    `evolve()` is the replacement — same shape, but it re-validates.
    """
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")

    unsafe = title.model_copy(update={"tmdb_id": "not-an-int"})
    # The declared type is `int | None`; mypy flags this comparison as
    # non-overlapping — that's the bug in miniature: model_copy(update=...)
    # produced a Title whose runtime value no longer matches its own type.
    assert unsafe.tmdb_id == "not-an-int"  # type: ignore[comparison-overlap]

    with pytest.raises(ValidationError):
        title.evolve(tmdb_id="not-an-int")


# --- AwareDatetime ----------------------------------------------------------


@pytest.mark.parametrize("field", ["created_at", "updated_at", "enriched_at"])
def test_naive_datetime_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        Title.model_validate(
            {
                "kind": TitleKind.MOVIE,
                "name": "Dune",
                "sort_name": "Dune",
                field: datetime(2026, 1, 1),  # no tzinfo
            }
        )


def test_created_at_and_updated_at_default_to_aware_now_when_omitted() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert title.created_at.tzinfo is not None
    assert title.updated_at.tzinfo is not None


# --- immutable containers / hashability -------------------------------------


@pytest.mark.parametrize("field", ["genres", "keywords", "spoken_languages", "origin_countries"])
def test_tuple_fields_are_immutable(field: str) -> None:
    title = Title.model_validate(
        {"kind": TitleKind.MOVIE, "name": "Dune", "sort_name": "Dune", field: ["a"]}
    )
    value = getattr(title, field)
    assert value == ("a",)
    with pytest.raises(AttributeError):
        value.append("b")


def test_field_provenance_dict_is_still_mutable_despite_frozen() -> None:
    """`frozen=True` blocks rebinding `title.field_provenance`, not mutating what is in it.

    This is why Title, alone among the five domain models, is unhashable.
    """
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    title.field_provenance["name"] = "tmdb"
    assert title.field_provenance == {"name": "tmdb"}


def test_title_is_not_hashable() -> None:
    """`field_provenance` is a dict, which poisons the generated `__hash__`.

    The failure is a loud TypeError, not silent corruption.
    """
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    with pytest.raises(TypeError):
        hash(title)


# --- value constraints -------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("year", -40),
        ("end_year", -1),
        ("runtime_minutes", -9),
        ("tmdb_vote_count", -1),
        ("tmdb_popularity", -1.0),
        ("tmdb_vote_average", -1.0),
        ("imdb_num_votes", -1),
        ("imdb_average_rating", -1.0),
    ],
)
def test_negative_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", **{field: value})


@pytest.mark.parametrize("field", ["tmdb_vote_average", "imdb_average_rating"])
def test_a_rating_rejects_values_outside_the_zero_to_ten_scale(field: str) -> None:
    """Both sources use the 0-10 scale, so the bound is asserted on both columns."""
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", **{field: 99.0})


@pytest.mark.parametrize("field", ["tmdb_vote_average", "imdb_average_rating"])
def test_a_rating_accepts_the_zero_to_ten_boundaries(field: str) -> None:
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", **{field: 0.0})
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", **{field: 10.0})


def test_empty_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="", sort_name="X")


def test_empty_sort_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="X", sort_name="")


# --- imdb_id pattern -----------------------------------------------------


def test_imdb_id_rejects_person_ids() -> None:
    """An `nm...` id identifies a person, not a title.

    A plausible copy-paste mistake that must not land on Title.imdb_id.
    """
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="nm99000001")


def test_imdb_id_rejects_unprefixed_ids() -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="1160419")


def test_imdb_id_accepts_seven_and_eight_digit_forms() -> None:
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt99000100")
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt99001000")


# --- ProductionStatus --------------------------------------------------


def test_production_status_includes_pilot_and_rumored() -> None:
    """TMDb returns these; a closed enum missing one forces an adapter to drop or raise."""
    Title(kind=TitleKind.SERIES, name="X", sort_name="X", status=ProductionStatus.PILOT)
    Title(kind=TitleKind.MOVIE, name="X", sort_name="X", status=ProductionStatus.RUMORED)


# --- enrichment_error ---------------------------------------------------


def test_enrichment_error_is_independent_of_enrichment_state() -> None:
    """Setting enrichment_error must not move enrichment_state.

    A failed enrichment attempt on a skeleton Title stays a skeleton Title.
    """
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        enrichment_error="TMDb request timed out",
    )
    assert title.enrichment_error == "TMDb request timed out"
    assert title.enrichment_state is EnrichmentState.SKELETON


def test_enrichment_error_defaults_to_none() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert title.enrichment_error is None


# --- serialization round-trip (the wire contract from M4 onward) -----------


def test_title_serialization_round_trips() -> None:
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        year=2021,
        tmdb_id=90000100,
        imdb_id="tt99000100",
        genres=["scifi", "drama"],
        field_provenance={"name": "tmdb"},
    )
    restored = Title.model_validate_json(title.model_dump_json())
    assert restored == title


# --- the three unbounded-above numbers, and which of them is a defect ------


def test_tmdb_popularity_refuses_a_non_finite_value() -> None:
    """`ge=0` alone does not refuse `inf`, and neither does the column.

    `titles.tmdb_popularity` is `double precision`, where IEEE `Infinity` is legal;
    `DomainModel`'s `allow_inf_nan=False` is the only layer that says no. `1e400` is
    well-formed JSON, so this is the value a TMDb payload actually delivers.
    """
    for value in (json.loads("1e400"), float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_popularity=value)


def test_a_finite_tmdb_popularity_is_still_accepted() -> None:
    """The control: `allow_inf_nan=False` refuses the non-finite values and nothing else.

    Without this, "refuses infinity" is also satisfied by a field that refuses every
    float.
    """
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_popularity=1739.421)
    assert title.tmdb_popularity == 1739.421
    bare = Title(kind=TitleKind.MOVIE, name="D", sort_name="D", tmdb_popularity=0.0)
    assert bare.tmdb_popularity == 0.0


@pytest.mark.parametrize("field", ["tmdb_vote_average", "imdb_average_rating"])
def test_a_rating_refuses_a_non_finite_value(field: str) -> None:
    """`DomainModel`'s `allow_inf_nan=False` refuses these, not the `le=10`.

    The ceiling is pinned by `test_a_rating_rejects_values_outside_the_zero_to_ten_scale`.
    """
    for value in (json.loads("1e400"), float("nan")):
        with pytest.raises(ValidationError):
            Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", **{field: value})


def test_year_and_vote_counts_still_accept_a_value_their_column_cannot_hold() -> None:
    """An excluded case, asserted rather than left unstated.

    `Field(ge=0)` against an `integer` column lets `2**31` construct cleanly where the
    column cannot hold it. The writers that fill these columns are `bulk.py`'s COPY
    paths, which never construct a `Title`, so a ceiling here would be invisible to the
    path that actually overflows them — and whoever closes that path sees this go red.
    """
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        year=2**31,
        tmdb_vote_count=2**31,
        imdb_num_votes=2**31,
    )
    assert title.year == 2**31
    assert title.tmdb_vote_count == 2**31
    assert title.imdb_num_votes == 2**31
