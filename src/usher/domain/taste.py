"""The taste centroid: one derived vector per user."""

import uuid
from dataclasses import dataclass

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel


class Centroid(DomainModel):
    """A user's taste, as one vector, with the evidence for its currency.

    `model_name` records the embedding *runtime and checkpoint*
    (`fastembed:BAAI/bge-small-en-v1.5`), which makes a centroid computed under
    a different embedder detectable by `IS DISTINCT FROM` rather than by
    somebody remembering to write a migration.

    **A centroid over no titles is not constructible.** A vector averaged over
    nothing is not "neutral taste": it is a point equidistant from everything,
    which makes every genre equally affine and every seed equally close -- noise
    wearing a reason. The honest value for a household that has watched nothing
    is `RowContext.taste = None`, refused here rather than guarded against at
    every reader.
    """

    user_id: uuid.UUID
    # A tuple, not a list: `DomainModel` is frozen, and a model carrying a
    # list is unhashable even so. `float` rather than the stored `halfvec` --
    # the quantisation lives in the database, and anything doing numpy work
    # with this must cast to `float32` first (`float16` is 140x slower).
    vector: tuple[float, ...] = Field(min_length=1)
    model_name: str = Field(min_length=1)
    # How many watch states went into the mean. `ge=1` is the refusal above;
    # it is also what lets a reader tell a centroid built from one film from
    # one built from two hundred, which is the difference between a signal
    # and an accident.
    title_count: int = Field(ge=1)
    computed_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class GenreAffinity:
    """One genre the household watches disproportionately to its own library.

    **In `domain/` rather than beside the service that computes it**:
    `RowContext` carries it, `ports/rows.py` must name the type, and a port may
    name a domain type or one of its own and nothing else. `services/taste.py`
    re-exports it, where it is computed and where callers name it.

    A plain frozen dataclass rather than a `DomainModel`: never stored, never
    validated at a boundary, never round-tripping through a repository.

    `support` is carried because a row built from four titles and a row built
    from forty are different claims, and the reason string must not pretend
    otherwise.
    """

    genre: str
    lift: float
    support: float


__all__ = ["Centroid", "GenreAffinity"]
