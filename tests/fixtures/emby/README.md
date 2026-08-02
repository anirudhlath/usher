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

**These three had a far weaker provenance than the item fixtures beside
them until 2026-08-02.** An item fixture was diffed field by field against a
live 4.9.5.0 response on 2026-07-31; these had never met a real message, and
what
[ADR-0004](../../../docs/prd/decisions/0004-push-over-polling.md)'s live run
of 2026-07-29 actually recorded is *which message types arrived* — `Sessions`
periodically, `UserDataChanged` twice on a played/unplayed toggle — and **not
one byte of any payload**. Everything below the `MessageType` line was
transcribed from Emby's own `UserItemDataDto`, `LibraryUpdateInfo` and
`SessionInfoDto` and from the decompilation of `SessionWebSocketListener`.

**M5's live verification captured all three and settled the table below.**
One socket held 70 minutes against the same 4.9.5.0 build, driving the
shipped `EmbyPushChannel` over the real `websockets` client: **146 frames —
134 `Sessions`, 5 `UserDataChanged`** (three of them from one restorative
write to a real account, two from the outage test) **and, for the first time
in this project's history, 7 `LibraryChanged`**. Four guesses were
**refuted** and the fixtures here now carry the measured shape.

| Guess | Verdict | Evidence |
|---|---|---|
| The envelope carries `MessageId` at all | **half confirmed, half refuted** | `UserDataChanged` and `LibraryChanged` carry one; **`Sessions` carries none**, on 65 of 65 frames. `push_sessions.json` no longer has one. |
| One `MessageId` per message *type* | **refuted** | A distinct 32-hex value (no dashes) per *message*: 3 messages, 3 ids. Nothing depends on either answer, and the fake still reuses the fixture's — recorded rather than fixed, because a channel that deduplicated on it is not a failure this repository can reach. |
| `UserDataChanged.Data` is an object with `UserId` and `UserDataList` | **confirmed** | Exactly those two keys, on every frame. |
| A `UserDataList` entry is a `UserItemDataDto` (`ItemId`, `PlaybackPositionTicks`, `Played`, …) | **confirmed, with two corrections** | Observed keys: `ItemId`, `PlaybackPositionTicks`, `Played`, `PlayCount`, `IsFavorite`, plus `PlayedPercentage` (a float, when the position is non-zero) and `LastPlayedDate` (when played). |
| `Key` equals the item id | **refuted — there is no `Key`** | Absent from every entry. Removed from the fixture, from `user_data_changed_frame`, and from the case that asserted it. |
| `UnplayedItemCount` on an entry | **not observed** | Absent from all five, all of them movies. Removed; a *series* entry is where it would plausibly appear and none was captured. |
| `LibraryChanged.Data`'s five arrays hold **ids** rather than item objects | **confirmed** | Seven real messages, all seven keys present on each, every array a list of id strings. One carried all six arrays non-empty at once (including a real `ItemsRemoved` on a library nothing was deleted from — ADR-0015's argument, observed) and one carried **42** `ItemsUpdated` ids against `push_max_items_per_event`'s default of 50. The shipped `to_source_events` produced 5 `ITEM_ADDED`, 3 `ITEM_UPDATED` and 1 `ITEM_REMOVED`: one event per non-empty array, live. |
| `Sessions.Data` is a list of session DTOs | **confirmed** | A list; entry keys are a superset of this fixture's, and the fixture now carries all of them (`AdditionalUsers`, `ApplicationVersion`, `DeviceId`, `InternalDeviceId`, `LastActivityDate`, `PlayableMediaTypes`, `PlaylistIndex`, `PlaylistLength`, `Protocol`, `RemoteEndPoint`, `ServerId`, `SupportedCommands`, `UserName`, and five more on `PlayState`) so the next diff is empty. |
| **A `UserDataChanged` entry is *correct* about `PlaybackPositionTicks` and `Played`** | **confirmed — and about `PlayCount` and `LastPlayedDate` too** | Compared against `GET /Users/{u}/Items/{item}` in the same second, across three transitions of one item: 613 s written → `PlaybackPositionTicks: 6130000000` with `Played: false`; marked played → `PlayCount: 1`, `Played: true`, and the *same* `LastPlayedDate` string the item route returned; restored → all zero. So this third shape is **not** the partly-honest one the listing route is, and the failure this row was written to catch — a zeroed position behind a green contract case — does not occur. The adapter still reports `play_count`/`last_played_at` as `None`; `usher.adapters.emby.mapping.user_data_states` says why. |
| `LibraryUpdateInfo.IsEmpty` means "no array carries anything" | **still unverified** | `false` on all seven, every one of which carried something. Consistent with the guess and not discriminating. Nothing reads it. |
| **`Sessions` arrives at least every 90 seconds on an idle library** | **confirmed for this deployment, and the mechanism is not what it looked like** | Median **34.7 s**, mean 31.6 s, p90 46.3 s, **max 60.1 s** over 133 intervals in 70 minutes, so `DEFAULT_STALE_AFTER_SECONDS = 90.0` holds with **1.5x** headroom. The worst gap grew with the window (52.6 s at 26 minutes), it is **change-driven, not periodic** (next row), and a 75-second probe earlier the same evening saw exactly one frame — so this is a measurement of one household at one hour rather than a protocol guarantee. `push_stale_after_seconds` stays a setting. |
| **`"0,1000"` in `SessionsStart` is `initialDelayMs,intervalMs`, i.e. a one-second cadence** | **confirmed, and it does not apply to an authenticated socket** | An *unauthenticated* connection receives `Sessions` at ~1 Hz (53 and 55 frames in 45 s) carrying the whole server's 83 sessions; the authenticated one received **one** in the same 45 s, carrying a 5-session row-filtered view. The timer is real; the filtered stream is sent when the filtered view changes. |
| **A working channel says something within 15 seconds** | **too tight on this deployment** | `SourceAdapter.probe_push`'s default `timeout_seconds`, against a median gap of 23.3 s, so `usher push --probe` will report `delivering=False` on a healthy idle source more often than not. It is a diagnostic default rather than a correctness property and the CLI takes an override; recorded here rather than changed, because raising it makes an operator wait longer for every answer including the true negatives. |

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
([ADR-0014](../../../docs/prd/decisions/0014-absence-is-not-zero.md)). Their
presence is now a record of what this server really sends — both were
measured truthful on 2026-08-02 — and they are in the fixture precisely so
that a mapper which started reading them fails a test. The reason the
adapter still does not is in
`usher.adapters.emby.mapping.user_data_states`: one movie, three
transitions, every one of them a write Usher itself made, is not a
measurement that a real entry never under-reports a history it did not
create, and writing a zero over one is permanent.

**What is still unmeasured, named rather than implied:** a `LibraryChanged`
with `IsEmpty: true`, and a `UserDataChanged` for a **series** entry — which
is where `UnplayedItemCount` would plausibly appear, and the reason it was
removed rather than kept with a note.

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
