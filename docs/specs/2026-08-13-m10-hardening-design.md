# M10 — Hardening — Design Spec

**Date:** 2026-08-13
**Status:** Awaiting review
**PRD:** [`docs/prd/`](../prd/README.md) — authoritative for *what and why*.
This spec is the point-in-time design for M10, scoped for an implementation
plan.

M10 is the last milestone of v1. [09](../prd/09-roadmap.md)'s table gives it one
line — *"Observability, failure modes, backup/restore, docs, public release"* —
and its doc-level marker records that **M10 has no plan** (`09-roadmap.md:8`).
`docs/plans/progress.md:25` says the same: `| M10 | Hardening + dashboards | — |
not planned |`. This spec is what that line was waiting for.

## Goal

**Make Usher something its author depends on and a stranger can install.**

Two success conditions, and they are different claims:

1. *You* point a client at Usher instead of Emby and stop thinking about it —
   because when it breaks you can see why, and when it dies you can restore it.
2. *A stranger* clones the public repository, follows the README, and reaches a
   working catalog without hammering a server they do not own.

## Why this milestone assembles rather than retrofits

The original design predicted this shape:
`docs/specs/2026-07-28-usher-v1-design.md:165` — *"M10 assembles the dashboards
over data that is already flowing, rather than retrofitting instrumentation at
the end."* That prediction held. [10](../prd/10-telemetry-and-dashboards.md)'s
metric catalogue (`:136–177`) is **34 rows and every one is ✅**, each shipped by
the milestone that owned it, and M2's plan restates three times (`:5389`,
`:5678`, `:5918`) that instrumentation was deliberately not deferred here.

So M10 owes almost no instrumentation. What it owes is everything that consumes
it, plus everything an operator needs when it goes wrong.

## Four phases

M10 runs as four gated phases rather than two tracks. The ordering is forced by
two findings, both established while scoping this spec on 2026-08-13:

**The observability stack does not exist.** `10-telemetry-and-dashboards.md:904`
places it externally at `~/code/observability/`, shared across the host and
deliberately not a service in Usher's `compose.yml`. That directory is absent,
no Grafana/Prometheus/Loki/Tempo container runs on this host, and
`.env.example:290` ships `OTEL_EXPORTER_OTLP_ENDPOINT` **empty** — the service
name beside it at `:291` is set, which is the detail that makes this easy to
miss. **Usher's telemetry has exported to no-op exporters for nine
milestones.** Nothing downstream can be verified until this is fixed,
and issue #13 cannot be honestly closed at all.

**The repository is already public.** It was created public on 2026-08-02
(`visibility: PUBLIC`, MIT) with no tags. The safety cluster is therefore not
release-blocking work to be done before a future publication — it is **live
risk today**. Anyone may clone Usher now and point it at a shared Emby with no
outbound rate limit and an unbounded delta walk on startup.

| Phase | Name | Contents |
|---|---|---|
| **0** | Foundation | The observability stack; Usher exporting to it for real; semconv names pinned |
| **1** | Safety | #19, #9, #13, #20 — being polite to a server you do not own |
| **2** | Operability | Dashboards, alerts, backup/restore, rotation, runbooks, the scheduler, failure-mode gaps, operator bugs |
| **3** | Release | Version wiring, changelog, first tag, image publish, community files, README truth-telling |

Phase 3 is droppable without invalidating 0–2. Phases 0 and 1 are not droppable
by anything.

## Scope

### Phase 0 — In

- `~/code/observability/` as its own compose project: **Grafana, Prometheus,
  Loki, Tempo, and an OTel collector**. It is infrastructure, not an Usher
  service — `10-telemetry-and-dashboards.md:906` decided this and M10 does not
  reopen it. Usher's only coupling stays the two environment variables it
  already has.
- Usher pointed at it, with a real span, a real log line carrying a `trace_id`,
  and a real metric sample observed in Grafana from the live catalog.
