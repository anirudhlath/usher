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

The gate is constructed per source (`EmbySession` builds one, keyed on the
source name so its metric series is per source). But the **rate** is one
setting, `USHER_SOURCE_REQUESTS_PER_SECOND`, not a per-source column.

Issue #19 asks whether the ceiling belongs per source — *"a self-hosted server
on the same LAN and a shared server across the internet do not deserve the same
ceiling"* — and the answer is **yes for the key and no for the value in Phase
1**. `Source` carries no tuning field at all (`src/usher/domain/source.py`: id,
kind, name, base_url, credentials_ref, device_id, enabled, supports_push,
created_at, updated_at — ten fields, none a rate), and adding one is DDL this
phase's data model does not open. Per-source *values* are a post-v1 candidate
with the column named, not left to be rediscovered. And M9's S3 already
established that a source is addressed by `source.id`; the gate keys on the same
identity.

The setting is named `source_*` and **not** `emby_*` deliberately — `config.py`
is not an adapter, and a setting named for one media server would be the first
source-specific concept to escape `adapters/`.

### 3. The default is derived from S1, and it binds a different regime than the concurrency ceiling

The default is **0.4 rps**, derived in one sentence from S1. Little's law over
the op class that dominates a walk — a 200-item page — at S1's measured p95 of
9.1713 s, paced by the concurrency `KIND_CONCURRENCY` gives the Emby-facing
kinds today (4): `4 / 9.1713 = 0.436` rps is the rate at which Usher already
goes as fast as this server answers a *concurrent* walk. The default is set
**below** it, so the setting is a courtesy margin rather than a re-statement of
the server's own speed. S7 may lower the concurrency, which only makes this
bind less.

🔴 **The finding that decides how this is written up: at the latency PRD 01
long claimed, a requests-per-second limiter is inert.** Four concurrent
requests each taking two seconds is 2 rps; a ceiling of 2 rps never fires, and
the operator has configured a limiter that does nothing while looking
configured — the exact defect `push_max_items_per_event`'s `le=500` bound was
written against. **At the latency S1 actually measured it is the opposite in
one regime and the same in the other, and neither substitutes for the concurrency
ceiling (#13/S7):**

- **A sequential walk** is one page at a time, ~0.17 rps, and no per-source
  limit above that ever fires on it (S1 states this directly).
- **A concurrent page walk** is ~0.44 rps four in flight, and 0.4 is the
  courtesy margin just under it — the concurrency ceiling is what actually
  bounds the load, and the rate limiter is a hair below it.
- **Single-item reads** at S1's 0.15 s run ~27 rps four in flight, and there
  the rate limiter is what binds every call while the concurrency ceiling of 4
  is nowhere near reached.

So the rate limiter and the concurrency ceiling bound **different regimes**, and
which regime a deployment is in is which op class dominates it — S1's table is
the evidence, not a guess. This is the same shape as `api/deps.py`'s *"a rate
limiter that limits nothing"*, arriving through latency instead of through
instance count.

### 4. The bound is per process, per source

`.claude/rules/api-telemetry-and-lanes.md`'s W1 entry already records that
*"`USHER_JOB_CONCURRENCY` and `USHER_TMDB_REQUESTS_PER_SECOND` are both per
process, against a rate limit that is per client"*. `USHER_SOURCE_REQUESTS_PER_SECOND`
is the same shape: the gate lives on one `EmbySession` in one process, so **a
second `usher work` container is a second process and doubles this one too.**
Said out loud rather than discovered.

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
