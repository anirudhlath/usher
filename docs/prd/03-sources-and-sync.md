# 03 — Sources and synchronisation

How media servers get into the catalog, stay current, and stop being visible
anywhere above the adapter boundary.

## The Emby adapter

### Durable client authentication

Usher authenticates to Emby as a **named, stable device**:

```
Authorization: MediaBrowser Client="Usher", Device="<source name>",
               DeviceId="<persisted UUID>", Version="<app version>"
POST /Users/AuthenticateByName  {"Username": ..., "Pw": ...}
→ AccessToken, User.Id
```

Authenticated requests carry the same identity header plus the session token in
`X-Emby-Token`.

- `DeviceId` is generated once and persisted on the `Source` row, so Usher
  appears as one device in Emby's dashboard.
- The token is cached in memory for the lifetime of the adapter, never written
  to the database, and **any 401 triggers silent re-authentication** with the
  stored credentials and the same `DeviceId`. No human ever pastes a token.
  There is no TTL and no proactive rotation. Exactly one retry is attempted per
  request, and a credential that is genuinely wrong is remembered for a
  cooldown.
- Credentials live behind `credentials_ref` indirection: an opaque, random
  token addressing a row in `source_credentials`, encrypted at rest under a key
  derived from `USHER_SECRET_KEY` ([08](08-operations.md)). Neither the
  username, the password nor the ref is ever returned by any endpoint,
  including admin. **The session token minted from them is the one documented
  exception**: it reaches a client inside a `direct` playback target's URL.
- **Outbound calls to one source are paced.** A minimum-interval gate spaces
  every outbound call at least `1/rate` seconds apart, where `rate` is
  `USHER_SOURCE_REQUESTS_PER_SECOND` (default **0.4**; 0 disables it). It is
  one gate per source per process, shared by the push lane, the worker lane and
  every request; a second process is a second gate, spending `2 × rate`. The
  seconds spent waiting are the `usher.source.throttle.wait` histogram
  ([10](10-telemetry-and-dashboards.md)).

### Push events

Emby exposes a WebSocket at `/embywebsocket?api_key=<token>&deviceId=<id>`.

| Message | Scope | Use | Becomes |
|---|---|---|---|
| `LibraryChanged` | Per-user, payload-filtered | Items added / updated / removed | One `SourceEvent` per non-empty array: `ITEM_ADDED`, `ITEM_UPDATED`, `ITEM_REMOVED`. A removal **retracts nothing** |
| `UserDataChanged` | Own data only | Watch position, played flags | One `WATCH_STATE_CHANGED` carrying the ids *and* the states the message itself contained |
| `Sessions` (subscribe) | Per-user row-filtered | Liveness only | Nothing. Counted before it is parsed |

Any received frame counts as liveness; on an idle library `Sessions` is the
only thing keeping `push_available` true. `push_stale_after_seconds` defaults to
90 s, and `usher.source.push.reconnects` shows a household where that is too
tight.

**No admin privileges are required** — a normal user token works. Not required
is not prevented: `POST /admin/sources` ([07](07-client-api.md)) takes whatever
account an operator supplies, and admin credentials put an admin token into
every direct-play URL and into the push socket. Configure a normal user.
`GET /admin/sources/{id}/status` reports `is_administrator` — three-valued,
`null` meaning the check did not run. An administrator account logs a warning
and is served anyway.

A `direct` `StreamTarget`'s URL carries the same `api_key` this channel is
opened with, so anything holding one of those URLs can open it.

Operational requirements:

- **Heartbeat every 20–25 s.**
- **Reconnect with exponential backoff and jitter**, then run a delta reconcile
  on reconnect to recover anything missed while disconnected.
- **Fall back cleanly.** A reverse proxy that does not forward `Upgrade`
  returns 404 instead of 101. If the socket cannot be established the adapter
  reports `supports_push = false` and the reconciler covers the gap.

**Push health is judged on received messages**, never on a successful upgrade.

A `WATCH_STATE_CHANGED` applies the states the upstream message carried; an
item whose state it could not read is fetched with one request. Pushed states
carry no `play_count`/`last_played_at` on Emby; the `watch_history` backfill
recovers them.

### Walking the library

`list_items` and `watch_state` page over the source's own listing, one page in
flight at a time:

- **Items are walked in ascending creation order**, so items added during a
  walk land at the end. A deletion mid-walk can shift one item out of view,
  which the next full reconcile covers. Duplicates are permitted; silent
  truncation is not.
