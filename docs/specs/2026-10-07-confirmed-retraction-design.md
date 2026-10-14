# Confirmed retraction: design

**Status:** Proposed.
**Date:** 2026-10-07.
**Follow-up:** slice 2, a presence pass after every push reconnect and nightly,
reuses this slice's confirmation to catch removals made while the push channel
was down.

## What the owner asked for

After the fast first sync's acceptance, the owner asked for an A+ in every graded
aspect of the sync, and named the goal behind it:

> The main thing is that I want it to automatically stay in sync with the
> server. Really I wanted a pub sub type mechanism.

The push lane already subscribes. What it does not do is remove anything.
The owner chose this slice first, and with it two rules:

1. **Usher retracts only what the source confirms gone.** Both the full walk's
   sweep and a push removal ask the source before marking anything unavailable.
2. **A removal the source confirms is applied at once, with no ceiling.** Usher
   mirrors the server, so a library removed on Emby disappears from Usher within
   seconds. The retraction ceiling keeps guarding walks.

The ruling of 2026-10-05 still governs: "the data and state should stay correct."

## The problem, measured

**A full walk's sweep retracts what the walk did not see, and a walk does not
see everything Emby serves.** Emby's full listing shows one item per version
group. A delta's `MinDateLastSaved` listing, and a push event's read by id, also
return alternate versions. So deltas and push events store alternate-version
rows that no full walk ever lists, and every full walk retracts them.

- **Walk 1** of the acceptance (2026-10-07) retracted 485 rows. 247 were files
  since replaced, and 238 were alternate versions. In a sample of 83 of those
  rows, 40 were still served when this design was written.
- **Walk 2** retracted 744. The 120 with no seen sibling were all gone (404). A
  sample of 45 of the other 624 found replaced files, plus three alternate
  versions still served.

**A push removal retracts nothing.** PRD 03 says so, because Emby sends
`ItemsRemoved` during an ordinary library refresh for items that have not gone
anywhere. Production's push lane received 63 removal events in the 24 hours
sampled on 2026-10-07. Nothing schedules a full walk, so on production a
removal reaches the catalog only when somebody runs one.

## What the probe found

A read-only probe on 2026-10-07 asked Shared Emby about the 83 sampled rows and
timed large id-only pages:

```
A: sample=83 listed=36 unlisted=47 of which gone=43 served=4
A: unlisted#0..3: listed-alone=True versions=6 a-version-in-same-request=True a-version-listed-there=True pair[alt=False other=True]
B: start=        0 limit=  1000 lean  items=1000  secs=3.17
B: start=        0 limit= 10000 lean  items=10000 secs=4.57
B: start=  1100000 limit=  1000 lean  items=1000  secs=6.96
B: start=  1100000 limit= 10000 lean  items=10000 secs=7.54
```

- **An `Ids` listing returns one member of a version group per request.** The
  four served rows the listing missed each had another version in the same
  request. Asked alone, each came back. Asked beside one other version, only the
  other came back.
- **Every row the listing never returned was gone.** All 43 failed a direct
  `GET` by id.
- **A page of 10,000 ids costs about what a page of 1,000 does.** That is slice
  2's finding: the whole library's ids take about 118 requests.

## Design

### The port

```python
class SourceAdapter(ABC):
    @abstractmethod
    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        """The subset of `external_ids` this source no longer serves."""
```

The contract:

- **It never returns an id the source still serves**, where "serves" means the
  account can read the item by its id.
- **It raises a `UsherPortError` when it cannot decide.** A partial answer is
  never returned as a whole one.
- The result is a subset of the input. An empty input returns an empty set
  without a request.

It is abstract. A default that returned nothing would quietly switch retraction
off for a new adapter. A default built on `get_item` would read an item the
mapper cannot model as gone.

### Emby's answer

The answer comes from `Ids` listings of `IDS_PER_REQUEST` (100) ids with
`Limit` set to the chunk's length, through `_page`, so the existing retries
apply. Only ids that were asked for count, as in `_series_for`. The telemetry
op is `confirm`.

**It asks in rounds.** The first round asks for every id. Each later round asks
again for the ids no earlier round listed. The rounds stop when one lists none
of the ids it asked for, and whatever remains is gone.

That stop is sound because of the probe's finding. A round lists at least one
member of every version group that has a served member among the ids it asked
for. So a round that lists nothing proves none of the remaining ids is served.
The number of rounds is at most the largest version group among the ids, plus
one.

**Cost.** Each round costs one request per 100 ids still asked. Walk 2's 744
candidates come to a few dozen requests at most. A push removal of a few items
costs a request or two.

