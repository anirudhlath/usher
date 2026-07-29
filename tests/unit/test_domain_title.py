import pytest
from pydantic import ValidationError

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title


def test_title_requires_only_kind_and_name() -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert title.name == "Dune"
    assert title.enrichment_state is EnrichmentState.SKELETON
    assert title.tmdb_id is None
    assert title.genres == []


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
