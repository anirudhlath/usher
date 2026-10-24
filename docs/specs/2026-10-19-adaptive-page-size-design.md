# Adaptive page size: design

**Status:** Proposed.
**Date:** 2026-10-19.
**Slice:** 2a of the A+ sync work. Slice 2b, the presence pass, follows with its
own spec and reuses this slice's sizer for its id listing. That pass's two-way
option is recorded as #107.

## What the owner asked for

> I want the page size to also be adaptive. It can start from 1000 and then go
> up and then finds the fastest paging size for itself quickly.

The terms the owner set for adaptive tuning on 2026-10-07 still govern. There is
no courtesy cap ("i dont care about everyone else"). The goal is Usher's own
items per second, and Usher backs off only on errors or timeouts.

Two of those terms changed on 2026-10-19:

- **Start at 1,000.** This replaces "start from the best measured values".
- **Keep no learned size per source.** The owner approved dropping it, because
  the climb takes a minute or two and a stored size can go stale.

## The problem, measured

- **The walk is fetch-bound.** The timed full walk of 2026-10-15 took 1,772 s
  for 1,173,248 items, about 660 items/s. Listing concurrency stayed at 4 the
  whole time. Its 1,197 listing requests averaged 5.36 s, against a walker cycle
  of about 5.9 s a page.
- **Pages cannot grow.** `USHER_SOURCE_PAGE_SIZE` is capped at 1,000
  (`le=1000`). The cap has been in `config.py` since the first adapter commit
  (652bbe90), and no document gives a reason for it. The fast-first-sync spec
  kept it without measuring anything above it.

## What the probe found

A read-only probe on 2026-10-19 timed pages carrying the walk's `Fields` across
Shared Emby's whole library. It sent one request at a time at the start of the
listing and 1.11M items deep, then four at once spread across the library:

```
size    head s  head/s  deep s  deep/s  JSON MB  parsed MB  4 in flight, items/s
 1000    3.6     280     8.0     125    4.5-6.5    11-15     430-460
 2500    3.7     680     8.0     310     11-13     27-30        -
 5000    4.2    1190     9.6     520     24-26     58-60    1300-1740
10000    5.4    1850     9.6    1040     50-51   117-121    2070-2150
20000    6.8    2920    11.4    1750    85-105   201-247        -
```

The probes of 2026-10-07 hit the same server. There, 50,000-item pages took
13.5 s at the head (3,708 items/s) and 17.4 s deep (2,870 items/s).

- **Most of each request is a fixed cost**, Emby seeking to the offset: about
  3.4 s at the head and 7.8 s deep. Each item adds about 0.17 ms. So bigger
  pages keep paying even with four requests in flight: 10,000-item pages move
  about 4.7 times the items of 1,000-item pages.
- **An item is about 5 KB of JSON and about 12 KB once parsed.** A
  10,000-item page holds about 120 MB of Python objects.
- **Emby served every size asked for, up to 50,000, within 18 s.** That is
  under a sixth of the 120 s read budget a listing page has.

So what limits the page size is on Usher's side: memory, and the writer. The
writer has kept up with 660 items/s, and its ceiling has never been measured.

## Design

### What the sizer maximises

**The sizer maximises items handed to the writer per second.** A hand-off is a
page leaving `_pages` for its consumer. The interval between two hand-offs of
one listing covers the rest of that page's request plus the consumer's time,
including any wait to put the page on the writer's queue.

- **While fetching is the bottleneck,** a bigger page shortens each item's share
  of the interval, so the size climbs.
- **Once the writer is the bottleneck,** a bigger page lengthens the interval
  in proportion and gains nothing, so the size stops climbing. Memory therefore
  stays at what speed needs.

The rate is per walker. At a fixed concurrency it is proportional to the walk's
total rate, and it needs no clock shared across walkers, which start and finish
units at different times.

### How it moves

A new `PageSizer` in `adapters/emby/sizer.py`, built and shared the way
`ListingLimit` is: one per adapter, used by every listing that adapter pages.

1. **Measure.** It takes four clean hand-offs at the current size. Their rate
   is the sum of their items over the sum of their seconds.
2. **Climb.** Below the ceiling, it moves to `min(2 × size, ceiling)` and
   measures that size.
   - If the new size is at least 10% faster than the one before it, the sizer
     keeps it and climbs again.
   - If not, the sizer goes back one step and settles.
3. **Re-probe.** A settled size below the ceiling takes 100 more clean
   hand-offs. The last four of them are its fresh rate, and the sizer then
   climbs as in step 2. If the climb gains nothing, it settles and counts again.
