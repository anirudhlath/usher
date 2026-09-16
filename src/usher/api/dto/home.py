"""`GET /home`'s wire shapes (PRD 06, PRD 07)."""

import uuid

from pydantic import BaseModel

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.rows import BuiltRow, DisplayHint, RowCard


class RowCardResponse(BaseModel):
    """One title on one shelf."""

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    enrichment_state: EnrichmentState
    owned: bool
    position_seconds: int
    runtime_seconds: int | None
    played: bool
    episode_id: uuid.UUID | None
    episode_label: str | None
    artwork: uuid.UUID | None

    @classmethod
    def of(cls, card: RowCard) -> "RowCardResponse":
        return cls(
            title_id=card.title_id,
            kind=card.kind,
            name=card.name,
            year=card.year,
            enrichment_state=card.enrichment_state,
            owned=card.owned,
            position_seconds=card.position_seconds,
            runtime_seconds=card.runtime_seconds,
            played=card.played,
            episode_id=card.episode_id,
            episode_label=card.episode_label,
            artwork=card.artwork,
        )


class RowResponse(BaseModel):
    """One shelf, rendered in the order given.

    `ttl` is deliberately absent. It is how long *the server* may reuse a built
    row, which is a server-side cost control rather than a client contract --
    and a client that saw it would cache against a number it has no way to know
    was invalidated early by the push lane. PRD 07's answer to freshness is
    `row.invalidated` over SSE, which is an instruction rather than a duration.

    `family` is absent for the same class of reason: it is the key the composer's
    diversity constraints are stated in, and a client that branched on it would be
    re-deciding a question the server has already settled.
    """

    slug: str
    title: str
    # `null` rather than `""` when a row has nothing to explain. An empty
    # string is a subtitle a client renders as a blank line, and it cannot be
    # told from a row that had something to say and said nothing. PRD 06: the
    # reason "is already written to be spoken aloud, not just displayed".
    reason: str | None
    display_hint: DisplayHint
    cards: tuple[RowCardResponse, ...]

    @classmethod
    def of(cls, row: BuiltRow) -> "RowResponse":
        return cls(
            slug=row.slug,
            title=row.title,
            reason=row.reason,
            display_hint=row.display_hint,
            cards=tuple(RowCardResponse.of(card) for card in row.cards),
        )


class HomeResponse(BaseModel):
    """The whole screen, in one response, with no cursor.

    An empty `rows` is a 200: `/home` is a screen rather than a resource, and a
    screen with nothing on it is a fact about the household. It is also
    **distinguishable**, which a padded one would not be -- a "popular titles"
    row on a household that has watched nothing produces a screen that looks
    personalised and is not.
    """

    rows: tuple[RowResponse, ...]

    @classmethod
    def of(cls, screen: tuple[BuiltRow, ...]) -> "HomeResponse":
        return cls(rows=tuple(RowResponse.of(row) for row in screen))


__all__ = ["HomeResponse", "RowCardResponse", "RowResponse"]
