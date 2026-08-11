"""Required attribution strings (PRD 04's hard rule 4, PRD 07's Meta table).

Beside `health.py`, which already carries `tags=["meta"]`. PRD 04 rule 4 says
*"the API exposes required attribution strings so every client can display
them"* and PRD 07's Meta table has named this route since M1 -- until this
task, neither was true: `grep -rn "\\.attribution" src/` found zero readers of
`BulkDataset.attribution`, only a comment in `adapters/bulk/movielens.py`.

**Static and not filtered by `import_runs`.** That table could answer "which
importers has this deployment actually run", and the answer would be wrong in
the direction that matters: on a fresh install it is empty, so a licence
string would be withheld from exactly the deployment most likely to be
rendering freshly imported data. Over-display costs a client one citation too
many; under-display is a licence breach.

**No `logo_url`.** PRD 04's table asks TMDb for "Logo + disclaimer", a string
cannot carry a logo, and Usher ships no image -- the logo stays a client
obligation, named in PRD 04.

**This is the first `usher.api -> usher.adapters` import in the project.**
Legal under all nine `lint-imports` contracts: the layering contract's layers
are `api -> services -> ports -> domain` and do not name `adapters`; the
contract forbidding a concrete adapter from escaping its package only names
`usher.adapters.emby`, `.search`, `.embedding` and `.llm` -- none of which
`usher.adapters.bulk` or `usher.adapters.tmdb` are.

**No `SessionDep`.** This route reads four module constants and returns them;
it cannot 503 and it cannot leak a host, as a property of the dependency
graph rather than an intention -- see
`tests/unit/test_api_meta.py::test_the_route_holds_no_sessiondep`.

`TMDB_ATTRIBUTION` is imported from `usher.adapters.tmdb.client` rather than
from `usher.adapters.bulk.tmdb_ids`, which defines a byte-identical second
copy for the reason `client.py`'s own comment gives ("an adapter reaching
across into a sibling for a constant couples two things that only happen to
share an upstream") -- that reasoning is about one adapter reaching into
another, not about this router, but picking one arbitrarily rather than
importing both keeps the served list at four entries instead of a fifth
duplicate. The two constants' equality is asserted directly in the test file
so a future edit to either cannot drift unnoticed.
"""

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
    AttributionEntry(source="MovieLens", text=MOVIELENS_ATTRIBUTION),
    AttributionEntry(source="Wikidata", text=WIKIDATA_ATTRIBUTION),
)


@router.get("/meta/attribution", response_model=list[AttributionEntry])
async def attribution() -> list[AttributionEntry]:
    """The four required attribution strings, unfiltered by deployment
    state. See the module docstring for why."""
    return list(_ATTRIBUTIONS)
