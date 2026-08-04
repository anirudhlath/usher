"""The search port's DTOs. The ABCs' behaviour is `tests/contract/
search_index_contract.py`; what is asserted here is the shape of the values
that cross the port, which is where 🔶 1's four defects actually lived.
"""

import dataclasses

import pytest

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.ports.search import (
    SearchDocument,
    SearchFilters,
    SearchHit,
    SearchMode,
    SearchOutcome,
    SearchRequest,
)


def test_a_search_document_carries_the_text_and_the_vector_the_service_assembled() -> None:
    """Defect 1 of the 🔶: `index(title_id)` forced a second engine to fetch
    each title back out to build its document -- 1.3M round-trips on a
    rebuild, against a service that was holding the `Title` already."""
    title_id = new_id()
    document = SearchDocument(
        title_id=title_id,
        kind=TitleKind.MOVIE,
        name="The Quiet Vacuum",
        sort_name="Quiet Vacuum, The",
        original_name="The Quiet Vacuum",
        overview="A signals engineer listens to an empty room for a year.",
        tagline="Nothing is a sound.",
        genres=("Drama",),
        keywords=("silence",),
        year=2019,
        popularity=4.25,
        vector=(0.6, 0.8),
    )
    assert document.title_id == title_id
    assert document.vector == (0.6, 0.8)


def test_a_search_document_reserves_credits_and_ships_them_empty() -> None:
    """Boundary call 2. There is no `Person`, `Credit` or `Collection` table
    anywhere in `src/`; the only place credits physically exist is
    `raw_payloads.payload`, and assembling a search document out of a
    *provider's* JSON shape would put a TMDb-shaped concept in `services/`.
    Reserved rather than repurposed, so the day M7 lands `Credit` filling it
    is a migration rather than a port change."""
    document = SearchDocument(
        title_id=new_id(),
        kind=TitleKind.SERIES,
        name="Harbour Lights",
        sort_name="Harbour Lights",
    )
    assert document.credits == ()


def test_search_filters_are_a_closed_vocabulary_not_a_dict() -> None:
    """Defect 2 of the 🔶. `filters: dict[str, Any]` has no key vocabulary,
    so two backends invent different ones and nothing fails -- the second
    engine simply means something else by `owned`."""
    filters = SearchFilters(
        kinds=(TitleKind.MOVIE,),
        year_from=1990,
        year_to=1999,
        genres=("Drama",),
        owned_only=True,
        min_enrichment=EnrichmentState.ENRICHED,
    )
    assert {field.name for field in dataclasses.fields(filters)} == {
        "kinds",
        "year_from",
        "year_to",
        "genres",
        "owned_only",
        "min_enrichment",
    }


def test_a_request_carries_the_query_vector_the_caller_computed() -> None:
    """Defect 4 of the 🔶, and the half that settles 🔶 3 as a side effect:
    the **caller** embeds. A backend doing its own embedding is a backend
    with its own model, its own prefix convention, and its own drift."""
    request = SearchRequest(
        query="an empty room",
        mode=SearchMode.SEMANTIC,
        query_vector=(1.0, 0.0),
    )
    assert request.query_vector == (1.0, 0.0)
    assert request.filters == SearchFilters()


def test_a_semantic_request_with_no_vector_is_refused_at_construction() -> None:
    """`SourceEvent.__post_init__`'s rule one port over: a DTO that can be
    built in a state no implementation can serve is a DTO that pushes the
    failure to whichever backend notices first. A `SEMANTIC` request with no
    vector has exactly two plausible readings -- "return nothing" and
    "embed it yourself" -- and the second is the one defect 4 exists to
    delete. Refused here, once, rather than argued about per backend.
    """
    with pytest.raises(ValueError, match="query_vector"):
        SearchRequest(query="an empty room", mode=SearchMode.SEMANTIC)
    with pytest.raises(ValueError, match="query_vector"):
        SearchRequest(query="an empty room", mode=SearchMode.FUSED)


def test_an_outcome_reports_semantic_coverage_beside_its_hits() -> None:
    """Point 3 of "the one thing this milestone must not get wrong". A title
    with no embedding is *absent from the semantic candidate list*, and RRF
    cannot tell "ranked last" from "never a candidate" -- so the fraction
    that had a vector rides back with the hits rather than being inferred."""
    outcome = SearchOutcome(hits=(SearchHit(title_id=new_id(), score=1.0),), semantic_coverage=0.5)
    assert outcome.semantic_coverage == 0.5
    assert SearchOutcome().hits == ()


@pytest.mark.parametrize(
    "dto",
    [SearchHit, SearchRequest, SearchDocument, SearchFilters, SearchOutcome],
)
def test_every_search_dto_is_frozen_and_slotted(dto: type) -> None:
    """`ports/search.py`'s two original dataclasses were the only port DTOs
    in the repository missing `slots=True`. A slotted dataclass refuses an
    attribute nobody declared, which is what makes a typo'd keyword a
    failure rather than a field that silently does nothing."""
    assert dataclasses.is_dataclass(dto)
    assert dto.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert "__slots__" in dto.__dict__
