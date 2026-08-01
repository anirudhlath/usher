# Emby fixtures — shape-recorded, value-synthetic

Read [`../README.md`](../README.md) first: it states the licensing rule, the
reserved identifier bands, and the guard that enforces them.

**Every value in these seven files is invented**, including the `ProviderIds`,
which until 2026-08-01 carried real TMDb/IMDb/TVDb ids for four real works.
That is a licensing constraint, not a style, and a doubled one: a real Emby
response *embeds TMDb-sourced metadata*, which TMDb's terms forbid
redistributing, and it also identifies a real library and carries real server
and user ids. `src/usher/adapters/emby/mapping.py`'s module docstring says the
same thing from the code's side.

**What is kept, deliberately, because it is Emby's protocol rather than
anyone's data:** field names and nesting; the `Type` vocabulary
(`Movie`/`Series`/`Episode`); `RunTimeTicks` as 100-nanosecond units; the
`.0000000Z` timestamp format; and — most load-bearing — the `VideoRange` /
`ExtendedVideoType` / `ExtendedVideoSubType` token vocabulary. Emby 4.9.5.0
emits `VideoRange ∈ {SDR, DolbyVision, HDR 10}` (with a space) and never
emits `VideoRangeType` or `DvProfile`; `Extended*` carries the **literal
string `"None"`**, not JSON null, so it is always truthy. Those tokens are
what the mapper switches on and were measured live, not guessed. Inventing
them would test nothing.

| File | Shaped to pin |
|---|---|
| `movie_item.json` | the ordinary movie; a Dolby Vision profile-8.1 stream; two audio streams where the **second** is `IsDefault` (so a mapper that took the first would report `aac`/2 instead of `truehd`/8) |
| `series_item.json` | `RunTimeTicks: null`, no `MediaSources` key at all (so a series yields no stream target), `UnplayedItemCount` |
| `episode_item.json` | an episode carrying **its own** provider ids rather than its series' — the payload fact behind "an episode never walks the match ladder" |
| `multi_version_movie.json` | three `MediaSources`, the first transcode-only (`Protocol: Http`, `Size: 0`, `SupportsDirectPlay: false`), so `primary_media_source`'s selection rule has something to select |

`multi_version_movie.json` has been looked for twice against the live server,
over disjoint slices totalling 1,400 movies, and has never met a real
payload — every one carried exactly one `MediaSource`. It stays: another
deployment will have them and the rule is cheap. `CLAUDE.md` records both
searches.

## Push messages (M5)

`push_user_data_changed.json`, `push_library_changed.json` and
`push_sessions.json` are the `/embywebsocket` envelope
(`{"MessageId", "MessageType", "Data"}`) for the three types
`usher.adapters.emby.push` reads. Every value is invented and every id is
inside the reserved bands above.

**These three have a far weaker provenance than the item fixtures beside
them, and it is stated rather than implied.** An item fixture was diffed
field by field against a live 4.9.5.0 response on 2026-07-31. These have
never met a real message, and what
[ADR-0004](../../../docs/prd/decisions/0004-push-over-polling.md)'s live run
of 2026-07-29 actually recorded is *which message types arrived* — `Sessions`
periodically, `UserDataChanged` twice on a played/unplayed toggle — and **not
one byte of any payload**. Everything below the `MessageType` line here is
transcribed from Emby's own `UserItemDataDto`, `LibraryUpdateInfo` and
`SessionInfoDto` and from the decompilation of `SessionWebSocketListener`,
which is documentation-grade evidence rather than measurement. Specifically
unverified, and named so M5's live verification has a list to work from:

