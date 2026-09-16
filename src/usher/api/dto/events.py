"""The SSE wire format (PRD 07)."""

import json
import uuid
from enum import StrEnum
from typing import Any

from usher.ports.events import ClientEventKind
from usher.services.events import SentEvent


class SseEventKind(StrEnum):
    """PRD 07's event names, on the wire."""

    TITLE_UPDATED = "title.updated"
    WATCHSTATE_UPDATED = "watchstate.updated"
    ROW_INVALIDATED = "row.invalidated"
    SYNC_PROGRESS = "sync.progress"
    BOOTSTRAP_PROGRESS = "bootstrap.progress"
    RESYNC_REQUIRED = "resync_required"


# Exhaustive by convention and by two cases: an internal kind with no wire name is a
# `KeyError` raised mid-response, after 200, where there is no status code left to
# report it with -- and **mypy does not check a dict literal for exhaustiveness over
# its key enum**, so nothing here makes it true by construction.
_WIRE: dict[ClientEventKind, SseEventKind] = {
    ClientEventKind.TITLE_UPDATED: SseEventKind.TITLE_UPDATED,
    ClientEventKind.WATCHSTATE_UPDATED: SseEventKind.WATCHSTATE_UPDATED,
    ClientEventKind.ROW_INVALIDATED: SseEventKind.ROW_INVALIDATED,
    ClientEventKind.SYNC_PROGRESS: SseEventKind.SYNC_PROGRESS,
    ClientEventKind.BOOTSTRAP_PROGRESS: SseEventKind.BOOTSTRAP_PROGRESS,
    ClientEventKind.RESYNC_REQUIRED: SseEventKind.RESYNC_REQUIRED,
}


def encode_sse(sent: SentEvent) -> str:
    """One `SentEvent` as an SSE frame.

    `id:` carries the epoch, which is what makes the bus's unknown-epoch
    branch reachable at all: a browser sends this value back verbatim as
    `Last-Event-ID`, and without the epoch a restarted server cannot tell a
    resume from a different sequence's id.

    `data:` is one line, always. A newline inside a payload is a second
    `data:` line to an SSE parser and silently truncates the frame, so
    `json.dumps` -- which escapes them -- is load-bearing rather than
    stylistic.
    """
    payload: dict[str, Any] = dict(sent.event.data)
    if sent.event.title_id is not None:
        payload["title_id"] = str(sent.event.title_id)
    if sent.event.episode_id is not None:
        payload["episode_id"] = str(sent.event.episode_id)
    return (
        f"id: {sent.epoch}-{sent.id}\n"
        f"event: {_WIRE[sent.event.kind].value}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )


def parse_titles(raw: str | None) -> frozenset[uuid.UUID] | None:
    """`?titles=id1,id2` into a filter, or `None` for "everything".

    Raises `ValueError` for anything that is not a uuid; the route turns that
    into a 422 whose detail names the *rule* and never the submitted value --
    `usher.api.errors` strips `input` from every validation error app-wide for
    the same reason, and PRD 08's "a rejected request never echoes the body it
    rejected" does not stop being true for a query string.

    **Raising rather than dropping the bad entries**, which is the difference
    between an error and a stream that silently never fires: a filter built
    from the half of `?titles=a,not-a-uuid` that parsed is *narrower* than the
    client asked for, and an empty one matches nothing at all.
    """
    if raw is None or raw == "":
        return None
    return frozenset(uuid.UUID(part.strip()) for part in raw.split(","))
