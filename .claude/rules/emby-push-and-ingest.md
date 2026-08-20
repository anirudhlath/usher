---
paths:
  - "src/usher/adapters/emby/**"
  - "src/usher/services/push.py"
  - "src/usher/services/ingest.py"
  - "src/usher/services/matching.py"
  - "src/usher/services/reconcile.py"
  - "src/usher/services/watch_sync.py"
---

# Emby, the push lane, and the ingest pipeline

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

## What a request to this Emby costs, measured 2026-08-15 — and *"~1–5 s/request"* was never about a request

**M10 S1.** `scripts/measure_source_latency.py`, **52 live requests**, sequential,
all `GET`, nothing written, no iterator anywhere in the run. Bar
`/var/tmp/m10-gate/BAR-S1.md`,
`sha256 b0ff82ac4a85db58fd04b1636500427e97ef122bc5b647c3d6b4807ec2f9c23b`,
written 02:35:16Z — before the harness existed and seven minutes before the
first request. **One household, one evening, one network path, one Emby
build.** Every number here is a snapshot with a date on it, not a constant.

**The window, quoted from the artifact rather than around it.** The harness
printed `02:42:42Z -> 02:45:11Z`, which is the **48 reps**; the four warm-ups
came before it and that version of the harness did not timestamp them (it does
now, and prints both windows). **Nothing was sent to the source after
02:45:11Z by this process** — the client was closed on the last response and
everything after it is a local Prometheus read. That is the claim available;
*"the server was idle"* is not, because this run has no visibility into what
else the household was asking of it and no right to assert one.

⚠️ **The version of the harness that produced these numbers is not the
committed one, and the difference is in the reporting rather than the
measurement.** The run was made by the file at `02:41:01Z`
(`/var/tmp/m10-gate/measure_source_latency.py.bak`); the committed harness
adds the warm-up-inclusive two-instrument comparison the 18.11% below made
necessary, prints the warm-up timings and the wider window, persists raw
observations under `--timings-out`, and moves the `--budget 0` guard ahead of
the client. **So re-running the committed script does not reproduce the 18.11%
line** — it reproduces the corrected comparison. The probe plan, the timing
call site and the export are unchanged.

⚠️ **"Emby 4.9.5.0" is carried over from M9's H4/H5 and was not measured
here.** This run never read `/System/Info`; the `verify` probe is
`/System/Info/Public`, whose body was recorded only as a byte count. Same
household, same server, thirty-six hours later — but the version string is an
inheritance, not an observation.

🔴 **The string this repository has cited 22 times and called *measured* 11
times was never measured.** *"Emby is slow (~1–5 s/request observed)"* enters in
`0c823e0` on 2026-07-28 — the **first PRD commit**, two days before
`src/usher/adapters/emby/` existed and before any request had been sent to any
Emby from this project. PRD 01:314 attributed it to *"the old table"*, i.e. to
an earlier revision of itself.

**The grep behind that, scoped and pinned, because the plan's version of it is
false:** `git log -S "1–5 s/request observed" 45398c2 -- docs/prd/` returns
**exactly one** commit, `0c823e0`. The M10 plan states it as `--all -- docs/`,
which returns **two** at `45398c2` (the second being `71c44e5`, the plan itself,
under `docs/plans/`) and **four** across the repo at HEAD. It was already false
in the commit that wrote it, and it was copied here verbatim before being run.
**A grep-checkable claim that nobody re-ran is a claim, and promoting one from
a plan into a rules file means running it first.**

**And the census is 22, not 21.** The plan says 21 — 16 in `src/` across 13
files, 5 in `docs/prd/`. There are **6** in `docs/prd/`: the sixth is
`docs/prd/README.md`'s own M10 row, which quotes the string while describing
this task. *"Called measured 11 times"* survives scrutiny: 12 lines match
`grep measur` beside the string, and the twelfth is that same README row
**denying** it was ever measured, which is not a citation calling it measured.

### The table

Wall clock around `EmbySession.request`, n = 12 per class, warm-up discarded.
⚠️ One exception, because the durable record should carry it rather than only
the module docstring: **`verify` is timed around `anonymous_json`**, which is
what the shipped `verify()` calls and which includes `decode_json` of a
138-byte body — microseconds against 125 ms, and the *only* class whose wall
clock and histogram do not bracket the identical span.

| class | n | median | mean | p95 | max | median body |
|---|---|---|---|---|---|---|
| `verify` (`GET /System/Info/Public`) | 12 | **0.1253 s** | 0.1543 s | 0.4721 s | 0.4721 s | 138 B |
| `get_item` (`GET /Users/{u}/Items/{id}`) | 12 | **0.1495 s** | 0.1649 s | 0.3587 s | 0.3587 s | 13,975 B |
| `list` @ `StartIndex=0` | 12 | **4.5356 s** | 4.6067 s | 4.8355 s | 4.8355 s | 945,131 B |
| `list` @ scattered `StartIndex` | 12 | **7.4155 s** | 7.4670 s | 9.6805 s | 9.6805 s | 819,964 B |
| `list`, both pooled (`op="list"`) | 24 | 5.0954 s | **6.0369 s** | 9.1713 s | 9.6805 s | — |

**The mean is beside the median because the distribution is right-tailed on
every class** (M9 S2's finding): any `Σ` over pages wants the mean.

### What it refutes, and the confirmations after

- 🔴 **"1–5 s" is not about a request, and where it *is* about something it
  **understates** it.** A 200-item page carrying the full `Fields` set costs
  **4.54 s at depth 0 and 7.42 s deep** — the pre-registered prediction was
  `median(list) ∈ [1, 5]` and 5.0954 s falls outside it. The max was predicted
  under 5 s and is **9.68 s**, and **12 of the 24 `list` reps exceeded 5 s** —
  every scattered page and no depth-0 one, which follows from the table (max
  `list@0` is 4.8355 and the pooled median 5.0954 forces min `list@scattered`
  to **at least** 5.3553 -- the 12th-smallest of 24 is <= 4.8355, so the 13th
  is >= 2*5.0954 - 4.8355; a lower bound, not the equality an earlier draft
  wrote) and is confirmed in Prometheus, where the replayed reps read
  `le="5"` = 12 against `le="10"` = 24. *An earlier version of this entry said
  nine; nine was a guess dressed as a count.*
- 🔴 **A single-item read is 34× cheaper than a page**, not "1–5 s".
  `median(list)/median(get_item)` = **34.1** (predicted ≥ 5). M9's H5 read
  0.141/0.142/0.143 s as an *upper* bound on a `get_item`; this measures the
  request itself at **0.1495 s median / 0.1649 s mean**, so H5's figure was
  essentially the request and not the worker pass around it.
- ✅ **The distribution is bimodal by op class**, which is the hypothesis the
  run was written against and declared in the bar rather than after.
- ✅ **`verify` is the cheapest class** — 0.1253 s median, a 138-byte body, and
  it is unauthenticated, so a status screen's cost is a tenth of a second and
  not a fifth of a minute.
- ✅ **Depth costs.** Scattered over depth-0 is **1.63×** (predicted ≥ 1.20).
  ⚠️ **Confounded by design and named in the bar**: `list@0` asks for the same
  page twelve times and is cacheable, `list@scattered` never repeats a
  `StartIndex`. It is depth *and* cacheability at once, so read it as "a real
  walk's pages cost more than its first page" rather than as a clean depth
  coefficient.
- ✅ **Right-tailed everywhere**: mean > median on all three ops.

### The numbers Phase 1 actually needs, derived from the mean

Library **1,134,919 items** on 2026-08-15 (up from 1,126,789 on 2026-08-02 and
1,126,674 on 2026-07-31 — it moves), i.e. **5,675 pages** at the shipped
`page_size=200`.

- **A full walk is 7.3–11.8 hours**, ~9.5 h at the pooled mean. The old
  *"5,634 pages at 1–5 s each"* gave 1.6–7.8 h, so this repository has been
  assuming a walk about **twice as cheap** as it is.
- **4.7–5.4 GB of JSON off the wire**, ~5.0 GB at the mean of the two median
  bodies. The arithmetic, written out because an earlier version of this entry
  said "~4.9 GB": 5,675 × 819,964 B = **4.65 GB**, 5,675 × 945,131 B =
  **5.36 GB**, and 5,675 × 882,547 B = **5.01 GB**. Decimal GB throughout.
  **"~4.9 GB" reproduces from nothing** — it needs 863,436 B/page in GB or
  927,107 B/page in GiB, and neither is a body this run measured. That is the
  whole finding; a second attempt at this entry offered "a GiB/GB slip on
  5.01 GB" as its origin and 5.01 GB is 4.66 GiB, so the story was itself
  unreproducible. **Inventing a derivation for a superseded number is the same
  error one register down: stop at "it reproduces from nothing".**
- **The source yields 33 items/s.** `scripts/measure_ingest.py` measured the
  local pipeline at **1,933–2,135 items/s**, so **the ingest side is ~60×
  faster than the source can feed it** — the walk is entirely upstream-bound,
  and any local optimisation of it is optimising the 1.7%.
- **Single-item ops run at ~6 rps sequentially** (1/0.1649). That is the number
  a source-side rate limiter's default has to be chosen against, not "1–5 s".
- **M5 measured a real `ItemsUpdated` batch at 42 ids**; applied inline at
  `get_item`'s mean that is **6.9 s**, i.e. about one page.
- ⚠️ **A rate limiter on `list` cannot bind *a sequential walk*.** One page at
  a time is already 0.17 rps, so any per-source limit above that never fires on
  the walk this run measured, and a limit below ~6 rps binds only the
  single-item ops. Whether it binds a *concurrent* walk is unmeasured and is
  S7's -- N pages in flight is N x 0.17 rps, which a limiter set for
  courtesy could well reach. The claim is about the sequential case, which is
  the only case this run has.

### Two instruments, and the second one caught a defect in the first's reporting

`usher.source.request.duration` — the histogram `EmbySession._send` has
recorded in a `finally` since M3, and which **nine milestones emitted and
nobody ever read** — was exported over OTLP into Phase 0's Prometheus and
compared against the harness's own `time.monotonic()`.

| op | median agreement | mean agreement |
|---|---|---|
| `get_item` | **0.13%** | 0.67% |
| `list` | **1.17%** | 0.92% |
| `verify` | **1.05%** | 18.11% ⚠️ |

🔴 **The 18.11% is real and it is the harness's fault, not the pipeline's.**
`_send` records in a `finally` on **every** request, including the four the
harness discards from its own statistics — so the histogram held 52
observations against the wall clock's 48, and one extra observation moves a
12-sample mean and barely moves its median. Proven arithmetically rather than
argued: the same 48 timings replayed under a second `service.name` reproduce
the wall-clock mean to **0.001%–0.027%**, and subtracting that series from the
live one leaves exactly **4 observations totalling 14.1825 s** — the warm-up,
whose `verify` leg is 0.5175 s because it is the first request of the run and
carries the TCP and TLS connect. The harness now compares over `warmups +
timings`. **The general form: a discard is a property of the analysis, not of
the instrument, and an instrument in a `finally` cannot be told about it.**

Also worth having: the histogram's own `_count` was **13 / 13 / 26 = 52**,
exactly the budget the harness reported spending. A metric nobody reads is also
an audit of the run that produced it.

⚠️ **The 48 raw observations were not persisted** — that harness printed a
4-dp table and nothing else, so `p95` and `max` in the table above rest on that
print. What Prometheus *can* corroborate, because the fine boundaries are 5%
wide, is each observation localised to one bucket: the largest `get_item` falls
in `(0.352224, 0.369835]` against a printed max of 0.3587, the largest `list`
in `(9.257674, 9.720557]` against 9.6805, and the largest `verify` in
`(0.495614, 0.520395]` — which is the **warm-up** at 0.5175 s, reached
independently by the subtraction above and by the bucket, two routes to one
number. The harness now takes `--timings-out` and writes every observation as
JSON; this run predates it, and no further live request was spent to recover
them.

### 🔴 The shipped telemetry cannot express any of the above, and this is the finding with the widest blast radius

`configure_metrics` (`src/usher/telemetry.py`) installs **no `View`**, so
`usher.source.request.duration` takes the OTel Python SDK's default explicit
bucket boundaries — `(0.0, 5.0, 10.0, 25.0, 50.0, …)` **in seconds**. Every
observation below five seconds falls in one bucket. Measured, by replaying the
identical 48 timings through a provider configured exactly as ship does and
querying Prometheus:

| op | `histogram_quantile(0.5, …)` as shipped | true median | wrong by |
|---|---|---|---|
| `verify` | **2.5000 s** | 0.1253 s | **20×** |
| `get_item` | **2.5000 s** | 0.1495 s | **16.7×** |
| `list` | 5.0000 s | 5.0954 s | 1.9% (coincidence — the true median sits on a boundary) |

So PRD 10 lists this metric ✅ M3 and any dashboard built on it would have
plotted **2.5 s for every sub-5-second operation this project performs**,
identically, forever — and would have read as a working panel. The same applies
to every seconds-unit histogram in the catalogue. Fixing it is a `View` in
`configure_metrics`, is *not* S1's task (S1 is a prose diff over 13 files and
must not be bundled with a mechanism), and is recorded here so whoever builds
Phase 2's dashboards does not build them on this.

