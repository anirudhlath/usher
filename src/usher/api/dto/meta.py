"""Response DTO for `GET /meta/attribution` (PRD 04's hard rule 4, PRD 07's Meta
table).
"""

from pydantic import BaseModel


class AttributionEntry(BaseModel):
    """One required attribution string and the source it belongs to."""

    source: str
    text: str
