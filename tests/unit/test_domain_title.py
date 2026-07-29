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
        tmdb_id=438631,
        imdb_id="tt1160419",
    )
    assert title.tmdb_id == 438631
    assert title.imdb_id == "tt1160419"


def test_title_is_immutable() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    with pytest.raises(ValidationError):
        title.name = "Other"  # type: ignore[misc]  # verifying the runtime rejection frozen=True enforces


# --- extra="forbid" -------------------------------------------------------


def test_extra_fields_are_rejected() -> None:
    """Adapters hand-map dozens of provider fields onto Title by keyword; a
    typo'd field name must fail loudly at construction instead of being
    silently discarded — the same standard usher.config.Settings holds."""
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
    """model_copy(update=...) is the write path the whole system must
    avoid: it applies a change with no validation at all. evolve() is the
    replacement — same shape, but it re-validates."""
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")

    unsafe = title.model_copy(update={"tmdb_id": "not-an-int"})
    # The declared type is `int | None`; mypy flags this comparison as
    # non-overlapping — that's the bug in miniature: model_copy(update=...)
    # produced a Title whose runtime value no longer matches its own type.
    assert unsafe.tmdb_id == "not-an-int"  # type: ignore[comparison-overlap]

    with pytest.raises(ValidationError):
        title.evolve(tmdb_id="not-an-int")


# --- AwareDatetime ----------------------------------------------------------


def test_naive_datetime_is_rejected_for_created_at() -> None:
    with pytest.raises(ValidationError):
        Title(
            kind=TitleKind.MOVIE,
            name="Dune",
            sort_name="Dune",
            created_at=datetime(2026, 1, 1),  # no tzinfo
        )


def test_created_at_and_updated_at_default_to_aware_now_when_omitted() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert title.created_at.tzinfo is not None
    assert title.updated_at.tzinfo is not None


# --- immutable containers / hashability -------------------------------------


def test_genres_is_an_immutable_tuple() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", genres=["scifi"])
    assert title.genres == ("scifi",)
    with pytest.raises(AttributeError):
        title.genres.append("thriller")  # type: ignore[attr-defined]


def test_field_provenance_dict_is_still_mutable_despite_frozen() -> None:
    """frozen=True blocks rebinding `title.field_provenance = ...`, not
    mutating a mutable value already inside it. This is exactly why Title,
    alone among the five domain models, is unhashable — see below."""
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    title.field_provenance["name"] = "tmdb"
    assert title.field_provenance == {"name": "tmdb"}


def test_title_is_not_hashable() -> None:
    """field_provenance is a dict, which poisons the generated __hash__
    even though the model is frozen. Failure is a loud TypeError, not
    silent corruption. See DomainModel's docstring."""
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
        ("vote_count", -1),
        ("popularity", -1.0),
    ],
)
def test_negative_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", **{field: value})


def test_community_rating_rejects_values_outside_the_tmdb_scale() -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", community_rating=99.0)


def test_community_rating_accepts_tmdb_scale_boundaries() -> None:
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", community_rating=0.0)
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", community_rating=10.0)


def test_empty_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="", sort_name="X")


def test_empty_sort_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="X", sort_name="")


# --- imdb_id pattern -----------------------------------------------------


def test_imdb_id_rejects_person_ids() -> None:
    """"nm..." identifies a person, not a title — a plausible copy-paste
    mistake that must not land on Title.imdb_id."""
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="nm0000190")


def test_imdb_id_rejects_unprefixed_ids() -> None:
    with pytest.raises(ValidationError):
        Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="1160419")


def test_imdb_id_accepts_seven_and_eight_digit_forms() -> None:
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt1160419")
    Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt11604190")


# --- ProductionStatus --------------------------------------------------


def test_production_status_includes_pilot_and_rumored() -> None:
    """TMDb actually returns these; a closed enum missing them would force
    an adapter to drop the field or raise."""
    Title(kind=TitleKind.SERIES, name="X", sort_name="X", status=ProductionStatus.PILOT)
    Title(kind=TitleKind.MOVIE, name="X", sort_name="X", status=ProductionStatus.RUMORED)


# --- enrichment_error (ADR-0008) ----------------------------------------


def test_enrichment_error_is_independent_of_enrichment_state() -> None:
    """Setting enrichment_error must not move enrichment_state — a failed
    enrichment attempt on a skeleton Title stays a skeleton Title."""
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
        tmdb_id=438631,
        imdb_id="tt1160419",
        genres=["scifi", "drama"],
        field_provenance={"name": "tmdb"},
    )
    restored = Title.model_validate_json(title.model_dump_json())
    assert restored == title
