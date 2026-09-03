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

Rules for this subsystem; the ADRs and docstrings named below hold the detail.
`ReconcileService` walks the source → `IngestService` writes `media_items` →
`MatchService` runs the ladder → `WatchStateSyncService` merges watch state.
Beside it `EmbyPushChannel` → `PushSupervisor` → `PushApplyService` run the
websocket, and `WatchWriteService` the client's own writes back out.

## Commands

```bash
uv run pytest tests/unit/test_adapters_emby_contract.py \
  tests/unit/test_services_{push,watch_write}.py         # the contract suite
uv run pytest tests/integration/test_services_{ingest,reconcile,push,watch_sync}.py
uv run usher sync --source "Living Room Emby"   # items, then watch state
uv run python scripts/measure_ingest.py --items 50000   # NOT a test; real database
```

## Bounding a live run against a real server

- ⚠️ **`MAX_PAGES` is a dead-man's switch, not a bound.** Exhausting it raises
  `PortDataMalformed`, so a reconcile "bounded" that way records `FAILED` and
  never reaches the sweep — usually the half the run exists to exercise. **Put
  the bound in the iterator**, and note that any "find the item where X" over a
  walk *is* a walk of ~1.13M items: filter server side.
- **Set `USHER_PUSH_ENABLED=false` and `USHER_WORKER_ENABLED=false`, or
  `USHER_PUSH_GAP_CLOSE=never`**, before any run whose request budget has to be
  statable — `push_enabled` is what gates the gap-closing walks below. Swap
  `EmbySession._authenticate_locked` in a `sitecustomize.py`, never in-process:
  a `usher work --once` subprocess cannot see a patched parent.

## Writing watch state back

Every route in `services/watch_write.py` runs four steps in an order that is the
contract: **write locally** with `origin = api`, so the next sync cannot mistake
Usher's own write for the source's truth; **commit** before anything is offered to
a client ([ADR-0033](../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md));
**invalidate and publish**, guarded on the row having changed; then **enqueue one
`WATCH_WRITEBACK` job per source copy**, deliberately *not* under that guard — it
says nothing about a source left out of step by a parked write-back.

- 🔴 **The request never touches a source, structurally.** `push_watch_state`
  **must raise** by contract, so "a client's write never fails on a down source"
  holds only if the call is **absent** rather than caught: nothing here may import
  `usher.ports.source`, and `tests/unit/test_api_watch.py` asserts that.
- **`_copies` branches to one of two statements, never both** — `list_for_title`
  carries `AND episode_id IS NULL` and `list_for_episode` is exactly the rows it
  excludes. Retracted copies are included on purpose: an unmounted drive is no
  reason to park.

## Emby's own write and read routes

- 🔴 **The write-back route is `POST /Users/{user}/Items/{item}/UserData`** with
  a JSON body (204). The session-scoped `PlayingItems/{item}/Progress` and
  `Sessions/Playing/Progress` answer **400**, keying off a play session Usher
  never has — and `FakeEmbyServer` implemented the adapter's own guess, so the
  whole contract suite passed against a write-back that had never worked.
- **That body must name `Played` even when `Played` is not what is changing** —
  it takes the DTO default and a position-only body unplays a played item.
  (`PlayCount` and `LastPlayedDate` survive the same omission; `Played` does
  not.)
- **`DELETE /Users/{user}/PlayedItems/{item}` is destructive beyond its name**:
  it resets `PlayCount`, clears `LastPlayedDate` *and* a non-zero resume
  position, so report unplayed through `UserData` instead. `POST` to it *is* how
  to mark played, and **a second press is a byte-identical no-op**, so a retry
  cannot move history on — which is what makes PRD 03's **"position first,
  played last"** load-bearing.
- 🔴 **A listing's `UserData` is not an item's.** `GET /Users/{user}/Items`
  reports `PlayCount: 0` and omits `LastPlayedDate` for an item whose own route
  reports the real values, and no `Fields=`/`EnableUserData`/`Ids` spelling
  fixes it. So **`watch_state()`, which walks listings, cannot carry play
  history**, and a walk must never write `play_count`/`last_played_at`
  ([ADR-0014](../../docs/prd/decisions/0014-absence-is-not-zero.md)). A pushed
  `UserDataChanged` *is* honest, so a read-back needs no polling.
