---
paths:
  - "src/usher/adapters/emby/**"
  - "src/usher/services/push.py"
  - "src/usher/services/ingest.py"
  - "src/usher/services/matching.py"
  - "src/usher/services/reconcile.py"
  - "src/usher/services/watch_sync.py"
---

# Emby, the push lane, and the ingest pipeline

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**Emby push works.** Verified 2026-07-29 against the live server with a normal
non-admin token: `/embywebsocket` upgrades (101), delivers periodic `Sessions`,
and pushes `UserDataChanged` within seconds of an out-of-band state change. Two
earlier negative findings were both wrong — see
[ADR-0004](../../docs/prd/decisions/0004-push-over-polling.md).

Health-check caveat: a handshake against *any* path succeeds, so a successful
upgrade is not a health signal. Assert on received messages instead.
**Re-measured 2026-08-02 and it is worse than that**: a socket carrying **no
credential at all** upgrades, accepts the subscription, and then delivers
`Sessions` *more* often than an authenticated one. So neither an upgrade nor
arriving messages establish that a channel is the one you think it is —
[ADR-0018](../../docs/prd/decisions/0018-push-health-is-a-message-ledger.md), and
the whole M5 live section below.
**A supervisor that resets its failure counter on connection is caught only
if the fake it is tested against has an *unbounded* supply of connections.**
`PushSupervisor` resets on delivery, and the mutation that moves the reset to
the connection is exactly the failure ADR-0004's caveat predicts: a proxy
that upgrades and buffers connects perfectly every time, so the ceiling is
never reached and PRD 08's "after N failures mark `supports_push = false`"
silently never fires. A scripted adapter whose list of connections *runs out*
terminates that mutated loop for the wrong reason and lets it pass. The fake
in `tests/unit/test_services_push.py` therefore hands out empty connections
forever and caps its own attempts with a plain `AssertionError` — never a
`UsherPortError`, so the supervisor cannot catch it — and the mutation fails
**4 cases in 0.43 s** with "the supervisor opened 41 channels; it is not
counting failures". Without that cap it would *hang*: `asyncio.wait_for`
cannot bound a loop that never yields, and the injected sleep therefore also
`await asyncio.sleep(0)`s.
**"Connect, then close the gap" is a concurrency claim and an ordering
assertion does not test it.** `order == ["connected", "gap"]` is what a
serialised run produces too, and it passes against an implementation that
connects, closes the socket, and then walks. The case that has teeth forces a
real 40 ms gap walk against a producer emitting on the open socket for ~30 ms
and asserts on measured intersection-over-union of the two windows —
**62.6% on this host, stable over five runs** (compare `JobQueueContract`'s
76.2% and M5 group B1's 80.3–85.4%) — plus "every event produced during the
walk was still delivered".
**Three obvious assertions about `SourceNotSupported` all survive its own
mutation.** Deleting the supervisor's `except SourceNotSupported` arm and
letting it fall through to `except UsherPortError` ends with
`push_available == [False]`, `push_connections == 0` and `gaps == 0` — the
ceiling is reached instead of the method returning, so every visible end
state is identical and only the five wasted attempts and four backoff sleeps
differ. Measured; the M5 plan's own draft asserted exactly those three and
the mutation survived it. Assert on `attempts == 1` and `sleeps == []`.
**`PushHealth.record_reconnect` was a method nothing in `src/` ever called**,
so PRD 10's `usher.source.push.reconnects` would have plotted a flat zero for
every source forever. The increment belongs in `record_open`, guarded on
`opened_at is not None` — on the second and later *open*, not on a failure,
because a lane that failed to connect five times and then succeeded
reconnected *once*. Both the unguarded version (every source starts at 1) and
the absent version are pinned.
**A push merge's `observed_at` mutation survives the whole unit file and is
killed only by real Postgres.** Measured 2026-08-01: replacing
`PushApplyService`'s `datetime.now(UTC)` with a plausible earlier instant —
the event's own timestamp, the last walk's `started_at` — passes all of
`tests/unit/test_services_push.py` and fails
`tests/integration/test_services_push.py`, because
`FakeWatchStateRepository` stores `observed_at` as `updated_at` while
`trg_watch_states_set_updated_at` owns that column in Postgres. Same trap
`backfill_one` documents, one lane over, and the reason that integration file
exists at all.
**The M5 plan's own self-review found a real bug and it is worth the general
form.** `_publish_watch_states` zipped the *matched subset* of targets
against the whole batch of states, so one unmatched item — which PRD 02
guarantees there will always be — shifted every pair by one and published
item A's resume position under item B's title id. Recovering a pairing
outside the loop that built it is the failure; `WatchStateSyncService`
therefore returns `MergedState(external_id, target)` and the pairing cannot
be reconstructed wrongly. Same rule `SourceEvent.watch_states` states one
layer up: keyed, never aligned by position.
**M5's live verification: the first real `/embywebsocket` message this
repository has ever parsed, and four of thirteen documented guesses were
wrong.** Run 2026-08-02 against the same live Emby **4.9.5.0** server,
driving the shipped `EmbyAdapter` → `EmbyPushChannel` → `connect_websocket`
→ real `websockets`, and for the long hold the shipped `PushSupervisor` with
recording callables in place of the three unit-of-work ones. From a
throwaway script outside the working tree, holding the operator's existing
token (no password, so `AuthenticateByName` was again not exercised).
**Bounded deliberately: one long-lived socket held 100 minutes, eight
short-lived probe sockets, and 14 HTTP requests in total** — no walk of any
kind, because the library is 1,126,789 items. The long socket received
**200 frames — 183 `Sessions`, 12 `LibraryChanged`, 5 `UserDataChanged` —
with zero reconnects, zero unforced failures, and `supports_push` true
throughout**, and the shipped mapper turned them into 20 `SourceEvent`s.

- **The envelope is not uniform, and that is the first correction.**
  `UserDataChanged` and `LibraryChanged` carry
  `{MessageId, MessageType, Data}` with a **distinct 32-hex `MessageId` per
  message** (not per type — 17 carried one, 17 distinct); **`Sessions`
  carries `{MessageType, Data}` and no `MessageId` at all**, on 183 of 183
  frames. `tests/fixtures/emby/
  push_sessions.json` claimed one and no longer does.
- **A real `UserDataChanged` entry is honest, including about play
  history.** One item, three transitions, each compared against
  `GET /Users/{u}/Items/{item}` in the same second: `PlaybackPositionTicks`
  6,130,000,000 (the 613 s written) with `Played: false`; then
  `PlayCount: 1`, `Played: true`, `LastPlayedDate` — *the same timestamp the
  item route returned*; then all-zero after the restore. **So the pushed
  shape is not the partly-honest one the listing route is**, and the
  M5-blocking failure this run existed to look for — an entry that zeroes
  the position, so the adapter reports a wrong resume point while the
  contract case stays green — **did not happen**. Through the shipped
  mapper, the first event carried `position_seconds=613`.
- **`play_count`/`last_played_at` stay `None` anyway, and that is a
  deliberate lag rather than an oversight.** ADR-0014's rule is that a
  reported number must be *true*, and the evidence here is one item across
  three transitions, all of them writes Usher itself made, on an item whose
  history was zero to begin with. The failure it guards against needs an
  entry reporting `0` for an item whose true count is 13, which this run
  could not produce without touching real history. Turning the field on is a
  measured opportunity worth one `watch_history` job per played item;
  recorded, not taken.
- **`LibraryChanged` arrives, its arrays hold ids, and one of them arrived
  carrying all six at once.** Never observed before this run; **twelve**
  arrived unprompted during the hold, with all seven documented keys
  (`ItemsAdded`/`ItemsUpdated`/`ItemsRemoved`,
  `FoldersAddedTo`/`FoldersRemovedFrom`, `CollectionFolders`, `IsEmpty`) and
  every array a **list of id strings** rather than of item objects. The
  committed fixture's shape was already right, field for field. The shipped
  `to_source_events` produced 7 `ITEM_ADDED`, 7 `ITEM_UPDATED` and 1
  `ITEM_REMOVED` from them — one event per non-empty array, live.
- **`ItemsRemoved` fires on a library from which nothing was removed, and
  that is ADR-0015's argument arriving as a measurement rather than as an
  argument.** Nobody deleted anything from this server during the 100-minute hold, and
  one frame still named an item in `ItemsRemoved` (alongside
  `FoldersRemovedFrom`, `ItemsAdded`, `FoldersAddedTo`, `CollectionFolders`
  and ten `ItemsUpdated`). M5 counts it and retracts nothing; had it
  retracted, one ordinary library refresh would have marked a present file
  unavailable.
- **A real `ItemsUpdated` batch reached 42 ids**, against
  `push_max_items_per_event`'s default of **50**. So the ceiling is not
  theoretical headroom over a hypothetical event — real traffic on an
  otherwise idle server comes within 16% of it, and the batch below it costs
  42 `get_item` calls applied inline. Raising the default would buy little
  and the deferral path (a delta walk) is the cheaper answer above it, which
  is the shape M5 already ships; recorded so the number is chosen against
  data next time it is chosen.
- **`Key` and `UnplayedItemCount` are not on a real `UserDataList` entry,
  and `PlayedPercentage` is.** Observed entry keys: `ItemId`,
  `PlaybackPositionTicks`, `Played`, `PlayCount`, `IsFavorite`, plus
  `PlayedPercentage` (a float, when the position is non-zero) and
  `LastPlayedDate` (when played). The fixture and `FakeEmbyServer` both
  rendered a `Key`; both stopped.
- **The `Sessions` interval, which `DEFAULT_STALE_AFTER_SECONDS = 90.0`
  rests on: median 38.7 s, mean 32.8 s, p90 46.5 s, max 72.9 s** over 182
  intervals in 100 minutes on an authenticated socket. **The 90 s default
  survives — but the headroom is 1.23x, not the comfortable margin the
  constant reads like, and it shrank monotonically as the window grew**: the
  worst gap was 52.6 s at 26 minutes, 60.1 s at 70 and **72.9 s at 96**, and
  only two of 182 intervals exceeded 60 s at all. So
  a longer hold would plausibly have crossed 90, and this is a bound that
  has **not been falsified** rather than one shown to be safe — on one
  household, on one evening. A 75-second smoke run earlier the same evening
  saw exactly **one** frame, and the cadence is not an interval at all
  (below).

  **The default is left at 90 anyway, and the reasoning is worth keeping.**
  A bigger constant chosen from a 96-minute sample would be just as
  unprincipled as this one was, and it costs detection time for the failure
  the whole milestone exists to catch. The real finding is that the constant
  is wrong *in kind*: there is no application-level heartbeat on this
  channel at all, so any fixed ceiling is a guess against a change-driven
  signal. The one genuinely periodic signal available is the WebSocket
  pong — and ADR-0018 deliberately refuses to count it, because a pong is
  not delivery. That tension is the honest statement of what this design
  costs. When it bites, it bites bounded and visible: a reconnect, a delta
  that returns 0 items, and `usher.source.push.reconnects` climbing.
- **`"0,1000"` really is `initialDelayMs,intervalMs`, and an authenticated
  socket does not honour it.** An *unauthenticated* socket receives
  `Sessions` at ~1 Hz — 53 and 55 frames in 45 s, with sub-second gaps —
  while the authenticated one on the same server in the same minute received
  **one**. The difference is the payload: the unauthenticated stream carries
  the **whole server's 83 sessions**, the authenticated one a 5-session
  row-filtered view. The natural reading, and the one that fits every
  number: the 1 s timer fires either way and the filtered stream is only
  *sent* when the filtered view changes. **So Usher's liveness signal is
  change-driven, not periodic**, and a genuinely quiet server could exceed
  any fixed `stale_after`. `push_stale_after_seconds` is the knob, and
  `usher.source.push.reconnects` is how the condition is seen.
- **`/embywebsocket` does not accept `X-Emby-Token` as a header, and the
  test that looks like it says otherwise is the trap.** A header-only socket
  upgrades and delivers — identically to one with **no credential at all**:
  53 frames of 83 sessions against 55 frames of 83 sessions. It is not
  authenticated; it is anonymous. So the token cannot be moved out of the
  URL this way and **ADR-0012's accepted risk stands unnarrowed**. A check
  written as "did it connect and receive messages" passes this. The only
  discriminator is the row-filtered payload, or a `UserDataChanged` that
  never comes.
- **A dropped socket raises rather than hanging, and Emby re-delivers
  nothing.** Aborting the TCP transport under a live channel raised
  `PortUnavailable` out of the iterator in **0.0 s** — not a hang, not the
  quiet end the port forbids — and `connected` went false. Over a **61 s**
  outage, a real played toggle and its restore were made out of band; the
  reconnected channel then listened for **90 s** and received three
  `Sessions` and **not one** `UserDataChanged`. The control is decisive: a
  *second* socket that stayed up throughout received both changes at the
  time they happened. **The gap-closing delta is not belt-and-braces, it is
  the only cover there is**, which is exactly what PRD 03 puts on the
  reconnect.
- **The `websockets` DEBUG token leak is real, and the fix holds against the
  real library and the real server.** Two runs at `USHER_LOG_LEVEL=DEBUG`
  with `configure_logging` installed exactly as `create_app` does, each
  writing to a real stdout captured to a file: the shipped path produced
  **804 bytes / 2 lines** with **no token, no `api_key=`, no `> GET`
  request line** and a channel that genuinely delivered
  (`messages_received == 1`); the control — the same URL with the library's
  own logger left alone — produced **16,857 bytes / 24 lines** with the
  token in it, `api_key=` in it, and the request line logged twice. Both
  halves, or the run proves nothing; the same discipline the network guard
  gets.
- **`permessage-deflate` is not negotiated.** `websockets` offers it by
  default and the handshake response carries no `Sec-WebSocket-Extensions`
  at all, on every connection made in this run. So nothing in this project
  is relying on compression, and a frame is a frame.
- **A client that stops reading loses the connection, which is what
  `max_queue=256` is buying.** With `max_queue=1` and no application read
  for 150 s, the socket came back **CLOSED** with a `ConnectionClosedError`
  and only two buffered `Sessions` behind it — so Emby's listener does not
  queue indefinitely for a stalled consumer. **The confound is named rather
  than glossed:** `websockets` services pings on the same reader task that
  backpressure stalls, so this measurement cannot separate a server-side
  close from the client's own pong timeout. Either way the operational
  conclusion is the same and it is the one `connect_websocket` was already
  written for: do not let the queue fill during the gap-closing walk.
- **The nonexistent path still upgrades**, ADR-0004's quirk re-measured on
  the same build: `/embywebsocket-nope` → 101, `Upgrade: websocket`,
  `Sessions` delivered.
- **`supports_push` is `False` before the first message and `True` after**,
  measured through the shipped adapter against the real server rather than
  against a fake — the contract's pre-message assertion, live.
- **The one write to a real account, and its restoration.** The same
  discipline M4's run set: an item whose complete `UserData` was already
  `{PlaybackPositionTicks: 0, PlayCount: 0, IsFavorite: false, Played:
  false}` with no `LastPlayedDate`, found with **one** 50-item listing plus
  one single-item read (never a search over a walk). `push_watch_state`
  wrote a 613 s position, then marked it played (`PlayCount: 1`,
  `LastPlayedDate`, position cleared — M3's ordering finding, re-confirmed),
  then `DELETE /Users/{u}/PlayedItems/{item}` restored it **byte-for-byte**
  (`after == before`). A second toggle and restore during the outage test
  ran on the same item and ended the same way; the final read-back matches
  the recorded `before` exactly.
- **`PlayedPercentage` appears on the item route too** when a position is
  set, and disappears when it is cleared. Nothing reads it; recorded because
  it is the one key the fixture was missing.
- **Not verified in this run, and named rather than implied:** `POST
  /Users/AuthenticateByName` and whether its response carries
  `User.Policy.IsAdministrator` (this run held a token, not a password —
  so Task 3's extra `GET /Users/{userId}` remains the verified path);
  silent 401 re-authentication end to end; durable-device registration
  across restarts; a socket held for four hours (**100 minutes** is what this
  run covers, with zero reconnects and zero unforced failures in it); a
  `LibraryChanged` with `IsEmpty: true` (all twelve observed carried
  something, so what that field means is still a guess about a field nothing
  reads); a `UserDataChanged` for a **series** entry, which is where
  `UnplayedItemCount` would plausibly appear; and whether a real entry is
  honest about play history for an item Usher did not itself write.
**M4's live verification: the design's central measurement holds, the
matcher's exact-name tier was expected to match "almost nothing" and matches
about three quarters, and the defect the plan called hypothetical is real in
this library.** Run
2026-07-31 against the same live Emby **4.9.5.0** server, driving the real
`EmbyAdapter` and the real `ReconcileService`/`IngestService`/`MatchService`/
`WatchStateSyncService` against a real `pgvector/pgvector:pg17` holding a
real M2 bootstrap (1,271,314 titles). Bounded deliberately: **600 items
ingested** and ~90 deliberate requests, from a throwaway script outside the
working tree. (Plus several hundred accidental ones from a single runaway
probe, killed — see the bounding note near the end of this file. Counted here
rather than quietly dropped: it is the mistake worth not repeating.)

- **The finding M4 exists to answer, re-measured through the real adapter,
  on one item, in one run.** The *listing* reports `PlayCount: 0` and no
  `LastPlayedDate`; the *single-item* route reports `PlayCount: 13` and
  `LastPlayedDate: 2026-07-30T08:12:53Z`; `PlaybackPositionTicks` and
  `Played` agree. Through `EmbyAdapter`: the walk yields
  `play_count=None, last_played_at=None`, `get_watch_state` yields `13` and
  the real timestamp. Over the first 100 states of a real
  `adapter.watch_state()` walk, `play_count` and `last_played_at` are
  `None` for **all 100**. ADR-0014's premise is measured, not assumed.
- **The milestone's central property, end to end against real payloads.** A
  row holding the authoritative `play_count = 13` was then fed the *listing*
  payload for the same item through `to_watch_state(...,
  play_history_is_trustworthy=False)` and `merge_from_source`. It reads back
  **13**, `played = true`, and the original `last_played_at`. The walk
  cannot zero real history, verified against the live server rather than
  against a fake told to behave like it.
- **`MatchService`'s exact-name rule matches ~74% of real Emby names, not
  "almost nothing".** Measured against the real 1,271,314-title catalog with
  the *identical* rule `_confident` applies (exact normalised name, year
  ±1, exactly one survivor), over 600 movies and 300 series sampled across
  six windows spanning the whole collection: **72.2% of movies** (433/600)
  and **75.3% of series** (223/296 distinct probes). Of the movie misses,
  142 are *absent* from the catalog and only 25 are *ambiguous* — so the
  review queue is a trickle, and what feeds it is mostly the catalog not
  holding the title at all rather than the rule being too strict. This
  reverses the plan's stated expectation and it is the single most
  load-bearing number the live run produced.

  **What this is and is not.** It is `_confident`'s *predicate*, run over
  the local catalog — i.e. tier 3 — not `_confident` against TMDb's own
  search results, which no run in this repository has ever made. The two
  differ in their candidate set, and in opposite directions: TMDb returns a
  handful of relevance-ranked results, so "exactly one survivor" is *easier*
  to satisfy than against 1,271,314 rows; but TMDb can also return nothing
  for a name the local skeleton holds. So treat 72–75% as a measurement of
  the rule on real names, not as a prediction of tier 4's yield.
- **On this library the name+year tier out-resolves the `tmdb_id` tier.**
  68.5% of movie TMDb refs and 68.7% of series TMDb refs resolve, against
  72.2%/75.3% for name+year — because only 291,772 of 1,271,314 catalog
  titles carry a `tmdb_id` at all. Tier 3 is not the fallback the ladder's
  ordering makes it look like.
- **A probe with no year resolves nothing, by construction, confirmed on
  real data.** `t.year BETWEEN p.year - 1 AND p.year + 1` propagates `NULL`,
  so the same 900 names re-run with the year stripped match **0**. That is
  the documented intent (the alternative matches every undated IMDb
  skeleton of the same name) — recorded here because "0.0%" looks like a
  bug and is not.
- **A malformed `ProviderIds.Imdb` is real, not hypothetical: 11 of 885 in
  the sample** (1.2%), all bare 6- or 7-digit numbers with no `tt` prefix.
  Fed to the real `MatchService` they resolve cleanly (9 stubs, 2 name+year)
  and nothing raises. **The guard that makes that true is `_as_imdb`, not
  `_usable_ids`** — the two are layered, and removing `_usable_ids`'s
  filtering alone still does not raise, because `_create_stub` calls
  `_as_imdb` again at the constructor. Removing `_as_imdb`'s pattern check
  raises `pydantic_core.ValidationError` on these exact real payloads, which
  is **not** a `UsherPortError`, which is a permanently aborted sync. Measured
  both ways.
- **An episode never walks the ladder, confirmed on real data.** Of 600 live
  items, 578 were episodes and every one returned `UNMATCHED` from
  `MatchService` with no lookups; `IngestService` attached them as
  `SERIES_PARENT`. Zero episodes reached a provider tier or the stub tier.
- **Stub-on-sight never fired, and that makes the cold and warm walks
  identical.** All 22 non-episode items resolved to existing catalog titles
  (21 by `tmdb_id`, 1 by `imdb_id`), so **zero stubs were created** — and
  walk 2 over the same 600 items cost exactly the same **40 statements**,
  `0.0667` per item, as walk 1. That is the "16,950 of the first walk's
  17,722 statements are stub-on-sight, bounded by new titles" claim
  arriving from the other direction: with no new titles, there is no cold
  penalty at all. (40 statements for one 600-item batch is above the 15.4
  statements/batch Task 25 averaged over 50 batches, because this is a
  single first batch where every series, season and episode is new.)
- **A delta walk completes and its cursor advances; a failed walk sweeps
  nothing.** A `DELTA` reconcile against the live server inherited the last
  completed `FULL` run's instant, returned 0 items (nothing had changed in
  that window), recorded `COMPLETED`, and advanced `sync_runs.cursor_at`. A
  `FULL` walk interrupted mid-stream recorded `FAILED` with its message and
  left all 601 `available` rows untouched — `items_retracted = 0`.
- **The delta filters, re-measured on a fresh 30-day window.**
  `MinDateLastSaved` = 28,955, `MinDateLastSavedForUser` = 29,027, unfiltered
  = 1,126,789. Still honoured, still genuinely different, and an *invented*
  parameter name still returns the full unfiltered count — the "degrades to
  a full walk" safety property, re-measured.
- **The library grew.** 1,126,789 items now (94,448 movies / 32,414 series /
  999,927 episodes), against 1,126,674 four days earlier. Any figure derived
  from it is a snapshot, not a constant.
- **`VideoRange`'s vocabulary holds over a second, different slice.** 600
  movies spread across the whole collection by `DateCreated` ascending:
  `SDR` 597, `DolbyVision` 2, `HDR 10` 1, with `ExtendedVideoType/SubType`
  ∈ {`None/None`, `Hdr10/Hdr10`, `DolbyVision/DoviProfile50`,
  `DolbyVision/DoviProfile81`}. `VideoRangeType`, `DvProfile` and
  `DvVersionMajor` are absent from every video stream. The mapper produced
  the right `SourceItem` for all 1,100 sampled payloads (600 movies, 300
  series, 200 episodes) with **zero failures and zero skips**, and the
  technical metadata survives all the way into `media_items`: 496 `h264` +
  85 `hevc`, 581 of 601 rows carrying width/container/file size (the 20
  without are `Series` rows, which have no `MediaSource` — correct), and one
  row carrying `hdr_format = DV` from a real `VideoRange: "DolbyVision"`
  payload. `SDR → NULL` and `DolbyVision → DV` are both confirmed on stored
  rows; `HDR 10 → HDR10` appeared in the sampled payloads but not in the
  ingested slice, so that arm is still fixture-only end to end.
- **Emby's `ProviderIds` key space is far wider than three, and case is not
  stable.** Observed on 900 movie/series payloads: `Tmdb`, `Imdb`, `Tvdb`,
  `TvMaze`, `Official Website`, `TvRage`, `X (Twitter)`, `Zap2It`,
  `TV Maze` (with a space, alongside `TvMaze` without), `Wikipedia`, `EIDR`,
  `Wikidata`, `Reddit`, `Fan Site`, `IMDB` (14 items — uppercase),
  `Facebook`, `Instagram`, `TmdbCollection`, `Youtube`, `tmdb` (3 items —
  lowercase), `Twitter`. `mapping.provider_ids`' `key.lower()` is what makes
  `IMDB` and `tmdb` usable at all, and an exact-key `get("tmdb")` is what
  keeps `TmdbCollection` from being read as a TMDb id. A prefix match there
  would attach films to collections. The one residual risk is an item
  carrying both `Imdb` and `IMDB` with different values, where `key.lower()`
  silently keeps whichever came last; none was observed.
- **The one write to a real account, and its restoration.** An item was
  chosen whose complete `UserData` was already
  `{PlaybackPositionTicks: 0, PlayCount: 0, IsFavorite: false, Played:
  false}` precisely so the one destructive Emby route is an *exact* restore.
  `push_watch_state(played=True)` took it to `PlayCount: 1`, `Played: true`,
  `LastPlayedDate: 2026-07-31T13:41:53Z`; `get_watch_state` — the backfill's
  own read path — returned `play_count=1` and that timestamp, which is the
  backfill verified end to end against a real write. `DELETE
  /Users/{u}/PlayedItems/{item}` restored the object **byte-for-byte** (the
  before/after diff is empty). Choosing an all-zero item is what made
  restoration exact rather than approximate; on any other item `PlayCount`
  is not restorable by any route this project knows.
- **Not verified in that run, and named rather than implied:** a full
  1,126,674-item walk; `POST /Users/AuthenticateByName`; silent 401
  re-authentication end to end; durable-device registration across
  restarts. Anything needing a TMDb API key was also unverified there and
  **is no longer** — a key was configured the next day and the TMDb half of
  Task 26 ran on 2026-08-01; see the TMDb live-verification section below,
  including `_confident` against TMDb's own search results, which that run
  measured at 83.1%/87.2% against the 72–75% the *local* rule scores.
  `EnrichService` and the `enrich` job handler are still driven only by
  fakes: the live run exercised `TmdbClient`, `TmdbMetadataProvider` and the
  mapper, not the service above them.
**M3's live verification found the write-back route was simply wrong, and
three other things worth not re-deriving.** Run 2026-07-31 against the live
Emby **4.9.5.0** server, driving the real `EmbyAdapter`/`EmbySession` with
`_authenticate_locked` swapped for one that installs a known token. Full
route-by-route table in the M3 plan's "Which Emby routes are guessed"
section.

- **`POST /Users/{user}/PlayingItems/{item}/Progress` answers 400** —
  `"Value cannot be null. (Parameter 'key')"` — bodyless, with an empty JSON
  body, with an `{ItemId, PositionTicks}` body, and with `MediaSourceId` and
  `IsPaused` added. So does `POST /Sessions/Playing/Progress`. Both are
  *session-scoped playback reporting*, keyed off a play session Usher never
  has. **Use `POST /Users/{user}/Items/{item}/UserData`** with a JSON body;
  it answers 204. `FakeEmbyServer` could not have caught this: it
  implemented the adapter's own guess, so 40 contract assertions passed
  against a write-back that had never worked once. This is the whole
  argument for a live run in one bug.
- **That `UserData` body must name `Played` even when it is not changing.**
  It deserialises into a DTO whose unset fields take their defaults, so a
  body carrying only `PlaybackPositionTicks` flips a played item to
  unplayed. `PlayCount` and `LastPlayedDate` survive the same omission.
- **`DELETE /Users/{user}/PlayedItems/{item}` is destructive beyond its
  name:** it resets `PlayCount` to 0, clears `LastPlayedDate`, *and* clears
  a non-zero resume position. Never use it to report an item unplayed while
  writing a position. `POST` to the same route *is* how you mark played —
  it advances `PlayCount` (to 1, idempotently, not `+1`), stamps
  `LastPlayedDate`, and clears the resume position. That last part is PRD
  03's load-bearing "position first, played last" ordering, verified for the
  first time.
- **`/Videos/{id}/stream` does not need `DeviceId`.** Measured one parameter
  at a time with a `Range` header: as built → 206 with real bytes; without
  `DeviceId` → still 206; without `api_key` → 401; without `static` → 400.
  The parameter is no longer sent (ADR-0012).
**A listing's `UserData` is not the same as an item's.** Verified: a
`GET /Users/{user}/Items` listing reports `PlayCount: 0` and omits
`LastPlayedDate` entirely, for the very item whose
`GET /Users/{user}/Items/{item}` reports `PlayCount: 2` and a real
`LastPlayedDate`. `PlaybackPositionTicks` and `Played` are correct in both.
Neither `Fields=UserDataPlayState`, `Fields=UserData`,
`EnableUserData=true`, nor restricting the listing to explicit `Ids`
changes it. So `watch_state()` — which walks listings — cannot carry play
history, and M4 must not write `play_count`/`last_played_at` from a walk or
it writes 0 over real history. Recovering them is one request per item
against 1,126,674 items. Making both fields optional on `SourceWatchState`
is the honest fix; it is a port change and is deliberately left to M4.
**Emby 4.9.5.0 emits neither `VideoRangeType` nor `DvProfile`.** Not once
across every video stream of 200 movies (the newest 100 4K and 100 HD of
94,438), including all 34 Dolby Vision files. What it emits is `VideoRange`
∈ {`SDR`, `DolbyVision`, `HDR 10`} — with a space — plus
`ExtendedVideoType`/`ExtendedVideoSubType` ∈ {`None`/`None`,
`Hdr10`/`Hdr10`, `DolbyVision`/`DoviProfile81`|`DoviProfile50`}. The
`Extended*` pair carries the **literal string `"None"`**, not JSON null, so
it is always truthy and any check on it must be a token lookup that falls
through. The `DOVIWith*` family the mapper also handles is Jellyfin's
vocabulary, not this server's; both are kept, since reading a field a server
omits costs nothing.
**Emby honours a secondary sort key, so `SortBy=DateCreated,SortName` is a
real request.** Shown on a tie-heavy primary key rather than hoped for:
`ProductionYear,SortName` returns the tied block in `SortName` order,
`ProductionYear` alone returns it in a different, insertion-shaped one. Tie
*instability* was **not** reproducible here — repeated pages came back
identical and overlapping `StartIndex` windows agreed exactly, with and
without the tiebreak — so the second key is a cheap guarantee rather than a
demonstrated-necessary fix. `MinDateLastSaved` and `MinDateLastSavedForUser`
are both honoured and are genuinely different filters (28,934 vs 29,005
items over the same 30-day window). An *invented* parameter name is ignored
outright and returns the full unfiltered count, which is the "degrades to a
full walk" safety property, measured.
**The library is 1,126,674 items, not 94,395.** 94,438 movies, 32,409
series, 999,827 episodes. The movie figure the adapter was designed around
was one third of the walk. At the default page size that is 5,634 pages —
**56% of `MAX_PAGES`**, so the headroom is 1.8x, not the ~21x the constant's
comment claimed. **Re-measured four days later: 1,126,789** (94,448 /
32,414 / 999,927). It moves; treat every figure derived from it as a
snapshot with a date on it, not a constant.
**A token presented with a different `DeviceId` neither forks nor
invalidates its session.** `GET /Sessions` was byte-identical before and
after, and the token still worked. Emby binds a session to the token's own
authentication record, made at `AuthenticateByName` time; the header's
`DeviceId` on later requests does not register a device. So "one durable
device" comes from authenticating once with a stable id, not from repeating
it.
**Not verified, and the docs say so rather than implying coverage:** `POST
/Users/AuthenticateByName` itself (that run held a token, not a password —
it is verified separately by ADR-0004's session), silent re-authentication
on a 401 end to end, durable-device registration across restarts, and
`multi_version_movie.json`'s shape.
**`multi_version_movie.json` has now been looked for twice, over disjoint
slices, and still has never met a real payload.** M3 searched the newest 800
movies; M4 searched 600 movies spread across six windows of the whole
94,448-movie collection ordered by `DateCreated` ascending (indices 0,
18889, 37779, 56668, 75558, 94348). **Every one of the 1,400 movies examined
carries exactly one `MediaSource`** — the count distribution is `{1: 600}`
with nothing else in it. So `primary_media_source`'s selection rule remains
fixture-only, and this deployment now looks like a genuinely
single-version library rather than one whose multi-version items happened to
sit outside the first sample. The fixture stays: another Emby deployment
will have them, and the rule is cheap.
**`Policy.IsAdministrator` is readable**, on `GET /Users/{userId}`, with the
user's own non-admin token — a 45-key `Policy` object. (`GET /Users/Me`
answers 500 on this build.) ADR-0012 assumes a non-admin account and nothing
enforces it; this is the check that would make it observable, recorded there
as recommended-not-implemented.
**An episode must never walk the match ladder, and the reason is in the
payload.** A live Emby episode carries the *episode's* own provider ids —
`{"Imdb": "tt2178782", "Tvdb": "4517466"}` on `tests/fixtures/emby/
episode_item.json` — not its series'. Two consequences, both catastrophic at
999,827 episodes. TVDb numbers episodes and series in different, numerically
overlapping namespaces and `usher.db.repositories.matching`'s TVDb statement
deliberately does not filter on kind, so an episode run through the provider
tiers resolves to whichever unrelated series holds that integer. And no
episode's IMDb id is in the catalog at all (`tvEpisode` is excluded from M2's
bootstrap by design), so the stub tier mints one junk `Title` per episode —
a catalog of rubbish roughly the size of the real one. `MatchService` returns
`UNMATCHED` for an episode with no lookups and **no remote-search job** (one
per episode is a queue the size of the library, and a TMDb title search for
an episode name is not a resolution path); `IngestService` attaches it to its
series' `Title`, labelled `MatchMethod.SERIES_PARENT`.
**Nothing a source can put in a payload may abort a walk.** `Title.imdb_id`
is pattern-validated (`^tt\d{7,8}$`) and `year` is `ge=0`, and a pydantic
`ValidationError` is **not** a `UsherPortError` — so `ReconcileService`, which
re-raises anything that is not one, would let a single stray
`ProviderIds.Imdb` in 1,126,674 items abort that source's sync permanently.
Filter every value to the shape the model accepts *before* the constructor.
**Verified live 2026-07-31: 11 of 885 real `Imdb` values in a 900-item sample
are bare digits with no `tt` prefix, so this is a live defect rather than a
defensive one.** The two filters are layered and only the inner one is
load-bearing: `_usable_ids` drops unusable refs, and `_create_stub` calls
`_as_imdb`/`_as_int` *again* on what survives. Removing `_usable_ids`'
filtering alone raises nothing on those exact payloads; removing `_as_imdb`'s
pattern check raises `ValidationError` on them immediately. So
`usher.services.matching._as_imdb` is the guard, and a mutation of
`_usable_ids` alone is an equivalent mutant.
**`sorted()` over a set of `ProviderRef`/`NameYearProbe` raises.** Both are
`@dataclass(frozen=True, slots=True)` without `order=True`, so there is no
`__lt__` — `TypeError: '<' not supported`. `dict.fromkeys` is the idiom used
throughout: it deduplicates *and* keeps the batch's own order, which is what
makes a failure read in the order the page arrived.
**A service that saves a frozen checkpoint per batch must not evolve its own
stale copy in the failure handler.** `ReconcileService._flush` saves an
evolved `SyncRun` after each batch, so when the walk raises, `reconcile`'s
binding is the pre-walk value — and `run.evolve(status=FAILED)` on it writes
`items_seen = 0` over a checkpoint that recorded eight. Same trap
`BootstrapService.import_dataset` documents; here there is no re-fetch to
recover from (`SyncRunRepository` is a history, not a per-source checkpoint),
so a small mutable holder carries the latest run across the `try`.
**Moving the availability sweep into a `finally:` really does retract a
healthy library, and the obvious test shape hides why.** Measured. Seed seven
items, fail the walk immediately, one batch: nothing is written before the
failure, so the sweep would retract 7 of 7 — 100%, refused by ADR-0015's
ceiling, and `AvailabilitySweepRefused` then escapes the `finally:` and
propagates out of `reconcile`. The case fails, but on an uncaught exception
rather than on its own assertion, and it never exercises a sweep that
*succeeds* after a failed walk. The shape that does is a walk that commits
eight of ten items and then raises: two stale rows, 20%, under the ceiling,
no refusal, two available items silently retracted. **The ceiling is not a
second line of defence for the success-path gate** — it fires on a fraction,
so it catches the catastrophe and misses the quiet one. Reproduced against
real Postgres as well as the fakes.
**`observed_at=now()` instead of the run's start instant is a *semantic*
break, not a race.** A per-row write instant is always later than
`run.started_at`, so the sweep's `last_seen_at < seen_since` still spares
everything the run saw and no retraction test fails. What breaks is the
meaning of the column. Assert `stored.last_seen_at == run.started_at`
directly; no frozen clock is needed.
**An episode's `MediaItem` carries two ids and its `WatchState` may carry
one, and the collapse between them is the whole of M4's episode watch
state.** `IngestService` writes the series' `title_id` *and* the
`episode_id` on an episode's row (a client browsing a season wants both);
`watch_states` has a `num_nonnulls(title_id, episode_id) = 1` CHECK. So
`WatchStateSyncService` collapses the pair with the episode winning
(`usher.services.watch_sync._watch_target`). Passing both through raises
`PortDataMalformed` by contract, which aborts a batch of five thousand
states over 89% of this library; passing the *title* through merges every
episode of a show onto one row and violates nothing. The same asymmetry
runs the other way in `MediaItemRepository.resolve_external_ids`, whose
title branch needs `episode_id IS NULL` or a series' own watch state
resolves to whichever of its episodes the planner reached first.
**A history backfill must carry its own fresh `observed_at`, and both
test layers are blind to why.** PRD 03's "latest `updated_at` wins" covers
the whole record, and `trg_watch_states_set_updated_at` stamps the *write*
instant — so a backfill carrying the walk's instant is refused by the very
row it exists to repair, writes nothing, and leaves that row matching
`played AND play_count = 0` forever. `FakeWatchStateRepository` stores
`observed_at` as `updated_at`, so it accepts what Postgres refuses; and the
integration suite cannot reproduce the production form either, because
`now()` is frozen per transaction and each test *is* one transaction.
`tests/integration/test_services_watch_sync.py` stages the row with
`clock_timestamp()` through a raw `INSERT` (the trigger is `BEFORE UPDATE`,
so an insert is the only way to own the column), which is as close as one
transaction allows.
**The bounded backfill terminates, measured.** Seven rows matching
`played AND play_count = 0`, drained three at a time, empty in exactly
three passes — against the fakes and against real Postgres, with the loop
bounded so a non-converging predicate fails the case rather than hanging
the suite. The honest half: convergence is a property of the *source*. A
source whose single-item route also cannot count leaves rows matching
forever, bounded at one request per row per pass and rotating rather than
starving, because `list_needing_history` is oldest-first and a merge moves
`updated_at`.
**Two guards in M4's services are unreachable through their own port's
contract, and are pinned by direct unit cases rather than deleted.**
`_watch_target`'s "matched to nothing" branch (`resolve_targets` omits an
unmatched item rather than answering with an empty pair) and `_links_for`'s
`is_valid` check (the OTel SDK also drops an invalid `Link` on the way into
a span, so a worker that built one records the same empty `links` tuple).
Both mutations survived the whole suite until the direct case existed.
**Two `IngestService` defects are invisible to every port fake and only real
Postgres catches them.** Skipping `resolve_seasons` or `resolve_episodes` and
trusting the freshly-minted UUIDv7 leaves all 24 unit cases green — a dict has
no foreign keys — and fails on `fk_episodes_season_id_seasons` /
`fk_media_items_episode_id_episodes` on the *second* walk, when that id names
no row. `tests/integration/test_services_ingest.py` and
`tests/integration/test_services_reconcile.py` are the paired runs; the latter
also pins "a refused sweep leaves the session usable for the `FAILED` row that
explains it", which no fake can express (the guard is evaluated in Python
after a successful `SELECT`, so Postgres never aborts the transaction).
**The ingest pipeline's measured cost, 2026-07-31 against
`pgvector/pgvector:pg17`** (`scripts/measure_ingest.py --items 50000`,
50,000 items in the measured library's proportions — 88.7% episodes — at
batch size 1,000):

