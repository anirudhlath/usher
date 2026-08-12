"""`GET /home` — [ADR-0006](../../../../docs/prd/decisions/0006-server-composed-home.md),
PRD 06 and PRD 07.

**A card carries one artwork reference, and it is an `images.id` -- resolvable
through `GET /images/{id}` and nothing else.** M7 shipped no such field at all,
absent rather than null, on the argument M5 wrote for `GET /titles/{id}`'s
absent `images` key: an always-null field is a client-side branch that never
takes its other arm, and *"the day M9 lands the image proxy and its table,
every client that shipped against the null already renders without it."* That
day is this one -- the table, the derivation and the proxy are all M9's -- so
the field arrives with values in it and a client written against its absence is
untouched.

**Never a URL and never a path.** A URL would put this deployment's CDN base
and ADR-0032's ladder rung inside a screen a client caches for thirty seconds;
a path is provider vocabulary a client would have to know how to assemble.
`GET /images/{id}` is the one place either is decided, which is also what makes
the reference survive a re-derivation (`m09c`'s natural key).

**One id, chosen against the row's `display_hint`** -- a poster for
`portrait`/`square`, a backdrop for `landscape`/`wide`. A list would be the
client re-deciding a question ADR-0006 puts on the server, and it could not
decide it anyway: the hint lives on the *row*, one level above the card.
`null` means the catalog holds no image of that kind for the title, which is
the ordinary state of a title nothing has derived yet.

**No cursor.** ADR-0006 composes a *screen*, and PRD 07's endpoint table gives
`/browse` a cursor and gives `/home` none. Paging through rows would be a
browse under a screen's name. Pagination on this surface is M9's.

**`display_hint` is a hint, never a layout** -- ADR-0006's only concrete
vocabulary, `portrait | landscape | wide | square`. A hint is what a card *is*
shaped like; a layout is how many fit, how they scroll, and what a client does
at 320 px. Nothing here carries a column count, a card width or a visible-row
count, and the day one is asked for, ADR-0006's own mitigation is the answer:
an optional layout *profile* that constrains composition, which is strictly
additive.

**`DisplayHint` is the domain enum, not a wire twin, and that is the opposite
of what `dto/events.py` does.** The reason is `response_model`: this route has
one, so `/openapi.json` describes the vocabulary and a rename is a visible
schema diff *plus* a mypy error here -- which is the deliberateness PRD 07 asks
for. `SseEventKind` exists precisely because a `StreamingResponse` is bytes and
FastAPI's serializer never sees it, so that surface has no schema for a rename
to be visible in. Same for `TitleKind` and `EnrichmentState`, which
`dto/title.py` already reuses.

**No `external_id`, and no source concept at all.** PRD 07's first line, and
`RowCard` carries none to begin with -- a card names a `Title.id`, which is
every route a client can call.
"""

import uuid

from pydantic import BaseModel

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.rows import BuiltRow, DisplayHint, RowCard


class RowCardResponse(BaseModel):
    """One title on one shelf.

    The progress pair is two facts rather than one fraction, exactly as
    `RowCard` carries them: `runtime_seconds` is nullable, so a fraction is
    either a division by `None` or a division by a `COALESCE`d zero -- and the
    latter renders every partially-watched title as finished. *"Half an hour
    in, of an unknown total"* is two true facts (ADR-0014, at the card).

    `episode_id` and `episode_label` ride **alongside** `title_id`, which stays
    the *series*: every other field here describes the series, so a `title_id`
    that sometimes meant an episode would be a second vocabulary in the one
    field every provider's cards agree on. `episode_id` is what makes a Next Up
    card playable rather than merely navigable; `episode_label` is composed on
    the server so the zero-padding is decided once instead of by each client.
    Both are `null` on every card of the other seven rows.

    `artwork` is an **image id**, not a URL and not a path: a client renders it
    by asking `GET /images/{id}`, which is where the CDN base, the ladder rung
    and the cache headers live. `null` is a real answer -- no poster (or, on a
    `landscape` row, no backdrop) is known for the title -- and unlike the
    fields above it is the *common* answer on a catalog nothing has derived,
    which is why the branch is a branch and not decoration.
    """

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

    `family` is absent for the same class of reason: it is the key the
    composer's diversity constraints are stated in, and a client that branched
    on it would be re-deciding a question ADR-0006 put on the server.
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
