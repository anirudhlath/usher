# Self-healing sync: design

**Status:** Proposed, with three ⏳ answers owed by the owner.
**Date:** 2026-11-05.
**Slices:** four, each with its own plan and PR. Slice 1 brings a stopped push
lane back by itself. Slice 2 runs a full walk every day. Slice 3 tells a person
when neither of those can fix something. Slice 4 runs the versions pass by itself.
Slice 2 must follow slice 1, because a sweep can deadlock with a push and end its
lane (`_sweep`'s docstring), and a daily sweep makes that a daily risk. The rest of
the order is by cost against what it buys (*my call*): slices 3 and 4 depend on
nothing and could move up. Slice 1's live proof waits on chance, so slice 2 does
not wait for it.
**Follows:** `docs/specs/2026-10-28-every-version-playable-design.md`, whose
slice 1 passed acceptance on 2026-11-05. That spec left this one to decide when
its versions pass runs. Slice 4 here replaces its "How it runs", and nothing else
in it.

## What the owner asked for

Told that the versions pass would run by hand because nothing schedules a sync,
the owner answered:

> Manual is fine for now but we need to have a self healing mechanism that
> proactively fixes issues like this.

> I basically want the emby to stay in sync with usher always, with all versions
> available.

The program's goal still governs: "automatically stay in sync with the server",
with an A+ in every aspect, shipped simplest slice first.

## The problem, measured

All of this was read on production on 2026-11-05: Usher's metrics, logs and
database, with GETs only against Shared Emby.

**Usher has three ways to follow a source, and none of them is dependable
unattended.**

| path | what it is for | today |
|---|---|---|
| push lane | every change, within seconds | stops for good after a brief outage |
| gap-closing delta | what a dropped socket missed | runs only when a lane reconnects |
| full walk and sweep | everything else, removals above all | nothing starts it; 11 runs ever, all by hand |

**Push was down for 51% of the last 16 days**, about 192 of about 378 hours. The
metrics exporter was alive throughout.

| lost the socket (UTC) | how long the failures lasted | came back | down for |
|---|---|---|---|
| 10-27 10:16 | 53 s | with a deploy, 10-29 | about 44 h |
| 10-29 10:30 | 87 s | with a deploy, 11-04 | about 135 h |
| 11-05 11:13 | 61 s | with a deploy, 11-05 | about 13 h |

- Each outage was a network blip of about a minute. Five failed connections in a
  row reach `USHER_PUSH_MAX_CONSECUTIVE_FAILURES`, `PushSupervisor.run` returns,
  and `LaneSupervisor.refresh` never restarts a finished lane. Only a process
  restart brings it back, since nothing ships a way to disable a source.
- The log line says it is "leaving this source to the nightly reconcile". No
  nightly exists.
- Reconnecting closes the gap with a delta, so changes were late rather than
  lost. A crashed lane behaves the same way as a stopped one: a database error
  in a lane ends it until a restart.

**A delta cannot see a removal.** Only a walk's sweep can, or a push event. The
walk of 2026-11-05 found 2,731 rows gone from the server that nothing had caught
since the walk 12 days before, for most of which push was down. Until then each
read as playable.

| full walk | rows it retracted | wall |
|---|---|---|
| 10-14 | 10,743 | 4,042 s |
| 10-15 | 101 | 1,763 s |
| 10-24 | 11 | 556 s |
| 11-05 | 2,731 | 655 s |

**Nobody was told.**

- `LaneSupervisor.refresh` releases a finished lane's adapter, so
  `usher_source_push_connected_ratio` vanishes rather than reading 0, and the
  `Push down` alert (`== 0`) cannot fire.
- It could not have reached anyone anyway. The shared Prometheus has no rule
  groups and no Alertmanager, and Grafana provisions only dashboards and
  datasources. The alerts in `dashboards/alerts/` are loaded nowhere.

**Two kinds of drift no path catches today.**

- **Rows retracted while the source still serves them:** 15 of 17,948, 4 of them
  the only copy on that source. The sweep asks only about available rows, and a
  walk's listing does not name these.
- **Versions Usher does not hold:** 15 of the 478 versions in Task 7's seeded
  sample of 19,563 items, which scales to about 900, somewhere between 500 and
  1,500. The first sample, of 20,000 items, found 11.

**What a daily walk would cost.**

- The walk itself: 655 s of wall time and 453 requests, of which 291 are listing
  pages, 131 are confirmation requests and 31 are others.
- **The jobs it leaves behind cost far more.** Every walk queues a `match` job for
  each unmatched row, and a finished job's row is deleted, so the next walk queues
  them all again. In the 24 hours after the walk of 11-05 that was 6,215 jobs, each
  a by-id read of Shared Emby, with 2,094 s of job time in the server's worker lane
  and about 660 requests to TMDb. So a walk's real cost on Shared Emby is about
  6,670 requests, not 453.
- **Most of those reads cannot find anything.** 184 of a sample of 200 unmatched
  rows are episodes, and `match_remote` searches only movies and series, so an
  episode's job reads the item and returns. 461 of the 6,215 jobs reached a search,
  and 2 of those were confident: 3 of 641 in all the metrics kept. The unmatched
  count itself barely moves: 7,032, 7,032, 6,189 and 6,199 over the last four walks.
- `media_items` holds at 0.57 GiB after 9.2 million updates, with 14,328 dead rows
  right after a walk, and autovacuum keeps up.
- The heartbeat stands still for 21.8 s at the end of a walk, at 4,690 kept rows,
  against a 600 s stale window.

**Where it would run.**

- The scheduler is on in production (`USHER_SCHEDULER_ENABLED=true`) and does real
  work in the server's process: one process's life recorded 25 scheduled runs
  totalling 33 hours. Runs happen one at a time, so a long one holds up the rest.
- No job worker has run a `sync` on production, in the 40 days of metrics kept.
  Every walk has been `usher sync` in a container of its own, so **no walk has yet
  shared the server's event loop**. `/health/ready` reads a median of 2.5 ms and a
  p99 of 5.5 ms without one.
- The scheduler's three metrics lose their job's name on the way to Prometheus:
  the attribute `job` arrives as the target's own `job="usher-prod"`. Two jobs
  cannot be told apart, and whether a second series collides with the first has
  not been observed.

## Design

Each path covers for the one before it, and whatever none of them can fix reaches
a person:

| what drifts | what heals it | how soon |
|---|---|---|
| a change while push is up | a push event | seconds |
| a push lane that stopped or crashed | slice 1 | about 5 minutes after the cause ends |
| a change while push was down | that lane's reconnect delta | as it reconnects |
| a removal while push was down | slice 2's sweep | within about a day |
| a row retracted but still served | slice 2's sweep | within about a day |
| a version nothing has named | slice 4's pass | within a round |
| anything none of these can fix | slice 3's alert | 15 minutes for push, 36 hours for a walk |

### Slice 1: a push lane comes back by itself

**A lane stops only when retrying cannot help.** During an outage it keeps trying.

- When failures in a row reach `USHER_PUSH_MAX_CONSECUTIVE_FAILURES`, the lane
  does what it does today: it logs one ERROR and writes `supports_push = false`.
  It then keeps trying instead of returning. The wait keeps doubling up to
  `USHER_PUSH_MAX_BACKOFF_SECONDS` and stays there, 150 to 300 s at the default
  with the existing jitter.
- When a connection comes back, its delta closes the gap, and the gauge reads 1 on
  the first frame of any kind. `_note` writes `supports_push = true` as it already
  does, at the first event that maps to something, and one INFO line there says how
  many attempts it took. On an idle library the column and that line can lag the
  gauge, so the gauge is what to read.
- The adapter stays open, so the gauge reads 0 for the whole outage and `Push down`
  means what it says. A one-minute blip heals in about two minutes, under that
  alert's 15.
- A socket that connects and never delivers, such as a buffering proxy, keeps
  trying as well. Each connection runs the delta it already runs, at most once per
  `USHER_PUSH_GAP_MIN_INTERVAL_SECONDS`, so that source is polled rather than
  abandoned. Its gauge reads 0.

**One stop remains: no push channel** (`SourceNotSupported`), as today. That answer
will not change.

**A rejected login slows to one try a day** (*my call*). `PortAuthFailed` cannot be
fixed by retrying every five minutes, and on a server the household does not run a
locked account may not be the household's to unlock. The account's record carries
an `InvalidLoginAttemptCount`, but no limit this account can read. So on
`PortAuthFailed` the lane writes `supports_push = false` and waits a day before its
next try, without returning.

- The gauge reads 0, so `Push down` fires. The ERROR line says to store the new
  password with `usher set-credentials` and restart Usher.
- A rejection that was only a moment's fault heals itself within a day.
- A 401 that survives a good login is the same exception, and waits the same day.

**A way to store a new password** (*my call*). Nothing ships one today. Registration
is the only write of a source's credentials, nothing can disable a source, and
deleting one cascades to its rows. So an ERROR line that said "fix the credentials"
would name something no operator can do.

- `usher set-credentials --source NAME --username-env VAR --password-env VAR`, on the
  model of `usher rotate-secret`: it takes the variables' names, never the values,
  refuses a value given by mistake, overwrites the stored row and prints neither.
- It is a command rather than an endpoint because the API has no authentication and
  this box is internet-facing.
- Running lanes and the worker's cached adapters hold the old password in memory, so
  its output says to restart Usher, and new adapters read the stored row. A lane
  waiting out its day does not notice by itself.
- PRD 08's "until an operator re-enters it" has a way to do it, for the first time.

**A crashed lane starts again** (*my call* on the spacing). When an exception
escapes the lane, a database error or a bug, `_guard` records when. The first
refresh at least `USHER_PUSH_MAX_BACKOFF_SECONDS` later starts a new lane on a new
adapter.

- `/health/ready` names the source in `crashed_sources` until then. That report is
  never gated on, so readiness does not change.
- The old adapter stays open until the new lane replaces it, so the gauge reads 0
  rather than vanishing, and a lane that crashes on every start still fires
  `Push down`. A lane that ended on `SourceNotSupported` releases its adapter as today.
- The spacing bounds logins and stack traces if the crash repeats.
- A new counter, `usher.source.push.crashes`, labelled by source, counts them.
- This also covers the deadlock PR #113 left open: if the push side loses to a
  sweep, its lane comes back.

**A trap in the arithmetic.** `_backoff` computes `2 ** (failures - 1)`, and a
float cannot hold that past 1,023 doublings, which an outage reaches in under
three days. The exponent has to be capped, or the first long outage crashes the
lane this slice exists to keep alive.

**Words that stop being true**, replaced in this slice:

- `services/push.py`'s ERROR line and `run`'s docstring. An indefinite retry no
  longer looks "identical to a working one": the gauge reads 0.
- `_guard`'s line, "will not restart until the next refresh", which is false today.
- `refresh`'s and `crashed_sources`' docstrings.
- `_sweep`'s docstring, where a lane stays down until a restart.
- PRD 08's "After `USHER_PUSH_MAX_CONSECUTIVE_FAILURES` … lean on the full walk",
  and the same setting's description.
- PRD 03's statements on a stopped lane.

### Slice 2: a full walk every day

**A scheduled job, `sync.full`.** `USHER_SYNC_FULL_PERIOD_HOURS` defaults to 24
(*my call*), and 0 leaves it unregistered. It sits behind `USHER_SCHEDULER_ENABLED`
like every scheduled job, so a fresh install still walks nothing unasked.

- **`last_done()` asks the artefact.** For each enabled source, the newest
  completed full run's `finished_at`. It answers the oldest of those, `None` if a
  source has none, and "now" if no source is enabled.
- **`run()` walks the due sources one at a time, oldest first.** Each walk is
  exactly what `sync_handler` does: the reconcile, its watch lane after the seed,
  and the watch lane again after the walk. The two callers share that code rather
  than each keeping a copy.
- **It registers first**, so that a long neighbour rebuild does not start ahead of
  a due walk.

**It runs in the scheduler's own task, not through the job queue** (*my call*).

- A failed walk records a `FAILED` run and returns, so a queue would see a finished
  job and offer the walk again at the tick rate. The scheduler already spaces a
  failing job.
- A parked `sync` row is never revived by asking again (`_ENQUEUE`), so one bad
  night through the queue could end the walks for good.
- `usher schedule` runs the same registry in its own process, so if a walk turns out
  to hurt the server, moving it off takes no Usher code. It does take a compose
  service and a change to the deploy workflow, which today stops, migrates and
  recreates only `usher`. A second container left out of that would keep its old
  image and keep walking through a migration.

**A walk stops queueing reads it cannot use** (*my call*). A daily walk would make
today's 6,200 job reads a daily cost, and about nine in ten of them cannot find
anything.

- An episode is attached by the walk that finds its series matched, and its `match`
  job changes nothing. So `IngestService` stops queueing them, and PRD 03's "enqueued
  for a re-match" goes. An unmatched episode stays in the review queue as it does today.
- That takes a walk's cost on Shared Emby from about 6,670 requests to about 910.
- The jobs for unmatched movies and series stay, about 460 reads a day and that day's
  roughly 660 requests to TMDb, because a re-search is the only way to find a title
  TMDb has gained since. Asking daily rather than every 12 days lowers an already low
  yield, so acceptance reads it after a week, and asking less often is a small later
  change if it stays near nothing.

**When a walk fails.**

- A failed walk, a refused sweep included, does not stop the other sources. After
  them, `run()` raises, naming the sources. `usher.scheduler.job.failures` counts
  it, and the scheduler offers the job again after 5, 10, 20 … minutes, up to the
  period: about nine attempts in the first day, then one a day.
- A walk refused because another is alive (`WalkRefused`) is no failure. If that
  is all that happened, `run()` answers `DECLINED` and the job is spaced the same
  way.
- A deploy stops a running walk. A clean stop closes the run as resumable, and a
  hard kill leaves it to go stale in 10 minutes. Either way a later offer resumes
  it, as `usher sync` would.
- **A rejected login is not retried for a period** (*my call*). Nine tries in a day
  is what slice 1 avoids. The failed run records it with a new `error_code`, and
  until one period after that run the job skips the source without contacting it,
  answering `DECLINED` if nothing else failed. It reads this from the run's row, so
  a restart does not forget it. `usher sync` by hand still tries at once.

**When it runs.** A period is a minimum interval since the last completion, so
starts creep later by a walk's length each day, and can land on any hour. Acceptance
reads whether that matters. If it does, `usher schedule --once` from cron gives a
wall-clock time in its own process.

**The sweep also asks about retracted rows** (*my call*), because nothing else ever
would, and those 15 rows read as gone while they play.

- Its candidates are read before the source is asked, and it asks about every
  retracted row of the source with the same confirmation it uses for rows the walk
  did not list.
- An entry the source still serves is written the way a kept hidden version is
  written, in the run's completing commit, which brings the row back, and is
  counted `restored`. A row the source does not serve stays retracted.
- These rows never count toward `USHER_SYNC_MAX_RETRACT_FRACTION`.
- This replaces PRD 03's "the sweep only ever sets `false`" and "asks only about
  available rows". It costs a few hundred requests a night at 17,948 rows, by the
  131 that 7,421 rows took.

**What it repaired is counted**, so that "self-healing" is something to read rather
than to believe. A new counter, `usher.sync.repairs`, labelled by source and `kind`:
`created` for rows a full walk or its sweep inserted, `retracted`, and `restored`.
One INFO line per walk says the same. These are the drift that push and deltas
missed, so a high count on a quiet night is a finding.

**How old the last walk is.** A new gauge, `usher.sync.full.age`, per source: the
seconds since its last completed full walk, or since the source was added if it has
none. `last_done()` refreshes a snapshot of the timestamps on each tick. The callback
stays synchronous, touches no database and computes the age when it is read, so the
line keeps rising if the ticks stop. A new alert, `Sync stale`, fires above 36 hours
for 15 minutes (*my call*: one and a half periods, enough to clear a rebuild ahead
of it). With push down, `Push down`'s description, "The nightly reconciler covers the
gap", becomes true.

**The scheduler's metrics get their job's name back** (*my call*). The attribute is
renamed from `job` to `scheduled_job` on all three instruments, with PRD 10 and any
panel that reads them, in this slice, because this slice's walks are what an operator
needs to tell apart.

**Docs in this slice:** PRD 03's "Nothing in Usher schedules it; run it nightly from
cron" and "enqueued for a re-match", PRD 08's "two named jobs" and its table, PRD 10's instruments, panels and
alert table (`tests/unit/test_alerts.py` grades the rule files against it), and the
settings, the guides and `Push down`'s description.

### Slice 3: someone hears when it cannot heal itself

Slices 1 and 2 fix what they can, and two alerts name what they cannot: `Push down`
and `Sync stale`. Neither reaches anyone until the rules are loaded and routed.

- **Load `dashboards/alerts/usher.yml` into the shared Prometheus**, the way that
  file's header says: a `rule_files` entry and a read-only mount. That is six alerts
  in seven rules today, and `Sync stale`.
- **Add one small Alertmanager container, with one route** (*my call*). Prometheus
  has none today. Grafana's own alerting would need no new container, but it would
  mean every rule written a second time in Grafana's format (`grafana/usher.yml` takes
  565 lines for two rules), against a Prometheus file the repository's tests already
  grade.
