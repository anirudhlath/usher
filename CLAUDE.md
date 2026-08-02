# CLAUDE.md

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL.

**Status: M5 complete.** The project scaffold, environment
config, domain models, port ABCs, persistence (SQLAlchemy schema + Alembic
migrations + title repository), the telemetry bootstrap, a FastAPI app
with liveness/readiness endpoints, the container image + compose stack + CI
(M1), the bulk-dataset bootstrap pipeline — IMDb skeleton, TMDb ID export,
Wikidata crosswalk, all resumable and checkpointed (M2) — the Emby
`SourceAdapter` with encrypted source credentials, a source repository, the
admin source routes, and a source-agnostic contract suite that runs against
both a pure in-memory adapter and the real one (M3) — and the ingest
pipeline: `MatchService`/`IngestService`/`ReconcileService`/
`WatchStateSyncService`/`EnrichService` over nine repository ports and a
Postgres priority queue, the TMDb metadata provider, the `sync`/
`sync-status`/`unmatched`/`work` CLI, and the pipeline's span tree and
metrics (M4) — all exist and are verified working, M3 and M4 including
against a live Emby 4.9.5.0 server and, as of 2026-08-01, against the live
TMDb v3 API — and the push lane, the supervised reconnect with its
gap-closing delta, `GET /titles/{id}` with demand promotion, `GET /events`
over SSE, and the two supervised lanes `create_app` now runs (M5), verified
2026-08-02 against the same live Emby server in the first run this
repository has ever made that parsed a real `/embywebsocket` message. See
`docs/plans/2026-07-28-m1-foundation.md`,
`docs/plans/2026-07-30-m2-bootstrap.md`,
`docs/plans/2026-07-30-m3-emby-adapter.md`,
`docs/plans/2026-07-31-m4-ingest.md` and
`docs/plans/2026-08-01-m5-push.md` for the task breakdowns and
`docs/prd/09-roadmap.md` for what's next (M6 — search). Do not invent
commands for tooling that does not exist yet — check the Commands section
below before assuming something runs.

**M4's four deliberate boundary calls**, each stated with its reason in the
M4 plan's Scope section and in PRD 09: the **index** stage is M6's (no
`index` job kind ships, because a job kind whose handler is a stub is a
queue that grows forever); **push/reconnect-delta/demand/SSE** are M5's (M4
builds the queue's promotion *mechanism* but nothing calls it with
`JobPriority.DEMAND`); the **three admin HTTP routes** are M9's, with the
same capability delivered through `usher.cli`; and enrichment populates
`Title`/`Season`/`Episode` only, with `Person`/`Credit`/`Collection`/`Image`
re-derived from `raw_payloads` by M7/M9 with **no second network call**.

## Keep the PRD current

`docs/prd/` is the authoritative, living description of what Usher is and why.
Code that contradicts it is a bug in one of them — resolve it, never let it
drift silently.

**Update the PRD in the same commit as the change that invalidates it.** Not in
a follow-up, not "later". A change that alters behaviour and leaves the PRD
stale is incomplete.

Start at `docs/prd/README.md` for the index. Detailed maintenance conventions
load automatically when working in `docs/`.

## Conventions that will bite you

- **Ports are `abc.ABC`, not `typing.Protocol`.** Deliberate — see
  [ADR-0001](docs/prd/decisions/0001-abc-over-protocol.md). Do not "modernise"
  them to Protocols.
- **Layering is enforced, not advisory.** `domain/` imports nothing from
  `adapters/`, `db/`, or `api/`; `services/` depends only on `domain/` and
  `ports/`. CI checks this with `import-linter`.
- **No source-specific concept escapes its adapter.** If something only makes
  sense for Emby, it belongs in `adapters/emby/` or on `MediaItem` — never on
  `Title`, never in an API response.
- **Identity is our UUIDv7.** `tmdb_id`/`imdb_id` are indexed attributes, never
  primary keys, never identifiers in an API contract.
- **Domain models are frozen — use `.evolve()`, never `model_copy(update=)`.**
  Every `usher.domain` model inherits `DomainModel`
  (`src/usher/domain/base.py`), so `model_copy(update=...)` is reachable on
  all of them but skips validation entirely: it can hand back an instance
  with a wrong-typed or out-of-range field that pydantic still serializes
  without complaint. `.evolve(**changes)` re-validates from scratch and is
  the only sanctioned write path.
- **Ship importers, never data.** No third-party metadata may be committed or
  included in a release artifact — IMDb and TMDb both prohibit redistribution.
  Users run importers and hold their own API keys. Attribution strings stay in
  the API surface.
- **Use `uv`** for all Python work: `uv sync`, `uv run <cmd>`, `uv add <pkg>`.
  Never pip/conda, never activate a venv.
- **TDD.** Failing test first, then implementation.
- **Secrets in `Settings` are `pydantic.SecretStr`**, never plain `str` —
  `database_url`, `secret_key`, `tmdb_api_key`. Unwrap with
  `.get_secret_value()` only at the point of use (e.g. handing a DSN to
  `create_async_engine`); never store the unwrapped value in a variable that
  outlives that call, and never let it reach a log line or an exception
  message. This is how `docs/prd/08-operations.md`'s "credentials are never
  logged" rule is enforced rather than merely asserted.

## Verified facts worth not re-deriving

**Emby push works.** Verified 2026-07-29 against the live server with a normal
non-admin token: `/embywebsocket` upgrades (101), delivers periodic `Sessions`,
and pushes `UserDataChanged` within seconds of an out-of-band state change. Two
earlier negative findings were both wrong — see
[ADR-0004](docs/prd/decisions/0004-push-over-polling.md).

Health-check caveat: a handshake against *any* path succeeds, so a successful
upgrade is not a health signal. Assert on received messages instead.
**Re-measured 2026-08-02 and it is worse than that**: a socket carrying **no
credential at all** upgrades, accepts the subscription, and then delivers
`Sessions` *more* often than an authenticated one. So neither an upgrade nor
arriving messages establish that a channel is the one you think it is —
[ADR-0018](docs/prd/decisions/0018-push-health-is-a-message-ledger.md), and
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

**`SELECT … FOR UPDATE SKIP LOCKED` is the whole of the queue's exclusion,
and both wrong spellings *hang* rather than answer.** Verified against
`pgvector/pgvector:pg17` by deleting each in turn from
`usher.db.repositories.jobs`: a bare `FOR UPDATE` makes the second worker
block on the first's uncommitted row lock, and removing the locking clause
entirely makes both workers read the same pending row so the second's
`UPDATE` blocks on the same lock one statement later. Neither returns a wrong
answer; both wait forever. So the concurrency cases in
`tests/integration/test_job_queue.py` bound every claim with
`asyncio.wait_for` — `pytest-timeout` is deliberately not a dependency, since
the timeout belongs to the two cases that need it rather than to the runner.

**A concurrency test must assert on *observed overlap*, not on a count.**
"Exactly one of two claimers got the job" is also what a serialised pair of
claims produces — the M3 failure verbatim, where a deleted single-flight lock
let a concurrency test pass five runs in a row. `JobQueueContract`'s harness
releases N claimers through an `asyncio.Barrier` and records the wall-clock
interval each claim occupied; `overlapping()` fails unless those intervals
genuinely intersect. Measured on this host: the two windows share **76.2%** of
their union.

**A concurrency claim whose failure mode is a *deadlock* needs a second kind
of case, and every burst around it needs a bound.** M5's `InMemoryEventBus`
exists to make "a slow subscriber never blocks a publisher" true, and the
one-line mutation that breaks it — `await queue.put(...)` for `put_nowait` —
does not answer wrongly, it hangs. Three consequences, all measured:

- **A timing case can only ever report a timeout against it**, so the M5
  plan's instruction to "confirm it fails on the interval assertion and not
  on a timeout" is unachievable. What has teeth is driving the coroutine
  **one step by hand**: `coro.send(None)` raises `StopIteration` for a
  coroutine that never awaited and hands back a future for one that parked.
  No scheduler, no clock, no timeout; it fails on its own assertion in
  microseconds, and it cannot be satisfied by a serialised run because it
  never involves two tasks. Fill the queue first — `asyncio.Queue.put` on a
  queue with room does not await either.
- **An unbounded burst turns that mutation from KILLED into HUNG**, which in
  a sweep log reads like a mutation nothing observed rather than one
  everything caught. It happened twice on this milestone, in two files, which
  is why `tests/contract/event_publisher_contract.publish_all` exists and
  every burst goes through it. Whole-suite, the mutation now fails 5 cases in
  46.7 s against a 42.8 s baseline, and the 4 s difference *is* the bounds
  firing.
- **The operational case is still worth keeping, and its harness has to
  subscribe before it publishes.** `asyncio.create_task` only schedules, so
  the first publish in the plan's draft reached an empty subscriber set and
  the reader parked forever — the case timed out on its own harness rather
  than on the bus. With the reader signalling first: the publisher's window
  sits inside the window a subscriber spent parked and unread for **99.3–99.6%
  of their union over five runs** (publish 4.3 ms, parked 4.4 ms), against
  `JobQueueContract`'s 76.2% and group D's 62.6%.

**A mutation can survive because CPython collected it, not because the code
is right.** "Subscribe outside the generator so the `finally` never runs"
survived the whole SSE suite when spelled as
`await bus.subscribe(...).__aenter__()` with the context manager left
unreferenced: refcounting destroys the `_AsyncGeneratorContextManager`
immediately, the async generator's finalizer closes it, and the `finally`
runs anyway. Spelled with a strong reference retained, the same mutation
fails `test_a_disconnect_unsubscribes` at once. A leak test only tests a leak
if the mutation actually leaks.

**`httpx.ASGITransport` buffers the whole response and therefore cannot test
SSE at all.** Its `handle_async_request` runs `await self.app(scope, receive,
send)` to *completion*, collects every `http.response.body` into a list, and
only then builds a `Response` over the joined bytes — so
`client.stream("GET", "/events")` against a route whose whole purpose is not
to complete blocks inside the transport forever, and every case written
against it would hang rather than fail. `tests/fakes/
streaming_asgi_transport.py` is the replacement: the app runs in a task,
`http.response.start` resolves a future, chunks go on a queue, and
`aclose()` sends `http.disconnect`. Its scope carries
`spec_version: "2.3"`, matching uvicorn 0.51's own, and that is load-bearing
— `StreamingResponse.__call__` only runs `listen_for_disconnect` below spec
2.4, so at 2.4+ a client going away would not cancel the body iterator and
the route's `finally` would never run.

**`status.HTTP_422_UNPROCESSABLE_ENTITY` is deprecated behind a Starlette 1.3
module `__getattr__`, so it warns once per *request*, not once per import.**
Use `HTTP_422_UNPROCESSABLE_CONTENT`; both are 422. This suite deliberately
runs with no expected warnings, for the reason the `testcontainers` shim was
replaced: a suite with one permanent warning is a suite where the next real
one is invisible.

**A replay ring and a per-subscriber queue are fed by the same `publish`
calls, so a lazily-resolved replay duplicates.** `InMemoryEventBus.subscribe`
snapshots the ring *before* it adds the subscriber, with no `await` in
between. Resolved lazily at the first `__anext__` instead — which is what the
M5 plan's draft did — everything published in the window between is in both
halves and the client sees it twice. The window is real: `api/routers/
events.py` reaches its first `anext` through an `asyncio.wait_for`, which
yields to the loop, and the push lane publishes from another task.

**Bulk loading bypasses the repository, and the SQL has three traps.**
Verified against `pgvector/pgvector:pg17` on 2026-07-30, all three of which
`usher.db.repositories.bulk` is built around:

- `ON CONFLICT` must repeat a partial index's predicate, or Postgres raises
  `InvalidColumnReferenceError: there is no unique or exclusion constraint
  matching the ON CONFLICT spec`.
- One statement may not hit the same conflict target twice —
  `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect row
  a second time`. Every staging read is `SELECT DISTINCT ON (<target>)`.
  IMDb's dumps and Wikidata's crosswalk both really contain such duplicates.
- `xmax = 0` in `RETURNING` is the only way to tell an insert from an update;
  rowcount reports their sum.

