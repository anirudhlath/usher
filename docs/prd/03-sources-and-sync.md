# 03 — Sources and synchronisation

How media servers get into the catalog, stay current, and stop being visible
anywhere above the adapter boundary.

## The Emby adapter

### Durable client authentication

Emby access tokens are per-device session tokens with no OAuth2 refresh flow,
so Usher authenticates as a **named, stable device**:

```
Authorization: MediaBrowser Client="Usher", Device="<source name>",
               DeviceId="<persisted UUID>", Version="<app version>"
POST /Users/AuthenticateByName  {"Username": ..., "Pw": ...}
→ AccessToken, User.Id
```

Authenticated requests carry the same identity header plus the session token in
`X-Emby-Token`.

- `DeviceId` is generated once and persisted on the `Source` row, so Usher
  appears as one device in Emby's dashboard rather than an accumulating pile of
  sessions.
- The token is cached in memory for the lifetime of the adapter, never written
  to the database, and **any 401 triggers silent re-authentication** with the
  stored credentials and the same `DeviceId`. No human ever pastes a token.
  There is no TTL and no proactive rotation. Re-authentication is
  single-flight, exactly one retry is attempted per request, and a credential
  that is genuinely wrong is remembered for a cooldown.
- Credentials live behind `credentials_ref` indirection: an opaque, random
  token addressing a row in `source_credentials`, encrypted at rest under a key
  derived from `USHER_SECRET_KEY` ([08](08-operations.md)). Neither the
  username, the password nor the ref is ever returned by any endpoint,
  including admin. The ref is random rather than derived from the source id so
  that rotation is expressible. **The session token minted from them is the one
  documented exception**: it reaches a client inside a `direct` playback
  target's URL.
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
| `Sessions` (subscribe) | Per-user row-filtered | **Nothing is derived from it. Its whole value is that it arrives.** | Nothing. Counted before it is parsed |

A received frame is evidence the socket is alive whatever it says, which is why
`Sessions` is counted before it is parsed: on an idle library it is the only
thing keeping `push_available` true. `push_stale_after_seconds` defaults to
90 s, and `usher.source.push.reconnects` is how a household where that is too
tight becomes visible rather than silent.

**No admin privileges are required** — a normal user token works. Not required
is not prevented: `POST /admin/sources` ([07](07-client-api.md)) takes whatever
account an operator supplies, and admin credentials put an admin token into
every direct-play URL and into the push socket. Configure a normal user.
`verify()` reads the account's role and `GET /admin/sources/{id}/status`
reports `is_administrator` — three-valued, `null` meaning the check did not
run. An administrator account logs a warning and is served anyway.

`api_key` and `deviceId` are exactly what a `direct` `StreamTarget`'s query
holds, so anything holding one of those URLs holds what this channel is opened
with.

Operational requirements:

- **Heartbeat every 20–25 s.** Emby sends no keepalive of its own, and proxies
  close idle connections.
- **Reconnect with exponential backoff and jitter**, then run a delta reconcile
  on reconnect to recover anything missed while disconnected.
- **Fall back cleanly.** A reverse proxy that does not forward `Upgrade`
  returns 404 instead of 101. If the socket cannot be established the adapter
  reports `supports_push = false` and the reconciler covers the gap.

**A successful upgrade proves nothing** — a handshake against a nonexistent
path also upgrades and receives `Sessions`, and so does a socket with no
credential at all. Push health is asserted on *received messages*.

A `WATCH_STATE_CHANGED` carries the states the upstream's own message already
contained, keyed by `external_id`; an id it could not parse falls back to
`get_watch_state`, one authoritative request. The two lists may differ in
length and are never aligned by position — a state names its own item, and one
the event did not list is refused at construction. A carried state's
`play_count`/`last_played_at` are reported `None` on Emby, and the
`watch_history` backfill recovers the pair from the single-item route.

### Walking the library

`list_items` and `watch_state` page over the source's own listing, one page in
flight at a time. Three properties the adapter contract enforces:

