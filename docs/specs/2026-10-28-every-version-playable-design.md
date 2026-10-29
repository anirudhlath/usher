# Every version playable as itself: design

**Status:** Proposed.
**Date:** 2026-10-28.
**Slices:** two, each with its own plan and PR. Slice 1 makes every copy Usher
holds describe and play its own file. Slice 2 finds the versions Usher has never
held. Slice 2's plan is written after slice 1 passes acceptance.

## What the owner asked for

Adaptive page size's acceptance walk (2026-10-24) left 4,638 rows that Emby still
serves and the walk never listed. They turned out to be alternate versions. The
owner answered:

> Well we need to fix that issue

> What do you mean, i want all version available

and, asked whether each version should be a copy that plays its own file:

> Yes, every version playable as itself

The program's goal still governs: "automatically stay in sync with the server",
with an A+ in every aspect, shipped simplest slice first. On coverage, the owner
agreed to a larger sample first and a full check only if the sample found gaps.
It found gaps, and the owner chose to skip the separate 6-hour check: "Yes, skip
it and write the design". Slice 2's first run produces the exact count instead.

## The problem, measured

All measurements were taken on production against Shared Emby, using read-only
GETs and SELECTs. The scripts lived outside the tree.

**How Emby presents versions.** Emby groups the versions of one film or episode.

- A listing (`/Users/{user}/Items`, an `Ids` listing included) names **one member
  per group**, and that entry carries **only its own** `MediaSources` entry.
  Which member gets named depends on the query: a season's episode listing named
  a different member than the library listing in 15–16 of 20 groups. The
  library listing's choice changed for about 4,500 groups between the 10-15 and
  10-24 walks.
- Nothing ungroups a listing. This was tested with `ProjectToMedia`, `/Items`
  with and without a user, and a `MinDateLastSaved` floor of 1900. No listing
  carries a version count either: there is no `MediaSourceCount`, and
  `PartCount` is always 1.
- The single-item route (`/Users/{user}/Items/{id}`) lists **every** version in
  `MediaSources`. Each entry's `ItemId` is that version's own item id, and its
  `Id` is `mediasource_<that id>`. All 193 multi-version items in the sample
  named themselves among their sources.
- A delta listing (`MinDateLastSaved` at a recent instant) returns alternates,
  as found on 2026-10-07. So does a push event, which names the items that
  changed. Both name **items**, not groups.

**What Usher does today.**

- `mapping.primary_media_source` picks the best source by pixels, then bytes.
  `to_source_item` (the description) and `build_stream_targets` (playback) both
  use it.
- A walk reads listings, where each entry carries only its own source, so a
  walked row describes its own file.
- A by-id read carries every version, so the push lane's `get_item` describes
  the **group's best file**, and `stream_targets` always plays it. In 9 of 20
  sampled reads, that was another version's file.

**How common versions are.** In a seeded sample of 20,000 listed items, from 200
random windows, each item was read by id (`/var/tmp/versions/`):

| | |
|---|---|
| items with more than one version | 193 (1%): 165 movies (about 9% of movies), 28 episodes (0.16%) |
| their other versions | 206 |
| movie versions in **another library**, held, listed by the last walk or seen since | 149 |
| hidden versions in the same library, held because the sweep keeps them | 46 (21 episodes, 25 movies) |
| versions Usher **does not hold** | 11 (7 episodes, 4 movies) |

- 195 of the 206 versions are held but play the group's best file. In most of
  those 195 cases, that means another version's file.
- The 11 unheld versions scale to about 640 across the 1,170,430 listed items,
  somewhere between 320 and 1,150.

**Why the 11 are missing.** All 11 were created on Emby between 2021-10 and
2026-01, and none has been saved since Usher's first walk (2026-09-24). Each has
stayed hidden behind a listed sibling since before Usher existed, and nothing
has named it since.

New versions do reach Usher:

- 6,330 rows were created between the 10-15 and 10-24 walks, by push events and
  deltas.
- Of the 860 rows the 10-24 walk created, 847 were items made before Usher's
  first completed walk: old versions that Emby's listing had just started naming.

So the gap is historical. The live paths name versions as they arrive.

**Read rates.** Single-item reads ran at 53.7/s at concurrency 8 and 68.8/s at
concurrency 16, with a median of 0.196 s and a p95 of 0.337 s. One read of every
listed item would take 5–6 hours.

## Design

### Slice 1: each copy describes and plays its own file

**The rule.** An Emby item's file is its **own** `MediaSources` entry. That is
the entry whose `ItemId` equals the item's `Id`, or failing that, whose `Id` is
the item's `Id` or `mediasource_<Id>`.

