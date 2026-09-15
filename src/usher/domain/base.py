"""Shared base for domain models."""

from typing import Self

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base for every Usher domain model."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    def evolve(self, **changes: object) -> Self:
        """Return a copy with `changes` applied, re-validated from scratch.

        The sanctioned write path. `model_copy(update=...)` skips validation
        and can hand back an invalid instance pydantic will still serialize;
        `evolve` re-runs every validator, so a bad change raises here rather
        than on the wire. `changes` is typed `object`, so the guarantee is a
        runtime one -- `title.evolve(name=123)` still type-checks.
        """
        return type(self).model_validate({**self.model_dump(), **changes})