| | statements | per item | items/s |
|---|---|---|---|
| first walk, cold catalog | 17,722 | 0.3544 | 1,933 |
| the nightly walk | 1,356 | **0.0271** | 2,135 |
**16,950 of the first walk's 17,722 statements are stub-on-sight**, and that
is the one path in the pipeline that is not set-based:
`MatchService._create_stub` calls `TitleRepository.add` per item, and that
add is SAVEPOINT-wrapped, so a new title costs three statements. It is
bounded by **new titles** (94,438 movies + 32,409 series), never by items —
an episode never walks the ladder, so the other 999,827 items cost nothing
there — and a second walk creates none. Batch-level cost is 772 statements,
0.0154 per item. Throughput is against a local database with no network in
the way; a real walk is bounded by Emby's 5,634 pages at 1–5 s each.
**Four scale risks, planned against the statement the repository actually
issued** (`scripts/measure_ingest.py --scale 1126674`; captured off
`before_cursor_execute`, never transcribed — a hand-copied lookalike drifts
and then reads like coverage, and two earlier tasks here were replaced for
exactly that):

- **`merge_from_source` at 1,126,674 `watch_states` with a 1,000-row batch:
  refuted.** `Nested Loop` + `Index Scan using ix_watch_states_title_id`,
  1,000 loops, 14.5 ms. No hash join, no seq scan.
