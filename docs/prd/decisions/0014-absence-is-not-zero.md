# ADR-0014 — Absence is not zero on `SourceWatchState`

**Status:** Accepted

## Context

`SourceAdapter.watch_state()` walks a source's listing. Verified 2026-07-31
against Emby 4.9.5.0, a `GET /Users/{user}/Items` listing reports
`PlayCount: 0` and omits `LastPlayedDate` entirely, for the very item whose
`GET /Users/{user}/Items/{item}` reports `PlayCount: 2` and a real
`LastPlayedDate`. `PlaybackPositionTicks` and `Played` are correct in both.
Neither `Fields=UserDataPlayState`, `Fields=UserData`, `EnableUserData=true`,
nor restricting the listing to explicit `Ids` changes it.

`SourceWatchState.play_count` was `int` with a default of `0`, so the walk
had no way to say anything other than "zero plays". M4 is the first caller
that writes these fields. Recovering them per item costs one request each
against a library measured at 1,126,674 items.

## Decision

`play_count: int | None = None` and `last_played_at: AwareDatetime | None =
None`. `None` means "this read could not determine it"; `0` and a real
datetime are positive claims a caller must honour.

`SourceAdapter` gains `get_watch_state(external_id) -> SourceWatchState |
None` — one request, the authoritative read, `None` for a deleted item and
a raise for an unreachable source, matching `get_item` exactly.

Inside the Emby adapter the distinction is carried by
`to_watch_state(..., play_history_is_trustworthy: bool)`, which **names the
route rather than a preference**: the listing walk passes `False`,
`get_watch_state` passes `True`. It has no default, so a call site that has
not thought about which route it read from does not type-check. Trusting the
route is still not inventing a value — a payload with no `PlayCount` key
yields `None` even under `True`.

`WatchStateRepository.merge_from_source` is `COALESCE`-shaped: `None` leaves
the stored column untouched, `0` overwrites it. Recovering history is a
queued backfill over `played = true AND play_count = 0`, bounded by the
household's watched items rather than by the library.

## Consequences

**Gained:** a walk cannot zero real play history, because the value it
carries is not a number. The contract suite can state the rule
source-agnostically — a reported number must be true, but presence is never
required — so a source whose listing *is* complete (Jellyfin may well be) is
not forced to lie in either direction.

**Given up:** every read site needs a null check, and `SourceAdapter` grows
a tenth abstract method every future adapter must implement.
`watch_states.play_count` stays `int NOT NULL DEFAULT 0`, so
"played but count unknown" is spelled `played AND play_count = 0` rather
than `IS NULL` — very slightly lossy, self-healing after one background
request, and far cheaper than pushing three-valued play counts through
`WatchState`, PRD 02, every API response, and every dashboard query.

**Rejected:** leaving the port alone and making M4 simply not write the
fields. That is a rule in a docstring rather than a property of a type, and
M5's push lane re-walks `watch_state(since=…)` with nothing to stop it
inheriting the same trap.

## Evidence

`tests/contract/source_adapter_contract.py::
SourceAdapterContract::test_a_walk_never_reports_play_history_it_cannot_know`
asserts the walked value is `None` *or* the seeded value and nothing else.
It passes on the `== 7` branch against `FakeSourceAdapter` and on the `is
None` branch against `EmbyAdapter` over `FakeEmbyServer` — both branches
live across the two runs.

Mutation-tested seven ways, each killing exactly the case written for it:
against `FakeSourceAdapter`, rebuilding `get_watch_state`'s answer without
history, yielding `play_count=0` from the walk, dropping the unknown-id
guard, and dropping the readiness check; against the Emby adapter, the walk
passing `play_history_is_trustworthy=True`.

The two that matter most are the pair that could drift together — the
adapter trusting the listing, and `FakeEmbyServer` rendering a listing that
carries history:

| adapter trusts listing | fake listing carries history | result |
|---|---|---|
| no | no | green |
| **yes** | no | `test_the_walk_reports_absent_play_history` and the contract case both fail |
| no | **yes** | `test_the_listing_route_omits_the_play_history_the_item_route_carries` fails |
| **yes** | **yes** | the first and third fail |

The third row is the one worth recording: with only the adapter-level tests
in place, the fake drifting back into agreement with the adapter was
**invisible** — a correct adapter discards those fields whatever the fake
supplies, so nothing objected. That is the exact shape of the M3 write-back
failure, where `FakeEmbyServer` implemented the adapter's own guess and 40
contract assertions passed against a route that had never worked. It is
closed by pinning the fake against the *measurement* directly, in
`tests/unit/test_fakes_emby_server.py`, rather than only through the
adapter. A live run remains the only thing that can catch all three files
being wrong together, which is why M4's definition of done keeps one.
