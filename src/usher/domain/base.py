"""Shared base for domain models."""

from typing import Self

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base for every Usher domain model.

    ``frozen=True``: instances are immutable once constructed. The write
    path is `.evolve()`, never `model_copy(update=...)` — the latter skips
    validation entirely and can hand back an instance with a wrong-typed or
    out-of-range field that serializes fine and only fails much later, on
    the way back in. See `evolve` below.

    ``extra="forbid"``: adapters hand-map dozens of provider fields onto
    these models by keyword. A typo'd field name (`tmbd_id=` for
    `tmdb_id=`) must fail loudly at construction, not be silently dropped —
    this is the same standard `usher.config.Settings` already holds.

    Note on hashability: a model with a `dict[...]` field is unhashable
    even though it is frozen — Python cannot hash a dict, and pydantic's
    generated `__hash__` hashes every field's value. `Title` carries
    `field_provenance: dict[str, str]` and is therefore the one domain
    model in this set that is *not* hashable; the other four carry no dict
    or list field and are. This asymmetry is intentional — see
    `Title`'s own docstring — and its failure mode is a loud, immediate
    `TypeError` from `hash()`, not silent corruption.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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
