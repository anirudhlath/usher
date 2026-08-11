"""`GET /titles/{id}/similar` -- PRD 05's precomputed neighbours, over
`SimilarityService` (`services/similar.py`).

M6 built the whole of what "similar" means -- the blend, `title_neighbors`,
`SimilarityService` -- and shipped no HTTP route
([09](../../../../docs/prd/09-roadmap.md)'s M6 boundary call 1). This module is
the wire shape of the route M9 adds over that finished wiring, and its whole
design question is freshness: `title_neighbors` has two causes of staleness
and only one of them is a query (`services/similar.py`'s module docstring).
Both signals reach this body, and neither is presented as the other.
"""

import uuid
from collections.abc import Sequence
from typing import Self

from pydantic import AwareDatetime, BaseModel

from usher.domain.enums import TitleKind
from usher.domain.search import SimilarTitle

__all__ = ["SimilarResponse", "SimilarTitleResponse"]


class SimilarTitleResponse(BaseModel):
    """One neighbour, in the **stored order** -- `SimilarityService.
    neighbors_of` already reads `title_neighbors` back by its own stamped
    `rank`, best first, ties broken by id (`ports/repository/search.py`'s
    `TitleNeighborRepository.list_for`), and this DTO never re-sorts on
    `score`. Reproducing the order from the score works only up to float
    ties, and a tie broken differently on two reads would show a client two
    different "most similar" titles for the same catalog.
    """

    id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    score: float

    @classmethod
    def of(cls, neighbor: SimilarTitle) -> Self:
        return cls(
            id=neighbor.title_id,
            kind=neighbor.kind,
            name=neighbor.name,
            year=neighbor.year,
            score=neighbor.score,
        )


class SimilarResponse(BaseModel):
    """Neighbours, plus both of `title_neighbors`' staleness signals --
    reported rather than implied, because a client that could not see either
    one would be shown yesterday's neighbours (or none at all) with no way to
    tell that from "this title genuinely has nothing like it".

    **`computed_at` answers the undecidable half.** `None` means the artefact
    has *never* been built -- a different fact from `neighbors == []`, which
    means the batch ran and found nothing for this seed. Collapsing the two
    would tell an operator a film has no similar titles when the truth is
    that nothing has run
    (`TitleNeighborRepository.computed_at`'s own docstring). When it is not
    `None`, it is the **oldest** stored row across the *whole* artefact, not
    a per-seed timestamp -- so it can be old even for a seed whose own row is
    recent, because some *other* title may have been embedded into this
    seed's neighbourhood since. That half is undecidable per row
    ([ADR-0020](../../../../docs/prd/decisions/0020-derived-state-carries-its-fingerprint.md))
    and this field is the closest this response gets to answering it: a
    whole-artefact age, not a guarantee.

    **`stale` answers the other half, exactly and per seed.** It is
    `count_stale(blend_fingerprint=blend_fingerprint(), title_id=<this
    title>) > 0` -- true when this seed's stored rows were written under a
    blend whose weights, stored count or candidate pool have since changed,
    which makes a score computed under the old meaning incomparable with one
    computed under the running one. **`stale=False` is not a freshness
    guarantee**: a seed can carry the running fingerprint and still be
    missing a neighbour that only exists because some other title was
    embedded after this seed's row was written -- the same undecidable half
    `computed_at` reports rather than resolves. Nothing schedules `usher
    similar --rebuild`; it is an operator's command or a cron entry.
    """

    neighbors: list[SimilarTitleResponse]
    computed_at: AwareDatetime | None
    stale: bool

    @classmethod
    def of(
        cls,
        neighbors: Sequence[SimilarTitle],
        *,
        computed_at: AwareDatetime | None,
        stale: bool,
    ) -> Self:
        return cls(
            neighbors=[SimilarTitleResponse.of(neighbor) for neighbor in neighbors],
            computed_at=computed_at,
            stale=stale,
        )