`asyncpg`'s binary `COPY` is strictly typed (a `str` into an `integer` column
raises `TypeError` client-side) and CHECK constraints fire during `COPY` into
a *constrained* table, so one bad row aborts its batch. Reach the driver with
`(await (await session.connection()).get_raw_connection()).driver_connection`.
This project's staging tables are deliberately unconstrained, which moves
that failure one statement later — see the staging note below.

**`ON CONFLICT DO UPDATE` cannot read a CTE, and that is what makes M4's
watch-state merge two statements.** Verified 2026-07-31 against
`pgvector/pgvector:pg17`. Three findings, in the order they bite:

- `ON CONFLICT (kind, key) DO UPDATE SET priority = d.a`, where `d` is the
  statement's own CTE, fails with `missing FROM-clause entry for table "d"`.
  Only `excluded` and the target table are in scope.
- **The natural one-statement spelling of the watch-state merge silently
  zeroes real play history.** `watch_states.play_count` is `NOT NULL`, so
  the insert path must write `COALESCE(play_count, 0)` — and that collapse
  happens before the conflict clause runs, so `excluded.play_count` is `0`
  rather than `NULL` and
  `COALESCE(excluded.play_count, watch_states.play_count)` always picks the
  zero. Measured on a row holding `play_count = 7`, fed a merge carrying
  `NULL`: reads back **0**. This is exactly the failure ADR-0014 exists to
  prevent, arriving at the one layer where it is permanent.
- **`last_played_at` survives that same statement**, because it is nullable
  and therefore never collapsed. So "the natural spelling zeroes history" is
  true of exactly one of the two columns, and a test suite that checked only
  the timestamp would have ratified the bug. The two need separate cases.

The working shape is `UPDATE … FROM deduped` (where the `NULL` is still
`NULL` and still in scope) followed by `INSERT … ON CONFLICT DO NOTHING` —
two statements per conflict target, four per batch, all set-based.
`usher/db/repositories/watch_state.py`.

**`watch_states` has a `BEFORE UPDATE` trigger that owns `updated_at`.**
`trg_watch_states_set_updated_at` assigns `now()` unconditionally (the core
schema creates it alongside `sources` and `titles`; `media_items` has none
deliberately). So a merge's own `updated_at = observed_at` lands on the
*insert* path only, and a merged row's stored `updated_at` is its write
instant. Benign for the "latest `updated_at` wins" conflict rule — if
anything the more honest reading — but it means that assignment is not
observable on the update path, and `FakeWatchStateRepository` stores
`observed_at` on both paths, so the two diverge there. Pinned by
`tests/integration/test_watch_state_repository.py::test_the_update_trigger_owns_updated_at`.

**`:param::type` does not work in a SQLAlchemy `text()` statement.** Its
bind-parameter regex treats a name immediately followed by `::` as a
Postgres cast and skips the bind entirely, so `:source_id::uuid` reaches the
driver as that literal string and asyncpg answers
`PostgresSyntaxError: syntax error at or near ":"`. Verified by compiling
both spellings against the asyncpg dialect. Use `CAST(:source_id AS uuid)`.

**That same regex scans SQL *comments*, so `:name` inside a `--` line
declares a real bind parameter.** Same family as the trap above, opposite
direction: there the bind is silently skipped, here one is silently created.
A comment reading `-- lower(t.name), not lower(:name) against t.name` made
every single call to that statement raise
`sqlalchemy.exc.InvalidRequestError: A value is required for bind parameter
'name'` — with the offending token visible only in the echoed SQL, inside a
comment nobody reads when debugging a bind error. Found by running it
(M4 group C2, `usher/db/repositories/matching.py`). Write a placeholder that
is not colon-prefixed when a comment needs to quote a parameter spelling.

**`now()` is `transaction_timestamp()` and is frozen for the life of a
transaction; `clock_timestamp()` is the instant the statement runs.** Both
appear in this schema and the difference is load-bearing in two places:

- `usher.db.repositories.jobs` uses `clock_timestamp()` in all four of its
  statements. `requeue_running`'s `updated_at <= clock_timestamp() -
  interval` cannot match a claim made in the same transaction if both sides
  read the same frozen `now()`, and a job that failed twenty minutes into a
  long transaction must back off from *now* rather than from when that
  transaction opened. The mutation back to `now()` fails three cases.
- The `set_updated_at()` trigger the core schema installs assigns `now()`,
  so **two updates to the same row inside one transaction read back the
  identical `updated_at`**. `tests/integration/`'s per-test fixture is one
  long transaction, which makes "the second write is later than the first"
  unobservable there — `tests/integration/test_episode_repository.py::
  test_the_update_trigger_owns_updated_at` backdates the row with a raw
  `INSERT` (the trigger is `BEFORE UPDATE`, so an `INSERT` dodges it; a plain
  `UPDATE` does not) to give the stamp something to move away from.

**`UPDATE … RETURNING` promises no row order, and at real queue depth it is
not the order you selected.** `PostgresJobQueue`'s claim is a locking,
`LIMIT`ed `SELECT` in a CTE plus an `UPDATE … FROM` it. Measured on
`pgvector/pgvector:pg17` at 2,000 / 50,000 / 300,000 pending rows: the
selection stage is `Index Scan using ix_jobs_claim` at every size, while the
*update* stage moves from `Hash Join` over a `Seq Scan` (2,000 rows, where a
seq scan really is cheaper — cost 45) to `Nested Loop` + `Index Scan using
pk_jobs` from 50,000 up. So `RETURNING` hands rows back in heap order on a
small table, and an outer `ORDER BY` over the data-modifying CTE is what makes
a documented claim ordering true rather than incidental. It also means an
unscoped "no `Seq Scan` anywhere" plan assertion fails on a small fixture for
a plan that is correct at scale — scope it to the stage that has an ordering
to serve.

**A second `ORDER BY` key that the chosen index already carries is
unobservable.** `ix_jobs_claim` is `(priority DESC, created_at) WHERE status =
'pending'`, so deleting `created_at` from the claim's own `ORDER BY` survives
every ordinary test: the index supplies it. Forcing `SET LOCAL
enable_indexscan = off` is what makes it observable, and only in combination
with two other things — a row re-written by an `UPDATE` (so heap order and
`created_at` order disagree at all) and a `LIMIT` smaller than the candidate
set (so the key decides *which* rows are kept, not just how they are
returned). Worth knowing before writing a plan-independent ordering test.

**A test that commits through `usher.db.staging` leaves its staging table
behind.** `stage_records` creates the table with DDL, Postgres DDL is
transactional, and the integration suite's usual isolation is a rolled-back
transaction — so only a test that *commits* (the job queue's concurrency
harness, which needs two real backends) leaks one. It surfaces as
`test_migration_matches_the_orm_metadata` reporting schema drift in a *later*
file, so the queue suite passes alone and takes the migration test down in
combination. Such a fixture must `DROP TABLE IF EXISTS stg_*` in its cleanup.

**A staged `COPY` does not fire the destination's CHECK constraints**, on
this project's path, because `usher.db.staging`'s staging tables are
declared without constraints. The violation surfaces one statement later, at
the `INSERT … SELECT`, which goes through SQLAlchemy and is therefore a
`sqlalchemy.exc.IntegrityError` a repository can translate. Had the
constraint been on the staging table, `copy_records_to_table` runs on the
raw asyncpg connection, outside SQLAlchemy's error translation, and would
raise `asyncpg.exceptions.CheckViolationError` straight past any
`except IntegrityError`. Do not add constraints to a staging DDL without
giving its caller a second `except`.

**`tmdb_id` is unique per `kind`.** TMDb's movie and series id spaces overlap
on 26,968 ids (measured against Wikidata, 2026-07-30 — 47.3% of all series
ids it knows). `ix_titles_tmdb_id_kind`, and `get_by_tmdb_id` takes a
`TitleKind`. [ADR-0011](docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).

**IMDb TSVs have no quoting mechanism** and their title fields contain
literal `"` (21 in the first 553,395 rows of `title.basics.tsv.gz`).
`csv.reader`'s default `QUOTE_MINIMAL` silently strips them — verified. Parse
with `line.split("\t")`.