- **The semconv convention pinned and recorded.**
  `10-telemetry-and-dashboards.md:191–199` warns that
  `OTEL_SEMCONV_STABILITY_OPT_IN=http` silently renames and re-units
  `http.server.duration`. Nothing sets it today. Phase 0 records which
  convention is in force and asserts the names Usher emits against the
  catalogue's 34 rows, so no panel in Phase 2 is authored against a name that
  is not on the wire.

### Phase 1 — In

Four issues, one theme, each verified against the **live shared Emby** in
bounded requests with no walk — the discipline M9's H4/H5 used.

| Issue | Surface | Shape |
|---|---|---|
| **#19** no outbound rate limiting | `src/usher/adapters/http.py` (207 lines) | The module's own docstring claims "rate-limit handling", and what it has is **reactive**: `port_error_for` (`:144`) turns a 429 into `PortRateLimited` and `retry_after_seconds` (`:71`) parses the header. Nothing is **proactive**. Phase 1 adds a per-source limiter so Usher does not earn the 429. The deployment target is a shared Emby owned by someone else at ~1–3 s/request, and W1's concurrency 4 is the first real parallelism aimed at it. |
| **#9** unbounded startup delta walk | `src/usher/api/lanes.py:401 _close_gap` | PRD 03's reconnect delta walks with no ceiling and nothing warns the operator. Gains a bound and an operator-visible signal. Found by M9's H4/H5. |
| **#13** concurrency are bounds, not measurements | `src/usher/services/jobs.py:166 KIND_CONCURRENCY`, resolved at `src/usher/composition.py:833` | Two entries from W1 are bounds presented as numbers. **Measured** against Phase 0's stack at the real source latency, and written with its denominator. |
| **#20** retraction guard assumes an owned library | `src/usher/config.py:217 sync_max_retract_fraction`, guard at `src/usher/ports/repository/media_item.py:136` | ADR-0015's 0.25 default assumes you control the library. Validated against a shared server, where a library you do not control may legitimately shed items. |

**#13 is why Phase 0 precedes Phase 1.** This project's named recurring failure
mode is a bounded measurement restated as an absolute one hop up the document
chain (`09-roadmap.md`, and it has cost a task and a milestone before). Closing
#13 without the instrument that measures it would commit that error knowingly.

### Phase 2 — In

**Dashboards and alerts.** Five dashboards as provisioned JSON in this
repository (`10-telemetry-and-dashboards.md:791`) and seven alerts (`:928–936`:
Ingest stalled, Push down, Jobs parking, Enrichment SLA missed, Provider
degraded, Disk projection, Cost anomaly). No panel is committed before it is
observed rendering real data. **D1 and D2 are audited first**: D3, D4 and D5
carry per-panel backing statements in PRD 10 and all are ✅; D1 and D2 carry
none, so a missing series there must surface as a Phase 2 task rather than as
an empty panel at the gate.

**Instrumentation gaps the dashboards expose.**

- **No metric series exists for the suggest path at all**
  (`10-telemetry-and-dashboards.md:213–222`) — a gap named there rather than
  left to be discovered from an empty panel. The gate measured p50 33.6 ms,
  p95 211 ms, max 730 ms.