- **A stable ascending sort by creation date, with a tiebreak.**
  `SortBy=DateCreated,SortName`. Items added during a walk land at the end, so
  an insertion cannot shift an unread item backwards past a consumed page
  boundary. A deletion mid-walk can still shift one item out of view, which the
  nightly full reconcile covers — the contract permits duplicates and forbids
  silent truncation.
- **The delta cursor is widened by one second.** `since` is contractually
  inclusive, and a superset is explicitly allowed because callers deduplicate
  by `external_id`.
- **An unrecognised filter degrades to a full walk, never to an empty result.**

Two different filters are sent, because a library edit and a watch-state change
do not touch the same timestamp: `MinDateLastSaved` for `list_items`,
`MinDateLastSavedForUser` for `watch_state`.

### Health and status

`verify()` returns a `SourceStatus`, not a bool: `GET /admin/sources/{id}/status`
([07](07-client-api.md)) reports bad credentials, unreachable, and
reachable-but-push-blocked as separate states. The unauthenticated
`/System/Info/Public` probe separates the first two and carries the `Version`
that becomes `server_version`. `verify()` also spends one request on the
account's own role.

`push_available` is three-valued and is filled from a message ledger rather
than from a probe: `verify()` opens no socket at all. It reports `null` ("not
probed") for an adapter that has never had a channel, and the live answer — a
connection *and* at least one received message *and* a recent one — for the
adapter a push lane is running.

The on-demand answer is `usher push --probe`, which opens a channel on purpose
and reports **what arrived** rather than that the handshake succeeded.

**`supports_push` and `events()` are related one way only.** An adapter
reporting `true` must offer a channel, and one with no channel must report
`false` — but an adapter that *has* a channel reports `false` from the moment
it opens until the first message arrives.

## Reconciliation is not optional

Push is the fast path, never the only path. Sockets drop, events are missed,
and `LibraryChanged` carries no guarantee of delivery.

| Lane | Trigger | Work |
|---|---|---|
| **Push** | WebSocket event | Apply it inline when it is small; defer to a delta when it is large. A `WATCH_STATE_CHANGED` carrying its own payload merges with no request at all; one naming more than `push_max_items_per_event` items with no payload becomes a delta walk |
| **Reconnect delta** | Socket re-established, or a push event deferred to a delta | Items changed since the last cursor. Bounded by `USHER_PUSH_GAP_MAX_ITEMS`, and **refused outright when there is no cursor** under `USHER_PUSH_GAP_CLOSE`'s `cursored` default |
| **Full reconcile** | Nightly, or an operator's `POST /admin/sources/{id}/sync` | Walk the source; upsert everything; mark unseen items `available = false` |

Polling is the backstop, not the design.

**A triggered sync is a queued job, never a walk run inside the request.** The
route enqueues `JobKind.SYNC` — the item lane then the watch lane, over one
adapter closed in a `finally` — and its 202 promises the row is queued and
nothing about how soon it runs ([08](08-operations.md)).

**A delta with no cursor is a walk of the whole library**, so the gap-closer
refuses it. `USHER_PUSH_GAP_CLOSE` is where the operator says which behaviour
they want: `cursored` (default) refuses, `always` walks, `never` turns
gap-closing walks off entirely. The refusal logs a WARNING naming the source
and `usher sync`, keeps the socket up, and covers the deferral trigger too, so
an oversized event on a cursorless source is discarded until the sync runs.
`usher sync --kind delta` on such a source still walks: the refusal is the
lane's, not `ReconcileService`'s. A bulk-bootstrapped deployment has a
populated catalog and no `sync_runs` row, so it is refused until an operator
syncs.

**A bounded walk records `FAILED`, never `COMPLETED`.**
`USHER_PUSH_GAP_MAX_ITEMS` (default **20,000**; 0 is unlimited) stops a
gap-closing delta that does have a cursor, with `error_code =
'gap_delta_ceiling'`. `latest_completed_cursor` reads `started_at` of the
newest *completed* run, so a truncated run recorded `COMPLETED` would advance
the cursor past everything it never reached. Nothing the bounded walk saw is
lost — `_flush` commits per batch — and the WARNING names
`usher sync --kind full`. **The ceiling bounds the item lane only**: the watch
lane owns its own cursor and still walks whole.

**A push `ITEM_REMOVED` retracts nothing.** An Emby library refresh emits
`ItemsRemoved` for items that have not gone anywhere. The event is counted and
logged; the row stays available until a walk sweeps it. Emby does not
re-deliver what a disconnected client missed, so the gap-closing delta is the
only cover there is.

**Retraction is a separate step, and it can decline.** Marking unseen items
unavailable is a distinct call the reconciler makes only after a walk returns
normally, and even then it refuses to retract more than
`sync_max_retract_fraction` (default `0.25`) of a source in one run, raising
and changing nothing. `1.0` disables the ceiling. The ceiling is a fraction of
what *Usher* holds, not of the source, so a catalogue that has ingested a
fraction of its source measures that source's churn against its own
incompleteness.

An item that reappears in a walk is available again at that moment: appearing
in a walk *is* the evidence of availability. The sweep only ever sets `false`.

**A source is either a library this deployment owns or a *view* of somebody
else's, and Usher does not model the difference.** Pointing Usher at a server
somebody else runs is an intended deployment: the retraction ceiling assumes
removals are the operator's to authorise, and the watch write-back writes play
state to an account on a server the operator does not administer.

**Only a full walk sweeps.** A delta walk returns only what changed, so by
construction nearly everything is unseen and a sweep after one would retract
the library. The gate is the success path, not the ceiling.

**A delta walk resumes from the newest run of *either* item lane that
completed**, because full and delta both walk `list_items` and differ only in
whether a `since` is passed. Only *completed* runs count. A full walk ignores
every cursor. `watch_state` is a third lane with its own cursor.

**The watch lane is resumable.** A run checkpoints its committed `StartIndex`
on `sync_runs.position`, and the next attempt reclaims that same row — its id,
its `cursor_at` and its `started_at` — and resumes there, so a transient
failure costs the page in flight rather than the whole walk. `position` only
advances and `completed` is absorbing. The item lanes leave `position` at 0.
🔶 The measured deployment has still completed no `watch_state` run, so its
`since` cursor is `None` and the first full walk is owed; nothing schedules
one.

**Each batch is committed with the run's counters**, and a `sync_runs` row an
operator can watch exists before the walk starts rather than after it finishes.

## Read-through with a priority queue

The catalog is usable immediately and improves under you.

**1. Stub-on-sight.** Ingest creates a `Title` in `stub` state from the
source's own metadata the moment an item is seen. It is queryable, browsable
and playable before any enrichment happens.

**2. A real priority queue.** Enrichment is a Postgres-backed `jobs` table
(`SELECT … FOR UPDATE SKIP LOCKED`), ordered by priority then age.

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
create or attach a `Title` stub.

**Source payloads are not stored.** `raw_payloads` caches *provider* responses
only.

The adapter emits movies, series **and episodes** — `SourceItem` carries
`series_external_id`, `season_number` and `episode_number`.

**An episode is attached to its series' `Title`, and the attachment spans
pages.** The series map is built from the whole page *and* from a batched read
of what earlier pages stored. An episode whose series is not yet known is
stored unmatched and enqueued for a re-match; it is never dropped and never
attached to a guess. An episode with no season or episode number is left
unmatched for the same reason.

**Enrichment is enqueued only for the titles that need it**, decided by one
batched read of the page's enrichment tiers compared through `ENRICHMENT_RANK`.

**Every repository call in this stage is once per batch**, including the season
and episode writes.

### 2. Match — resolve to a canonical Title

Ordered by confidence, stopping at the first hit. Every lookup is issued once
per batch, not once per item.

1. `ProviderIds.Tmdb` → `(tmdb_id, kind)` lookup. The kind is not optional.
2. `ProviderIds.Imdb` → local lookup against the bootstrapped IMDb skeleton
   ([04](04-catalog-bootstrap.md)), no network call. One global namespace.
3. `ProviderIds.Tvdb` → `tvdb_id` lookup. Emby series routinely carry a TVDb id
   and no TMDb one.
4. Name + year against the local skeleton, accepted above a confidence bar
   (normalised title match, year within ±1) **and only when unambiguous**. A
   probe carrying no year resolves nothing, by construction.
5. A trusted provider id the catalog does not hold → **create a stub**. An id
   from TMDb, IMDb or TVDb is an identity claim strong enough to build a
   canonical title on; a bare name is not. Every id is filtered to the shape
   `Title` accepts before the constructor sees it, because a source reports
   malformed ones.
6. No confident match → `title_id` stays NULL; the item enters the review
   queue, and a `match` job is enqueued at `BACKFILL` priority.

**The TMDb search tier is queued, not inline**, so a walk's duration is not a
function of TMDb's rate limit. A year-filtered TMDb search that finds nothing
is automatically re-asked without the year, because TMDb's year filter is exact
where this ladder's is ±1.

**Episodes never walk this ladder.** An Emby episode payload carries the
episode's own provider ids rather than its series', TVDb numbers episodes and
series in overlapping namespaces, and no episode's IMDb id is in the catalog at
all. An episode is resolved by attaching it to its series' `Title` during
ingest.

Bootstrapping first makes stages 2–3 local, which is why matching is fast and
mostly offline.

### 3. Enrich

One TMDb request per title — for a series that one request carries the whole
season hierarchy — and sets `field_provenance`.

| Kind | Request |
|---|---|
| movie | `GET /movie/{id}?append_to_response=credits,keywords,images,videos,external_ids,release_dates` |
| series | `GET /tv/{id}?append_to_response=credits,keywords,images,videos,external_ids,content_ratings,season/0,…,season/13` — one request, seasons and episodes included; listed seasons outside that window cost `ceil(n/20)` follow-ups, so a series costs `1 + ceil(n/20)` requests |

**The `append_to_response` list is not the same for both id spaces.**
`release_dates` is movie-only and `content_ratings` is the TV-only equivalent;
asking either half for the other's namespace answers `200` with the key simply
absent, so one shared list silently loses half the catalog's certification.

TMDb's documented ceiling is 20 appended items (21 is a 400). A season the
series does not have is silently omitted, which is what makes the blind window
legal; the window is reconciled against the `seasons[]` summary the same
response carries, and any listed number it missed is fetched by a follow-up. An
appended block is byte-identical to the season's own detail response but for a
missing top-level `id`, and no `season/N` key survives into `raw_payloads`.

The same divergence runs through the field names (`title`/`name`,
`release_date`/`first_air_date`, `keywords.keywords`/`keywords.results`, a
top-level `imdb_id` against `external_ids.imdb_id`, `runtime` against
`episode_run_time`), the search endpoints and the change feeds. All of it stops
in `usher.adapters.tmdb`; nothing above the adapter reads a TMDb key.

Enrichment populates `Title`, `Season` and `Episode` and caches the response
verbatim. `Person`, `Credit`, `Collection` and `Image` are derived from that
cached payload by stage 5, with no second network call.

Re-enrichment is driven by TMDb's `/movie/changes` feed rather than blind TTL
sweeps, with a hard re-fetch ceiling under 6 months to respect TMDb's caching
term. The feed is walked through a resumable cursor
(`changed_since(since, cursor) -> ChangedPage`), and a provider may answer a
narrower window than it was asked for — **TMDb caps it at 14 days**, both
endpoints inclusive, and 15 is an HTTP 422 — so an exhausted feed is not proof
that nothing older changed. The adapter clamps `start` to `today - 14 days`.

The tier a title lands on is the pipeline's decision, never the provider's:
`to_result` does not set `enrichment_state`, and `EnrichService` only ever
raises it through `ENRICHMENT_RANK`. A failed enrichment records
`Title.enrichment_error` and leaves the tier where it was.

**`genres` is the one replaced field where the merge asks a second question.**
Every field in `_ENRICHABLE` is replaced wholesale when the provider supplies
it; for a set that deleted labels naming concepts the provider has no word for.
`MetadataProvider.genre_vocabulary` names the canonical concepts a provider can
express — the set it is entitled to delete — and a label outside it survives. A
label the provider *could* have said and did not is still overwritten.

⚠️ **Titles enriched before that rule keep their deletions, and `usher genres
--backfill` does not repair them**: it normalises spellings, and a deleted
label is not a spelling. Restoring one is a bootstrap-shaped operation.

**A successful enrichment enqueues exactly one `index` job** for the title it
just wrote, after the commit and beside the `title.updated` publish.

### 4. Index

Update the search document and compute the embedding
([05](05-search-and-similarity.md)). Both derive from the Title, so this stage
is a pure function of catalog state and can be rebuilt from scratch at any
time.

**The two halves are not maintained the same way.** The search document is a
`GENERATED ALWAYS AS (…) STORED` column on `titles`, so PostgreSQL recomputes
it inside the statement that writes `name` or `overview` — no job is involved,
and a skeleton title is fully searchable with no queued work at all. The
embedding needs a model, so it is a `JobKind.INDEX` job that can fail, park or
never be enqueued; `title_embeddings` records `model_name` and a
`source_fingerprint` of the exact text embedded, which makes staleness a SQL
predicate rather than something inferred from the queue.

The `index` job is enqueued **after** the enrichment commit, **on the success
path only**, at `BACKFILL` priority: nothing a client renders depends on a
search document, so it must never sit in front of a `match` or a
demand-promoted `enrich`.

**The embedded population is the enriched tier, not the catalog**
(`enrichment_state <> 'skeleton'`). `usher index --backfill` drains anything
the queue missed, keyset-paged and re-runnable at zero write cost.

**No second client event is published on index completion.** `title.updated`
already fires from stage 3 and nothing a client renders depends on the search
document or the embedding.

What this stage deliberately does *not* do:

- **It does not rebuild the search document.** That would make the cheap,
  always-correct half depend on the expensive, fallible half, so a parked
  embedding job would also mean a stale full-text document with the two
  failures indistinguishable.
- **It refuses a degenerate document, and records the refusal.** A title with
  no overview, no genres and no keywords composes to whitespace, and every
  whitespace-only input embeds to the identical vector — a degenerate cluster
  pinned to the top of every "more like this" result. The refusal is written,
  as a row with a `NULL` embedding and the fingerprint of the degenerate text,
  not skipped: a skipped refusal keeps matching the stale predicate forever.

### 5. Derive — people, credits, collections and artwork, with no second network call

`DeriveService`, `JobKind.DERIVE` and `usher derive` produce `Person`,
`Credit`, `Collection` and `Image` from `raw_payloads`. A household that
enriched its library last year can derive today, offline; derived image rows
carry a provider *path*, and fetching the bytes is the serve-time proxy's job.

⚠️ **Expect a small `images written` against a large cache.** `images` is one
of the `append_to_response` namespaces, so only payloads fetched with it derive
a full set — but `poster_path` and `backdrop_path` are top-level detail fields,
so every cached payload derives the two references the artwork consumers
render. A per-kind cap keeps a popular film's hundred posters from becoming a
hundred rows.

**The join back is keyed on the kind as well as the id.** `raw_payloads` has no
`title_id`, so the join is
`(provider='tmdb', kind=title.kind.value, reference=str(title.tmdb_id))`; a
derivation keyed on the integer alone attaches a series' cast to a film,
silently, on ids that are all real.

`RawPayloadStore.iterate` walks the cache with a keyset cursor on the port.

**Two forms, and the bare one is read-only.** `usher derive` reports cached
payloads, titles carrying credits, people and collections; `usher derive
--backfill` re-derives inline rather than enqueueing. It also maintains
`titles.credit_names` in the same transaction that writes `credits`, so the two
cannot disagree.

**`credit_names` has a second source, and the two partition the catalog.**
IMDb's `title.principals` joined to `name.basics` fills the column with no API
call at all, and `BulkCatalogRepository.fill_credit_names` writes only where
`enrichment_state = 'skeleton'` — so a title TMDb has enriched is TMDb's,
permanently and in both directions.

**No `people` and no `credits` row is bulk-loaded from IMDb.** `credits`' only
unique key is `tmdb_credit_id`, NULL on every IMDb row, so the table cannot
deduplicate an IMDb load. Merging people across the two sources is possible but
costs one `GET /person/{id}/external_ids` per person.

**`alternative_titles` is not derived**, because it appears in neither
`append_to_response` list and adding it would re-fetch the whole enriched tier.
Aliases come from IMDb's `title.akas` instead, with no API call:
`BulkCatalogRepository.replace_aliases` fills
[05](05-search-and-similarity.md)'s `title_search_names` from it, with `region`
and `language`.

## Watch state

**Canonical in Usher; sources are event streams and write targets.**

- **Inbound:** `UserDataChanged` (push) and the nightly reconcile write
  `WatchState` with `origin = source`. Progress made in Infuse or Emby's own
  apps flows in.

  **`play_count` and `last_played_at` are absent from a walk, not zero.** A
  listing reports `PlayCount: 0` and omits `LastPlayedDate` for the same item
  whose single-item fetch reports a real count and date; position and played
  flag are correct in both. Both fields are therefore `int | None` /
  `AwareDatetime | None` on `SourceWatchState`, where `None` means "this read
  could not determine it" and `0` stays a positive claim, because a reset has
  to remain propagable. `SourceAdapter.get_watch_state(external_id)` is the
  authoritative single-item read, and `merge_from_source` is `COALESCE`-shaped,
  so a walk cannot write zero over real history. Recovering the pair is a
  queued backfill over `played = true AND play_count = 0`, bounded by the
  household's watched items rather than by the library.

  **That backfill's merge is stamped with the instant it read the source, never
  with the walk's.** Latest `updated_at` wins for the whole record, and
  `watch_states` has a `BEFORE UPDATE` trigger, so a repair carrying the walk's
  instant would be refused by the very row it exists to repair.

  **An episode's watch state attaches to its `Episode`, never to its series'
  `Title`.** A `MediaItem` for an episode carries both ids and a `WatchState`
  may carry exactly one, so the inbound merge collapses the pair with the
  episode winning.
- **Outbound:** client actions write `WatchState` with `origin = api`, then
  push to the source best-effort. Failure enqueues a retry and never blocks the
  API response. On Emby that push is one call, plus a second only when the item
  is being marked played:
  - The position goes to `POST /Users/{userId}/Items/{itemId}/UserData` as a
    JSON body. The session-scoped playback-reporting routes answer 400, and
    Usher never plays anything.
  - `Played` is named in that body even when it is not changing, because the
    route deserialises into a DTO whose unset fields take their defaults — a
    body carrying only a position silently flips a played item to unplayed.
  - Marking played is the second call,
    `POST /Users/{userId}/PlayedItems/{itemId}`, and it goes **last**: it is
    the only route that advances `PlayCount` and stamps `LastPlayedDate`, and
    it clears the resume position as it does so. The reverse order leaves a
    just-finished film resumable at the last reported second.
  - Reporting an item unplayed does **not** use `DELETE .../PlayedItems`. That
    route resets `PlayCount`, clears `LastPlayedDate` and clears a non-zero
    resume position.

  Both writes are idempotent, so the retry after a partial failure is safe.
- **Conflicts:** latest `updated_at` wins.

Because state attaches to the canonical Title, adding a second source later
unifies automatically instead of fragmenting.

## Playback

Usher does not stream. `stream_targets()` returns ranked `StreamTarget`s
describing how to play an item — direct URL, container and codec facts, and any
client-specific deep-link forms the source can produce. Choosing between them
is the client's business.

Because Usher never proxies the bytes, a `direct` target's URL carries the
session token above — the one documented place a credential reaches a client
([07](07-client-api.md) has the client-facing contract).

It carries **three** query parameters: `static=true`, `MediaSourceId` and
`api_key`. It deliberately does not carry Usher's own `DeviceId`, which would
make a captured playback URL a drop-in for the push channel's credentials.

`StreamTarget` also carries `scheme` (for deep links) and `audio` (a single
composite token such as `truehd_atmos_7_1`, which is a different thing from the
raw codec).