**Wikidata's crosswalk is seconds, not an hour.** The three property joins
measured 14.5 s / 2.1 s / 1.1 s unchunked. WDQS's timeout surfaces as
`HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
`Retry-After`. A live end-to-end run stored 336,200 pairs.

**Suspending `ix_titles_sort_name`/`ix_titles_name_lower_year` during Phase 0
is a real, if modest, win — kept, not emptied.** Measured 2026-07-30 against
the live `title.basics.tsv.gz` (1,271,138 retained titles): 35.8 s suspended
vs 40.2 s kept (11.0% faster), and the rebuilt pair is ~24% smaller (97 MB
vs 127 MB) than building them incrementally across the same load. Only
applies to a first bootstrap (`bulk_load_window` declines on a non-empty
`titles`), so the saving costs nothing when it doesn't apply. See PRD 04's
Phase 0 section for the full numbers.

**`PostgresImportRunRepository.save()` must roll back on a caught
`IntegrityError`, not just translate it.** Without the rollback, Postgres
leaves the *session* — not just the failed call — with an aborted
transaction, so the very next statement on it raises `sqlalchemy.exc.
PendingRollbackError` instead of running. `BootstrapService.import_dataset`'s
except handler is exactly such a next statement, so the missing rollback
broke its documented "does not re-raise" contract for real, verified against
real Postgres with two engine-bound sessions racing to bootstrap the same
dataset (`tests/integration/test_import_run_repository.py`). Deliberately a
full `session.rollback()`, not a `PostgresTitleRepository`-style SAVEPOINT —
see `usher/db/repositories/import_run.py`'s module docstring for why this
repository's one caller never has independent pending work on the session
worth a SAVEPOINT protecting.

**Fixing that session-poisoning bug surfaced a second one, one layer up, in
`BootstrapService.import_dataset` itself: the loser's failure handler
overwrote the winner's checkpoint.** Once `self._runs.get(dataset.name)`
after a caught `RepositoryConflict` stopped raising and started actually
returning a row, it returns the *other*, winning process's row — the loser
never got one of its own (`start()` never returned it one). The except
handler used to re-fetch by dataset name unconditionally and evolve+save
`FAILED` onto whatever it found, which is correct when that row is the
caller's own (a `_drain` failure, after `start()` succeeded) but silently
corrupts a legitimately `RUNNING` or already-`COMPLETED` import when it
belongs to someone else (a `start()` conflict) — worse than the crash it
replaced, because the crash was loud and this would not have been: a
subsequent resume reads exactly that corrupted record. `RepositoryConflict`
can only ever reach `import_dataset` from `start()` itself — once any row
exists for a dataset, every later `start()`/`save()` call updates that same
row rather than competing for a new one, so `_drain`'s own `save()` calls
(which always update the id `start()` already returned) cannot trigger it.
That made the fix a clean split: a `RepositoryConflict` from `start()`
specifically now goes to `_concede_to_other_owner`, which touches nothing
(no `save`, no `commit`) and returns the current owner's row exactly as
stored; every other `UsherPortError` path is unchanged. Verified against
real Postgres with a forced two-session race
(`tests/integration/test_bootstrap_concurrency.py`) — reproduced the
overwrite on the pre-fix code first (the winner's row read back `FAILED`
with the loser's unrelated conflict message), then confirmed the fix
leaves it untouched. The unit-level fakes needed a matching fix to even be
capable of catching this: the original conflict test double raised
`RepositoryConflict` with no competing row present at all, so asserting
only "the caller didn't crash" passed both before and after either bug —
it needs a real winner row seeded first, and an assertion that it comes
back byte-for-byte unchanged.

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

**`FakeTitleRepository` and `FakeTitleMatchRepository` are one table and are
now wired together.** `TitleRepository.add` flushes, so a stub the match
stage just wrote is visible to the very next `TitleMatchRepository` read.
Keeping two independent dicts made a *correct* service fail rather than a
wrong one pass: `IngestService`'s second walk of a series it had itself
stubbed missed the ladder, re-created the stub, conflicted on
`ix_titles_tvdb_id`, and had nothing left to look the winner up with. Pass a
`FakeTitleRepository` to the constructor; leaving it out is still meaningful
and models a read that missed another worker's committed write, which is the
only deterministic way to produce the race `MatchService`'s conflict handler
exists for.

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

**`Job.key` is the source's own `external_id` for `match` and
`watch_history`, and `(kind, key)` is therefore unique across *sources*.**
Every enqueue site is inside a walk, which holds the external id and would
need a round trip per item to turn it into a `MediaItem.id` — 1,126,674 of
them a walk. The cost is that two servers addressing different items by the
same string collapse into one job; Emby and Jellyfin both mint per-server
GUIDs, so it is currently unreachable rather than merely unlikely. Recorded
on `usher.domain.jobs.Job`.

**`depth()` cannot see a job a worker forgot to complete.** It counts
`pending`, so a `running` row left behind by a `JobWorker` that ran the
handler and never called `complete` reads back as an empty queue —
deleting that call fails nothing in `tests/unit/test_services_jobs.py`
unless the case asserts through `startup()`/`requeue_running`, which is the
only thing that can see it.

**Two guards in M4's services are unreachable through their own port's
contract, and are pinned by direct unit cases rather than deleted.**
`_watch_target`'s "matched to nothing" branch (`resolve_targets` omits an
unmatched item rather than answering with an empty pair) and `_links_for`'s
`is_valid` check (the OTel SDK also drops an invalid `Link` on the way into
a span, so a worker that built one records the same empty `links` tuple).
Both mutations survived the whole suite until the direct case existed.

**No test in this repository makes a network request, and that is measured
rather than asserted.** Verified 2026-07-31, **re-verified 2026-08-01 after
the live TMDb run**, **again after the fixture scrub and the CLI/deps
changes**, **again after M5 group E added an SSE route and a streaming
ASGI transport**, and **again after group F added `GET /titles/{id}`**, by
running the whole suite under a
`sitecustomize.py` that patches `socket.socket.connect`, `connect_ex` and
`socket.getaddrinfo` to raise on anything that is not loopback (`AF_UNIX` is
left alone, so Docker's socket still works and `testcontainers` still reaches
`127.0.0.1`). **1,549 unit + 429 integration passed (2 unit cases skipped), zero blocks**, with
`[netguard] installed` printed by the module itself in the same run and
`socket.getaddrinfo("api.themoviedb.org", 443)` raising
`RuntimeError: NETWORK BLOCKED` in the same environment. Group F's re-run:
**1,586 unit + 442 integration passed (2 unit cases skipped), zero blocks**,
and group G's, after `create_app` grew its two supervised lanes:
**1,623 unit + 450 integration passed (2 unit cases skipped), zero blocks**,
`[netguard] installed` on stderr, and the same `getaddrinfo` probe raising in
the same `uv run` environment. **Re-verified a sixth time on 2026-08-02, at
the end of M5 and after a live run that really did open sockets to a real
Emby server from a throwaway script outside the tree: 1,624 unit + 474
integration passed (2 unit cases skipped), zero blocks**, with
`[netguard] installed` printed by the module itself in the same run, both
`getaddrinfo("api.themoviedb.org", 443)` and `connect(("1.1.1.1", 443))`
raising `RuntimeError: NETWORK BLOCKED` in that same environment, and the
in-process case (`/tmp/netguard/test_guard_is_live_in_pytest.py`) passing
under the same `PYTHONPATH`. The
guard lives outside the tree — it is a check to re-run, not a dependency to
add, because `PYTHONPATH`-injecting a socket monkeypatch into every developer's
suite costs more than it catches.

**Prove the guard is installed before believing a green run.** A
`sitecustomize.py` that is not on `PYTHONPATH` produces exactly the same
output as one that is and blocks nothing — the same family as the
venv-shebang trap. The 2026-08-01 re-run printed `[netguard] installed` from
the module itself and then, in the same environment,
`socket.getaddrinfo("api.themoviedb.org", 443)` raised
`RuntimeError: NETWORK BLOCKED`. Both checks, or the run proves nothing.

**`.env` has two readers with different vocabularies, and that broke the
README's own first step for four milestones.** Docker Compose reads `.env`
to substitute `${...}` into `compose.yml`; pydantic-settings reads the same
file as a settings source with `extra="forbid"`. So a compose-only variable
is an *extra* input to `Settings` — and `USHER_HOST_PORT`, the host-side
publish port shipped in `.env.example` since M1, made `cp .env.example .env`
fail **every** entry point: `uv run pytest` at 1637 passed / 461 errors,
`usher bootstrap-status` and `usher push --probe` with a raw traceback and
exit 1. Found by M5's smoke test on 2026-08-02, present on `origin/main`
since M1, and invisible to 2,098 passing tests for the reason
`tests/conftest.py::clean_environment` exists at all: it neutralises the
`env_file` source so a developer's own `.env` cannot fail the suite. **The
461 errors that did appear came from the one path that fixture cannot
reach** — `tests/integration/conftest.py::_upgrade_head`, session-scoped,
which saves and restores `os.environ` but has no way to hide a file.

- **`extra="forbid"` is worth keeping and is why the fix is a namespace.**
  It is what turns `USHER_LOG_LEVL=DEBUG` into a startup failure rather than
  a line in `.env` that silently does nothing. `extra="ignore"` fixes the
  crash by breaking that; splitting the files leaves compose nothing to read
  (compose substitutes from `.env` and nowhere else, short of `--env-file`
  on every invocation); renaming the one key fixes today and lets the next
  compose variable reintroduce it. So the two readings are separated by
  **name**: `USHER_COMPOSE_*` is dropped before validation, everything else
  under `USHER_` is a setting or a typo.
- **The test that matters is not the one that copies the file.** A case
  building `Settings` from `.env.example` passes against a fix that
  special-cases `usher_host_port`. What fails if a *future* compose variable
  reintroduces the outage is `test_every_variable_compose_substitutes_is_a_
  setting_or_compose_reserved`, which regex-scans the whole of `compose.yml`
  for `${...}` — over the whole file, not just `ports:`, because a variable
  added to a `volumes:` or an `image:` line is the same hazard — plus its
  twin over `.env.example`. Both are needed: the M1 commit that introduced
  `USHER_HOST_PORT` touched both files.
- **Any case written for this must pass `_env_file=` explicitly.** The
  autouse fixture neutralises the class-level `env_file`, so a case that
  relies on it proves nothing. Same shape as the `sitecustomize.py`
  installation proof.

**`env_file:` and `environment:` are different mechanisms, and picking the
second forwarded 5 of 30 settings into the container.** `printenv` inside
the running container showed `USHER_DATABASE_URL`, `USHER_SECRET_KEY`,
`USHER_TMDB_API_KEY` and the two `OTEL_*` — nothing else. 24 documented
settings were unreachable, **12 of them M5's own** (`USHER_PUSH_*`,
`USHER_SSE_*`, both lane switches). `environment:` names one variable at a
time and compose substitutes its value; `env_file:` hands the file over. The
first needs a line somebody remembers to write, which is why the count drifts
by a milestone's worth of settings at a time.

- **`USHER_WORKER_ENABLED` is the one with teeth.** It is documented
  (`README.md`, `.env.example`) and it *works* when delivered directly —
  `/health/ready` reports `"worker": false` and the lane stops. Set in
  `.env`, the only place the docs point at, it was silently ignored, so an
  operator following the README leaves `worker: true` and then starts
  `usher work` in a second container: two workers, and `JobWorker.startup()`
  requeues everything `running`, so each steals the other's live claims.
- **`environment:` still wins over `env_file:`, so what is left in it is
  what the compose *topology* owns**, four keys, each with its reason in the
  file: `USHER_DATABASE_URL` (`postgres`, not `localhost`),
  `USHER_HOST`/`USHER_PORT` (bind-all and 8000 — what `ports:`, `EXPOSE` and
  the healthcheck all assume), and `USHER_SECRET_KEY` (kept as `${...:?}`
  purely for the guard that fails at `docker compose up` with a sentence).
- **Measured with `docker compose config`, not argued**: 5 `USHER_*`/`OTEL_*`
  keys rendered into the container before, **39 after** (38 `Settings` fields
  plus `USHER_COMPOSE_HOST_PORT`, which the app ignores by design — the
  namespace proving itself), with `published: "8100"` → `target: 8000`
  unchanged. `env_file:` uses the long form with `required: false` so a
  checkout with no `.env` still parses and fails on the secret-key guard
  rather than on a missing file.

**A per-process fact logged in a per-pass function is ~17,280 warnings a
day.** `build_worker` logged `no TMDb API key configured; enrich jobs will
not be claimed` unconditionally, and `usher.api.lanes._run_worker` calls it
once per pass at `IDLE_SLEEP_SECONDS = 5.0` — measured at exact 5 s
intervals in the default no-key deployment, and in `usher push` too. The
information is worth surfacing; at that rate it trains an operator to ignore
warnings, which is the failure a log level exists to prevent. It moved to
`composition.metadata_provider`, which is where the decision is *made* and
which each of the three composition roots calls exactly once per process —
and which a push-only deployment never reaches at all, correctly, since with
no worker there are no enrich jobs to leave unclaimed. `usher work` was
already calling `build_worker` once outside its loop, so that root saw one
warning either way; the lane was the one at 5 s. The case that has teeth
drains **three** worker passes and asserts the sink is empty — asserting
after one pass cannot tell "once" from "per pass", the same shape
`test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass` needed.

**M5's final mutation sweep: 56 mutations, 50 killed, and every one of the
six survivors was predicted.** Run 2026-08-02 in place, each mutation
against the **whole** 2,098-test suite rather than its own task's selection.
Baseline green before (`2098 passed, 2 skipped in 47.20s`), restored green
after, the group-G harness's rules enforced throughout — target must appear
exactly once, `cp` backups never `git checkout --`, a run that did not run is
`DID-NOT-RUN`, a syntax error is `BROKEN-MUTATION`, a hang is `HUNG`.
**Zero HUNG, zero DID-NOT-RUN, zero BROKEN**, and every mutation was
dry-run through `ast.parse` before the sweep started so an `IndentationError`
could not be scored as a kill.

The six survivors, and the one prediction that was wrong in the *other*
direction:

- **Five are the plan's own named equivalent mutants, each surviving for
  the stated reason**: the `stale_after` boundary (`<=` → `<`; the clocks in
  those cases step past the boundary rather than onto it), the
  `except asyncio.CancelledError: raise` arm (a `BaseException` in 3.13, so
  the `UsherPortError` arm would not catch it anyway), `list(self._subscribers)`
  (`publish` does not await, so nothing can be removed mid-iteration),
  `rpartition` → `partition` (the epoch is hex and holds no `-`), and
  `is ENRICHED` in place of the rank comparison (both agree on all three
  rungs today).
- **The sixth is `_write_push_available`'s "nothing changed" guard**, which
  is not on the plan's list but *is* already recorded above as an equivalent
  mutant against today's repository: SQLAlchemy emits no `UPDATE` when no
  attribute actually changed, so the `set_updated_at` trigger never fires
  either way.
- **The plan's sixth named survivor was killed, and for a different reason
  than the plan reasoned about.** `socket_logger`'s `propagate = False` was
  predicted to survive because "the level alone is sufficient", which is
  true *as a security property* — and it dies anyway, on
  `test_the_socket_logger_is_re_silenced_on_every_call`, which pins all
  three fields directly rather than asserting the leak. Worth knowing before
  anyone reads that kill as evidence the propagate flag is load-bearing for
  the token.

Three results worth carrying forward. The milestone's headline mutation —
moving `failures = 0` from delivery to connection — **fails 4 cases**, so
PRD 08's "after N failures mark `supports_push = false`" cannot silently
stop firing against a buffering proxy. Deleting the watchdog call fails 4,
and `is_delivering` returning `self.connected` fails **11**, the largest
blast radius in the sweep. And the ADR-0014 mutation on the *third* payload
shape (`play_count=as_int(entry.get("PlayCount"))` in `user_data_states`)
fails 2 — which matters more now that the live run has shown that field
would be *telling the truth*: the test suite forbids reading it on the
strength of a rule about evidence, not on the strength of the value being
wrong.

**M4's final mutation sweep: 39 mutations, one survivor, and the survivor is
an equivalent mutant the code comment predicted.** Run 2026-07-31 in place,
each mutation against the **whole** 1,713-test suite rather than its own
task's selection — which is the point of a final sweep, since a per-task
sweep cannot see collateral in another file. Baseline green before,
restored green after, `/tmp/mutate.py`'s rules enforced throughout (a run
that did not run is `DID-NOT-RUN`, never `KILLED`; the target must appear
exactly once; `cp` backups, never `git checkout --`). **38/39 killed.**

The survivor is `priority = GREATEST(jobs.priority, excluded.priority)` →
`priority = excluded.priority` in `_ENQUEUE`, and it survives because the
same statement's `WHERE jobs.status <> 'parked' AND jobs.priority <
excluded.priority` already guarantees `excluded.priority` is the larger.
`jobs.py`'s own comment says exactly this and keeps both anyway ("one is
*when* to write, the other *what* to write"). Verified rather than assumed:
removing **both** together fails 2 cases, so PRD 03's no-demotion property is
covered — by the `WHERE` clause. So
`test_re_enqueueing_at_a_lower_priority_does_not_demote` passes against a
`SET` clause that would demote, and is really a test of the predicate. Worth
knowing before anyone "simplifies" the `WHERE` on the strength of that case's
name.

Two other results worth carrying forward. `claim-without-skip-locked` is the
only mutation whose run is measurably slower (57.2 s against a ~41.6 s
baseline) — that is `asyncio.wait_for` bounding the blocked claim rather than
the suite hanging, which is why `pytest-timeout` is deliberately not a
dependency. And `usable-ids-filters-nothing` **is** caught (2 cases), by
`test_a_malformed_imdb_id_does_not_abort_the_batch`'s *second* item, whose
only id is unusable — the first item survives the mutation intact, so a
version of that case carrying one item would have ratified it.

**Mutation sweeps on this host: the shell is zsh, and it does not
word-split an unquoted `$VAR`.** A selection passed as `$C="path1 path2"`
reaches pytest as one bogus path, nothing runs, the exit code is non-zero,
and a naive harness records the mutation as caught having measured nothing.
Three were, before the harness started requiring that a run actually ran.
Same family as the venv-shebang trap: the sweep proves nothing and looks
like it proved something.

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

**The TMDb half of M4's live verification: ten open guesses, eight
settled, and the two corrections both went the same way — TMDb is more
silent than the code assumed, not louder.** Run 2026-08-01 against
`api.themoviedb.org/3` with a real v3 key, driving the shipped
`TmdbClient`/`TmdbMetadataProvider`/`usher.adapters.tmdb.mapping`/
`usher.services.matching._confident` from a throwaway script outside the
working tree. **712 requests total**, GET only, no write route of any
kind touched. Before this run **no request had ever been made** from this
repository and every TMDb fixture was a transcription of documentation.
The whole status distribution, since it is the evidence for half the table
below: **699 × 200, 7 × 404, 2 × 401, 2 × 422, 2 × 400 — and no 429 and no
5xx at all.** Every one of the thirteen non-200s was deliberately provoked
and is accounted for in the table below.

| # | Guess | Verdict | Evidence |
|---|---|---|---|
| 1 | TMDb sends `Retry-After` on a 429 | **still unverified** | Zero 429s in 712 requests at 25 rps, and no `retry-after` header on *any* response including the 401s and 422s. Deliberately not provoked. |
| 2 | An invalid `append_to_response` namespace errors | **refuted** | `200`, key silently absent — for a wrong-space namespace *and* for `zzz_not_a_namespace`. |
| 3 | The 404 body shape | **confirmed & recorded** | `{"success": false, "status_code": 34, "status_message": "The resource you requested could not be found."}`, `application/json;charset=utf-8`, on `/movie`, `/tv` and `/tv/{id}/season/{n}` alike. |
| 4 | A v4 read access token is JWT-shaped | **unverifiable here, cost bounded** | The configured credential is a classic 32-hex v3 key; `_is_v4_token` correctly says no. A false positive was measured instead: the v3 key sent as `Authorization: Bearer` answers **401** (`status_code: 7`), i.e. loud and immediate, never a wrong answer. |
| 5 | The changes window's inclusivity and its 14-day cap | **confirmed, and it is the boundary** | `start == end` is a valid one-day window (4,278 results); `[d, d+1]` covers both days deduplicated; `[today-14, today]` → 200; `[today-15, today]` → **422**, `"Invalid date range: Should be a range no longer than 14 days."` The shipped clamp sits exactly on it with nothing spare. |
| 6 | `credits` is a valid TV append namespace | **confirmed** | Present with 14 cast entries. `aggregate_credits` is *also* valid — a second view, not a replacement. |
| 7 | `append_to_response=season/N` works | **confirmed — see below** | It does, and it collapses a series from 1+N requests to 1. |
| 8 | A season the series lists that 404s on its own route | **still unverified** | 320 listed seasons across 30 series, **zero** absent. The propagate-and-park branch has still never met a real occurrence. Sample skews popular, so it is weak evidence of absence. |
| 9 | Search orders by relevance with the obvious answer first | **confirmed** | 263 of 266 confident resolutions were TMDb's **first** result (max rank 3; series 126/126 at rank 0), and the top result was an exact normalised name match on 269 of 320 probes. |
| 10 | `spoken_languages[].iso_639_1` and `origin_country` are well-formed | **confirmed** | Zero anomalies over 59 detail payloads; `origin_country` present on 29/29 movies and 30/30 series, always a list of strings. |

**Two things live TMDb contradicted, both now fixed with a failing test
first.**

- **A 4xx that is not a 429 is `PortDataMalformed`, not `PortUnavailable`.**
  Observed: **422** for a 15-day change window (`status_code: 20`) and
  **400** for a 21-item `append_to_response` (`status_code: 27`, *"the
  maximum number of remote calls is 20"*). Both were classified as outages,
  so `JobWorker` would spend five rate-limited retries and a backoff
  schedule reaching the identical answer and then park with the wrong
  reason. 408 is excluded and stays retryable — TMDb has never been
  observed sending one, but `Settings.tmdb_base_url` exists so a household
  can front TMDb with a proxy.
- **TMDb's year filter is exact where the match ladder's is ±1.** All 294
  candidates returned across 320 probes carried *exactly* the year asked
  for, so `_confident`'s own `abs(candidate.year - item.year) <= 1` never
  fired once and tier 4 silently ran at ±0. 26 of 320 came back empty
  rather than one year off; re-asking those without the year resolves
  **13**, every one a title TMDb dates a year away from IMDb (Danny Phantom
  2003/2004, Toast of London 2012/2013, …). `TmdbMetadataProvider._search_one`
  now retries yearless when the filtered search finds nothing. A *fallback*
  and not a widening, because dropping the filter outright was measured too
  and is worse: 6 of 133 already-resolving names stop resolving, since
  "exactly one survivor" across every year at once is a harder test than
  within one.

**`_confident` against TMDb's own search: 83.1%, and 87.2% with the
yearless fallback.** The number the Emby half explicitly could not take.
320 IMDb names (160 movies / 160 series) stratified into four `numVotes`
bands, each searched through the shipped provider and judged by the shipped
rule: **87.5% of movies**, **78.8% of series**; by band, 90.0% / 91.3% /
81.3% / 70.0% descending, so a real library — which sits at the popular end
— should expect the high eighties to low nineties. Failures decompose as 26
zero-result, 22 results-but-no-exact-name, 6 ambiguous. Compare tier 3's
72.2%/75.3% for the identical predicate over the local 1.27M-row catalog:
**different candidate sets and different name samples, so these are
counterparts, not a before/after.** The IMDb-derived names are a proxy for
Emby names, which were not available to this run — stated rather than
implied.

**`append_to_response=season/N` works, and it is worth ~10x on the series
half of the enrichment path.** One request carrying
`credits,keywords,images,videos,external_ids,content_ratings` plus
`season/0…season/13` — **exactly** TMDb's 20-item ceiling — returned Game of
Thrones' entire hierarchy, **all 373 episodes across 9 seasons**, in place
of the ten requests the shipped path costs. Four supporting facts, each
measured because the change rests on it:

- The ceiling is **enforced**: 21 items is a **400**, `status_code: 27`.
  Six namespaces already appended leaves exactly 14 season slots.
- `season/0` (specials) appends like any other, 300 episodes on GoT.
- An unlisted season number is **silently omitted**, not an error — which
  is also the cheap detector guess 8 was scanned with.
- The appended block is identical to the season's own detail response
  **but for a missing top-level `id`**, and the series' own `seasons[]`
  summary carries that same id (3627/3624/107971 on GoT, byte-identical to
  the season route's). So `_compose_seasons`' existing merge-over-the-summary
  would lose nothing.

**Not implemented.** It changes PRD 03's request table, PRD 04's crawl
arithmetic and `TmdbMetadataProvider.fetch`, and belongs in its own change
rather than folded into a verification run.

**The arithmetic, corrected 2026-08-01 — it was internally inconsistent
when first recorded, and the wrong number was the headline one.** The
shipped path costs `1 + N` requests for a series (one detail, one per
season); the appended path costs 1. At **32,409 series** and a **median of
9 seasons** that is 32,409 × 10 = **~324k requests** against **~32k**, i.e.
**~10x** — not the "~190k → ~35k, ~5x" first written here. `~190k` was
[PRD 04](docs/prd/04-catalog-bootstrap.md)'s Phase-3 tier-1 line, "~189k
titles with ≥100 IMDb votes", borrowed one section over: a *whole-catalog
title* count read as a *series request* count. Nothing measured it. The two
figures cannot both be right — 32,409 × 10 is 324k, and ~190k would need a
median of ~4.9 seasons.

**The median is measured, and its sample is not a library.** 320 listed
seasons across the 30 series the 2026-08-01 run walked, which is also the
sample guess 8 is scanned against and which that entry already calls
popular-skewed and weak evidence. Popular series have many seasons, so a
real 32,409-series library's median is very likely *lower* and ~324k is an
upper bound on the measurement taken rather than a prediction. Recorded
with its sample instead of laundered into a constant — the same treatment
`_confident`'s 72–75% and 83.1% get, and for the same reason.

**~32k, not ~35k, and the difference is the ceiling.** One request per
series is 32,409 exactly. Six namespaces leave 14 season slots, so a series
with more than 14 seasons needs a second request; that is a small tail, so
~32k is the figure and ~35k a generous allowance for it. Both are the same
number to one significant figure; the ~10x is what matters and it holds
either way.

**TMDb's movie/TV divergence runs through three layers of its API, not
one, and all three are now measured rather than read.** The field-name and
endpoint rows were read from `developer.themoviedb.org` on 2026-07-31 and
**every one was confirmed live on 2026-08-01** over 29 movie and 30 series
detail responses.

- **Field names.** `title`/`name`, `original_title`/`original_name`,
  `release_date`/`first_air_date`, `runtime` (minutes) against
  `episode_run_time` (an array), `keywords.keywords` against
  `keywords.results`, a top-level `imdb_id` against `external_ids.imdb_id`.
  Tabulated in `usher.adapters.tmdb.mapping`'s docstring. Live: 29/29
  movies carried the whole movie column and **none** of the series column;
  30/30 series the mirror, with `external_ids.tvdb_id` non-null on all 30.
- **Endpoints.** `/movie/{id}` against `/tv/{id}`; `/search/movie` with
  `primary_release_year` against `/search/tv` with `first_air_date_year`;
  `/movie/changes` against `/tv/changes`; and a series' episodes live
  behind `/tv/{id}/season/{n}`, which has no movie counterpart at all.
- **`append_to_response` vocabularies.** `release_dates` is a movie-only
  namespace and `content_ratings` is the TV-only equivalent. **The
  consequence was stated wrongly and is corrected**: a shared list does not
  ask for a namespace that does not exist and get an error, it gets `200`
  with the key absent. So the failure is silent — half the catalog loses
  its certification on a response that looks entirely successful — which is
  a *stronger* reason for the split than the one previously recorded.

**`episode_run_time` is empty on 86.7% of series** — `[]` on 26 of 30 live
detail responses, Game of Thrones among them. `Title.runtime_minutes` is
simply not a fact TMDb still holds about most television, and `None` is the
answer rather than a mapping gap. The committed `series.json` fixture
carries the rarer populated shape, so the common one needed its own case
(`test_an_empty_episode_run_time_is_the_common_case_and_is_not_a_failure`).

**ADR-0011 is not a theoretical hazard: 12 of 14 small ids probed are live
in both id spaces, and every pair is an unrelated work.** Live 2026-08-01 —
`550` is *Fight Club* and *Till Death Us Do Part*; `238` is *The Godfather*
and *Star Cops*; `680` is *Pulp Fiction* and *Shaquille*; `605` is *The
Matrix Revolutions* and *Sabrina, the Teenage Witch*. No movie payload
carried a `name` key and no series payload a `title` key, so
`kind_of_payload`'s exactly-one rule resolved all 24 correctly and
`title_from_payload` produced two unrelated canonical titles per id with no
possibility of conflation.

**A kind-less TMDb reference is `PortDataMalformed`, never a guess.**
ADR-0011 at the request layer: 26,968 ids are live in both spaces, so
`GET /movie/{id}` for a ref that meant a series returns a **real payload
for an unrelated film**, which is then written onto the title as enriched
metadata with no error anywhere. Verified live through the real provider.

**A TMDb 404 is `PortDataMalformed`, not `PortUnavailable`.** The catalog
holds 291,737 TMDb ids from a bulk export that ages, and TMDb answers 404
for an id it has merged away. Retrying cannot turn any of them into an
answer, so this is the branch that makes `JobWorker`'s park-immediately
path fire in production rather than only in a test. Confirmed live, body
shape and all, and now generalised to the whole 4xx range above.

**The committed TMDb fixtures were transcriptions and they held up.** The
first shape diff any of them has ever had (2026-08-01, via
`scripts/capture_tmdb_fixture.py`) found **not one key in any fixture that
the live response lacks** — every field the mapper reads was transcribed
correctly from documentation. The live API carried six the fixtures did
not, all now added shape-only so the *next* diff is empty and a real drift
is visible: `softcore` (a boolean, on movie details, series details, search
results and the change feed), `iso_3166_1` on every `images.*` entry, and
**`networks` on the season detail**, which the `tv-season-details`
reference page does not show. Two differences are deliberately left open
because they are value-level, not shape-level, and closing them would make
a fixture claim something false — see `tests/fixtures/tmdb/README.md`.

**Still not verified after this run, named rather than implied:** a real
429 and whether one carries `Retry-After`; a v4 read access token in any
form (so `_is_v4_token`'s positive branch has never been exercised against
a real credential); a season TMDb lists that its own route refuses; TMDb's
behaviour under sustained concurrency (this run was sequential through one
token bucket at 25 rps); and any of it against a non-`US` `tmdb_region`.

**A TMDb v3 API key in the query string lands in every trace.**
`HTTPXClientInstrumentor` (wired in `configure_tracing`) records the full
URL as a span attribute, and TMDb v3 has no header form for a v3 key. So
`TmdbClient` sends an `Authorization: Bearer` header whenever the
configured secret is JWT-shaped (a v4 "API Read Access Token", which
TMDb's own docs say works on v3 endpoints and gives "the same level of
access") and falls back to `api_key` otherwise. For the same reason no
exception message in that module may carry a URL — `EmbySession`
interpolates the httpx exception into its own message and explains why
that is safe *there*; it is not safe here.

**`EnrichmentState.ENRICHED > EnrichmentState.STUB` is `False`, and the
consequence is not the one you would guess.** A tier guard spelled as a
direct comparison does not "sometimes downgrade" — it never promotes
anything at all, silently, because `ENRICHED` is lexicographically below
both other rungs. So a test asserting "an enriched title stays enriched"
passes against the bug (`ENRICHED` is the top rung, so nothing moves
either way) and the case that catches it is **promoting a stub**. The M4
plan's own mutation table pointed at the wrong one.

**A failure handler that resets the tier is invisible to a test seeded at
that tier.** `enrichment_state=SKELETON` alongside the error is exactly
what a careless handler reaches for, and a case seeded with a skeleton
cannot see it — the write is a no-op. Found by mutation on
`EnrichService`; `tests/unit/test_services_enrich.py` parametrizes over
all three rungs now. Same family as "a concurrency test must assert on
observed overlap, not on a count".

**Enrichment must read season ids back before writing episodes.**
`MetadataProvider.to_result` mints a fresh UUIDv7 per `Season`, and a
season the catalog already holds keeps the id it was inserted with — so
an episode carrying the minted id names no row and fails on
`fk_episodes_season_id_seasons`, on the **second** enrichment rather than
the first. `IngestService._ensure_seasons` re-reads for exactly this
reason; `EnrichService._store_hierarchy` now does too, and no port fake
can see either (a dict has no foreign keys).

**A job key that does not parse must become a `UsherPortError` inside the
handler.** `uuid.UUID("not-a-uuid")` raises `ValueError`, and `JobWorker`
deliberately lets anything that is not a `UsherPortError` propagate — "a
bug in a handler is not an upstream failure". So one corrupted `enrich`
key would take the worker process down instead of parking its own job.
`usher.services.handlers` converts every key, once.

**`SQLAlchemyInstrumentor` was wired and produced no spans at all, for
three milestones.** `instrument()` patches the *module attribute*
`sqlalchemy.ext.asyncio.create_async_engine` with `wrapt`; `usher.db.base`
did `from sqlalchemy.ext.asyncio import create_async_engine` at module
scope, which is evaluated long before `configure_tracing` ever runs and
binds the **original, unwrapped** function into that namespace forever.
Verified directly: after `instrument()`, `usher.db.base.create_async_engine`
and `sqlalchemy.ext.asyncio.create_async_engine` are different objects. The
failure is silent in the worst way — the package is installed, the wiring
reports success, `connect` spans still appear (`_wrap_connect` patches
`Engine.connect` on the *class*, so it fires however the engine was built),
and not one `SELECT`/`INSERT`/`UPDATE` span is ever produced. `build_engine`
now calls `sa_asyncio.create_async_engine` through the module. A test that
accepts a `connect` span is not enough; assert on a *statement* span.

**Pipeline spans nest under the request's server span, asserted as
parentage.** `tests/integration/test_pipeline_spans.py` walks the parent
chain `match.title → ingest.item → sync.reconcile → GET …` on a real
`create_app()` through a real request, with SQLAlchemy statement spans
under the pipeline span that issued them. A pipeline that started its own
*root* spans passes every other assertion in this repository — valid ids,
exporting traces, PRD 10's span names all present — and fails only this.
A worker's `job.*` span is the deliberate exception: a root with a `Link`.

**`set_meter_provider` is set-once and `_ProxyMeter` caches, exactly like
the tracer.** Every `usher` module calls `metrics.get_meter(...)` at import
time, so each holds a `_ProxyMeter` whose instruments are `_Proxy*` shells
that cache the first real instrument they are handed. Without
`tests/conftest.py::reset_otel_meter_provider`, three rounds of "install a
`MeterProvider` with an `InMemoryMetricReader`, record through
`usher.services.jobs._job_duration`, read the reader" print the metric once
and then raise `AttributeError: 'NoneType' object has no attribute
'resource_metrics'` — the second `set_meter_provider` is refused and the
second reader is never registered with any provider.

`SQLAlchemyInstrumentor` needs the same treatment and the shared reset
cannot give it: it resolves its tracer *once*, eagerly, into a `wrapt`
closure, so it is a real `Tracer` rather than a `ProxyTracer` and nothing
in `usher.*` holds it. `tests/integration/test_pipeline_spans.py`'s own
fixture calls `SQLAlchemyInstrumentor().uninstrument()` before installing
its provider; without that line its database-span case passes alone and
finds an empty exporter when it runs third in its own file.

**An observable OTel callback cannot query this database.** OTel invokes it
from the metric reader's *background thread* and every database call here is
a coroutine on asyncpg, so a callback that queried would have to bounce a
coroutine onto the event loop (`run_coroutine_threadsafe`) and block the
exporter thread on it — a deadlock whenever the loop is itself blocked.
`usher.telemetry.register_queue_gauges` therefore takes a **synchronous**
reader returning the caller's most recent *complete* re-read of the `jobs`
table (`usher work` refreshes it after every pass), which is stale but never
wrong — unlike the counter-incremented-on-enqueue the plan was guarding
against. The SDK also keeps only the **first** observable gauge registered
under a name and silently discards the rest (verified directly), so the
reader is a module global that is replaced rather than a closure captured at
instrument-creation time.

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

**`ON CONFLICT DO UPDATE` with no `WHERE` rewrites every row it touches.**
`_ENQUEUE`'s update clause fired for every job a nightly walk re-saw —
1,126,674 dead-weight row versions a night, plus the WAL and the vacuum, on
a table whose entire purpose is to stay small, for no state change at all
(`priority` was already `GREATEST` of itself and `created_at` is
deliberately untouched). `AND jobs.priority < excluded.priority` makes a
re-seen job cost one index probe and zero writes, and `enqueue` then reports
0 rows written, which is the honest number. A promotion still writes.

**A statement-count assertion needs the right thing held fixed.** "20
episodes and 200 cost the same statements" is hollow when they share one
series: `IngestService._series_titles` only queries for series the page does
*not* carry, so with the whole library in one batch that list is empty and a
per-item spelling of it issues zero statements. Measured — the mutation
survived. Hold the **batch count** fixed and vary the page instead (nine
batches of 5 against nine batches of 50), across many series and many
titles, which is also the production shape: at 32,409 series among
1,126,674 items an episode's series nearly always arrived in an earlier
page.

**A route-driven test commits for real.** `get_session` is the request's
commit boundary, so an integration test that drives a walk through a
*route* writes durably against the session-scoped container — unlike every
rolled-back test in the suite. Leaving `tests/integration/
test_pipeline_spans.py`'s stubbed `titles` and enqueued `jobs` behind took
down four tests in three other files (a duplicate `ix_titles_tmdb_id_kind`,
a queue depth of 2 where 0 was expected, a claim that found 3 jobs instead
of 1, and a global `count_by_state`), each of which passed in isolation.
`media_items` and `sync_runs` go with the source's `ON DELETE CASCADE`;
`titles` and `jobs` do not.

**A read on `media_items.title_id` alone is a read of the whole show, and
`AND episode_id IS NULL` is the whole of the bound.** `IngestService` writes
an episode's row with its series' `title_id` **and** its own `episode_id`,
deliberately — a client browsing a season wants both — so
`WHERE title_id = :id` answers a *series* with one row per episode file, and
999,827 of the one measured source's 1,126,789 items are episodes. Measured
2026-08-01 on the statement `PostgresMediaItemRepository.list_for_title`
actually issues (captured off `before_cursor_execute`, then `EXPLAIN
(ANALYZE, BUFFERS)`'d verbatim; 80,201 `media_items` rows, one 20,000-episode
series): **1 row, 0.251 ms, 21 buffers** with the clause — `Sort ← Bitmap
Heap Scan ← BitmapAnd(ix_media_items_episode_id, ix_media_items_title_id)` —
against **20,001 rows, 22.901 ms, 402 buffers, 3.4 MB of sort memory**
without it. The wrong half is linear in the episode count and the right half
is flat, which is the difference between a response shape and a design
defect. `resolve_external_ids`' title branch carries the identical clause for
the identical reason. `ix_media_items_episode_id` earns its keep twice: M4
added it for the FK's `SET NULL` scan, and the planner reads `IS NULL`
straight out of it.

**A trailing `UPDATE` only separates heap order from id order if it is
*non-HOT*.** The idiom for making a missing `ORDER BY` tiebreak observable —
re-write a row so physical order and the answer disagree — silently does not
work when the update touches no indexed column: Postgres performs a
heap-only-tuple update, the existing index entry keeps pointing at the
original TID, and an `Index Scan` still arrives in the original order.
Measured on `media_items`: re-upserting a row unchanged left `ORDER BY
available DESC, last_seen_at DESC` (no `id`) answering `[a, b, c]` — already
sorted, already passing. Moving `last_seen_at`, which is in
`ix_media_items_sweep`, forces a new index entry and the same read answers
`[b, c, a]`. Every id here is a UUIDv7 minted at insert time, so a run of
plain inserts has id order and storage order as one sequence and no seeding
separates them.

**`FakeJobQueue.enqueue` counts a no-op re-enqueue as a row written, and
Postgres answers 0.** The fake takes its update branch and increments
whatever it changed; `_ENQUEUE`'s `AND jobs.priority < excluded.priority`
matches nothing for work already at that priority. So anything whose
behaviour turns on the *count* rather than on the stored row is untestable
against the fake: `TitleReadService._promote` returns whether an enqueue was
*attempted*, and the version that returned "a row changed" passes all 18
cases in `tests/unit/test_services_titles.py` and then reports
`promoted = False` for every second open of the same stub — telling a client
that an already-promoted title declined to be promoted. Killed only by
`tests/integration/test_services_titles.py`. Recorded as the fake's seventh
divergence rather than fixed, because a fake that modelled the whole
promotion predicate would be a second implementation rather than a stand-in.

**`TitleReadService` holds no `SourceAdapter`, and that is asserted on its
imports rather than on its behaviour.** PRD 08's "a degraded subsystem
narrows functionality; it never fails a request local state can answer" is
only a property of the code if the failing call is *absent* rather than
caught — "it did not raise" is also what a service that swallowed everything
would produce. Two things the obvious check misses, both measured: a
signature check spelled `parameter.annotation in (SourceAdapter, ...)` (or
via `annotation.__name__`) does not see a **string** annotation, which is the
one form needing no import at all; and an `ast.ImportFrom`-only scan does not
see `import usher.ports.source`. Read the annotation as text and walk both
node types. This is what makes M5's deferral of PRD 07's RFC 9457 envelope a
structural claim: with no adapter reachable there is no 503 to give a `code`
to, and the first route whose honest answer is "the source is down and I
cannot serve this from local state" is M9's `POST /titles/{id}/play`.

**A `GET /titles/{id}` leak check may not forbid the word "emby".** The
availability badge carries the name an *operator* typed, and "Living Room
Emby" is a correct value for it — PRD 07's own example spells it that way. A
rule that forbids the substring forbids the feature. What must not escape is
the source's own **item id**, so the assertion is against a distinctive
`external_id` and against the key `external_id`, not against a vendor name.

**The server process runs the lanes, and that is proved by a job
disappearing rather than by an assertion about wiring.** `create_app`'s
lifespan builds a `LaneSupervisor` and starts a push lane per enabled source
plus one job worker (both settings-gated, PRD 01's `--worker` flag as
configuration). A unit test of the supervisor proves it does what it is
told; it says nothing about whether the lifespan tells it anything.
`tests/integration/test_lanes_in_the_server_process.py` commits a real
`match` job, starts nothing but `LifespanManager(create_app(settings))`, and
asserts the row is gone before the app stops — with the mirror case
(`worker_enabled=False`, the row survives) as the control that makes it
evidence. The mutation `await lanes.start()` → `pass` fails exactly that one
case out of 2,072.

**Both lane switches default on, so every test that builds an app has to say
it does not want them.** Nine fixtures now pass
`push_enabled=False, worker_enabled=False`. Without it a worker lane polls
the real `jobs` table under `tests/integration/test_pipeline_spans.py`, which
enqueues jobs through its own probe route and asserts on them; and a push
lane in `tests/integration/test_admin_sources.py` builds the **real**
`EmbyAdapter` against `https://emby.invalid` and opens a socket, because
`dependency_overrides` do not reach the lifespan. Stated per fixture rather
than defaulted in `conftest.py`, so it is greppable.