### Where the run deviated from its own bar

**The bar is not edited to match the run — that is the one property a
pre-registered bar has.** These are recorded here instead, which is where a
deviation belongs.

- **The bar says the page is priced by calling `EmbySession.json_body`
  directly; the harness calls `EmbySession.request`.** Same wire request, same
  parameters, deliberate: `json_body` is `request` plus `ok()`'s status check
  plus `decode_json`, and decoding a 945 KB page is tens of milliseconds that
  belong to neither instrument. Keeping the decode outside the timed window is
  what lets the wall clock and `_send`'s `finally` bracket as nearly the same
  span as two instruments can — which is the acceptance criterion the bar
  itself sets two paragraphs later. The status check and the decode still
  happen, immediately after the clock stops.
- **The bar's own `View` deviation, declared in it before the run** and
  restated here because it is the reason the numbers are readable at all: the
  harness installs fine geometric bucket boundaries for its export because the
  shipped configuration cannot resolve a sub-5-second median. See the section
  above.
- **`verify` is timed around `anonymous_json`, not `request`** — noted at the
  table.

🔴 **And one correction to how the reconciliation was described, which is a
finding rather than a footnote.** S1's citation diff was reported as *"prose
only, no behaviour"*, proven by every changed `src/` module being AST-identical
with docstrings stripped. **The AST claim is true and "no behaviour" was
false.** `LaneReport` (`src/usher/api/dto/health.py`) is a **pydantic model**,
so pydantic emits its class docstring as the JSON-Schema `description` and
FastAPI publishes it at `/openapi.json` — the edit put a `.claude/rules/…`
path, a `⚠️` glyph and an internal task id into the public API contract.
Repaired by moving the correction into a comment; the published description
now differs from `45398c2` in exactly one of 1,691 leaf nodes and the
difference is the **removal** of the false "1–5 s per request" claim, verified
by regenerating `openapi.json` from a `git archive` of `45398c2` and diffing
the whole document.

**The general form — a `src/` docstring is not automatically prose; on a
pydantic model, a route handler or a `Field(description=…)` it is a wire
artifact, and an AST comparison cannot see the difference — is recorded in
`.claude/rules/api-telemetry-and-lanes.md`, not here.** This file's trigger is
`adapters/emby/**` and five `services/*.py`; that one's is `src/usher/api/**`,
which is where the finding fires. It is cross-referenced rather than duplicated
so the two cannot drift, and the three standing DTO leaks it names are recorded
there too.

### Still unverified, named rather than implied

- ✅ **This server under sustained concurrency** — closed by S7 on 2026-08-19,
  in the section below. Every request in *this* section was sequential, one in
  flight, and nothing in it licenses a concurrency figure; the one that does is
  its own measurement.
- **Any other Emby build**, and any other network path. One household.
- **`op="watch_history"`** — the same route and payload shape as `get_item`
  (`_fetch(external_id, op=…)`), differing only in its telemetry label, but not
  one of the four classes the budget bought.
- **A cold server.** Nothing here flushed Emby's caches, and `list@0`'s
  twelve identical requests are the arm most likely to have been served warm.
- **`POST /Users/AuthenticateByName`**, still never exercised by this project —
  the run installed the operator's existing token.
- **Whether `page_size` trades linearly.** Only 200 was measured, so "halve the
  page and halve the latency" is a guess.
- **This server's version.** Not read in this run — see the note above the
  table. `4.9.5.0` is M9's observation, thirty-six hours old.
- **Whether the server was idle.** The claim available is that *this process*
  sent nothing after 02:45:11Z; what else the household asked of that server
  during the run is unobserved.

  ⚠️ **Two caveats on the `list` spread, and this is the weaker of them.** The
  declared confound is **cacheability** — `list@0` repeats one page and
  `list@scattered` never repeats a `StartIndex` — and it is in the bar, before
  the run. Unobserved household load is a second and lesser one, and the run
  was built against it: `plan_probes` is **round-robin**, so the two `list`
  arms alternate across the whole 2:29 window rather than occupying separate
  stretches of it, and a drift in what the server was doing lands on both
  roughly equally. What it cannot rule out is a burst that happened to fall on
  scattered requests; what it does rule out is a monotonic drift being read as
  a depth effect.

## M9's live verification — it ran on 2026-08-12, and the reason it had not is the first finding

**Both halves passed against the same real Emby 4.9.5.0, and H5 is the first
time this project has written to the operator's account *through the HTTP
surface* and restored it byte-for-byte.** H4 is `POST /titles/{id}/play` → a
minted ticket → `GET /stream/{ticket}` → `302` → a real `206`; H5 is the watch
write-back round trip read back **from Emby**.