- **`dashboards/alerts/grafana/usher.yml` stays unloaded** (*my call*). It holds
  `Cost anomaly` and `Disk projection`'s database half, both through the
  `usher-postgres` datasource, and in this stack that reads the dev database. Loaded
  today, they would judge the wrong database.
- **⏳ Where an alert lands is the owner's answer.** Alertmanager's webhook body is
  fixed, so the destination sets the work. A Home Assistant automation with a webhook
  trigger can notify a phone with no new service. Signal through `signal-cli` needs a
  small translator. Alfred's push needs a new Alfred endpoint. The recommendation is
  Home Assistant.

**The rules come from the deploy, not from a checkout** (*my call*). The header's
mount, `../usher/dashboards/alerts`, reads `~/code/usher`, which nobody pulls and which
last moved on 2026-09-24, so it would load old rules with no `Sync stale`.

- The deploy workflow publishes the file to `~/code/usher-deploy/alerts/`, beside the
  compose files it already copies, and the mount reads from there.
- It runs `promtool check rules` first. Prometheus refuses to start on a bad rule file
  and this stack is shared with Alfred, so a file that fails stops the copy and is
  reported, and does not fail the deploy.
- It then signals Prometheus to re-read its rules, and a Prometheus that isn't there
  does not fail the deploy either.