- **The claim scan behind a wall of backed-off jobs: confirmed, unfixed.**
  216 ms with `Rows Removed by Filter: 1126674`. `ix_jobs_claim` is
  `(priority DESC, created_at) WHERE status = 'pending'` and a backed-off
  job is *still* `pending`, so every poll walks past all of them.
  `run_after <= clock_timestamp()` is not an indexable partial predicate
  (`clock_timestamp()` is not immutable), and putting `run_after` first
  destroys the priority ordering — so this is recorded rather than solved.
  It only bites when a large fraction of the queue is backed off, i.e. when
  an upstream is broken.
- **`list_unmatched`'s `OFFSET`: confirmed.** 43.7 ms at offset 0, 388.9 ms
  at offset 1,126,574 — linear per page, quadratic to drain. Fine for an
  operator reading the first few pages, wrong for a client paging the whole
  review queue; a keyset cursor is the fix when something needs one.
- **The availability sweep: half.** `ix_media_items_sweep`
  (`source_id, available, last_seen_at`) takes the sweep's `UPDATE` from
  `Seq Scan` (`Rows Removed by Filter: 1,126,474`, 173 ms) to `Index Scan`
  with an `Index Cond` on all three columns, 102 ms. It does **not** help
  the guard's `count(*)`, a `Parallel Seq Scan` with the index (87 ms) and
  without it (86 ms) — ADR-0015's ceiling is a *fraction*, so the
  denominator is unavoidable and a source that *is* the whole table gives
  `source_id` no selectivity. Both numbers are in migration
  `f1a7d3c9e824`, not the flattering one alone.

