"""What a composed home screen is made of, once built."""

import uuid
from datetime import timedelta
from enum import StrEnum

from pydantic import Field

from usher.domain.base import DomainModel
from usher.domain.enums import EnrichmentState, TitleKind


class RowFamily(StrEnum):
    """PRD 06's row *family*, and the key the composer's diversity constraint is stated in.

    *"no three consecutive similarity rows; cap per family"*.
    """

    SOURCE = "source"
    SIMILARITY = "similarity"
    CURATED = "curated"


class DisplayHint(StrEnum):
    """ADR-0006's only concrete client vocabulary, and its whole of it.

    *"Rows carry a display **hint** (`portrait | landscape | wide | square`) but never a
    layout."*.

    Closed on purpose. The way this goes wrong is a fifth member -- `HERO`, or
    `GRID_3_COLUMN` -- which is a layout wearing a hint's name and which the
    server has no business specifying. A hint says what shape a card *is*; a
    layout says where a client should put it.
    """

    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"
    WIDE = "wide"
    SQUARE = "square"


class RowCard(DomainModel):
    """One title on one shelf, hydrated and ready to render."""

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None = None
    enrichment_state: EnrichmentState
    owned: bool = False
    # Zero is a *true* value here and not an ADR-0014 stand-in: a household
    # that has not started a title is genuinely nought seconds into it. The
    # absence that must not become zero is the runtime below.
    position_seconds: int = Field(default=0, ge=0)
    # `ge=0` rather than `gt=0`, matching `WatchState.runtime_seconds`
    # exactly. The refusal that matters is `None`, not the boundary: a
    # hydration that has no runtime must pass `None` and never `0`.
    runtime_seconds: int | None = Field(default=None, ge=0)
    # Rides along cheaply, because the alternative is a client inferring
    # "watched" from `position_seconds >= runtime_seconds` -- wrong for
    # exactly the titles whose runtime is `None`, which are the ones that
    # most need the badge.
    played: bool = False
    # **Two nullable fields for the two rows that are about a chapter rather than about
    # a title**, added by Group G/H because `NextUpProvider`'s own headline case asserts
    # on the label and `ContinueWatchingProvider` has to be able to resume an episode
    # file.
    episode_id: uuid.UUID | None = None
    episode_label: str | None = None
    # **M9's field, and the one the module docstring is about.** An `images.id`,
    # resolvable through `GET /images/{id}` and nothing else -- never a path, never a
    # URL, never a list.
    artwork: uuid.UUID | None = None


class BuiltRow(DomainModel):
    """One shelf, built: what a client renders in the order it is given.

    **Constructible with no cards, on purpose.** An empty row and an absent
    row are different states. Were `cards` to carry `min_length=1`,
    `Row.build()` would have to return `BuiltRow | None`, and then "this row
    built and had nothing to show" and "this row was never proposed" collapse
    into one `None` -- a quiet household and a dead provider respectively,
    which Group I's metrics have to tell apart. `Row.empty()` is a real method
    returning a real value only because of this.

    `cards` is a tuple rather than a list for two reasons: `DomainModel`'s
    docstring notes that a model with a `list`/`dict` field is unhashable even
    when frozen, and a cached `BuiltRow` handed to two concurrent requests
    must not be mutable by either.
    """

    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    # PRD 06: "the `reason` field is already written to be spoken aloud, not just
    # displayed" -- Alfred reads it out.
    reason: str | None = None
    family: RowFamily
    # On the row, never on the card: a hint describes the shelf, and a card
    # carrying one lets a single row disagree with itself about its own shape,
    # which is a per-item layout instruction arriving by a second route.
    display_hint: DisplayHint
    # On the value rather than on the `Row` class where PRD 06's sketch puts it, and
    # required rather than defaulted.
    ttl: timedelta
    cards: tuple[RowCard, ...] = ()


__all__ = ["BuiltRow", "DisplayHint", "RowCard", "RowFamily"]