- This is Usher's one PR for the slice, and it lands first, so the directory exists
  before the mount does. The rule file's header gets the new mount.

**Where the wiring lives** (*my call*). `~/code/observability` is a local repository
with no remote, shared with Alfred, and its working tree holds another session's
uncommitted changes, which its live Grafana is already running. A worktree cannot bring
that stack up: the mounts are relative, `.env` is gitignored and `name: observability`
would replace the live containers.

- A new `compose.override.yml` there, which compose reads by itself, holds the rule
  mount and the Alertmanager service, so the dirty `compose.yml` stays untouched.
- That file, `prometheus/prometheus.yml` and the Alertmanager config are committed on
  `master` by explicit path, which leaves the other session's work alone.
- `docker compose up -d --no-deps prometheus alertmanager` from that directory touches
  only those two. Restarting Prometheus interrupts Alfred's telemetry for a moment, so
  that step needs the owner's OK.

### Slice 4: the versions pass runs by itself

The every-version spec's "What it does" and "What the port needs" stand. This replaces
its "How it runs".

**A scheduled job, `sync.versions`**, not an operator's command and not a new lane
(*my call*). Each time it is offered, once per tick, it reads the next
`USHER_VERSIONS_READS_PER_TICK` rows of each enabled source, at most 8 at a time.
The default is 0, which leaves it unregistered, because a fresh install must not send
a million reads at anyone's server unasked. `usher schedule --once` runs one chunk by
hand, so no separate command is needed.

