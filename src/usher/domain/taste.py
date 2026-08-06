"""The taste centroid: one derived vector per user.

Declared here in Group A rather than with the service that computes it
(Group G, `services/taste.py`), because `ports/rows.py` must name the type on
`RowContext.taste` and a port may only name a domain type or one of its own.
The plan's file structure listed `db/models/taste.py`,
`db/repositories/taste.py` and `services/taste.py` and no `domain/taste.py`,
which is a correction Task 37 records.

PRD 06 defines the centroid as *"the mean embedding of recently watched and
highly rated titles"*. `watch_states` has **no rating column** -- none on
`WatchState`, none on `SourceWatchState`, and the Emby adapter reads neither
the user rating nor `IsFavorite` -- so M7 substitutes the engagement signal it
actually has (`played`, `play_count`, completion). Group G owns that
substitution, its correction to PRD 06, and the decision about whether it
earns an ADR of its own.
"""

import uuid
from dataclasses import dataclass

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel


class Centroid(DomainModel):
    """A user's taste, as one vector, with the evidence for its currency.

    `model_name` is the field with an argument attached: it records the
    embedding *runtime and checkpoint* (`fastembed:BAAI/bge-small-en-v1.5`),
    which is what makes a centroid computed under a different embedder
    detectable by `IS DISTINCT FROM` rather than by somebody remembering to
    write a migration. `ports/embedding.py`'s own `model_name` docstring makes
    the case at length, and a centroid is a derived vector with exactly the
    same staleness shape as a `title_embedding` -- ADR-0020.

    **A centroid over no titles is not constructible**, and that is ADR-0014
    applied to the taste signal rather than to a source's play history. A
    vector averaged over nothing is not "neutral taste": it is a point
    equidistant from everything, which makes every genre equally affine and
    every seed equally close -- a row that is noise wearing a reason. The
    honest value for a household that has watched nothing is
    `RowContext.taste = None`, so the zero-vector stand-in is refused here
    rather than guarded against at every reader.
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

    **In `domain/` rather than beside the service that computes it**, and the
    reason is `Centroid`'s exactly: `RowContext` carries it, `ports/rows.py`
    must name the type, and a port may name a domain type or one of its own
    and nothing else. `services/taste.py` re-exports it, because that is
    where it is *computed* and where every existing caller names it.

    A plain frozen dataclass rather than a `DomainModel`, unlike `Centroid`:
    it is never stored, never validated at a boundary, and never round-trips
    through a repository -- it is the return shape of one method.

    All three fields are read by `GenreAffinityProvider`: `lift` for the score,
    `genre` for the query and the sentence, and `support` because a row built
    from four titles and a row built from forty are different claims and the
    reason string must not pretend otherwise.
    """

    genre: str
    lift: float
    support: float


__all__ = ["Centroid", "GenreAffinity"]
