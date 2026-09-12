"""Usher-owned identifiers."""

import uuid

from uuid6 import uuid7


def new_id() -> uuid.UUID:
    """Generate a fresh time-ordered identifier."""
    return uuid7()
