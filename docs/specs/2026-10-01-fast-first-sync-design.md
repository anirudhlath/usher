# Fast, resumable first sync: design

**Status:** Proposed.
**Date:** 2026-10-01.
**Follow-up:** a second spec covers on-demand fetch, what the console shows
while a source is still syncing, and starting a new source's first sync
automatically. This one is the engine those build on.

## What the owner asked for

A first sync of a large library took long enough to make Usher unusable. The
owner wants both of these, not one or the other:

1. **Reachable within a minute or two.** Continue watching and next up are right
   almost at once, the movies and series arrive in minutes, and the rest fills
   in behind.
2. **Complete within about an hour, even at a million items.** Several requests
   may be in flight at once against the source, which relaxes today's
   one-at-a-time politeness. Backoff protects a server that struggles.

Two assumptions were stated and not corrected. First, the source may be a
server the operator does not administer, so the default request rate stays
where it is. Second, a typical library is far smaller than the one measured
here, so this one is the stress case.

## The problem, measured

The measured source is "Shared Emby": 95,672 movies, 33,408 series and
1,027,950 episodes, for **1,157,030 items** in 15 libraries (nine movie
libraries, five TV libraries, one home-video library). The largest TV library
holds 606,412 items.

**A first sync is two whole-library walks.** The item walk and the watch walk
both page `GET /Users/{id}/Items` through the adapter's one `_walk`. That walk
uses `SortBy=DateCreated,SortName` and `StartIndex` offsets, with
`Limit=200` (`USHER_SOURCE_PAGE_SIZE`) and `EnableTotalRecordCount=true` on
every page.

**Offset paging slows with depth.** One page of 200 with the count took
4.8 s at `StartIndex` 0, 12.0 s at 500,000 and 19.6 s at 1,000,000. The
production run that started 2026-10-01 17:20 UTC tells the same story:

- it walked about 35 items/s in its first hour and about 22 items/s past
  780,000;
- it reached 780,800 items after 8.5 h and 907,000 after 10.2 h;
- pages near 780,000 exceeded the 30 s read timeout and were retried.

That projects to about 14 h for the item walk. The watch walk would repeat it,
even though this account's whole watch state is **970 items** (763 played,
207 in progress). A single `Filters=IsPlayed` request returned all 763 in 6 s.

**What the probes found:**

| Probe | Result |
|---|---|
| 1,000 items vs 200 per page, at the head | 5.6–6.4 s vs 5.7–6.0 s: the bigger page is nearly free |
| 3 pages at once vs one after another | 6.1 s wall vs 17.5 s: Emby overlaps requests |
| Movies and series only (`Movie,Series`), 120,000 deep | 2.3 s for 1,000 items |
| A page 545,000 deep inside one library | 3.9 s; that library's first page, cold, took 16.9 s |
| `EnableImages=false` with `EnableUserData=false` | 13.9 s vs about 6 s: slower, so not used |
| `AnyProviderIdEquals` with 10 IMDb ids | all 10 in one request (used by the follow-up spec) |
| One title by IMDb id; one series' episodes | 0.8–3.8 s; 0.2–3.2 s |

Every probe ran while that production sync was hammering the same server, so a
single number can be off by several seconds and cache swings are large. The
defaults below are therefore re-measured cleanly before they are fixed (see
*Measurement and acceptance*).

## Targets

These hold on Shared Emby at default settings: 0.4 requests/s, four walkers and
1,000-item pages.

| What | Target |
|---|---|
| Watch state, plus the series and next-up episodes behind it | within 2 minutes |
| Every movie and series | within 10 minutes |
| The whole library | within 75 minutes |
| A failure part-way | resumes where it stopped instead of restarting |

`USHER_SOURCE_REQUESTS_PER_SECOND` spaces request **starts**, so it caps
overlap only indirectly. At 0.4 requests/s with 1,000 items a page, the
ceiling is about 400 items/s, which puts a 1.16M-item walk at roughly 48
minutes at best. The setting raises that ceiling, but its default does not
move.

## Phase 1: tune the single walk

The first PR is a small one. Everything in it is inside the Emby adapter and the
watch-lane service, and it speeds up the next sync of every deployment.

### 1.1 Pages of 1,000

The `USHER_SOURCE_PAGE_SIZE` default goes from 200 to 1,000. The existing cap of
1,000 stays.

### 1.2 The total, once