Verified working as of M3 (the Emby adapter) — a source can be registered
and interrogated over HTTP, and the suite is 865 tests (733 unit / 132
integration), mypy strict clean over `src` and `tests`, 6 import contracts:

```bash
uv run usher --help                              # the CLI, also installed as `python -m usher`
uv run pytest                                    # 1744 passed + 1 skipped (1320 unit / 425 integration)
uv run pytest tests/unit                         # 1319 passed + 1 skipped, no Docker and no network
uv run pytest tests/unit/test_adapters_emby_contract.py  # the contract suite against the real adapter
uv run mypy src tests                            # strict, including tests/
uv run ruff check --no-cache . && uv run ruff format --check .
uv run lint-imports                              # 7 kept, 0 broken

# Register a source and read its health, against a running app:
curl -sS -X POST http://localhost:8000/admin/sources \
  -H 'content-type: application/json' \
  -d '{"kind":"emby","name":"Living Room Emby","base_url":"https://emby.example","username":"...","password":"..."}'
curl -sS http://localhost:8000/admin/sources/<id>/status

# Diff a live server's *shape* against the committed fixtures. NOT a test,
# and its output is deliberately never committed -- see the module docstring.
export USHER_EMBY_URL=... USHER_EMBY_USER=... USHER_EMBY_PASSWORD=...
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json

# The same thing for TMDb. Verified working against the live API 2026-08-01;
# `set -a; . ./.env; set +a` rather than a literal key, so no credential ever
# reaches a shell history or a recorded command.
set -a; . ./.env; set +a
uv run python scripts/capture_tmdb_fixture.py --kind movie  --id 550   > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind series --id 1399  > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind season --id 1399 --season 1
uv run python scripts/capture_tmdb_fixture.py --kind search --query Dune --year 2021
uv run python scripts/capture_tmdb_fixture.py --kind changes
```

**A live run against this Emby server must be bounded, and the bound has to
be in the *iterator*, not in `max_pages`.** Exhausting `max_pages` raises
`PortDataMalformed` — it is the walk's dead-man's switch — so a reconcile
bounded that way records `FAILED` and never reaches the sweep, which is the
half of the pipeline the run exists to exercise. Truncate the async
generator instead. Learned the expensive way in the same run: a probe that
walked `adapter.watch_state()` *looking for one known item id* is a walk of
1,126,789 items to reach something a filtered listing already had, and it
issued several hundred requests against a shared server before it was
killed. Any "find the item where X" over a walk is a full walk; ask the
server with a filter.
