---
paths:
  - "src/usher/adapters/emby/**"
  - "src/usher/services/push.py"
  - "src/usher/services/ingest.py"
  - "src/usher/services/matching.py"
  - "src/usher/services/reconcile.py"
  - "src/usher/services/watch_sync.py"
  - "src/usher/services/watch_write.py"
  - "scripts/measure_ingest.py"
---

# Emby, the push lane, and the ingest pipeline

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

## Commands

```bash
# The gate, measured on this branch 2026-09-02. `uv sync --extra eval` first:
# without it five `test_eval_*` modules abort at collection and pytest runs nothing.
uv run pytest                    # 5721 passed, 26 skipped (5,747 collected)
uv run pytest tests/unit         # 4442 passed + 4 skipped, no Docker and no network
uv run pytest tests/integration  # 1279 passed + 22 skipped, needs Docker
uv run lint-imports              # 12 kept, 0 broken
uv run mypy src tests            # strict, including tests/
uv run ruff check . && uv run ruff format --check .

# The files that pin this subsystem specifically.
uv run pytest tests/unit/test_adapters_emby_contract.py    # the contract suite
uv run pytest tests/unit/test_services_push.py \
              tests/unit/test_services_watch_write.py
uv run pytest tests/integration/test_services_ingest.py \
              tests/integration/test_services_reconcile.py \
              tests/integration/test_services_push.py \
              tests/integration/test_services_watch_sync.py

# The pipeline, from the CLI.
uv run usher sync --source "Living Room Emby"    # items, then watch state
uv run usher sync --kind delta                   # every enabled source
uv run usher sync-status                         # runs, queue depth, parked
uv run usher unmatched --limit 50 --offset 0     # the review queue (OFFSET; see below)
uv run usher unmatched --resolve <media_item_id> --title <title_id>
uv run usher push --probe                        # connect, report what arrived, exit
uv run usher push --source "Living Room Emby"

# Cost, against a throwaway Postgres. NOT a test; writes to a real database.
uv run python scripts/measure_ingest.py --items 50000
uv run python scripts/measure_ingest.py --scale 1126674   # EXPLAIN only

# Register a source and read its health, against a running app:
curl -sS -X POST http://localhost:8000/admin/sources \
  -H 'content-type: application/json' \
  -d '{"kind":"emby","name":"Living Room Emby","base_url":"https://emby.example","username":"...","password":"..."}'
curl -sS http://localhost:8000/admin/sources/<id>/status
curl -sS 'http://localhost:8000/admin/unmatched?limit=50'   # keyset-paged, ADR-0034

# Diff a live server's *shape* against the committed fixtures. NOT a test, and
# its output is deliberately never committed -- see the module docstring.
export USHER_EMBY_URL=... USHER_EMBY_USER=... USHER_EMBY_PASSWORD=...
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
```

The TMDb equivalents live in `.claude/rules/tmdb-and-enrichment.md`, which is
where `capture_tmdb_fixture.py` and the enrichment tier are written up.

⚠️ **`MAX_PAGES` is a dead-man's switch, not a bound.** Exhausting it raises
`PortDataMalformed` (`adapters/emby/adapter.py:251`), so a reconcile "bounded"
that way records `FAILED` and never reaches the sweep — which is usually the
half of the pipeline a live run exists to exercise. Truncate the async generator
instead. **Learned the expensive way in M4's own run**: a probe that walked
`adapter.watch_state()` *looking for one known item id* is a walk of 1,126,789
items to reach something a filtered listing already had, and it issued several
hundred requests against a shared server before it was killed.

## How the pipeline is wired

`ReconcileService` walks the source → `IngestService` writes `media_items` →
`MatchService` runs the ladder → `WatchStateSyncService` merges watch state.
Beside it, `EmbyPushChannel` → `PushSupervisor` → `PushApplyService` handle the
websocket, and `WatchWriteService` handles the client's own writes back out.

### `WatchWriteService` — the four verbs, in the order that is the contract

PRD 07's four actions (`PUT /watch/titles/{id}`, `PUT /watch/episodes/{id}`,
`POST`/`DELETE /watch/titles/{id}/played`) all land on
`services/watch_write.py`, and the order is load-bearing rather than incidental:

1. **Write locally**, `origin = api`. `WatchStateRepository.set_from_client`
   writes it, and **that is the correctness property this service exists to
   extend** — it stops the next sync mistaking Usher's own write for the
   source's truth and round-tripping a position the household never set.
   Nothing here may write watch state by any other route. It wins over a walk in
   flight by construction: `trg_watch_states_set_updated_at` stamps the write
   instant, later than the `observed_at` of a walk that started before it.
2. **Commit**, before anything is offered to a client
   ([ADR-0033](../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)).
   Publishing first is reachable *here* in a way it is not in the push lane: a
   subscriber told a position landed refetches through a **second** connection,
   which cannot see an uncommitted row.
3. **Invalidate and publish**, two calls and not one, guarded on the row having
   actually changed (`_changed`) — because a write that changed nothing is a
   full recompose per second of playback.
4. **Enqueue the write-back**, one `JobKind.WATCH_WRITEBACK` job per source
   *copy*.

- 🔴 **The request never touches a source, and that is structural rather than
  defensive.** PRD 03 calls the write-back *"best effort"*, which describes the
  caller and not the port: `push_watch_state` **must raise** by contract, so *"a
  client's write never blocks or fails on a down source"* is only a property of
  this code if the call is **absent** rather than caught. Nothing in
  `watch_write.py` or `api/routers/watch.py` names or imports
  `usher.ports.source`, **asserted on the imports of both** in
  `tests/unit/test_api_watch.py` — because *"it did not raise"* is also what a
  service that swallowed everything produces.
- **`_copies` uses two different reads and the difference is measured.**
  `list_for_title` carries `AND episode_id IS NULL`; `list_for_episode` is
  precisely the rows that clause excludes. A title write served by the unbounded
  read would enqueue one job per episode *file* — 20,000 for one press on a long
  serial, measured at **20,001 rows / 22.901 ms against 1 row / 0.251 ms**.
