---
paths:
  - "src/usher/api/**"
  - "src/usher/telemetry.py"
  - "src/usher/composition.py"
  - "src/usher/services/jobs.py"
  - "src/usher/services/events.py"
  - "src/usher/services/playback.py"
  - "src/usher/services/playback_ticket.py"
  - "src/usher/services/titles.py"
  - "src/usher/services/visibility.py"
  - "src/usher/services/sources.py"
---

# The HTTP surface, OpenTelemetry and the supervised lanes

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed. The always-on conventions live in `CLAUDE.md`; this file is the
evidence.

## Run it, and re-derive anything you are about to quote

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head
uv run uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000
curl http://localhost:8000/health        # liveness  — {"status":"ok"}, 200 always
curl http://localhost:8000/health/ready  # readiness — 200 or 503
curl -sD- -o/dev/null http://localhost:8000/health | grep -i traceresponse

uv run pytest --collect-only -q | tail -1                 # suite size
uv run lint-imports                                       # contract count
ls src/usher/api/routers/*.py | grep -v __init__ | wc -l  # routers
grep -oE 'Postgres[A-Za-z]*Repository' src/usher/api/deps.py | sort -u | wc -l
grep -rc 'push_enabled=False' tests/ | grep -v ':0'       # lane-switch fixtures
```

**Every count in this file is dated because every one of them has gone stale at
least once.** As of **2026-09-02**: **5,747** tests collected, **12**
import-linter contracts kept, **17** routers, **20** distinct
`Postgres*Repository` constructions in `api/deps.py`, **74**
`push_enabled=False` occurrences across **51** test files.

## SSE, and why `ASGITransport` cannot test it

**`httpx.ASGITransport` buffers the whole response and therefore cannot test
SSE at all.** Its `handle_async_request` runs `await self.app(scope, receive,
send)` to *completion*, collects every `http.response.body` into a list, and
only then builds a `Response` over the joined bytes — so `client.stream("GET",
"/events")` against a route whose whole purpose is not to complete blocks
inside the transport forever, and every case written against it hangs rather
than fails. `tests/fakes/streaming_asgi_transport.py` is the replacement: the
app runs in a task, `http.response.start` resolves a future, chunks go on a
queue, `aclose()` sends `http.disconnect`. Its scope carries `spec_version:
"2.3"`, matching uvicorn 0.51's own, and that is load-bearing —
`StreamingResponse.__call__` only runs `listen_for_disconnect` below spec 2.4,
so at 2.4+ a client going away would not cancel the body iterator and the
route's `finally` would never run.

**The pending `__anext__` is held across heartbeats with `asyncio.wait`, and
`asyncio.wait_for` is the bug it was written to avoid.** `api/routers/
events.py:118–119` is `pending = asyncio.ensure_future(anext(iterator))` then
`await asyncio.wait({pending}, timeout=settings.sse_heartbeat_seconds)`.
`wait_for` cancels what it waits on, and cancelling `__anext__` *closes the
async generator*, so the next `anext` raises `StopAsyncIteration` and the route
returns — in production, every SSE client disconnecting one
`sse_heartbeat_seconds` after its last event. The six-line reproduction and the
whole argument are already in the comment at `events.py:93–113`; do not
"simplify" that loop back to a `wait_for`.

**A replay ring and a per-subscriber queue are fed by the same `publish` calls,
so a lazily-resolved replay duplicates.** `InMemoryEventBus.subscribe`
snapshots the ring *before* it adds the subscriber, with no `await` in between
(`services/events.py:39` records the same rule at the definition). Resolved
lazily at the first `__anext__` instead — which is what the M5 plan's draft did
— everything published in the window between is in both halves and the client
sees it twice. **The window is real:** the route reaches its first `anext`
through `asyncio.wait`, which yields to the loop, and the push lane publishes
from another task.

## An event is a statement about committed state

**Every measured `events.publish` site commits before it publishes, and the
open transaction at the instant of an `enrich` frame is `JobWorker`'s rather
than the handler's.** Measured 2026-08-11 (M9 G1) against real Postgres 17,
each site driven on a **committing** session with a second connection reading
the event's own subject inside `publish`:

| publish | sees | committed at |
|---|---|---|
| `services/enrich.py:339` | `enrichment_state='enriched'` | `enrich.py:226` |
| `push.py:209`, `push.py:244` | the merged `watch_states` row | `push.py:170` |
| `push.py:278` | the `media_items` row | `push.py:275` |
| `services/reconcile.py:288` | `sync_runs.items_seen` at 2 then 4 | `reconcile.py:266` |

So PRD 09's carried-debt sentence — *"a client is told an event landed before
the transaction that produced it committed"* — is **false of the event's
subject at every site measured**, and is corrected there and in
`docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md`.

What survives is smaller and one layer up. At the same instant, the only `jobs`
row visible for that key is `('enrich', 'running')`; the two `BACKFILL`
requests staged at `enrich.py:308–312` become `('derive','pending'),
('index','pending')` only after `JobWorker._run` reaches `complete(job.id)`
(`services/jobs.py:501`) and the commit beside it (`:505`). **That pair, plus
the `DELETE` that completes the job, is the entire residual window**
— so a rollback there costs two enqueues and one duplicate `title.updated` on
the recovery re-run, not a lie to a client. **The property G2 buys is ordering,
not durability, and it needs no outbox table.**

⚠️ **The table above is a sample, not the tree.** M9's E7 added
`BootstrapService._publish_progress` (`services/bootstrap.py:473`), which
satisfies the same rule at the same reading. It is deliberately **not** in
`DeferredEventPublisher`: deferring one frame per committed batch behind a
26-batch `--phase imdb` load is the 0%-to-100% jump the frame exists to
prevent, there is no residual window left for the buffer to close, and
`discard()` on a failed job would drop frames naming batches that really did
commit. ADR-0033 carries the amendment; `composition.build_worker` carries the
argument at the registration site, pinned from both sides because swapping the
two publishers there is invisible to every unit case of `JobWorker`. And
`services/watch_write.py:274` and `:296` publish two frames G1 never drove, so
the write-back lane is **unmeasured**. Re-measure a site before quoting the
rule at it.

**`xmin` is not evidence of an uncommitted read, and a whole milestone's worth
of agents read it as one.** `test_sse_end_to_end.py`'s `assert await
_job_xmin(...) is None` failing as `assert '745' is None` was relayed three
times as *"a row a transaction has not committed is visible"*. Postgres never
shows an uncommitted row version to another connection; `xmin` names the
transaction that wrote the version the reader **can** see. Measured at the
failure: `xmin='745', status='running', attempts=0`, against the reader's own
`pg_current_snapshot() = '749:749:'` — an **empty** in-progress list — so 745
is settled and committed. It is the *claim's* `UPDATE` (`run_once`,
`services/jobs.py:299`, committed in the `_claim` helper it calls at `:390`)
still being current because the `DELETE` has not committed. **Before reading an
`xmin` as a visibility anomaly, read `pg_current_snapshot()` beside it: if the
writer is below the snapshot's xmin and absent from its in-progress list,
nothing anomalous happened and what you are looking at is an ordering window.**

**The same case's flakiness is that window, and the control three separate
reports got wrong was the variable, not the count.** Unplanted at load average
7–9: **6 failures in 13 runs**, every one on `_job_xmin` reporting the
identical row state. With `await asyncio.sleep(0.25)` planted in
`JobWorker._run` between the handler returning and `complete(job.id)`: **5 of
5**, still on `_job_xmin`, with `probe.seen` and the refetch both passing —
which is what separates *"the assertion races the completing commit"* from
*"the client was told too early"*. Three implementers reported 5/5, 3/9 and
"green, 927 integration passing" on the same base; all three are consistent
with a load-sensitive race and **none distinguishes a defect from a scheduling
window, because a rate is not a mechanism.** A planted delay is, and it costs
one line.

**A probe that never ran records nothing, and every absence claim over it
passes.** The G1 harness for `push._apply_items` first recorded `[]` — the
fixture had seeded no title the match ladder could find, and `_apply_items`
publishes only for an outcome carrying a `title_id`. Read as a result it says
*"the availability event publishes nothing"*. `test_sse_end_to_end.py` asserts
`probe.seen` non-empty before any claim is read out of it.

## OpenTelemetry: providers cache, instrumentors bind early

**`SQLAlchemyInstrumentor` was wired and produced no spans at all, for three
milestones.** `instrument()` patches the *module attribute*
`sqlalchemy.ext.asyncio.create_async_engine` with `wrapt`; `usher.db.base` did
`from sqlalchemy.ext.asyncio import create_async_engine` at module scope, which
is evaluated long before `configure_tracing` runs and binds the **original,
unwrapped** function into that namespace forever — verified directly: after
`instrument()` the two names are different objects. The failure is silent in
the worst way: the package is installed, the wiring reports success, and
`connect` spans still appear (`_wrap_connect` patches `Engine.connect` on the
*class*, so it fires however the engine was built) while not one
`SELECT`/`INSERT`/`UPDATE` span is ever produced. `build_engine` now calls
`sa_asyncio.create_async_engine` through the module. **A test that accepts a
`connect` span is not enough; assert on a *statement* span.**

**`set_meter_provider` is set-once and `_ProxyMeter` caches, exactly like the
tracer.** Every `usher` module calls `metrics.get_meter(...)` at import time,
so each holds a `_ProxyMeter` whose instruments are `_Proxy*` shells that cache
the first real instrument they are handed. Without
`tests/conftest.py::reset_otel_meter_provider`, three rounds of "install a
`MeterProvider` with an `InMemoryMetricReader`, record through
`usher.services.jobs._job_duration`, read the reader" print the metric once and
then raise `AttributeError: 'NoneType' object has no attribute
'resource_metrics'` — the second `set_meter_provider` is refused and the second
reader is never registered with any provider.

`SQLAlchemyInstrumentor` needs the same treatment and the shared reset cannot
give it: it resolves its tracer *once*, eagerly, into a `wrapt` closure, so it
is a real `Tracer` rather than a `ProxyTracer` and nothing in `usher.*` holds
it. `tests/integration/test_pipeline_spans.py`'s fixture calls
`SQLAlchemyInstrumentor().uninstrument()` before installing its provider;
without that line its database-span case passes alone and finds an empty
exporter when it runs third in its own file.

**Pipeline spans nest under the request's server span, asserted as parentage.**
`test_pipeline_spans.py` walks the chain `match.title → ingest.item →
sync.reconcile → GET …` on a real `create_app()` through a real request, with
SQLAlchemy statement spans under the pipeline span that issued them. A pipeline
that started its own *root* spans passes every other assertion in this
repository — valid ids, exporting traces, PRD 10's span names all present — and
fails only this. A worker's `job.*` span is the deliberate exception: a root
with a `Link`.

**The cache counters are `CACHE_HITS`/`CACHE_MISSES`, module-level in
`telemetry.py`** (`:706` and `:709`), imported by `services/rows/cache.py:113`
and `services/images.py:60`. They live in `telemetry.py` because neither
caller's own module could hold them without a cycle — `telemetry.py` imports
nothing from `usher`, which is what makes it the safe home rather than merely a
convenient one — and their descriptions name no cache in particular, because
"Row/screen" was a lie the moment the pair had two callers.

*Moved here from `mutation-sweeps.md` on 2026-09-01; found in M9's screen-cache
counter sweep.* **A positional swap on a `Counter.add` is a clean kill, not an
equivalent mutant.** `CACHE_HITS.add({"cache": "screen"}, 1)` — arguments
reversed — dies because
`opentelemetry.sdk.metrics._internal.instrument.Counter.add` calls
`math.isfinite(amount)` before anything else, and `math.isfinite` on a `dict`
raises `TypeError: must be real number, not dict` (confirmed directly). Every
case in `test_telemetry_cache.py`/`test_services_rows_cache.py` installs a real
`MeterProvider`, so `Counter._is_enabled()` is true and the swap is caught.
Reporting it as a surviving control would have been the exact inversion
`mutation-sweeps.md`'s controls exist to prevent: a kill mistaken for a
survivor hides a broken control rather than a broken suite.

**An observable OTel callback cannot query this database.** OTel invokes it
from the metric reader's *background thread* and every database call here is a
coroutine on asyncpg, so a callback that queried would have to bounce a
coroutine onto the loop (`run_coroutine_threadsafe`) and block the exporter
thread on it — a deadlock whenever the loop is itself blocked.
`usher.telemetry.register_queue_gauges` therefore takes a **synchronous**
reader returning the caller's most recent *complete* re-read of the `jobs`
table (`usher work` refreshes it after every pass): stale but never wrong,
unlike the counter-incremented-on-enqueue the plan was guarding against. The
SDK also keeps only the **first** observable gauge registered under a name and
silently discards the rest (verified directly), so the reader is a module
global that is replaced rather than a closure captured at instrument creation.

**Where the providers are installed, and what is conditional.** Every request
gets a real server span (`FastAPIInstrumentor`, wired in `create_app`) with
SQLAlchemy queries and outbound httpx calls nested under it
(`SQLAlchemyInstrumentor`/`HTTPXClientInstrumentor`, wired in
`configure_tracing`) — without this, nothing called
`tracer.start_as_current_span()` during request handling, so
`inject_trace_context` never fired in the running service, only in tests that
built their own span. `configure_tracing`/`configure_metrics` install a real
`TracerProvider`/`MeterProvider` **unconditionally** (a bare provider with zero
processors still assigns valid ids and records instruments, verified directly);
only the OTLP *export* is conditional on `settings.telemetry_enabled`. Both are
`isinstance`-guarded against reconfiguration on a second `create_app()` in the
same process: without the guard, 5 calls with telemetry enabled leaked 5
background export threads; with it, flat at the 2 the first installs. With no
`OTEL_EXPORTER_OTLP_ENDPOINT`, nothing gRPC-related is ever constructed; with
an endpoint set and nothing listening, the SDK's retry loop logs a warning
rather than raising or hanging the app.

## Logging is reclaimed, and `.disabled` is below every hook

Stdlib `logging` (uvicorn's access/error logs, SQLAlchemy warnings, the OTel
exporter's retries) is bridged into loguru via `_InterceptHandler` — without
it, confirmed on a live run, only `usher`'s own logger calls were structured
JSON; everything else printed as plain text, ignored `log_level`/`log_json`,
and never got `trace_id`/`span_id` patched in.

**`configure_logging` reclaims logging from libraries that took it, and it was
not reclaiming `.disabled` — so one `fileConfig` call muted a logger for the
rest of the process.** Found 2026-08-10 from CI, and the shape of the failure
is the finding: `uv run pytest tests/unit` was green, `uv run pytest` was not.
`fileConfig`/`dictConfig` default `disable_existing_loggers` to **True** and
set `.disabled` on every logger their own config does not name;
`db/migrations/env.py` calls `fileConfig` against an `alembic.ini` naming only
root, sqlalchemy and alembic, and the integration suite migrates in-process. So
by the time the unit suite ran, `httpx` was disabled and
`test_httpxs_per_request_info_line_does_not_reach_the_sink` failed on its
second arm — the WARNING that must still *arrive*.

The repair is one line in the reclaim loop beside the existing `handlers = []`
/ `propagate = True`, and it belongs there rather than in `env.py` alone
because of where `logging` checks the flag: **`Logger.handle` tests `.disabled`
below both the level check and the handler walk**, so nothing this function can
do to sinks, levels or handlers reaches a disabled logger. `env.py` also passes
`disable_existing_loggers=False` (`db-and-sql.md`); neither subsumes the other,
because any dependency may call `dictConfig`. (The loop also snapshots
`logging.root.manager.loggerDict`'s keys before iterating — `getLogger` on a
`PlaceHolder` entry can insert parent placeholders mid-iteration.)

**Two negative-assertion traps, one per subsystem, and the same fix.** An
intercepted-record path is asserted almost entirely by what must *not* arrive,
so the single case requiring a stdlib record to *arrive* is what caught the
total mute above — both arms, or a "nothing reached the sink" fix passes. The
mirror, moved here from `mutation-sweeps.md` on 2026-09-01 (M9's curate-CLI
sweep): **a `sink == []` assertion is a false green wherever the fixture makes
the logging impossible.** `usher curate` printed a ~900-character `httpx` INFO
envelope on stdout in front of its report on the shipped defaults —
`report=False` silences *Usher's* line and can do nothing about a third-party
library's — and the integration fixture substitutes `FakeLLMClient`, which
opens no socket, so `sink == []` passes against a shipped path that logs. The
case with teeth is one layer down in `tests/unit/test_telemetry.py`, driving
the **stdlib** logger through `configure_logging` and asserting through a
**DEBUG** loguru sink, so a "fix" that raised the threshold instead would fail
it. **Before writing a negative assertion about output, ask what in the fixture
makes the output impossible, and put the case where that thing is real.**

## Health, readiness and the lanes

**`/health` and `/health/ready` are deliberately different.** Liveness must
never depend on Postgres — a database outage is not a reason to kill and
restart the process — so only readiness executes `SELECT 1` and, only if that
succeeds, compares the live `alembic_version` table against
`usher.db.migrations.status.code_head_revision()` (PRD 08: *"the app refuses to
serve on a schema mismatch rather than guessing"*). Readiness returns **503**,
not 200-with-a-body: no PRD text pins a status code, but a readiness probe's
real consumers — Kubernetes, Docker `healthcheck`, load balancers — gate on the
code and never parse the body. Verified against a real container: stopping
Postgres mid-session leaves `/health` at `{"status":"ok"}`/200 while
`/health/ready` switches to `{"status":"degraded","checks":{"database":false,
"migrations":false}}`/503 — same process, no restart, self-healing when
Postgres returns. Corrupting `alembic_version` gives the same shape with
`database: true, migrations: false`. Both responses are typed
(`api/dto/health.py`), so `/openapi.json` describes real shapes instead of
`{"type": "object"}`.

**Readiness reports the lanes and never gates on them, and the case that proves
it cannot live in the unit file.** `tests/unit/test_api_health.py`'s app points
at an unreachable database, so readiness is *already* 503 there and both
mutations — `all(checks) and lanes.running_sources()`, and moving `push` inside
`ReadinessChecks` where `all(...)` picks it up automatically — survive every
case in it. Against a **reachable** database with no lanes running, both turn a
200 into a 503 and both die, so that case lives in
`tests/integration/test_health.py`. `LaneReport` is a separate model from
`ReadinessChecks` for exactly this reason: every field of the latter is part of
the status code by construction.

**The server process runs the lanes, and that is proved by a job disappearing
rather than by an assertion about wiring.** `create_app`'s lifespan builds a
`LaneSupervisor` and starts a push lane per enabled source plus one job worker
(both settings-gated, PRD 01's `--worker` flag as configuration). A unit test
of the supervisor proves it does what it is told and says nothing about whether
the lifespan tells it anything. `tests/integration/
test_lanes_in_the_server_process.py` commits a real `match` job, starts nothing
but `LifespanManager(create_app(settings))`, and asserts the row is gone before
the app stops — with the mirror case (`worker_enabled=False`, the row survives)
as the control that makes it evidence. The mutation `await lanes.start()` →
`pass` fails **exactly that one case**: out of 2,072 when it was scored, 5,747
on 2026-09-02.

**Both lane switches default on, so every test that builds an app has to say it
does not want them.** Every app-building fixture passes `push_enabled=False,
worker_enabled=False` — 74 occurrences across 51 test files on 2026-09-02.
Without it a worker lane polls the real `jobs` table under
`tests/integration/test_pipeline_spans.py`, which enqueues jobs through its own
probe route and asserts on them; and a push lane in
`tests/integration/test_admin_sources.py` builds the **real** `EmbyAdapter`
against `https://emby.invalid` and opens a socket, because
`dependency_overrides` do not reach the lifespan. Stated per fixture rather
than defaulted in `conftest.py`, so it is greppable.

**`start()` creates tasks and awaits nothing, and the case with teeth drives
the coroutine by hand.** `coro.send(None)` must raise `StopIteration`; a
`start()` that read the source list inline hands back a future instead. That is
what keeps `/health` at 200 with Postgres down — the M5 plan's own draft did
`await self.refresh()` there, which opens a connection, and its own Step 4 then
asserted the opposite. The first refresh happens *inside* the refresher task,
which refreshes and then sleeps, so nothing waits
`USHER_PUSH_SOURCE_REFRESH_SECONDS` for its first lane either.

**Per-lane crash isolation comes from one task per lane, not from the
`except`.** Measured: deleting `_guard`'s `except` survives the whole suite,
while removing `return_exceptions=True` from `stop()`'s gather fails **11**
cases on its own — so the two are not the belt-and-braces pair a comment
claimed. What `_guard` buys is that a crashed lane is not silent (without it
CPython reports an unretrieved task exception at GC time, to stderr, with no
source name in it), which needs a log assertion to see. And `running_sources()
== ["B"]` is not a test of isolation: a supervisor whose second lane was
created and never scheduled reports the same thing. The case asserts B ingests
an item pushed *after* A's task is already `done()`. Two lanes genuinely
overlapping is its own measurement — **99.3–99.4% of their union over five
runs**, against a serialised supervisor's 0.0.

**A guard can be right and unobservable, and `_write_push_available`'s is.**
Deleting its "nothing changed" check does not move `sources.updated_at`:
`PostgresSourceRepository.update` sets attributes on a *loaded ORM row* and
SQLAlchemy's unit of work emits no `UPDATE` when none actually changed, so the
`set_updated_at` trigger never fires either way. Recorded as an equivalent
mutant against today's repository and kept, because the day that repository
issues a bare `UPDATE … SET` a flapping lane moves a column an operator reads,
once per reconnect. Same treatment M4 gave `_ENQUEUE`'s `GREATEST`.

**Recovery is a lease, not a startup sweep — and the old entry's conclusion is
gone with it.** The pre-W1 rule read *"`JobWorker.startup()` requeues
everything left `running`, so there is one worker per deployment, not per
process"*, and it was true of `startup()`: `requeue_running`'s default
`older_than_seconds=0.0` is correct at exactly one worker and at two steals the
other's live claims. W1 (2026-08-12) moved recovery onto a lease.
`JobWorker.recover()` (`services/jobs.py:268`) passes
`older_than_seconds=self._lease_seconds` (`jobs.py:288`) and **never requeued
everything**; `api/lanes.py:638` calls it once per throttle interval, half a
lease. Two workers no longer take each other's live claims, so
`USHER_WORKER_ENABLED=false` beside a `usher work` container is a *capacity*
decision, not a correctness one.

⚠️ **`grep -rn 'startup()' src/` still returns seven hits and every one is
prose.** `grep -rn 'def startup' src/` is empty — the method is gone. The
string survives in comments at `cli.py:570`, `api/lanes.py:64`, `:172`, `:605`,
`services/events.py:51`, and `services/jobs.py:34`, `:106`, all of them
recording what `recover()` replaced. A reader who greps to check this entry
gets hits; read what they say before concluding the method is back.

**`SourceStatus` refuses "push available without being authenticated", and
`dataclasses.replace` re-runs `__post_init__`.** So the obvious one-liner for
reporting a running lane's push health — `replace(status,
push_available=self._push_health(source_id))` — raises `ValueError` out of
`GET /admin/sources/{id}/status` for a state a rotated password produces, on
the screen an operator opens to diagnose it.
`SourceService._with_lane_push_health` (`services/sources.py:166–191`) takes
the lane's answer **only when the status is authenticated**; the adapter's own
answer stands otherwise, because claiming a working channel on a source that
cannot authenticate is the more misleading of the two. `replace` rather than
`.evolve()` is deliberate: `CLAUDE.md`'s frozen-model rule is about
`usher.domain`'s `DomainModel` subclasses, and `SourceStatus` is a port DTO.

## Wiring, dependencies and the import contracts

`get_session` (`api/deps.py`) is the request's commit/rollback boundary:
commits once the handler completes without raising, rolls back and re-raises
otherwise. Before it, nothing in `src/` ever called `commit()` —
`ports/repository.py`'s "the caller owns the session and the transaction" had
no concrete caller, so a write endpoint that forgot to commit would have lost
data silently.

`tests/integration/test_health.py`'s async `client` fixture needs
`asgi_lifespan.LifespanManager` wrapping the app: `httpx.ASGITransport`
implements the ASGI "http" protocol and not "lifespan", so a bare
`AsyncClient(transport=ASGITransport(app=app))` never runs `create_app`'s
lifespan and `app.state.session_factory` is never set — `/health/ready` then
raises `AttributeError` while the other two tests in the file still pass.
`deps.py`'s `get_session_factory` raises a diagnosable `RuntimeError` for this
exact case instead of Starlette's generic `AttributeError`.

**The default user was reachable only from `usher.cli`, so a server-only
deployment had an empty `users` table** — `docker compose up` against a healthy
Postgres left `watch_states.user_id` with nothing to reference, and the row
appeared only once `work --once` ran. Fixed as
`usher.api.deps.get_default_user_id`/`DefaultUserIdDep`, a **request-scoped
dependency and deliberately not a lifespan call**: a write at startup turns a
database outage into a crash loop and an unmigrated schema into a failure to
boot, trading a documented, tested degradation for a worse one, for a row only
a request ever needs — and would have broken `tests/unit/test_api_health.py`
and `test_telemetry.py`, which build a real app against no Postgres at all.
`tests/integration/test_pipeline_deps.py` drives it through a real request and
asserts the row is *committed*, read back on a second session.

`api/deps.py` carries the whole repository set — **20** distinct
`Postgres*Repository` constructions on 2026-09-02 — plus
`MatchService`/`IngestService`/`ReconcileService`/`WatchStateSyncService`.
**`EnrichService` is deliberately absent**: its provider owns the token bucket
that keeps this deployment under TMDb's ~40 rps ceiling, and a request-scoped
`TmdbClient` gives every concurrent request a *fresh* bucket — N in-flight
requests get N × 30 rps, a rate limiter that limits nothing. It belongs on
`app.state` at lifespan, and nothing in PRD 07's surface calls enrichment
directly (M5's demand promotion enqueues a job; `usher work` runs it).

**`TitleReadService` holds no `SourceAdapter`, and that is asserted on its
imports rather than on its behaviour.** PRD 08's "a degraded subsystem narrows
functionality; it never fails a request local state can answer" is a property
of the code only if the failing call is *absent* rather than caught — "it did
not raise" is also what a service that swallowed everything would produce. Two
things the obvious check misses, both measured: a signature check spelled
`parameter.annotation in (SourceAdapter, ...)` (or via `annotation.__name__`)
does not see a **string** annotation, the one form needing no import at all;
and an `ast.ImportFrom`-only scan does not see `import usher.ports.source`.
Read the annotation as text and walk both node types.

**`usher.composition` is the wiring both roots share, and it needed no new
contract of its own.** `usher.cli` carries one saying nothing may import it, so
shared code cannot live there. The new module sits outside every contract's
source list — and that hole is closed by what it *imports* rather than by a
rule: it imports `usher.db` and `usher.adapters`, so a core module reaching it
breaks the hexagonal-layering and adapters-are-driven contracts, which report
indirect chains by default. Verified by planting `from usher.composition import
Pipeline` in `usher/services/push.py`: **4 kept, 2 broken**, against the six
contracts that existed at M5.

**That argument covers the core layers and not `usher.api`, which M8 had to
close with a new contract (2026-08-07).** Those two contracts are sourced at
`usher.domain`, `usher.ports` and `usher.services` only, so the indirect chain
that catches a core module reaching `usher.composition` does not exist for a
router — and `usher.api` is a composition root, so it is *allowed* to reach
`usher.db` and `usher.adapters` directly. A router doing `from usher.composition
import build_curation_service` (the one public factory in `src/` returning a
`CurationService` holding an `LLMClient`) therefore passed every contract then
defined, ruff, mypy and both suites — planted and measured. The contract added
is *"no router names the wiring, the curation service or the LLM port"*.
**It requires `allow_indirect_imports = true` and does not hold without it**:
every router imports `usher.api.deps`, which is the API's composition root and
imports `usher.composition` on purpose, so unflagged the contract is BROKEN at
HEAD on three chains through `api/deps.py` and `api/lanes.py`. With the flag
the line drawn is the intended one — a router may *reach* the wiring through a
dependency and may not *name* it. Verified BROKEN on each of the three
forbidden modules planted directly, in `routers/rows.py` and `routers/home.py`.

⚠️ **Cite contracts by name, not ordinal.** There were six at M5 and eight at
M8's close; `uv run lint-imports` reports **12 kept, 0 broken** on 2026-09-02,
and the numbering has shifted underneath every ordinal ever written down.
`src/usher/composition.py`'s own module docstring still says *"Seven contracts
exist"*.

## Routes, the problem envelope and the DTO scan

**`app.routes` on FastAPI 0.140 does not contain the app's routes, and a walk
over it reads as a passing sweep.** Measured 2026-08-11 building A2's "every
route answers a problem document" case. `include_router` appends **one opaque
`fastapi.routing._IncludedRouter`** per router rather than flattening its
routes into the app, so `[r for r in create_app().routes if isinstance(r,
APIRoute)]` finds **zero** of Usher's own routes — and four of FastAPI's
(`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`) are plain
Starlette `Route`s, not `APIRoute`s, so the filtered walk returns an empty list
a `for` loop iterates happily. Descend through `route.original_router.routes`
recursively, and carry a premise guard that the descent found a known path:
"a run that did not run is not a pass", arriving in a route walk. Note also
that `/admin/sources` is **two** `APIRoute` objects (one per method), so any
per-path assertion has to group by `route.path` first — and Starlette's 405
carries the `Allow` header of the *first* partial match only, so
`PUT /admin/sources` answers `Allow: POST` rather than `GET, POST`.

**RFC 9457's `instance` is the request path, so a 422 for a malformed *path
parameter* does echo the value it rejected, and there is no spelling that
avoids it.** Found 2026-08-11: `GET /titles/not-a-uuid` answers `"instance":
"/titles/not-a-uuid"`, failing M5's
`test_a_malformed_id_is_a_422_that_does_not_echo_it` on its blanket
`"not-a-uuid" not in response.text`. State the distinction precisely, because
the next reader will reach for the blanket assertion again: PRD 08 is about
what a client **submitted as data** — a body or a query string — and both are
still absent, `instance` being `request.url.path` and **never**
`request.url`. No credential is ever in a path in this API; `?q=` is a query
and is dropped. The narrowed case asserts over pydantic's `input` (the field
that carried whole request bodies), not the whole response text.

**A route with a status nobody has minted a code for silently opts out of the
envelope.** The claim four milestones deferred the envelope on was discharged
2026-08-11 by the first route whose honest answer is "the source is down and I
cannot serve this from local state": `POST /titles/{id}/play`. Two reds on the
way, and the second says something about `api/errors.py`: before the route
existed the case failed `assert 404 == 503`; against a route raising a **bare**
`HTTPException(503)` it failed `KeyError: 'code'`. `_CODE_FOR_STATUS`
(`api/errors.py:107`) holds 404, 405 and 422 only, and
`http_error_as_a_problem_document` hands an unmapped status to FastAPI's own
handler rather than inventing a name — so a 503 with no `ProblemCode` member
answers `{"detail": …}` at `application/json`, indistinguishable from the
pre-envelope shape. Adopting the envelope is `raise
ProblemException(status_code=…, code=…, detail=…)`; adopting the *status* is
not enough, and the fix is not to widen `_CODE_FOR_STATUS` (ADR-0030 owns the
vocabulary) but to keep the "every route that can fail declares its problem
responses" scan green.

**FastAPI has no per-response media type, so `/openapi.json` described 56
problem responses at `application/json` while the wire sent
`application/problem+json`.** Measured 2026-08-19 (issue #6) against the whole
document: 56 responses over 35 operations carry a `ProblemResponse`, every one
filed under the wrong key. `openapi/utils.py` renders an additional response's
model under ``route_response_media_type or "application/json"`` — the *route's*
own type, off its `response_class` — and there is no override: adding `content`
to the `responses=` dict **appends** an entry beside the generated one
(`deep_dict_update` merges), so a route would declare its 404 twice, once
truthfully; dropping `model=` to hand-write the `$ref` is worse, because with
no route naming the model `ProblemResponse` stops being a component and every
ref dangles. What works is a post-pass: `UsherAPI.openapi` (`api/app.py`)
delegates to `super()`, then moves any `application/json` body whose `$ref` is
`ProblemResponse` onto `PROBLEM_MEDIA_TYPE`. **Key it off the `$ref`, never off
a status list**, so a route added later is covered by the same act that adopts
the envelope; and it must be idempotent, because `app.openapi()` caches into
`app.openapi_schema` and the override runs again over its own output. A
subclass rather than `app.openapi = …`: the assignment needs a
`type: ignore[method-assign]` under mypy strict *and* obliges the override to
re-implement the cache and the `_openapi_routes_version` invalidation
`FastAPI.openapi` has since grown.

**That debt was carried on a reason that reads well and is wrong.** M9's H2
recorded that spelling the media type in "buys a client nothing it cannot read
off the `type` member" — true of a client that has already decided to parse the
body as a problem document, and a generated one decides that from the
**declared media type**, before it parses anything. *What a document contains*
and *what a consumer is told it contains* are different claims.

**`status.HTTP_422_UNPROCESSABLE_ENTITY` is deprecated behind a Starlette 1.3
module `__getattr__`, so it warns once per *request*, not once per import.**
Use `HTTP_422_UNPROCESSABLE_CONTENT`; both are 422. This suite runs with no
expected warnings, for the reason the `testcontainers` shim was replaced
(`fixtures-and-fakes.md`): a suite with one permanent warning is a suite where
the next real one is invisible.

**A `GET /titles/{id}` leak check may not forbid the word "emby".** The
availability badge carries the name an *operator* typed, and "Living Room Emby"
is a correct value for it — PRD 07's own example spells it that way, so a rule
forbidding the substring forbids the feature. What must not escape is the
source's own **item id**: assert against a distinctive `external_id` and
against the key `external_id`, not against a vendor name.

**`api/dto/` names every model `…Response`, nested ones included, and that is
load-bearing.** `tests/unit/test_api_dto.py` discovers response models by
`name.endswith("Response")` and asserts none declares a credential-shaped field
or a `SecretStr`. `WatchStateResponse`, `AvailabilityResponse` and
`RowCardResponse` are all nested and all follow it; the M9 plan spelled the
playback ones `PlayTarget`/`PlaySource`, under which they would have been the
only models in the package the scan could not see — and `PlayTargetResponse` is
the one model rendering a value derived from a credential-bearing URL.

## M9's live run (H4) — three route findings, measured 2026-08-12

The Emby half of that run is in `.claude/rules/emby-push-and-ingest.md`.

✅ **Starting the shipped app against a real source was itself an unbounded
walk with nothing warning you — closed 2026-08-19 (issue #9).**
`LaneSupervisor` starts a push lane per **enabled** source, and the lane's
reconnect gap-closer calls `reconcile(source, SyncRunKind.DELTA, adapter)`.
Against a real household — 1,126,789 items on the one this project measures —
that was exactly the walk `emby-push-and-ingest.md` forbids, issued by a bare
`uvicorn usher.api.app:create_app --factory` with default settings. H4/H5's run
set `USHER_PUSH_ENABLED=false` and `USHER_WORKER_ENABLED=false` for that
reason, and **those two settings are still what make such a run's request
budget *statable*** — with the lanes on, the count is whatever a websocket and
a gap closer decide.

**`push_gap_min_interval_seconds` looks like the bound and is not.** At its
shipped 60 s throughout, it rate-limits how *often* the gap is closed and says
nothing about how large the walk is. The size lives in
`ReconcileService.cursor_for` (`services/reconcile.py:200`, public since this
fix and for exactly this reason): a DELTA resumes from the newest *completed*
item-lane run, so with none there is no `since` and `list_items(since=None)`
reads the whole library. `LaneSupervisor._close_gap` (`api/lanes.py:401`) asks
that method before committing to a walk, and `USHER_PUSH_GAP_CLOSE` (`cursored`
| `always` | `never`, default `cursored`; `config.py:726`) is what it does with
the answer. **The bound is a refusal rather than a cap, and that is not
squeamishness**: a truncated walk records `COMPLETED`, and
`latest_completed_cursor` then reads its `started_at`, so every item the
truncation never reached is skipped by every later delta, silently and
permanently.

**Every arm logs, and the log lives in `_close_gap` rather than in `refresh()`
or `_start_lane`** — the refresher calls those once per
`push_source_refresh_seconds` forever, which is the *"a per-process fact logged
in a per-pass function"* shape `config-cli-and-deployment.md` records against
the pre-W1 `build_worker` call site.
`test_the_gap_close_is_logged_per_close_and_not_per_supervisor_poll` drains
twelve units of work and asserts the sink holds **one** line, because a case
asserting after a single poll cannot tell "once" from "per poll". Writing the
lane cases at all needed a harness change: `FakeSyncRunRepository` was
constructed *per unit of work* in `tests/unit/test_api_lanes.py` — a database
that forgot every completed walk when the session closed, under which no delta
ever has a cursor. It is on `_Fakes` now, beside the queue.

**Playback tickets: `api/deps.py`'s `quote(ticket, safe="=")` is the only
encoding step on that path, and it is a no-op at the shipped length.**
`request.url_for` substitutes a path parameter raw — read from the source
2026-08-11: `starlette.routing.Route.url_path_for` → `replace_params` →
`StringConvertor.to_string`, whose whole body is `str(value)` plus two asserts.
Nothing encodes, so that `quote` is not belt-and-braces over a library that
would have done it. H4 measured the artefact: a ticket for a real Emby direct
URL is **292 characters** of url-safe base64 plus `=` padding, and the segment
Starlette hands back is byte-identical to the one minted — no `%` in the path
at any point. Same day: **`RedirectResponse` re-quotes the `Location` it is
given**, with `safe=":/%#?=@[]!$&'()*+,;"`, leaving a realistic Emby direct URL
byte-identical (`?api_key=…&DeviceId=…` unchanged) and escaping only characters
illegal in a URI anyway (`a b.mkv?q=1|2` → `a%20b.mkv?q=1%7C2`); `%` is safe,
so there is no double-encoding hazard.

**The `deep_link` wrapper does not double-encode, and that is the specific
defect H4 was dispatched to look for.** `PlaybackService._with_tickets`
rebuilds a deep link as `wrap_deep_link(<the ticket URL>)`, so the whole Usher
URL — already percent-encoded once — is percent-encoded again by
`quote(inner_url, safe="")`. Measured against the real route: the `url=`
parameter decoded **exactly once** is byte-identical to the `direct` target's
ticket URL, and a `GET` of that decoded string answers the same `302` to the
same `Location`.

**Ticket expiry, driven against the wall clock rather than a frozen one.**
`TICKET_TTL_SECONDS: Final = 300` is a module constant and deliberately not a
setting, so a live run cannot lower it — the honest alternative is to wait. One
ticket was honoured at **127 s** (`302`) and refused at **312 s**
(`404 ticket_invalid`), and a four-character tamper answered
`404 ticket_invalid` too, which is D1's one-answer-for-expired-and-forged
decision observed rather than asserted. Mint and redeem must happen in the
**same** process: a ticket minted under one `USHER_SECRET_KEY` and redeemed
against a server started with another is undecryptable and looks exactly like a
ticket bug.

## M9 Task W1 — the worker lane is a bounded pool (2026-08-12)

**`build_worker` takes a `UnitOfWork`, not a `Pipeline`, and the lane builds it
once per *process*.** `_run_worker` used to open a session, rebuild the whole
worker inside it and run one pass; the worker now holds a scope factory and
opens a session per claim and per job, so the only per-pass work left is the
gauge refresh, which needs a pipeline of its own and gets one. The build is
still **lazy** — inside the loop, guarded by `if worker is None`
(`api/lanes.py:623`) — because `await self._user_id()` is a database call, and
`start()`'s promise that a lane connects to nothing is what makes `/health`
answer 200 with Postgres down. `api/lanes.py:595`'s docstring states the
once-per-process rule at the definition.

**Three concurrency hazards inside the lane's own wiring, none of them the
claim loop.** Each would have been introduced by adding a `gather` and leaving
everything else alone:

- **`SourceRegistry` held the pipeline.** `resolve` issues two reads of its own
  (`sources.list_all`, `media_items.get_by_external_id`), so a registry
  `rebind`-ed once a pass was a second door onto one `AsyncSession` — not the
  handler's repositories, which the per-job scope separates, but the
  *resolver's*. It now holds only the adapter cache and takes the scope's
  pipeline as an argument (`bound(pipeline)`), making the split a signature
  rather than a convention.
- **Adapter construction had no lock.** It is the one `await` in `resolve` that
  mutates the cache, so two jobs for one source both miss, both authenticate,
  and one adapter is overwritten in the dict and never closed — a leaked socket
  per race, visible only under load. Double-checked locking, with the re-read
  inside the lock so the loser takes the winner's adapter.
- **The event buffer was the worker's**: `discard()` on a failing job emptied a
  *concurrent* job's frames. See
  [ADR-0033](../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)'s
  amendment.

**`asyncio.wait`, never a `TaskGroup` and never `gather`** — the same argument
`_guard` and one-task-per-lane make, one layer down. A task group cancels its
siblings on the first escape, turning one poisoned job into N claims abandoned
mid-write; `gather(return_exceptions=False)` returns while the siblings are
still running and unawaited. Under `asyncio.wait` the first escaping exception
is re-raised after every task has settled, so the lane's own `except Exception`
still sees it and nothing is left in flight; on `CancelledError` (how `stop()`
works) the in-flight tasks are cancelled and awaited, so each job's `finally`
fails or completes it *now* rather than leaving a claim for the lease.

**A count cannot express the defect the lease exists to prevent.**
`test_the_worker_lane_recovers_on_a_lease_and_not_on_every_pass` asserted
`requeues == 1` over three passes — which a lane calling `requeue_running()`
**bare** satisfies exactly as well as a correct one, because the count is the
same and only the *age* differs. The fake now records the argument and the case
asserts it is the lease.

**What two workers still cost is budget, not correctness.**
`USHER_JOB_CONCURRENCY` and `USHER_TMDB_REQUESTS_PER_SECOND` are both per
process, against a rate limit that is per client.

## Issue #31 — a lane switch was gating a request-path resource (2026-08-19)

**`create_app`'s lifespan built the embedding model under
`settings.worker_enabled` and parked it nowhere, so `GET /search?mode=semantic`
answered `422` on every deployment there was** — including one with a live
`openai:BAAI/bge-m3` endpoint and 130,720 vectors, where the identical query
through `usher search` returned coherent results at the same moment.
`?mode=fused` narrowed to `full_text` and said so, which is the degradation
working; nothing said the narrowing was unconditional.

**The lane switch was standing in for a setting that already says the same
thing more precisely.** `composition.embedder` answers `(None, no-op)` unless
`embedding_enabled`, which is off by default and is an operator naming a model,
so `worker_enabled` added nothing except an exclusion — and it excluded exactly
the split deployment this file recommends (`USHER_WORKER_ENABLED=false` on a
server beside a `usher work` container). The model is now built whatever the
lane switches say and parked on `app.state.embedder`, and
`api/deps.get_search_service` reads it. `report=settings.worker_enabled` is the
switch's remaining job and the whole of it: every warning in `embedder` ends
*"index jobs will not be claimed"*, false of a process that claims none.

**Two arguments in the old docstrings, and only one was ever true.**

- *"Would work in development and 500 in exactly the push-only deployment PRD
  08 describes."* Never reachable: no model is `None`, `None` is
  `build_search_service`'s own default, and the answer is the 422 naming the
  missing capability. What made it *look* reachable is that a conditionally
  built resource parked nowhere leaves the attribute **absent** rather than
  `None` — so the fix and the fear are the same line. Pinned by
  `test_a_deployment_with_no_embedding_model_exposes_none_rather_than_nothing`.
- *"A once-per-process 65 MB resource."* Real, **runtime-dependent, and about
  the wrong verb.** It is `fastembed:`'s ONNX session (65 MB, 4.84 s cold);
  `openai:` is an `httpx.AsyncClient` holding no model, and the prefix has
  selected between them since 2026-08-13. It argues against *building* a model
  per API process, not against *reading* one the process built anyway.

**A cost sentence with no date and no runtime named is a measurement of one
configuration wearing the grammar of a rule** — which is why the number went on
being quoted through the release that made it optional.

## `traceresponse` — the id leaves the process (2026-08-19)

**The console was built for a link the backend could not supply, and both
halves looked finished.** `web/docs/patterns.md` §3 makes *"`Problem` MUST
render 'Open trace' into Tempo"* a MUST; `Problem` takes `traceId`/`traceHref`,
`Settings.tempo_url` exists, `/console/config.json` serves it and
`useTraceUrl()` formats the URL — and **no response carried a trace id**, so
the chain was inert on every deployment. Every request has had a real server
span since M1, so nothing had to be *built*; the id had to be **sent**.
`api/trace_response.py` is one ASGI middleware and `telemetry.traceresponse`
(`telemetry.py:88`) is one formatter.

**The header name is not the settled standard the obvious reading suggests, and
checking cost one fetch.** Read 2026-08-19: `https://www.w3.org/TR/
trace-context-2/`, the *published* Level 2 Recommendation, defines
`traceparent` and `tracestate` and **no response header at all** — the string
`traceresponse` does not occur in it. On `w3c/trace-context` `main`,
`spec/21-http_response_header_format.md` is now *Trace Context Server Timing
Metric Format* and the binding has moved onto `Server-Timing` under the metric
name `trace`. **The value grammar did not move** — `version "-" trace-id "-"
child-id "-" trace-flags`, 2/32/16/2 lowercase hex, all-zeroes forbidden on
both ids, `ff` a forbidden version — so the bytes are current and only the
field carrying them is contested. Shipped as `traceresponse` because that is
what `opentelemetry.instrumentation.propagators.TraceResponsePropagator` emits;
`Server-Timing` is one more `set` over the same value if anything wants it.
**"Prefer the standard header" is not a decision until you have read which
document is current, because a header can be implemented everywhere and
specified nowhere.**

**Where an `add_middleware` actually lands under the instrumentor, measured.**
`FastAPIInstrumentor.instrument_app` monkey-patches `build_middleware_stack`
and rebuilds it as `ServerErrorMiddleware → OpenTelemetryMiddleware →
ServerErrorMiddleware → ExceptionHandlerMiddleware → [user middleware] →
ExceptionMiddleware → router`. Two consequences and one gap:

- **The server span is current in the user slot**, because
  `OpenTelemetryMiddleware` holds it open with `trace.use_span` around
  everything inside. So `trace.get_current_span()` read at
  `http.response.start`, *before* delegating, is the SERVER span and not the
  `http send` span the ASGI instrumentation opens inside its own `send`.
- **`ExceptionMiddleware` is inside it**, so both of `api/errors.py`'s handlers
  — the 404/405 problem document and the 422 — carry the header. A 404 is
  exactly when somebody wants the link.
- ⚠️ **A bare 500 does not.** `ServerErrorMiddleware` sends its synthesised
  response through the `send` it was *given* rather than the one it passed
  down, and both of its instances are outside the user slot. Measured against a
  real app: `/health` 200 ✓, `/no-such-route` 404 ✓, `/images/nope` 422 ✓, an
  unhandled `RuntimeError` 500 ✗. **Both alternatives are worse and were priced
  rather than assumed.** OTel's `set_global_response_propagator` does reach it,
  but it is a *process* global (the shape both provider installs already exist
  to defend against), does not honour `is_recording()`, adds an
  `Access-Control-Expose-Headers` this deployment does not need, and its own
  docstring calls it experimental. And overriding `build_middleware_stack` to
  wrap the finished stack makes `instrument_app`'s `isinstance(inner,
  ServerErrorMiddleware)` check fail, at which point it **logs one line and
  skips FastAPI instrumentation entirely** — trading the header on a 500 for
  the span on every request. Read from the installed package's source.

**A raw ASGI middleware rather than `BaseHTTPMiddleware`**, because
`GET /events` is a live `text/event-stream` and `BaseHTTPMiddleware` runs the
downstream app in its own task and wraps `receive` — the exact machinery this
file's SSE entry records `StreamingResponse`'s disconnect handling depending
on.

**The absence rule, in its third subsystem.** `traceresponse()` answers `None`
— no header at all — for a span that is not recording and for an invalid
context, because `00-000…0-000…0-00` is precisely what the guardless version
emits: well-formed to every regex, field-for-field the shipped shape, naming
nothing. Same rule as `_observations`' "no reader means no observation, never a
zero" and `current_traceparent`'s `NULL` for a job enqueued outside a span. The
*sampled-out* case is the half an all-zero check misses: a dropped span has a
perfectly valid trace id and no exported trace, so a link built from it opens
an empty Tempo page — "no trace" and "a trace you cannot find" are different
facts and only the first is one this product may state.

**And the test-side finding: a regex is not an identity test.** Planted a
hard-coded `00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01` in the
middleware in place of the live read. The three shape cases — a well-formed
header on a 200, on a 404 problem document and on a 422 — **all passed**,
because a constant has a shape. What killed it was reading the request's own
SERVER span back out of the tracer through a span processor and comparing the
whole header against it, plus the cheap control that two requests differ (which
catches a *cached* read the identity case would not, since the cache is
populated by the very request that case makes). The mirror plant — deleting the
two guards from `traceresponse()` — left all six shape/identity cases green and
killed exactly the three absence cases: the two halves cover different things.