- A payload with a single entry uses that entry, whatever its ids say. Every
  listing entry has a single entry.
- A payload with several entries and no own entry falls back to today's
  best-version pick. It logs a WARNING naming no id and counts the fallback
  (*my call*: it never happened in the sample, so its rate is the thing worth
  seeing).
- A folder item with no entries is unchanged.

**One change covers both halves.** `to_source_item` and `build_stream_targets`
already share the selection, and the runtime derivation depends on that sharing.
So replacing the selection fixes the description and the playback URL together:

- `MediaSourceId` names the copy's own version.
- `/Videos/{id}` names the copy's own item.
- `_playback_rank` survives only as the fallback.

**The sweep re-describes the hidden versions it keeps.** The sweep's `Ids`
confirmation asks for the walk's fields (`ITEM_FIELDS`). Each entry it returns
for a candidate is written the way a walk writes a listed entry, at the run's
`started_at`, in the commit that already stamps those rows as seen (*my call*,
which the owner accepted). Each such entry carries only its own source, so its
description is its own file. A push removal's confirmation may ignore the
entries it gets back.

**What converges, and when.**

- From the deploy onward, every push read and every `/play` uses the copy's own
  file.
- A description written by an earlier by-id read (a push, typically) may still
  describe another version's file. The next full walk rewrites every listed row,
  since the upsert overwrites the descriptive columns. Its sweep rewrites every
  hidden one.

**Choosing between versions** uses the ranking that already exists (*my call*,
which the owner accepted). `/ways` ranks every copy per screen from its stored
facts. Once each copy's facts are its own, that ranking chooses between
versions correctly, with no new rule.

**`/play` adopts that ranking's quality order** (*my call*, added after the
builder check).

- **Why it has to change.** Today `/play` takes copies in the repository's
  order: available first, then latest seen, then id. The console marks the
  first target as best. While every copy played the group's best file, that
  order could not matter. After slice 1, it would put whichever version was
  read last first, a 1080p copy ahead of the 4K one, say.
- **The new order.** Copies are ordered available first, then by
  `domain.ways.quality_of(copy).score` descending, which is the device-free
  part of `/ways`' ranking, with today's order as the tiebreak.

**Watch progress stays shared** per film or episode (*my call*, which the owner
accepted). Write-back still goes to every copy.

### Slice 2: the versions pass

**What it does.**

- It reads every available row of a source by id, using the slice 1 read. It
  skips rows that carry no file, such as a series' own row (*my call*; the
  builder decides how to tell).
- Every other version a read names that Usher does not hold is read by id and
  ingested like any new item. It is matched by the ladder, an episode attaches
  to its series, and the version becomes a copy of its own.
- Rows it only reads are not rewritten (*my call*). Their descriptions are the
  walk's job, and so 99% of the pass writes nothing.

**What the port needs.** The adapter has to say which other versions a by-id read
named, as their external ids. Name the concept generically, as other copies of
the same work on this source, because other servers have versions too. The
shape and the name are the builder's.

**How it runs** (*my calls*):

- **On an operator's command.** Nothing schedules it, as with the full walk. It
  runs once after slice 2 deploys, and again whenever the operator wants it. The
  CLI spelling (a `usher sync` kind or a subcommand) is the builder's.
- **Resumably.** A deploy restarts the container and kills a job running in it.
  So the pass keeps its position, and the next pass of the same source continues
  from it rather than starting over.
- **At bounded concurrency.** A setting gives the number of reads in flight,
  default **8**: about 54 reads a second, roughly 6 hours for this library, on a
  server the household does not run. The pass goes through the source's normal
  request path, so retries and any outbound rate limit apply as they do to every
  other read.
- **Through failures** (*my call*, added after the builder check).
  - A read that fails as unreachable is retried on a listing page's schedule
    (15, 30, 60, 120 and 240 s). One that still fails stops the pass at its
    committed position: the run is failed, and the next pass resumes it.
  - A read whose answer is unusable, such as another 4xx or a malformed
    payload, is counted and skipped. Nothing a source puts in a payload may
    stop the pass.
- **Beside everything else.**
  - It retracts nothing and sweeps nothing. A 404 is counted and skipped, and
    the next full walk's sweep decides.
  - It never counts as an item-lane cursor. A delta resumes from completed full
    and delta runs, not from a pass.
  - It is safe beside a walk, push and the worker: running both must leave
    every row correct. Whether one waits for the other is the builder's call.
  - Each row it creates is stamped seen at the instant of its read.