🔴 **The premise that stopped them was false, and it was false in eight places
across seven files.** M9 recorded H4/H5 as *did not run* on the ground that *"no
Emby credentials exist on this host — verified rather than assumed"*. What was
verified was `~/code/usher/.env`, and nothing else. The operator's Emby base
URL, access token, user id and device id are in a Home Assistant secrets file
one directory over — which is precisely where `CLAUDE.md`'s live-verification
rule says such a run reads them from. **A negative established by checking the
one place the answer was expected is not a negative**, and it cost a milestone
its two most valuable runs. The eight sites (this file, `CLAUDE.md`,
`README.md`, PRD 09 twice, `docs/prd/README.md`, `docs/plans/progress.md`, and
the M9 plan's final-gate section) are corrected in the same commit as this
entry; the milestone's own reconciliation task counted **five** of them.

**Bounded deliberately: 23 requests to the operator's server in total**, and
**no walk of any kind** — three reachability probes, one filtered listing, one
single-item confirmation, one `get_item` for the ingest, two for H4 (the play
resolution's `stream_targets` read and the `Range` fetch), fourteen for H5's
writes, read-backs and the restore, and one post-teardown read confirming the
account is still restored. The item was chosen by a **filtered**
listing (`IncludeItemTypes=Movie&Filters=IsUnplayed&Limit=25`); the ingest's
bound is in the **iterator**, a `list_items` replaced by a closed one-element
list feeding the shipped `get_item` → `to_source_item` → `IngestService` path.

### H4 — the read half

- **The whole chain works and the claim is bytes.** `POST /titles/{id}/play`
  answered `200` with **two** ranked targets for a single-`MediaSource` movie —
  `direct` then `deep_link` — every `url` an Usher ticket URL.
  `GET /stream/{ticket}` answered `302` with `Cache-Control: no-store`, and the
  `Location` was byte-for-byte the URL `build_stream_targets` builds. A
  `Range: bytes=0-65535` against it answered **`206`,
  `Content-Range: bytes 0-65535/729664590`, `Content-Type: video/x-matroska`,
  65,536 bytes whose first four are the Matroska magic `1A 45 DF A3`.** M3
  measured the URL *as built* answering 206 (ADR-0012); what this adds is that
  the **ticket path does not mangle it**.
- **The absence claim, and its control fired first.** `token_appears_in` was
  pointed at the `302`'s `Location`, where the token **must** be —
  `True` — and only then at the `/play` response body, where it found nothing:
  no `api_key`, no token, no source host. A run whose control found nothing was
  pre-registered as `DID-NOT-RUN` yielding no absence claim; it did not come to
  that.
- **The double percent-encoding candidate does not fire.** This is the specific
  refutation H4 existed to look for. The `deep_link`'s `url=` parameter,
  percent-decoded **once**, is exactly the direct target's ticket URL, and a
  `GET` of that decoded string answers the same `302` to the same `Location`.
  `wrap_deep_link` encodes the ticket URL once and nothing encodes it again.
- **The ticket round-trips.** 292 characters, url-safe base64 plus `=` padding,
  no `%` in the path segment — D1's `quote(ticket, safe="=")` is a no-op at this
  length, live. A four-character tamper answers `404 ticket_invalid`.
- **Ticket expiry was driven live rather than named as unverified**, and the
  decision rule's flattering half was unavailable: group D shipped
  `TICKET_TTL_SECONDS: Final = 300` deliberately **not** as a setting, so there
  was nothing to lower. One ticket was held against the wall clock instead —
  honoured at **127 s** (`302`), refused at **312 s** (`404 ticket_invalid`).
- **ADR-0029 observed, not asserted.** After the redirect, the third party
  holds the real source URL with the token in it, exactly as before the ticket
  existed. *What changes is the artifact, not the grant*, measured.
- **`MediaSourceId` on this build is `mediasource_<item id>`, a namespace of its
  own rather than the item id.** `build_stream_targets` spells it
  `media_source.get("Id") or external_id`; the `or` arm is therefore not what
  runs here, and a URL built from the item id alone would be a different URL.

### H5 — the write half, and it wrote to a real account

M4's method exactly, because it is the only one that makes restoration exact.
The item was chosen by filtered request and its **complete** `UserData` read
from the **item** route — never a listing, which M3 measured as dishonest about
`PlayCount` — and the run refused to write unless it was already
`{PlaybackPositionTicks: 0, PlayCount: 0, IsFavorite: false, Played: false}`
with no `LastPlayedDate`. It was, on the first confirmed candidate.

- **The control was run before the write and seen to be red.** *"Emby's item
  differs from the recorded prior object"* answered **`False`** against a write
  that had not run — which is exactly the state M3 shipped forty passing
  contract assertions against. Only then was the same comparison believed after
  each write.
- **The position is exact and Emby does not round it.** `PUT /watch/titles/{id}`
  with `position_seconds=613`, one real `usher work --once`, and Emby's own item
  read reports **`PlaybackPositionTicks: 6130000000`** — 613 × 10,000,000 — with
  `Played: false` and `PlayCount: 0` untouched.
- **`POST /watch/titles/{id}/played` → `PlayCount: 1`, `Played: true`, a real
  `LastPlayedDate`, and the resume position cleared to `0`.** PRD 03's "position
  first, played last" as a consequence rather than a preference, through the
  shipped route and the shipped job.
- **The second press is a complete no-op, not merely a non-increment.** M3
  recorded `POST /PlayedItems` advancing `PlayCount` to 1 idempotently; this run
  adds that a second press leaves the **whole** `UserData` object byte-identical
  — `LastPlayedDate` is not re-stamped either. So a retried write-back cannot
  move a household's play history forward in time.
- **The unplayed path goes through `UserData` and not through
  `DELETE /PlayedItems`, and the observation is what proves it.** After
  `DELETE /watch/titles/{id}/played` and one worker pass, Emby reports
  `Played: false` while **`PlayCount` is still 1 and `LastPlayedDate` survives**
  — all three of which `DELETE /PlayedItems` would have reset. The local 613 s
  position rode along in the same body, which is the other half of M3's finding:
  the body names `Played` even when `Played` is not the field being changed.
- **Restored byte-for-byte.** `DELETE /Users/{u}/PlayedItems/{item}`, then a
  read-back: the before/after diff is `{}`. Choosing the all-zero item is what
  made that exact; on any other item `PlayCount` is not restorable by any route
  this project knows.
- 🔴 **Emby's read-back does not lag, which refutes the risk H5's own spec
  names.** That spec warns that "Emby's own indexing is asynchronous; a
  read-back immediately after a 204 may lag", and asks for bounded polling with
  the observed latency recorded. Measured: the change was visible on the
  **first** read every time, at **0.141 s / 0.142 s / 0.143 s** after the worker
  pass returned. Zero polls were consumed. `UserData` is not the asynchronous
  half of this server.
- **`PlayedPercentage` is on the item route when a position is set** (19.73% at
  613 s of a 3,107 s runtime) and gone when it is cleared — M5's observation
  confirmed a second time, on a second item. Nothing reads it.
- **Usher's own state, reported as the second and weaker observation it is:**
  `{position_seconds: 613, played: false, play_count: 1, last_played_at: …}`,
  agreeing with Emby at every step. ⚠️ One divergence worth knowing:
  **Usher's `last_played_at` after a local `/played` press is Usher's own write
  instant, not the one Emby stamped** (`…19:55:40.845654Z` locally against
  Emby's `…19:55:40.0000000Z`). Nothing reconciles the two until a
  `watch_history` backfill reads the item back.
- **One `usher work --once` per press was enough**, each pass claiming exactly
  `1 jobs`: the write-back is enqueued at `VISIBLE` and coalesced on
  `(kind, key)`. A real CLI subprocess against the same database, never a
  hand-called handler.

### `PortRateLimited.retry_after` — not provoked, and the premise it was dispatched under is stale

**Not provoked, named rather than implied.** Emby answered no `429` in any of
the 23 requests, no job ever recorded an attempt, and `run_after` is `NULL` on
the only row left in the queue. And the dispatch's own premise — *"constructed
at six sites and read nowhere outside its own `__init__`"* — was measured before
D9 landed. At the milestone head it is constructed at **six** `raise`/`return`
sites and **read exactly once in `src/`**, at `services/jobs.py:200`
(`JobWorker._fail`), which is D9's whole product. What remains true is that no
real upstream this project talks to has ever produced the header that feeds it.

### How the run was driven, because two parts of it are traps

- **The operator's secrets file holds an access token and a user id, not a
  password**, so `POST /Users/AuthenticateByName` cannot be exercised and
  `EmbySession._authenticate_locked` was swapped for one that installs the
  known token — M3, M4 and M5 all did exactly this. **The swap lives in a
  `sitecustomize.py` on `PYTHONPATH`, not in an in-process monkeypatch**,
  because H5's worker pass has to be a real `usher work --once` **subprocess**
  and a patched parent cannot reach it. It writes a marker file that the caller
  asserts on: a plant that did not land looks exactly like a check that passed.
- 🔴 **Starting the shipped app against a real source is itself an unbounded
  walk, and nothing warns you.** `LaneSupervisor` starts a push lane per enabled
  source, and its reconnect gap-closer calls
  `reconcile(source, SyncRunKind.DELTA, adapter)` — against this server that is
  the walk every rule in this file forbids, issued by `uvicorn` with default
  settings and no command of its own. This run set
  `USHER_PUSH_ENABLED=false` and `USHER_WORKER_ENABLED=false` on the app. Anyone
  driving a live HTTP run against a real household must do the same, or budget
  for a delta walk they did not ask for.

**Emby push works.** Verified 2026-07-29 against the live server with a normal
non-admin token: `/embywebsocket` upgrades (101), delivers periodic `Sessions`,
and pushes `UserDataChanged` within seconds of an out-of-band state change. Two
earlier negative findings were both wrong — see
[ADR-0004](../../docs/prd/decisions/0004-push-over-polling.md).

Health-check caveat: a handshake against *any* path succeeds, so a successful
upgrade is not a health signal. Assert on received messages instead.
**Re-measured 2026-08-02 and it is worse than that**: a socket carrying **no
credential at all** upgrades, accepts the subscription, and then delivers
`Sessions` *more* often than an authenticated one. So neither an upgrade nor
arriving messages establish that a channel is the one you think it is —
[ADR-0018](../../docs/prd/decisions/0018-push-health-is-a-message-ledger.md), and
the whole M5 live section below.
**A supervisor that resets its failure counter on connection is caught only
if the fake it is tested against has an *unbounded* supply of connections.**
`PushSupervisor` resets on delivery, and the mutation that moves the reset to
the connection is exactly the failure ADR-0004's caveat predicts: a proxy
that upgrades and buffers connects perfectly every time, so the ceiling is
never reached and PRD 08's "after N failures mark `supports_push = false`"
silently never fires. A scripted adapter whose list of connections *runs out*
terminates that mutated loop for the wrong reason and lets it pass. The fake
in `tests/unit/test_services_push.py` therefore hands out empty connections
forever and caps its own attempts with a plain `AssertionError` — never a
`UsherPortError`, so the supervisor cannot catch it — and the mutation fails
**4 cases in 0.43 s** with "the supervisor opened 41 channels; it is not
counting failures". Without that cap it would *hang*: `asyncio.wait_for`
cannot bound a loop that never yields, and the injected sleep therefore also
`await asyncio.sleep(0)`s.
**"Connect, then close the gap" is a concurrency claim and an ordering
assertion does not test it.** `order == ["connected", "gap"]` is what a
serialised run produces too, and it passes against an implementation that
connects, closes the socket, and then walks. The case that has teeth forces a
real 40 ms gap walk against a producer emitting on the open socket for ~30 ms
and asserts on measured intersection-over-union of the two windows —
**62.6% on this host, stable over five runs** (compare `JobQueueContract`'s
76.2% and M5 group B1's 80.3–85.4%) — plus "every event produced during the
walk was still delivered".
**Three obvious assertions about `SourceNotSupported` all survive its own
mutation.** Deleting the supervisor's `except SourceNotSupported` arm and
letting it fall through to `except UsherPortError` ends with
`push_available == [False]`, `push_connections == 0` and `gaps == 0` — the
ceiling is reached instead of the method returning, so every visible end
state is identical and only the five wasted attempts and four backoff sleeps
differ. Measured; the M5 plan's own draft asserted exactly those three and
the mutation survived it. Assert on `attempts == 1` and `sleeps == []`.
**`PushHealth.record_reconnect` was a method nothing in `src/` ever called**,
so PRD 10's `usher.source.push.reconnects` would have plotted a flat zero for
every source forever. The increment belongs in `record_open`, guarded on
`opened_at is not None` — on the second and later *open*, not on a failure,
because a lane that failed to connect five times and then succeeded
reconnected *once*. Both the unguarded version (every source starts at 1) and
the absent version are pinned.
**A push merge's `observed_at` mutation survives the whole unit file and is
killed only by real Postgres.** Measured 2026-08-01: replacing
`PushApplyService`'s `datetime.now(UTC)` with a plausible earlier instant —
the event's own timestamp, the last walk's `started_at` — passes all of
`tests/unit/test_services_push.py` and fails
`tests/integration/test_services_push.py`, because
`FakeWatchStateRepository` stores `observed_at` as `updated_at` while
`trg_watch_states_set_updated_at` owns that column in Postgres. Same trap
`backfill_one` documents, one lane over, and the reason that integration file
exists at all.
**The M5 plan's own self-review found a real bug and it is worth the general
form.** `_publish_watch_states` zipped the *matched subset* of targets
against the whole batch of states, so one unmatched item — which PRD 02
guarantees there will always be — shifted every pair by one and published
item A's resume position under item B's title id. Recovering a pairing
outside the loop that built it is the failure; `WatchStateSyncService`
therefore returns `MergedState(external_id, target)` and the pairing cannot
be reconstructed wrongly. Same rule `SourceEvent.watch_states` states one
layer up: keyed, never aligned by position.
**M5's live verification: the first real `/embywebsocket` message this
repository has ever parsed, and four of thirteen documented guesses were
wrong.** Run 2026-08-02 against the same live Emby **4.9.5.0** server,
driving the shipped `EmbyAdapter` → `EmbyPushChannel` → `connect_websocket`
→ real `websockets`, and for the long hold the shipped `PushSupervisor` with
recording callables in place of the three unit-of-work ones. From a
throwaway script outside the working tree, holding the operator's existing
token (no password, so `AuthenticateByName` was again not exercised).
**Bounded deliberately: one long-lived socket held 100 minutes, eight
short-lived probe sockets, and 14 HTTP requests in total** — no walk of any
kind, because the library is 1,126,789 items. The long socket received
**200 frames — 183 `Sessions`, 12 `LibraryChanged`, 5 `UserDataChanged` —
with zero reconnects, zero unforced failures, and `supports_push` true
throughout**, and the shipped mapper turned them into 20 `SourceEvent`s.

- **The envelope is not uniform, and that is the first correction.**
  `UserDataChanged` and `LibraryChanged` carry
  `{MessageId, MessageType, Data}` with a **distinct 32-hex `MessageId` per
  message** (not per type — 17 carried one, 17 distinct); **`Sessions`
  carries `{MessageType, Data}` and no `MessageId` at all**, on 183 of 183
  frames. `tests/fixtures/emby/
  push_sessions.json` claimed one and no longer does.
- **A real `UserDataChanged` entry is honest, including about play
  history.** One item, three transitions, each compared against
  `GET /Users/{u}/Items/{item}` in the same second: `PlaybackPositionTicks`
  6,130,000,000 (the 613 s written) with `Played: false`; then
  `PlayCount: 1`, `Played: true`, `LastPlayedDate` — *the same timestamp the
  item route returned*; then all-zero after the restore. **So the pushed
  shape is not the partly-honest one the listing route is**, and the
  M5-blocking failure this run existed to look for — an entry that zeroes
  the position, so the adapter reports a wrong resume point while the
  contract case stays green — **did not happen**. Through the shipped
  mapper, the first event carried `position_seconds=613`.
- **`play_count`/`last_played_at` stay `None` anyway, and that is a
  deliberate lag rather than an oversight.** ADR-0014's rule is that a
  reported number must be *true*, and the evidence here is one item across
  three transitions, all of them writes Usher itself made, on an item whose
  history was zero to begin with. The failure it guards against needs an
  entry reporting `0` for an item whose true count is 13, which this run
  could not produce without touching real history. Turning the field on is a
  measured opportunity worth one `watch_history` job per played item;
  recorded, not taken.
- **`LibraryChanged` arrives, its arrays hold ids, and one of them arrived
  carrying all six at once.** Never observed before this run; **twelve**
  arrived unprompted during the hold, with all seven documented keys
  (`ItemsAdded`/`ItemsUpdated`/`ItemsRemoved`,
  `FoldersAddedTo`/`FoldersRemovedFrom`, `CollectionFolders`, `IsEmpty`) and
  every array a **list of id strings** rather than of item objects. The
  committed fixture's shape was already right, field for field. The shipped
  `to_source_events` produced 7 `ITEM_ADDED`, 7 `ITEM_UPDATED` and 1
  `ITEM_REMOVED` from them — one event per non-empty array, live.
- **`ItemsRemoved` fires on a library from which nothing was removed, and
  that is ADR-0015's argument arriving as a measurement rather than as an
  argument.** Nobody deleted anything from this server during the 100-minute hold, and
  one frame still named an item in `ItemsRemoved` (alongside
  `FoldersRemovedFrom`, `ItemsAdded`, `FoldersAddedTo`, `CollectionFolders`
  and ten `ItemsUpdated`). M5 counts it and retracts nothing; had it
  retracted, one ordinary library refresh would have marked a present file
  unavailable.
- **A real `ItemsUpdated` batch reached 42 ids**, against
  `push_max_items_per_event`'s default of **50**. So the ceiling is not
  theoretical headroom over a hypothetical event — real traffic on an
  otherwise idle server comes within 16% of it, and the batch below it costs
  42 `get_item` calls applied inline. Raising the default would buy little
  and the deferral path (a delta walk) is the cheaper answer above it, which
  is the shape M5 already ships; recorded so the number is chosen against
  data next time it is chosen.
- **`Key` and `UnplayedItemCount` are not on a real `UserDataList` entry,
  and `PlayedPercentage` is.** Observed entry keys: `ItemId`,
  `PlaybackPositionTicks`, `Played`, `PlayCount`, `IsFavorite`, plus
  `PlayedPercentage` (a float, when the position is non-zero) and
  `LastPlayedDate` (when played). The fixture and `FakeEmbyServer` both
  rendered a `Key`; both stopped.
- **The `Sessions` interval, which `DEFAULT_STALE_AFTER_SECONDS = 90.0`
  rests on: median 38.7 s, mean 32.8 s, p90 46.5 s, max 72.9 s** over 182
  intervals in 100 minutes on an authenticated socket. **The 90 s default
  survives — but the headroom is 1.23x, not the comfortable margin the
  constant reads like, and it shrank monotonically as the window grew**: the
  worst gap was 52.6 s at 26 minutes, 60.1 s at 70 and **72.9 s at 96**, and
  only two of 182 intervals exceeded 60 s at all. So
  a longer hold would plausibly have crossed 90, and this is a bound that
  has **not been falsified** rather than one shown to be safe — on one
  household, on one evening. A 75-second smoke run earlier the same evening
  saw exactly **one** frame, and the cadence is not an interval at all
  (below).

  **The default is left at 90 anyway, and the reasoning is worth keeping.**
  A bigger constant chosen from a 96-minute sample would be just as
  unprincipled as this one was, and it costs detection time for the failure
  the whole milestone exists to catch. The real finding is that the constant
  is wrong *in kind*: there is no application-level heartbeat on this
  channel at all, so any fixed ceiling is a guess against a change-driven
  signal. The one genuinely periodic signal available is the WebSocket
  pong — and ADR-0018 deliberately refuses to count it, because a pong is
  not delivery. That tension is the honest statement of what this design
  costs. When it bites, it bites bounded and visible: a reconnect, a delta
  that returns 0 items, and `usher.source.push.reconnects` climbing.
- **`"0,1000"` really is `initialDelayMs,intervalMs`, and an authenticated
  socket does not honour it.** An *unauthenticated* socket receives
  `Sessions` at ~1 Hz — 53 and 55 frames in 45 s, with sub-second gaps —
  while the authenticated one on the same server in the same minute received
  **one**. The difference is the payload: the unauthenticated stream carries
  the **whole server's 83 sessions**, the authenticated one a 5-session
  row-filtered view. The natural reading, and the one that fits every
  number: the 1 s timer fires either way and the filtered stream is only
  *sent* when the filtered view changes. **So Usher's liveness signal is
  change-driven, not periodic**, and a genuinely quiet server could exceed
  any fixed `stale_after`. `push_stale_after_seconds` is the knob, and
  `usher.source.push.reconnects` is how the condition is seen.
- **`/embywebsocket` does not accept `X-Emby-Token` as a header, and the
  test that looks like it says otherwise is the trap.** A header-only socket
  upgrades and delivers — identically to one with **no credential at all**:
  53 frames of 83 sessions against 55 frames of 83 sessions. It is not
  authenticated; it is anonymous. So the token cannot be moved out of the
  URL this way and **ADR-0012's accepted risk stands unnarrowed**. A check
  written as "did it connect and receive messages" passes this. The only
  discriminator is the row-filtered payload, or a `UserDataChanged` that
  never comes.
- **A dropped socket raises rather than hanging, and Emby re-delivers
  nothing.** Aborting the TCP transport under a live channel raised
  `PortUnavailable` out of the iterator in **0.0 s** — not a hang, not the
  quiet end the port forbids — and `connected` went false. Over a **61 s**
  outage, a real played toggle and its restore were made out of band; the
  reconnected channel then listened for **90 s** and received three
  `Sessions` and **not one** `UserDataChanged`. The control is decisive: a
  *second* socket that stayed up throughout received both changes at the
  time they happened. **The gap-closing delta is not belt-and-braces, it is
  the only cover there is**, which is exactly what PRD 03 puts on the
  reconnect.
- **The `websockets` DEBUG token leak is real, and the fix holds against the
  real library and the real server.** Two runs at `USHER_LOG_LEVEL=DEBUG`
  with `configure_logging` installed exactly as `create_app` does, each
  writing to a real stdout captured to a file: the shipped path produced
  **804 bytes / 2 lines** with **no token, no `api_key=`, no `> GET`
  request line** and a channel that genuinely delivered
  (`messages_received == 1`); the control — the same URL with the library's
  own logger left alone — produced **16,857 bytes / 24 lines** with the
  token in it, `api_key=` in it, and the request line logged twice. Both
  halves, or the run proves nothing; the same discipline the network guard
  gets.
- **`permessage-deflate` is not negotiated.** `websockets` offers it by
  default and the handshake response carries no `Sec-WebSocket-Extensions`
  at all, on every connection made in this run. So nothing in this project
  is relying on compression, and a frame is a frame.
- **A client that stops reading loses the connection, which is what
  `max_queue=256` is buying.** With `max_queue=1` and no application read
  for 150 s, the socket came back **CLOSED** with a `ConnectionClosedError`
  and only two buffered `Sessions` behind it — so Emby's listener does not
  queue indefinitely for a stalled consumer. **The confound is named rather
  than glossed:** `websockets` services pings on the same reader task that
  backpressure stalls, so this measurement cannot separate a server-side
  close from the client's own pong timeout. Either way the operational
  conclusion is the same and it is the one `connect_websocket` was already
  written for: do not let the queue fill during the gap-closing walk.
- **The nonexistent path still upgrades**, ADR-0004's quirk re-measured on
  the same build: `/embywebsocket-nope` → 101, `Upgrade: websocket`,
  `Sessions` delivered.
- **`supports_push` is `False` before the first message and `True` after**,
  measured through the shipped adapter against the real server rather than
  against a fake — the contract's pre-message assertion, live.
- **The one write to a real account, and its restoration.** The same
  discipline M4's run set: an item whose complete `UserData` was already
  `{PlaybackPositionTicks: 0, PlayCount: 0, IsFavorite: false, Played:
  false}` with no `LastPlayedDate`, found with **one** 50-item listing plus
  one single-item read (never a search over a walk). `push_watch_state`
  wrote a 613 s position, then marked it played (`PlayCount: 1`,
  `LastPlayedDate`, position cleared — M3's ordering finding, re-confirmed),
  then `DELETE /Users/{u}/PlayedItems/{item}` restored it **byte-for-byte**
  (`after == before`). A second toggle and restore during the outage test
  ran on the same item and ended the same way; the final read-back matches
  the recorded `before` exactly.
- **`PlayedPercentage` appears on the item route too** when a position is
  set, and disappears when it is cleared. Nothing reads it; recorded because
  it is the one key the fixture was missing.
- **Not verified in this run, and named rather than implied:** `POST
  /Users/AuthenticateByName` and whether its response carries
  `User.Policy.IsAdministrator` (this run held a token, not a password —
  so Task 3's extra `GET /Users/{userId}` remains the verified path);
  silent 401 re-authentication end to end; durable-device registration
  across restarts; a socket held for four hours (**100 minutes** is what this
  run covers, with zero reconnects and zero unforced failures in it); a
  `LibraryChanged` with `IsEmpty: true` (all twelve observed carried
  something, so what that field means is still a guess about a field nothing
  reads); a `UserDataChanged` for a **series** entry, which is where
  `UnplayedItemCount` would plausibly appear; and whether a real entry is
  honest about play history for an item Usher did not itself write.
**M4's live verification: the design's central measurement holds, the
matcher's exact-name tier was expected to match "almost nothing" and matches
about three quarters, and the defect the plan called hypothetical is real in
this library.** Run
2026-07-31 against the same live Emby **4.9.5.0** server, driving the real
`EmbyAdapter` and the real `ReconcileService`/`IngestService`/`MatchService`/
`WatchStateSyncService` against a real `pgvector/pgvector:pg17` holding a
real M2 bootstrap (1,271,314 titles). Bounded deliberately: **600 items
ingested** and ~90 deliberate requests, from a throwaway script outside the
working tree. (Plus several hundred accidental ones from a single runaway
probe, killed — see the bounding note near the end of this file. Counted here
rather than quietly dropped: it is the mistake worth not repeating.)

- **The finding M4 exists to answer, re-measured through the real adapter,
  on one item, in one run.** The *listing* reports `PlayCount: 0` and no
  `LastPlayedDate`; the *single-item* route reports `PlayCount: 13` and
  `LastPlayedDate: 2026-07-30T08:12:53Z`; `PlaybackPositionTicks` and
  `Played` agree. Through `EmbyAdapter`: the walk yields
  `play_count=None, last_played_at=None`, `get_watch_state` yields `13` and
  the real timestamp. Over the first 100 states of a real
  `adapter.watch_state()` walk, `play_count` and `last_played_at` are
  `None` for **all 100**. ADR-0014's premise is measured, not assumed.
- **The milestone's central property, end to end against real payloads.** A
  row holding the authoritative `play_count = 13` was then fed the *listing*
  payload for the same item through `to_watch_state(...,
  play_history_is_trustworthy=False)` and `merge_from_source`. It reads back
  **13**, `played = true`, and the original `last_played_at`. The walk
  cannot zero real history, verified against the live server rather than
  against a fake told to behave like it.
- **`MatchService`'s exact-name rule matches ~74% of real Emby names, not
  "almost nothing".** Measured against the real 1,271,314-title catalog with
  the *identical* rule `_confident` applies (exact normalised name, year
  ±1, exactly one survivor), over 600 movies and 300 series sampled across
  six windows spanning the whole collection: **72.2% of movies** (433/600)
  and **75.3% of series** (223/296 distinct probes). Of the movie misses,
  142 are *absent* from the catalog and only 25 are *ambiguous* — so the
  review queue is a trickle, and what feeds it is mostly the catalog not
  holding the title at all rather than the rule being too strict. This
  reverses the plan's stated expectation and it is the single most
  load-bearing number the live run produced.

  **What this is and is not.** It is `_confident`'s *predicate*, run over
  the local catalog — i.e. tier 3 — not `_confident` against TMDb's own
  search results, which no run in this repository has ever made. The two
  differ in their candidate set, and in opposite directions: TMDb returns a
  handful of relevance-ranked results, so "exactly one survivor" is *easier*
  to satisfy than against 1,271,314 rows; but TMDb can also return nothing
  for a name the local skeleton holds. So treat 72–75% as a measurement of
  the rule on real names, not as a prediction of tier 4's yield.
- **On this library the name+year tier out-resolves the `tmdb_id` tier.**
  68.5% of movie TMDb refs and 68.7% of series TMDb refs resolve, against
  72.2%/75.3% for name+year — because only 291,772 of 1,271,314 catalog
  titles carry a `tmdb_id` at all. Tier 3 is not the fallback the ladder's
  ordering makes it look like.
- **A probe with no year resolves nothing, by construction, confirmed on
  real data.** `t.year BETWEEN p.year - 1 AND p.year + 1` propagates `NULL`,
  so the same 900 names re-run with the year stripped match **0**. That is
  the documented intent (the alternative matches every undated IMDb
  skeleton of the same name) — recorded here because "0.0%" looks like a
  bug and is not.
- **A malformed `ProviderIds.Imdb` is real, not hypothetical: 11 of 885 in
  the sample** (1.2%), all bare 6- or 7-digit numbers with no `tt` prefix.
  Fed to the real `MatchService` they resolve cleanly (9 stubs, 2 name+year)
  and nothing raises. **The guard that makes that true is `_as_imdb`, not
  `_usable_ids`** — the two are layered, and removing `_usable_ids`'s
  filtering alone still does not raise, because `_create_stub` calls
  `_as_imdb` again at the constructor. Removing `_as_imdb`'s pattern check
  raises `pydantic_core.ValidationError` on these exact real payloads, which
  is **not** a `UsherPortError`, which is a permanently aborted sync. Measured
  both ways.
- **An episode never walks the ladder, confirmed on real data.** Of 600 live
  items, 578 were episodes and every one returned `UNMATCHED` from
  `MatchService` with no lookups; `IngestService` attached them as
  `SERIES_PARENT`. Zero episodes reached a provider tier or the stub tier.
- **Stub-on-sight never fired, and that makes the cold and warm walks
  identical.** All 22 non-episode items resolved to existing catalog titles
  (21 by `tmdb_id`, 1 by `imdb_id`), so **zero stubs were created** — and
  walk 2 over the same 600 items cost exactly the same **40 statements**,
  `0.0667` per item, as walk 1. That is the "16,950 of the first walk's
  17,722 statements are stub-on-sight, bounded by new titles" claim
  arriving from the other direction: with no new titles, there is no cold
  penalty at all. (40 statements for one 600-item batch is above the 15.4
  statements/batch Task 25 averaged over 50 batches, because this is a
  single first batch where every series, season and episode is new.)
- **A delta walk completes and its cursor advances; a failed walk sweeps
  nothing.** A `DELTA` reconcile against the live server inherited the last
  completed `FULL` run's instant, returned 0 items (nothing had changed in
  that window), recorded `COMPLETED`, and advanced `sync_runs.cursor_at`. A
  `FULL` walk interrupted mid-stream recorded `FAILED` with its message and
  left all 601 `available` rows untouched — `items_retracted = 0`.
- **The delta filters, re-measured on a fresh 30-day window.**
  `MinDateLastSaved` = 28,955, `MinDateLastSavedForUser` = 29,027, unfiltered
  = 1,126,789. Still honoured, still genuinely different, and an *invented*
  parameter name still returns the full unfiltered count — the "degrades to
  a full walk" safety property, re-measured.
- **The library grew.** 1,126,789 items now (94,448 movies / 32,414 series /
  999,927 episodes), against 1,126,674 four days earlier. Any figure derived
  from it is a snapshot, not a constant.
- **`VideoRange`'s vocabulary holds over a second, different slice.** 600
  movies spread across the whole collection by `DateCreated` ascending:
  `SDR` 597, `DolbyVision` 2, `HDR 10` 1, with `ExtendedVideoType/SubType`
  ∈ {`None/None`, `Hdr10/Hdr10`, `DolbyVision/DoviProfile50`,
  `DolbyVision/DoviProfile81`}. `VideoRangeType`, `DvProfile` and
  `DvVersionMajor` are absent from every video stream. The mapper produced
  the right `SourceItem` for all 1,100 sampled payloads (600 movies, 300
  series, 200 episodes) with **zero failures and zero skips**, and the
  technical metadata survives all the way into `media_items`: 496 `h264` +
  85 `hevc`, 581 of 601 rows carrying width/container/file size (the 20
  without are `Series` rows, which have no `MediaSource` — correct), and one
  row carrying `hdr_format = DV` from a real `VideoRange: "DolbyVision"`
  payload. `SDR → NULL` and `DolbyVision → DV` are both confirmed on stored
  rows; `HDR 10 → HDR10` appeared in the sampled payloads but not in the
  ingested slice, so that arm is still fixture-only end to end.
- **Emby's `ProviderIds` key space is far wider than three, and case is not
  stable.** Observed on 900 movie/series payloads: `Tmdb`, `Imdb`, `Tvdb`,
  `TvMaze`, `Official Website`, `TvRage`, `X (Twitter)`, `Zap2It`,
  `TV Maze` (with a space, alongside `TvMaze` without), `Wikipedia`, `EIDR`,
  `Wikidata`, `Reddit`, `Fan Site`, `IMDB` (14 items — uppercase),
  `Facebook`, `Instagram`, `TmdbCollection`, `Youtube`, `tmdb` (3 items —
  lowercase), `Twitter`. `mapping.provider_ids`' `key.lower()` is what makes
  `IMDB` and `tmdb` usable at all, and an exact-key `get("tmdb")` is what
  keeps `TmdbCollection` from being read as a TMDb id. A prefix match there
  would attach films to collections. The one residual risk is an item
  carrying both `Imdb` and `IMDB` with different values, where `key.lower()`
  silently keeps whichever came last; none was observed.
- **The one write to a real account, and its restoration.** An item was
  chosen whose complete `UserData` was already
  `{PlaybackPositionTicks: 0, PlayCount: 0, IsFavorite: false, Played:
  false}` precisely so the one destructive Emby route is an *exact* restore.
  `push_watch_state(played=True)` took it to `PlayCount: 1`, `Played: true`,
  `LastPlayedDate: 2026-07-31T13:41:53Z`; `get_watch_state` — the backfill's
  own read path — returned `play_count=1` and that timestamp, which is the
  backfill verified end to end against a real write. `DELETE
  /Users/{u}/PlayedItems/{item}` restored the object **byte-for-byte** (the
  before/after diff is empty). Choosing an all-zero item is what made
  restoration exact rather than approximate; on any other item `PlayCount`
  is not restorable by any route this project knows.
- **Not verified in that run, and named rather than implied:** a full
  1,126,674-item walk; `POST /Users/AuthenticateByName`; silent 401
  re-authentication end to end; durable-device registration across
  restarts. Anything needing a TMDb API key was also unverified there and
  **is no longer** — a key was configured the next day and the TMDb half of
  Task 26 ran on 2026-08-01; see the TMDb live-verification section below,
  including `_confident` against TMDb's own search results, which that run
  measured at 83.1%/87.2% against the 72–75% the *local* rule scores.
  `EnrichService` and the `enrich` job handler are still driven only by
  fakes: the live run exercised `TmdbClient`, `TmdbMetadataProvider` and the
  mapper, not the service above them.
**M3's live verification found the write-back route was simply wrong, and
three other things worth not re-deriving.** Run 2026-07-31 against the live
Emby **4.9.5.0** server, driving the real `EmbyAdapter`/`EmbySession` with
`_authenticate_locked` swapped for one that installs a known token. Full
route-by-route table in the M3 plan's "Which Emby routes are guessed"
section.

- **`POST /Users/{user}/PlayingItems/{item}/Progress` answers 400** —
  `"Value cannot be null. (Parameter 'key')"` — bodyless, with an empty JSON
  body, with an `{ItemId, PositionTicks}` body, and with `MediaSourceId` and
  `IsPaused` added. So does `POST /Sessions/Playing/Progress`. Both are
  *session-scoped playback reporting*, keyed off a play session Usher never
  has. **Use `POST /Users/{user}/Items/{item}/UserData`** with a JSON body;
  it answers 204. `FakeEmbyServer` could not have caught this: it
  implemented the adapter's own guess, so 40 contract assertions passed
  against a write-back that had never worked once. This is the whole
  argument for a live run in one bug.
- **That `UserData` body must name `Played` even when it is not changing.**
  It deserialises into a DTO whose unset fields take their defaults, so a
  body carrying only `PlaybackPositionTicks` flips a played item to
  unplayed. `PlayCount` and `LastPlayedDate` survive the same omission.
- **`DELETE /Users/{user}/PlayedItems/{item}` is destructive beyond its
  name:** it resets `PlayCount` to 0, clears `LastPlayedDate`, *and* clears
  a non-zero resume position. Never use it to report an item unplayed while
  writing a position. `POST` to the same route *is* how you mark played —
  it advances `PlayCount` (to 1, idempotently, not `+1`), stamps
  `LastPlayedDate`, and clears the resume position. That last part is PRD
  03's load-bearing "position first, played last" ordering, verified for the
  first time.
- **`/Videos/{id}/stream` does not need `DeviceId`.** Measured one parameter
  at a time with a `Range` header: as built → 206 with real bytes; without
  `DeviceId` → still 206; without `api_key` → 401; without `static` → 400.
  The parameter is no longer sent (ADR-0012).
**A listing's `UserData` is not the same as an item's.** Verified: a
`GET /Users/{user}/Items` listing reports `PlayCount: 0` and omits
`LastPlayedDate` entirely, for the very item whose
`GET /Users/{user}/Items/{item}` reports `PlayCount: 2` and a real
`LastPlayedDate`. `PlaybackPositionTicks` and `Played` are correct in both.
Neither `Fields=UserDataPlayState`, `Fields=UserData`,
`EnableUserData=true`, nor restricting the listing to explicit `Ids`
changes it. So `watch_state()` — which walks listings — cannot carry play
history, and M4 must not write `play_count`/`last_played_at` from a walk or
it writes 0 over real history. Recovering them is one request per item
against 1,126,674 items. Making both fields optional on `SourceWatchState`
is the honest fix; it is a port change and is deliberately left to M4.
**Emby 4.9.5.0 emits neither `VideoRangeType` nor `DvProfile`.** Not once
across every video stream of 200 movies (the newest 100 4K and 100 HD of
94,438), including all 34 Dolby Vision files. What it emits is `VideoRange`
∈ {`SDR`, `DolbyVision`, `HDR 10`} — with a space — plus
`ExtendedVideoType`/`ExtendedVideoSubType` ∈ {`None`/`None`,
`Hdr10`/`Hdr10`, `DolbyVision`/`DoviProfile81`|`DoviProfile50`}. The
`Extended*` pair carries the **literal string `"None"`**, not JSON null, so
it is always truthy and any check on it must be a token lookup that falls
through. The `DOVIWith*` family the mapper also handles is Jellyfin's
vocabulary, not this server's; both are kept, since reading a field a server
omits costs nothing.
**Emby honours a secondary sort key, so `SortBy=DateCreated,SortName` is a
real request.** Shown on a tie-heavy primary key rather than hoped for:
`ProductionYear,SortName` returns the tied block in `SortName` order,
`ProductionYear` alone returns it in a different, insertion-shaped one. Tie
*instability* was **not** reproducible here — repeated pages came back
identical and overlapping `StartIndex` windows agreed exactly, with and
without the tiebreak — so the second key is a cheap guarantee rather than a
demonstrated-necessary fix. `MinDateLastSaved` and `MinDateLastSavedForUser`
are both honoured and are genuinely different filters (28,934 vs 29,005
items over the same 30-day window). An *invented* parameter name is ignored
outright and returns the full unfiltered count, which is the "degrades to a
full walk" safety property, measured.
**The library is 1,126,674 items, not 94,395.** 94,438 movies, 32,409
series, 999,827 episodes. The movie figure the adapter was designed around
was one third of the walk. At the default page size that is 5,634 pages —
**56% of `MAX_PAGES`**, so the headroom is 1.8x, not the ~21x the constant's
comment claimed. **Re-measured four days later: 1,126,789** (94,448 /
32,414 / 999,927). It moves; treat every figure derived from it as a
snapshot with a date on it, not a constant.
**A token presented with a different `DeviceId` neither forks nor
invalidates its session.** `GET /Sessions` was byte-identical before and
after, and the token still worked. Emby binds a session to the token's own
authentication record, made at `AuthenticateByName` time; the header's
`DeviceId` on later requests does not register a device. So "one durable
device" comes from authenticating once with a stable id, not from repeating
it.
**Not verified, and the docs say so rather than implying coverage:** `POST
/Users/AuthenticateByName` itself (that run held a token, not a password —
it is verified separately by ADR-0004's session), silent re-authentication
on a 401 end to end, durable-device registration across restarts, and
`multi_version_movie.json`'s shape.
**`multi_version_movie.json` has now been looked for twice, over disjoint
slices, and still has never met a real payload.** M3 searched the newest 800
movies; M4 searched 600 movies spread across six windows of the whole
94,448-movie collection ordered by `DateCreated` ascending (indices 0,
18889, 37779, 56668, 75558, 94348). **Every one of the 1,400 movies examined
carries exactly one `MediaSource`** — the count distribution is `{1: 600}`
with nothing else in it. So `primary_media_source`'s selection rule remains
fixture-only, and this deployment now looks like a genuinely
single-version library rather than one whose multi-version items happened to
sit outside the first sample. The fixture stays: another Emby deployment
will have them, and the rule is cheap.
**`Policy.IsAdministrator` is readable**, on `GET /Users/{userId}`, with the
user's own non-admin token — a 45-key `Policy` object. (`GET /Users/Me`
answers 500 on this build.) ADR-0012 assumes a non-admin account and nothing
enforces it; this is the check that would make it observable, recorded there
as recommended-not-implemented.
**An episode must never walk the match ladder, and the reason is in the
payload.** A live Emby episode carries the *episode's* own provider ids —
`{"Imdb": "tt2178782", "Tvdb": "4517466"}` on `tests/fixtures/emby/
episode_item.json` — not its series'. Two consequences, both catastrophic at
999,827 episodes. TVDb numbers episodes and series in different, numerically
overlapping namespaces and `usher.db.repositories.matching`'s TVDb statement
deliberately does not filter on kind, so an episode run through the provider
tiers resolves to whichever unrelated series holds that integer. And no
episode's IMDb id is in the catalog at all (`tvEpisode` is excluded from M2's
bootstrap by design), so the stub tier mints one junk `Title` per episode —
a catalog of rubbish roughly the size of the real one. `MatchService` returns
`UNMATCHED` for an episode with no lookups and **no remote-search job** (one
per episode is a queue the size of the library, and a TMDb title search for
an episode name is not a resolution path); `IngestService` attaches it to its
series' `Title`, labelled `MatchMethod.SERIES_PARENT`.
**Nothing a source can put in a payload may abort a walk.** `Title.imdb_id`
is pattern-validated (`^tt\d{7,8}$`) and `year` is `ge=0`, and a pydantic
`ValidationError` is **not** a `UsherPortError` — so `ReconcileService`, which
re-raises anything that is not one, would let a single stray
`ProviderIds.Imdb` in 1,126,674 items abort that source's sync permanently.
Filter every value to the shape the model accepts *before* the constructor.
**Verified live 2026-07-31: 11 of 885 real `Imdb` values in a 900-item sample
are bare digits with no `tt` prefix, so this is a live defect rather than a
defensive one.** The two filters are layered and only the inner one is
load-bearing: `_usable_ids` drops unusable refs, and `_create_stub` calls
`_as_imdb`/`_as_int` *again* on what survives. Removing `_usable_ids`'
filtering alone raises nothing on those exact payloads; removing `_as_imdb`'s
pattern check raises `ValidationError` on them immediately. So
`usher.services.matching._as_imdb` is the guard, and a mutation of
`_usable_ids` alone is an equivalent mutant.
**`sorted()` over a set of `ProviderRef`/`NameYearProbe` raises.** Both are
`@dataclass(frozen=True, slots=True)` without `order=True`, so there is no
`__lt__` — `TypeError: '<' not supported`. `dict.fromkeys` is the idiom used
throughout: it deduplicates *and* keeps the batch's own order, which is what
makes a failure read in the order the page arrived.
**A service that saves a frozen checkpoint per batch must not evolve its own
stale copy in the failure handler.** `ReconcileService._flush` saves an
evolved `SyncRun` after each batch, so when the walk raises, `reconcile`'s
binding is the pre-walk value — and `run.evolve(status=FAILED)` on it writes
`items_seen = 0` over a checkpoint that recorded eight. Same trap
`BootstrapService.import_dataset` documents; here there is no re-fetch to
recover from (`SyncRunRepository` is a history, not a per-source checkpoint),
so a small mutable holder carries the latest run across the `try`.
**Moving the availability sweep into a `finally:` really does retract a
healthy library, and the obvious test shape hides why.** Measured. Seed seven
items, fail the walk immediately, one batch: nothing is written before the
failure, so the sweep would retract 7 of 7 — 100%, refused by ADR-0015's
ceiling, and `AvailabilitySweepRefused` then escapes the `finally:` and
propagates out of `reconcile`. The case fails, but on an uncaught exception
rather than on its own assertion, and it never exercises a sweep that
*succeeds* after a failed walk. The shape that does is a walk that commits
eight of ten items and then raises: two stale rows, 20%, under the ceiling,
no refusal, two available items silently retracted. **The ceiling is not a
second line of defence for the success-path gate** — it fires on a fraction,
so it catches the catastrophe and misses the quiet one. Reproduced against
real Postgres as well as the fakes.
**`observed_at=now()` instead of the run's start instant is a *semantic*
break, not a race.** A per-row write instant is always later than
`run.started_at`, so the sweep's `last_seen_at < seen_since` still spares
everything the run saw and no retraction test fails. What breaks is the
meaning of the column. Assert `stored.last_seen_at == run.started_at`
directly; no frozen clock is needed.
**An episode's `MediaItem` carries two ids and its `WatchState` may carry
one, and the collapse between them is the whole of M4's episode watch
state.** `IngestService` writes the series' `title_id` *and* the
`episode_id` on an episode's row (a client browsing a season wants both);
`watch_states` has a `num_nonnulls(title_id, episode_id) = 1` CHECK. So
`WatchStateSyncService` collapses the pair with the episode winning
(`usher.services.watch_sync._watch_target`). Passing both through raises
`PortDataMalformed` by contract, which aborts a batch of five thousand
states over 89% of this library; passing the *title* through merges every
episode of a show onto one row and violates nothing. The same asymmetry
runs the other way in `MediaItemRepository.resolve_external_ids`, whose
title branch needs `episode_id IS NULL` or a series' own watch state
resolves to whichever of its episodes the planner reached first.
**A history backfill must carry its own fresh `observed_at`, and both
test layers are blind to why.** PRD 03's "latest `updated_at` wins" covers
the whole record, and `trg_watch_states_set_updated_at` stamps the *write*
instant — so a backfill carrying the walk's instant is refused by the very
row it exists to repair, writes nothing, and leaves that row matching
`played AND play_count = 0` forever. `FakeWatchStateRepository` stores
`observed_at` as `updated_at`, so it accepts what Postgres refuses; and the
integration suite cannot reproduce the production form either, because
`now()` is frozen per transaction and each test *is* one transaction.
`tests/integration/test_services_watch_sync.py` stages the row with
`clock_timestamp()` through a raw `INSERT` (the trigger is `BEFORE UPDATE`,
so an insert is the only way to own the column), which is as close as one
transaction allows.
**The bounded backfill terminates, measured.** Seven rows matching
`played AND play_count = 0`, drained three at a time, empty in exactly
three passes — against the fakes and against real Postgres, with the loop
bounded so a non-converging predicate fails the case rather than hanging
the suite. The honest half: convergence is a property of the *source*. A
source whose single-item route also cannot count leaves rows matching
forever, bounded at one request per row per pass and rotating rather than
starving, because `list_needing_history` is oldest-first and a merge moves
`updated_at`.
**Two guards in M4's services are unreachable through their own port's
contract, and are pinned by direct unit cases rather than deleted.**
`_watch_target`'s "matched to nothing" branch (`resolve_targets` omits an
unmatched item rather than answering with an empty pair) and `_links_for`'s
`is_valid` check (the OTel SDK also drops an invalid `Link` on the way into
a span, so a worker that built one records the same empty `links` tuple).
Both mutations survived the whole suite until the direct case existed.
**Two `IngestService` defects are invisible to every port fake and only real
Postgres catches them.** Skipping `resolve_seasons` or `resolve_episodes` and
trusting the freshly-minted UUIDv7 leaves all 24 unit cases green — a dict has
no foreign keys — and fails on `fk_episodes_season_id_seasons` /
`fk_media_items_episode_id_episodes` on the *second* walk, when that id names
no row. `tests/integration/test_services_ingest.py` and
`tests/integration/test_services_reconcile.py` are the paired runs; the latter
also pins "a refused sweep leaves the session usable for the `FAILED` row that
explains it", which no fake can express (the guard is evaluated in Python
after a successful `SELECT`, so Postgres never aborts the transaction).
**The ingest pipeline's measured cost, 2026-07-31 against
`pgvector/pgvector:pg17`** (`scripts/measure_ingest.py --items 50000`,
50,000 items in the measured library's proportions — 88.7% episodes — at
batch size 1,000):

| | statements | per item | items/s |
|---|---|---|---|
| first walk, cold catalog | 17,722 | 0.3544 | 1,933 |
| the nightly walk | 1,356 | **0.0271** | 2,135 |
**16,950 of the first walk's 17,722 statements are stub-on-sight**, and that
is the one path in the pipeline that is not set-based:
`MatchService._create_stub` calls `TitleRepository.add` per item, and that
add is SAVEPOINT-wrapped, so a new title costs three statements. It is
bounded by **new titles** (94,438 movies + 32,409 series), never by items —
an episode never walks the ladder, so the other 999,827 items cost nothing
there — and a second walk creates none. Batch-level cost is 772 statements,
0.0154 per item. Throughput is against a local database with no network in
the way; a real walk is bounded by Emby's pages, **measured 2026-08-15 at
4.61 s for the first page and 7.47 s deep** (M10 S1, at the top of this file) —
5,675 pages of a 1,134,919-item library, i.e. **7.3–11.8 h**. The "1–5 s each"
this sentence used to carry was never measured and was about half the truth.
That is 33 items/s off the wire against 1,933–2,135 items/s through the
pipeline below, so **the walk is upstream-bound by a factor of ~60** and every
statement count on this page is 1.7% of the wall clock.
**Four scale risks, planned against the statement the repository actually
issued** (`scripts/measure_ingest.py --scale 1126674`; captured off
`before_cursor_execute`, never transcribed — a hand-copied lookalike drifts
and then reads like coverage, and two earlier tasks here were replaced for
exactly that):

- **`merge_from_source` at 1,126,674 `watch_states` with a 1,000-row batch:
  refuted.** `Nested Loop` + `Index Scan using ix_watch_states_title_id`,
  1,000 loops, 14.5 ms. No hash join, no seq scan.
- **The claim scan behind a wall of backed-off jobs: confirmed, unfixed.**
  216 ms with `Rows Removed by Filter: 1126674`. `ix_jobs_claim` is
  `(priority DESC, created_at) WHERE status = 'pending'` and a backed-off
  job is *still* `pending`, so every poll walks past all of them.
  `run_after <= clock_timestamp()` is not an indexable partial predicate
  (`clock_timestamp()` is not immutable), and putting `run_after` first
  destroys the priority ordering — so this is recorded rather than solved.
  It only bites when a large fraction of the queue is backed off, i.e. when
  an upstream is broken.
- **`list_unmatched`'s `OFFSET`: confirmed.** 43.7 ms at offset 0, 388.9 ms
  at offset 1,126,574 — linear per page, quadratic to drain. Fine for an
  operator reading the first few pages, wrong for a client paging the whole
  review queue; a keyset cursor is the fix when something needs one.
- **The availability sweep: half.** `ix_media_items_sweep`
  (`source_id, available, last_seen_at`) takes the sweep's `UPDATE` from
  `Seq Scan` (`Rows Removed by Filter: 1,126,474`, 173 ms) to `Index Scan`
  with an `Index Cond` on all three columns, 102 ms. It does **not** help
  the guard's `count(*)`, a `Parallel Seq Scan` with the index (87 ms) and
  without it (86 ms) — ADR-0015's ceiling is a *fraction*, so the
  denominator is unavoidable and a source that *is* the whole table gives
  `source_id` no selectivity. Both numbers are in migration
  `f1a7d3c9e824`, not the flattering one alone.

Verified working as of M3 (the Emby adapter) — a source can be registered
and interrogated over HTTP, and the suite is 865 tests (733 unit / 132
integration), mypy strict clean over `src` and `tests`, 6 import contracts:

```bash
uv run usher --help                              # the CLI, also installed as `python -m usher`
uv run pytest                                    # 1744 passed + 1 skipped (1320 unit / 425 integration)
uv run pytest tests/unit                         # 1319 passed + 1 skipped, no Docker and no network
uv run pytest tests/unit/test_adapters_emby_contract.py  # the contract suite against the real adapter
uv run mypy src tests                            # strict, including tests/
uv run ruff check --no-cache . && uv run ruff format --check .
uv run lint-imports                              # 8 kept, 0 broken

# Register a source and read its health, against a running app:
curl -sS -X POST http://localhost:8000/admin/sources \
  -H 'content-type: application/json' \
  -d '{"kind":"emby","name":"Living Room Emby","base_url":"https://emby.example","username":"...","password":"..."}'
curl -sS http://localhost:8000/admin/sources/<id>/status

# Diff a live server's *shape* against the committed fixtures. NOT a test,
# and its output is deliberately never committed -- see the module docstring.
export USHER_EMBY_URL=... USHER_EMBY_USER=... USHER_EMBY_PASSWORD=...
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json

# The same thing for TMDb. Verified working against the live API 2026-08-01;
# `set -a; . ./.env; set +a` rather than a literal key, so no credential ever
# reaches a shell history or a recorded command.
set -a; . ./.env; set +a
uv run python scripts/capture_tmdb_fixture.py --kind movie  --id 550   > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind series --id 1399  > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind season --id 1399 --season 1
uv run python scripts/capture_tmdb_fixture.py --kind search --query Dune --year 2021
uv run python scripts/capture_tmdb_fixture.py --kind changes
```

**A live run against this Emby server must be bounded, and the bound has to
be in the *iterator*, not in `max_pages`.** Exhausting `max_pages` raises
`PortDataMalformed` — it is the walk's dead-man's switch — so a reconcile
bounded that way records `FAILED` and never reaches the sweep, which is the
half of the pipeline the run exists to exercise. Truncate the async
generator instead. Learned the expensive way in the same run: a probe that
walked `adapter.watch_state()` *looking for one known item id* is a walk of
1,126,789 items to reach something a filtered listing already had, and it
issued several hundred requests against a shared server before it was
killed. Any "find the item where X" over a walk is a full walk; ask the
server with a filter.

## M10 Task S7 — this server under concurrency, and a tail that was our own TLS (2026-08-19)

**44 bounded read-only requests against the operator's live Emby**, 15:48:51Z →
15:49:13Z, the operator having stated the server was quiet from the last
request. Every probe a `GET /Users/{user}/Items/{item}` over an `external_id`
`media_items` already held — no walk, no iterator, no write, nothing sent to
`/PlayedItems` or `/UserData`, so there is nothing to restore. Driven from
`scripts/measure_source_lane.py` with the base URL, token, user id and device
id redacted from everything printed. Pre-registered bar at
`/var/tmp/m10-gate/BAR-S7.md`
(`sha256 ea7a2b5db249fd9afdd34680f2c91954c60de7a02c19c8a72ab7ecd8ca6ce4f4`,
written 15:39:13 before the first request and re-hashed by the harness at run
time — the digest printed in both run logs matches). Quiet-check: CPU drift
−0.034 against a ±0.1 limit, foreign `pytest` census 0.

**Group S budget: S1 spent 52, S7 spent 142 (98 + 44 — see the lost run below),
leaving 62 of the declared 256 for S8 and S11.**

### The ladder, gate off — this server does not degrade at four

Settings **interleaved and rotated** rather than blocked: interleaving spreads
wall-clock drift across all three (S1's finding), and rotating the order per
round stops `c1` always being the one that pays for a cold cache and subsidises
`c2` and `c4` — a bias that runs in exactly the direction that would make
concurrency look free.

| in flight | n | median | mean | max | steady-state | vs c=1 |
|---|---|---|---|---|---|---|
| 1 | 12 | 0.1377 s | 0.1368 s | 0.1410 s | **7.40 rps** | — |
| 2 | 11* | 0.1405 s | 0.1394 s | 0.1431 s | **14.21 rps** | 1.92× |
| 4 | 10* | 0.1363 s | 0.1370 s | 0.1417 s | **28.75 rps** | **3.89×** |

Overlap, because *"four requests finished"* is also what a serialised loop
produces (`CLAUDE.md`'s fourth evidence rule): peak in flight **1 / 2 / 4**,
mean in flight **1.00 / 1.99 / 3.51**, IoU **0.000 / 0.986 / 0.995**. The
concurrency was achieved, measured on the wire.

**Per-request latency is flat and throughput is near-linear.** The c=4 median
is **1% below** the c=1 median. 🔴 **This refutes the run's own pre-registered
prediction**, which took W1's TMDb result — a 37% per-worker throughput loss at
three workers — as the prior and predicted >20% median degradation at c=4, a
throughput ratio in [1.5, 3.5] and a p95 more than 1.5× worse. Median: refuted.
Ratio: 3.89×, above the band. p95: see below. **A degradation curve measured
against a CDN-backed public API is not a prior for a machine on the same LAN.**

### 🔴 The tail was the harness's own connection pool, and the naive reading would have moved a shipped default

\* Three requests in the whole ladder exceeded 0.25 s — one in the first `c2`
block (0.4153 s) and two in the first `c4` block (0.4098, 0.4085) — against a
baseline of ~0.137. Read naively that is *"p95 triples under concurrency"*, it
satisfies the pre-registered p95 prediction, and it would have justified moving
`KIND_CONCURRENCY[MATCH]` down to 2.

**It is httpx opening connections #2, #3 and #4.** The evidence is internal and
cost no extra request:

- The slow requests are **exactly** the ones that must open a new connection —
  one at the first appearance of concurrency 2, two at the first appearance of
  concurrency 4 — and the excess over baseline (~0.27 s) is a TLS handshake to
  a remote host.
- **Every later block at every setting is clean.** `c4`'s second and third
  blocks have a max of 0.1407 and 0.1360; `c2`'s have 0.1431 and 0.1420. If the
  server degraded under concurrency the tail would recur, and it never does.
- **Server-side caching is excluded**, which is what makes this decisive rather
  than plausible: round 0 ran `c1` first over the same four item ids, so by the
  time `c2` and `c4` ran, those items had already been served once and twice.
  The only thing new at each first block is a **connection**.

With the three handshakes removed, `c4`'s max (0.1417 s) is within 0.5% of
`c1`'s (0.1410 s). **The general form: a harness that opens connections lazily
manufactures a latency tail at exactly the moment it first raises concurrency,
and that tail is indistinguishable from server contention in any summary
statistic.** Warm the pool to the maximum concurrency before the first measured
block, or — as here — keep enough structure in the run that the artifact can be
isolated afterwards. Nearest relative in `mutation-sweeps.md` is the `cp -a`
venv shebang: a complete, plausible, wrong result.

### Arm C — the shipped default makes the concurrency entry moot, measured on the wire

Six requests, four coroutines in flight, `USHER_SOURCE_REQUESTS_PER_SECOND` at
its shipped **0.4**:

| | measured |
|---|---|
| wire send-to-send gaps | **2.5031, 2.5026, 2.5026, 2.5007, 2.5035 s** |
| peak in flight | **1** |
| IoU | **0.000** |

So **`KIND_CONCURRENCY[MATCH] = 4` is not what bounds this deployment's request
rate to a source, and has not been since S3 landed.** `_MinInterval` holds its
lock across the wait and `SourceGateRegistry` gives one source one gate shared
by every adapter, so four job slots queue behind one 2.5 s interval. The entry
bounds jobs in flight — sessions and connections held — and the **gate** bounds
the wire. Raising it would not raise the rate.

🔴 **And the instrument that would have said otherwise was caught by a stub
rehearsal, before it cost a request.** The harness times around
`session.request`, and `EmbySession._send` calls `await self._limiter.take()`
*inside* that region — deliberately, since the gate's wait is its own series.
So the coroutine window under a gated arm is dominated by **queueing**: the
same six requests read **peak in flight 4, IoU 0.802** on that instrument,
which is the exact opposite conclusion. Overlap is therefore computed from
httpx's own `request`/`response` event hooks, downstream of the gate. **When a
component's whole job is to make callers wait, any timer that wraps the wait
measures the waiting and not the work.**

### 🔴 The run before this one spent 98 live requests and retained nothing

The first invocation completed all 96 ladder requests and then raised
`TypeError: build_session() got an unexpected keyword argument 'limiter'` in
the arm-C session builder. The `except` clause caught
`(BudgetExceeded, ProbeFailed, UsherPortError)` — S1's tuple — so the
`TypeError` propagated past every line that reports, and **96 observations
already bought from somebody else's server were discarded**, along with the
two warm-ups. `--timings-out` is written after the try/finally, so it never ran.

S1 had already recorded this shape (*"a run that ends early otherwise loses
every observation it bought"*) and defended against it by catching the budget
exception and reporting anyway. **That defence is a denylist**: it enumerates
the ways a run was *expected* to end, and the way this one ended — an ordinary
programming error in a later arm — is on no such list and never will be.

Three repairs, and the first is the only one that generalises:

- **The report is no longer what makes an observation durable; the write is.**
  Every timing is appended to a JSONL journal and flushed **on arrival**, so a
  crash, a `SIGKILL` or an exception of any type leaves everything already paid
  for on disk. JSONL rather than one document because a partial JSONL file is
  readable and a partial `json.dumps([...])` is not.
- The `except` is widened to `Exception`, so a bug in a later arm cannot
  invalidate an earlier arm's data.
- A `client_factory` seam, so the whole run rehearses against a stub. S1's
  harness has one and this arm did not — which is why its first rehearsal was
  the live server. The rehearsal now runs before every live invocation and
  costs nothing; it is what then caught the arm-C instrument error above.

**The general form: a live-request budget is spent at the transport, but it is
*earned back* only by the write. Journal on arrival, and treat every line
between the last request and the report as code that can lose the run.**

### Still unverified, named rather than implied

- **A *paging* load under concurrency.** Every request here is a single-item
  read. S1 measured a 200-item page at 5.0954 s median — ~34× dearer — and
  nothing has put pages in flight. Four concurrent *pages* is a different
  question and this section does not answer it.
- **Any Emby build but 4.9.5.0**, and any other network path.
- **N > 1 Usher processes against one source.** Every limiter in this group is
  per process and `SourceGateRegistry` is per composition root, so two
  processes are two gates and 0.8 rps. This is the one bound the whole group
  cannot express.
- **Concurrency above 4.** The ladder stops at the entry under test; 8 in
  flight against this server is unmeasured.
- **A real 429 from this server.** S4 met one only against a stub.

## M10 Task S8 — the retraction ceiling on somebody else's library, and the guard had already fired here (2026-08-19)

**One live request.** `scripts/measure_source_drift.py`, a single
`GET /Users/{user}/Items` with `Limit=1&EnableTotalRecordCount=true` — no
iterator, no `StartIndex`, nothing written. Window 23:07:31Z → 23:07:40Z. Bar
`/var/tmp/m10-gate/BAR-S8.md`
(`sha256 e5cbd3a0d0c079492e3259fa3be89801ee4b147b19673b6b07ab706caacd554f`,
written 18:06 before the harness existed, re-hashed by the harness at run time
and again when this entry was written — all three match). Base URL, token, user
id and device id redacted from everything printed.

| | reading |
|---|---|
| source's live `TotalRecordCount` | **1,137,502** |
| Usher's `count(media_items WHERE available)` for it | **11,851** |
| would-retract lower bound | **0** |
| fraction | 0.0000 against a 0.25 ceiling |
| would the guard refuse? | **no** |

⚠️ **Lower bound only, and every use of the number carries the sentence.** A
count is not a set: an owner who removed 300 items and added 300 shows zero
drift here and would still trip 0.25 on a real walk. It bounds the guard from
*below*, which is the useful direction for *"does this fire at all"* — a
reading already past the ceiling proves the guard would fire; a reading under
it proves nothing about a walk.

### 🔴 The probe cannot say anything on this deployment, and the reason is not churn

**D1 predicted the two counts within ±2%. They differ by 96×** — Usher holds
**1.04%** of the source. The bar's rationale (*"S1 measured this library at
1,134,919 items and the catalog was built from it"*) named the wrong table:
`titles` holds **1,272,870** rows and comes from M2's IMDb/TMDb bulk import,
while the guard's denominator is `media_items`, which only an Emby walk fills
and which nothing here had ever filled.

**So the probe's arithmetic is structurally dead whenever Usher's catalogue
lags its source.** `would_retract` is `max(0, usher_available − live_total)`,
clamped at zero because a *grown* source is `upsert_many`'s business rather
than a retraction — and any deployment that has not finished a walk sits on the
clamp. It answers 0 and the 0 means *"Usher is behind"*, not *"the library is
stable"*. **The probe is informative only when Usher's available count is at or
above the source's total**, which is the state a completed walk produces and
this deployment has never reached. That limit is now in the script's own
docstring; it was not in the bar, and it should have been.

### 🔴 D2 is refuted, by the deployment's own history rather than by the probe

Issue #20 asked for a reading *"across at least one genuine churn event"*,
which nobody can schedule. **Nobody had to: the ceiling already fired on this
household, on 2026-08-13**, and the row is still in `sync_runs`:

```
kind=full  status=failed  started 04:14:07Z  elapsed 19.3 s  items_seen=120
error: refusing to mark 60 of 180 items unavailable in one run
       (33% exceeds the 25% ceiling); nothing was retracted
```

**And what tripped it was not churn at all — it was a bounded walk.** 120 items
seen against 180 Usher held available; nobody deleted anything. The run before
it (04:12:29Z, `full`, COMPLETED, **60 items**, 9.6 s) is the only full walk
that has ever completed here, and the two together are ADR-0015's *Context*
arriving as an event: *"a walk that succeeds and returns far less than the
library holds"*, generated by Usher's own bounded test tooling rather than by
the source. `reconcile._walk`'s ceiling comment says the same thing forward —
*"a bounded walk has items it never looked at, so a sweep after one would
retract every one of them"* — and this is it happening.

**The transferable half, and it is the one S9 has to decide against: on a
library the operator does not own, the thing that reaches the ceiling first is
Usher's own partial coverage, not the owner's deletions.** Both produce the
identical `AvailabilitySweepRefused`, and the refusal is the correct answer to
both — but a default chosen against *churn* is being asked to hold against
*coverage*, and only one of those is bounded by how much the household deletes.

### The rest of the deployment's sync history, because it is the sample

28 `sync_runs` rows: 2 full (1 completed, 1 refused), 13 delta (all completed),
13 watch_state (**10 failed, 3 orphaned as `running`, 0 completed — ever**).
`media_items` reached 11,851 almost entirely through one delta on 2026-08-19
that saw 11,295 items; the rest saw 0–221 each. `items_retracted` is **0 on
every row in the table**, so the *accepted* sweep path has never once run
against this server — the only sweeps that ever had anything to retract were
the one that was refused and the ones that never happened because no full walk
finished.

The watch lane's never-completing loop is filed as
[#41](https://github.com/anirudhlath/usher/issues/41) and diagnosed there: no
completed run means no cursor, so every pass walks the whole library and one
transient error restarts it. It is named here because it is *why* the full-walk
column is empty, and therefore why S8's probe had nothing to measure against.

### Still unverified, named rather than implied

- **Churn composition.** Additions and removals net out in a count. Only a walk
  distinguishes them and no walk was run.
- **The ceiling against a *completed* full walk of this library.** Nothing has
  ever produced one — 5,688 pages at S1's measured 6.98 s/page is ~11 hours —
  so the number the guard would see on an honest walk is unmeasured.
- **Per-library scoping.** The probe asks the same `IncludeItemTypes` the
  adapter walks; a library removed from *view* rather than from disk is
  invisible to both.
- **Any Emby build but 4.9.5.0**, one household, one evening.