- **Retracted copies are told too.** `list_for_title` returns them with
  `available = false` (PRD 02's soft-delete availability); the common cause is a
  temporarily unmounted drive, and `watch_writeback_handler` completes rather
  than parks for an item a source no longer has. Including one costs a job that
  completes; excluding it costs a write that never arrives.
- **The enqueue is deliberately *not* under the changed-row guard.** That guard
  compares Usher's own row before and after and says nothing about the source,
  which may be out of step because an earlier write-back was parked or lost.
- ⚠️ **`dict.fromkeys` in `_enqueue_write_back` is a measured equivalent mutant,
  not a correctness step** — deleting it survives all 47 cases in three files,
  because both arms of `JobQueue.enqueue` deduplicate on `(kind, key)` already.
  Recorded so the next reader does not take it for one.
- **Step 4 rides the request's own commit**, which is the one honest cost here: a
  crash in that window leaves the local row committed and the source untold, and
  the nightly walk will not repair it because *"latest `updated_at` wins"*
  correctly keeps the newer local row. Enqueueing before the commit buys the
  window back and costs the order the four verbs are named for. Nothing here is
  an outbox — a different question, which M9's group G explicitly refused.

## M9's live verification — it ran 2026-08-12, and the reason it had not is the first finding

**Both halves passed against the same real Emby 4.9.5.0, and H5 is the first time
this project has written to the operator's account *through the HTTP surface* and
restored it byte-for-byte.**

🔴 **The premise that stopped them was false, and it was false in eight places
across seven files.** M9 recorded H4/H5 as *did not run* on the ground that *"no
Emby credentials exist on this host — verified rather than assumed"*. What was
verified was `~/code/usher/.env`, and nothing else. The operator's Emby base URL,
access token, user id and device id are in a Home Assistant secrets file one
directory over — precisely where `CLAUDE.md`'s live-verification rule says such a
run reads them from. **A negative established by checking the one place the
answer was expected is not a negative**, and it cost a milestone its two most
valuable runs. The milestone's own reconciliation task counted **five** of the
eight sites.

**Bounded deliberately: 23 requests to the operator's server in total**, and **no
walk of any kind** — three reachability probes, one filtered listing, one
single-item confirmation, one `get_item` for the ingest, two for H4, fourteen for
H5's writes/read-backs/restore, and one post-teardown read confirming the account
is still restored. The item was chosen by a **filtered** listing
(`IncludeItemTypes=Movie&Filters=IsUnplayed&Limit=25`); the ingest's bound is in
the **iterator**, a `list_items` replaced by a closed one-element list feeding the
shipped `get_item` → `to_source_item` → `IngestService` path.

### H4 — the read half, and the two findings that are Emby's rather than the ticket's

➡️ **The ticket findings — `quote(ticket, safe="=")` as a no-op at the real
length, the `deep_link` double-encoding refutation, the 127 s/312 s expiry, the
tamper `404`, and ADR-0029 observed — are in
`.claude/rules/api-telemetry-and-lanes.md`**, whose trigger owns
`services/playback_ticket.py`. What belongs here is what the Emby adapter did:

- **The chain ends in bytes.** `GET /stream/{ticket}` answered `302` with
  `Cache-Control: no-store` and a `Location` byte-for-byte equal to the URL
  `build_stream_targets` builds; a `Range: bytes=0-65535` against it answered
  **`206`, `Content-Range: bytes 0-65535/729664590`,
  `Content-Type: video/x-matroska`, 65,536 bytes whose first four are the
  Matroska magic `1A 45 DF A3`.** M3 measured the URL *as built* answering 206
  (ADR-0012); what this adds is that the **ticket path does not mangle it**.
- **`MediaSourceId` on this build is `mediasource_<item id>`, a namespace of its
  own rather than the item id.** `build_stream_targets` spells it
  `media_source.get("Id") or external_id`, so the `or` arm is **not** what runs
  here, and a URL built from the item id alone would be a different URL.
- **The absence claim, and its control fired first.** `token_appears_in` was
  pointed at the `302`'s `Location`, where the token **must** be — `True` — and
  only then at the `/play` response body, where it found nothing: no `api_key`,
  no token, no source host. A run whose control found nothing was pre-registered
  as `DID-NOT-RUN` yielding no absence claim.

### H5 — the write half, and it wrote to a real account

M4's method exactly, because it is the only one that makes restoration exact. The
item's **complete** `UserData` was read from the **item** route — never a
listing, which M3 measured as dishonest about `PlayCount` — and the run refused to
write unless it was already
`{PlaybackPositionTicks: 0, PlayCount: 0, IsFavorite: false, Played: false}` with
no `LastPlayedDate`.

- **The control was run before the write and seen to be red.** *"Emby's item
  differs from the recorded prior object"* answered **`False`** against a write
  that had not run — which is exactly the state M3 shipped forty passing contract
  assertions against. Only then was the same comparison believed after each write.
- **The position is exact and Emby does not round it.** `position_seconds=613`,
  one real `usher work --once`, and Emby's own item read reports
  **`PlaybackPositionTicks: 6130000000`** with `Played: false` and `PlayCount: 0`
  untouched.
- **`POST /watch/titles/{id}/played` → `PlayCount: 1`, `Played: true`, a real
  `LastPlayedDate`, and the resume position cleared to `0`.** PRD 03's *"position
  first, played last"* as a consequence rather than a preference.
- **The second press is a complete no-op, not merely a non-increment.** A second
  press leaves the **whole** `UserData` object byte-identical — `LastPlayedDate`
  is not re-stamped either — so a retried write-back cannot move a household's
  play history forward in time.
- **The unplayed path goes through `UserData`, not `DELETE /PlayedItems`, and the
  observation is what proves it.** After `DELETE /watch/titles/{id}/played` and
  one worker pass, Emby reports `Played: false` while **`PlayCount` is still 1
  and `LastPlayedDate` survives** — all three of which `DELETE /PlayedItems`
  would have reset. The local 613 s position rode along in the same body, which
  is the other half of M3's finding: the body names `Played` even when `Played`
  is not the field being changed.
- **Restored byte-for-byte.** The before/after diff is `{}`. Choosing the all-zero
  item is what made that exact; on any other item `PlayCount` is not restorable
  by any route this project knows.
- **`PlayedPercentage` is on the item route when a position is set** (19.73% at
  613 s of a 3,107 s runtime) and gone when it is cleared — M5's observation
  confirmed a second time, on a second item. Nothing reads it; recorded because
  it is the one key the fixture was missing.
- 🔴 **Emby's read-back does not lag, which refutes the risk H5's own spec
  names.** That spec warns *"Emby's own indexing is asynchronous; a read-back
  immediately after a 204 may lag"* and asks for bounded polling. Measured: the
  change was visible on the **first** read every time, at **0.141 / 0.142 /
  0.143 s** after the worker pass returned. Zero polls consumed. `UserData` is
  not the asynchronous half of this server.
- ⚠️ **Usher's `last_played_at` after a local `/played` press is Usher's own
  write instant, not the one Emby stamped** (`…19:55:40.845654Z` locally against
  Emby's `…19:55:40.0000000Z`). Nothing reconciles the two until a
  `watch_history` backfill reads the item back.
- **One `usher work --once` per press was enough**, each pass claiming exactly
  `1 jobs`: the write-back is enqueued at `VISIBLE` and coalesced on
  `(kind, key)`. A real CLI subprocess against the same database, never a
  hand-called handler.

### How the run was driven, because two parts of it are traps

- **The operator's secrets file holds an access token and a user id, not a
  password**, so `POST /Users/AuthenticateByName` cannot be exercised and
  `EmbySession._authenticate_locked` was swapped for one that installs the known
  token — M3, M4 and M5 all did exactly this. **The swap lives in a
  `sitecustomize.py` on `PYTHONPATH`, not in an in-process monkeypatch**, because
  H5's worker pass has to be a real `usher work --once` **subprocess** and a
  patched parent cannot reach it. It writes a marker file the caller asserts on:
  a plant that did not land looks exactly like a check that passed.
- **`PortRateLimited.retry_after` was not provoked, and the premise it was
  dispatched under is stale.** Emby answered no `429` in any of the 23 requests
  and `run_after` is `NULL` on the only queued row. The dispatch's premise —
  *"constructed at six sites and read nowhere outside its own `__init__`"* — was
  measured before D9 landed; at the milestone head it is constructed at **six**
  sites and **read exactly once in `src/`**, in `JobWorker._fail`, which is D9's
  whole product. What remains true is that **no real upstream this project talks
  to has ever produced the header that feeds it.**

### Starting the shipped app against a real source used to be an unbounded walk

✅ **Closed 2026-08-19 (issue #9) for the item lane.** `LaneSupervisor` starts a
push lane per enabled source, and its reconnect gap-closer calls
`reconcile(source, DELTA, adapter)`. **The half that made it a full walk is
`cursor_for`, not the gap-closer**: a DELTA resumes from the newest *completed*
item-lane run, so with none there is no `since` and `list_items(since=None)` reads
the whole library — issued by `uvicorn` with default settings and no command of
its own. `USHER_PUSH_GAP_CLOSE` now defaults to `cursored`, so the lane closes a
gap only when a completed walk gives it a `since`, and logs a `WARNING` naming the
source otherwise. **The bound is a refusal rather than a cap on purpose**: a
truncated walk records `COMPLETED`, `latest_completed_cursor` then reads its
`started_at`, and everything the truncation never reached is skipped by every
later delta, silently and permanently.

⚠️ **That closure is narrower than it reads, and the gap is a second lane — found
2026-08-25 by issue #41.** `_close_gap` runs the item lane *and then*
`watch.sync(...)`, while `USHER_PUSH_GAP_CLOSE`'s guard is
`cursor_for(source, DELTA)` — **the item lane's cursor, gating both**. The two
cursors are independent by design (`MinDateLastSaved` against
`MinDateLastSavedForUser`, and `latest_completed_cursor` is scoped by kind), and
the measured deployment was in exactly the state that makes this bite: **13
completed delta runs against zero completed `watch_state` runs**. Such a source
passes the guard, closes a real delta gap in seconds, and then walks the whole
library on the *watch* half — ~1.14M items, ~5,688 pages, about **eleven hours**.
Stated as the reachable path rather than an observed run. **Neither log line would
name it**: the `WARNING` says *"no **item** sync has ever completed"* and the
`INFO` says *"a delta walk of everything changed since {since}"*, both true of the
half that is cheap. ADR-0042 makes that walk *resumable* — it checkpoints
`StartIndex` on `sync_runs.position` — so it converges instead of restarting,
which it never once did before (the deployment carried three `RUNNING` rows aged
7–11 h). It is **not** shorter and it is still unasked.

➡️ **A live run whose request budget has to be *statable* therefore needs
`USHER_PUSH_ENABLED=false` (and `USHER_WORKER_ENABLED=false`) or
`USHER_PUSH_GAP_CLOSE=never`, both of which return before either walk.**
`LaneSupervisor` itself lives in `api/lanes.py`; its supervision and readiness
mechanics are in `.claude/rules/api-telemetry-and-lanes.md`.

## The push lane

**Emby push works.** Verified 2026-07-29 against the live server with a normal
non-admin token: `/embywebsocket` upgrades (101), delivers periodic `Sessions`,
and pushes `UserDataChanged` within seconds of an out-of-band state change. Two
earlier negative findings were both wrong —
[ADR-0004](../../docs/prd/decisions/0004-push-over-polling.md).

⚠️ **Health-check caveat, and re-measured 2026-08-02 it is worse than first
recorded**: a handshake against *any* path succeeds, and a socket carrying **no
credential at all** upgrades, accepts the subscription, and then delivers
`Sessions` *more* often than an authenticated one. So neither an upgrade nor
arriving messages establish that a channel is the one you think it is —
[ADR-0018](../../docs/prd/decisions/0018-push-health-is-a-message-ledger.md).
Assert on received messages, and on the *right* messages.

**A supervisor that resets its failure counter on connection is caught only if
the fake it is tested against has an *unbounded* supply of connections.**
`PushSupervisor` resets on delivery, and the mutation that moves the reset to the
connection is exactly the failure ADR-0004's caveat predicts: a proxy that
upgrades and buffers connects perfectly every time, so the ceiling is never
reached and PRD 08's *"after N failures mark `supports_push = false`"* silently
never fires. A scripted adapter whose list of connections *runs out* terminates
that mutated loop for the wrong reason and lets it pass. The fake in
`tests/unit/test_services_push.py` therefore hands out empty connections forever
and caps its own attempts with a plain `AssertionError` — never a
`UsherPortError`, so the supervisor cannot catch it — and the mutation fails **4
cases in 0.43 s** with *"the supervisor opened 41 channels; it is not counting
failures"*. **Without that cap it would hang**: `asyncio.wait_for` cannot bound a
loop that never yields, so the injected sleep also `await asyncio.sleep(0)`s.

**Three obvious assertions about `SourceNotSupported` all survive its own
mutation.** Deleting the supervisor's `except SourceNotSupported` arm and letting
it fall through to `except UsherPortError` ends with `push_available == [False]`,
`push_connections == 0` and `gaps == 0` — the ceiling is reached instead of the
method returning, so every visible end state is identical and only the five
wasted attempts and four backoff sleeps differ. The M5 plan's own draft asserted
exactly those three. **Assert on `attempts == 1` and `sleeps == []`.**

**"Connect, then close the gap" is a concurrency claim and an ordering assertion
does not test it.** `order == ["connected", "gap"]` is what a serialised run
produces too, and it passes against an implementation that connects, closes the
socket, and then walks. The case with teeth forces a real 40 ms gap walk against
a producer emitting on the open socket for ~30 ms and asserts on measured
intersection-over-union of the two windows — **62.6% on this host, stable over
five runs** (compare `JobQueueContract`'s 76.2% and M5 group B1's 80.3–85.4%) —
plus *"every event produced during the walk was still delivered"*.

**`PushHealth.record_reconnect` was a method nothing in `src/` ever called**, so
PRD 10's `usher.source.push.reconnects` would have plotted a flat zero for every
source forever. The increment belongs in `record_open`, guarded on
`opened_at is not None` — on the second and later *open*, not on a failure,
because a lane that failed to connect five times and then succeeded reconnected
**once**. Both the unguarded version (every source starts at 1) and the absent
version are pinned.

**A push merge's `observed_at` mutation survives the whole unit file and is killed
only by real Postgres.** Measured 2026-08-01: replacing `PushApplyService`'s
`datetime.now(UTC)` with a plausible earlier instant — the event's own timestamp,
the last walk's `started_at` — passes all of `tests/unit/test_services_push.py`
and fails `tests/integration/test_services_push.py`, because
`FakeWatchStateRepository` stores `observed_at` as `updated_at` while
`trg_watch_states_set_updated_at` owns that column in Postgres. **That is the
reason that integration file exists at all.**

**The M5 plan's own self-review found a real bug and it is worth the general
form.** `_publish_watch_states` zipped the *matched subset* of targets against
the whole batch of states, so one unmatched item — which PRD 02 guarantees there
will always be — shifted every pair by one and published item A's resume position
under item B's title id. **Recovering a pairing outside the loop that built it is
the failure**; `WatchStateSyncService` therefore returns
`MergedState(external_id, target)` and the pairing cannot be reconstructed
wrongly. Same rule `SourceEvent.watch_states` states one layer up: **keyed, never
aligned by position.**

### M5's live run — the first real `/embywebsocket` message this repository has parsed

Run 2026-08-02 against the same live Emby **4.9.5.0**, driving the shipped
`EmbyAdapter` → `EmbyPushChannel` → `connect_websocket` → real `websockets`, and
for the long hold the shipped `PushSupervisor` with recording callables in place
of the three unit-of-work ones. **Bounded deliberately: one long-lived socket held
100 minutes, eight short-lived probe sockets, and 14 HTTP requests in total** — no
walk of any kind, because the library is 1,126,789 items. The long socket received
**200 frames — 183 `Sessions`, 12 `LibraryChanged`, 5 `UserDataChanged` — with
zero reconnects, zero unforced failures, and `supports_push` true throughout**,
and the shipped mapper turned them into 20 `SourceEvent`s. **Four of thirteen
documented guesses were wrong.**

- **The envelope is not uniform, and that is the first correction.**
  `UserDataChanged` and `LibraryChanged` carry `{MessageId, MessageType, Data}`
  with a **distinct 32-hex `MessageId` per message** (not per type — 17 carried
  one, 17 distinct); **`Sessions` carries `{MessageType, Data}` and no
  `MessageId` at all**, on 183 of 183 frames.
  `tests/fixtures/emby/push_sessions.json` claimed one and no longer does.
- **A real `UserDataChanged` entry is honest, including about play history.** One
  item, three transitions, each compared against `GET /Users/{u}/Items/{item}` in
  the same second: `PlaybackPositionTicks` 6,130,000,000 with `Played: false`;
  then `PlayCount: 1`, `Played: true`, `LastPlayedDate` — *the same timestamp the
  item route returned*; then all-zero after the restore. **So the pushed shape is
  not the partly-honest one the listing route is**, and the M5-blocking failure
  this run existed to look for — an entry that zeroes the position — **did not
  happen**.
- **`play_count`/`last_played_at` stay `None` anyway, and that is a deliberate
  lag rather than an oversight.** ADR-0014's rule is that a reported number must
  be *true*, and the evidence here is one item across three transitions, all of
  them writes Usher itself made, on an item whose history was zero to begin with.
  The failure it guards against needs an entry reporting `0` for an item whose
  true count is 13, which this run could not produce without touching real
  history. Turning the field on is a measured opportunity worth one
  `watch_history` job per played item; recorded, not taken.
- **`LibraryChanged` arrives, its arrays hold ids, and one carried all six at
  once.** Never observed before this run; **twelve** arrived unprompted, with all
  seven documented keys (`ItemsAdded`/`ItemsUpdated`/`ItemsRemoved`,
  `FoldersAddedTo`/`FoldersRemovedFrom`, `CollectionFolders`, `IsEmpty`) and every
  array a **list of id strings** rather than of item objects. The committed
  fixture's shape was already right, field for field. `to_source_events` produced
  7 `ITEM_ADDED`, 7 `ITEM_UPDATED` and 1 `ITEM_REMOVED` — one event per non-empty
  array, live.
- **`ItemsRemoved` fires on a library from which nothing was removed**, which is
  ADR-0015's argument arriving as a measurement. Nobody deleted anything during
  the 100-minute hold and one frame still named an item in `ItemsRemoved`. M5
  counts it and retracts nothing; had it retracted, one ordinary library refresh
  would have marked a present file unavailable.
- **A real `ItemsUpdated` batch reached 42 ids**, against
  `push_max_items_per_event`'s default of **50**. So the ceiling is not
  theoretical headroom — real traffic on an otherwise idle server comes within
  16% of it, and the batch below it costs 42 `get_item` calls applied inline.
  Raising the default would buy little and the deferral path (a delta walk) is the
  cheaper answer above it; recorded so the number is chosen against data next
  time it is chosen.
- **`Key` and `UnplayedItemCount` are not on a real `UserDataList` entry, and
  `PlayedPercentage` is.** Observed keys: `ItemId`, `PlaybackPositionTicks`,
  `Played`, `PlayCount`, `IsFavorite`, plus `PlayedPercentage` (a float, when the
  position is non-zero) and `LastPlayedDate` (when played). The fixture and
  `FakeEmbyServer` both rendered a `Key`; both stopped.
- ⚠️ **The `Sessions` interval, which `DEFAULT_STALE_AFTER_SECONDS = 90.0` rests
  on: median 38.7 s, mean 32.8 s, p90 46.5 s, max 72.9 s** over 182 intervals in
  100 minutes on an authenticated socket. **The 90 s default survives — but the
  headroom is 1.23x, not the comfortable margin the constant reads like, and it
  shrank monotonically as the window grew**: worst gap 52.6 s at 26 minutes,
  60.1 s at 70, **72.9 s at 96**, with only two of 182 intervals over 60 s. So a
  longer hold would plausibly have crossed 90. **This is a bound that has not been
  falsified rather than one shown to be safe** — one household, one evening.
  **The default is left at 90 anyway**: a bigger constant chosen from a 96-minute
  sample would be just as unprincipled, and it costs detection time for the
  failure the whole milestone exists to catch. The real finding is that the
  constant is wrong *in kind* — there is no application-level heartbeat on this
  channel, so any fixed ceiling is a guess against a change-driven signal, and the
  one genuinely periodic signal available is the WebSocket pong, which ADR-0018
  deliberately refuses to count because **a pong is not delivery**. When it bites,
  it bites bounded and visible: a reconnect, a delta that returns 0 items, and
  `usher.source.push.reconnects` climbing.
- **`"0,1000"` really is `initialDelayMs,intervalMs`, and an authenticated socket
  does not honour it.** An *unauthenticated* socket receives `Sessions` at ~1 Hz —
  53 and 55 frames in 45 s — while the authenticated one on the same server in
  the same minute received **one**. The difference is the payload: the
  unauthenticated stream carries the **whole server's 83 sessions**, the
  authenticated one a 5-session row-filtered view. The reading that fits every
  number: the 1 s timer fires either way and the filtered stream is only *sent*
  when the filtered view changes. **So Usher's liveness signal is change-driven,
  not periodic**, and a genuinely quiet server could exceed any fixed
  `stale_after`.
- 🔴 **`/embywebsocket` does not accept `X-Emby-Token` as a header, and the test
  that looks like it says otherwise is the trap.** A header-only socket upgrades
  and delivers — identically to one with **no credential at all**: 53 frames of 83
  sessions against 55 frames of 83 sessions. It is not authenticated; it is
  anonymous. So the token cannot be moved out of the URL this way and **ADR-0012's
  accepted risk stands unnarrowed**. A check written as *"did it connect and
  receive messages"* passes this. The only discriminator is the row-filtered
  payload, or a `UserDataChanged` that never comes.
- **A dropped socket raises rather than hanging, and Emby re-delivers nothing.**
  Aborting the TCP transport raised `PortUnavailable` out of the iterator in
  **0.0 s** — not a hang, not the quiet end the port forbids — and `connected`
  went false. Over a **61 s** outage a real played toggle and its restore were
  made out of band; the reconnected channel listened for **90 s** and received
  three `Sessions` and **not one** `UserDataChanged`. The control is decisive: a
  *second* socket that stayed up throughout received both changes as they
  happened. **The gap-closing delta is not belt-and-braces, it is the only cover
  there is** — exactly what PRD 03 puts on the reconnect.
- **The `websockets` DEBUG token leak is real, and the fix holds against the real
  library and the real server.** Two runs at `USHER_LOG_LEVEL=DEBUG` with
  `configure_logging` installed exactly as `create_app` does: the shipped path
  produced **804 bytes / 2 lines** with **no token, no `api_key=`, no `> GET`
  request line** and a channel that genuinely delivered
  (`messages_received == 1`); the control — the same URL with the library's own
  logger left alone — produced **16,857 bytes / 24 lines** with the token in it,
  `api_key=` in it, and the request line logged twice. **Both halves, or the run
  proves nothing.**
- **`permessage-deflate` is not negotiated.** `websockets` offers it by default
  and the handshake response carries no `Sec-WebSocket-Extensions` at all, on
  every connection made in this run. So nothing here relies on compression.
- **A client that stops reading loses the connection, which is what
  `max_queue=256` is buying.** With `max_queue=1` and no application read for
  150 s, the socket came back **CLOSED** with a `ConnectionClosedError` and only
  two buffered `Sessions` behind it. **The confound is named rather than
  glossed:** `websockets` services pings on the same reader task that backpressure
  stalls, so this cannot separate a server-side close from the client's own pong
  timeout. Either way the operational conclusion is the one `connect_websocket`
  was written for: **do not let the queue fill during the gap-closing walk.**
- **The nonexistent path still upgrades** — `/embywebsocket-nope` → 101,
  `Upgrade: websocket`, `Sessions` delivered — and **`supports_push` is `False`
  before the first message and `True` after**, measured through the shipped
  adapter against the real server rather than a fake.

## M4's live verification (2026-07-31) — the match ladder measured

**The design's central measurement holds, the matcher's exact-name tier was
expected to match "almost nothing" and matches about three quarters, and the
defect the plan called hypothetical is real in this library.** Driving the real
`EmbyAdapter` and the real `ReconcileService`/`IngestService`/`MatchService`/
`WatchStateSyncService` against a real `pgvector/pgvector:pg17` holding a real M2
bootstrap (1,271,314 titles). Bounded deliberately: **600 items ingested** and ~90
deliberate requests. (Plus several hundred accidental ones from a single runaway
probe, killed — counted here rather than quietly dropped; it is the mistake worth
not repeating, and it is why the `MAX_PAGES` note is in the Commands section.)

- **The finding M4 exists to answer, re-measured through the real adapter.** The
  *listing* reports `PlayCount: 0` and no `LastPlayedDate`; the *single-item*
  route reports `PlayCount: 13` and `LastPlayedDate: 2026-07-30T08:12:53Z`;
  `PlaybackPositionTicks` and `Played` agree. Through `EmbyAdapter`: the walk
  yields `play_count=None, last_played_at=None`, `get_watch_state` yields `13` and
  the real timestamp. Over the first 100 states of a real `adapter.watch_state()`
  walk, both are `None` for **all 100**. ADR-0014's premise is measured, not
  assumed.
- **The milestone's central property, end to end against real payloads.** A row
  holding the authoritative `play_count = 13` was fed the *listing* payload for
  the same item through `to_watch_state(..., play_history_is_trustworthy=False)`
  and `merge_from_source`. It reads back **13**, `played = true`, and the original
  `last_played_at`. **The walk cannot zero real history**, verified against the
  live server rather than against a fake told to behave like it.
- 🔴 **`MatchService`'s exact-name rule matches ~74% of real Emby names, not
  "almost nothing".** Measured against the real 1,271,314-title catalog with the
  *identical* rule `_confident` applies (exact normalised name, year ±1, exactly
  one survivor), over 600 movies and 300 series sampled across six windows
  spanning the whole collection: **72.2% of movies** (433/600) and **75.3% of
  series** (223/296 distinct probes). Of the movie misses, 142 are *absent* from
  the catalog and only 25 are *ambiguous* — so the review queue is a trickle, and
  what feeds it is mostly the catalog not holding the title at all rather than the
  rule being too strict. **This reverses the plan's stated expectation and is the
  single most load-bearing number the live run produced.**

  **What this is and is not.** It is `_confident`'s *predicate* run over the local
  catalog — tier 3 — not `_confident` against TMDb's own search results. The two
  differ in their candidate set, in opposite directions: TMDb returns a handful of
  relevance-ranked results, so "exactly one survivor" is *easier* to satisfy than
  against 1,271,314 rows; but TMDb can also return nothing for a name the local
  skeleton holds. **So treat 72–75% as a measurement of the rule on real names,
  not as a prediction of tier 4's yield.** The TMDb counterpart — 83.1%/87.2% —
  was measured the next day and is in `.claude/rules/tmdb-and-enrichment.md`.
- **On this library the name+year tier out-resolves the `tmdb_id` tier.** 68.5% of
  movie TMDb refs and 68.7% of series TMDb refs resolve, against 72.2%/75.3% for
  name+year — because only 291,772 of 1,271,314 catalog titles carry a `tmdb_id`
  at all. **Tier 3 is not the fallback the ladder's ordering makes it look like.**
- **A probe with no year resolves nothing, by construction, confirmed on real
  data.** `t.year BETWEEN p.year - 1 AND p.year + 1` propagates `NULL`, so the
  same 900 names re-run with the year stripped match **0**. That is the documented
  intent (the alternative matches every undated IMDb skeleton of the same name) —
  recorded here because "0.0%" looks like a bug and is not.
- **A malformed `ProviderIds.Imdb` is real, not hypothetical: 11 of 885 in the
  sample** (1.2%), all bare 6- or 7-digit numbers with no `tt` prefix. Fed to the
  real `MatchService` they resolve cleanly (9 stubs, 2 name+year) and nothing
  raises. **The guard that makes that true is `_as_imdb`, not `_usable_ids`** —
  the two are layered, and removing `_usable_ids`' filtering alone still does not
  raise, because `_create_stub` calls `_as_imdb` again at the constructor.
  Removing `_as_imdb`'s pattern check raises `pydantic_core.ValidationError` on
  these exact real payloads, which is **not** a `UsherPortError`, which is a
  permanently aborted sync. Measured both ways, so **a mutation of `_usable_ids`
  alone is an equivalent mutant.**
- **An episode never walks the ladder, confirmed on real data.** Of 600 live
  items, 578 were episodes and every one returned `UNMATCHED` with no lookups;
  `IngestService` attached them as `SERIES_PARENT`. Zero episodes reached a
  provider tier or the stub tier.
- **Stub-on-sight never fired, and that makes the cold and warm walks identical.**
  All 22 non-episode items resolved to existing catalog titles (21 by `tmdb_id`,
  1 by `imdb_id`), so **zero stubs were created** — and walk 2 over the same 600
  items cost exactly the same **40 statements**, `0.0667` per item, as walk 1.
  That is the *"16,950 of the first walk's 17,722 statements are stub-on-sight"*
  claim arriving from the other direction: with no new titles there is no cold
  penalty at all.
- **A delta walk completes and its cursor advances; a failed walk sweeps
  nothing.** A `DELTA` reconcile inherited the last completed `FULL` run's
  instant, returned 0 items, recorded `COMPLETED`, and advanced
  `sync_runs.cursor_at`. A `FULL` walk interrupted mid-stream recorded `FAILED`
  with its message and left all 601 `available` rows untouched —
  `items_retracted = 0`.
- **The delta filters, re-measured on a fresh 30-day window.**
  `MinDateLastSaved` = 28,955, `MinDateLastSavedForUser` = 29,027, unfiltered =
  1,126,789. Still honoured, still genuinely different, and an *invented*
  parameter name still returns the full unfiltered count — the "degrades to a full
  walk" safety property.
- **`VideoRange`'s vocabulary holds over a second, different slice.** 600 movies
  by `DateCreated` ascending: `SDR` 597, `DolbyVision` 2, `HDR 10` 1, with
  `ExtendedVideoType/SubType` ∈ {`None/None`, `Hdr10/Hdr10`,
  `DolbyVision/DoviProfile50`, `DolbyVision/DoviProfile81`}. `VideoRangeType`,
  `DvProfile` and `DvVersionMajor` are absent from every video stream. The mapper
  produced the right `SourceItem` for all 1,100 sampled payloads with **zero
  failures and zero skips**, and the technical metadata survives into
  `media_items`: 496 `h264` + 85 `hevc`, 581 of 601 rows carrying
  width/container/file size (the 20 without are `Series` rows, which have no
  `MediaSource` — correct), and one row carrying `hdr_format = DV`. `SDR → NULL`
  and `DolbyVision → DV` are confirmed on stored rows; **`HDR 10 → HDR10` appeared
  in the sampled payloads but not in the ingested slice, so that arm is still
  fixture-only end to end.**
- ⚠️ **Emby's `ProviderIds` key space is far wider than three, and case is not
  stable.** Observed on 900 payloads: `Tmdb`, `Imdb`, `Tvdb`, `TvMaze`,
  `Official Website`, `TvRage`, `X (Twitter)`, `Zap2It`, `TV Maze` (with a space,
  alongside `TvMaze` without), `Wikipedia`, `EIDR`, `Wikidata`, `Reddit`,
  `Fan Site`, `IMDB` (14 items — uppercase), `Facebook`, `Instagram`,
  `TmdbCollection`, `Youtube`, `tmdb` (3 items — lowercase), `Twitter`.
  `mapping.provider_ids`' `key.lower()` is what makes `IMDB` and `tmdb` usable at
  all, and an **exact-key `get("tmdb")`** is what keeps `TmdbCollection` from
  being read as a TMDb id — **a prefix match there would attach films to
  collections.** The one residual risk is an item carrying both `Imdb` and `IMDB`
  with different values, where `key.lower()` silently keeps whichever came last;
  none was observed.
- **The backfill's own read path is verified end to end against a real write.**
  `push_watch_state(played=True)` took the chosen all-zero item to `PlayCount: 1`,
  `Played: true`, `LastPlayedDate: 2026-07-31T13:41:53Z`, and `get_watch_state` —
  which is what the history backfill reads — returned `play_count=1` and that
  timestamp. `DELETE /Users/{u}/PlayedItems/{item}` restored the object
  byte-for-byte.
- **The library grew.** 1,126,789 items (94,448 movies / 32,414 series / 999,927
  episodes), against 1,126,674 four days earlier. **Any figure derived from it is
  a snapshot, not a constant.**

## M3's live verification (2026-07-31) — the write-back route was simply wrong

Driving the real `EmbyAdapter`/`EmbySession` with `_authenticate_locked` swapped
for one that installs a known token. Full route-by-route table in the M3 plan's
*"Which Emby routes are guessed"* section.

- 🔴 **`POST /Users/{user}/PlayingItems/{item}/Progress` answers 400** —
  *"Value cannot be null. (Parameter 'key')"* — bodyless, with an empty JSON body,
  with an `{ItemId, PositionTicks}` body, and with `MediaSourceId` and `IsPaused`
  added. So does `POST /Sessions/Playing/Progress`. Both are *session-scoped
  playback reporting*, keyed off a play session Usher never has. **Use `POST
  /Users/{user}/Items/{item}/UserData`** with a JSON body; it answers 204.
  **`FakeEmbyServer` could not have caught this: it implemented the adapter's own
  guess, so 40 contract assertions passed against a write-back that had never
  worked once. This is the whole argument for a live run in one bug.**
- **That `UserData` body must name `Played` even when it is not changing.** It
  deserialises into a DTO whose unset fields take their defaults, so a body
  carrying only `PlaybackPositionTicks` flips a played item to unplayed.
  `PlayCount` and `LastPlayedDate` survive the same omission.
- **`DELETE /Users/{user}/PlayedItems/{item}` is destructive beyond its name:** it
  resets `PlayCount` to 0, clears `LastPlayedDate`, *and* clears a non-zero resume
  position. Never use it to report an item unplayed while writing a position.
  `POST` to the same route *is* how you mark played — it advances `PlayCount` (to
  1, idempotently, not `+1`), stamps `LastPlayedDate`, and clears the resume
  position. That last part is PRD 03's load-bearing *"position first, played
  last"* ordering, verified for the first time.
- **`/Videos/{id}/stream` does not need `DeviceId`.** Measured one parameter at a
  time with a `Range` header: as built → 206 with real bytes; without `DeviceId` →
  still 206; without `api_key` → 401; without `static` → 400. The parameter is no
  longer sent (ADR-0012).
- 🔴 **A listing's `UserData` is not the same as an item's.** A
  `GET /Users/{user}/Items` listing reports `PlayCount: 0` and omits
  `LastPlayedDate` entirely, for the very item whose
  `GET /Users/{user}/Items/{item}` reports `PlayCount: 2` and a real
  `LastPlayedDate`. `PlaybackPositionTicks` and `Played` are correct in both.
  Neither `Fields=UserDataPlayState`, `Fields=UserData`, `EnableUserData=true`,
  nor restricting the listing to explicit `Ids` changes it. So `watch_state()` —
  which walks listings — **cannot carry play history**, and M4 must not write
  `play_count`/`last_played_at` from a walk or it writes 0 over real history.
  Recovering them is one request per item against 1,126,674 items.
- **Emby 4.9.5.0 emits neither `VideoRangeType` nor `DvProfile`.** Not once across
  every video stream of 200 movies, including all 34 Dolby Vision files. What it
  emits is `VideoRange` ∈ {`SDR`, `DolbyVision`, `HDR 10`} — with a space — plus
  `ExtendedVideoType`/`ExtendedVideoSubType`. **The `Extended*` pair carries the
  literal string `"None"`, not JSON null**, so it is always truthy and any check
  on it must be a token lookup that falls through. The `DOVIWith*` family the
  mapper also handles is Jellyfin's vocabulary, not this server's; both are kept,
  since reading a field a server omits costs nothing.
- **Emby honours a secondary sort key, so `SortBy=DateCreated,SortName` is a real
  request.** Shown on a tie-heavy primary key rather than hoped for:
  `ProductionYear,SortName` returns the tied block in `SortName` order,
  `ProductionYear` alone returns it in a different, insertion-shaped one. **Tie
  *instability* was not reproducible here** — repeated pages came back identical
  and overlapping `StartIndex` windows agreed exactly — so the second key is a
  cheap guarantee rather than a demonstrated-necessary fix.
- 🔴 **The library is 1,126,674 items, not 94,395.** 94,438 movies, 32,409 series,
  999,827 episodes. The movie figure the adapter was designed around was one third
  of the walk. At the default page size that is 5,634 pages — **56% of
  `MAX_PAGES`, so the headroom is 1.8x, not the ~21x the constant's comment
  claimed.**
- **A token presented with a different `DeviceId` neither forks nor invalidates
  its session.** `GET /Sessions` was byte-identical before and after, and the
  token still worked. Emby binds a session to the token's own authentication
  record, made at `AuthenticateByName` time. **So "one durable device" comes from
  authenticating once with a stable id, not from repeating it.**
- **`Policy.IsAdministrator` is readable**, on `GET /Users/{userId}`, with the
  user's own non-admin token — a 45-key `Policy` object. (`GET /Users/Me` answers
  500 on this build.) ADR-0012 assumes a non-admin account and nothing enforces
  it; this is the check that would make it observable, recorded there as
  recommended-not-implemented.
- **`multi_version_movie.json` has now been looked for twice, over disjoint
  slices, and still has never met a real payload.** M3 searched the newest 800
  movies; M4 searched 600 spread across six windows of the whole 94,448-movie
  collection. **Every one of the 1,400 movies examined carries exactly one
  `MediaSource`** — the count distribution is `{1: 600}` with nothing else in it.
  So `primary_media_source`'s selection rule remains fixture-only, and this
  deployment looks like a genuinely single-version library. The fixture stays:
  another Emby deployment will have them, and the rule is cheap.

## Rules the pipeline enforces, each learned from a defect

- **An episode must never walk the match ladder, and the reason is in the
  payload.** A live Emby episode carries the *episode's* own provider ids —
  `{"Imdb": "tt2178782", "Tvdb": "4517466"}` — not its series'. Two consequences,
  both catastrophic at 999,827 episodes. TVDb numbers episodes and series in
  different, numerically overlapping namespaces and
  `usher.db.repositories.matching`'s TVDb statement deliberately does not filter
  on kind, so an episode run through the provider tiers resolves to whichever
  unrelated series holds that integer. And no episode's IMDb id is in the catalog
  at all (`tvEpisode` is excluded from M2's bootstrap by design), so the stub tier
  mints one junk `Title` per episode — **a catalog of rubbish roughly the size of
  the real one.** `MatchService` returns `UNMATCHED` with no lookups and **no
  remote-search job** (one per episode is a queue the size of the library, and a
  TMDb title search for an episode name is not a resolution path);
  `IngestService` attaches it to its series' `Title` as `MatchMethod.SERIES_PARENT`.
- **Nothing a source can put in a payload may abort a walk.** `Title.imdb_id` is
  pattern-validated (`^tt\d{7,8}$`) and `year` is `ge=0`, and a pydantic
  `ValidationError` is **not** a `UsherPortError` — so `ReconcileService`, which
  re-raises anything that is not one, would let a single stray
  `ProviderIds.Imdb` in 1,126,674 items abort that source's sync permanently.
  **Filter every value to the shape the model accepts *before* the constructor.**
- **`sorted()` over a set of `ProviderRef`/`NameYearProbe` raises.** Both are
  `@dataclass(frozen=True, slots=True)` without `order=True`, so there is no
  `__lt__` — `TypeError: '<' not supported`. `dict.fromkeys` is the idiom used
  throughout: it deduplicates *and* keeps the batch's own order, which is what
  makes a failure read in the order the page arrived.
- **A service that saves a frozen checkpoint per batch must not evolve its own
  stale copy in the failure handler.** `ReconcileService._flush` saves an evolved
  `SyncRun` after each batch, so when the walk raises, `reconcile`'s binding is
  the pre-walk value — and `run.evolve(status=FAILED)` on it writes
  `items_seen = 0` over a checkpoint that recorded eight. Same trap
  `BootstrapService.import_dataset` documents; here there is no re-fetch to
  recover from, so a small mutable holder carries the latest run across the `try`.
- 🔴 **Moving the availability sweep into a `finally:` really does retract a
  healthy library, and the obvious test shape hides why.** Measured. Seed seven
  items, fail the walk immediately, one batch: nothing is written before the
  failure, so the sweep would retract 7 of 7 — 100%, refused by ADR-0015's
  ceiling, and `AvailabilitySweepRefused` then escapes the `finally:`. The case
  fails, but **on an uncaught exception rather than on its own assertion**, and it
  never exercises a sweep that *succeeds* after a failed walk. The shape that does
  is a walk that commits eight of ten items and then raises: two stale rows, 20%,
  under the ceiling, no refusal, **two available items silently retracted**. **The
  ceiling is not a second line of defence for the success-path gate** — it fires
  on a fraction, so it catches the catastrophe and misses the quiet one.
  Reproduced against real Postgres as well as the fakes.
- **`observed_at=now()` instead of the run's start instant is a *semantic* break,
  not a race.** A per-row write instant is always later than `run.started_at`, so
  the sweep's `last_seen_at < seen_since` still spares everything the run saw and
  no retraction test fails. What breaks is the meaning of the column. **Assert
  `stored.last_seen_at == run.started_at` directly**; no frozen clock is needed.
- **An episode's `MediaItem` carries two ids and its `WatchState` may carry one.**
  `IngestService` writes the series' `title_id` *and* the `episode_id` on an
  episode's row (a client browsing a season wants both); `watch_states` has a
  `num_nonnulls(title_id, episode_id) = 1` CHECK. So `WatchStateSyncService`
  collapses the pair with the episode winning (`watch_sync._watch_target`).
  Passing both through raises `PortDataMalformed` by contract, which aborts a
  batch of five thousand states over 89% of this library; passing the *title*
  through merges every episode of a show onto one row and violates nothing. The
  same asymmetry runs the other way in `MediaItemRepository.resolve_external_ids`,
  whose title branch needs `episode_id IS NULL` or a series' own watch state
  resolves to whichever of its episodes the planner reached first.
- **A history backfill must carry its own fresh `observed_at`, and both test
  layers are blind to why.** PRD 03's *"latest `updated_at` wins"* covers the
  whole record, and `trg_watch_states_set_updated_at` stamps the *write* instant —
  so a backfill carrying the walk's instant is refused by the very row it exists
  to repair, writes nothing, and leaves that row matching
  `played AND play_count = 0` forever. `FakeWatchStateRepository` stores
  `observed_at` as `updated_at`, so it accepts what Postgres refuses; and the
  integration suite cannot reproduce the production form either, because `now()`
  is frozen per transaction and each test *is* one transaction.
  `tests/integration/test_services_watch_sync.py` stages the row with
  `clock_timestamp()` through a raw `INSERT` (the trigger is `BEFORE UPDATE`, so
  an insert is the only way to own the column), which is as close as one
  transaction allows.
- **The bounded backfill terminates, measured.** Seven rows matching
  `played AND play_count = 0`, drained three at a time, empty in exactly three
  passes — against the fakes and against real Postgres, with the loop bounded so a
  non-converging predicate fails the case rather than hanging the suite. **The
  honest half: convergence is a property of the *source*.** A source whose
  single-item route also cannot count leaves rows matching forever, bounded at one
  request per row per pass and rotating rather than starving, because
  `list_needing_history` is oldest-first and a merge moves `updated_at`.
- **Two guards in M4's services are unreachable through their own port's contract,
  and are pinned by direct unit cases rather than deleted.** `_watch_target`'s
  "matched to nothing" branch (`resolve_targets` omits an unmatched item rather
  than answering with an empty pair) and `_links_for`'s `is_valid` check (the OTel
  SDK also drops an invalid `Link` on the way into a span). Both mutations
  survived the whole suite until the direct case existed.
- **Two `IngestService` defects are invisible to every port fake and only real
  Postgres catches them.** Skipping `resolve_seasons` or `resolve_episodes` and
  trusting the freshly-minted UUIDv7 leaves all 24 unit cases green — **a dict has
  no foreign keys** — and fails on `fk_episodes_season_id_seasons` /
  `fk_media_items_episode_id_episodes` on the *second* walk.
  `tests/integration/test_services_ingest.py` and
  `tests/integration/test_services_reconcile.py` are the paired runs; the latter
  also pins *"a refused sweep leaves the session usable for the `FAILED` row that
  explains it"*, which no fake can express (the guard is evaluated in Python after
  a successful `SELECT`, so Postgres never aborts the transaction).

## The pipeline's measured cost (2026-07-31, `pgvector/pgvector:pg17`)

`scripts/measure_ingest.py --items 50000`, 50,000 items in the measured library's
proportions — 88.7% episodes — at batch size 1,000:

| | statements | per item | items/s |
|---|---|---|---|
| first walk, cold catalog | 17,722 | 0.3544 | 1,933 |
| the nightly walk | 1,356 | **0.0271** | 2,135 |

**16,950 of the first walk's 17,722 statements are stub-on-sight**, and that is
the one path in the pipeline that is not set-based: `MatchService._create_stub`
calls `TitleRepository.add` per item, SAVEPOINT-wrapped, so a new title costs
three statements. It is bounded by **new titles** (94,438 movies + 32,409
series), never by items — an episode never walks the ladder, so the other 999,827
cost nothing there — and a second walk creates none. Batch-level cost is 772
statements, 0.0154 per item. **Throughput is against a local database with no
network in the way; a real walk is bounded by Emby's 5,634 pages at 1–5 s each.**

### Four scale risks, planned against the statement the repository actually issued

`scripts/measure_ingest.py --scale 1126674`, captured off `before_cursor_execute`
and **never transcribed** — a hand-copied lookalike drifts and then reads like
coverage, and two earlier tasks here were replaced for exactly that.

- 🔴 **`merge_from_source` at 1,126,674 `watch_states` with a 1,000-row batch:
  refuted.** `Nested Loop` + `Index Scan using ix_watch_states_title_id`, 1,000
  loops, 14.5 ms. No hash join, no seq scan.
- ⚠️ **The claim scan behind a wall of backed-off jobs: confirmed, unfixed.**
  216 ms with `Rows Removed by Filter: 1126674`. `ix_jobs_claim` is
  `(priority DESC, created_at) WHERE status = 'pending'` and a backed-off job is
  *still* `pending`, so every poll walks past all of them.
  `run_after <= clock_timestamp()` is not an indexable partial predicate
  (`clock_timestamp()` is not immutable), and putting `run_after` first destroys
  the priority ordering — so this is recorded rather than solved. It only bites
  when a large fraction of the queue is backed off, i.e. when an upstream is
  broken.
- ✅ **`list_unmatched`'s `OFFSET`: confirmed, and M9's E4 shipped the fix.**
  43.7 ms at offset 0, 388.9 ms at offset 1,126,574 — linear per page, quadratic
  to drain. `GET /admin/unmatched` now runs on
  `MediaItemRepository.list_unmatched_page` (`db/repositories/media_item.py:641`,
  called from `api/routers/unmatched.py:211`) with ADR-0034's typed cursor. **The
  offset method still exists and is still the CLI's** (`usher unmatched --limit
  --offset`, `cli.py:490`), which is the access pattern it is right for.

  **Measured on `pgvector/pgvector:pg17`, 200,000 items of which 70,000 unmatched,
  and what it says is that the keyset fixes the depth and not the page:** keyset
  page 1 = 70,000 scanned / 966 buffers / 16.4 ms; from a dated boundary = 34,999
  scanned / 23.0 ms; **from an undated boundary = 99 scanned / 328 buffers /
  1.9 ms**; `OFFSET 0` = 17.4 ms; `OFFSET 69,900` = 69,951 scanned with an
  **external merge sort on disk** (3.2 MB + a worker's 2.2 MB), 57.3 ms. **The
  offset's cost grows with depth and the keyset's does not — but the *sort*
  dominates either way**, because `ix_media_items_unmatched` is
  `(source_id) WHERE title_id IS NULL` and carries neither `added_at` nor `id`.
  The covering index that would remove it is an unminted migration;
  `.claude/rules/db-and-sql.md` carries the plans.

  ⚠️ **ADR-0034's NULL trap is reachable on this exact read.** `added_at` is
  nullable, so a page boundary can land inside the undated group — which is
  precisely the population an operator opens this queue to review. Postgres
  evaluates a row comparison element-wise and answers **NULL, not false**, when
  the first differing pair involves one, so the spelling a reader reaches for
  first (`((added_at IS NOT NULL), added_at, id) > (…)`) **drops the whole undated
  tail while every page it served looks full.** The boundary's own NULL-ness picks
  the branch instead, which the caller knows before it builds the statement.
- **The availability sweep: half.** `ix_media_items_sweep`
  (`source_id, available, last_seen_at`) takes the sweep's `UPDATE` from `Seq
  Scan` (`Rows Removed by Filter: 1,126,474`, 173 ms) to `Index Scan` with an
  `Index Cond` on all three columns, 102 ms. It does **not** help the guard's
  `count(*)`, a `Parallel Seq Scan` with the index (87 ms) and without it (86 ms)
  — ADR-0015's ceiling is a *fraction*, so the denominator is unavoidable and a
  source that *is* the whole table gives `source_id` no selectivity. **Both
  numbers are in migration `f1a7d3c9e824`, not the flattering one alone.**

## Not verified against a real Emby, named rather than implied

Consolidated across M3 (2026-07-31), M4 (2026-07-31), M5 (2026-08-02) and M9's
H4/H5 (2026-08-12). None of these has been exercised by any run:

- **`POST /Users/AuthenticateByName` itself.** Every run held a token, not a
  password. Task 3's extra `GET /Users/{userId}` remains the verified path for
  `User.Policy.IsAdministrator`, so whether the authenticate response carries it
  is still open.
- **Silent 401 re-authentication end to end**, and **durable-device registration
  across restarts.**
- **A full 1,126,674-item walk.** The longest socket held is **100 minutes** (zero
  reconnects, zero unforced failures); **four hours is unverified.**
- **A `LibraryChanged` with `IsEmpty: true`** — all twelve observed carried
  something, so what that field means is still a guess about a field nothing
  reads.
- **A `UserDataChanged` for a *series* entry**, which is where
  `UnplayedItemCount` would plausibly appear; and **whether a real entry is honest
  about play history for an item Usher did not itself write.**
- **`multi_version_movie.json`'s shape** — 1,400 real movies over two disjoint
  slices, every one carrying exactly one `MediaSource`.
- **A real `429` from Emby**, so `PortRateLimited.retry_after` has never been fed
  by a real upstream.