- **The delta cursor is widened by one second.**
- **An unrecognised filter degrades to a full walk, never to an empty result.**

The item lane filters on the library edit time, the watch lane on the user-data
change time.

### Health and status

`GET /admin/sources/{id}/status` ([07](07-client-api.md)) reports bad
credentials, unreachable, and reachable-but-push-blocked as separate states,
and the server's version as `server_version`.

`push_available` is three-valued: `null` ("not probed") when no push lane is
running for the source, otherwise the live answer — a connection *and* at least
one received message *and* a recent one. The status check opens no socket.

The on-demand answer is `usher push --probe`, which opens a channel on purpose
and reports **what arrived** rather than that the handshake succeeded.

An adapter that has a channel reports `supports_push = false` from the moment
it opens until the first message arrives.

## Reconciliation is not optional

Push is the fast path, never the only path. Sockets drop, events are missed,
and `LibraryChanged` carries no guarantee of delivery.

| Lane | Trigger | Work |
|---|---|---|
| **Push** | WebSocket event | Apply it inline when it is small; defer to a delta when it is large. A `WATCH_STATE_CHANGED` carrying its own payload merges with no request at all; one naming more than `push_max_items_per_event` items with no payload becomes a delta walk |
| **Reconnect delta** | Socket re-established, or a push event deferred to a delta | Items changed since the last cursor. Bounded by `USHER_PUSH_GAP_MAX_ITEMS`, and **refused outright when there is no cursor** under `USHER_PUSH_GAP_CLOSE`'s `cursored` default |
| **Full reconcile** | An operator's `POST /admin/sources/{id}/sync` or `usher sync`. Nothing in Usher schedules it; run it nightly from cron | Walk the source; upsert everything; mark unseen items `available = false` |

**A triggered sync is a queued job, never a walk run inside the request.** The
route enqueues `JobKind.SYNC` — the item lane then the watch lane — and its 202
promises the row is queued and nothing about how soon it runs
([08](08-operations.md)).

**A delta with no cursor is a walk of the whole library**, so the gap-closer
refuses it. `USHER_PUSH_GAP_CLOSE` is where the operator says which behaviour
they want: `cursored` (default) refuses, `always` walks, `never` turns
gap-closing walks off entirely. The refusal logs a WARNING naming the source
and `usher sync`, keeps the socket up, and covers the deferral trigger too, so
an oversized event on a cursorless source is discarded until the sync runs.
`usher sync --kind delta` on such a source still walks. A bulk-bootstrapped
deployment has a populated catalog and no `sync_runs` row, so it is refused
until an operator syncs.

**A bounded walk records `FAILED`, never `COMPLETED`.**
`USHER_PUSH_GAP_MAX_ITEMS` (default **20,000**; 0 is unlimited) stops a
gap-closing delta that does have a cursor, with `error_code =
'gap_delta_ceiling'`. Nothing the bounded walk saw is lost, and the WARNING
names `usher sync --kind full`. **The ceiling bounds the item lane only**: the
watch lane owns its own cursor and still walks whole.

**A push `ITEM_REMOVED` retracts nothing.** The event is counted and logged;
the row stays available until a walk sweeps it.

**Retraction is a separate step, and it can decline.** Marking unseen items
unavailable happens only after a walk returns normally, and even then it
refuses to retract more than `sync_max_retract_fraction` (default `0.25`) of a
source in one run, raising and changing nothing. `1.0` disables the ceiling.
The ceiling is a fraction of what *Usher* holds for that source, not of the
source itself.

An item that reappears in a walk is available again at that moment. The sweep
only ever sets `false`.

**A source is either a library this deployment owns or a *view* of somebody
else's, and Usher does not model the difference.** Pointing Usher at a server
somebody else runs is supported, with two consequences: the retraction ceiling
treats removals as the operator's to authorise, and the watch write-back writes
play state to an account on a server the operator does not administer.

**Only a full walk sweeps.**

**A delta walk resumes from the newest completed run of *either* item lane**
(full or delta). A full walk ignores every cursor. `watch_state` is a third lane
with its own cursor.

**The watch lane is resumable.** A run checkpoints its position on
`sync_runs.position`, and the next attempt reclaims that same row and resumes
there, so a transient failure costs the page in flight rather than the whole
walk. 🔶 Until a source has completed one `watch_state` run, its watch lane has
no cursor and its next run walks the whole library; nothing schedules that
first walk.

