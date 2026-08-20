---
paths:
  - "src/usher/api/**"
  - "src/usher/telemetry.py"
  - "src/usher/composition.py"
---

# The HTTP surface, OpenTelemetry and the supervised lanes

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

## 🔴 A `src/` docstring is not automatically prose — under `api/` it is often a wire artifact

**Found 2026-08-15 by M10's S1, in review, after the change had already been
reported as "prose only, no behaviour" and the claim proven by an AST
comparison.** Filed here rather than beside the measurement that produced it
because *here* is the trigger that fires for the paths it is about —
`.claude/rules/emby-push-and-ingest.md` loads on `adapters/emby/**` and five
`services/*.py`, so a person editing `api/dto/` would never have seen it.
(Precedent: `b737105`, *"file the config finding where it fires"*.)

**The mechanism.** `LaneReport` (`src/usher/api/dto/health.py`) is a pydantic
model, so pydantic emits its **class docstring** as the JSON-Schema
`description` and FastAPI publishes it at `/openapi.json`. A comment
correcting a stale claim was written into that docstring and thereby shipped a
`.claude/rules/…` path, a `⚠️` glyph and an internal task id into the public
API contract — measured on `LaneReport.model_json_schema()["description"]`.

**Why the usual proof did not catch it.** Stripping docstrings and comparing
the AST of every changed module is a genuine proof of *no code change*, and it
is exactly blind to this: the docstring **is** the artifact. `mypy`, `ruff`,
`lint-imports` and the whole suite are equally blind, because the contract is
not asserted anywhere.

**The three places it applies**, none of which a reader distinguishes from
ordinary prose by looking at them:

- a **pydantic model's class docstring** → the schema's `description`;
- a **route handler's docstring** → the operation's `description`;
- a **`Field(description=…)`** → the property's `description`.

**The check that settles it, and it is cheap.** Regenerate the document from a
`git archive` of the base commit and diff the whole thing:

```bash
git archive <base-sha> | tar -x -C /var/tmp/basetree
# then, in each tree:
python -c "import json,sys; sys.path.insert(0,'src'); from usher.api.app import create_app; \
print(json.dumps(create_app().openapi(), indent=1, sort_keys=True))" > openapi.json
diff -u /var/tmp/basetree/openapi.json openapi.json
```

S1's repair moved the correction into a comment; the resulting document
differs from `45398c2` in **exactly one** of 1,691 leaf nodes, and the
difference is the **removal** of a false *"1–5 s per request"* claim that had
been published since M1.

⚠️ **Three descriptions already publish a `.claude/rules/…` path and still
do** — `BootstrapPhase`, `SeasonResponse`, `SuggestResponse`. Measured 3
before S1 and 3 after, so S1 added one and removed it and did not add to the
standing gap. Left alone deliberately: **editing a published `description` is
itself a wire change**, so fixing them inside a task whose claim was "no wire
diff" would have traded a recorded gap for an unreviewed OpenAPI diff. They
are a task of their own, and this paragraph is the record that they are known.

**`httpx.ASGITransport` buffers the whole response and therefore cannot test
SSE at all.** Its `handle_async_request` runs `await self.app(scope, receive,
send)` to *completion*, collects every `http.response.body` into a list, and
only then builds a `Response` over the joined bytes — so
`client.stream("GET", "/events")` against a route whose whole purpose is not
to complete blocks inside the transport forever, and every case written
against it would hang rather than fail. `tests/fakes/
streaming_asgi_transport.py` is the replacement: the app runs in a task,
`http.response.start` resolves a future, chunks go on a queue, and
`aclose()` sends `http.disconnect`. Its scope carries
`spec_version: "2.3"`, matching uvicorn 0.51's own, and that is load-bearing
— `StreamingResponse.__call__` only runs `listen_for_disconnect` below spec
2.4, so at 2.4+ a client going away would not cancel the body iterator and
the route's `finally` would never run.
**All five `events.publish` sites commit before they publish, and the open
transaction at the instant of an `enrich` frame is `JobWorker`'s rather than
the handler's.** Measured 2026-08-11 (M9 G1) against real Postgres 17, each
site driven on a **committing** session with a second connection reading the
event's own subject inside `publish`. `enrich.py:289` sees
`enrichment_state='enriched'` (committed :208); `push.py:209` and `:244` see
the merged `watch_states` row (committed :170); `push.py:278` sees the
`media_items` row (committed :275); `reconcile.py:267` sees
`sync_runs.items_seen` at 2 then 4 (committed :245). So PRD 09's carried-debt
sentence — *"a client is told an event landed before the transaction that
produced it committed"* — is **false of the event's subject at every site**,
and is corrected there and in
`docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md`.

What survives is smaller and one layer up. At the same instant, the only
`jobs` row visible for that key is `('enrich', 'running')`; the two `BACKFILL`
requests staged at `enrich.py:270–277` become
`('derive','pending'), ('index','pending')` only after `JobWorker._run`
reaches `complete(job.id)` + `_commit()` (`jobs.py:143–147`). **That pair, plus
the `DELETE` that completes the job, is the entire content of the residual
window** — so a rollback there costs two enqueues and one duplicate
`title.updated` on the `requeue_running` re-run, not a lie to a client. **The
property G2 buys is ordering, not durability, and it needs no outbox table.**

*Five as of 2026-08-11. **M9's E7 added a sixth**,
`BootstrapService._publish_progress`, which satisfies the same rule at the same
reading — it publishes immediately after the per-batch `self._commit()`, and
`JobWorker` commits a `bootstrap` job's claim before the handler runs, so no
transaction of the worker's spans the work either. It is deliberately **not**
wrapped in `DeferredEventPublisher`: deferring one frame per committed batch
behind a 26-batch `--phase imdb` load is the 0%-to-100% jump the frame exists to
prevent, there is no residual window left for the buffer to close, and
`discard()` on a failed job would drop frames naming batches that really did
commit. ADR-0033 carries the amendment; `composition.build_worker` carries the
argument at the registration site, pinned from both sides because swapping the
two publishers there is invisible to every unit case of `JobWorker`.*

