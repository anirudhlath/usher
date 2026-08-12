"""The SSE wire format."""

import json
import uuid

import pytest

from usher.api.dto.events import _WIRE, SseEventKind, encode_sse, parse_titles
from usher.ports.events import ClientEvent, ClientEventKind
from usher.services.events import SentEvent


def _sent(event: ClientEvent, *, event_id: int = 7, epoch: str = "abcd1234") -> SentEvent:
    return SentEvent(id=event_id, epoch=epoch, event=event)


def test_a_frame_carries_an_id_an_event_and_a_json_data_line() -> None:
    """The three fields an `EventSource` reads. `id:` is what comes back as
    `Last-Event-ID`, so the epoch has to be in it -- the bus's whole
    unknown-epoch branch is unreachable otherwise."""
    frame = encode_sse(
        _sent(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, data={"fields": ["overview"]}))
    )
    lines = frame.split("\n")
    assert lines[0] == "id: abcd1234-7"
    assert lines[1] == "event: title.updated"
    assert json.loads(lines[2].removeprefix("data: ")) == {"fields": ["overview"]}
    assert frame.endswith("\n\n"), "an SSE frame is terminated by a blank line"


def test_a_title_id_is_on_the_data_line_not_only_in_the_filter() -> None:
    """A client subscribed to three titles has to know which one changed.
    The `?titles=` filter decides *whether* a frame is sent, not what it
    says."""
    title_id = uuid.uuid4()
    frame = encode_sse(_sent(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, title_id=title_id)))
    payload = json.loads(frame.split("\n")[2].removeprefix("data: "))
    assert payload["title_id"] == str(title_id)
    assert "episode_id" not in payload


def test_an_episode_event_carries_both_ids() -> None:
    title_id, episode_id = uuid.uuid4(), uuid.uuid4()
    frame = encode_sse(
        _sent(
            ClientEvent(
                kind=ClientEventKind.WATCHSTATE_UPDATED,
                title_id=title_id,
                episode_id=episode_id,
                data={"played": True},
            )
        )
    )
    payload = json.loads(frame.split("\n")[2].removeprefix("data: "))
    assert payload == {"played": True, "title_id": str(title_id), "episode_id": str(episode_id)}


def test_a_data_payload_never_spans_two_lines() -> None:
    """A newline inside `data:` is a second `data:` line to an SSE parser,
    and an unescaped one silently truncates the frame. `json.dumps` escapes
    them; this is the case that stops somebody replacing it with an f-string.
    """
    frame = encode_sse(
        _sent(ClientEvent(kind=ClientEventKind.SYNC_PROGRESS, data={"error": "line one\nline two"}))
    )
    assert len([line for line in frame.split("\n") if line.startswith("data: ")]) == 1
    # The escaping is not merely "one line" -- the payload has to survive it.
    payload = json.loads(frame.split("\n")[2].removeprefix("data: "))
    assert payload == {"error": "line one\nline two"}


def test_the_wire_vocabulary_is_its_own_enum() -> None:
    """`api/dto/` types are distinct from domain models (PRD 07: "Internal
    refactors don't break clients; wire changes are deliberate"). This is
    the assertion that makes renaming `ClientEventKind.TITLE_UPDATED` a
    compile-time decision rather than a silent wire break."""
    assert {kind.value for kind in SseEventKind} == {
        "title.updated",
        "watchstate.updated",
        "row.invalidated",
        "sync.progress",
        "bootstrap.progress",
        "resync_required",
    }


def test_every_internal_kind_has_a_wire_name() -> None:
    """The mapping is total in the direction that matters. A
    `ClientEventKind` with no wire member is a `KeyError` in the middle of a
    stream that has already answered 200 -- there is no status code left to
    say so with, which is exactly why the SSE vocabulary is pinned as an
    enum rather than deferred to PRD 07's problem-details envelope."""
    for kind in ClientEventKind:
        frame = encode_sse(_sent(ClientEvent(kind=kind)))
        assert f"event: {kind.value}\n" in frame


def test_no_titles_filter_means_everything_rather_than_nothing() -> None:
    """An absent `?titles=` is an admin UI wanting `sync.progress`, which
    belongs to no title. `frozenset()` would be the opposite answer -- a
    stream that never fires."""
    assert parse_titles(None) is None
    assert parse_titles("") is None


