# 03 — Sources and synchronisation

How media servers get into the catalog, stay current, and stop being visible
anywhere above the adapter boundary.

## The Emby adapter

### Durable client authentication

Emby access tokens are per-device session tokens. A client that authenticates
anonymously gets a session that upstream servers prune or rotate, and the token
dies with no way to renew it. (This is the concrete failure that motivated
Usher: a token stored in a Home Assistant dashboard silently started returning
401 on every authenticated endpoint.)

Usher authenticates as a **named, stable device**:

```
Authorization: MediaBrowser Client="Usher", Device="<source name>",
               DeviceId="<persisted UUID>", Version="<app version>"
POST /Users/AuthenticateByName  {"Username": ..., "Pw": ...}
→ AccessToken, User.Id
```

Authenticated requests carry the **same** identity header — that is what makes
every request attributable to one device rather than only to the login — plus
the session token in `X-Emby-Token`. Emby has no OAuth2 and therefore no
refresh-token flow; this pattern *is* the refresh mechanism.

- `DeviceId` is generated once and persisted on the `Source` row. Usher appears
  as one device in Emby's dashboard rather than an accumulating pile of sessions.
- The token **is cached** — in memory, for the lifetime of the adapter, never
  written to the database — and **any 401 triggers silent re-authentication**
  with the stored credentials and the same `DeviceId`. That is the refresh
  mechanism; no human ever pastes a token. It is also the *only* trigger:
  there is no TTL, no proactive rotation, and no expiry Usher applies of its
  own, so a minted token stays current until Emby prunes the session or an
  operator revokes it. Re-authentication is **single-flight** — concurrent 401s
  collapse into one `AuthenticateByName` — and exactly one retry is attempted
  per request. A credential that is genuinely *wrong* is remembered for a
  cooldown, so a bad password cannot turn every call into two requests against
  a source measured at 1–5 s per request. That matters beyond this section,
  because a direct-play URL carries this token
  ([ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md)).
- Credentials live behind `credentials_ref` indirection: an opaque, random
  token addressing a row in `source_credentials`, encrypted at rest under a key
  derived from `USHER_SECRET_KEY` ([08](08-operations.md)). The plaintext
  exists only in memory in the adapter, and neither the username nor the
  password nor the ref is ever returned by any endpoint, including admin. The
  ref is random rather than derived from the source id so that rotation — write
  the new secret under a new ref, flip the pointer, delete the old row — is
  expressible at all. **The session token minted from them is the one
  documented exception**: it reaches a client inside a `direct` playback
  target's URL, because Usher does not proxy the bytes and the route serving
  them authenticates
  ([ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md); PRD
  [08](08-operations.md) carries the same qualification).

> **The identity header was exercised against the live server on 2026-07-31,
> and one thing it does *not* do is worth recording.** Presenting an existing
> token alongside a *different* `DeviceId` in the `Authorization` header
> neither invalidated that token nor created a second session — `GET /Sessions`
> was byte-identical before and after. Emby binds a session to the token's own
> authentication record, made at `AuthenticateByName` time; the header's
> `DeviceId` on later requests is not what registers a device. So the "one
> durable device" property comes from authenticating once with a stable
> `DeviceId`, not from repeating it — which is what the adapter does, and is
> also why the live run could borrow a token without disturbing the client that
> minted it.

### Push events

Emby exposes a WebSocket at `/embywebsocket?api_key=<token>&deviceId=<id>`.
Verified against Emby 4.9.5.0 binaries (the version this deployment runs):

| Message | Scope | Use | Becomes |
|---|---|---|---|
| `LibraryChanged` | Per-user, payload-filtered | Items added / updated / removed | One `SourceEvent` per non-empty array: `ITEM_ADDED`, `ITEM_UPDATED`, `ITEM_REMOVED`. A removal **retracts nothing** — see below |
| `UserDataChanged` | Own data only | Watch position, played flags | One `WATCH_STATE_CHANGED` carrying the ids *and* the states the message itself contained |
| `Sessions` (subscribe) | Per-user row-filtered | **Nothing is derived from it. Its whole value is that it arrives.** | Nothing. Counted before it is parsed |

**That last row said "Playback events", and it read as though Usher derived
something from them. It does not, and the correction matters.** `Sessions`
carries playback state for sessions Usher is not part of; deriving anything
from it would mean tracking play sessions Usher never starts. It maps to zero
`SourceEvent`s by design, and it is counted anyway — before it is parsed —
because a received frame is evidence the socket is alive whatever it says. On
an idle library, which is most libraries most of the time, `Sessions` is the
*only* thing keeping `push_available` true. Counting only mapped events would
make a library nobody touched for a day read as a dead channel and reconnect it
forever.

**Its cadence is measured, and it is not the interval it looks like.**
`SessionsStart`'s `"0,1000"` really is `initialDelayMs,intervalMs` — an
*unauthenticated* socket receives `Sessions` at ~1 Hz — but the
row-filtered stream an authenticated socket receives arrives only when the
filtered view changes: **median 38.7 s, p90 46.5 s, max 72.9 s** over 182
intervals in 100 minutes, 2026-08-02. So `push_stale_after_seconds`' 90 s
default survives, with **1.23x** headroom over the worst gap seen — and the
worst gap grew monotonically with the window (52.6 s at 26 minutes, 60.1 s
at 70, 72.9 s at 96), on one household on one evening, against a signal that
is change-driven rather than periodic. Read it as a bound that has not been
falsified rather than one shown to be safe: there is no application-level
heartbeat on this channel, so any fixed ceiling is a guess. It is a setting for exactly that reason, and
`usher.source.push.reconnects` is how a household where 90 s is too tight
becomes visible rather than silent.

**No admin privileges are required** — a normal user token works, and there is
no role check in Emby's subscription path. (Note: guidance derived from Jellyfin
is misleading here; Jellyfin added admin gating after forking, so its docs claim
restrictions Emby does not have.)

**Not required is not the same as prevented, and nothing prevents it.**
`POST /admin/sources` ([07](07-client-api.md)) takes whatever account an
operator supplies, and nothing in the adapter inspects its role, so admin
credentials work — and put an admin token into every direct-play URL, and
(from M5) into a long-lived push socket, which widens what a captured URL or
socket grants from "this user's library and watch state" to "everything an
Emby administrator can do". Configure a normal user. This is an accepted
risk, recorded in
[ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md).