- **`ExtendedVideoType`/`ExtendedVideoSubType` hold the string `"None"`, not
  null** — always truthy, so check them by token lookup, never for presence.

## The push lane

**Emby push works** ([ADR-0004](../../docs/prd/decisions/0004-push-over-polling.md)):
`/embywebsocket` upgrades, delivers `Sessions` on change, and pushes
`UserDataChanged` within seconds of an out-of-band change.

- ⚠️ **Neither an upgrade nor arriving messages establish that a channel is the
  one you think it is.** A handshake against *any* path succeeds, and a socket
  with **no credential at all** upgrades, subscribes and receives `Sessions` more
  often than an authenticated one, whose stream is row-filtered and sent only on
  change. **Assert on the *right* messages**
  ([ADR-0018](../../docs/prd/decisions/0018-push-health-is-a-message-ledger.md)).
- 🔴 **`/embywebsocket` does not accept `X-Emby-Token` as a header** — such a
  socket is anonymous, so the token cannot leave the URL and ADR-0012's accepted
  risk stands unnarrowed. **Liveness here is change-driven, not periodic**, so
  `DEFAULT_STALE_AFTER_SECONDS` (90 s) is a guess against a quiet server; ADR-0018
  refuses to count the pong, because **a pong is not delivery**.
- **`PushSupervisor` resets its failure counter on *delivery*, not on
  connection** — a buffering proxy connects perfectly every time, so a reset on
  connection means PRD 08's "mark `supports_push = false` after N failures"
  never fires, and catching that needs a fake with an **unbounded** supply of
  connections. The reset belongs in `PushHealth.record_open`, guarded on
  `opened_at`.
- **`ItemsRemoved` fires on a library from which nothing was removed**, so count
  it and retract nothing on it, or one refresh marks a present file unavailable
  ([ADR-0015](../../docs/prd/decisions/0015-availability-is-retracted-only-by-a-finished-walk.md)).
- **A dropped socket raises `PortUnavailable` rather than hanging, and Emby
  re-delivers nothing**, so **the gap-closing delta is the only cover there
  is**; no real `429` has ever been seen. Do not let the queue fill during that
  walk.
- 🔒 **`socket_logger()`'s level is the token defence; `configure_logging` is
  what it defends against** — that sets `propagate = True` on every existing
  logger and a root handler at level 0, so at `DEBUG` the `websockets` URL logs
  the session token. `socket_logger` sets `CRITICAL + 1`, the one thing it never
  touches.

## Gap-closing walks are unasked-for work

