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

- `DeviceId` is generated once and persisted on the `Source` row. Usher appears
  as one device in Emby's dashboard rather than an accumulating pile of sessions.
- The token is cached, and **any 401 triggers silent re-authentication** with
  the stored credentials and the same `DeviceId`. That is the refresh mechanism;
  no human ever pastes a token.
- Credentials live behind `credentials_ref` indirection, never in the database
  as plaintext and never sent to a client.

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

Operational requirements:

- **Heartbeat every 20–25 s.** Emby sends no keepalive of its own. nginx closes
  idle connections at 60 s and Cloudflare at ~100 s, so the client must generate
  traffic.
- **Reconnect with exponential backoff and jitter**, then run a delta reconcile
  on reconnect to recover anything missed while disconnected.
- **Fall back cleanly.** Reverse proxies that don't forward `Upgrade` return 404
  instead of 101. If the socket can't be established, the adapter reports
  `supports_push = false` and the reconciler covers the gap.

> **Open item.** A probe of this deployment's Emby returned 404 on
> `/embywebsocket`. That is inconclusive — the probe was not a complete
> handshake and used an expired token — but it is the documented signature of a
> proxy stripping upgrade headers. **Must be retested with a live token before
> the push path is assumed available.** Fallbacks, in order: Emby per-user
> webhooks (needs the server operator to enable "Notifications" under the
> account's Feature Access — a single checkbox, and Emby Premiere must be
> active on the server), then polling.

## Reconciliation is not optional

Push is the fast path, never the only path. Sockets drop, events are missed, and
`LibraryChanged` carries no guarantee of delivery.

| Lane | Trigger | Work |
|---|---|---|
| **Push** | WebSocket event | Enqueue affected items at high priority |
| **Reconnect delta** | Socket re-established | Items changed since last cursor |
| **Full reconcile** | Nightly | Walk the source; upsert everything; mark unseen items `available = false` |

Polling is the backstop, not the design.

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

Normalise the source item; store the raw payload in `raw_payloads`; upsert
`MediaItem` on `(source_id, external_id)`; create or attach a `Title` stub.

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

### 4. Index

Update the search document and compute the embedding
([05](05-search-and-similarity.md)). Both derive from the Title, so this stage
is a pure function of catalog state and can be rebuilt from scratch at any time.

## Watch state

**Canonical in Usher; sources are event streams and write targets.**

- **Inbound:** `UserDataChanged` (push) and the nightly reconcile write
  `WatchState` with `updated_by = source`. Progress made in Infuse or Emby's own
  apps flows in.
- **Outbound:** client actions write `WatchState` with `updated_by = api`, then
  push to the source best-effort. Failure enqueues a retry and never blocks the
  API response.
- **Conflicts:** latest `updated_at` wins. With push-based inbound updates the
  window where this matters is seconds.

Because state attaches to the canonical Title, adding a second source later
unifies automatically instead of fragmenting.

## Playback

Usher does not stream. `stream_url()` returns a `StreamTarget` describing how to
play an item — direct URL, container and codec facts, and any client-specific
deep-link forms the source can produce. Choosing between them is the client's
business; Usher's job is to hand over complete information.
