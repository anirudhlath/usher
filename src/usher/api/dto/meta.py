"""Response DTO for `GET /meta/attribution` (PRD 04's hard rule 4, PRD 07's
Meta table).

A flat list of `{source, text}`, not a mapping keyed by `BulkDataset.name` --
that alternative puts a dataset-key vocabulary on the wire that has no other
client-facing use, and `routers/meta.py` is the only reader either way.
"""

from pydantic import BaseModel


class AttributionEntry(BaseModel):
    """One required attribution string and the source it belongs to."""

    source: str
    text: str
