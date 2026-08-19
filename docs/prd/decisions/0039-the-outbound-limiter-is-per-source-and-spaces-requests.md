# ADR-0039 — The outbound limiter is per source, spaces requests, and binds a different regime than the concurrency ceiling

**Status:** Accepted — adds the proactive half of [01](../01-architecture.md)'s
*"retry/backoff, and rate-limit handling"* promise that
`src/usher/adapters/http.py` never had, and records what its default is derived
from ([03](../03-sources-and-sync.md), [10](../10-telemetry-and-dashboards.md))

## Context

`src/usher/adapters/http.py`'s module docstring quotes PRD 01's promise of the
rate-limit handling the Emby and TMDb adapters both need, and **what the file
held was entirely reactive**: `retry_after_seconds` parses a `Retry-After`
header and `port_error_for` turns a 429 into `PortRateLimited`. Both are about
a limit already hit. Nothing paced an outbound call to a source at all. That is
issue #19: no outbound rate limiting.

The failure the operator has already hit on this exact server is recorded in
issue #19 — a Home Assistant card that had to cap concurrent loads at 3 because
prefetching *"floods the server and starves visible posters"* for real users. A
media source is a machine somebody is watching television on; being polite to a
server you do not own is the whole of M10 Phase 1.

What a request to this server costs was measured by S1 on 2026-08-15
(`.claude/rules/emby-push-and-ingest.md`, one household, one evening): a
200-item page has a **p95 of 9.1713 s** and a single-item read a median of
**0.1495 s**. The distribution is bimodal by op class, and that fact decides
everything below.

## Decision

Four decisions, each with its evidence.

### 1. A minimum-interval gate, not a token bucket

`_MinInterval` spaces one source's calls at least `1/rate` seconds apart, with
**no burst credit**, under a lock held across the wait.

`TmdbClient` already has a token bucket (`_TokenBucket`) with a second of burst,
and that is right for TMDb: its median request is 0.0588 s over 130,334 live
requests, so a burst of thirty against a CDN-backed public API is absorbed. It
is the wrong shape here. **A bucket permits precisely the flood issue #19
recorded — it banks up to a second of credit while idle and lets a whole
second's worth of calls through at once — and a minimum-interval gate cannot
express it.** So the gate has no bucket to bank.

The lock is held across the wait for the same reason `_TokenBucket`'s is: N
coroutines that each read the next slot and then sleep independently all decide
the same slot is free, which is the burst of N the gate exists to prevent. Held
across the wait, each waiter computes its own slot.

`rate=0` is unlimited — the `ge=0` shape `push_gap_min_interval_seconds` already
uses, not the `ge=1` a size takes — and it does not await, because a disabled
limiter that still slept is one an operator cannot turn off.

### 2. Keyed per source; the *value* is one setting, and why

The gate is constructed on each `EmbySession` (keyed on the source *name* so
its metric series is per source; §4 covers where the gate itself lives and why
that placement is provisional). But the **rate** is one setting,
`USHER_SOURCE_REQUESTS_PER_SECOND`, not a per-source column.

Issue #19 asks whether the ceiling belongs per source — *"a self-hosted server
on the same LAN and a shared server across the internet do not deserve the same
ceiling"* — and the answer is **yes for the key and no for the value in Phase
1**. `Source` carries no tuning field at all (`src/usher/domain/source.py`: id,
kind, name, base_url, credentials_ref, device_id, enabled, supports_push,
created_at, updated_at — ten fields, none a rate), and adding one is DDL this
phase's data model does not open. Per-source *values* are a post-v1 candidate
with the column named, not left to be rediscovered. Addressing a source by
`source.id` is M9's **W1** — the worker's `SourceRegistry`
(`src/usher/composition.py:1631,1668`) — **not** M9's S3, which is the TMDb
priority-tier enrichment run (130,334 requests,
`.claude/rules/tmdb-and-enrichment.md`) and settled nothing about source
addressing. And the gate does not key on `source.id` at all: it keys on
`source.name` (`src/usher/adapters/emby/session.py:189`; the metric records
`{"source": source_name}`), matching the existing `usher.source.request.duration`
label rather than minting a second per-source identity.