`EnableTotalRecordCount=true` goes on a walk's first page only; every later page
sends `false`. The walk ends:

- on an empty page; or
- on a page shorter than requested **and** `start ≥` the first page's total.

The second condition is what keeps a server that silently caps `Limit` from
ending a walk early: its short pages arrive while `start` is still below the
total. A library that grew during the walk is still read to its end, because
its pages stay full past the stale total.

### 1.3 Overlapping pages

After the first page, a page is requested at `StartIndex = max(0, cursor −
PAGE_OVERLAP)`, where `cursor` is the index just past the last item received
and `PAGE_OVERLAP` is 50. Items the previous page already yielded, matched by
external id, are dropped. The new cursor is the request's `StartIndex` plus the
items returned.

This matters because a deletion behind the cursor shifts every later item left
by one. Without overlap, the next page then skips one item per deletion. A
skipped item is neither ingested nor refreshed, so the sweep marks a file that
is still there unavailable.

If the previous page's last id is missing from the new page, the shift was
larger than the overlap. The walk logs a WARNING naming the `StartIndex` and
carries on. That damage cannot be repaired inside the walk, and the next full
walk covers it.

### 1.4 Read-ahead

As soon as page N arrives, the walk requests page N+1, then yields page N's
items while that request is in flight. That is one page of read-ahead, and the
outstanding request is cancelled when the consumer stops (`aclose`). The gate
still spaces the request.

### 1.5 A listing timeout

Listing pages get a 120 s read timeout through httpx's per-request `timeout=`,
and every other call keeps the adapter's 30 s. A timed-out request does not
stop the server's query, so retrying a slow page early just adds a second copy
of the slowest query there is. `failure_detail` already reports the budget a
request ran under.

### 1.6 The first watch walk asks only for what was watched

With `since=None`, `watch_state` makes two walks:

- `Filters=IsPlayed`;
- then `Filters=IsResumable`, skipping ids the first walk already yielded.

Each walk is paged as in 1.1–1.4. With a `since`, nothing changes: it remains
the `MinDateLastSavedForUser` delta.

The port contract changes to match. With no `since`, the method yields every
**non-default** state, meaning played or holding a resume position, and nothing
else. An item it does not yield is not asserted unplayed. The service already
merges only what it is given, so a default state that is never yielded changes
nothing.

**An incomplete first walk is not resumed.** If `latest_incomplete_run` returns
a run whose `cursor_at` is `None`, the service closes it `failed` with
`error = "superseded: a first watch walk restarts"` and starts a fresh run at
position 0. This is load-bearing in two ways:

- A run left part-way through an old-style first walk carries a position such
  as 300,000. That would skip the whole filtered walk, which is a few hundred
  states long, and complete it having merged nothing.
- `SyncRunRepository.save` only ever raises `position` (`GREATEST`), so
  resetting the old row in place is impossible.