**It is now observable, and still not refused.** `verify()` reads
`Policy.IsAdministrator` off `GET /Users/{userId}` — which answers 200 to the
user's own non-admin token; `GET /Users/Me` answers 500 on Emby 4.9.5.0 and is
not a shortcut — and `GET /admin/sources/{id}/status`
([07](07-client-api.md)) reports `is_administrator`. Three-valued, like
`push_available`: `null` means the check did not run, and a failure to read
the role narrows the answer rather than failing the status request. An
administrator account logs a warning and is served anyway, because an operator
whose only working account is an administrator account still needs a catalog.

**Both of this socket's parameters are also carried by a direct-play URL.**
`api_key` and `deviceId` are exactly what a `direct` `StreamTarget`'s query
holds, so anything holding one of those URLs holds what this channel is opened
with, under Usher's own registered device id — which is why the "one durable
device" property buys revocability but not attribution. Same ADR.

Operational requirements:

- **Heartbeat every 20–25 s.** Emby sends no keepalive of its own. nginx closes
  idle connections at 60 s and Cloudflare at ~100 s, so the client must generate
  traffic.
- **Reconnect with exponential backoff and jitter**, then run a delta reconcile
  on reconnect to recover anything missed while disconnected.
- **Fall back cleanly.** Reverse proxies that don't forward `Upgrade` return 404
  instead of 101. If the socket can't be established, the adapter reports
  `supports_push = false` and the reconciler covers the gap.

> **Verified end to end 2026-07-29.** Against this deployment, with a normal
> non-admin token and a stable `DeviceId`: the handshake returns **101**,
> `Sessions` arrives periodically, and **`UserDataChanged` fires within seconds
> of an out-of-band played/unplayed change**. Push is the real mechanism here,
> not an aspiration. Full detail and two corrected earlier findings:
> [ADR-0004](decisions/0004-push-over-polling.md).
>
> **Health-check caveat:** a handshake against a nonexistent path also upgrades
> and receives `Sessions`, so a successful upgrade proves nothing. The adapter
> must assert on *received messages* to consider push healthy.
> [ADR-0018](decisions/0018-push-health-is-a-message-ledger.md) is the whole
> of M5 built on this sentence.

> **Re-measured live 2026-08-02, and the first run in this repository ever to
> parse a real `/embywebsocket` message.** Five things it settled, each of
> which the code had been guessing at:
>
> - **`LibraryChanged` arrives, and its five arrays hold *ids*.** Never once
>   observed before this run; **twelve** arrived unprompted in 100 minutes,
>   with all seven documented keys and every array a list of id strings
>   rather than of item objects. The shipped mapper produced 7 `ITEM_ADDED`,
>   7 `ITEM_UPDATED` and 1 `ITEM_REMOVED` from them. One frame carried a
>   real `ItemsRemoved` **on a library from which nothing was removed** —
>   which is [ADR-0015](decisions/0015-availability-is-retracted-only-by-a-finished-walk.md)'s
>   central argument, observed rather than reasoned. One `ItemsUpdated`
>   carried **42** ids against `push_max_items_per_event`'s default of 50.
> - **The envelope is not uniform.** `UserDataChanged` carries
>   `{MessageId, MessageType, Data}` with a distinct 32-hex `MessageId` per
>   *message*; `Sessions` carries `{MessageType, Data}` and **no `MessageId`
>   at all**, on 183 of 183 frames.
> - **`UserDataChanged.Data` is an object** with `UserId` and `UserDataList`,
>   and an entry carries `ItemId`, `PlaybackPositionTicks`, `Played`,
>   `PlayCount`, `IsFavorite`, plus `PlayedPercentage` when the position is
>   non-zero and `LastPlayedDate` when it is played. There is **no `Key`**.
> - **A pushed entry agreed with `GET /Users/{u}/Items/{item}` field for
>   field**, in the same second, across three transitions of one item —
>   including `PlayCount` and `LastPlayedDate`. So this third payload shape
>   is *not* the partly-honest one the listing route is. The adapter still
>   reports `play_count`/`last_played_at` as `None`; see the ADR-0014 note
>   below for why that is a deliberate lag rather than an oversight.
> - **The handshake proves even less than ADR-0004 recorded.** A socket with
>   **no credential at all** also upgrades, also accepts `SessionsStart`, and
>   then delivers `Sessions` roughly once a second carrying the *whole
>   server's* session list, where the authenticated socket receives a
>   row-filtered view at a median of one frame per ~24 s. So neither an
>   upgrade nor arriving messages establish that a channel is the
>   authenticated one.

> **Settled in M5.** `SourceEvent` carries the states the upstream's own
> message already contained (`watch_states`, keyed by `external_id`), and an
> id it could not parse falls back to `get_watch_state` — one authoritative
> request. The alternative the marker named, re-walking
> `watch_state(since=...)`, was never close: that walk's only knob is the
> cursor, and over a 30-day `MinDateLastSavedForUser` window it returns
> 29,027 items, per event, on a lane budgeted at one connection per source.
>
> The two lists may differ in length and are never aligned by position — a
> state names its own item, and one the event did not list is refused at
> construction. Position-aligning them would let a single unparseable entry
> write every later state onto the wrong item.
>
> **A carried state's `play_count`/`last_played_at` are `None` on Emby**, and
> that is [ADR-0014](decisions/0014-absence-is-not-zero.md) rather than
> laziness: a `UserDataChanged` message is a third payload shape, and absence
> is the only honest report of a field nobody had measured. The
> `watch_history` backfill recovers the pair from the single-item route,
> which is the chain M4 already built.
>
> **The 2026-08-02 live run measured it, and it was truthful** — a pushed
> entry's `PlayCount` and `LastPlayedDate` matched the single-item route
> exactly. That makes reading them a **measured opportunity**, worth roughly
> one `watch_history` job per played item, and it is deliberately **not
> taken here**: the evidence is one item across three transitions, all of
> them writes Usher itself made, and ADR-0014's rule is that a reported
> number must be *true* rather than merely present. Writing a `0` over a real
> `13` is permanent, so the bar for turning that field on is a measurement
> over items with real history that Usher did not author. Recorded so the
> next run has one thing to check rather than a design to redo.