The setting is named `source_*` and **not** `emby_*` deliberately — `config.py`
is not an adapter, and a setting named for one media server would be the first
source-specific concept to escape `adapters/`.

### 3. The default is derived from S1, and it binds a different regime than the concurrency ceiling

The default is **0.4 rps**, and its honest derivation needs two of S1's
statistics rather than one silently chosen, so both are named. S1 measured this
server on 2026-08-15 (`.claude/rules/emby-push-and-ingest.md`, one household,
one evening, 24 pooled `list` reps and 12 `get_item`): a 200-item page has a
**mean of 6.0369 s** and a **p95 of 9.1713 s**, and a single-item read a
**median of 0.1495 s**. The distribution is bimodal by op class — the fact that
makes a single rate insufficient.

Little's law over the op class that dominates a walk — a 200-item page — paced
by the concurrency `KIND_CONCURRENCY` gives the Emby-facing kinds today (**4**,
which S7 may lower, and lowering only makes this bind less) yields *two* rates,
because which page-latency figure you divide by is a choice this project already
has a rule about:

- **The expected concurrent-walk rate is the mean-based one.** This project's
  own rule is that *"any Σ over pages wants the mean"*
  (`.claude/rules/emby-push-and-ingest.md`), and a walk is a Σ over pages, so
  four pages in flight come off this server at `4 / 6.0369 = 0.66` rps — the
  same figure S1 records as `N × 0.17` (its sequential `0.17 = 1 / 6.0369` rps
  per page), i.e. **~0.68 rps** four in flight.
- **The conservative ceiling is the p95-based one.** `4 / 9.1713 = 0.436` rps is
  the pessimistic estimate — the rate four-in-flight would sustain if every page
  returned at the p95 rather than the mean — and it is the right figure to set a
  *ceiling* under precisely because it is the *lower* one: a limit below the
  pessimistic estimate is below the expected estimate too.

The shipped **0.4** is below all three readings — `4/9.1713 = 0.436`,
`4/6.0369 = 0.663`, and S1's `0.68` — so it is a courtesy margin under every one
of them rather than a re-statement of the server's own speed.