- **No `propose` span** (`:113–117`), which D4's home-composition panel wants.
- **`llm_calls` has no read method and no index beyond its primary key.** M8
  withheld both deliberately until a reader existed
  (`docs/plans/2026-08-06-m8-curation.md:803`; `02-data-model.md:699` names the
  two right indexes in `m08a`'s docstring). The withholding is enforced by
  `test_the_cost_ledger_has_no_read_method`. **That guard retires in the same
  task that adds the read** — a guard left standing against a shipped reader is
  worse than either state.
- **D5's "cost per play attributed to an LLM row" is ⏳-marked M9**
  (`:896–898`). It needed `search_queries.played`, which M9 shipped. The marker
  is stale and collectable.

**The PRD 10 amendment.** `GET /search/suggest` writes no `search_queries` row
on either tier (`:595–626`). PRD 10 names two fixes and says they are *"named
here so M10 plans one rather than rediscovering the choice"* (`:608–610`).
**M10 takes the column, not the enum** — see *Key design decisions*. Bundled
with it: `search_queries` has **no retention job anywhere in `src/` and no index
on `at`**, so an operator's pruning `DELETE` is a sequential scan (`:638–648`).

**Backup and restore.** `usher backup` and `usher restore` as CLI commands,
inside ADR-0026's error boundary. PRD 08's rebuildable/precious split
(`08-operations.md:581–583`) becomes **a manifest in code**, with a test
asserting every table in the database is classified one or the other.

**Secret rotation.** `USHER_SECRET_KEY` rotation, documented at
`08-operations.md:232` and assigned to M10 by name in M3's plan
(`docs/plans/2026-07-30-m3-emby-adapter.md:7926`). `build_cipher` was made
public so a rotation tool could hold two ciphers at once; the seam exists and
has no caller.

**Failure-mode gaps.**

- **The `supports_push = false` degradation never fires**
  (`08-operations.md:298`): the counter resets on *delivery*, not on
  *connection*, so a documented degradation path is unreachable.
- **Orphaned claims produce nothing in `/health/ready`.** W1 shipped the lease,
  `touch()` and `recover()`; the operator still cannot see the condition.
- The image proxy's honest 502 has **no code in ADR-0030's closed vocabulary**
  (`:301`).

**The scheduler — the one genuinely new component.** Issue #17 needs something
to run `usher similar --rebuild`, and **nothing in `src/` schedules anything**.
The rebuild costs 594.7 ms/seed, a **21.6-hour** full walk at `halfvec(1024)`,
so this is an overnight job with a resumption story, not a cron line. It has a
second customer immediately — the retention above — so it ships as one bounded
component with two registered jobs.

**Operator bugs.** #5 (`usher unmatched --resolve` stack-traces 60 frames on an
unknown `--title`; `RepositoryConflict` is not in `cli.OPERATOR_ERRORS`) · #6
(`/openapi.json` advertises `application/json` for problem responses while the
wire sends `application/problem+json`; the fix forks assertions in
`test_api_playback.py` and `test_api_watch.py`) · #10 (45 bounded columns leak a
raw driver exception across the port boundary; 31 go through `stage_records`'
`copy_records_to_table` on the raw asyncpg connection — needs the scoped
decision M9 declined as its boundary call 8) · **#7**, where the honest fix is
making the ordering premise a **fact** via a session log. The debt entry warns
against the tempting fix: `.github/workflows/ci.yml:46` runs the whole suite
with no deselection, and deselecting there would mean the first CI red arrives
with nothing saying it is known.

**#8 — a diagnosis task, not a fix task.** `usher work` can die on an unhandled
`MissingGreenlet` and **the cause is unknown**. W1 removed the operational
damage — the lease and `recover()` return stranded claims — so what remains is a
crash that self-heals. The task's exit condition is **a cause**, because a task
promising a fix would be promising an unknown. Phase 0's stack is the
instrument: a crash that leaves a trace and a correlated log line is a different
problem from one that leaves nothing. If the cause is found and the fix is
small, it ships here; if it is large, it becomes an issue with a *known* cause,
which is strictly better than today.

**Runbooks.** `docs/runbooks/` — restore, upgrade, disaster recovery, rotation.
None exist; there is no `docs/runbooks/` and no `docs/ops/`.

### Phase 3 — In

- **Version wiring.** `pyproject.toml:3` reads `version = "0.1.0"` and
  **nothing consumes it** — no `__version__`, no `--version`, nothing in the API
  surface. Wired through and surfaced on `/health` so a running instance can be
  identified.
- **Release machinery.** `CHANGELOG.md`, a stated semver policy, **the first git
  tag** (`v0.1.0`; there are none today), and a tag-triggered CI job that builds
  and publishes the container image. `.github/` today contains exactly one
  46-line workflow with a single `check` job: no release job, no tag trigger, no
  container build or push, no artifact upload, no issue or PR templates.