**Each batch is committed with the run's counters**, and a `sync_runs` row an
operator can watch exists before the walk starts rather than after it finishes.

## Read-through with a priority queue

The catalog is usable immediately and improves under you.

**1. Stub-on-sight.** Ingest creates a `Title` in `stub` state from the
source's own metadata the moment an item is seen. It is queryable, browsable
and playable before any enrichment happens.

**2. A real priority queue.** Enrichment is a Postgres-backed `jobs` table,
ordered by priority then age.

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
work, and the fifth makes **no network request at all**.

### 1. Ingest

Normalise the source item; upsert `MediaItem` on `(source_id, external_id)`;
create or attach a `Title` stub. Movies, series **and episodes** are ingested.

**Source payloads are not stored.** `raw_payloads` caches *provider* responses
only.

**An episode is attached to its series' `Title`, even when the series arrived
on an earlier page.** An episode whose series is not yet known is stored
unmatched and enqueued for a re-match; it is never dropped and never attached
to a guess. An episode with no season or episode number is left unmatched.

**Enrichment is enqueued only for the titles that need it.**

### 2. Match — resolve to a canonical Title

Ordered by confidence, stopping at the first hit.

1. `ProviderIds.Tmdb` → `(tmdb_id, kind)` lookup. The kind is not optional.
2. `ProviderIds.Imdb` → local lookup against the bootstrapped IMDb skeleton
   ([04](04-catalog-bootstrap.md)), no network call. One global namespace.
3. `ProviderIds.Tvdb` → `tvdb_id` lookup.
4. Name + year against the local skeleton, accepted above a confidence bar
   (normalised title match, year within ±1) **and only when unambiguous**. An
   item with no year never matches on name.
5. A trusted provider id (TMDb, IMDb or TVDb) the catalog does not hold →
   **create a stub**. A bare name never creates one. A malformed provider id is
   ignored rather than failing the walk.
6. No confident match → `title_id` stays NULL; the item enters the review
   queue, and a `match` job is enqueued at `BACKFILL` priority.

**The TMDb search tier is queued, not inline.** A year-filtered TMDb search
that finds nothing is re-asked without the year.

**Episodes never walk this ladder.** An episode is resolved by attaching it to
its series' `Title` during ingest.

With a bootstrapped catalog, matching is mostly local and offline.

### 3. Enrich

One TMDb request per title — for a series that one request carries the whole
season hierarchy — and sets `field_provenance`.

| Kind | Request |
|---|---|
| movie | `GET /movie/{id}?append_to_response=credits,keywords,images,videos,external_ids,release_dates` |
| series | `GET /tv/{id}?append_to_response=credits,keywords,images,videos,external_ids,content_ratings,season/0,…,season/13` — one request, seasons and episodes included; listed seasons outside that window cost `ceil(n/20)` follow-ups, so a series costs `1 + ceil(n/20)` requests |

Enrichment populates `Title`, `Season` and `Episode` and caches the response
verbatim. `Person`, `Credit`, `Collection` and `Image` are derived from that
cached payload by stage 5, with no second network call. Nothing above the TMDb
adapter reads a TMDb field name.

Re-enrichment is driven by TMDb's `/movie/changes` feed rather than blind TTL
sweeps, with a hard re-fetch ceiling under 6 months to respect TMDb's caching
term. The feed reaches back at most **14 days**, so a change older than that is
picked up only by the 6-month re-fetch.

Enrichment only ever raises a title's tier. A failed enrichment records
`Title.enrichment_error` and leaves the tier where it was.

**Enrichment replaces each field the provider supplies, except `genres`**: a
genre label naming a concept the provider has no word for survives; a label the
provider *could* have said and did not is overwritten.

⚠️ **Titles enriched before that rule keep their deleted genre labels, and
`usher genres --backfill` does not restore them** — it only normalises
spellings. Restoring one is a bootstrap-shaped operation.

**A successful enrichment enqueues exactly one `index` job** for the title it
just wrote.

### 4. Index

Update the search document and compute the embedding
([05](05-search-and-similarity.md)). Both derive from the Title and can be
rebuilt from scratch at any time.

**The two halves are not maintained the same way.** PostgreSQL recomputes the
search document whenever `name` or `overview` is written — no job is involved,
and a skeleton title is fully searchable with no queued work at all. The
embedding is a `JobKind.INDEX` job that can fail, park or never be enqueued;
`title_embeddings` records `model_name` and a fingerprint of the exact text
embedded, so a stale embedding is detectable in SQL.