`LaneSupervisor`'s reconnect gap-closer calls `reconcile(source, DELTA, adapter)`,
and **a DELTA with no completed item-lane run has no `since`, so
`list_items(None)` reads the whole library.** `USHER_PUSH_GAP_CLOSE` defaults to
`cursored`, closing a gap only when a completed walk gives it a `since` (#9).
⚠️ **The bound is a refusal rather than a cap on purpose**: a truncated walk
records `COMPLETED`, so everything it never reached is skipped by every later
delta, permanently. ⚠️ **And that guard reads the item lane's cursor only, while
`_close_gap` also runs `watch.sync(...)`** (#41) — the cursors are independent,
so a source with completed delta runs and no completed `watch_state` run passes
the guard, closes a delta gap in seconds, then walks the whole library on the
watch half for ~11 hours — a reachable path, not an observed run — and **neither
log line names it**. [ADR-0042](../../docs/prd/decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md)
made that walk resumable from `sync_runs.position`; it did not teach the guard to
read both cursors.

## The match ladder

Six tiers: `tmdb_id`; `imdb_id`; `tvdb_id`; exact normalised name with year ±1
and exactly one survivor (`_confident`); stub-on-sight; unmatched — where a
`match` job is enqueued at `BACKFILL` for the remote search.

- **An episode must never walk the ladder.** It carries the *episode's* own
  provider ids, not its series'; TVDb numbers episodes and series in overlapping
  namespaces and the TVDb statement does not filter on kind; and no episode IMDb
  id is in the catalog, so the stub tier would mint one junk `Title` per episode.
  `MatchService` returns `UNMATCHED`, with no lookups and **no remote-search
  job**, and `IngestService` attaches it as `MatchMethod.SERIES_PARENT`.
- **Tier 4 is not the fallback its position suggests** — name+year out-resolves
  the `tmdb_id` tier on a real library, most catalog titles carrying no
  `tmdb_id`. A probe with **no** year resolves nothing at all: the year
  `BETWEEN` propagates `NULL`, so "0.0%" there is not a bug.
- **A malformed `ProviderIds.Imdb` is real** (bare digits, no `tt`), and
  **`_as_imdb` is the guard, not `_usable_ids`**: removing the latter's filtering
  raises nothing, while dropping `_as_imdb`'s pattern check raises a
  `ValidationError` — not a `UsherPortError`, so an aborted sync.
- ⚠️ **`ProviderIds`' key space is wider than three and its case is not stable**
  (`Tmdb`, `IMDB`, `tmdb`, `TvMaze`, `TV Maze`, `TmdbCollection`, …), so
  `key.lower()` makes `IMDB` usable while an **exact-key `get("tmdb")`** keeps
  `TmdbCollection` out — a prefix match attaches films to collections.

## Rules the pipeline enforces, each learned from a defect

- **Nothing a source puts in a payload may abort a walk.** A pydantic
  `ValidationError` is not a `UsherPortError`, so one stray `ProviderIds.Imdb`
  would abort that source's sync permanently. **Filter every value to the shape
  the model accepts before the constructor.**
- **A service that checkpoints per batch must not evolve its own stale copy in
  the failure handler** — `reconcile`'s binding is the pre-walk value, so
  `run.evolve(status=FAILED)` writes `items_seen = 0` over a real checkpoint.
- 🔴 **The availability sweep must not move into a `finally:`, and the obvious
  test shape hides why.** A walk that fails immediately retracts everything,
  which ADR-0015's ceiling refuses — the case then fails for the wrong reason
  and never exercises a sweep that *succeeds* after a failed walk. The shape
  with teeth commits most of the library and then raises (ADR-0015 has the
  arithmetic).
- **`observed_at` must be the run's start instant, not `now()`.** A per-row write
  instant is later than `run.started_at`, so the sweep still spares everything and
  no retraction test fails; what breaks is the column's meaning. **Assert
  `stored.last_seen_at == run.started_at`.**
- **An episode's `MediaItem` carries two ids and its `WatchState` may carry one**
  (`num_nonnulls(title_id, episode_id) = 1`), so `_watch_target` collapses the
  pair with the episode winning: passing both raises `PortDataMalformed` and
  aborts a batch of thousands. `resolve_external_ids`' title branch needs
  `episode_id IS NULL`.
- **A history backfill must carry its own fresh `observed_at`, and both test
  layers are blind to why.** The trigger stamps the *write* instant, so a backfill
  carrying the walk's instant is refused by the row it exists to repair; the fake
  accepts what Postgres refuses and `now()` is frozen per transaction, so the
  integration suite stages it with `clock_timestamp()` in a raw `INSERT`.
  **Skipping `resolve_seasons`/`resolve_episodes`** is the same shape: unit cases
  stay green — a dict has no foreign keys — and the FK fails on walk two.

## Scale

- **`GET /admin/unmatched` is keyset-paged (ADR-0034); the `OFFSET` method stays
  the CLI's.** ⚠️ **ADR-0034's NULL trap is reachable here**: `added_at` is
  nullable, so a page boundary can land in the undated group — the population an
  operator opens this queue for — where a row comparison answers **NULL, not
  false**, dropping the undated tail while every page served looks full. Branch
  on the boundary's own NULL-ness.
- ⚠️ **The claim scan walks past backed-off jobs**, recorded rather than solved
  (PRD 08), and **stub-on-sight is the one path here that is not set-based** —
  bounded by new titles, not items.