def test_a_titles_filter_parses_a_comma_separated_list() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    assert parse_titles(f"{first}, {second}") == frozenset({first, second})


def test_a_malformed_title_id_raises_rather_than_being_dropped() -> None:
    """Silently dropping the unparseable half of `?titles=a,not-a-uuid`
    leaves a filter that is *narrower* than the client asked for, so a
    detail screen quietly never updates. The route turns this into a 422."""
    with pytest.raises(ValueError, match=r"badly formed|invalid"):
        parse_titles(f"{uuid.uuid4()},not-a-uuid")


def test_the_wire_map_is_total_over_the_internal_enum_directly() -> None:
    """**The direct spelling of the guard above it**, and it is a *second* case
    rather than a replacement.

    The plan for this milestone claimed `_WIRE` had no exhaustiveness guard at
    all -- that adding a member to both enums and forgetting the mapping "passes
    mypy and the whole suite, then raises `KeyError` inside a response that has
    already answered `200 text/event-stream`". **That is refuted**: M5's
    `test_every_internal_kind_has_a_wire_name` encodes every kind through
    `encode_sse`, so a missing entry raises `KeyError` there and the case fails.
    Measured, not reasoned about, by making exactly that mutation.

    This one is kept anyway because it fails *differently*: on a set comparison
    naming the missing member, rather than on a `KeyError` from inside a
    formatter, which is the difference between a diagnosis and a symptom.
    """
    assert set(_WIRE) == set(ClientEventKind)


def test_a_row_invalidation_carries_its_slug_on_the_data_line() -> None:
    """PRD 07's payload is "Row slug" and its client action is "Refetch that
    row" -- so the slug is the whole payload, and a frame without it is an
    instruction with no object. Kills an event published with an empty `data`,
    which is a well-shaped frame that tells a client nothing."""
    frame = encode_sse(
        _sent(ClientEvent(kind=ClientEventKind.ROW_INVALIDATED, data={"slug": "continue-watching"}))
    )

    lines = frame.split("\n")
    assert lines[1] == "event: row.invalidated"
    assert json.loads(lines[2].removeprefix("data: ")) == {"slug": "continue-watching"}


def test_a_row_invalidation_carries_no_title_id() -> None:
    """A row is not a title. `title_id` on this event would be a filter key that
    half-works -- it would wake exactly the detail screens subscribed to
    whichever title happened to be attached, which is neither "every subscriber"
    nor "the right ones"."""
    frame = encode_sse(
        _sent(ClientEvent(kind=ClientEventKind.ROW_INVALIDATED, data={"slug": "next-up"}))
    )

    assert "title_id" not in json.loads(frame.split("\n")[2].removeprefix("data: "))


def test_a_bootstrap_progress_frame_carries_its_cursor_and_no_title_id() -> None:
    """PRD 07's payload column for this row is corrected in M9's E7 from
    *"Phase, percent"* to what a `BulkCursor` can honestly supply, and this is
    that correction on the wire.

    **No `title_id` on the data line and none in the filter**, which is what
    makes *"Admin UI only"* a property: the frame reaches unfiltered
    subscribers and no others, exactly as `row.invalidated` does and for the
    inverse reason -- a bulk import touches most of the catalog, so a title id
    here would wake every detail screen in the household once per batch.

    Kills a frame published with an empty `data`, which is a well-shaped SSE
    event that tells a progress bar nothing.
    """
    frame = encode_sse(
        _sent(
            ClientEvent(
                kind=ClientEventKind.BOOTSTRAP_PROGRESS,
                data={
                    "dataset": "imdb.title.basics",
                    "phase": "imdb",
                    "rows_seen": 100000,
                    "rows_written": 100000,
                    "position": 1043771,
                },
            )
        )
    )

    lines = frame.split("\n")
    assert lines[1] == "event: bootstrap.progress"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload["dataset"] == "imdb.title.basics"
    assert payload["phase"] == "imdb"
    assert payload["rows_seen"] == 100000
    assert payload["position"] == 1043771
    assert "title_id" not in payload
    assert "percent" not in payload