The `index` job is enqueued **after** the enrichment commit, **on the success
path only**, at `BACKFILL` priority — or at the rung its `enrich` job was
claimed at, when that is `VISIBLE` or above ([01](01-architecture.md)).

**The embedded population is the enriched tier, not the catalog**
(`enrichment_state <> 'skeleton'`). `usher index --backfill` drains anything
the queue missed, keyset-paged and re-runnable at zero write cost.

**No second client event is published on index completion.**

- **A failed embedding never leaves the search document stale**; the index
  stage does not touch it.
- **A title with no overview, no genres and no keywords is not embedded.** The
  refusal is recorded as a row with a `NULL` embedding and the fingerprint of
  the text, and is re-tried when that text changes.

### 5. Derive — people, credits, collections and artwork, with no second network call

`DeriveService`, `JobKind.DERIVE` and `usher derive` produce `Person`,
`Credit`, `Collection` and `Image` from `raw_payloads`, offline. Derived image
rows carry a provider *path*; the image proxy fetches the bytes on first
request.

⚠️ **Expect a small `images written` against a large cache.** Only payloads
fetched with the `images` namespace derive a full set; every cached payload
derives at least its poster and backdrop references. A per-kind cap limits the
rows kept per title.

**Two forms, and the bare one is read-only.** `usher derive` reports cached
payloads, titles carrying credits, people and collections; `usher derive
--backfill` re-derives inline rather than enqueueing. It also maintains
`titles.credit_names` in the same transaction that writes `credits`, so the two
cannot disagree.

**`credit_names` has a second source, and the two partition the catalog.**
For a `skeleton` title, IMDb's `title.principals` joined to `name.basics` fills
the column with no API call. Once TMDb has enriched a title, its credit names
are TMDb's.

**No `people` and no `credits` row is bulk-loaded from IMDb.**

**`alternative_titles` is not derived.** Aliases come from IMDb's `title.akas`
instead, with no API call, filling [05](05-search-and-similarity.md)'s
`title_search_names` with `region` and `language`.

## Watch state

**Canonical in Usher; sources are event streams and write targets.**

- **Inbound:** `UserDataChanged` (push) and the full reconcile write
  `WatchState` with `origin = source`. Progress made in Infuse or Emby's own
  apps flows in.

  **`play_count` and `last_played_at` are unknown after a walk, not zero.** An
  Emby listing reports `PlayCount: 0` and no `LastPlayedDate` even for a
  watched item, so a walk records both as unknown and never writes zero over
  real history; `0` from a single-item read stays a positive claim, so a reset
  still propagates. The pair is
  recovered by a queued backfill over `played = true AND play_count = 0`, one
  single-item read per watched item — bounded by the household's watched items
  rather than by the library.

  **An episode's watch state attaches to its `Episode`, never to its series'
  `Title`.**
- **Outbound:** client actions write `WatchState` with `origin = api`, then
  push to the source best-effort. Failure enqueues a retry and never blocks the
  API response. On Emby that push is one call, plus a second only when the item
  is being marked played:
  - The position goes to `POST /Users/{userId}/Items/{itemId}/UserData` as a
    JSON body, always naming `Played` so that a position update never unplays
    a played item.
  - Marking played is the second call,
    `POST /Users/{userId}/PlayedItems/{itemId}`, and it goes **last**: it
    advances `PlayCount`, stamps `LastPlayedDate` and clears the resume
    position.
  - Reporting an item unplayed does **not** use `DELETE .../PlayedItems`, which
    would also reset `PlayCount`, `LastPlayedDate` and the resume position.

  Both writes are idempotent, so the retry after a partial failure is safe.
- **Conflicts:** latest `updated_at` wins.

Watch state attaches to the canonical Title, so adding a second source later
unifies it instead of fragmenting it.

## Playback

Usher does not stream. `stream_targets()` returns ranked `StreamTarget`s
describing how to play an item — direct URL, container and codec facts, and any
client-specific deep-link forms the source can produce. Choosing between them
is the client's business.

A `direct` target's URL carries the session token above — the one documented
place a credential reaches a client ([07](07-client-api.md) has the
client-facing contract).

It carries **three** query parameters: `static=true`, `MediaSourceId` and
`api_key`. It does not carry Usher's own `DeviceId`.

`StreamTarget` also carries `scheme` (for deep links) and `audio` (a single
composite token such as `truehd_atmos_7_1`, which is a different thing from the
raw codec).