**`xmin` is not evidence of an uncommitted read, and a whole milestone's worth
of agents read it as one.** `test_sse_end_to_end.py`'s
`assert await _job_xmin(...) is None` failing as `assert '745' is None` was
relayed three times as *"a row a transaction has not committed is visible"*.
Postgres never shows an uncommitted row version to another connection; `xmin`
names the transaction that wrote the version the reader **can** see. Measured
at the failure: `xmin='745', status='running', attempts=0`, against the
reader's own `pg_current_snapshot() = '749:749:'` — an **empty** in-progress
list — so 745 is settled and committed. It is the *claim's* `UPDATE`
(`run_once`'s commit at `jobs.py:118–124`) still being current because the
`DELETE` has not committed. **Before reading an `xmin` as a visibility
anomaly, read `pg_current_snapshot()` beside it: if the writer is below the
snapshot's xmin and absent from its in-progress list, nothing anomalous
happened and what you are looking at is an ordering window.**

**The same case's flakiness is that window, and the control three separate
reports got wrong was the variable, not the count.** Unplanted on one tree at
load average 7–9: **6 failures in 13 runs**, every one on `_job_xmin` and every
one reporting the identical row state. With `await asyncio.sleep(0.25)`
planted in `JobWorker._run` between the handler returning and
`complete(job.id)`: **5 of 5**, still on `_job_xmin`, with `probe.seen` and the
refetch both passing — which is what separates *"the assertion races the
completing commit"* from *"the client was told too early"*. Three
implementers reported 5/5, 3/9 and "green, 927 integration passing" on the same
base; all three are consistent with a load-sensitive race and **none of them
distinguishes a defect from a scheduling window, because a rate is not a
mechanism.** A planted delay is, and it costs one line.

**A probe that never ran records nothing, and every absence claim over it
passes.** The G1 harness for `push._apply_items` first recorded `[]` — the
fixture had seeded no title the match ladder could find, and `_apply_items`
publishes only for an outcome carrying a `title_id`. Read as a result it says
*"the availability event publishes nothing"*. `test_sse_end_to_end.py` now
asserts `probe.seen` non-empty before any claim is read out of it.

**A replay ring and a per-subscriber queue are fed by the same `publish`
calls, so a lazily-resolved replay duplicates.** `InMemoryEventBus.subscribe`
snapshots the ring *before* it adds the subscriber, with no `await` in
between. Resolved lazily at the first `__anext__` instead — which is what the
M5 plan's draft did — everything published in the window between is in both
halves and the client sees it twice. The window is real: `api/routers/
events.py` reaches its first `anext` through an `asyncio.wait_for`, which
yields to the loop, and the push lane publishes from another task.
**`SQLAlchemyInstrumentor` was wired and produced no spans at all, for
three milestones.** `instrument()` patches the *module attribute*
`sqlalchemy.ext.asyncio.create_async_engine` with `wrapt`; `usher.db.base`
did `from sqlalchemy.ext.asyncio import create_async_engine` at module
scope, which is evaluated long before `configure_tracing` ever runs and
binds the **original, unwrapped** function into that namespace forever.
Verified directly: after `instrument()`, `usher.db.base.create_async_engine`
and `sqlalchemy.ext.asyncio.create_async_engine` are different objects. The
failure is silent in the worst way — the package is installed, the wiring
reports success, `connect` spans still appear (`_wrap_connect` patches
`Engine.connect` on the *class*, so it fires however the engine was built),
and not one `SELECT`/`INSERT`/`UPDATE` span is ever produced. `build_engine`
now calls `sa_asyncio.create_async_engine` through the module. A test that
accepts a `connect` span is not enough; assert on a *statement* span.
**Pipeline spans nest under the request's server span, asserted as
parentage.** `tests/integration/test_pipeline_spans.py` walks the parent
chain `match.title → ingest.item → sync.reconcile → GET …` on a real
`create_app()` through a real request, with SQLAlchemy statement spans
under the pipeline span that issued them. A pipeline that started its own
*root* spans passes every other assertion in this repository — valid ids,
exporting traces, PRD 10's span names all present — and fails only this.
A worker's `job.*` span is the deliberate exception: a root with a `Link`.
**`set_meter_provider` is set-once and `_ProxyMeter` caches, exactly like
the tracer.** Every `usher` module calls `metrics.get_meter(...)` at import
time, so each holds a `_ProxyMeter` whose instruments are `_Proxy*` shells
that cache the first real instrument they are handed. Without
`tests/conftest.py::reset_otel_meter_provider`, three rounds of "install a
`MeterProvider` with an `InMemoryMetricReader`, record through
`usher.services.jobs._job_duration`, read the reader" print the metric once
and then raise `AttributeError: 'NoneType' object has no attribute
'resource_metrics'` — the second `set_meter_provider` is refused and the
second reader is never registered with any provider.

`SQLAlchemyInstrumentor` needs the same treatment and the shared reset
cannot give it: it resolves its tracer *once*, eagerly, into a `wrapt`
closure, so it is a real `Tracer` rather than a `ProxyTracer` and nothing
in `usher.*` holds it. `tests/integration/test_pipeline_spans.py`'s own
fixture calls `SQLAlchemyInstrumentor().uninstrument()` before installing
its provider; without that line its database-span case passes alone and
finds an empty exporter when it runs third in its own file.
**An observable OTel callback cannot query this database.** OTel invokes it
from the metric reader's *background thread* and every database call here is
a coroutine on asyncpg, so a callback that queried would have to bounce a
coroutine onto the event loop (`run_coroutine_threadsafe`) and block the
exporter thread on it — a deadlock whenever the loop is itself blocked.
`usher.telemetry.register_queue_gauges` therefore takes a **synchronous**
reader returning the caller's most recent *complete* re-read of the `jobs`
table (`usher work` refreshes it after every pass), which is stale but never
wrong — unlike the counter-incremented-on-enqueue the plan was guarding
against. The SDK also keeps only the **first** observable gauge registered
under a name and silently discards the rest (verified directly), so the
reader is a module global that is replaced rather than a closure captured at
instrument-creation time.
**`TitleReadService` holds no `SourceAdapter`, and that is asserted on its
imports rather than on its behaviour.** PRD 08's "a degraded subsystem
narrows functionality; it never fails a request local state can answer" is
only a property of the code if the failing call is *absent* rather than
caught — "it did not raise" is also what a service that swallowed everything
would produce. Two things the obvious check misses, both measured: a
signature check spelled `parameter.annotation in (SourceAdapter, ...)` (or
via `annotation.__name__`) does not see a **string** annotation, which is the
one form needing no import at all; and an `ast.ImportFrom`-only scan does not
see `import usher.ports.source`. Read the annotation as text and walk both
node types. This is what makes M5's deferral of PRD 07's RFC 9457 envelope a
structural claim: with no adapter reachable there is no 503 to give a `code`
to, and the first route whose honest answer is "the source is down and I
cannot serve this from local state" is M9's `POST /titles/{id}/play`.
**A `GET /titles/{id}` leak check may not forbid the word "emby".** The
availability badge carries the name an *operator* typed, and "Living Room
Emby" is a correct value for it — PRD 07's own example spells it that way. A
rule that forbids the substring forbids the feature. What must not escape is
the source's own **item id**, so the assertion is against a distinctive
`external_id` and against the key `external_id`, not against a vendor name.
**The server process runs the lanes, and that is proved by a job
disappearing rather than by an assertion about wiring.** `create_app`'s
lifespan builds a `LaneSupervisor` and starts a push lane per enabled source
plus one job worker (both settings-gated, PRD 01's `--worker` flag as
configuration). A unit test of the supervisor proves it does what it is
told; it says nothing about whether the lifespan tells it anything.
`tests/integration/test_lanes_in_the_server_process.py` commits a real
`match` job, starts nothing but `LifespanManager(create_app(settings))`, and
asserts the row is gone before the app stops — with the mirror case
(`worker_enabled=False`, the row survives) as the control that makes it
evidence. The mutation `await lanes.start()` → `pass` fails exactly that one
case out of 2,072.
**Both lane switches default on, so every test that builds an app has to say
it does not want them.** Nine fixtures now pass
`push_enabled=False, worker_enabled=False`. Without it a worker lane polls
the real `jobs` table under `tests/integration/test_pipeline_spans.py`, which
enqueues jobs through its own probe route and asserts on them; and a push
lane in `tests/integration/test_admin_sources.py` builds the **real**
`EmbyAdapter` against `https://emby.invalid` and opens a socket, because
`dependency_overrides` do not reach the lifespan. Stated per fixture rather
than defaulted in `conftest.py`, so it is greppable.
**`start()` creates tasks and awaits nothing, and the case with teeth drives
the coroutine by hand.** `coro.send(None)` must raise `StopIteration`; a
`start()` that read the source list inline hands back a future instead. That
is what keeps `/health` answering 200 with Postgres down while
`/health/ready` reports 503 — the M5 plan's own draft did
`await self.refresh()` there, which opens a connection, and its own Step 4
then asserted the opposite. The first refresh happens *inside* the refresher
task, which refreshes and then sleeps, so nothing waits `USHER_PUSH_SOURCE_REFRESH_SECONDS`
for its first lane either.
**Per-lane crash isolation comes from one task per lane, not from the
`except`.** Measured: deleting `_guard`'s `except` survives the whole suite,
while removing `return_exceptions=True` from `stop()`'s gather fails **11**
cases on its own — so the two are not the belt-and-braces pair a comment
claimed. What `_guard` buys is that a crashed lane is not silent (without it
CPython reports an unretrieved task exception at GC time, to stderr, with no
source name in it), which needs a log assertion to see. And
`running_sources() == ["B"]` is not a test of isolation: a supervisor whose
second lane was created and never scheduled reports the same thing. The case
asserts B ingests an item pushed *after* A's task is already `done()`.
Two lanes genuinely overlapping is its own measurement — **99.3–99.4% of
their union over five runs**, against a serialised supervisor's 0.0.
**A guard can be right and unobservable, and `_write_push_available`'s is.**
Deleting its "nothing changed" check does not move `sources.updated_at`,
because `PostgresSourceRepository.update` sets attributes on a *loaded ORM
row* and SQLAlchemy's unit of work emits no `UPDATE` when none actually
changed — so the `set_updated_at` trigger never fires either way. Recorded
as an equivalent mutant against today's repository and kept, because the day
that repository issues a bare `UPDATE … SET` a flapping lane moves a column
an operator reads, once per reconnect. Same treatment M4 gave `_ENQUEUE`'s
`GREATEST`.
**`JobWorker.startup()` requeues everything left `running`, so there is one
worker per deployment, not per process.** `requeue_running`'s default
`older_than_seconds=0.0` is correct at exactly one worker and at two steals
the other's live claims. The server now runs one, so a deployment that also
runs `usher work` must set `USHER_WORKER_ENABLED=false` on the server.
`LaneSupervisor` calls `startup()` once rather than per pass, which was
untestable until `idle_seconds` became a constructor argument nothing in
`src/` passes: the case asserts one requeue over three passes.
**Readiness reports the lanes and never gates on them, and the case that
proves it cannot live in the unit file.** `tests/unit/test_api_health.py`'s
app points at an unreachable database, so readiness is *already* 503 there
and both mutations — `all(checks) and lanes.running_sources()`, and moving
`push` inside `ReadinessChecks` where `all(...)` picks it up automatically —
survive every case in it. Against a **reachable** database with no lanes
running, both turn a 200 into a 503 and both die, so that case lives in
`tests/integration/test_health.py`. `LaneReport` is a separate model from
`ReadinessChecks` for exactly this reason: every field of the latter is part
of the status code by construction.
**`SourceStatus` refuses "push available without being authenticated", and
`dataclasses.replace` re-runs `__post_init__`.** So the obvious one-liner
for reporting a running lane's push health —
`replace(status, push_available=self._push_health(source_id))` — raises
`ValueError` out of `GET /admin/sources/{id}/status` for a state a rotated
password produces, on the screen an operator opens to diagnose it. The
lane's answer is taken only when the status is authenticated.
**`usher.composition` is the wiring both roots share, and it needs no
seventh import-linter contract.** `usher.cli` carries one saying nothing may
import it, so shared code cannot live there. The new module sits outside
every contract's source list — and that hole is closed by what it imports
rather than by a rule: it imports `usher.db` and `usher.adapters`, so a core
module reaching it breaks contracts two and three, which report indirect
chains by default (unlike contract six's `allow_indirect_imports = true`).
Verified by planting `from usher.composition import Pipeline` in
`usher/services/push.py`: **4 kept, 2 broken.**

**That argument covers the core layers and does not cover `usher.api`, which
M8 had to close with an eighth contract (2026-08-07).** Contracts two and
three are sourced at `usher.domain`, `usher.ports` and `usher.services` only,
so the indirect chain that catches a core module reaching `usher.composition`
does not exist for a router — and `usher.api` is a composition root, so it is
*allowed* to reach `usher.db` and `usher.adapters` directly. A router doing
`from usher.composition import build_curation_service` (the one public factory
in `src/` that returns a `CurationService` holding an `LLMClient`) therefore
passed all seven contracts, ruff, mypy and both suites — planted and measured.
The eighth contract forbids `usher.api.routers` from naming
`usher.composition`, `usher.services.curation` or `usher.ports.llm`.
**It requires `allow_indirect_imports = true` and does not hold without it**:
every router imports `usher.api.deps`, which is the API's composition root and
imports `usher.composition` on purpose, so unflagged the contract is BROKEN at
HEAD on three chains through `api/deps.py` and `api/lanes.py`. With the flag
the line drawn is the intended one — a router may *reach* the wiring through a
dependency and may not *name* it. Verified BROKEN on each of the three
forbidden modules planted directly, in `routers/rows.py` and `routers/home.py`.

Verified working as of Group F (telemetry bootstrap, FastAPI app with health
endpoints, then hardened in a follow-up review pass) — the app is now a
runnable service:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head
uv run uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000
curl http://localhost:8000/health          # liveness  -- {"status":"ok"}, HTTP 200 always
curl http://localhost:8000/health/ready    # readiness -- {"status":"ready","checks":{"database":true,"migrations":true}}, HTTP 200 or 503
```

`/health` and `/health/ready` are deliberately different: liveness must never
depend on Postgres (a database outage is not a reason to kill and restart
the process — restarting doesn't fix Postgres), so only readiness executes
`SELECT 1` (and, only if that succeeds, compares the live `alembic_version`
table against `usher.db.migrations.status.code_head_revision()` — PRD 08:
"the app refuses to serve on a schema mismatch rather than guessing").
Readiness returns HTTP 503 (not 200) when any check fails: no PRD text pins
a status code, but a readiness probe's real consumers — Kubernetes, Docker
`healthcheck`, load balancers — gate on the code and never parse the body.
Verified directly against a real container: stopping Postgres mid-session
leaves `/health` returning `{"status":"ok"}`/200 unchanged while
`/health/ready` switches to `{"status":"degraded","checks":{"database":
false,"migrations":false}}`/503 — same running process, no restart.
Readiness self-heals once Postgres comes back, still without restarting
Usher. Corrupting `alembic_version` on an otherwise-healthy database
produces the same degraded/503 shape with `database: true, migrations:
false` — a live demonstration is in the "readiness reports migration
state" commit.

Every request gets a real server span (`FastAPIInstrumentor`, wired in
`create_app`) with SQLAlchemy queries and outbound httpx calls nested under
it (`SQLAlchemyInstrumentor`/`HTTPXClientInstrumentor`, wired in
`configure_tracing`) — without this, nothing ever called
`tracer.start_as_current_span()` during request handling, so
`inject_trace_context` never fired in the running service, only in tests
that built their own span. `configure_tracing`/`configure_metrics` install a
real `TracerProvider`/`MeterProvider` *unconditionally* (a bare provider
with zero processors still assigns valid ids/records instruments, verified
directly) — only the actual OTLP *export* is conditional on
`settings.telemetry_enabled`. Both are `isinstance`-guarded against being
reconfigured on a second `create_app()` call in the same process (verified
directly: without the guard, 5 calls with telemetry enabled leaked 5
background export threads; with it, flat at the 2 the first call installs).
With no `OTEL_EXPORTER_OTLP_ENDPOINT` set, the default (unset) config still
carries zero *export*-related risk — nothing gRPC-related is ever
constructed. If an endpoint *is* set but nothing is listening there, the
OTel SDK's own retry loop logs a warning rather than raising or hanging the
app — graceful, but not literally silent in that specific case.

Stdlib `logging` (uvicorn's access/error logs, SQLAlchemy warnings, the OTel
exporter's own retry messages) is bridged into loguru via `_InterceptHandler`
(loguru's own documented recipe) — without it, confirmed on a live run, only
`usher`'s own logger calls were structured JSON; everything else printed as
plain text, ignored `log_level`/`log_json`, and never got
`trace_id`/`span_id` patched in.

`get_session` (`api/deps.py`) is the request's commit/rollback boundary:
commits once the handler completes without raising, rolls back and
re-raises otherwise. Previously nothing in `src/` ever called `commit()` —
`ports/repository.py`'s "the caller owns the session and the transaction"
had no concrete caller yet, so a future write endpoint that forgot to
commit would have lost data silently.

`/health` and `/health/ready` responses are typed (`api/dto/health.py`,
`LivenessResponse`/`ReadinessResponse`/`ReadinessChecks`), so
`/openapi.json` describes real shapes instead of `{"type": "object"}`.

`tests/integration/test_health.py`'s async `client` fixture needs
`asgi_lifespan.LifespanManager` (new dev dependency) wrapping the app:
`httpx.ASGITransport` only implements the ASGI "http" protocol, not
"lifespan" (confirmed against its source and FastAPI's own docs), so a bare
`AsyncClient(transport=ASGITransport(app=app))` never runs `create_app`'s
lifespan and `app.state.session_factory` is never set. Reproduced directly:
without the fix, `/health/ready` raises `AttributeError` while the other two
tests in the file still pass. `deps.py`'s `get_session_factory` now raises a
diagnosable `RuntimeError` for this exact case instead of Starlette's
generic `AttributeError`.

**It was reachable only from `usher.cli`, so a server-only deployment had an
empty `users` table** — `docker compose up` against a healthy Postgres left
`watch_states.user_id` with nothing to reference, and the row appeared only
once `work --once` ran. Not a live bug in M4 (no route writes a watch state;
the three admin routes are M9's) and a live bug the moment M5 adds one.
Fixed as `usher.api.deps.get_default_user_id`/`DefaultUserIdDep`, a
**request-scoped dependency and deliberately not a lifespan call**:
`create_app`'s lifespan builds an engine and opens no connection, which is
what makes `/health` answer 200 with Postgres down while `/health/ready`
reports 503 — verified live against a real container. A write at startup
turns a database outage into a crash loop and an unmigrated schema into a
failure to boot, trading a documented, tested degradation for a worse one,
for a row only a request ever needs. It also would have broken
`tests/unit/test_api_health.py` and `test_telemetry.py`, which build a real
app against no Postgres at all. Nothing routes over it yet, for the same
reason nothing routes over the pipeline services beside it;
`tests/integration/test_pipeline_deps.py` drives it through a real request
and asserts the row is *committed*, read back on a second session.

`api/deps.py` carries all eight new repositories plus `MatchService`/
`IngestService`/`ReconcileService`/`WatchStateSyncService`, so M9 adds
routers over finished wiring. **`EnrichService` is deliberately absent**:
its provider owns the token bucket that keeps this deployment under TMDb's
~40 rps ceiling, and a request-scoped `TmdbClient` gives every concurrent
request a *fresh* bucket — N in-flight requests get N × 30 rps, a rate
limiter that limits nothing. It belongs on `app.state` at lifespan, and
nothing in PRD 07's surface calls enrichment directly (M5's demand
promotion enqueues a job; `usher work` runs it).

**`status.HTTP_422_UNPROCESSABLE_ENTITY` is deprecated behind a Starlette 1.3
module `__getattr__`, so it warns once per *request*, not once per import.**
Use `HTTP_422_UNPROCESSABLE_CONTENT`; both are 422. This suite deliberately
runs with no expected warnings, for the reason the `testcontainers` shim was
replaced (see `fixtures-and-fakes.md`): a suite with one permanent warning is a
suite where the next real
one is invisible.

**`configure_logging` reclaims logging from libraries that took it, and it was
not reclaiming `.disabled` — so one `fileConfig` call muted a logger for the
rest of the process.** Found 2026-08-10 from CI, and the shape of the failure
is the finding: `uv run pytest tests/unit` was green, `uv run pytest` was not.
`logging.config.fileConfig`/`dictConfig` default `disable_existing_loggers` to
**True** and set `.disabled` on every logger their own config does not name;
`db/migrations/env.py` calls `fileConfig` against an `alembic.ini` naming only
root, sqlalchemy and alembic, and the integration suite migrates in-process. So
by the time the unit suite ran, `httpx` was disabled, and
`test_httpxs_per_request_info_line_does_not_reach_the_sink` failed on its
second arm — the WARNING that must still *arrive*.

The repair is one line in the reclaim loop beside the existing
`handlers = []` / `propagate = True`, and the reason it belongs there rather
than in `env.py` alone is where `logging` checks the flag: **`Logger.handle`
tests `.disabled` below both the level check and the handler walk**, so nothing
this function can do to sinks, levels or handlers reaches a disabled logger.
The loop already existed to take logging back from a library that grabbed it
(uvicorn's own handlers); a disabled logger defeats that as completely as a
stray handler does. `env.py` now also passes `disable_existing_loggers=False`
(`db-and-sql.md`) — that stops the damage, this repairs it whoever caused it,
and neither subsumes the other, because any dependency may call `dictConfig`.

Two smaller things measured on the way. The reclaim loop iterated
`logging.root.manager.loggerDict` directly while calling `logging.getLogger`
inside it, and `getLogger` on a `PlaceHolder` entry can insert parent
placeholders — a mutation during iteration; it now snapshots the keys, which
costs nothing. And the defect was invisible to every assertion in the suite but
one: **an intercepted-record path is asserted almost entirely by what must not
arrive**, so the single case requiring a stdlib record to arrive is what caught
a total mute. Both arms, or a "nothing reached the sink" fix passes.

**`app.routes` on FastAPI 0.140 does not contain the app's routes, and a walk
over it reads as a passing sweep.** Measured 2026-08-11 building A2's "every
route answers a problem document" case. `include_router` appends **one opaque
`fastapi.routing._IncludedRouter`** per router rather than flattening its
routes into the app, so `[r for r in create_app().routes if isinstance(r,
APIRoute)]` finds **zero** of Usher's fourteen routes — and four of FastAPI's
own (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`), which are
plain Starlette `Route`s and are not even `APIRoute`s, so the same walk
filtered on `APIRoute` returns an empty list that a `for` loop iterates
happily. Descend through `route.original_router.routes` recursively, and carry
a premise guard that the descent found a known path: this is the "a run that
did not run is not a pass" family arriving in a route walk, and it is the shape
every "every route declares X" scan in this milestone is built on. Note also
that `/admin/sources` is **two** `APIRoute` objects (one per method), so any
per-path assertion has to group by `route.path` first — and Starlette's 405
carries the `Allow` header of the *first* partial match only, so
`PUT /admin/sources` answers `Allow: POST` rather than `GET, POST`.

**RFC 9457's `instance` is the request path, so a 422 for a malformed *path
parameter* does echo the value it rejected, and there is no spelling that
avoids it.** Found 2026-08-11 landing the envelope: `GET /titles/not-a-uuid`
now answers `"instance": "/titles/not-a-uuid"`, which failed M5's
`test_a_malformed_id_is_a_422_that_does_not_echo_it` on its blanket
`"not-a-uuid" not in response.text`. The credential rule is untouched and the
distinction is worth stating precisely, because the next reader will reach for
the blanket assertion again: PRD 08 is about what a client **submitted as
data** — a body or a query string — and both are still absent, `instance` being
`request.url.path` and **never** `request.url`. No credential is ever in a path
in this API; `?q=` is a query and is dropped. The narrowed case asserts over
pydantic's `input` (the field that carried whole request bodies) rather than
over the whole response text.

**The structural claim four milestones deferred the envelope on is now
discharged, and the second red is the measurement worth keeping.** The entry
above ends *"the first route whose honest answer is 'the source is down and I
cannot serve this from local state' is M9's `POST /titles/{id}/play`"*. It is,
and it landed 2026-08-11. Two reds on the way, both recorded because the second
is the one that says something about `api/errors.py`: before the route existed
the case failed `assert 404 == 503`; against a route raising a **bare**
`HTTPException(503)` it failed `KeyError: 'code'`. `_CODE_FOR_STATUS` holds 404,
405 and 422 only, and `http_error_as_a_problem_document` hands an unmapped
status to FastAPI's own handler rather than inventing a name — so a 503 with no
`ProblemCode` member answers `{"detail": …}` at `application/json`, which is
indistinguishable from the pre-envelope shape. **A route with a status nobody
has minted a code for silently opts out of the envelope**, and the only thing
that notices is a case asserting `body["code"]`. Adopting the envelope is
`raise ProblemException(status_code=…, code=…, detail=…)`; adopting the
*status* is not enough.

**`request.url_for` substitutes a path parameter raw, so the caller owns the
percent-encoding.** Read from the source and confirmed 2026-08-11:
`starlette.routing.Route.url_path_for` → `replace_params` →
`StringConvertor.to_string`, whose whole body is `str(value)` plus two asserts
(`"/" not in value`, non-empty). Nothing encodes. So
`api/deps.py`'s `quote(ticket, safe="=")` is not belt-and-braces over a library
that would have done it — it is the only encoding step on that path. Related
and measured the same day: **`RedirectResponse` re-quotes the `Location` it is
given**, with `safe=":/%#?=@[]!$&'()*+,;"`, which leaves a realistic Emby
direct URL byte-identical (`?api_key=…&DeviceId=…` measured unchanged) and
escapes only characters illegal in a URI anyway (`a b.mkv?q=1|2` →
`a%20b.mkv?q=1%7C2`). `%` is in the safe set, so there is no double-encoding
hazard for a URL that already carries an escape.

**`api/dto/` names every model `…Response`, nested ones included, and that is
load-bearing.** `tests/unit/test_api_dto.py` discovers response models by
`name.endswith("Response")` and asserts none declares a credential-shaped field
or a `SecretStr`. `WatchStateResponse`, `AvailabilityResponse` and
`RowCardResponse` are all nested and all follow it; the M9 plan spelled the
playback ones `PlayTarget`/`PlaySource`, under which they would have been the
only models in the package the scan could not see — and `PlayTargetResponse` is
the one model in the API that renders a value derived from a credential-bearing
URL. Shipped as `PlayTargetResponse`/`PlaySourceResponse`, for the reason
`ProblemResponse`'s own docstring gives for its name.

## M9's live run (H4) — three route findings, measured 2026-08-12

The Emby half of that run is in `.claude/rules/emby-push-and-ingest.md`; these
three are about the *route* and belong here.

🔴 **Starting the shipped app against a real source is itself an unbounded
walk, and nothing warns you.** `LaneSupervisor` starts a push lane per **enabled**
source, and the lane's reconnect gap-closer calls
`reconcile(source, SyncRunKind.DELTA, adapter)`. Against a real household —
1,126,789 items on the one this project measures — that is exactly the walk
`emby-push-and-ingest.md` forbids, issued by a bare
`uvicorn usher.api.app:create_app --factory` with default settings and no
command of its own. H4/H5's run set `USHER_PUSH_ENABLED=false` and
`USHER_WORKER_ENABLED=false` for that reason (and the second is required anyway,
because H5's worker pass has to be a real `usher work --once`). **Any live HTTP
run against a real source must set both, or budget for a delta walk it did not
ask for.** The two settings are also what make such a run's request budget
*statable*: with the lanes on, the count is whatever a websocket and a gap
closer decide.

**`quote(ticket, safe="=")` is a no-op at the length the shipped path actually
produces, confirmed live.** D1 measured the encoding question over synthetic
plaintext lengths; H4 measured the artefact: a ticket minted for a real Emby
direct URL is **292 characters**, url-safe base64 plus `=` padding, and the
segment that comes back out of Starlette is byte-identical to the one minted.
No `%` appears in the path at any point.

**The `deep_link` wrapper does not double-encode, and this is the specific
defect H4 was dispatched to look for.** `PlaybackService._with_tickets` rebuilds
a deep link as `wrap_deep_link(<the ticket URL>)`, so the whole Usher URL —
itself already percent-encoded once by `quote(ticket, safe="=")` — is
percent-encoded again by `quote(inner_url, safe="")`. Measured against the real
route: the `url=` parameter decoded **exactly once** is byte-identical to the
`direct` target's ticket URL, and a `GET` of that decoded string answers the
same `302` to the same `Location`. One encode, one decode; the two are inverses
at this length and alphabet.

**Ticket expiry, driven against the wall clock rather than a frozen one.**
`TICKET_TTL_SECONDS: Final = 300` is a module constant and deliberately not a
setting, so a live run cannot lower it — the honest alternative is to wait. One
ticket minted by the running server was honoured at **127 s** (`302`) and
refused at **312 s** (`404 ticket_invalid`), and a four-character tamper of a
live ticket answered `404 ticket_invalid` as well, which is D1's one-answer-for-
expired-and-forged decision observed rather than asserted. Both the mint and the
redeem happened in the **same** process: a ticket minted under one
`USHER_SECRET_KEY` and redeemed against a server started with another is
undecryptable and looks exactly like a ticket bug.

## M9 Task W1 — the worker lane is a bounded pool, and three things in it were not the `gather` (2026-08-12)

**`build_worker` takes a `UnitOfWork`, not a `Pipeline`, and the worker lane
builds it once per *process* rather than once per pass.** `_run_worker` used to
open a session, rebuild the whole worker inside it and run one pass; the worker
now holds a scope factory and opens a session per claim and per job, so the
only per-pass work left is the gauge refresh, which needs a pipeline of its own
and gets one. The build is still **lazy** — inside the loop, on the first
iteration — because `await self._user_id()` is a database call, and `start()`'s
promise that a lane connects to nothing is what makes `/health` answer 200 with
Postgres down.

**Three concurrency hazards inside the lane's own wiring, none of them the
claim loop.** Each would have been introduced by adding a `gather` and leaving
everything else alone:

- **`SourceRegistry` held the pipeline.** `resolve` issues two reads of its own
  (`sources.list_all`, `media_items.get_by_external_id`), so a registry
  `rebind`-ed once a pass was a second door onto one `AsyncSession` — not the
  handler's repositories, which the per-job scope separates, but the
  *resolver's*. It now holds only the adapter cache and takes the scope's
  pipeline as an argument (`bound(pipeline)`), which makes the split a
  signature rather than a convention.
- **Adapter construction had no lock.** It is the one `await` in `resolve` that
  mutates the cache, so two jobs for one source both miss, both authenticate,
  and one adapter is overwritten in the dict and never closed — a leaked socket
  per race, visible only under load. Double-checked locking, with the re-read
  inside the lock so the loser takes the winner's adapter.
- **The event buffer was the worker's.** See
  [ADR-0033](../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)'s
  amendment: `discard()` on a failing job emptied a *concurrent* job's frames.

**`asyncio.wait`, never a `TaskGroup` and never `gather`.** This is the same
argument `_guard` and the one-task-per-lane rule already make, one layer down: a
task group cancels its siblings on the first escape, which turns one poisoned
job into N claims abandoned mid-write, and `gather(return_exceptions=False)`
returns while the siblings are still running and unawaited. The first escaping
exception is re-raised after every task has settled, so the lane's own
`except Exception` still sees it and nothing is left in flight. On
`CancelledError` — which is how `stop()` works — the in-flight tasks are
cancelled and awaited, so each job's `finally` fails or completes it *now*
rather than leaving a claim for the lease to clean up minutes later.

**Recovery moved from "once, at startup" to "on a timer, on a lease", and the
lane's own case had to grow a second assertion to see the difference.**
`test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass` asserted
`requeues == 1` over three passes — which a lane calling `requeue_running()`
**bare** satisfies exactly as well as a correct one, because the count is the
same and only the *age* differs. The fake now records the argument, and the
case asserts it is the lease: at `older_than_seconds=0.0` a recovery pass takes
the worker's own live claims, which is a defect the count cannot express.
Throttled to half a lease because it is an `UPDATE` scanning
`status = 'running'` and there is nothing to find between leases.

**One lane docstring is now false and is corrected in place:** *"one worker per
deployment, not per process"*. Two workers no longer corrupt each other — the
lease is what changed — so `USHER_WORKER_ENABLED=false` on a server beside a
`usher work` container is a **capacity** decision rather than a correctness
one. What two processes still do is spend the same upstream budget twice:
`USHER_JOB_CONCURRENCY` and `USHER_TMDB_REQUESTS_PER_SECOND` are both per
process, against a rate limit that is per client.

## Phase 0's live run — the first telemetry this project has ever exported (2026-08-14, M10 O2)

**"Telemetry has exported to no-op exporters for nine milestones" is false, and
the true claim is stronger: no exporter object is constructed at all.**
`telemetry.py:191-197` installs a real SDK `TracerProvider` **unconditionally**
and adds `BatchSpanProcessor(OTLPSpanExporter(...))` only inside
`if settings.telemetry_enabled`; `:222-232` mirrors it with
`metric_readers=readers`, where `readers` is the empty list when telemetry is
disabled. The suite has pinned that from both sides since M1, by monkeypatching
each exporter class to raise if constructed. Three consequences, and the first
two are counter-intuitive enough that the "no-ops" phrasing hid them:

- **Spans have always had real, valid ids.** The provider is a real
  `TracerProvider`, not the API's no-op default, so `inject_trace_context`
  (`telemetry.py:63-67`) has been firing all along and **every JSON log line in
  every deployment already carries `trace_id` and `span_id`**. Observed on this
  run *before* any endpoint mattered: the `GET /health` line carried both.
- **Metric points were recorded into a provider with no reader**, so they were
  aggregated in memory and never collected.
- **Nothing has ever left the process** — which is what to say, rather than that
  the exporters were no-ops.

**This file already had it right**, in the Group F paragraph above
(*"install a real `TracerProvider`/`MeterProvider` unconditionally … nothing
gRPC-related is ever constructed"*), which is why the correction below is to
the six documents that disagreed with it rather than to anything here. All six
are listed at the end of this entry, along with why it took three searches of
increasing shape — not increasing wording — to find them all.

Corrected in `docs/prd/10-telemetry-and-dashboards.md` in the same commit. **The
edit was sized to keep that paragraph's line count fixed**, because
`~/code/observability/README.md` cites `:936-937` for the *next* sentence; the
file stayed at 951 lines and the citation was re-checked after the edit rather
than assumed.

**The endpoint's scheme is load-bearing and the wrong spelling fails silently.**
Measured in the installed `opentelemetry-exporter-otlp-proto-grpc` **1.44.0**,
`exporter.py:316-323`: with no `insecure=` argument and
`OTEL_EXPORTER_OTLP_INSECURE` unset — exactly how `telemetry.py:195` and `:224`
call it, passing `endpoint=` and nothing else — `insecure` defaults to
`parsed_url.scheme == "http"`. So the spelling a person types,
`OTEL_EXPORTER_OTLP_ENDPOINT=127.0.0.1:4317`, builds a **TLS** channel against a
plaintext collector and every export fails inside the SDK's own retry loop,
which logs a warning and **does not raise**; `http://127.0.0.1:4317` builds an
insecure one. `:325-326` then discards the scheme and keeps the netloc, so
**both spellings store the identical endpoint** and the flag is the only
observable difference — verified directly (`_insecure` `True` vs `False`,
`_endpoint` `127.0.0.1:4317` in both). Pinned by
`test_an_endpoint_without_a_scheme_builds_a_secure_channel_against_a_plaintext_collector`,
which asserts the flag *differs between the two spellings* rather than that one
is `False`, so a future normalisation that prepends a scheme is caught.

**That case and its sibling point at `127.0.0.1:1`, not at the collector**, and
the reason belongs here because this file is where the real endpoint is
recorded. They assert on what the exporter *constructs*, never on what answers,
so aiming them at the live collector this host runs would let an
environment-independent suite (`tests/unit`: "no Docker, no network") pass partly
because of the environment. Nothing would be sent either way — gRPC channels
connect lazily and those cases record nothing, so `shutdown()` flushes an empty
queue — but that is a property of what they happen not to do, and one recorded
span would end it. A dead loopback port makes the isolation structural.

**"The app started" is not evidence the export works**, for the same reason: the
retry loop is quiet. Only the three observations below are evidence.

### The three signals, from one `GET /search?q=Blade+Runner&limit=5` over the live 1,272,401-title catalog

Queried through Grafana's datasource proxy, because Prometheus, Loki and Tempo
are not published to the host.

1. **The trace.** Tempo, TraceQL `{name="GET /search"}` → one trace,
   `2fa839a2eb19612d9aeb7c3fca9b441e`, root `GET /search` from scope
   `opentelemetry.instrumentation.fastapi`, **73.95 ms, 13 spans**. Beneath the
   root: **six `SELECT` spans plus `SAVEPOINT`, `INSERT` and `RELEASE`** from
   `opentelemetry.instrumentation.sqlalchemy`, and one `connect`. This is the
   live confirmation of the instrumentor finding recorded earlier in this file —
   the fix that made `build_engine` call `sa_asyncio.create_async_engine`
   *through the module* is still holding, and a `connect` span is not what was
   accepted here.
2. **The correlated log line**, found in the direction that proves correlation:
   the 32-hex id was read **off the Tempo trace** and then grepped for in Loki,
   not the reverse. Loki,
   `{service_name="usher"} |= "2fa839a2eb19612d9aeb7c3fca9b441e"` → **exactly one
   line**, `... - "GET /search?q=Blade%20Runner&limit=5 HTTP/1.1" 200`, carrying
   `trace_id=2fa839a2eb19612d9aeb7c3fca9b441e` and
   `span_id=35dad4c7b4bc6de1`. Finding *a* line with *a* `trace_id` would have
   proved only that the patcher runs, which — per the first section above — it
   always has.
3. **The two metric samples**, one Usher's own instrument and one the
   framework's, because they travel different code paths:
   - `usher_search_duration_seconds_count{job="usher",mode="full_text"}` → **1**;
     `_sum` → **0.05405643954873085** (seconds).
   - `http_server_duration_milliseconds_count{job="usher",http_target="/search",http_status_code="200"}`
     → **1**; `_sum` → **118** (ms, and the client measured 118.8 ms).

### The stored series name is not the OTLP name, and the transformation includes the *unit*

The half most likely to be guessed wrong, so it is written out — and **the first
version of this entry generalised a rule from a sample that could not exhibit
its own counter-examples**, which is the mistake to read before the table.
Measured against the collector's `prometheusremotewrite` exporter, whose
`add_metric_suffixes` is left at its default `true`
(`~/code/observability/otel-collector/config.yaml`), 2026-08-14.

**What one `GET /search` could and could not show.** It exported sixteen series,
and every one of them was a histogram, a gauge or `target_info`: **not a single
counter fired**. So that run measured unit handling and was structurally
incapable of measuring `_total` — and both of its `unit="1"` examples happened
to be histograms, so it was equally incapable of showing what `1` does on a
gauge. Two rules were written from it that the sample could not support.

**Observed in the live run:**

| instrument | unit | kind | stored series |
|---|---|---|---|
| `usher.search.duration` | `s` | histogram | `usher_search_duration_seconds_{bucket,count,sum}` |
| `usher.search.results` | `1` | histogram | `usher_search_results_{bucket,count,sum}` |
| `http.server.duration` | `ms` | histogram | `http_server_duration_milliseconds_{bucket,count,sum}` |
| `http.server.response.size` | `By` | histogram | `http_server_response_size_bytes_{bucket,count,sum}` |
| `http.server.active_requests` | `{request}` | up-down counter | `http_server_active_requests` |
| `db.client.connections.usage` | `connections` | up-down counter | `db_client_connections_usage` |
| **`usher.sse.connections`** | **`1`** | **observable gauge** | **`usher_sse_connections_ratio`** |

**Measured by a probe**, because the shapes Usher declares most of and this run
never exercised are exactly the ones the table above cannot reach. One
instrument per rule, emitted 2026-08-14 under `service.name="namingprobe"`, each
mirroring a real Usher declaration:

| probe instrument | unit | kind | stored | mirrors |
|---|---|---|---|---|
| `probe.items` | `1` | counter | `probe_items_total` | `usher.ingest.items` and eight more |
| `probe.cachehits` | *(none)* | counter | `probe_cachehits_total` | `usher.cache.hits`/`.misses` |
| `probe.wait` | `s` | counter | `probe_wait_seconds_total` | unit **and** `_total` together |
| `probe.queued` | `1` | observable gauge | `probe_queued_ratio` | `usher.jobs.queued` and five more |
| `probe.connections.usage` | `connections` | up-down counter | `probe_connections_usage` | the `db.client` row above |
| `probe.things` | `{things}` | up-down counter | `probe_things` | the `active_requests` row above |
| `probe.payload` | `By` | histogram | `probe_payload_bytes_{…}` | `http.server.response.size` |

**The rule, one clause per measurement:**

1. **Dots become underscores.** Every row.
2. **The unit is expanded and appended** — `s`→`_seconds`, `ms`→`_milliseconds`,
   `By`→`_bytes`. This is the genuinely valuable finding and the part no
   prediction had.
3. **A brace annotation unit is dropped entirely** — `{request}`, `{things}`.
4. **A unit already contained in the name is not appended.**
   `db.client.connections.usage` with unit `connections` stores unsuffixed,
   while `By` is appended to a name containing no "bytes". So *"the unit is
   always appended"* is itself too strong.
5. **Unit `1` depends on the instrument kind.** Dropped for a histogram
   (`usher.search.results`) and for a counter (`probe.items`); it becomes
   **`_ratio` for a gauge** (`usher.sse.connections`, `probe.queued`).
6. **`_total` is for monotonic counters only.** Both up-down counters above get
   nothing.
7. `service.name` becomes `job`, `service.instance.id` becomes `instance`, and
   the collector adds `otel_scope_name`/`otel_scope_version`.

**What that means for Usher's own catalogue, and it is not what anyone would
guess.** Every one of Usher's roughly ten counters declares `unit="1"`, so by
rules 5 and 6 they store as `usher_ingest_items_total`,
`usher_provider_requests_total`, `usher_curation_rows_total` and so on — the
unit vanishes and `_total` arrives. **And every one of Usher's observable gauges
also declares `unit="1"`, so they all take `_ratio`**: `usher_jobs_queued_ratio`,
`usher_jobs_parked_ratio`, `usher_source_push_connected_ratio`,
`usher_search_embeddings_stale_ratio`, `usher_similarity_neighbors_stale_ratio`.
A queue-depth gauge stored under `_ratio` looks like a mistake and is not.
**These are derived by applying the measured rule to declarations read from
source, not observed under their real names** — only `usher.sse.connections` and
the two `usher.search.*` histograms have been seen end to end. Treat the derived
names as strong predictions with a known mechanism, and confirm the first one a
dashboard actually needs.

**The correction to the M10 plan's prediction, scoped to what was measured.**
The plan predicted *"dots become underscores, and a monotonic counter gains
`_total`"*. **Both halves hold** — `_total` is confirmed by the probe above and
independently by O1's smoke test, whose `observability.smoke.events` stores as
`observability_smoke_events_total`. What the prediction *omits* is unit handling
entirely, and that is where every wrong guess will come from: four of the seven
rules above are about the unit. An earlier version of this entry claimed the
prediction's `_total` half was refuted; it was not refuted, it was **untested**,
and saying so was the same error in the opposite direction.

**The negative controls, run rather than assumed:** querying the un-suffixed
names `usher_search_duration` and `http_server_duration` returns **no series at
all** — not an error, not a zero. A dashboard panel written against the name a
person would guess is permanently empty and nothing distinguishes it from a
healthy zero, which is the same failure `services/search.py` already warns about
for near-miss instrument names, one layer further out. **`http.server.duration`
is not Usher's name to choose** — it is whatever the instrumentor emits, so a
panel plotting it is coupled to that package's version.

### Three traps in running this against the live catalog, each measured

1. **`docker compose up` from an M10 worktree starts a second, empty Postgres.**
   `compose.yml`'s bind mount is `./data/postgres`, so a different worktree is a
   different compose project with its own empty volume, and the "live catalog"
   observation would be over **zero** titles. The 1.27M-row database belongs to
   the project rooted at the main checkout. Phase 0's run is therefore a **host
   process** pointed at the existing container over its compose network, which
   publishes no host port.
2. **`sources` holds one row and it is enabled**, so the shipped lanes would
   start a push lane and, on reconnect, a gap-closing `DELTA` reconcile against
   a real Emby — an unbounded walk. **`USHER_PUSH_ENABLED=false` and
   `USHER_WORKER_ENABLED=false` are mandatory for this run, not advisory**, and
   they are what makes the request budget against the live Emby statable at
   **zero**. Confirmed two ways: no lane log line was emitted, and the trace
   contains **no `httpx` client span** — `HTTPXClientInstrumentor` is
   instrumented *unconditionally* (`telemetry.py:199`), so any outbound call
   would have produced one. An absence claim with a positive control behind it.
3. **Port 8000 is already taken on this host** by an unrelated service. The run
   used **8100**, which is `USHER_COMPOSE_HOST_PORT`'s own default in
   `compose.yml`.

The endpoint itself goes in `.env`, which `.gitignore` already covers and
`compose.yml` hands to the container whole. **`.env.example` is not edited**, and
that is enforced rather than preferred: `tests/unit/test_deployment_config.py`
asserts its key set *equals* the `Settings` field set and that every value equals
that field's **default**, so the endpoint line has to stay blank there.

### The same false claim stood in six places, and three greps each found fewer than the last

The progression is the finding, not an embarrassment, and it converged only
when the *shape* of the search changed rather than its wording:

| search | found |
|---|---|
| two literal strings — *"no-op exporter"*, *"exporters become no-ops"* | 1 of 6 |
| the concept, one line at a time — `no-op` **and** `export` on the same line | 5 of 6 |
| the concept, **across line breaks** | 6 of 6 |

**The sixth hid behind a line break, and no line-oriented grep can ever find
it.** `docs/prd/08-operations.md:448-449` wrapped the sentence mid-claim:

```
448: Telemetry is optional: with no OTLP endpoint configured the exporters are
449: no-ops and Usher runs normally.
```

Line 448 contains `exporters` and no `no-op`; line 449 contains `no-ops` and no
`export`. A per-line filter requires both tokens in one line, so it is
*structurally* blind here — not unlucky, blind. **Prose wraps at about 80
columns, so any claim longer than a few words is a coin flip to straddle a
newline**, and `grep`, `rg` without `-U`, and every `| grep` pipeline are all
line-oriented by default.

**The pattern that finds all six**, and the one to run when retiring a claim —
`\s+` and `[\s\S]` are what make it newline-blind in the right direction:

```python
# tight: the claim itself, wherever it wraps
re.compile(r"exporters?\s+(?:are|become|becomes|degrade\s+to|to\s+be)\s+no-?ops?", re.I)
# loose: the concept -- `no-op` within ~120 chars of `export`, newlines included
re.compile(r"(?:export\w*[\s\S]{0,120}?no-?ops?|no-?ops?[\s\S]{0,120}?export\w*)", re.I)
```

Run over `src/`, `docs/`, `tests/`, `.claude/` and `.env.example` **but
excluding this file**, the tight pattern returns **4** hits and the loose one
**17**; both are supersets of the six corrected copies. **The loose pattern
found no seventh**, which is the only reason to believe the list is now
complete. Its extra hits are all correct as they stand and a future run must not
"fix" them:

- `docs/plans/2026-07-28-m1-foundation.md` (3 hits) — a historical plan, which
  `.claude/rules/prd-maintenance.md` forbids retro-editing.
- This entry, `docs/prd/decisions/0007-telemetry-architecture.md`'s `## Evidence`
  section, the M10 plan and spec — all quoting the false claim in order to
  refute it.
- `src/usher/telemetry.py:208` — *"the API's **no-op default**"*, which is true:
  the OTel **API**'s default provider genuinely is a no-op, and that is exactly
  the contrast the correction turns on.
- `src/usher/services/rows/curated.py:445` — a different subject entirely
  (`set_attribute` outside a span).
- `tests/unit/test_telemetry.py:96` — quoting the old requirement prose as
  history.

**Why this file is excluded from its own count, which is a trap worth naming.**
The first version of this entry quoted *"tight 10, loose 28"*. Both were correct
when measured and the loose one was stale one commit later — at **32** — because
*writing the search down* added the six-wordings table and the two regexes to
the very corpus the search scans. **A hit count over a corpus that includes the
document stating the count is self-referential, and it moves every time the
document is edited.** Excluding this file makes the number reproducible and
stable under documentation edits; quoting it with the commit it was measured at
would be the alternative. The same applies to any "N occurrences remain" claim
in any rules file here.

The six false copies, all corrected:

| file | the wording it hid behind |
|---|---|
| `docs/prd/10-telemetry-and-dashboards.md:936` | *"exporters become no-ops"* — the only one the first search matched |
| `src/usher/telemetry.py:4` | *"the exporters are no-ops"* |
| `src/usher/config.py:913` | *"exporters are no-ops"* — on `telemetry_enabled` itself, the very property the claim describes |
| `docs/prd/decisions/0007-telemetry-architecture.md:55` | *"the exporters are no-ops"* |
| `.env.example:289` | *"exporters become no-ops when unset"*, a comment |
| `docs/prd/08-operations.md:448-449` | split across the line break, three lines below its own pointer to PRD 10 |

**A literal-string search is a scan, and a scan that globs the wrong pattern
reports a clean result exactly like a scan that found nothing to report** — the
same family as this repository's standing *"a run that did not run is not a
pass"*.

**Four of the edits are strictly line-count-neutral by construction**, verified
with `git diff --numstat` returning equal adds and deletes, because these files
are cited by line from elsewhere: `src/usher/telemetry.py` in **25** distinct
places in this repository, `src/usher/config.py` in **29**, and
`docs/prd/08-operations.md` in **36** — including `:447`, immediately above the
edit. `~/code/observability` cites `src/usher/telemetry.py:16-17` and `:105-114`
across the repository boundary; both were re-read afterwards and neither moved.
`docs/prd/decisions/0007-telemetry-architecture.md` gets **ADR treatment rather
than a reword**, per `.claude/rules/prd-maintenance.md`: its `Status` is
annotated and the measurement is added as its own `## Evidence` section, because
an ADR left silently contradicting the PRD is exactly what that rule exists to
prevent.

**And this task broke two citations itself, which is the honest version of the
lesson.** O2's *first* commit added two import lines at the top of
`tests/unit/test_telemetry.py` — additive, touching no existing case, and
therefore easy to wave through — and thereby shifted every line below by two,
including the two the M10 plan cites for the pair of negative cases
(`:94`→`:96`, `:231`→`:233`). Repaired in the same series. **An "append-only"
edit is only line-count-safe if the append is at the end**; an import block is
at the top, and the top is above everything anybody cites. The later commits
checked citations before editing and this one did not, which is the whole
difference.

## The HTTP semantic convention in force is the **default** one, pinned by version (2026-08-14, M10 O3)

O2's section above is about what happens to a name *after* it leaves the
process — the collector's storage rename. This one is about what the name **is
on the wire**, which is a different mechanism with a different trigger, and the
two compose: a Phase 2 panel is wrong if either is guessed.

**The convention, by name and version.** The **default** HTTP semantic
conventions, under `opentelemetry-sdk` **1.44.0** and
`opentelemetry-instrumentation-fastapi`/`-asgi` **0.65b0**
(`opentelemetry-semantic-conventions` is also 0.65b0). `uv.lock` pins all of
those; `pyproject.toml:26-31` asks only for `>=1.28` / `>=0.49b0`, so **the
convention in force is a property of the lock file, not of the manifest**, and
`uv lock --upgrade` is enough to move it with no source change anywhere.

**The two rows, driven through a real `create_app()` into an
`InMemoryMetricReader`, one request each, in two separate processes:**

| | metric name | unit | attributes on the point |
|---|---|---|---|
| default (nothing set) | `http.server.duration` | `ms` | `http.scheme`, `http.host`, `http.flavor`, `http.method`, `http.server_name`, `http.status_code`, `http.target` |
| `OTEL_SEMCONV_STABILITY_OPT_IN=http` | `http.server.request.duration` | `s` | `url.scheme`, `http.request.method`, `http.route`, `http.response.status_code`, `network.protocol.version` |

**Under the opt-in `http.server.duration` is absent entirely — not renamed
alongside, gone.** That is the clause worth reading twice: a panel written
against it does not start plotting a differently-named series, it plots
nothing, and nothing anywhere raises. (The M10 plan's version of this table
omits `network.protocol.version` from the opt-in row; it is on the point.)

**The mechanism is two predicates in the installed contrib package**, and
knowing them is what makes the third mode predictable rather than surprising.
`_semconv.py:196-201` defines `_report_new(mode)` as `mode != DEFAULT` and
`_report_old(mode)` as `mode not in (HTTP, DATABASE)`. `asgi/__init__.py:622-635`
then builds `duration_histogram_old` (`ms`) behind `_report_old` and
`duration_histogram_new` (`s`, with second-scaled bucket advice) behind
`_report_new`. So:

- `DEFAULT` satisfies only `_report_old` → the `ms` histogram only.
- `http` satisfies only `_report_new` → the `s` histogram only.
- **`http/dup` satisfies both and emits both histograms.** Observed: one request
  under it recorded `http.server.duration` at `ms` *and*
  `http.server.request.duration` at `s`, plus both response-size spellings.
  **It is the migration path, and the only setting under which a panel written
  against either name keeps working.**

### `http.target` is absent on an unrouted path, and that is the finding a panel needs

`http.target` is `route.path_format` — `_collect_target_attribute`,
`asgi/__init__.py:528-551`, which reads `path_format` off the ASGI scope's
`route` and **returns `None` when nothing matched**. The ASGI middleware omits
a `None` attribute rather than recording an empty one. Measured:
`GET /no-such-route` records an `http.server.duration` point whose attributes
are exactly `{http.scheme, http.host, http.flavor, http.method,
http.server_name, http.status_code: 404}` — the key is **absent**, not empty.

**So a panel that groups by `http.target` silently drops every 404 on an
unrouted path**, which is the one class of request an operator most wants to
see, and it drops them the same way an empty panel looks healthy. Recorded here
rather than discovered from a dashboard that under-counts. Pinned by
`tests/unit/test_telemetry_metric_names.py::test_a_path_that_matched_no_route_carries_no_http_target_at_all`,
whose routed control fires first — "the key is missing" is also what a build
that never recorded the attribute at all would produce.

**Where the opt-in can and cannot be set is recorded in**
`.claude/rules/config-cli-and-deployment.md` — its subject is `config.py`,
`compose.yml` and `.env.example`, and *this* file does not load on any of
those paths, so the finding would never have reached the person adding an
`OTEL_*` key. In one line: the only door is the launching process's own
environment; `.env`, `.env.example` and `compose.yml` each fail a test or
every entry point.

### 34 and 35 (35 and 36 since S2, 2026-08-18) are both right, about different things, and the M10 spec collapsed them

The M10 design spec `:32-33` says the catalogue is *"34 rows and every one is
✅"*. **The catalogue (`10-telemetry-and-dashboards.md:143-177`) is 35 rows, 35
distinct names, and all 35 carry ✅** — counted; there is no unticked row. 34 is
a real measurement of something else: an AST scan of `src/usher/` for the seven
`Meter.create_*` instrument factories finds **exactly 34 declared instrument
names**, and their set is the catalogue **minus `http.server.duration`** — the
one row Usher does not declare, because `FastAPIInstrumentor` emits it. The
honest sentence is **34 instruments Usher declares plus one it inherits,
against 35 documented rows**, and PRD 10's preamble now carries it so the next
reader counts the right thing.

⚠️ **The count has moved twice since, both times inside M10.** S2 added
`usher.source.throttle.wait` (the outbound rate gate's own series,
`usher.adapters.http`) on 2026-08-18, and S8 added
`usher.sync.retraction.fraction` (`usher.services.reconcile`) on 2026-08-19, so
it is now **36 declared plus one inherited against 37 rows** — `assert
len(catalogue) == 37` in `test_telemetry_metric_names.py`. The *mechanism* above
is unchanged; only the two numbers moved, and each time all four sites (the
census assertion, that file's docstring and its `create_*` arithmetic,
`test_telemetry_search.py`'s cross-reference comment, and PRD 10's preamble)
were updated in the same commit as the instrument. The "34 and 35" prose here is
the reading as of 2026-08-14 and is left dated rather than rewritten.

**Four sites, and the census assertion is the only one that fails.** The other
three are prose, so an instrument added without touching them leaves the file
asserting one number and explaining a different one — which is why they are
listed here rather than left to a grep for `36`.

**The scan has a trap that manufactures plausible missing metrics, and the
filter is the factory name — never the prefix.** Re-measured 2026-08-14 at
`fe4becd`: a naive walk over `src/usher/` for calls whose attribute name
`startswith("create_")` finds **120** sites (121 counting the one bare-name
call, `create_async_engine`). **79** are Alembic's `op.create_table` /
`create_index` / `create_foreign_key` under `db/migrations/versions/`, 34 are
the instrument factories, six are `asyncio.create_task` and one is
`sa_asyncio.create_async_engine` (`db/base.py:110`). **All six tasks carry a
`name=`, and every one of them renders `usher.*`** — four string literals
(`usher.lane.worker`, `usher.lane.refresh`, `usher.lane.rows.refresh` at
`api/lanes.py:204-208`, and `usher.jobs.heartbeat` at `services/jobs.py:317`)
plus two f-strings a literal-only scan cannot see at all:
`f"usher.lane.push.{source.name}"` (`api/lanes.py:358`) and
`f"usher.job.{job.kind.value}"` (`services/jobs.py:340`). A prefix filter puts
**six** task names into a catalogue comparison looking exactly like six
undocumented metrics.

⚠️ *An earlier draft of this entry — and of the comment in the test file —
said "four carry a `name=`, three of those are `usher.*`", then listed four.
It was inherited from the M10 plan's prose and nobody re-counted, which is the
same failure as the plan's 117/83 one layer in. **The trap is worse than it was
documented as, which makes the factory-name filter more justified, not less.***

*The M10 plan states 117 sites and 83 Alembic ones, measured 2026-08-13. Both
differ from the above and the plan's own arithmetic does not close (117 − 83 −
4 = 30, against 34 instruments that must also be in the total); 120 = 79 + 34 +
6 + 1 does. Treat 120/79 as the figures and re-derive rather than trusting
either, since both move with every migration.*

Pinned by
`tests/unit/test_telemetry_metric_names.py::test_every_metric_name_usher_emits_is_a_row_of_prd_10s_catalogue`,
which asserts `declared == catalogue - {"http.server.duration"}` — 34 == 34.
**Both halves of it are scans, so both carry premise guards placed before the
value they protect**: the walk must find something and must find
`usher.jobs.queued`; the table parse must find 35 rows, which is the premise a
Markdown regex loses the moment the table is reformatted. Demonstrated red two
ways on the declared side — a **deleted** row (the 35-row guard fires) and a
**renamed** row at a preserved count of 35 (the set comparison fires, which is
what proves the guard is not the only live assertion).

### Three stored series names, observed under Usher's own instruments rather than derived

O2's seven-clause naming rule above is the mechanism and is **not restated
here**; this is a status change to three of the names it produced. O2 marked
Usher's counters and observable gauges as *derived* — the rule applied to
declarations read from source, never seen under their real names. Two of the
three are now **observed**:

| catalogue name | kind, unit (from source) | stored series | status |
|---|---|---|---|
| `usher.search.duration` | histogram, `s` (`services/search.py:106-107`) | `usher_search_duration_seconds_{bucket,count,sum}` | **observed** — O2's live `GET /search`, re-confirmed |
| `usher.ingest.items` | counter, `1` (`services/ingest.py:55-56`) | `usher_ingest_items_total` | **observed** 2026-08-14, was derived |
| `usher.jobs.queued` | observable gauge, `1` (`telemetry.py:299`) | `usher_jobs_queued_ratio` | **observed** 2026-08-14, was derived |

Observed by importing Usher's **own** instrument objects — `_items_counter`
from `services/ingest.py`, `_search_duration` from `services/search.py`, and
the gauge via `telemetry.register_queue_gauges` — installing a real
`MeterProvider` with an OTLP reader against the collector, and reading the
names back out of Prometheus. Nothing was re-declared, so the name, unit and
instrument kind all came from shipped source. `usher_jobs_parked_ratio` came
free from the same registration and is observed too. The emitting resource
carried `service.name="o3nameprobe"` so the series are attributable and are not
mistaken for a real Usher run; the storage rename is computed by the
collector's `prometheusremotewrite` exporter from `(name, unit, kind)` alone
and is independent of every resource attribute, which is what makes that
substitution sound.

**`usher_jobs_queued_ratio` is the one to carry.** A queue-depth gauge stored
under `_ratio` looks like a mistake and is not — it is O2's rule 5 (`unit="1"`
becomes `_ratio` on a gauge) meeting the fact that **every Usher observable
gauge declares `unit="1"`**. The same applies to `usher_jobs_parked_ratio`,
`usher_source_push_connected_ratio`, `usher_search_embeddings_stale_ratio` and
`usher_similarity_neighbors_stale_ratio`, which remain derived.

**O2's own evidence was re-verified rather than assumed**, and one thing about
how to look at it is worth keeping: all seven `probe_*` series are still in the
shared dev Prometheus under `job="namingprobe"`, and `usher_search_duration_*`
under `job="usher"`. But an **instant** query for any of them returns *no
series at all*, because Prometheus's instant lookback is ~5 minutes and those
samples are a day old — indistinguishable, at a glance, from data that was
never written. Query `/api/v1/series` over an explicit range before concluding
that a recorded series is gone.
