"""Shared base for domain models."""

from typing import Self

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base for every Usher domain model."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    def evolve(self, **changes: object) -> Self:
        """Return a copy with `changes` applied, re-validated from scratch.

        `model_copy(update=...)` applies changes *without* validation — it
        can produce an invalid instance (wrong type, out-of-range value)
        that pydantic will still happily serialize. `evolve()` re-runs
        every field validator and the model's own `model_validator`s, so an
        invalid change raises immediately instead of reaching the wire.

        This is a runtime guarantee only, not a static one: `changes` is
        typed `object`, so `title.evolve(name=123)` still type-checks under
        mypy and only fails when this method actually runs. That's short of
        what a dedicated pydantic-aware mypy plugin could give a hand-typed
        `evolve` per model, which this project doesn't have. It is still
        strictly better than `model_copy(update=...)`, which validates
        nothing at either time.
        """
        return type(self).model_validate({**self.model_dump(), **changes})
