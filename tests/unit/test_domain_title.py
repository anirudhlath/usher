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
    """sort_name has no normalization contract (see the field's comment in
    title.py) but it is a distinct field, not a mirror of name — e.g. an
    article-first display name sorts under a different letter than it
    displays under. The rest of this file's fixtures set sort_name equal
    to name for brevity; this test exists so that never reads as a rule."""
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
        ("community_rating", -1.0),
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
    """ "nm..." identifies a person, not a title — a plausible copy-paste
    mistake that must not land on Title.imdb_id."""
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
        tmdb_id=90000100,
        imdb_id="tt99000100",
        genres=["scifi", "drama"],
        field_provenance={"name": "tmdb"},
    )
    restored = Title.model_validate_json(title.model_dump_json())
    assert restored == title


# --- the three unbounded-above numbers, and which of them is a defect ------


def test_popularity_refuses_a_non_finite_value() -> None:
    """PRD 09's carried *"`Title.popularity` accepts infinity"* debt, closed by
    M10's F9.

    `1e400` is well-formed JSON and `json.loads` maps it onto `inf` with no
    error at all, so this is the value a TMDb payload actually delivers rather
    than a constructed one — spelled that way here so the case is about the
    reachable shape and not about `float("inf")`.

    `ge=0` alone does not refuse it (`float("inf") >= 0` is `True`), and
    neither does the column: `titles.popularity` is `sa.Float()` — `double
    precision`, where IEEE `Infinity` is legal — and `Infinity >= 0` satisfies
    `ck_titles_popularity_non_negative` too. So this model is the only layer
    that can say no, which is why ADR-0041 (`docs/prd/decisions/0041-a-bounded-\
column-is-a-declared-type-that-refuses.md`) leaves it to the field while
    leaving every *narrower*-than-its-field column to the repository.
    """
    for value in (json.loads("1e400"), float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", popularity=value)


def test_a_finite_popularity_is_still_accepted() -> None:
    """The control for the case above: `allow_inf_nan=False` must refuse the
    two non-finite values and nothing else. Without this, "refuses infinity"
    is also satisfied by a field that refuses every float."""
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", popularity=1739.421)
    assert title.popularity == 1739.421
    assert Title(kind=TitleKind.MOVIE, name="D", sort_name="D", popularity=0.0).popularity == 0.0


def test_community_rating_refuses_a_non_finite_value_by_its_ceiling() -> None:
    """`community_rating` never had `popularity`'s defect, and the reason is
    the `le=10` rather than anything about the field being better designed.

    Stated as a case because the field carries no `allow_inf_nan=False`: if a
    later change relaxes or removes that ceiling — TMDb changing scale, say —
    the protection goes with it silently. This is the thing that notices.
    """
    for value in (json.loads("1e400"), float("nan")):
        with pytest.raises(ValidationError):
            Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", community_rating=value)


def test_year_and_vote_count_still_accept_a_value_their_column_cannot_hold() -> None:
    """An **excluded** case, asserted rather than left unstated.

    `Field(ge=0)` against an `integer` column is what
    `.claude/rules/db-and-sql.md` calls *"the common shape here"*, and
    `titles.year` and `titles.vote_count` are both live examples: `2**31`
    constructs cleanly and the column cannot hold it. M10's F9 deliberately
    does **not** close them, for the reason ADR-0041 gives in its question (5)
    — the writers that put values in those two columns are
    `bulk.py:upsert_titles` and `bulk.py:apply_ratings`, which take
    `ports.bulk.ImdbTitle` and `ports.bulk.ImdbRating` and never construct a
    `Title` at all, so a ceiling here would be invisible to the path that
    actually overflows them. Both are in ADR-0041's `exposed-copy` bucket,
    which M9's boundary call 8 keeps out of M10 whole.

    The case exists so the exclusion is a recorded state rather than an
    oversight: whoever closes the COPY path will see it go red.
    """
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", year=2**31, vote_count=2**31)
    assert title.year == 2**31
    assert title.vote_count == 2**31