- **Community files.** `CONTRIBUTING.md`, `SECURITY.md`, issue and PR templates.
- **README truth-telling** — four statements it does not currently make:
  1. **Backup policy.** `08-operations.md:585–588` says to state the
     rebuildable/precious asymmetry loudly in the README. `grep -ni backup
     README.md` returns nothing.
  2. **The unauthenticated-admin posture.** Every admin route is
     unauthenticated, including `POST /admin/bootstrap/{phase}` and
     `POST /admin/sources/{id}/sync`, which `08-operations.md:406–412` notes can
     block the queue for hours. For a LAN-local self-hosted product this is a
     documented posture, not a defect — but a stranger will not infer it.
  3. **The playback-token caveat** (`08-operations.md:242–264`): a `direct`
     target URL carries the source's session token, so the grant outlives the
     response that carried it. M9's ticket made the artifact opaque, not the
     grant narrower.
  4. **TMDb's logo obligation.** README `:697` correctly identifies it as a
     client obligation; a stranger building a client will not read that line.
- **PRD 04 gains the git-history paragraph** — see *Licensing*.
- **A quickstart a stranger can follow**, clone to working catalog. Following it
  start to finish on a clean checkout is Phase 3's exit condition.

### Out — and why

**Seven open issues stay out**, because including them would make M10 a feature
milestone wearing a hardening label:

- **#11 (T5/T6 — the IMDb credits parsers and writer)** and **#12** (the
  `(title_id, source)` index that rides on it). #11 is flagged as the next
  substantive piece of work and it is real work — it is not hardening.
- **#21** (RRF scores partial coverage as relevance) — a genuine search-quality
  bug, and a ranking change, not a hardening one.
- **#15 / #16** (query expansion measured on one model and one 150-document
  corpus; and expansion billed on searches the semantic lane cannot serve).
  `09-roadmap.md:1112` scopes these **post-v1 unless `search_queries` supplies a
  real evaluation set**, and that table has no rows from real use yet.
- **#14** (a covering index for `GET /admin/unmatched`, measured at 16.4 ms and
  declined) — the decline still holds.
- **#18** (API-key auth for `POST /admin/sources`) — an enhancement.

**Authentication stays out.** It is M9's boundary call 1 and sits under
*Post-v1 candidates* (`09-roadmap.md:1335`). M10 **documents** the posture; it
does not build auth. Shipping auth would also invalidate the "no auth yet"
reasoning behind the version number below.

**Resource limits stay advisory.** `08-operations.md:643–674` carries a red
warning that its table is sizing estimates for an operator provisioning a disk,
that nothing reads them, no host enforces them and no policy derives from them.
M9's Track 2 misused it as a budget and withdrew a design (ADR-0036). M10 does
not repeat that: no cgroup limits are added to `compose.yml` on the strength of
that table.

## Architecture

M10 adds **one new component to `src/`** and one new repository outside it.