### The sweep

`ReconcileService._sweep` still runs for full walks only, and only after the
walk has returned normally. A fallback single walk sweeps through the same
code. The sweep becomes:

1. **Count and read the candidates**: the source's available rows whose
   `last_seen_at` is before `run.started_at`.
2. **Apply the ceiling first.** More candidates than `sync_max_retract_fraction`
   (default `0.25`) of the source's rows raises `AvailabilitySweepRefused`
   before the source is asked anything. The run fails with `error_code =
   'availability_ceiling'` and is not resumed, as today.
3. **Ask `confirm_gone`.** The heartbeat beats every `heartbeat_seconds` while
   it waits. The helper that beats while a plan is made is extracted from
   `_walk_plan`, and both callers share it.
4. **Retract the gone rows.** Each is set `available = false`, still guarded by
   `available AND last_seen_at < run.started_at`. That spares a row any read
   has seen since the walk began, such as a push event's.
5. **Stamp the rest.** Rows Emby still serves get `last_seen_at =
   run.started_at`, under the same guard. They were served during the walk,
   just not listed, so PRD 03's "an item keeps the latest instant it was seen
   at" stays true.
6. **Record the run.** `items_retracted` counts the rows retracted, and
   `usher.sync.retraction.fraction` records the share under `swept`, as today.

Steps 4–6 commit with the run's `COMPLETED` row, in one transaction. The sweep
publishes no `title.updated`, just as the rest of a walk publishes none.

**If the source cannot answer**, nothing is retracted or stamped. The run fails
with that error, which is not `availability_ceiling`. The next whole-library
walk therefore resumes it in place, finds every unit complete, and runs only
the sweep.

### Push removals

`PushApplyService._ignore_removal` becomes an applier:

1. Take `asked_at = now()`.
2. **Narrow** the event's ids to those Usher holds available for that source.
   Ids it does not hold, such as folders, seasons, extras and items never
   stored, are dropped without asking.
3. **Ask `confirm_gone`** for the rest.
4. **Retract** the gone rows, guarded by `available AND last_seen_at <
   asked_at`.
5. **Commit**, then publish one `title.updated` with `fields: ["availability"]`
   per distinct title or episode retracted, exactly as an add does. An
   unmatched row publishes nothing.

It has no ceiling, per the owner's rule. It has no `push_max_items_per_event`
cap either, because the lookups are batched: a whole library's removal is
applied inline, at one request per 100 ids per round, before the lane reads
its next event.

**If the source cannot answer**, the error propagates like any failed read in
the lane. The lane counts a failure, reconnects and runs its gap-closing delta.
The removal waits for the next full walk's sweep, or for slice 2's presence
pass after that reconnect.

### The repository

`MediaItemRepository.mark_unseen_unavailable` is replaced by four methods:

| Method | Does |
|---|---|
| `list_unseen(source_id, *, seen_since, max_retract_fraction)` | The source's row count and its available rows unseen since `seen_since`. Raises `AvailabilitySweepRefused` over the ceiling |
| `available_among(source_id, external_ids)` | Which of these ids the source holds available |
| `retract(source_id, external_ids, *, unseen_since)` | Sets `available = false` where available and `last_seen_at < unseen_since`. Returns how many rows changed and the distinct titles and episodes they belong to |
| `mark_seen(source_id, external_ids, *, seen_at)` | Moves `last_seen_at` forward to `seen_at` on available rows. Never moves it back, and never restores a row |

Each is one set-based statement, and none commits, as the port already
requires. No schema change is needed.

### Visibility

- **A new counter, `usher.sync.retraction.checks`,** counts the ids asked
  about, labelled by `source`, `path` (`sweep` or `push`) and `outcome` (`gone`
  or `served`). On the sweep path, `served` counts the rows the old sweep would
  have retracted while Emby still served them.
- **A panel on Dashboard 3** shows checks per path and outcome.
- **One INFO line per sweep**, counting candidates, gone and still served.
  **One per removal event**, counting asked, retracted and still served.

## Testing

Everything is TDD. Every guard is planted false and seen to fail on its own line
before it counts.

**The fake server.**

- `FakeEmbyServer` learns version groups. An `Ids` listing returns one member of
  each group per request: the first in the server's own order.
- The behaviour is recorded first from the real server and redacted, with a row
  in `tests/fixtures/emby/README.md`. The recording is one `Ids` listing naming
  two versions of one item, and each version alone.

**Contract** (the Emby adapter against the fake server):