4. **Fail.** An outage on a listing page halves the size, never below the
   floor, and settles there. An outage is a 5xx, a 408, a timeout, or a refused
   or dropped connection.
   - Only a request made during the current trial can halve the size. A burst
     of failures therefore halves it once, whether several walkers' pages fail
     or one page fails its retries.
   - A 429 changes nothing. It concerns rate, and the listing limit already
     answers rate.

The constants are named at module level, as `RAISE_AFTER` is:
`TRIAL_HANDOFFS = 4`, `GAIN = 1.10` and `REPROBE_AFTER = 100`.

**A single request varies by about ±7%.** At 5,000 items, 1.11M deep, two
requests took 8.9 s and 10.4 s. Averaging four hand-offs brings the error well
inside the 10% gate. A wrong call near the knee only chooses between two sizes
that, by definition, move about as fast.

**On Shared Emby, while fetching stays the bottleneck,** the sizer goes 1,000 →
2,000 → 4,000 → 8,000 → 10,000. That is five measurements of about two walker
cycles each, a minute or two.

### Clean hand-offs

Each size change, each change in the listing limit's concurrency and each
failure starts a new **trial**. A hand-off counts only when all of these hold:

- **One trial.** The page it hands off, and the page before it in the same
  listing, were both requested during the current trial. So the interval never
  mixes two sizes, two concurrencies, or a retry's waits.
- **A middle page.** Its page is not the listing's first, and not the page that
  ended the listing.
- **No re-read.** No page that moved came between the two hand-offs.
- **Time passed.** Its interval is positive.

Each hand-off carries `ListingLimit.limit`. A hand-off at a concurrency other
than its trial's starts a new trial and is not counted.

### Limits and settings

- **`USHER_SOURCE_PAGE_SIZE`** (default 1,000) is the size every adapter starts
  at and the floor it never goes below. Its `le=1000` cap goes.
- **`USHER_SOURCE_PAGE_SIZE_MAX`** (new, default 10,000) is the ceiling.
  - It must be at least `USHER_SOURCE_PAGE_SIZE`, or startup refuses.
  - Setting the two equal turns adaptation off.
- **Both are capped at 50,000,** the largest page Emby has been measured
  serving.
- **Memory at the default ceiling.** A walker holds at most two parsed pages:
  the one it handed off and the one it is reading ahead. Four walkers therefore
  hold about 1 GB. Counting the bodies still being parsed and the writer's
  queue, the peak is roughly 1–1.5 GB.
- **The adapter's own default is no adaptation.** `page_size_max=None` means
  the floor, so a direct construction in a test or a script pages exactly as it
  is asked. `ConfiguredSourceAdapterFactory` passes the setting.
- **`MAX_PAGES` is unchanged.** Every page but a listing's last asks for at
  least the floor, so PRD 03's bound still holds: a walk fails only on a listing
  that 10,000 pages do not finish, 9,500,050 items or more at the default.
- **The writer needs no change.** Its statements load rows by `COPY` and match
  with `= ANY(array)`, so a 10,000-item commit runs into no bind-parameter
  limit. A commit already holds at least one whole page, since `_write` commits
  once a unit's pages reach `USHER_SYNC_BATCH_SIZE` items.

### The window

**`OffsetWindow` loses `limit`.** `receive(entries, total, *, asked)` takes the
size each page was requested at. A page is short when it holds fewer entries
than `asked`, or than the longest page served so far if that is fewer.

Today a page is compared with the longest page served alone. After a size drop,
that reads a full page as short. A short page past the first page's total ends
the walk, so a library that grew during the walk would lose the items at its
end.

Nothing else in the window depends on the page size:

- the reach-back: 50 items, clamped to half the page before;
- the backup before a page that moved;
- resume by item offset and `DateCreated` checkpoint.

### The adapter

- **Each request takes its size from the sizer.** `_pages` keeps that size and
  the trial with the pending request. It passes the size to the window as
  `asked`, and reports each clean hand-off timed by the adapter's clock.
- **`_page` calls `PageSizer.failed(trial)` beside `ListingLimit.failed()`**
  when a listing page fails as an outage. It never does for a 429, and never for
  the reads by `Ids` or the counts, which are not listing pages.
- **A retry asks for the same page unchanged,** so the window's arithmetic stays
  exact. The halving applies from the next request.
- **The other listings share the sizer.** The seed, the watch-state listing and
  the delta listing all page through `_pages`, but they rarely produce enough
  clean hand-offs to move it.

### Visibility

- **A new gauge, `usher.source.listing.page_size`,** labelled by `source`,
  sits beside `usher.source.listing.concurrency`. `_pages` writes the size each
  time a listing page arrives, so an adapter that never lists leaves no
  reading.
- **One INFO line per change** names the sizes and the per-walker rates that
  decided it, for example:
  - "Shared Emby's listing pages grow from 2,000 to 4,000 items (1,020 → 1,690
    items/s a walker)";
  - "… settle at 4,000 items: 8,000 gave 1,710 items/s against 1,690";
  - "… drop from 8,000 to 4,000 items after a failed listing".
