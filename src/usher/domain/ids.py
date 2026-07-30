"""Usher-owned identifiers.

UUIDv7 rather than v4: time-ordered, so index locality stays good during the
bulk imports that insert millions of rows. See ADR-0003.
"""

import uuid

from uuid6 import uuid7


def new_id() -> uuid.UUID:
    """Generate a fresh time-ordered identifier."""
    return uuid7()