- **It keeps its place.** A position per source commits in the same transaction as
  what the chunk created, so a kill costs at most one chunk's reads. At the end of a
  round it starts again from the beginning, and one INFO line gives the round's counts.
- **"Does not hold" means no available row**, so a version that is held but retracted
  is healed too.
- **One bad row cannot stop it, and an outage cannot hold it up** (*my call*). A 404,
  an unusable answer or an unreachable read is counted and skipped. Sixteen unreachable
  reads in a row (twice the concurrency) end the chunk there, instead of spending it on
  timeouts: the position moves past them like any skipped row, what it created is kept,
  and the job raises, so the failure is counted. The job's period is one tick, so the
  scheduler offers it again next tick. At worst an outage costs sixteen rows a tick,
  about 4,600 a day or 0.4% of the library, and the round's line counts them so that the
  next round reads them. There is no stuck state and no probe. This replaces the
  every-version spec's retry schedule.
- **A rejected login moves nothing and holds that source's pass for a day**, stored
  with its position, so a pass cannot add a failed login every five minutes.
- **Beside everything else,** it retracts nothing, and it is never a delta's cursor.
  It and `sync.full` share the scheduler's task, so they never overlap there.
- **Visibly:** rounds, position and counts in `usher sync-status`, and
  `usher.sync.repairs` with `kind="version_created"`.

