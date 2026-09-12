"""Required attribution strings (PRD 04's hard rule 4, PRD 07's Meta table)."""

from fastapi import APIRouter

from usher.adapters.bulk.imdb import IMDB_ATTRIBUTION
from usher.adapters.bulk.movielens import MOVIELENS_ATTRIBUTION
from usher.adapters.bulk.wikidata import WIKIDATA_ATTRIBUTION
from usher.adapters.tmdb.client import TMDB_ATTRIBUTION
from usher.api.dto.meta import AttributionEntry

router = APIRouter(tags=["meta"])

_ATTRIBUTIONS: tuple[AttributionEntry, ...] = (
    AttributionEntry(source="IMDb", text=IMDB_ATTRIBUTION),
    AttributionEntry(source="TMDb", text=TMDB_ATTRIBUTION),
    AttributionEntry(source="Wikidata", text=WIKIDATA_ATTRIBUTION),
    AttributionEntry(source="MovieLens", text=MOVIELENS_ATTRIBUTION),
)


@router.get("/meta/attribution", response_model=list[AttributionEntry])
async def attribution() -> list[AttributionEntry]:
    """The four required attribution strings, unfiltered by deployment
    state. See the module docstring for why."""
    return list(_ATTRIBUTIONS)
