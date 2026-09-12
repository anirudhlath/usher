"""The one result type more than one aggregate's port returns."""

from dataclasses import dataclass

__all__ = [
    "BulkWriteResult",
]


@dataclass(frozen=True, slots=True)
class BulkWriteResult:
    """What one batch write actually changed, split so a re-import is
    visibly a no-op (`inserted == 0`) rather than indistinguishable from a
    first run."""

    inserted: int
    updated: int
