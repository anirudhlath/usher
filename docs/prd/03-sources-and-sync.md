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

| Message | Scope | Use |
|---|---|---|
| `LibraryChanged` | Per-user, payload-filtered | Items added / updated / removed |
| `UserDataChanged` | Own data only | Watch position, played flags |
| `Sessions` (subscribe) | Per-user row-filtered | Playback events |

**No admin privileges are required** — a normal user token works, and there is
no role check in Emby's subscription path. (Note: guidance derived from Jellyfin
is misleading here; Jellyfin added admin gating after forking, so its docs claim
restrictions Emby does not have.)

**Not required is not the same as prevented, and nothing prevents it.**
`POST /admin/sources` ([07](07-client-api.md)) takes whatever account an
operator supplies, and nothing in the adapter inspects its role, so admin
credentials work — and put an admin token into every direct-play URL, which
widens what a captured URL grants from "this user's library and watch state"
to "everything an Emby administrator can do". Configure a normal user. This is
an accepted risk, recorded in
[ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md) along with the
check that would make it observable.

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

> 🔶 **Provisional.** `SourceEvent`, the push channel's own DTO, carries no
> payload beyond `kind` and the affected `external_ids`. That forces a
> `WATCH_STATE_CHANGED` event to re-walk `watch_state(since=...)` to
> discover what actually changed, even though Emby's own `UserDataChanged`
> message already carries the position and played flag. Settle in **M5**,
> when the push lane is built and the cost of re-walking is measurable
> against just carrying the payload through.
>
> **Reviewed in M3, deliberately left alone.** M3 settled the other two 🔶
> markers this section and 07 named for it (`SourceAdapter.verify()`,
> `StreamTarget`) but builds no push lane itself, so the re-walk-cost
> measurement this marker is waiting for is still not available. Still
> M5's to settle.

### Walking the library

`list_items` and `watch_state` page over the source's own listing, one page in
flight at a time. Measured against the live deployment on 2026-07-31, a walk of
it is **1,126,674 items** — 94,438 movies, 32,409 series and 999,827 episodes —
so materialising one is not an option. Three properties the adapter contract
enforces, each now checked against that server:

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

`push_available` is deliberately three-valued, and `null` ("not probed") is what
every adapter reports until M5. See the health-check caveat above: a handshake
against a nonexistent path also upgrades, so an upgrade is not evidence and only
received messages are.

## Reconciliation is not optional

Push is the fast path, never the only path. Sockets drop, events are missed, and
`LibraryChanged` carries no guarantee of delivery.

| Lane | Trigger | Work |
|---|---|---|
| **Push** | WebSocket event | Enqueue affected items at high priority |
| **Reconnect delta** | Socket re-established | Items changed since last cursor |
| **Full reconcile** | Nightly | Walk the source; upsert everything; mark unseen items `available = false` |

Polling is the backstop, not the design.

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

Four idempotent, resumable stages. Any stage can be re-run without duplicating
work.

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

### 2. Match — resolve to a canonical Title

Ordered by confidence, stopping at the first hit:

1. `ProviderIds.Tmdb` from the source → direct `tmdb_id` lookup.
2. `ProviderIds.Imdb` → local lookup against the bootstrapped IMDb skeleton
   ([04](04-catalog-bootstrap.md)) — no network call, because the catalog
   already knows 12.7M titles.
3. Name + year against the local skeleton, accepted above a confidence bar
   (normalised title match, year within ±1).
4. TMDb search API as a last resort.
5. No confident match → `title_id` stays NULL; the item enters the review queue.

Bootstrapping first makes stages 2–3 local, which is why matching is fast and
mostly offline.

### 3. Enrich

One TMDb request per title using
`append_to_response=credits,keywords,images,videos,external_ids,release_dates`,
plus per-season episode fetches for series. Populates `Title`, `Season`,
`Episode`, `Person`, `Credit`, `Collection`, `Image`, and sets
`field_provenance`.

Re-enrichment is driven by TMDb's `/movie/changes` feed rather than blind TTL
sweeps, with a hard re-fetch ceiling under 6 months to respect TMDb's caching
term.

> 🔶 **Provisional.** `MetadataProvider.to_title()` returns a single
> `Title`, but this stage populates `Season`, `Episode`, `Person`,
> `Credit`, `Collection`, and `Image` too — none of which exist as domain
> models yet, so the real return shape (a `Title` plus its aggregate, an
> `EnrichmentResult` bundle, or several methods) would be guesswork today.
> Settle in **M4**, once those models exist.

### 4. Index

Update the search document and compute the embedding
([05](05-search-and-similarity.md)). Both derive from the Title, so this stage
is a pure function of catalog state and can be rebuilt from scratch at any time.

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
