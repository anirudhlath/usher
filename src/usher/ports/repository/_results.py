"""The one result type more than one aggregate's port returns.

`BulkWriteResult` is returned by six ports across six modules. Homing it
in `bulk.py` -- where it was written, next to the first port to need it --
and importing it back the other way resolves perfectly well today, and
makes five aggregates drag the bulk-load port into every consumer. Private
and shared is the shape that does not; `test_ports_repository_package.py`
is what keeps it applied.
"""

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