A delta still resumes as it does today (#41).

**Phase 1's effect on Shared Emby:** the item walk takes about 3–5 h instead of
about 14 h, and the first watch walk takes two or three requests instead of
about 14 h.

## Phase 2: the unit walk

### 2.1 The port

```python
class WalkStage(StrEnum):
    SEED = "seed"          # what the account is watching
    TITLES = "titles"      # movies and series
    EPISODES = "episodes"

@dataclass(frozen=True, slots=True)
class WalkUnit:
    key: str               # opaque and adapter-owned
    stage: WalkStage
    label: str             # for logs and the CLI only
    expected_items: int | None

@dataclass(frozen=True, slots=True)
class WalkPlan:
    units: tuple[WalkUnit, ...]
    expected_total: int | None

class SourceAdapter(ABC):
    async def plan_walk(self) -> WalkPlan: ...
    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncIterator[SourceItem]: ...
```

Both methods are concrete on the ABC. By default the plan is a single unit,
`all`, whose `list_unit` is today's `list_items(None)`, so an adapter without
units behaves exactly as now.

The contract: every unit outside SEED, taken together, yields at least every
item `list_items(None)` yields. Units may overlap, because ingest is an
idempotent upsert on `(source_id, external_id)`. A SEED unit may repeat items
that other units also yield.

**Unit keys are opaque strings that the adapter owns.** They are persisted above
the adapter the same way `MediaItem.external_id` is, and never appear in an API
response.

### 2.2 Emby's plan

**SEED.** One unit. It first fetches three things, each with full
`ITEM_FIELDS`: the `Filters=IsPlayed` items, the `Filters=IsResumable` items, and
the account's `/Shows/NextUp` episodes. It then fetches the series all of those
belong to, by `Ids`. It yields the series first, so every episode after them
finds its series.

**Per library.** Libraries come from `/Users/{id}/Views`. Views of type
`boxsets` and `playlists` are skipped, because they only point at items held in
libraries. **Every library gets both kinds of unit**, so a movie library holding
an episode is still covered. An EPISODES unit of a movie library costs one empty
page.

- **One TITLES unit:** `ParentId=<view>`, `IncludeItemTypes=Movie,Series`.
- **EPISODES units:** `ParentId=<view>`, `IncludeItemTypes=Episode`, split into
  `StartIndex` chunks of `USHER_SYNC_UNIT_MAX_ITEMS` (default 100,000).
  - **Sizing:** the number of chunks comes from the library's total. The last
    chunk has no upper bound and runs to the end under 1.2's rule, so a library
    that grows is still covered.
  - **Boundaries:** every other chunk reads `PAGE_OVERLAP` items past its end,
    so a shift at a chunk boundary is covered the same way 1.3 covers one
    between pages.

**Counts and coverage.** The plan makes one `Limit=0` count per library, over
the walked item types, plus one count over the whole source. That is 16 requests
on Shared Emby, sent concurrently through the same gate. A unit learns its own
exact size from the count on its first page (1.2). If the library counts sum to
less than the source total, the plan falls back to the default single unit,
which is Phase 1's walk, and logs both numbers. (On Shared Emby the libraries
summed to 1,164,898 against a total of 1,157,030, but the two counts were taken
hours apart.)

**Timing at the gate.** One views request, 16 counts and four seed requests are
21 request starts. At 0.4 requests/s that is about 53 s, so the seed and the
watch lane after it fit inside the 2-minute target.

Within a stage, the largest unit goes first.

### 2.3 Which walks use the plan

A **whole-library walk** uses the plan: a full walk, or a delta that has no
cursor yet. The admin trigger defaults to `delta`, so a console-triggered first
sync is exactly that second case.

A delta with a cursor keeps Phase 1's single walk, and so does the gap-closer,
whose `max_items` ceiling applies to such deltas only. **Only a full walk
sweeps**, as today.

### 2.4 Walkers and one writer

`ReconcileService` starts `USHER_SYNC_WALKERS` walker tasks (default 4).

- **Walkers fetch.** Each claims the next pending unit, in stage order and then
  by size. It iterates `list_unit(key, start_index=unit.position)` and puts each
  page into an `asyncio.Queue(maxsize=walkers)` as `(unit_key, items,
  cursor)`. Walkers never touch the database.
- **The reconcile task is the only writer.** For each page it:
  1. runs `ingest_batch(..., observed_at=run.started_at)`;
  2. advances that unit's `position` and `items_seen` and the run's counters;
  3. saves both;
  4. commits;
  5. publishes `sync.progress`.

  This is today's single session with a commit per batch. If the database falls
  behind, the bounded queue makes the walkers wait.
- **The stage barrier.** No EPISODES unit is claimed until every TITLES unit has
  committed complete. That way `resolve_series_titles` finds every series.
  Without the barrier, each early episode stays UNMATCHED and costs a re-match
  job, which is the ingest rule today.
- **The watch lane runs after the seed.** When the SEED stage has committed, the
  writer awaits an optional `after_seed` hook. `usher sync` and the worker's
  `sync_handler` pass one that runs `WatchStateSyncService.sync`, now a small
  first walk or a delta. Both callers still run the watch lane after the walk,
  as today, and that run is now a cheap delta.
- **When a unit is done.** A unit is complete when its iterator has ended and
  its last page has committed.

### 2.5 Persistence, resume and the heartbeat

**The migration** adds two things:

- a table `sync_run_units` with columns `run_id` (FK to `sync_runs`,
  `ON DELETE CASCADE`), `unit_key`, `stage`, `label`, `position ≥ 0`,
  `expected_items` (nullable), `items_seen ≥ 0` and `status` (`pending`,
  `running`, `completed` or `failed`), keyed on `(run_id, unit_key)`;
- a nullable `sync_runs.heartbeat_at timestamptz`.

A unit's save follows `SyncRunRepository.save`'s two rules: `position` only
rises, and `completed` cannot be undone.

**A whole-library walk starts** by reading `latest_incomplete_run` for its kind:

| Newest incomplete run | What happens |
|---|---|
| none | a fresh run; the plan is made, and its units are persisted in the same commit as the run's first heartbeat |
| has units, and `heartbeat_at` is null or older than 10 minutes | resumed in place: same row, same `started_at`, no new plan. Units that are `pending`, `running` or `failed` continue from their committed positions; `completed` units are skipped |
| has units, `running`, heartbeat within 10 minutes | refused: "a whole-library walk of `<source>` is already running". `usher sync` exits non-zero; a worker job fails and is retried |
| a **full** run with no units, i.e. it predates this change (2026-09-24's on Shared Emby) | closed `failed` as superseded, then a fresh run |
| a **delta** run with no units, i.e. an ordinary cursored delta | left alone, and a fresh run starts |

**The heartbeat.** The writer sets `heartbeat_at` on every commit, and at least
once a minute while it waits on the queue. A page under retry can take about 8
minutes, so without that the run would look dead while it is merely waiting.

### 2.6 Backoff

The adapter caps listing requests in flight at the walker count, with one limiter
shared by every walker and by every read-ahead (1.4). The cap is passed in at
construction, the same way the page size is. Any failure that `_page` retries,
an outage or a 429, drops the limit to 1. Each run of 10 successful pages in a
row raises it by 1, back up to the walker count. Walkers waiting on the limiter
hold no request. With `USHER_SYNC_WALKERS=1` the cap is 1, so even Phase 1's
read-ahead waits its turn.

A unit whose page gives up, or fails a way that is never retried, fails the run.
The writer cancels the walkers and records the run `failed` through the
existing failure path. Every unit keeps its position, and the failing unit is
marked `failed`. The next whole-library walk resumes only what is left.

### 2.7 The sweep

The sweep runs only when every unit has completed and the plan passed its
coverage check. A fallback plan is Phase 1's single walk, and its rules don't
change. The sweep compares against `run.started_at`, which survives resumes, so
an item seen in any attempt was written with that same instant and is spared.

### 2.8 Visibility

**The event.** `sync.progress` gains:

- `stage`;
- `units_done` and `units_total`;
- `items_expected`, the sum of the units' counts, or null when unknown.

**The CLI and the API.** `usher sync-status` shows the same fields, and so does
the latest run in `GET /admin/sources/{id}/status`. That second change is
additive to PRD 07's contract.

**Telemetry.** Two new instruments:

- `usher.sync.unit.duration`, a histogram labelled by source and stage;
- `usher.source.listing.concurrency`, a gauge of the listing limiter's current
  cap labelled by source, so backoff shows up on the dashboards.

## Testing

Everything is TDD. Every guard is planted false and seen to fail on its own line
before it counts.

**The fake server.**

- `FakeEmbyServer` learns views, `ParentId`, `Filters=IsPlayed` and
  `Filters=IsResumable`, `Ids`, `/Shows/NextUp`, `EnableTotalRecordCount=false`,
  a capped `Limit`, and deletions between pages.
- Every new behaviour is recorded first from the real server into
  `tests/fixtures/emby/`, redacted, with a row in that README. The fake has
  mirrored the adapter's own guess before (the write-back route), and this
  prevents it from doing so again.

**Phase 1.**

- `EnableTotalRecordCount` is `true` on the first page only, asserted on the
  wire.
- A short page ends the walk, but a capped `Limit` does not.
- A deletion between pages causes no skip, and a shift past the overlap logs the
  WARNING.
- Read-ahead is shown by observed overlap: the next page's request interval
  intersects the consumer's handling of the current page.
- Listing requests carry a 120 s budget, and every other call carries 30 s.
- The first watch walk yields played plus in-progress items, each once, and
  never a default state.
- An incomplete cursorless watch run is superseded rather than resumed. A delta
  still resumes.

**Phase 2.**

- **Contract:** the non-SEED units together cover exactly what `list_items()`
  yields, including an item in two libraries. The ABC default is one unit equal
  to `list_items()`.
- **Order:**
  - SEED, then the `after_seed` hook, then TITLES, then EPISODES;
  - no EPISODES page is fetched before the last TITLES unit commits;
  - every case asserts its own ordering premise.
- **Concurrency:** walkers' fetch intervals are recorded and asserted to
  intersect, not counted.
- **Resume:**
  - a crash after N committed pages resumes on the same row with the same
    `started_at`, each unit from its position, with nothing counted twice;
  - a unit's `completed` cannot be undone;
  - its `position` never moves back.
- **The heartbeat:** a live one refuses a second walk, and a stale one resumes.
  A legacy run without units is superseded.
- **Backoff:** a retryable failure drops in-flight requests to one, and ten good
  pages raise the limit by one.
- **The sweep:** it never runs before every unit completes, and always compares
  against the run's original `started_at`. A fallback plan sweeps under the
  single walk's existing rule.
- **A cursorless delta** runs the plan and does not sweep.

## Measurement and acceptance

These are live runs, read-only against the source, made with a throwaway script
outside the repository that writes no token, user id or host. They start only
after the current production sync has finished. Results go on the PR as raw
output, not summaries.

1. **Before the defaults are fixed:**
   - page cost inside libraries at the head, the middle and the deep end;
   - pages per second with 1, 2 and 4 walkers;
   - the count against no count.

   These numbers set `USHER_SYNC_UNIT_MAX_ITEMS` and `USHER_SYNC_WALKERS`. If
   deep pages inside a library stay above about 10 s, the planner's split moves
   from offset chunks to name ranges (`NameStartsWithOrGreater` with
   `NameLessThan`). This spec does not adopt name ranges unmeasured.
2. **The writer:** `scripts/measure_ingest.py` must show it sustains 400 items/s,
   the ceiling the gate allows. If it can't, the writer is fixed before any
   target is claimed.
3. **Acceptance on Shared Emby at default settings:**
   - **Phase 1:** a first watch walk under 1 minute and an item walk of at most
     6 h.
   - **Phase 2:** the 2-, 10- and 75-minute targets above, plus a resume after
     the process is killed mid-EPISODES.

## Rollout

**Two PRs, Phase 1 first.** Each updates, in the same commit as the behaviour it
changes:

- PRD 02 (the new table and column);
- PRD 03 (the walk, units, resume and the first watch walk);
- PRD 07 (the progress fields);
- PRD 08 (the settings);
- PRD 10 (the instruments);
- `CHANGELOG.md`, `.env.example`, `docs/guide/configuration.md`, the console's
  `Config.settings.ts` catalogue and `.claude/rules/emby-push-and-ingest.md`.

The new plan gets rows in `docs/plans/progress.md` and in `docs/prd/README.md`.

**Settings.**

- `USHER_SOURCE_PAGE_SIZE`: default 1,000, up from 200.
- `USHER_SYNC_WALKERS`: default 4. Setting 1 restores one request at a time.
- `USHER_SYNC_UNIT_MAX_ITEMS`: default 100,000.

**Migration.** Phase 2 only, and purely additive. As with every migration, the
previous image refuses the newer schema, so a rollback goes through the
pre-deploy backup.

**The production handover.** The running sync was started on the old image. Its
item walk finishes on that code, sweep included, and then its old whole-library
watch walk begins. Once Phase 1 is deployed:

1. Stop that container mid-watch-walk. Its watch run is left `running`.
2. The next `usher sync` supersedes that run, because it has no cursor, and runs
   the filtered first walk in seconds.

**Never deploy Phase 2 under a full walk that an old-image process is still
running.** That run has no units and no heartbeat, so the next whole-library
walk would supersede it while the old process is still walking.
`docker ps --filter name=usher-prod-sync` is the check.

## Out of scope

These belong to the follow-up spec:

- fetching an opened title or series from the source on demand, at priority 100;
- what the console shows while a source's first walk is incomplete, where an
  unseen title is *unknown* rather than *not owned*;
- starting a new source's first sync automatically;
- a gate that lets on-demand requests go ahead of walk pages.

## Honest caveats

- **Offset chunks inside a big library still pay that library's depth.** On
  Shared Emby the deepest is 606,000. That is why the split strategy is
  measured before it is fixed.
- **The ordering assumes `DateCreated,SortName` is stable.** Emby can take
  `DateCreated` from a file's creation time, so a newly added item can land
  behind a cursor. This walk then misses it, and the next delta reads it
  (`MinDateLastSaved`).
- **A plan is fixed for one logical run.** A library added mid-run is walked by
  the next whole-library walk. An item deleted from the source during a run
  that spans several attempts is retracted by the next full walk, not this one.
- **Concurrency multiplies load on a server the operator may not administer.**
  The protection is backoff plus a default rate that does not move, and
  `USHER_SYNC_WALKERS=1` turns it off.