### Walking the library

`list_items` and `watch_state` page over the source's own listing, one page in
flight at a time. Measured against the live deployment on 2026-07-31, a walk of
it is **1,126,674 items** — 94,438 movies, 32,409 series and 999,827 episodes —
so materialising one is not an option. (Re-measured at the end of the same
week: **1,126,789** — 94,448 / 32,414 / 999,927. Every figure derived from a
live library is a dated snapshot, not a constant; the numbers below keep the
date they were taken on rather than being refreshed in place.) Three properties
the adapter contract enforces, each now checked against that server:

- **A stable ascending sort by creation date, with a tiebreak.**
  `SortBy=DateCreated,SortName`. Items added during a walk land at the end, so
  an insertion cannot shift an unread item backwards past a page boundary
  already consumed. A *deletion* mid-walk can still shift one item out of view;
  that is a bounded imprecision the nightly full reconcile covers, and it is why
  the contract permits duplicates but forbids silent truncation.

  **Emby honours the secondary key** — demonstrated on a tie-heavy primary key,
  where `ProductionYear,SortName` returns the tied block in `SortName` order and
  `ProductionYear` alone returns it in a different, insertion-shaped one. Tie
  *instability* was not reproducible here: repeated identical pages came back
  identical, and overlapping `StartIndex` windows agreed exactly, with and
  without the tiebreak. The tiebreak stays anyway — it costs one word in a query
  string, the failure it prevents is silent, and "this server's query plan was
  stable across three requests" is a much weaker claim than "the order is
  total".
- **The delta cursor is widened by one second.** `since` is contractually
  inclusive; whether the upstream's own comparison is `>=` or `>` is not
  something Usher should have to be right about. Sending one second early is
  correct either way, and a superset is explicitly allowed because callers
  deduplicate by `external_id`.
- **An unrecognised filter degrades to a full walk, never to an empty result.**
  Measured: an invented parameter name was ignored outright and the request
  returned the full unfiltered `TotalRecordCount`. So the worst case of a wrong
  delta-filter name is the nightly reconcile's own behaviour.

Two different filters are sent, because a library edit and a watch-state change
do not touch the same timestamp: `MinDateLastSaved` for `list_items`,
`MinDateLastSavedForUser` for `watch_state`. Both are honoured, and they are
genuinely different filters rather than aliases — against those 1,126,674
items, a cursor ten years ahead returned 0 for each, and a 30-day cursor
returned 28,934 and 29,005 respectively.

### Health and status

`verify()` returns a `SourceStatus`, not a bool: `GET /admin/sources/{id}/status`
([07](07-client-api.md)) has to report bad credentials, unreachable, and
reachable-but-push-blocked as separate states. The unauthenticated
`/System/Info/Public` probe is what separates the first two — a failure there is
a reachability failure and cannot be anything else.

That split holds against the real server, checked 2026-07-31:
`/System/Info/Public` answers **200 with no credential of any kind** and carries
the `Version` that becomes `server_version`, while `/System/Info` answers
**401** without a token. A live `verify()` returned `reachable: true`,
`authenticated: true`, `push_available: null`, `server_version: "4.9.5.0"`.