**The scheduler** (`src/usher/services/`) — a bounded component that runs
registered long-period jobs. Two registrations at birth: the neighbour rebuild
(#17) and `search_queries` retention. It is deliberately *not* a general cron:
its contract is a small set of named jobs with a period, a resumption story, and
observability, because the only two customers that exist both take hours and
both must survive a restart. It sits behind a port, like everything else that
crosses a boundary here, and the import-linter contracts extend to cover it.

**`~/code/observability/`** — its own repository, outside Usher, holding the
Grafana/Prometheus/Loki/Tempo compose project.
`10-telemetry-and-dashboards.md:904` already decided this placement. Note the
asymmetry PRD 10 sets up and M10 honours: **the dashboards are an asset of
Usher's repository; the stack that renders them is not.**

Everything else in M10 extends existing surfaces: `adapters/http.py` for the
limiter, `cli.py` for backup/restore/rotate, `api/lanes.py` for the gap bound,
`api/analytics.py` and a migration for the `search_queries` amendment,
`telemetry.py` for the suggest metrics and the `propose` span.

## Key design decisions

**1. The stack comes before the safety cluster.** #13 is a bound presented as a
number, and the only honest way to close it is to measure it. Phase 0 is small
and unblocks Phase 1 completely.

**2. Backup is a CLI command with the manifest in code, not a documented
`pg_dump`.** PRD 08's literal wording suggests the latter, and the reason for
departing is that the prose version **has already drifted**: `:638–641` says row
provider enable/disable *"belongs in the precious column the day it exists"*, M9
shipped `row_provider_settings` in `m09a`, and the table was never updated. A
manifest with a test — every table is classified rebuildable or precious —
makes the next drift a build failure instead of a silent data loss. `llm_calls`
is the case that makes this worth paying for: `08-operations.md:608–615` calls
it *"the first thing in this project that is not rebuildable from anything, at
any price."*

**3. The `search_queries` amendment takes the column, not the enum.**
`SearchMode`'s three members describe **how a query was answered** — lexical,
semantic, fused. A `SUGGEST` member would describe **where it came from**, which
is a different axis, and it still could not express suggest's two tiers without
a fifth member or a second field. So: a `surface` column (`search` | `suggest`)
and a `tier` column (`prefix` | `fuzzy` | null), one migration, with a default
for the rows M9's F2 has already written. That migration is also the natural
place for the missing index on `at`, which the retention job needs.

**4. The scheduler is one component with two jobs, not two timers.** Both
customers arrive in the same phase, both take hours, and both need the same
resumption and observability story. Two ad-hoc timers would be the shape that
grows a third.

**5. `git history` is documented, not rewritten** — see *Licensing*.

**6. The release is `v0.1.0`, not `v1.0.0`.** [09](../prd/09-roadmap.md) frames
M1–M10 as *"v1 — the abstraction works end to end"*, and that success condition
is met. But **"v1" there names a scope milestone and "1.0.0" in semver names a
compatibility promise**, and those are different claims. The second is not true
yet: there is no authentication, one source adapter, and seven open feature
issues that will move the surface. `0.x` lets the API change while real use
teaches what it should be; `1.0.0` remains available the day it is meant.
README `:10`'s **"Pre-release."** becomes **"Beta."**

## Data model

One migration, `m10a`: two columns on `search_queries` (`surface`, `tier`) with
a backfill default for existing rows, and the index on `at` that retention
needs. Plus the two `llm_calls` indexes named in `m08a`'s docstring, which ship
with the read method and the panel that reads it.

No other schema change. In particular, **backup and restore add no tables** —
the manifest is code, and the classification test reads the live schema.

## The gates

Each phase ends with the standard gate — `ruff check`, `ruff format --check`,
`mypy`, `lint-imports`, the unit and integration suites, the PRD link check —
plus one thing only that phase can prove:

| Phase | Phase-specific gate |
|---|---|
| **0** | A real span, a real `trace_id`-carrying log line, and a real metric sample observed in Grafana from the live catalog. The semconv convention recorded. |
| **1** | All four fixes verified against the live shared Emby, in bounded requests, with no walk. #13's number written with its denominator. |
| **2** | Every committed panel observed rendering real data. The restore drill succeeds into a scratch `pgvector/pgvector:pg17` container. |
| **3** | The quickstart followed start to finish on a clean checkout. |

A mutation sweep per phase, logged to `.claude/rules/mutation-sweeps.md`.

## Build sequence

1. **Phase 0** — stack, exporters, semconv. *Gate.*
2. **Phase 1** — #19, #9, #13, #20. *Gate.*
3. **Phase 2** — D1/D2 audit first (it can generate tasks), then the
   instrumentation gaps, the amendment migration, the scheduler, backup/restore,
   rotation, the failure-mode gaps, the operator bugs, #8's diagnosis, the
   dashboards and alerts, runbooks. *Gate.*
4. **Phase 3** — version, changelog, community files, README, PRD 04, image
   publish, quickstart, tag. *Gate.*

Within a phase, implementers are **serialised** — a mutation sweep is
invalidated by any concurrent edit anywhere the suite imports, not only by an
overlapping file. Reviewers may run concurrently against a frozen `git archive`
copy.

## Acceptance criteria

- Grafana renders five dashboards from provisioned JSON in this repository, and
  every panel was observed with real data before its commit.
- Seven alert rules exist and each has been fired at least once, deliberately.
- `usher backup` produces an artifact that `usher restore` loads into an empty
  database, verified in a scratch container.
- A test fails if any table in the schema is unclassified by the backup
  manifest.
- `usher` has a documented secret-rotation command with a caller and a test.
- The ten in-scope issues (#5, #6, #7, #8, #9, #10, #13, #17, #19, #20) are
  closed or, for #8, carry a **known cause**.
- `git tag` shows `v0.1.0`; a container image is published from that tag; the
  README states backup, the admin posture, the token caveat and the TMDb logo
  obligation.
- The quickstart has been followed on a clean checkout.

## Testing

Unchanged in method from M8 and M9, because it is what has been catching
things: **subagent-driven** — an implementer per task, then a spec-compliance
review, then a code-quality review, each dispatched fresh with the full task
text rather than a pointer to the plan. **Reviewers are told not to trust the
implementer's report and to re-measure.**

Two test shapes are new to this milestone:

- **The backup classification test**, which reads the live schema rather than a
  list, so it fails on a table added in M11.
- **Panel verification**, which is not a unit test — it is an observation
  recorded beside the panel, with the query and the data it returned.

Measurement tasks follow the house rule: write the bar *before* running it
(`/tmp/m10-gate/BAR.md`), run live, report guess-by-guess with refutations
first, and commit the measurement rather than the data.

## Risks and open items

**The stack is new work outside this repository.** Phase 0 is small but it is
setup against a host with an RTX 4090, existing containers on published ports,
and an internet-facing nginx-proxy-manager. Port collisions and exposure are
real; the stack must not be published to the internet.

**D1 and D2 may generate tasks.** They carry no backing statements. The audit is
scheduled first precisely so this lands as a Phase 2 task list rather than as a
gate failure, but Phase 2's size is not fully known until that audit runs.

**#8's schedule is unknown by construction.** It is scoped as a diagnosis with a
cause as its exit condition, and it sits in Phase 2 rather than Phase 1 so it is
not on the critical path for anything.

**#10 needs a scoped decision before it can be a task.** M9 looked at it and
declined (boundary call 8). The candidate fix — declaring staging columns wide
(`bigint`, `text`) so refusal moves to the `INSERT … SELECT` — is a design, not
a patch, and the plan must scope it before an implementer sees it.

**The similar rebuild is 21.6 hours.** The scheduler's first registered job
cannot be exercised end to end inside a normal task. It is verified against a
bounded seed subset with the full walk run once, overnight, as its own task.

**TMDb's cache ceiling has no panel.** `provider_cache_meta` tracks a live
≤ 6-month compliance obligation, and PRD 10 `:880` specifies the panel for it.
That panel is inside D5's scope and must not be dropped if D5 is trimmed.

## Licensing

**Nothing in M10 changes Usher's licensing posture**, which
`04-catalog-bootstrap.md:619–680` establishes and
`tests/unit/test_no_third_party_data.py` enforces mechanically: the repository
and its release artifacts contain zero third-party metadata, and commercial use
is out of scope because TMDb names AI/ML training on their content as
commercial. Phase 3 publishes a container image, and that image copies `src/`,
which the guard test already scans.

**One finding, recorded here and to be written into PRD 04.** The rule was
broken from M1 to M4 and fixed on 2026-08-01 by commit `1196a9d`, whose message
states it plainly. But **the pre-fix commits are reachable on the public
remote**: commit `1162346` is an ancestor of `origin/main` and contains
`tests/fixtures/bulk/title.ratings.slice.tsv` with real tconsts and real vote
counts — `tt0111161 9.3 2900000` — which the fix commit itself identifies as
the most licence-restricted part of that dataset. The repository was created
public on 2026-08-02, after the fix, but the history went with it.

**The decision is to document rather than rewrite.** Two fixture rows are de
minimis against IMDb's non-commercial clause, while this project's engineering
record — 678 commits of measured findings, ADRs and plans, cross-referenced by
hash — is one of its most valuable artifacts. Rewriting every hash to remove two
rows trades a large real asset for a small theoretical risk, and would not
remove them from GitHub's caches or from forks without a support request
anyway. PRD 04 gains a paragraph naming what is in history, which commits, and
why it was left. The decision is reversible; the inverse is not.
