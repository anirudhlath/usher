"""No domain model accepts a non-finite float."""

import math
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usher.domain.enums import TitleKind
from usher.domain.search import SearchResult, SimilarTitle
from usher.domain.taste import Centroid

NON_FINITE = [math.inf, -math.inf, math.nan]


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_search_result_refuses_a_non_finite_popularity(value: float) -> None:
    with pytest.raises(ValidationError):
        SearchResult(title_id=uuid.uuid4(), kind=TitleKind.MOVIE, name="anything", popularity=value)


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_search_result_refuses_a_non_finite_score(value: float) -> None:
    with pytest.raises(ValidationError):
        SearchResult(title_id=uuid.uuid4(), kind=TitleKind.MOVIE, name="anything", score=value)


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_similar_title_refuses_a_non_finite_score(value: float) -> None:
    with pytest.raises(ValidationError):
        SimilarTitle(title_id=uuid.uuid4(), kind=TitleKind.MOVIE, name="anything", score=value)


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_centroid_refuses_a_non_finite_component(value: float) -> None:
    """A NaN here does not raise anywhere downstream; it makes every distance NaN."""
    with pytest.raises(ValidationError):
        Centroid(
            user_id=uuid.uuid4(),
            vector=(0.1, value, 0.3),
            model_name="bge-m3",
            title_count=1,
            computed_at=datetime.now(UTC),
        )