- **Visibly.** At the end, and as it goes, it reports rows read, versions named,
  versions created, 404s and unusable answers. Progress shows where an operator already
  looks (`usher sync-status` or the run's row).

**Why push needs no change.** A push event and a delta name the version that
changed, and both now read it as itself. The gap the pass closes is the
historical one, plus any version added while both missed it. The pass catches
that whenever it is next run.

## Testing

TDD, with the repository's gate green before every commit.

**Slice 1.**

- **Selection.**
  - The own entry is found by `ItemId`, by a plain `Id`, and by
    `mediasource_<Id>`.
  - The own entry wins when it is not the best version, which is the case that
    fails today.
  - A single entry is used whatever its ids say.
  - Several entries with no own entry fall back to the best pick, log the
    WARNING, and count.
  - A folder item gets no entry.
- **One file per copy.** For a two-version payload, read once as each version,
  `to_source_item`'s container, codecs, resolution, HDR, size and runtime match
  that version's own entry. So do `build_stream_targets`' `MediaSourceId`, path
  id and facts. The test also asserts that the two versions differ in each of
  those fields, so a mixed-up selection cannot pass.
- **A fixture.** It is a by-id payload with two versions, captured from a real
  server with every id, name and path redacted, as the existing fixtures are.
  The contract suite runs on it.
- **`/play`'s order.** A 4K copy seen earlier ranks ahead of a 1080p copy seen
  later. The test asserts its own premise first: the repository's order puts
  the 1080p copy first. An available copy still ranks ahead of a retracted one
  of better quality.
- **The sweep.**
  - A kept hidden version's stored description changes to its own entry, at
    `started_at`, in the stamping commit.
  - A gone candidate is still retracted.
  - A push removal's confirmation still answers what it did.

**Slice 2.**

- An unheld version named by a read is created, matched and attached. An episode
  version attaches to its episode.
- A version that is already held is not read again.
- A 404 is counted, and nothing is retracted.
- An unusable answer is counted and skipped.
- An outage that outlasts the retries stops the pass, failed and resumable.
- A pass stopped partway resumes from its position, and reads nothing twice that
  it had committed.
- The concurrency bound holds, shown by observed overlap of the recorded
  intervals, not by a count.
- A pass is never a delta's cursor.

## Acceptance

Acceptance is live, on production, using GETs only against Shared Emby. Raw
output is posted on each PR.

**Slice 1**, after its deploy and one full walk:

- Re-run the seeded 20,000-item sample. For every held copy in a version group:
  - its stored description equals its own `MediaSources` entry;
  - `/play`'s direct target names its own `MediaSourceId`.
- For 20 groups, a one-byte ranged GET of each copy's stream URL reports a total
  size equal to that copy's own size. This proves Emby serves the file the copy
  describes.

**Slice 2**, after its first pass:

- The pass's counts.
- Versions created, set against the sample's estimate of 320–1,150.
- A re-run of the sample finds no unheld version created before the pass began.

## Rollout

1. Slice 1's PR. The merge deploys and is the owner's.
2. A full walk, with the owner's go-ahead, then slice 1's acceptance.
3. Slice 2's plan, then its PR, merged by the owner.
4. The first pass on production, with the owner's go-ahead: about 6 hours of
   reads on a server the household does not run. Then slice 2's acceptance.

PRD changes ride each slice's commits:

- **PRD 03:** playback, the sweep and the versions pass.
- **PRD 02:** a MediaItem is one version in one place.
- **PRD 07:** `/play`'s copy order.
- **PRD 08:** the pass's setting.
- **PRD 10:** any metric.
- **`docs/plans/progress.md` and `docs/prd/README.md`:** the status tables.

## Out of scope

- Merging versions into one copy, or a default-version rule beyond the existing
  ranking.
- Watch progress kept separately per version.
- A listing that ungroups versions; none exists.
- Scheduling the pass.
- Multi-part files (`PartCount` is always 1 here).
- Emby's own version labels, such as a source's `Name`, shown to clients.

## Honest caveats

- **The 640 estimate is wide.** It rests on 11 versions found in 20,000 items.
  The pass gives the real number.
- **A version can be missed between passes.** One added while push is down and
  the gap-closing delta is refused (no cursor, or past its ceiling) stays
  missing until the next pass.
- **The fallback has never fired.** All 193 multi-version items named
  themselves, so the fallback's real-world shape is unseen.
- **Resume positions can differ.** Two versions' positions on Emby can differ
  until a write-back reaches both. `/play` offers the position of the version it
  plays.
- **The size check rests on an assumption.** It assumes Emby's static stream
  serves the file unchanged, which `static=true` asks for.