**`verify()` also spends one request on the account's own role.** `GET
/Users/{userId}` carries `Policy.IsAdministrator` and answers 200 to the
user's own non-admin token, so `is_administrator` reports the configuration
this section calls "not required but not prevented". It is a warning and a
field, never a refusal, and `null` means the check did not run — see the
paragraph above and
[ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md).

`push_available` is deliberately three-valued, and **M5 fills it from a message
ledger rather than from a probe**
([ADR-0018](decisions/0018-push-health-is-a-message-ledger.md)). `verify()`
opens no socket at all — a status
screen a dashboard polls must not cost a socket per poll against a server
measured at 1–5 s per request, and it would still be answering a question about
a socket that is not the one doing the work. It reports `null` ("not probed")
for an adapter that has never had a channel, and the live answer — a connection
*and* at least one received message *and* a recent one — for the adapter a push
lane is running. `GET /admin/sources/{id}/status` reads the lane's, injected by
the composition root.

The on-demand answer is `usher push --probe`, which opens a channel on purpose
because an operator asked it to, and reports **what arrived** — the event kinds
and whether the channel is delivering — rather than that the handshake
succeeded. `SourceAdapter.probe_push` is a *concrete* method on the port whose
body is calls to `events()` and `supports_push` and nothing else, so a second
adapter inherits that rule instead of re-deriving it; re-deriving it wrongly is
one line. See the health-check caveat above: a handshake against a nonexistent
path also upgrades, so an upgrade is not evidence and only received messages
are.

**`supports_push` and `events()` are related one way only.** `supports_push` is
a health signal grounded in messages; `SourceNotSupported` from `events()` is a
capability answer. An adapter reporting `true` must offer a channel, and one
with no channel must report `false` — but an adapter that *has* a channel
reports `false` from the moment it opens until the first message arrives on it,
which is the whole point. A contract asserting the two agree in both directions
would forbid exactly the honest implementation.

## Reconciliation is not optional

Push is the fast path, never the only path. Sockets drop, events are missed, and
`LibraryChanged` carries no guarantee of delivery.

| Lane | Trigger | Work |
|---|---|---|
| **Push** | WebSocket event | **Apply it inline when it is small; defer to a delta when it is large.** A `WATCH_STATE_CHANGED` carrying its own payload merges with no request at all; one naming more than `push_max_items_per_event` items with no payload becomes a delta walk instead, because a request per item against a 1,126,789-item library is a design defect rather than a slow path |
| **Reconnect delta** | Socket re-established | Items changed since the last cursor. **The walk runs *after* the socket is up**, so anything that changes during it arrives on a connection that is already buffering (`max_queue=256`); the reverse order leaves the window between the walk and the handshake silently uncovered. Rate-limited, so a flapping socket cannot turn into one delta per few seconds |
| **Full reconcile** | Nightly | Walk the source; upsert everything; mark unseen items `available = false` |

Polling is the backstop, not the design.

**A push `ITEM_REMOVED` retracts nothing.**
[ADR-0015](decisions/0015-availability-is-retracted-only-by-a-finished-walk.md)
is unambiguous — only a walk that provably finished sweeps — and an Emby
library refresh emits `ItemsRemoved` for items that have not gone anywhere.
**Observed 2026-08-02**: one arrived during a 100-minute listen on a server
where nothing was deleted. The event is counted and logged; the row stays
available until the nightly
walk sweeps it, which [08](08-operations.md) already prices as "availability
goes stale, not wrong". **Emby does not re-deliver what a disconnected client
missed** — measured 2026-08-02, over a 61 s outage with a real change made
inside it and 90 s of listening afterwards — so the gap-closing delta is not
belt-and-braces, it is the only cover there is.

**Retraction is a separate step, and it can decline.** Marking unseen items
unavailable is a distinct call the reconciler makes only after the walk
returns normally — never a side effect of the upsert — and even then it
refuses to retract more than `sync_max_retract_fraction` (default `0.25`) of
a source in one run, raising and changing nothing. `list_items` raising
rather than truncating already covers a walk that *failed*; this covers a
walk that *succeeded* and returned far less than the library holds, which an
unmounted drive, an accidentally-removed library, and a permissions change on
Usher's own account all produce identically. `1.0` disables the ceiling,
which is what an operator deliberately removing a library passes. See
[ADR-0015](decisions/0015-availability-is-retracted-only-by-a-finished-walk.md).

An item that reappears in a walk is available again at that moment: the
upsert restores it, because appearing in a walk *is* the evidence of
availability. The sweep only ever sets `false`.

**Only a full walk sweeps, and the ceiling is not a substitute for that.** A
delta walk returns only what changed, so by construction nearly everything is
"unseen" — a sweep after one would retract the library. The ceiling fires on
a *fraction*, so it catches that catastrophe and misses the quiet version: a
full walk that failed after committing eight of ten batches leaves 20% of the
source stale, which is under the default ceiling, so a sweep that ran anyway
would succeed and silently retract those rows. The gate is the success path,
not the guard.

**A delta walk resumes from the newest run of *either* item lane that
completed.** Full and delta both walk `list_items` and differ only in whether
a `since` is passed, so a nightly full run that finished at 03:00 is a valid
floor for a delta at noon; reading only the delta lane would re-walk a window
the nightly run already covered. Only *completed* runs count — resuming from
one that failed halfway skips everything it never reached, silently. A full
walk ignores every cursor: one that inherited a `since` would return only
what changed and then sweep, which is exactly the combination ADR-0015 exists
to make unreachable. (`watch_state` is a third lane with its own cursor: it
walks a different method under a different upstream filter.)

**Each batch is committed with the run's counters.** 1,126,674 items is
hours; a crash must cost the batch in flight rather than the walk, and a
`sync_runs` row an operator can watch has to exist before the walk starts
rather than after it finishes.

## Read-through with a priority queue

The catalog is usable immediately and improves under you. Three mechanisms:

**1. Stub-on-sight.** Ingest creates a `Title` in `stub` state from the source's
own metadata the moment an item is seen. It is queryable, browsable, and
playable before any enrichment happens.

**2. A real priority queue.** Enrichment is a Postgres-backed `jobs` table
(`SELECT … FOR UPDATE SKIP LOCKED`), ordered by priority then age. Default
priority derives from popularity and recency.

| Priority | Source of demand |
|---|---|
| 100 | Title opened by a client right now |
| 80 | Title visible in a row the client just requested |
| 50 | Newly added to a source |
| 20 | Background backfill |

**3. Demand promotes work.** Requesting an unenriched title promotes its job to
the front of the queue rather than blocking the response. The API returns the
stub immediately with `enrichment_state: "stub"`.

**4. The client is told when it changes.** Completion publishes a
`title.updated` event on a Server-Sent Events channel; clients patch in place.
No polling on either side of the system.

```
client opens title ──▶ API returns stub instantly
                       └─▶ promote job to priority 100
                                   └─▶ enrich (TMDb)
                                            └─▶ index + embed
                                                     └─▶ SSE title.updated
                                                              └─▶ client patches