**How fast** (⏳ the steady rate is the owner's answer, and 300 a tick is the
recommendation):

| | reads per tick | what the server sees | one round of about 1.2 million |
|---|---|---|---|
| the first round, on the owner's go | 16,000 | about 5 minutes of reads, then 5 minutes' rest | about 13 hours, plus any neighbour rebuild it waits behind |
| afterwards | 300 | about 6 seconds of reads every 5 minutes, about 86,000 a day, or about 95 times a daily walk's 910 | about 14 days |

The first round is the one that matters: it finds the estimated 900. After it, a new
version reaches Usher through push or a delta, so the pass only catches what those
missed. When the second round ends, its counts decide whether to lower the rate or
stop (*my call*).

**Why not fewer reads.** No listing names a group's other versions or says how many
there are, so only a by-id read can find them (the every-version spec's measurements).

### ⏳ The presence pass the owner approved on 2026-10-19

**This is the owner's answer.** On 2026-10-19 the owner approved an id-only pass after
every push reconnect, plus nightly: removals and counts only, in about 118 requests.
`docs/specs/2026-10-19-adaptive-page-size-design.md` calls it slice 2b, to follow with
its own spec, and the owner's approval is needed at each stage. That was before walks
fell from about 4,000 s to about 655 s, and before this design made them daily.

- **What changed.** A daily sweep now bounds a removal to about a day, and slice 1
  shrinks a lost socket's gap to minutes.
- **What the pass would still add.** A removal seen within minutes of a reconnect, for
  about 118 requests against the walk's about 910.
- **The recommendation:** hold it, and let `usher.sync.repairs{kind="retracted"}` over
  slice 2's first weeks say whether reconnect gaps matter. If they do, it gets the spec
  it was promised. This design builds nothing for it either way.

## Testing

TDD, with the repository's gate green before every commit.

**Slice 1.**

- A fake channel fails past the ceiling and then delivers: `run` did not return,
  `supports_push` went false once and then true, the delta ran, and the wait between
  attempts reached the maximum and stayed there.
- A channel failing 2,000 times raises nothing, which fails without the capped
  exponent.
- `SourceNotSupported` returns, and no refresh restarts it.
- A rejected login does not return: no second login reaches the fake until a day has
  passed by a fake clock, the gauge's value reads 0 meanwhile, and a good login after
  the day delivers.
- A lane that crashes is named by `crashed_sources`, reads 0 on the gauge through the
  spacing, is not restarted by a refresh inside the spacing, is restarted by one past it
  on a new adapter, counts once, and stops being named.
- `usher set-credentials` overwrites the stored row so that a new adapter logs in with
  the new password, keeps the source's id and its rows, refuses a missing variable and a
  value given in place of a name, and prints neither value on any path.
- With two lanes, the second keeps ingesting after the first crashes and again while
  the first restarts. The assertion is that it ingested, not that it was running.
- A disabled source's crashed lane does not come back.

**Slice 2.**

- `last_done`: the oldest of the newest completed runs. A failed or running run does
  not count, a disabled source is ignored, a source with no run makes it due, and no
  sources means not due.
- `run`: a due source is walked once and a source walked recently is not. With two due,
  the older goes first, and the first's failure still walks the second and then raises.
  `WalkRefused` alone answers `DECLINED`. The watch lane runs as `sync_handler`'s does.
- A failed walk is not offered again on the next tick, with a fake clock, and is at the
  backoff.
- A rejected login records its code, is not tried again inside the period, including
  across a new job object, and is tried once the period has passed.
- A cancelled walk leaves a run that resumes, on real Postgres.
- The sweep, on real Postgres: a retracted row still served comes back with its own
  description and is counted `restored`; a retracted row gone stays retracted; neither
  moves the retract fraction's verdict; a row a push retracted after the source answered
  stays retracted, as it does today.
- An unmatched episode queues no `match` job, an unmatched movie and an unmatched series
  each still do, and an episode whose series has since matched attaches on the next walk.
- The counter's values are asserted for each kind, not its presence.
- The gauge rises between ticks with no database call, by a fake clock.
- The three scheduler instruments carry `scheduled_job` and not `job`.

**Slice 3.**

- The workflow's publish step, run by hand against a planted bad rule file: the file is
  not copied, the step reports it, and the deploy would go on. The plant's presence is
  asserted first.
- The same step with the good file: the copy equals the repository's, and Prometheus
  lists its rules after the signal.

**Slice 4.**

- The every-version spec's slice 2 cases, except its retry and failed-run cases, plus:
  a single unreachable read is skipped; sixteen in a row end the chunk with its position
  past them, having made no more reads than those and what was already in flight, and
  raise; the next chunk starts after them and the round's line counts them; a rejected
  login holds the
  source across a new job object, then tries again after the day; a kill between chunks
  reads nothing committed twice; a held but retracted version named by a read comes
  back; a position past the last row starts a new round; the reads per tick and the
  concurrency bound hold, the latter by observed overlap of recorded intervals.

## Acceptance

Live, on production, with raw output posted on each PR. Against Shared Emby that means
GETs only, plus the login each `/play` makes.

**Slice 1**, at the next natural outage, of which there were three in 16 days. This is
the one acceptance that has to wait, because forcing an outage on production would cost
more than waiting.

- The series reads 0 and returns to 1, with no deploy between.
- The delta ran, and `supports_push` went false, and true again by the first mapped
  event afterwards.
- The slice's rows read landed, with acceptance pending, until then.

**Slice 2**, over its first walk and the night after. The bars are set here, before any
number exists:

- A completed `full` run nobody started.
- `/health/ready`'s p99 over the walk's window stays under **1,000 ms**.
- `/play` probed once every 30 s through the walk stays within **1 s** of its median
  outside the walk.
- The 15 retracted rows still served read 0 afterwards, by `ev-retracted`'s count.
- `usher.sync.repairs` and `usher.sync.full.age` read real numbers, and the age falls at
  the walk's end.
- Prometheus shows `scheduled_job` with each job's own name.
- The server's memory through the walk, read and posted. The container has no limit, so
  this has no bar.
- The second night's counts, read and posted, whatever they are.
- The day's requests on Shared Emby, set against the estimate of about 910.
- A week of `usher.match.result`'s `provider_search` counts, read and posted, for the
  question of how often to re-ask.

If either latency bar fails, the walk moves to `usher schedule` in a container of its
own, with `USHER_SCHEDULER_ENABLED=false` on the server, and the bars are read again.
That needs the compose service and the workflow change above, as a small PR of its own.

**Slice 3.** Prometheus lists the rules and one active Alertmanager. A test alert posted
to Alertmanager's API reaches the owner, who confirms it. What fires on day one is read and posted, and the
first real firing of each alert gets a ruling: keep it, tune it, or take it off the
route. Parked jobs (25,990 `enrich`) may well fire `Jobs parking` or `Enrichment SLA
missed` at once, and that needs its own ruling.

**Slice 4**, after its first round:

- The round's counts, set against the estimate of 500 to 1,500.
- A re-run of the sample finds no unheld version created before the round began.
- `/health/ready` and `/play` over a chunk, against the same bars as slice 2.

## Rollout

1. Slice 1's PR. Merging deploys, and is the owner's.
2. Slice 2's PR. **Merging it is the go for daily walks**, because production's scheduler
   is already on. The first walk starts on the first tick at least 24 hours after the
   last one finished, which is the deploy's own first tick if a day has already passed,
   so the latency probes start before the merge. A later merge that lands during a walk
   stops it, and that walk resumes.
3. Slice 3's Usher PR, which publishes the rule file. Then the commit on `master` in
   `~/code/observability`, and the owner's OK to restart its Prometheus.
4. Slice 4's plan and PR. The first round on the owner's go, with
   `USHER_VERSIONS_READS_PER_TICK=16000` in `~/code/usher-deploy/.env`, then the steady
   value.

PRD changes ride each slice's commits, and `docs/plans/progress.md` and
`docs/prd/README.md` carry the status tables.

## Out of scope

- **Rows Usher holds and cannot match:** 6,199 of 1.2 million. They stay in the
  unmatched queue, and slice 2 changes only which of them cost a read.
- **Parked jobs:** 25,990 `enrich` and 18 `watch_history`. They are not drift from the
  source, and they get their own look.
- **Moving walks off the server pre-emptively.** Slice 2's bars decide that from data.
- **A lane picking up changed credentials by itself.** `usher set-credentials` and a
  restart do it.
- **A way to disable a source.** Nothing ships one, and nothing here needs one.
- **`Cost anomaly` and `Disk projection`'s database half**, until Grafana has a datasource
  that reads production.
- **`/play`'s latency**, a median of 1.76 s against about 0.2 s for the by-id read inside
  it, seen in passing.
- **Any source other than Emby** gets the walk and the sweep unchanged, since both go
  through the port. The pass needs only the port concept the every-version spec names.

## Honest caveats

- **No walk has shared the server's event loop.** A listing page of 8,000 items parses on
  that loop, so stalls are plausible, and the size is unknown until the first one. Slice 2's
  bars exist for this.
- **Nobody hears anything until slice 3.** Slices 1 and 2 heal silently before then.
- **Whether the shared server locks accounts is unknown.** The day's wait limits what the
  lane, the walk and the pass add, but `/play` still tries a login each time a person
  presses it.
- **A one-off 401 costs up to a day of push.** The delta and the daily walk cover that day,
  and `Push down` fires.
- **Slice 1's proof waits on chance.** Its logic is tested, but the live proof needs an
  outage to happen.
- **One source's failing walk spaces all sources' walks**, by at most a period, since the
  scheduler spaces a job and not a source. Production has one source, so this is stated
  rather than solved.
- **A row can come back wrongly for a day.** If a source drops a retracted row in the moment
  between the sweep's read and its commit, the sweep restores it, and the next night's sweep
  retracts it.
- **The retracted set only grows**, and the sweep asks about all of it, so the cost creeps
  up: 17,948 rows now. An age limit waits until it matters.
- **A daily walk moves a delta's cursor.** A hidden version added while only the socket is
  down, before a walk that then completes, is named by neither that walk nor the reconnect's
  delta. Slice 4 finds it within a round.
- **The steady rate is a guess.** 300 reads a tick is cheap for Usher, and its effect on a
  server the household does not run has not been seen. The second round's counts say whether
  it earns its keep.
- **The 900 is wide**, resting on 15 versions in 19,563 items.