**`start()` creates tasks and awaits nothing, and the case with teeth drives
the coroutine by hand.** `coro.send(None)` must raise `StopIteration`; a
`start()` that read the source list inline hands back a future instead. That
is what keeps `/health` answering 200 with Postgres down while
`/health/ready` reports 503 — the M5 plan's own draft did
`await self.refresh()` there, which opens a connection, and its own Step 4
then asserted the opposite. The first refresh happens *inside* the refresher
task, which refreshes and then sleeps, so nothing waits `USHER_PUSH_SOURCE_REFRESH_SECONDS`
for its first lane either.

**Per-lane crash isolation comes from one task per lane, not from the
`except`.** Measured: deleting `_guard`'s `except` survives the whole suite,
while removing `return_exceptions=True` from `stop()`'s gather fails **11**
cases on its own — so the two are not the belt-and-braces pair a comment
claimed. What `_guard` buys is that a crashed lane is not silent (without it
CPython reports an unretrieved task exception at GC time, to stderr, with no
source name in it), which needs a log assertion to see. And
`running_sources() == ["B"]` is not a test of isolation: a supervisor whose
second lane was created and never scheduled reports the same thing. The case
asserts B ingests an item pushed *after* A's task is already `done()`.
Two lanes genuinely overlapping is its own measurement — **99.3–99.4% of
their union over five runs**, against a serialised supervisor's 0.0.