| Guess | Why it is a guess |
|---|---|
| The envelope carries `MessageId` at all | Never observed; the run recorded types, not frames |
| `UserDataChanged.Data` is an object with `UserId` and `UserDataList` | Could be a bare list, as `Sessions`' `Data` is |
| A `UserDataList` entry is a `UserItemDataDto` (`ItemId`, `PlaybackPositionTicks`, `Played`, …) | The DTO's *shape* is documented; that this message carries it is inferred |
| `LibraryChanged.Data`'s five arrays hold **ids** rather than item objects | `LibraryUpdateInfo` declares `List<string>`; no capture confirms it, and `LibraryChanged` was never observed arriving at all |
| `Sessions.Data` is a list of session DTOs | Same |
| `Key` equals the item id | A field the mapper never reads, filled in for shape only |
| **A `UserDataChanged` entry is *correct* about `PlaybackPositionTicks` and `Played`** | Measured for the two payload shapes that exist: the item route is correct about all four fields and the **listing route is only partly** correct — right position and played flag, `PlayCount: 0`, no `LastPlayedDate` (2026-07-31). Nothing has measured the push entry, which is a *third* shape, so "it is correct about the two fields the adapter does read" is the same class of assumption the listing route already violated for the two it does not. `tests/fakes/emby_server.py`'s `user_data_changed_frame` renders the item's true position and played flag, and `SourceAdapterContract::test_events_yields_what_the_source_pushed` asserts on the position that comes back — so if a real entry zeroes the position, that case is green against an adapter that reports a wrong resume point. Capturing one entry for an item with a known non-zero position settles it. |
| `LibraryUpdateInfo.IsEmpty` means "no array carries anything" | Nothing reads it; `library_changed_frame` computes it from the three item arrays and ignores the three folder ones. A guess about a field with no consumer, recorded so the capture can settle it cheaply rather than so anything depends on it |
| One `MessageId` per message *type* | `tests/fakes/emby_server.py` reuses each fixture's `MessageId` for every frame it renders of that type, so nothing in this repository could catch a channel that deduplicated on it. Real Emby presumably mints one per message; nothing depends on either answer today |
| **`Sessions` arrives at least every 90 seconds on an idle library** | ADR-0004's run recorded that it arrives "periodically" and **not at what interval**. `DEFAULT_STALE_AFTER_SECONDS = 90.0` is the staleness watchdog's ceiling, so this is now the assumption a healthy idle lane depends on: if the real interval is longer, the watchdog tears down working sockets every 90 s forever. Mitigated rather than removed — reconnecting is cheap and correct (the gap-closing delta returns 0 items) and `usher.source.push.reconnects` makes it visible — and `push_stale_after_seconds` is a setting so an operator can raise it. Measuring the interval is a named step of M5's live verification. |
| **`"0,1000"` in `SessionsStart` is `initialDelayMs,intervalMs`, i.e. a one-second cadence** | The frame itself is verified (ADR-0004's session sent it and messages followed); the *reading* of its two numbers comes from the decompiled `SessionWebSocketListener` and no run has confirmed that the observed interval matches. If it is really seconds rather than milliseconds the guess above is far less comfortable. The same capture that measures the interval settles this. |
| **A working channel says something within 15 seconds** | `SourceAdapter.probe_push`'s default `timeout_seconds`. It is an operator-facing default rather than a correctness property — a probe that reports `delivering=False` on a healthy source is a misleading diagnostic, not a broken lane — and it rests on the same unmeasured interval. |

**The ids here are short numeric strings, and the item fixtures beside them
use 32-hex GUIDs.** That is deliberate and it is the *push* files that are
right: `src/usher/adapters/emby/mapping.py`'s module docstring records, from
the 2026-07-31 live diff, that "this server's item ids are short numeric
strings rather than 32-hex GUIDs" — the older fixtures get that wrong in a
way nothing depends on, and a new file had no reason to inherit it. They are
still inside the reserved band (≥ 90,000,000), so
`tests/unit/test_no_third_party_data.py` covers them as it covers every
other id here.

**The one thing that is verified live is the subscription frame**,
`{"MessageType":"SessionsStart","Data":"0,1000"}`, which ADR-0004's
end-to-end session sent before anything started arriving. It lives in
`usher.adapters.emby.push.SUBSCRIBE_FRAME`, not here.

`PlayCount` and `LastPlayedDate` appear on `push_user_data_changed.json`
**and the adapter deliberately ignores both**
([ADR-0014](../../../docs/prd/decisions/0014-absence-is-not-zero.md): a
`UserDataChanged` entry is a third payload shape and no run here has parsed
one), so their presence is a record of what the DTO is documented to carry
rather than a claim about what this server sends. They are in the fixture
precisely so that a mapper which started reading them fails a test.

Capturing real messages and diffing their shape against these three is a
named step of M5's live verification.

## Regenerating

Never paste a capture in. `scripts/capture_emby_fixture.py` replaces every
leaf with the name of its type, so its output is a shape to diff against, not
a fixture to commit:

```bash
export USHER_EMBY_URL=... USHER_EMBY_USER=... USHER_EMBY_PASSWORD=...
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
```

Diff that against the committed file and hand-add any missing **key** with an
invented value.

`tests/fakes/emby_server.py` uses these files as templates and overwrites the
identity fields from the seeded `SourceItem` — the item routes from the four
item fixtures, and `user_data_changed_frame`/`library_changed_frame`/
`sessions_frame` from the three push ones — so the contract suite is not
coupled to the values here. Rendering the push frames *from* these files
rather than from dicts built inline is what keeps the fake from drifting
away from the artefact the live capture diffs against; it is not evidence
about Emby, and the table above is the list of what is still unmeasured.

Read directly, with no fake server involved:
`tests/unit/test_adapters_emby_mapping.py`,
`tests/unit/test_adapters_emby_playback.py` (the item fixtures) and
`tests/unit/test_adapters_emby_push.py` (the push ones — a wrong field
*name* fails there even though a wrong envelope cannot).
`tests/unit/test_fakes_emby_server.py` reads them to assert the rendered
frames keep this shape.