- **A panel on Dashboard 3** plots the page size per source, beside panel 11's
  listing limit.

## Testing

Everything is TDD. Every guard is planted false and seen to fail on its own line
before it counts.

**The sizer** (unit cases; each hand-off carries its own seconds, so no clock is
needed):

- it starts at the floor;
- it climbs while each doubling gains at least 10%, and settles one step back
  when one does not;
- a gain of exactly 10% climbs;
- its last climb is clamped to the ceiling, and it never passes the ceiling;
- with the floor equal to the ceiling, it never moves;
- it re-probes after 100 clean hand-offs, and climbs again if the doubling now
  gains;
- `failed()` halves it, never below the floor, and nothing climbs until the next
  re-probe;
- a failure of a request made before the last halving does not halve again;
- a hand-off from an earlier trial is ignored;
- a hand-off at another concurrency starts a new trial;
- a non-positive interval is ignored.

**The window:**

- after a size drop, a full page at the new size is not short, and the walk
  reads past a stale total;
- a server that serves fewer items than asked, at every size, still ends the
  walk where it does today.

**The adapter against the fake server:**

- a walk whose size climbs partway through a listing reads every item exactly
  once;
- so does a walk whose size drops after an outage, with items inserted and
  deleted between pages, as the existing reach-back cases do;
- each request asks for the sizer's size, read off the wire;
- an outage halves the next request's size, and the retry's is unchanged;
- a 429 leaves the size alone, and so does an outage on a read by `Ids`;
- with `page_size_max` unset, every request asks for `page_size`, as today;
- the gauge reads the size after a listing page, and an adapter that never
  lists writes no reading.

**The walk:**

- a whole-library walk with adaptation on commits every item;
- a walk killed mid-unit resumes at the committed offset, under a different page
  size from the one it died at.

**Settings:**

- a ceiling below the floor is refused at startup;
- both bounds are enforced.

## Acceptance

Merging deploys, so acceptance runs on production after the owner merges. Each
check posts its raw output on the PR.

1. **A timed full walk, on the owner's go.** It records:
   - the wall time, against 1,772 s;
   - the page-size gauge's trace and the INFO lines;
   - the container's peak memory, sampled with `docker stats`.
2. **Reading the result.**
   - If the size settles below the ceiling, the writer is the bottleneck, and
     the walk's rate is the writer's rate.
   - If the size pins at 10,000, fetching is still the bottleneck, and raising
     the default ceiling becomes a follow-up.

## Rollout

**One PR.** In the same commit as the behaviour each one describes, it updates:

- **PRD 03, "Walking the library":** pages start at `USHER_SOURCE_PAGE_SIZE`
  and adapt up to `USHER_SOURCE_PAGE_SIZE_MAX`. "At most 1,000" goes.
- **PRD 10:** the gauge's catalogue row, a paragraph beside the concurrency
  gauge's, and the panel.
- **`docs/guide/configuration.md`, "Walking a large library":** both settings,
  and the 10,000-page bound stated from the floor.
- **`.env.example`:** the new setting and its comment.
- **`.claude/rules/emby-push-and-ingest.md`:** the paging bullet gains "a page
  is short against the size it was asked at".
- **`CHANGELOG.md`.**

The plan gets its own heading in `docs/plans/progress.md` and a row in
`docs/prd/README.md`.

**No migration.** A deploy restarts the container, so the PR is merged with no
walk running (`docker ps --filter name=usher-prod-sync`).

## Out of scope

- **Slice 2b, the presence pass.** It gets its own `PageSizer` for its lean id
  listing.
- **Keeping a learned size per source.** Dropped on 2026-10-19.
- **Adaptive concurrency by throughput.** The listing limit stays
  failure-driven, between 1 and `USHER_SYNC_WALKERS`.
- **Streamed JSON parsing.** Only pages well past the default ceiling need it.
- **Shrinking a page on retry.** See the caveats.

## Honest caveats

- **The per-walker rate rises as a walk winds down.** While the last units
  finish, fewer walkers contend for Emby and each one's hand-offs speed up. A
  re-probe then can credit a doubling with gains that came from less
  contention. The cost is a bigger page than needed for the walk's tail: memory
  within the ceiling, no time lost.
- **A page too big for the server fails its retries at the same size.** That is
  about eight minutes of waits, after which the walk fails and resumes in place;
  the halving applies only to later requests. Emby has served 50,000 items
  within 18 s, so at the default 10,000 this is not expected.
- **One failure resets every walker's measurement,** because the walkers share
  one trial.
- **Every new adapter starts at the floor,** so each walk spends its first minute
  or two climbing.