**A guard can be right and unobservable, and `_write_push_available`'s is.**
Deleting its "nothing changed" check does not move `sources.updated_at`,
because `PostgresSourceRepository.update` sets attributes on a *loaded ORM
row* and SQLAlchemy's unit of work emits no `UPDATE` when none actually
changed — so the `set_updated_at` trigger never fires either way. Recorded
as an equivalent mutant against today's repository and kept, because the day
that repository issues a bare `UPDATE … SET` a flapping lane moves a column
an operator reads, once per reconnect. Same treatment M4 gave `_ENQUEUE`'s
`GREATEST`.

**`JobWorker.startup()` requeues everything left `running`, so there is one
worker per deployment, not per process.** `requeue_running`'s default
`older_than_seconds=0.0` is correct at exactly one worker and at two steals
the other's live claims. The server now runs one, so a deployment that also
runs `usher work` must set `USHER_WORKER_ENABLED=false` on the server.
`LaneSupervisor` calls `startup()` once rather than per pass, which was
untestable until `idle_seconds` became a constructor argument nothing in
`src/` passes: the case asserts one requeue over three passes.

**Readiness reports the lanes and never gates on them, and the case that
proves it cannot live in the unit file.** `tests/unit/test_api_health.py`'s
app points at an unreachable database, so readiness is *already* 503 there
and both mutations — `all(checks) and lanes.running_sources()`, and moving
`push` inside `ReadinessChecks` where `all(...)` picks it up automatically —
survive every case in it. Against a **reachable** database with no lanes
running, both turn a 200 into a 503 and both die, so that case lives in
`tests/integration/test_health.py`. `LaneReport` is a separate model from
`ReadinessChecks` for exactly this reason: every field of the latter is part
of the status code by construction.