```

Target: under 5 seconds from open to enriched for a single title.

## The ingest pipeline

Five idempotent, resumable stages. Any stage can be re-run without duplicating
work. **It was four until M7 added the fifth** — derivation — and the fifth is
unlike the other four in one way worth stating at the top: it makes **no
network request at all**, because everything it needs is already in
`raw_payloads`.

### 1. Ingest

Normalise the source item; upsert `MediaItem` on `(source_id, external_id)`;
create or attach a `Title` stub.

**Source payloads are not stored.** This stage used to say to keep each
item's raw payload in `raw_payloads`; at 1,126,674 items and ~8 kB apiece
that is ~9 GB against a database [08](08-operations.md) budgets at 8–12 GB
total, to cache something re-readable from the source in one request.
`raw_payloads` caches *provider* responses only — see
[ADR-0016](decisions/0016-raw-payloads-cache-providers-not-sources.md).

The adapter emits movies, series, **and episodes** — Emby addresses episodes
directly, and `SourceItem` carries `series_external_id`, `season_number`, and
`episode_number` for exactly this (all three verified present on a live episode
payload, 2026-07-31). Episodes are also the bulk of the work: 999,827 of this
deployment's 1,126,674 items. `Season`/`Episode` and the `seasons`/`episodes`
tables landed with **M4**, so the series hierarchy is now storable;
`media_items.episode_id` and `watch_states.episode_id` have real foreign-key
targets for the first time.

**An episode is attached to its series' `Title`, and the attachment spans
pages.** A walk is sorted by creation date, which guarantees nothing about a
series preceding its own episodes — Emby genuinely interleaves them — so the
series map is built from the whole page *and* from a batched read of what
earlier pages already stored. An episode whose series is not yet known is
stored unmatched and enqueued for a re-match; it is never dropped and never
attached to a guess. An episode with no season or episode number is left
unmatched for the same reason: defaulting the numbers to zero collapses every
such episode of a series into one row.

**Enrichment is enqueued only for the titles that need it.** A nightly walk
sees all 1,126,674 items every night; enqueueing an `enrich` job for each
makes the queue permanently the size of the library. One batched read of the
titles' enrichment tiers per page decides, compared through `ENRICHMENT_RANK`
([ADR-0008](decisions/0008-enrichment-tier-vs-failure.md)) rather than by
comparing `EnrichmentState` members, which are `StrEnum` and order
lexicographically.

**Every repository call in this stage is once per batch**, including the
season and episode writes: at 999,827 episodes, three round trips apiece is
the difference between a walk that finishes and one that does not.

### 2. Match — resolve to a canonical Title

Ordered by confidence, stopping at the first hit. **Every lookup is issued
once per batch**, not once per item: `MatchService` turns a page of source
items into one set of provider references and one set of name+year probes,
and `TitleMatchRepository` answers each in a bounded number of statements.
At 1,126,674 items a per-item matcher is not slow, it is a design defect.

1. `ProviderIds.Tmdb` from the source → `(tmdb_id, kind)` lookup. The kind is
   not optional — TMDb's movie and series id spaces overlap on 26,968 ids
   ([ADR-0011](decisions/0011-tmdb-id-is-namespaced-by-kind.md)).
2. `ProviderIds.Imdb` → local lookup against the bootstrapped IMDb skeleton
   ([04](04-catalog-bootstrap.md)) — no network call, because the catalog
   already knows 12.7M titles. One global namespace, so no kind participates.
3. `ProviderIds.Tvdb` → `tvdb_id` lookup. M2's bootstrap linked 50,793 titles
   this way and Emby series routinely carry a TVDb id and no TMDb one, so a
   ladder stopping at IMDb pushes most television into the review queue for
   no reason. Like IMDb, one namespace, no kind.
4. Name + year against the local skeleton, accepted above a confidence bar
   (normalised title match, year within ±1) **and only when unambiguous** —
   several titles sharing a name, kind and year is common (remakes, and
   IMDb's own duplicates), and picking one attaches watch history to the
   wrong film.

   **Measured on real source names, 2026-07-31**, against the live Emby
   deployment and a real 1,271,314-title bootstrap: this rule resolves
   **72.2% of movie names** (433/600, sampled across six windows spanning the
   whole 94,448-movie collection) and **75.3% of series names** (223/296).
   It was expected to resolve almost nothing — edition suffixes, years in
   the name and release-group noise — and it does not. Of the movie misses,
   **142 are absent from the catalog and only 25 are ambiguous**, so what
   feeds the review queue is mostly a catalog that does not hold the title,
   not a bar set too high. A probe carrying **no year resolves nothing at
   all**, by construction: `year BETWEEN p.year - 1 AND p.year + 1`
   propagates `NULL`, which is deliberate — any other spelling matches every
   undated IMDb skeleton sharing the name.
5. A trusted provider id the catalog does not hold → **create a stub**
   ("stub-on-sight"). Deliberately narrower than "create a Title from what
   the source said": an id from TMDb, IMDb or TVDb is an identity claim
   strong enough to build a canonical title on; a bare name is not. Only
   291,772 of the catalog's 1,271,314 titles carry a `tmdb_id`, so this is
   expected to be the common path for anything modern.

   **It was not, on the one live slice measured.** A real 600-item walk
   created **zero** stubs: all 22 non-episode items resolved at tier 1 or 2
   (21 by `tmdb_id`, 1 by `imdb_id`), and the other 578 were episodes, which
   never walk the ladder. One consequence worth keeping: with no new titles,
   a first walk and a nightly walk cost *exactly* the same — 40 statements
   for a 600-item batch either way — because stub creation is the only
   non-set-based step in the pipeline.

   **A provider id the source reports may not be one.** 11 of 885 real
   `ProviderIds.Imdb` values in a 900-item sample are bare digits with no
   `tt` prefix. `Title.imdb_id` is pattern-validated and a pydantic
   `ValidationError` is not a `UsherPortError`, so an unfiltered value here
   aborts that source's sync permanently. Every id is filtered to the shape
   the model accepts before the constructor sees it.
6. No confident match → `title_id` stays NULL; the item enters the review
   queue, and a `match` job is enqueued at `BACKFILL` priority.

**The TMDb search tier is queued, not inline.** It is one network call per
unmatched item, and a first full walk against an unbootstrapped catalog
produces those in the hundreds of thousands — running them inside the walk
makes the walk's duration a function of TMDb's rate limit rather than of the
source's. It runs off the priority queue instead, which is what the queue's
concurrency limit is for.

**Measured against TMDb's own search, 2026-08-01** — the measurement tier 4
had never had, because until Task 26 nothing in this repository had made a
TMDb API request. 320 IMDb names (160 movies, 160 series, stratified into
four `numVotes` bands so the popular end a real library sits at is visible
separately from the long tail), each searched through the shipped
`TmdbMetadataProvider` and judged by the shipped `_confident`:

| | rate |
|---|---|
| all 320 | **83.1%** |
| movies (160) | 87.5% |
| series (160) | 78.8% |
| `numVotes` ≥ 100,000 | 90.0% |
| 10,000–100,000 | 91.3% |
| 1,000–10,000 | 81.3% |
| 100–1,000 | 70.0% |

Two things fall out of it. **TMDb's relevance ordering really does put the
obvious answer first** — 263 of the 266 confident resolutions were TMDb's
*first* result, the other three no lower than rank 3, and for series it was
126 of 126 — so the rule and the ordering agree, and the rule is not
depending on the ordering to do it. And **TMDb's year filter is exact where
this ladder's is ±1**: all 294 candidates it returned carried exactly the
year asked for, so 26 of the 320 came back with *nothing* rather than with a
one-year-off answer. Re-asking those 26 without the year resolves 13 of
them, every one a title TMDb dates a year away from IMDb; the adapter now
does that automatically when a year-filtered search finds nothing, which
takes the table above to **87.2%** overall. Dropping the year filter
outright was measured too and is worse — 6 of 133 already-resolving names
stop resolving, because "exactly one survivor" across every year at once is
a harder test than within one.

This is the tier-3 number's counterpart, not its replacement: the two are
different candidate sets and both are now measured (72–75% locally, 83–87%
remotely, on different name samples).

**Episodes never walk this ladder.** An Emby episode payload carries the
*episode's* own provider ids (`{"Imdb": "tt2178782", "Tvdb": "4517466"}` on a
live payload), not its series'. TVDb numbers episodes and series in different
namespaces that overlap numerically, so tier 3 would resolve an episode to
whichever unrelated series holds that integer; and no episode's IMDb id is in
the catalog at all (`tvEpisode` is excluded from the bootstrap by design), so
tier 5 would mint one junk `Title` per episode — 999,827 of them. An episode
is resolved by attaching it to its series' `Title` during ingest, and
enqueued for a re-match only when that series is not yet known.

Bootstrapping first makes stages 2–3 local, which is why matching is fast and
mostly offline.

### 3. Enrich

One TMDb request per title, plus per-season episode fetches for series, and
sets `field_provenance`.

**The `append_to_response` list is not the same for both id spaces**, which
is a correction to what this section said before M4 built the adapter:
`release_dates` is a movie-only namespace and `content_ratings` is the TV-only
equivalent. Read from TMDb's published reference on 2026-07-31, then
**measured against the live API on 2026-08-01 — and the measurement corrects
the correction.** Asking either half for the other's namespace is not an
error: TMDb answers `200` with the requested key simply absent from the body,
and does the same for a namespace that does not exist at all. So one shared
list is worse than an endpoint that does not exist would be. It is silent:
half the catalog loses its certification on a response that looks completely
successful.

| Kind | Request |
|---|---|
| movie | `GET /movie/{id}?append_to_response=credits,keywords,images,videos,external_ids,release_dates` |
| series | `GET /tv/{id}?append_to_response=credits,keywords,images,videos,external_ids,content_ratings`, then `GET /tv/{id}/season/{n}` per season |

Two facts about that second row, both measured live on 2026-08-01 and both
consequential enough to state here rather than in an adapter docstring:

- **`credits` is a valid TV namespace** (`aggregate_credits` exists
  alongside it and is *also* valid — it is a second view, not a replacement).
- **`append_to_response=season/N` works**, so the per-season requests in
  that row are optional rather than necessary. One request carrying
  `credits,keywords,images,videos,external_ids,content_ratings` plus
  `season/0…season/13` — exactly TMDb's documented 20-item ceiling, which is
  enforced with an HTTP 400 at 21 — returned Game of Thrones' entire
  hierarchy, all 373 episodes across 9 seasons, in place of the ten requests
  the row above costs. A season the series does not have is silently omitted
  rather than erroring, and the appended block is byte-identical to the
  season's own detail response but for a missing top-level `id`, which the
  series' own `seasons[]` summary already carries. **This is recorded and
  not yet taken**: it is a change to this row, to the adapter's `fetch`, and
  to the request-budget arithmetic in [04](04-catalog-bootstrap.md), and it
  belongs in its own change rather than folded into a verification run.

The same divergence runs through the field names (`title`/`name`,
`release_date`/`first_air_date`, `keywords.keywords`/`keywords.results`, a
top-level `imdb_id` against `external_ids.imdb_id`, `runtime` against
`episode_run_time`), the search endpoints (`/search/movie` with
`primary_release_year` against `/search/tv` with `first_air_date_year`), and
the change feeds (`/movie/changes` against `/tv/changes`). All of it stops in
`usher.adapters.tmdb`, whose `mapping.py` tabulates the eight field-level
rows; nothing above the adapter reads a TMDb key.

**M4 populates `Title`, `Season` and `Episode`, and caches the response
verbatim.** `Person`, `Credit`, `Collection` and `Image` are populated by the
milestone that first *reads* them — **`Person`/`Credit` and `Collection`
shipped in M7 as stage 5 below**, `Image` is M9's — each re-derived from the
cached payload in `raw_payloads`
with **no second network call**, which is what
[02](02-data-model.md)'s cache is for
([ADR-0016](decisions/0016-raw-payloads-cache-providers-not-sources.md)). So
`MetadataProvider.to_result()` returns an `EnrichmentResult` carrying the
title, its season/episode hierarchy, and the raw payload; the four deferred
entities are added to it as *fields*, not as a signature change
([ADR-0017](decisions/0017-the-metadata-port-is-an-aggregate-and-a-cursor.md)).

Re-enrichment is driven by TMDb's `/movie/changes` feed rather than blind TTL
sweeps, with a hard re-fetch ceiling under 6 months to respect TMDb's caching
term. The feed is walked through a resumable cursor
(`changed_since(since, cursor) -> ChangedPage`), and a provider may answer a
narrower window than it was asked for — TMDb caps it at 14 days — so an
exhausted feed is not proof that nothing older changed.

**That cap is real, both endpoints of the window are inclusive, and the
clamp sits exactly on the boundary with nothing to spare.** Measured live
2026-08-01: `start_date == end_date` is a valid one-day window returning
4,278 movies, `[d, d+1]` returns 8,155 against 4,278 and 4,373 for the two
days taken separately (so it covers both, deduplicated), `[today-14, today]`
returns 47,945, and `[today-15, today]` is **HTTP 422** —
`"Invalid date range: Should be a range no longer than 14 days."` The
adapter clamps `start` to `today - 14 days`, which is the widest window TMDb
accepts; one day wider and the daily re-enrichment job fails outright.

The tier a title lands on is the pipeline's decision, never the provider's:
`to_result` does not set `enrichment_state`, and `EnrichService` only ever
raises it through `ENRICHMENT_RANK`
([ADR-0008](decisions/0008-enrichment-tier-vs-failure.md)). A failed
enrichment records `Title.enrichment_error` and leaves the tier exactly where
it was.

**A successful enrichment now does one more thing:** after the commit, beside
the `title.updated` publish, it enqueues exactly one `index` job for the title
it just wrote — one job per enriched title, on this stage's hot path. Stage 4
below owns the reasoning for the ordering and the priority.

### 4. Index

Update the search document and compute the embedding
([05](05-search-and-similarity.md)). Both derive from the Title, so this stage
is a pure function of catalog state and can be rebuilt from scratch at any time.

**The two halves are not maintained the same way, and the asymmetry is
deliberate.** The search document is a `GENERATED ALWAYS AS (…) STORED` column
on `titles`, so PostgreSQL recomputes it inside the statement that writes
`name` or `overview` — no job is involved, and a skeleton title is fully
searchable with no queued work at all. The embedding needs a model, which the
database cannot run, so it is a `JobKind.INDEX` job; and because it is queued
it can fail, park, or never be enqueued. `title_embeddings` therefore records
`model_name` and a `source_fingerprint` of the exact text embedded, which makes
staleness a SQL predicate rather than something inferred from the queue. So
this stage's correctness does not depend on the queue being reliable: a title
whose job was lost still matches the predicate, and the backfill still claims
it. That asymmetry is the milestone's central decision and it is argued in
full, with its costs, in
[ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md).

**The last sentence of the paragraph above is still true and now means
something operationally.** "A pure function of catalog state, rebuildable
from scratch at any time" is what lets the backfill be a *predicate* rather
than a cursor over everything: self-draining, idempotent, and re-runnable at
zero write cost, because `enqueue`'s
`WHERE jobs.status <> 'parked' AND jobs.priority < excluded.priority` means a
re-run writes no rows at all.

**A finished enrichment enqueues exactly one `index` job**, after the commit
that writes the title and beside the `title.updated` publish, on the success
path only. *After*, because a worker claiming the job reads `titles` in a
different transaction: enqueued before the commit it can fingerprint and embed
the *pre-enrichment* text and then stop matching the stale predicate, which is
a permanently stale vector produced by the enqueue meant to prevent one. *On
the success path only*, because a failure leaves the tier and the text where
they were, so the job would find the row already current and complete without
embedding — once per attempt of a backoff schedule. It is enqueued at
`BACKFILL` priority: nothing a client renders depends on a search document, so
it must never sit in front of a `match` or a demand-promoted `enrich`.

**The embedded population is the enriched tier, not the catalog.** Enrichment
completion is the producer — not the nightly walk, for the reason stage 1
already gives about enqueueing per item — so the population is
`enrichment_state <> 'skeleton'` (2k–10k titles), for which
`ix_titles_enrichment_state` is already exactly the partial index. Embedding
all 1,271,138 titles would produce a vector of each skeleton's name, which
full-text already does better and cheaper, and would cost 4–6 hours against 25
seconds to 2 minutes. `usher index --backfill` drains anything the queue
missed, keyset-paged and re-runnable at zero write cost.

**No second client event is published on index completion.** `title.updated`
already fires from stage 3 and nothing a client renders depends on the search
document or the embedding, so a `title.indexed` would be an event with no
consumer — which [ports/events.py](../../src/usher/ports/events.py) rules out
by name. Boundary call 5; [09](09-roadmap.md) carries the corrected roadmap
wording.

**What this stage deliberately does *not* do**, each with its reason, because
a stage described only by what it does reads as one that does everything:

- **It did not fill weight class B, and M7 did.** In M6 no `Person`/`Credit`
  table existed anywhere in `src/`, the only place credits physically existed
  was `raw_payloads.payload` — a *provider's* JSON shape, which has no business
  in `services/` — so the class shipped reserved and empty. Boundary call 2.
  **M7 filled it from stage 5's output**, and the sentence M6 wrote about the
  cost ("a migration rather than a rewrite") was true of the search *path* and
  optimistic about everything else: a generated column cannot reach another
  table at all, so class B is a `setweight` over a denormalised
  `titles.credit_names`, and changing the expression forces a full column
  rewrite and re-embeds the whole enriched tier
  ([05](05-search-and-similarity.md)).
- **It does not rebuild the search document.** That would be the obvious
  symmetry and it is wrong: it makes the cheap, always-correct half depend on
  the expensive, fallible half, so a parked embedding job would *also* mean a
  stale full-text document with the two failures indistinguishable.
- **It refuses a degenerate document, and records the refusal.** A title with
  no overview, no genres and no keywords composes to whitespace, and every
  whitespace-only input embeds to the *identical* vector — cosine 1.0000
  exactly — which is a degenerate cluster pinned to the top of every "more
  like this" result rather than a bad result. So the composer refuses; and
  the refusal is **written**, as a row with a `NULL` embedding and the
  fingerprint of the degenerate text, not skipped. A skipped refusal keeps
  matching the stale predicate forever, and the backfill re-claims it every
  pass. **This project has shipped exactly that bug once already** — the
  watch-history repair that was refused by the very row it existed to repair
  and then matched `played AND play_count = 0` permanently — which is why it
  is worth a paragraph in the PRD: it is a *class* of bug this pipeline keeps
  producing.

### 5. Derive — people, credits and collections, with no second network call

✅ **Shipped in M7**, and it is the stage this section previously described
only as a promise: *"`Person`, `Credit` and `Collection` are populated by the
milestone that first reads them"* named no mechanism, no job kind and no
command. All three now come out of `raw_payloads` — `DeriveService`,
`JobKind.DERIVE` and its handler, and `usher derive` on the command line.

**No second network call**, which is [ADR-0016](decisions/0016-raw-payloads-cache-providers-not-sources.md)'s
whole point arriving three milestones after the cache was built, and M4's
boundary call 2 paying off: the payloads the enrichment crawl already fetched
carry `credits`, `created_by` and `belongs_to_collection`, so re-deriving is a
read of a local table. A household that enriched its library last year can
derive today, offline.

**The join back is the trap, and it is a data-integrity one rather than a
performance one.** `raw_payloads` has no `title_id` — it is keyed by the
provider reference that fetched it — so the join is
`(provider='tmdb', kind=title.kind.value, reference=str(title.tmdb_id))`, and
**`tmdb_id` is unique per kind rather than globally**
([ADR-0011](decisions/0011-tmdb-id-is-namespaced-by-kind.md)): 26,968 ids are
live in both spaces, so a derivation keyed on the integer alone attaches a
series' cast to a film, silently, on ids that are all real. The `kind` in that
tuple is the whole of the guard.

**It walks the cache with a keyset cursor on the port, not with a `SELECT` in
a service.** `RawPayloadStore.iterate` is a port method with a fake and a
contract case, because the alternative — a service reaching past the port for
one paged read — is the layering violation contract three forbids, and because
the cache is the one table here whose size is the enriched tier rather than a
batch.

**Two forms, and the bare one is read-only.** `usher derive` reports cached
payloads, titles carrying credits, people and collections; `usher derive
--backfill` re-derives inline rather than enqueueing, because a derivation is
a local read and a queue in front of it buys latency and a second failure
mode. It also maintains `titles.credit_names`, weight class B's denormalised
input, in the same call and the same transaction that writes `credits` — one
writer, so the two
cannot disagree ([05](05-search-and-similarity.md)).

⏳ **`alternative_titles` is not derived, because it is not in the cache**, and
this is named here rather than left implied by the deferral it blocks.
It appears in neither `append_to_response` list above, so aliases are not in
`raw_payloads` at all — landing them changes the crawl's *request shape* and
re-fetches the whole enriched tier, i.e. it is a metadata-provider change
wearing a search table's name. It is the blocker on
[05](05-search-and-similarity.md)'s `title_search_names`, and it is
**unassigned**: M9 owns the people half of that table and nothing owns this
one.

## Watch state

**Canonical in Usher; sources are event streams and write targets.**

- **Inbound:** `UserDataChanged` (push) and the nightly reconcile write
  `WatchState` with `origin = source`. Progress made in Infuse or Emby's own
  apps flows in.

  **`play_count` and `last_played_at` are absent from a walk, not zero**
  ([ADR-0014](decisions/0014-absence-is-not-zero.md)). Verified 2026-07-31
  against Emby 4.9.5.0: a *listing* reports `PlayCount: 0` and omits
  `LastPlayedDate` entirely, for the same item whose single-item fetch
  reports `PlayCount: 2` and a real date. Position and played flag are
  correct in both. No `Fields` value, `EnableUserData`, or `Ids` restriction
  changes it. Recovering the pair costs one request per item and this
  library is 1,126,674 items, so the walk cannot.

  Both fields are therefore `int | None` / `AwareDatetime | None` on
  `SourceWatchState`, where `None` means "this read could not determine it"
  and `0` stays a positive claim — a reset has to remain propagable, which
  is the same reason an all-zero state is emitted rather than filtered out
  of a walk. `SourceAdapter.get_watch_state(external_id)` is the
  authoritative single-item read, and `merge_from_source` is `COALESCE`-
  shaped, so a walk *cannot* write zero over real history rather than merely
  being trusted not to. Recovering it is a queued backfill over
  `played = true AND play_count = 0`, bounded by the household's watched
  items rather than by the library.

  **That backfill's merge is stamped with the instant it read the source,
  never with the walk's.** "Latest `updated_at` wins" applies to the whole
  record, and `watch_states` has a `BEFORE UPDATE` trigger that stamps the
  write instant — so a repair carrying the walk's instant is refused by the
  very row it exists to repair, and the row keeps matching
  `played AND play_count = 0` for good. Measured to terminate otherwise:
  seven rows drained three at a time empty in three passes. A source whose
  *single-item* route also cannot count leaves rows matching indefinitely,
  bounded at one request per row per pass and rotating rather than starving
  (the queue-filling query is oldest-first and a merge moves `updated_at`).

  **An episode's watch state attaches to its `Episode`, never to its
  series' `Title`.** A `MediaItem` for an episode carries both ids and a
  `WatchState` may carry exactly one, so the inbound merge collapses the
  pair with the episode winning. Attaching to the title instead violates no
  constraint and merges every episode of a show onto one row — 999,827
  episodes onto 32,409 series.
- **Outbound:** client actions write `WatchState` with `origin = api`, then
  push to the source best-effort. Failure enqueues a retry and never blocks the
  API response. On Emby that push is **one call, plus a second only when the
  item is being marked played** — and every part of that sentence was settled
  by running it against the live server (Emby 4.9.5.0, 2026-07-31) rather than
  by reading the API:
  - The position goes to `POST /Users/{userId}/Items/{itemId}/UserData` as a
    JSON body. The obvious `POST /Users/{userId}/PlayingItems/{itemId}/Progress`
    answers **400** for every body and parameter set tried, as does
    `POST /Sessions/Playing/Progress`: both are *session-scoped playback
    reporting*, and Usher never plays anything.
  - `Played` is named in that body even when it is not changing, because the
    route deserialises into a DTO whose unset fields take their defaults — a
    body carrying only a position silently flips a played item to unplayed.
  - Marking played is the second call, `POST /Users/{userId}/PlayedItems/{itemId}`,
    and it goes **last**: it is the only route that advances `PlayCount` and
    stamps `LastPlayedDate`, and it clears the resume position as it does so.
    The reverse order leaves a just-finished film resumable at the last
    reported second, which is how it reappears in Continue Watching.
  - Reporting an item *unplayed* does **not** use `DELETE .../PlayedItems`.
    That route resets `PlayCount` to 0, clears `LastPlayedDate`, and clears a
    non-zero resume position — so using it to write a resume position would
    erase the household's play history *and* then discard the position it was
    called to write.

  Both writes are idempotent — marking an already-counted item played leaves
  `PlayCount` where it is rather than incrementing — so the retry after a
  partial failure is safe.
- **Conflicts:** latest `updated_at` wins. With push-based inbound updates the
  window where this matters is seconds.

Because state attaches to the canonical Title, adding a second source later
unifies automatically instead of fragmenting.

## Playback

Usher does not stream. `stream_targets()` returns ranked `StreamTarget`s
describing how to play an item — direct URL, container and codec facts, and
any client-specific deep-link forms the source can produce. Choosing between
them is the client's business; Usher's job is to hand over complete
information.

Because Usher never proxies the bytes, a `direct` target's URL carries the
session token above — the one documented place a credential reaches a client
([ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md), and
[07](07-client-api.md)'s playback section for the client-facing contract).

It carries **three** query parameters, and each was measured against the live
server on 2026-07-31 by removing it and re-requesting: `static=true` (removed →
400), `MediaSourceId`, and `api_key` (removed → 401). It used to carry a fourth,
Usher's own `DeviceId` — removed, because the route answers 206 with real bytes
without it, and sending it made a captured playback URL a drop-in for the push
channel's `api_key`/`deviceId` pair above.

`StreamTarget` also carries `scheme` (for deep links) and `audio` (a single
composite token such as `truehd_atmos_7_1`, which is a different thing from the
raw codec) — the 🔶 that named M3, settled.