- a gone id is returned;
- a served alternate whose sibling shares its request is not returned, and the
  next round is seen asking for it again;
- three versions of one item, all asked for, return none;
- an empty input makes no request;
- a failure past the retries raises;
- an id that comes back without being asked for is ignored.

`FakeSourceAdapter` answers from its seeded items: `forget()` makes one gone. It
can also be told to fail.

**The repository** (both arms of the contract):

- `list_unseen`'s counts and its refusal;
- `retract`'s guard on `available` and `last_seen_at`, and the targets it
  returns;
- `mark_seen` moving forward only and restoring nothing;
- `available_among`.

**The sweep:**

- it retracts exactly what the source confirms gone, and stamps the rest with
  `stored.last_seen_at == run.started_at`;
- over the ceiling it refuses, and the adapter was asked nothing
  (`len(calls) == 0`);
- a failed confirmation fails the run, with nothing retracted or stamped. The
  next walk resumes the same row with the same `started_at`, re-walks no unit,
  and sweeps;
- a row a push event read after the walk began survives a "gone" answer;
- a slow confirmation is beaten through, shown by beats observed while it is
  held;
- a delta never asks.

**Push removals:**

- a confirmed removal retracts, commits and publishes one `title.updated` per
  distinct title or episode. An unmatched row publishes nothing;
- a removal Emby still serves retracts and publishes nothing;
- an id Usher does not hold is never asked about;
- an event naming more than `push_max_items_per_event` ids is applied inline,
  not deferred;
- a failed confirmation raises, and nothing is retracted.

**The counter:** one case per path and outcome, read back from the in-memory
metric reader.

## Acceptance

Merging deploys, so acceptance happens on production after the owner merges.
Each check posts its raw output on the PR.

1. **The first full walk after deploy, on the owner's go.** Its sweep line and
   the counter show how many unseen rows Emby still served. A throwaway
   read-only script then reads every retracted row by id, and all must be 404.
2. **A day of push traffic.** The counter shows removals checked on the `push`
   path, with every `gone` row retracted and no `served` row touched.

## Rollout

**One PR.** In the same commit as the behaviour each one describes, it updates:

- **PRD 03:**
  - the push-events table's "A removal **retracts nothing**";
  - the paragraph opening "A push `ITEM_REMOVED` retracts nothing";
  - the retraction section, where the ceiling now guards a walk's unseen rows
    and the sweep retracts only confirmed rows and stamps the rest;
  - "the retraction ceiling treats removals as the operator's to authorise",
    which now covers walks only;
  - the full-reconcile row of the lanes table.
- **PRD 10:** the counter's row and the new panel.
- **`.claude/rules/emby-push-and-ingest.md`:** "`ItemsRemoved` fires on a
  library from which nothing was removed" becomes confirm-then-retract, and the
  `Ids` version-group finding replaces the line that goes.
- The docstrings that state the old rule (`_ignore_removal`,
  `MediaItemRepository`, `AvailabilitySweepRefused`) and `CHANGELOG.md`.

The plan gets its own heading in `docs/plans/progress.md` and a row in
`docs/prd/README.md`.

**No migration and no new setting.** A deploy restarts the container, so it is
merged with no walk running (`docker ps --filter name=usher-prod-sync`).

## Out of scope

- **Slice 2, the presence pass** after every push reconnect and nightly. It
  catches removals made while the channel was down, and replaces the ceiling's
  operator step with a whole-library id check of about 118 requests.
- **Rows earlier sweeps retracted while Emby still served them.** Production
  holds 1,620 unavailable rows, 1,446 of them beside an available sibling. They
  come back when Emby next saves them, since a delta or a push update then reads
  them. Re-checking them in bulk needs a record of which retractions were
  confirmed, which this slice does not add.
- **Modelling versions**, that is, whether an alternate version should be a
  copy of its primary.
- **Scheduling full walks.**

## Honest caveats

- **The rounds rest on one measured Emby behaviour:** a request lists at least
  one member of every version group that has a served member among the ids
  asked. A server build that broke it would make a served alternate read as
  gone. The fake pins the behaviour, the fixture records it, and acceptance
  check 1 reads every retraction back by id.
- **Confirmation scales with the unseen count.** A walk that missed close to the
  ceiling, about 290,000 ids on Shared Emby, spends roughly two hours a round at
  0.4 requests per second. It is correct but slow, and slice 2's id listing
  bounds it.
- **A removal pushed during a full walk can be undone by that walk** if it read
  the item before the removal. The next full walk confirms and retracts it.
- **Confirming inline delays the push lane's next event** by the time the
  confirmation takes.