**`SourceStatus` refuses "push available without being authenticated", and
`dataclasses.replace` re-runs `__post_init__`.** So the obvious one-liner
for reporting a running lane's push health —
`replace(status, push_available=self._push_health(source_id))` — raises
`ValueError` out of `GET /admin/sources/{id}/status` for a state a rotated
password produces, on the screen an operator opens to diagnose it. The
lane's answer is taken only when the status is authenticated.

**`usher.composition` is the wiring both roots share, and it needs no
seventh import-linter contract.** `usher.cli` carries one saying nothing may
import it, so shared code cannot live there. The new module sits outside
every contract's source list — and that hole is closed by what it imports
rather than by a rule: it imports `usher.db` and `usher.adapters`, so a core
module reaching it breaks contracts two and three, which report indirect
chains by default (unlike contract six's `allow_indirect_imports = true`).
Verified by planting `from usher.composition import Pipeline` in
`usher/services/push.py`: **4 kept, 2 broken.**

## Commands

Verified working as of Group A (scaffold + config):

```bash
uv sync                          # install dependencies
uv run pytest                    # run the test suite (now needs Docker — see Group E below)
uv run pytest tests/unit         # fast unit tests only, no Docker required
uv run ruff check .              # lint — clean
uv run ruff format .             # format — clean
uv run mypy                      # type check, strict mode — clean
uv run lint-imports              # enforce architecture contracts — 6 kept, 0 broken
```

`[tool.ruff] extend-exclude = ["docs"]` keeps ruff off `docs/plans/*.md` and
`docs/prd/*.md` — ruff 0.16+ formats/lints Python code fences embedded in
Markdown by default, and those two directories hold planning and PRD prose
with embedded code fences that must stay byte-identical for other groups to
transcribe. Without the exclude, an unscoped `ruff format .` silently
rewrites that prose.

Verified working as of Group D (db engine, models, migrations) — requires a
live Postgres (e.g. `docker run -d -e POSTGRES_USER=usher -e
POSTGRES_PASSWORD=usher -e POSTGRES_DB=usher -p 5432:5432
pgvector/pgvector:pg17`), so not part of the default `uv run pytest` run:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head                       # apply migrations
uv run alembic downgrade base                     # reverse them (0001 is fully reversible)
uv run alembic revision --autogenerate -m "..."    # generate a migration from model changes
```

**`--autogenerate` is blind to two categories of change — verify by eye, not
just by running it:**
- **CHECK constraint bodies.** Changing a bound (e.g. loosening
  `ck_titles_year_non_negative`'s `>= 0`) and running `--autogenerate`
  produces an empty `pass` migration with no warning — verified directly.
  This schema deliberately mirrors every Pydantic field constraint as a
  CHECK, so this will eventually bite: tightening or loosening one in a
  model file does not, by itself, get picked up.
- **Triggers and functions** (the three `set_updated_at()` triggers from
  the first migration). These aren't SQLAlchemy `Table` metadata at all, so
  autogenerate never sees them, in either direction — adding, dropping, or
  changing one is always a hand-written `op.execute(...)` migration.

**Import `testcontainers.community.postgres`, not `testcontainers.postgres`.**
The latter is a shim that raises a `DeprecationWarning` at import time and
was the only warning this suite emitted; the community module is the same
class with the same behaviour (confirmed by running the whole integration
suite against it). Changed 2026-08-01 — a shim that announces its own
removal eventually takes it, and a suite with one permanently-expected
warning is a suite where the next real warning is invisible. Still imported
*inside* the `postgres_url` fixture rather than at module scope: `pytest -m
"not integration"` imports that conftest even though it filters every test
in it back out, and `testcontainers` drags in `docker`.

Verified working as of Group E (title repository, first integration tests) —
`tests/integration/` runs against a real PostgreSQL, started and torn down
per test run by `testcontainers` (`pgvector/pgvector:pg17`; first run pulls
the image, ~625 MB). Docker must be running; nothing else to set up. Its
schema comes from running the real Alembic migration once per test session
(`postgres_url`, `tests/integration/conftest.py`), not `Base.metadata.
create_all` — CHECK constraint bodies and the three `set_updated_at`
triggers are invisible to `create_all` the same way they're invisible to
`--autogenerate` (above), so a suite that never runs the migration can't
catch either drifting from the models. Each test still gets a fully
isolated database via a connection-bound transaction rolled back
afterward, not a schema recreate — cheaper than the 23-tests-worth of
`create_all`/`drop_all` cycles that used to cost, and `tests/integration/
test_migrations.py` is the ongoing regression check (trigger existence,
plus an autogenerate diff against the migrated database asserting no
drift):

```bash
uv run pytest                        # full suite — 235 tests, needs Docker for the 44 under tests/integration/
uv run pytest tests/unit             # 191 tests, no Docker
uv run pytest tests/integration      # 44 tests, needs Docker
uv run pytest -m "not integration"   # marker equivalent of tests/unit
uv run pytest -m integration         # marker equivalent of tests/integration
```

Two ways to select the same split — pick whichever fits: directory (what
Task 10 itself was written and verified against) or the `integration`
marker (registered in `pyproject.toml`, auto-applied to everything under
`tests/integration/` by that directory's `conftest.py`). Both are kept in
sync deliberately, so Group G's CI can use either without the two
diverging. Not wired into `addopts` as a default `-m "not integration"` —
that would make `pytest tests/integration/...` silently collect zero tests
instead of running them.

`tests/contract/title_repository_contract.py` holds the behavioural
assertions every `TitleRepository` implementation must satisfy — the same
suite runs against `FakeTitleRepository` (`tests/unit/`, no Docker) and
`PostgresTitleRepository` (`tests/integration/`, real Postgres), so the two
are verified to actually agree instead of merely looking alike. This is the
pattern PRD 08 calls the "contract suite" for `SourceAdapter`; M3 is
expected to reuse it.

Verified working as of Group F (telemetry bootstrap, FastAPI app with health
endpoints, then hardened in a follow-up review pass) — the app is now a
runnable service:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head
uv run uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000
curl http://localhost:8000/health          # liveness  -- {"status":"ok"}, HTTP 200 always
curl http://localhost:8000/health/ready    # readiness -- {"status":"ready","checks":{"database":true,"migrations":true}}, HTTP 200 or 503
```

`/health` and `/health/ready` are deliberately different: liveness must never
depend on Postgres (a database outage is not a reason to kill and restart
the process — restarting doesn't fix Postgres), so only readiness executes
`SELECT 1` (and, only if that succeeds, compares the live `alembic_version`
table against `usher.db.migrations.status.code_head_revision()` — PRD 08:
"the app refuses to serve on a schema mismatch rather than guessing").
Readiness returns HTTP 503 (not 200) when any check fails: no PRD text pins
a status code, but a readiness probe's real consumers — Kubernetes, Docker
`healthcheck`, load balancers — gate on the code and never parse the body.
Verified directly against a real container: stopping Postgres mid-session
leaves `/health` returning `{"status":"ok"}`/200 unchanged while
`/health/ready` switches to `{"status":"degraded","checks":{"database":
false,"migrations":false}}`/503 — same running process, no restart.
Readiness self-heals once Postgres comes back, still without restarting
Usher. Corrupting `alembic_version` on an otherwise-healthy database
produces the same degraded/503 shape with `database: true, migrations:
false` — a live demonstration is in the "readiness reports migration
state" commit.

Every request gets a real server span (`FastAPIInstrumentor`, wired in
`create_app`) with SQLAlchemy queries and outbound httpx calls nested under
it (`SQLAlchemyInstrumentor`/`HTTPXClientInstrumentor`, wired in
`configure_tracing`) — without this, nothing ever called
`tracer.start_as_current_span()` during request handling, so
`inject_trace_context` never fired in the running service, only in tests
that built their own span. `configure_tracing`/`configure_metrics` install a
real `TracerProvider`/`MeterProvider` *unconditionally* (a bare provider
with zero processors still assigns valid ids/records instruments, verified
directly) — only the actual OTLP *export* is conditional on
`settings.telemetry_enabled`. Both are `isinstance`-guarded against being
reconfigured on a second `create_app()` call in the same process (verified
directly: without the guard, 5 calls with telemetry enabled leaked 5
background export threads; with it, flat at the 2 the first call installs).
With no `OTEL_EXPORTER_OTLP_ENDPOINT` set, the default (unset) config still
carries zero *export*-related risk — nothing gRPC-related is ever
constructed. If an endpoint *is* set but nothing is listening there, the
OTel SDK's own retry loop logs a warning rather than raising or hanging the
app — graceful, but not literally silent in that specific case.

Stdlib `logging` (uvicorn's access/error logs, SQLAlchemy warnings, the OTel
exporter's own retry messages) is bridged into loguru via `_InterceptHandler`
(loguru's own documented recipe) — without it, confirmed on a live run, only
`usher`'s own logger calls were structured JSON; everything else printed as
plain text, ignored `log_level`/`log_json`, and never got
`trace_id`/`span_id` patched in.

`get_session` (`api/deps.py`) is the request's commit/rollback boundary:
commits once the handler completes without raising, rolls back and
re-raises otherwise. Previously nothing in `src/` ever called `commit()` —
`ports/repository.py`'s "the caller owns the session and the transaction"
had no concrete caller yet, so a future write endpoint that forgot to
commit would have lost data silently.

`/health` and `/health/ready` responses are typed (`api/dto/health.py`,
`LivenessResponse`/`ReadinessResponse`/`ReadinessChecks`), so
`/openapi.json` describes real shapes instead of `{"type": "object"}`.

`tests/integration/test_health.py`'s async `client` fixture needs
`asgi_lifespan.LifespanManager` (new dev dependency) wrapping the app:
`httpx.ASGITransport` only implements the ASGI "http" protocol, not
"lifespan" (confirmed against its source and FastAPI's own docs), so a bare
`AsyncClient(transport=ASGITransport(app=app))` never runs `create_app`'s
lifespan and `app.state.session_factory` is never set. Reproduced directly:
without the fix, `/health/ready` raises `AttributeError` while the other two
tests in the file still pass. `deps.py`'s `get_session_factory` now raises a
diagnosable `RuntimeError` for this exact case instead of Starlette's
generic `AttributeError`.

Verified working as of Group G (container image, compose stack, CI) — M1
is now deployable, not just runnable from a dev shell:

```bash
docker build -t usher .                       # multi-stage, ~332MB, non-root
echo "USHER_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build                  # postgres + usher, both healthchecked
curl -sf http://localhost:8100/health         # {"status":"ok"}
curl -sf http://localhost:8100/health/ready   # {"status":"ready","checks":{"database":true,"migrations":true}}
docker compose down                           # data/ bind mounts survive -- not removed by down, -v or not
```

`USHER_COMPOSE_HOST_PORT` (`.env`, defaults to `8100`) is the *host*-side
publish port for `usher`'s container port `8000` — deliberately not a bare
`"8000:8000"`, since this host already publishes an unrelated container's
app on host port 8000. Postgres's own port is never published to the host
at all, only reachable from `usher` over the compose network as
`postgres:5432`, matching PRD 08's deployment shape. It was `USHER_HOST_PORT`
until 2026-08-02, which is the bug below.

The image is genuinely multi-stage: a `builder` stage has `uv` and builds
the venv, a `runtime` stage copies only `.venv/` and `src/` across. No
dependency in `uv.lock` needed a compiler to install (verified: `python:
3.13-slim` has none, and the build never installed one) — every one
resolved to a prebuilt `cp313` wheel. Verified directly against the built
image: runs as `uid=1000(usher)` (`touch /root/nope` → `Permission
denied`), has neither `uv` nor `gcc`/`cc` on `PATH`. `pyproject.toml`
declares `readme = "README.md"`; hatchling (the build backend) reads that
file while building `usher`'s own wheel, so `README.md` has to be `COPY`'d
into the builder stage before the second `uv sync` (the one that installs
the project itself, not just its dependencies) — omitted, that step fails.

**The Postgres healthcheck forces TCP
(`pg_isready -h 127.0.0.1 -U usher -d usher`), not the more obvious
`pg_isready -U usher -d usher`.** `pgvector/pgvector:pg17` runs a
*temporary* bootstrap server during `initdb` on a fresh volume — started
with `listen_addresses=''` (Unix socket only, confirmed against the
running container's own log line: `LOG: listening on Unix socket
"/var/run/postgresql/.s.PGSQL.5432"`, no TCP line) — to run init scripts
before the real server starts. `pg_isready` with no `-h` defaults to the
Unix socket, so an unqualified healthcheck reaches that temporary server.
Verified directly, twice: once with a standalone `docker run` polled every
~0.1s, once against the literal container `docker compose up` creates for
this project (same tight poll, racing the container's own creation from a
background process started before `docker compose up`). Both runs show
the same shape — the Unix-socket form reports "accepting connections"
while the bootstrap server is up, then "rejecting connections" for
roughly a second while it shuts down and the real server starts, then
"accepting" again once the real server is listening (standalone:
accepting at t+1.8s, rejecting t+2.0s–2.9s, accepting again from t+3.0s;
against the compose-managed container: same shape, ~1.1s-wide window). The
TCP-forced form (`-h 127.0.0.1`) never once false-positived in either run:
"no response" solidly until the exact moment the real server started
accepting TCP connections, because the bootstrap server never listens on
TCP at all. `depends_on: condition: service_healthy` gates on the first
successful check, not N consecutive ones, and `start_period` only exempts
early *failures* from counting — it does not delay a false-positive
*success* from being believed — so the Unix-socket form is a real,
reproducible way for `usher` to start against a Postgres that is about to
be torn down and restarted. Docker's own 2s-interval healthcheck did not
happen to land inside the ~1.1s window in the compose runs observed here —
that's host-load luck, not a guarantee, which is why this was verified by
tight-polling the mechanism directly rather than trusting a handful of
`docker compose up` runs to have been unlucky in the right way.

**`usher`'s own healthcheck targets `/health/ready`, not `/health`.**
Plain `docker compose` (no Swarm) never restarts a container because its
healthcheck failed — verified against Docker's documented behaviour, an
unhealthy status only ever changes what `docker compose ps` reports and
what `depends_on: condition: service_healthy` gates on; `restart:
unless-stopped` triggers on the container's *process* exiting, a
condition a failing healthcheck alone does not cause. With no restart-loop
risk in this deployment shape, `/health/ready` (database + migration
state) is strictly more informative for what a compose healthcheck
actually gates than `/health` (always 200, checks nothing) would be.
Compose has no separate liveness/readiness probe pair the way Kubernetes
does, so one healthcheck necessarily conflates the two; readiness is the
more useful of the two to conflate it into. No `curl`/`wget` in
`python:3.13-slim` (and adding either would cut against a small image), so
both the `usher` healthcheck and the CI verification below use Python's
own `urllib.request` — `urlopen` already raises on any non-2xx status or
connection failure, which is already a nonzero exit, so no explicit
try/except is needed for a check where any exception already means
"unhealthy".

`Settings.host`/`Settings.port` validated but were previously read by
nothing — the only way to start the server was the `uvicorn` CLI with
hardcoded `--host 0.0.0.0 --port 8000`. `src/usher/__main__.py`
(`python -m usher`, what the container's `CMD` now runs after `alembic
upgrade head`) fixes this: `uvicorn.run("usher.api.app:create_app",
factory=True, host=settings.host, port=settings.port)`, the same code
path the CLI form uses internally. Local dev is unaffected — `uv run
uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000`
still works exactly as documented above.

Migrations run on container start (`alembic upgrade head && exec python -m
usher`, `exec` so `docker stop`'s SIGTERM reaches uvicorn directly instead
of being swallowed by the wrapping shell) — verified end to end against a
clean volume: `docker exec ... psql -c '\dt'` shows all five core tables
(`titles`, `sources`, `media_items`, `users`, `watch_states`) plus
`alembic_version` at `a8a0e10ff464`, and `SELECT tgname FROM pg_trigger
WHERE NOT tgisinternal` shows all three `set_updated_at` triggers — the
migration ran for real, not `create_all`. **This has no distributed lock**
— fine at M1's one-replica scale, a real problem the moment `usher` is
ever scaled past one replica, at which point migrations belong in a
separate one-shot step instead of every replica's own startup;
`/health/ready`'s migration-mismatch check would surface a lost race as a
503 rather than prevent it. Noted in the Dockerfile's own `CMD` comment,
not solved — nothing in M1 runs more than one replica.

Test count grew from 235 to 237 (`src/usher/__main__.py`'s two new unit
tests). Full suite with coverage, exactly as CI runs it: `uv run pytest
--cov=usher --cov-report=term-missing` → 237 passed, 98% coverage.

CI (`.github/workflows/ci.yml`) pins `actions/checkout@v7` and
`astral-sh/setup-uv@v9` — the plan's `@v4`/`@v5` were several majors
stale by the time this ran (checked against each action's own GitHub
releases). A new `.python-version` file (`3.13`) at the repo root exists
because of a real gap found by running the install step, not by
inspection: `pyproject.toml`'s `requires-python = ">=3.13"` has no upper
bound, and a bare `uv sync --frozen` on a machine with no Python
preinstalled (verified on a stock `ubuntu:24.04` container with a
freshly-installed `uv`, standing in for a fresh runner) resolved **Python
3.14.6** — newer than the 3.13.14 every group has actually developed and
had mypy strict/pytest/ruff verified against. With `.python-version`
present, the identical command resolves `3.13.14` instead. `act` is not
installed on this host and was not added to check this workflow (a
GitHub-Actions emulator whose own correctness is itself unverified doesn't
add much confidence over not having it) — instead, every `run:` step's
literal command was run locally exactly as written, in order, and all
passed: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format
--check .`, `uv run mypy` (`Success: no issues found in 67 source files`
— the mypy-override contingency for `usher.db.migrations.*` was never
needed), `uv run lint-imports` (4 contracts kept), `uv run pytest --cov=
usher --cov-report=term-missing`. Not reproduced byte-for-byte: the
`setup-uv` action's own code (its net effect — a working `uv` on `PATH`
that obeys `.python-version` — was verified by installing `uv` the same
way, astral's own install script, on a bare `ubuntu:24.04` container,
which is a reasonable proxy for a fresh runner but not the literal
`ubuntu-latest` GitHub-hosted image), and Docker-in-CI for
`tests/integration/`'s testcontainers (GitHub's own docs state
`ubuntu-latest` ships Docker running by default, and this project's `uv
run pytest` already depends on exactly that locally, but no run happened
on an actual GitHub-hosted runner).

Verified working as of M2's final group (end-to-end integration, the index
measurement, and documentation) — the bulk-dataset bootstrap pipeline is
runnable for real, not just under test:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run python -m usher bootstrap --phase all       # import IMDb + TMDb ids + crosswalk
uv run python -m usher bootstrap --phase imdb      # one phase at a time
uv run python -m usher bootstrap-status            # progress and catalog size
uv run python scripts/measure_bulk_load.py         # NOT a test -- downloads the real dump
```

Verified directly against a scratch `pgvector/pgvector:pg17`, 2026-07-30,
downloading the real IMDb/TMDb dumps and querying live Wikidata — nothing
mocked. `bootstrap --phase imdb` killed mid-run at 700,000/1,271,138 titles
committed; re-run logged `resuming imdb.title.basics from position 6033908
(700000 rows already seen)` and finished at the identical 1,271,138 titles
an uninterrupted run reaches. A full `bootstrap --phase all` then ran end to
end: 1,271,138 titles (899,828 movies / 371,310 series), 538,937 with a
community rating, 291,737 linked to a `tmdb_id` (236,712 movies / 55,025
series, zero `(tmdb_id, kind)` duplicates — ADR-0011 holds under real data),
50,793 linked to a `tvdb_id`. Two known titles spot-checked correct end to
end: `tt0111161` (The Shawshank Redemption) landed with `tmdb_id=278`,
`community_rating=9.3`; `tt0944947` (Game of Thrones) landed with
`tmdb_id=1399`, `tvdb_id=121361`, `community_rating=9.2`. `bootstrap-status`'s
final report:

```text
titles in catalog: 1271138
wikidata.crosswalk       completed  position=30 seen=386364 written=385805
tmdb.ids.series          completed  position=228100 seen=228100 written=228100
tmdb.ids.movie           completed  position=1226544 seen=1226544 written=1226544
imdb.title.ratings       completed  position=1700616 seen=1700615 written=538937
imdb.title.basics        completed  position=12678891 seen=1271138 written=1271138
```

**Gotcha found running this: `kill -9 "$(cat pidfile)"` on a backgrounded
`uv run <command> &` does not stop the work.** `uv run` forks a child
process (the real interpreter) rather than exec-replacing itself — verified
directly with `ps --forest`, which showed two live PIDs, the `uv` wrapper
and its `python3` child. Killing only the wrapper PID left the child
running, orphaned, still committing to the database — the first kill/resume
attempt against this exact pipeline was contaminated by exactly this before
it was caught (a `bootstrap-status` read raced an orphaned child still
writing). A real deployment is unaffected: systemd's `KillMode=control-group`,
Docker's container-wide signal delivery, and an interactive terminal's
Ctrl-C all reach the whole process group, not just one PID in it. A
hand-rolled `nohup ... & echo $!` script does not — kill the child
(`pgrep -P "$wrapper_pid"`) or the whole process group, never just the
captured `$!`.

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
uv run lint-imports                              # 6 kept, 0 broken

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

**`--kind search` sends `primary_release_year` whatever it is given**, so it
records the `/search/movie` shape and never `/search/tv`'s. Fine for a shape
diff (the two pages are the same shape but for `title`/`name` and
`release_date`/`first_air_date`), worth knowing before reading its output as
evidence about TV search.

Verified working as of M4 group F1 (the CLI, telemetry, and the end-to-end
measurement) — the ingest pipeline is runnable by an operator, not just by a
test:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head
uv run usher --help                          # every command and flag
uv run usher sync --source "Living Room Emby"   # items, then watch state
uv run usher sync --kind delta                  # every enabled source
uv run usher sync --allow-full-retraction       # ADR-0015's ceiling off
uv run usher sync-status                        # runs, queue depth, parked
uv run usher unmatched --limit 50               # the review queue
uv run usher unmatched --resolve <media_item_id> --title <title_id>
uv run usher work --once                        # one pass over the queue
uv run usher work                               # a worker daemon

# NOT tests -- they write to a real database. See each module's docstring.
uv run python scripts/measure_ingest.py --items 50000
uv run python scripts/measure_ingest.py --scale 1126674
```

**A live end-to-end run needs a real catalog, and building one costs three
minutes and no API key.** `bootstrap --phase all` pulls IMDb's
`title.basics`/`title.ratings` dumps, TMDb's *public daily id export files*
(not the API — no key), and Wikidata's public SPARQL endpoint. Re-run
2026-07-31 against a scratch `pgvector/pgvector:pg17`: **1,271,314 titles,
291,772 with a `tmdb_id`, 539,006 with a community rating**, in 2 min 59 s
wall clock end to end. That is the catalog M4's match ladder has to be
measured against — an empty one sends everything to tier 5 and measures
nothing.

**`usher` is a console script (`[project.scripts]`, added 2026-08-01) and
`python -m usher` is the same code path.** Both land on `usher.cli.main`.
The container's `CMD` stays `alembic upgrade head && exec python -m usher` —
the module form is the one whose `exec`/SIGTERM behaviour was verified
against a running container, and there is nothing to gain by re-verifying an
equivalent spelling. Verified from a clean `uv sync`.

**A console script calls `main()` with *no arguments*, which is why `main`
reads `sys.argv` itself.** Before that it treated `argv is None` as "no
arguments at all" and substituted `["serve"]`, so `usher sync-status` would
have silently started the HTTP server — an entry point that ignores
everything it is given and looks like it works, because the server does
start. `argv or ["serve"]` still applies once `sys.argv[1:]` is empty, which
is the property the container's `CMD` depends on. Both halves pinned in
`tests/unit/test_main.py`.

`--source` is optional: omitted, `sync` walks every *enabled* source, and a
source whose credential row has gone missing is skipped with a message
rather than taking the other two down. `--kind` offers `full` and `delta`
only — `watch_state` is a real `SyncRunKind` and is a lane `sync` always
runs *after* the item walk (it resolves each state against a `MediaItem`),
never an alternative to it. `--resolve` and `--title` are used together, and
`parse_args` refuses one without the other: `attach_title` writes what it is
given, so `--resolve` alone would blank a link instead of creating one.

`usher.db.users.ensure_default_user` creates the row nothing ever had.
`usher.domain.watch.User` documents a singleton `is_default` user as what
stands in PRD 01's authentication seam and `watch_states.user_id` is a real
foreign key, so the watch lane and the `watch_history` handler were both
unrunnable without it. Deliberately not a repository port — no *service*
needs it (`WatchStateSyncService` takes a `user_id` per call), and an ABC
plus a fake plus a contract suite for one `SELECT` is a port with nothing on
the other side.

**It was reachable only from `usher.cli`, so a server-only deployment had an
empty `users` table** — `docker compose up` against a healthy Postgres left
`watch_states.user_id` with nothing to reference, and the row appeared only
once `work --once` ran. Not a live bug in M4 (no route writes a watch state;
the three admin routes are M9's) and a live bug the moment M5 adds one.
Fixed as `usher.api.deps.get_default_user_id`/`DefaultUserIdDep`, a
**request-scoped dependency and deliberately not a lifespan call**:
`create_app`'s lifespan builds an engine and opens no connection, which is
what makes `/health` answer 200 with Postgres down while `/health/ready`
reports 503 — verified live against a real container. A write at startup
turns a database outage into a crash loop and an unmigrated schema into a
failure to boot, trading a documented, tested degradation for a worse one,
for a row only a request ever needs. It also would have broken
`tests/unit/test_api_health.py` and `test_telemetry.py`, which build a real
app against no Postgres at all. Nothing routes over it yet, for the same
reason nothing routes over the pipeline services beside it;
`tests/integration/test_pipeline_deps.py` drives it through a real request
and asserts the row is *committed*, read back on a second session.

`api/deps.py` carries all eight new repositories plus `MatchService`/
`IngestService`/`ReconcileService`/`WatchStateSyncService`, so M9 adds
routers over finished wiring. **`EnrichService` is deliberately absent**:
its provider owns the token bucket that keeps this deployment under TMDb's
~40 rps ceiling, and a request-scoped `TmdbClient` gives every concurrent
request a *fresh* bucket — N in-flight requests get N × 30 rps, a rate
limiter that limits nothing. It belongs on `app.state` at lifespan, and
nothing in PRD 07's surface calls enrichment directly (M5's demand
promotion enqueues a job; `usher work` runs it).

**Every fixture is shape-recorded and value-synthetic, and that is a
licensing constraint, not a style.** A real Emby response embeds
TMDb-sourced metadata, which TMDb's terms forbid redistributing and which
"ship importers, never data" above already forbids committing; it also
identifies a real library and carries real server and user ids. Regenerate
a scrubbed *shape* with the script above and diff that; never paste a
capture in.

**That rule was broken from M1 to M4 and nothing noticed, which is the more
useful half of the finding.** `tests/fixtures/bulk/` held verbatim IMDb
rows — real ids, titles, years, runtimes, genres, and two `title.ratings`
rows *with their vote counts*, the most licence-restricted part of that
dataset — under a `README.md` asserting the rows were "typed by hand" and
therefore only "recognisable identifiers". Hand-typing a real value does
not make it synthetic, and **the false assurance was worse than the data**:
it is what stopped three milestones of readers from checking. The TMDb and
Emby fixtures had invented prose but kept real ids, air dates, runtimes,
season/episode counts and `credit_id` ObjectIds — including, on
`movie.json`, a real IMDb id belonging to a *different film* than the rest
of the record was shaped after. Root cause is benign and worth knowing:
**TMDb's reference pages illustrate their endpoints with real responses**,
so "transcribed from published documentation" was transcribing a real
payload. `scripts/capture_tmdb_fixture.py` was never the problem — it
replaces every leaf with its type name — though its `--id 550` *default*
was, and is now required.

All of it was replaced on 2026-08-01, preserving every shape and format
edge case (`\N`, tab separation, the header row, the movie/series `kind`
split, the no-quoting-mechanism row, Emby's `VideoRange` vocabulary, every
TMDb key and type). The one that needed care: the quoted-title row only
pins the `csv.reader` trap if the invented title **opens and closes** with
`"` — `csv` treats `"` as a quote character only at the start of a field,
so a title with *interior* quotes survives both parsers and tests nothing.
Verified both ways before committing.

**`tests/unit/test_no_third_party_data.py` is the control, because a
convention nothing checks is not one.** Three checks over `src/` and
`tests/` — every IMDb id in a reserved `tt99`/`nm99` band; every id inside
a committed fixture at or above a 90,000,000 floor (two orders of magnitude
above TMDb's own daily-export id space); and a **hashed** regression list of
the identifiers this repository once committed, hashed so the guard is not
itself the last file holding them. `docs/` and `CLAUDE.md` are deliberately
outside those three: neither ships, and naming a real row as the *specimen*
for a measurement is a claim about a dataset rather than a copy of one —
which is why this file still names one and
`src/usher/adapters/bulk/imdb.py` no longer does.

**A fourth check scans the whole repository, `docs/` included, for a
dataset *row* rather than an identifier — and that location-independent one
is what caught the two the other three missed.** `docs/plans/2026-07-30-m2-
bootstrap.md` prescribed the original fixture verbatim, ratings rows and
vote counts included: data, *and* the instruction that recreates it, which
is the worse half and is why "docs are just notes" does not hold for a row.
And `usher.adapters.bulk.tmdb_ids`' module docstring carried two real TMDb
id-export records — in the wheel. Both are corrected. Matching on shape (a
tconst followed by a tab; a JSON object carrying `original_title`/
`original_name`) is what makes scanning prose free of noise: no sentence
looks like that.

Plus two cases that fail if the scans stop scanning — a guard that globs
nothing passes exactly like a guard that passes, the same family as the
`sitecustomize.py` installation proof. **Mutation-verified 11/11:** a real
tconst back in a TSV fixture, a real TMDb id back in a JSON fixture, a real
TVDb id back in an Emby fixture, a real TMDb id back in a `.py` test, a
real dataset row back in a plan document, a real export record back in a
shipped docstring, `_SCANNED_ROOTS` narrowed to `("src",)`, the repo-wide
walk emptied, and each of the three matchers made to match nothing.
`tests/fixtures/README.md` holds the bands and the allocation table.

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

**Live-verification runs must not write a credential, a token, a user id or
a host into the repo.** M3's run was driven from a throwaway script outside
the working tree, reading the operator's own secrets file, redacting every
one of those four values from anything it printed. Its one write to a real
account recorded the item's complete `UserData` first and restored it
exactly afterwards, confirmed by reading it back.