🔴 **The regime this ADR first got wrong is the concurrent walk, and the
correction is the point of this section.** At the latency PRD 01 long (falsely)
claimed — *"1–5 s per request"* — a requests-per-second limiter is inert: four
concurrent requests each taking two seconds is 2 rps, so any ceiling above 2
never fires and the operator has a limiter that does nothing while looking
configured, the exact defect `push_max_items_per_event`'s `le=500` bound was
written against. At the latency S1 actually measured, 0.4 lands in **three
regimes**, and it substitutes for the concurrency ceiling (#13/S7) in none:

- **A sequential walk** is one page at a time, `1 / 6.0369 = 0.17` rps, and no
  per-source limit above that ever fires on it (S1 states this directly).
- **A concurrent page walk** is the contested one. Under S1's mean-based
  expected rate of ~0.66–0.68 rps a 0.4 gate **binds** it — by roughly 41%
  (`(0.68 − 0.4) / 0.68`), *not* "a hair below" it as this ADR first wrote.
  Whether it binds a concurrent walk **in fact** is **unmeasured and is S7's to
  settle**: S1 reserved exactly this — *"whether it binds a concurrent walk is
  unmeasured and is S7's … a limiter set for courtesy could well reach it"* —
  because every S1 request was sequential and no concurrency figure is licensed
  here.
- **Single-item reads** at S1's 0.1495 s median run ~27 rps four in flight, and
  there the rate limiter binds every call while the concurrency ceiling of 4 is
  nowhere near reached.

So the rate limiter and the concurrency ceiling bound **different regimes**, and
which regime a deployment is in is which op class dominates it — but *"0.4 is
inert on a concurrent walk"* is not one of the things S1's table settles, and an
earlier draft of this section asserted it anyway. The `api/deps.py` parallel
(*"a rate limiter that limits nothing"*) holds only in the sequential regime; in
the concurrent regime the honest statement is that the question is open.

### 4. Where the gate lives today — per adapter instance, and known-provisional pending S3

`.claude/rules/api-telemetry-and-lanes.md`'s W1 entry records that
*"`USHER_JOB_CONCURRENCY` and `USHER_TMDB_REQUESTS_PER_SECOND` are both per
process, against a rate limit that is per client"*.
`USHER_SOURCE_REQUESTS_PER_SECOND` is **not yet even that** at this head, and
this section says so plainly rather than claiming the property S3 will add.

The gate is constructed **inside each `EmbySession`**
(`src/usher/adapters/emby/session.py:189`), and every
`ConfiguredSourceAdapterFactory.build()` mints a fresh session and therefore a
fresh `_MinInterval`. So the gate is **per adapter instance, not per process.**
The measured consequence: the push lane (`src/usher/api/lanes.py:179`,
`_open_adapters`) and the worker (`SourceRegistry._adapters`,
`src/usher/composition.py:1631`) keep **separate** adapter caches, and
`create_app` runs **both lanes in one process** (both settings-gated, both
default on). So a single server process *already* holds **≥2 gates for the same
source**, each pacing independently — a source given a 0.4 rps gate on each of
two lanes sees up to 0.8 rps — plus a transient gate per admin
connection-test and per `usher sync`. **The rate is already multiplied by the
lane count within one process**; a second `usher work` container multiplies it
again, but the doubling does not wait for a second container.

**This placement is known-provisional and anticipates S3.** S3 (*"the limiter
reaches every adapter that dials out"*) is the task that makes *"one gate per
source per process"* true, and it is an **extend/rework, not a tear-out**:
`_MinInterval`, the `take()` call, the `usher.source.throttle.wait` metric and
the `source_requests_per_second` config field all survive — what S3 reworks is
the factory→adapter→session **scalar threading** that today hands the rate down
into a per-session gate. `composition.py`, `factory.py` and the two `emby/`
modules are S3's Files list, not S2's; S2's own minimum was the single reader
`tests/unit/test_config.py::test_every_setting_is_read_by_something` demands —
the one `.source_requests_per_second` line in `src/` (`composition.py:326`) —
and it is placed early rather than left dead, because a knob that reads config
and paces nothing is worse than a live gate in a provisional place. The wiring
is not ripped out; it is described here as what it is.

## Consequences

- **A new metric, emitted rather than documented:** `usher.source.throttle.wait`,
  a histogram labelled `source`, recording the seconds a caller spent inside the
  gate. Zero is a real reading — it is how an operator sees the limiter is
  enabled and not binding — so it is recorded on **every** call the gate
  governs, not only when it waits. A *disabled* gate (`rate=0`) records nothing,
  because "off" and "on and never binding" must not read alike.
  ⚠️ Until Phase 2 installs a metric `View` (S1's finding: `configure_metrics`
  installs none, so every seconds-unit histogram inherits the SDK's
  `(0, 5, 10, 25, …)` boundaries), a sub-second wait buckets coarsely — the
  panel is honest about count and sum and unreadable at the quantile. Phase 2
  owns dashboards; this metric is emitted correctly and reads coarsely until
  then.
- **The limiter lives in `usher.adapters.http` and imports no adapter and no
  config.** The tenth import contract's neighbour — *"the shared http helpers
  import no concrete adapter"* — already covers it, and it takes its `rate` as
  a constructor argument rather than reading `Settings`, so `EmbySession`
  passes it down from the composition root.
- **No behavioural change at the default.** `EmbySession` defaults the gate to
  unlimited; only `composition.adapter_factory` sets the real rate, and every
  request-making test either builds the adapter directly or overrides
  `get_source_adapter_factory`, so no existing test acquires the 0.4 rps gate.

## Evidence

S1's measurement (`.claude/rules/emby-push-and-ingest.md`, 2026-08-15):
`list` p95 9.1713 s, `get_item` median 0.1495 s, bimodal by op class, one
household one evening. The concurrency (4) is `KIND_CONCURRENCY`'s Emby-facing
entry (`src/usher/services/jobs.py`). The `_MinInterval` vs `_TokenBucket`
discrimination is pinned by
`tests/unit/test_adapters_http.py::test_two_calls_are_spaced_and_a_burst_is_not_permitted_after_an_idle_period`,
which asserts the gate spaces five idle-period calls and the bucket bursts them.
