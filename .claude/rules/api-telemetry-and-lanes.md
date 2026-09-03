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

Rules for this subsystem; the arguments are in the ADRs and docstrings cited.

## SSE and the event bus

- **Never drive SSE through `httpx.ASGITransport`** — it runs the app to
  completion and buffers, so a stream hangs rather than fails. Use
  `tests/fakes/streaming_asgi_transport.py`.
- **Keep that fake's scope at `spec_version: "2.3"`** —
  `StreamingResponse.__call__` installs `listen_for_disconnect` only below 2.4;
  above it a disconnect never cancels the iterator and no `finally` runs.
- **Hold the pending `__anext__` with `asyncio.wait`, never `wait_for`**
  (`api/routers/events.py:118`). `wait_for` cancels what it waits on, closing
  the generator and disconnecting every client one heartbeat after its last
  event. Argument at `events.py:93`; do not "simplify" that loop.
- **Snapshot the replay ring before adding the subscriber, with no `await`
  between** (`services/events.py:39`) — resolving replay lazily at the first
  `__anext__` delivers the window twice.
- **Commit before you publish.** ADR-0033 owns the rule and its exceptions; the
  residual window is job ordering, not durability, and needs no outbox.
  `services/watch_write.py`'s publish sites are unmeasured.
- **`BootstrapService._publish_progress` stays out of
  `DeferredEventPublisher`** (pinned at `composition.build_worker`): buffering
  it is the 0%-to-100% jump the frame prevents, and `discard()` would drop
  frames for batches that committed.
- **`xmin` is not evidence of an uncommitted read.** Read
  `pg_current_snapshot()` beside it: a writer below its xmin and absent from the
  in-progress list is committed, and what you have is an ordering window.

## OpenTelemetry and logging

- **Call instrumented functions through their module** — `SQLAlchemyInstrumentor`
  patches a module attribute, so a module-scope `from … import X` binds the
  unwrapped original forever. **Assert on a *statement* span**; `connect` spans
  appear either way.
- **Providers are set-once and `_Proxy*` instruments cache the first real
  instrument.** A test installing its own needs
  `conftest.py::reset_otel_meter_provider` **and** its own
  `SQLAlchemyInstrumentor().uninstrument()`, which the shared reset cannot do.
- **Assert pipeline spans by parentage, not by name.** A worker's `job.*` span
  is the deliberate exception: a root with a `Link`.
- **An observable callback must be synchronous and must not touch the
  database** — it runs on the reader's background thread, so bouncing a
  coroutine onto the loop deadlocks. `register_queue_gauges` reads a replaceable
  module global, since the SDK keeps only the first gauge under a name.
- **Providers install unconditionally; only OTLP export is gated on
  `telemetry_enabled`.** Both are `isinstance`-guarded so a second
  `create_app()` does not leak export threads.
- **Stdlib logging is reclaimed into loguru via `_InterceptHandler`** — without
  it only `usher`'s own calls are structured and carry `trace_id`/`span_id`.
- **The reclaim loop resets `.disabled` beside `handlers`/`propagate`.**
  `Logger.handle` tests it below the level check and the handler walk, so a
  `fileConfig` anywhere else mutes a logger process-wide. Snapshot
  `loggerDict` keys before iterating (a `getLogger` can insert mid-iteration).
- **`sink == []` is a false green wherever the fixture made the logging
  impossible.** Put the case where the real logger is, and assert through a
  DEBUG sink so raising a threshold cannot pass for a fix.

## Health, readiness and the lanes

- **`/health` never touches Postgres.** Only `/health/ready` runs `SELECT 1` and
  compares `alembic_version` against `code_head_revision()`, answering **503** —
  probes gate on the code, never the body. Both typed in `api/dto/health.py`.
- **Readiness reports the lanes and never gates on them.** `LaneReport` is
  separate from `ReadinessChecks` because every field of the latter is part of
  the status code; the case proving it needs a reachable database.
- **Both lane switches default on, so every app-building fixture passes
  `push_enabled=False, worker_enabled=False`**, per fixture so it is greppable —
  otherwise a worker polls the real `jobs` table and a push lane opens a real
  socket. `dependency_overrides` never reach a lifespan.
- **`start()` creates tasks and awaits nothing**, which keeps `/health` at 200
  with Postgres down. Drive it by hand: `coro.send(None)` must raise
  `StopIteration`.
- **Crash isolation comes from one task per lane and `return_exceptions=True`
  on `stop()`'s gather**, not from `_guard`'s `except`. `running_sources()` is
  no isolation test: assert the second lane ingests after the first is `done()`.
- **Recovery is a lease, not a startup sweep.** `JobWorker.recover()` passes
  `older_than_seconds=self._lease_seconds`, called every half-lease, so two
  workers never steal live claims. `def startup` is gone; the greps are prose.
- **`SourceStatus` refuses "push available without authenticated" and
  `dataclasses.replace` re-runs `__post_init__`**, so `_with_lane_push_health`
  (`services/sources.py:166`) takes the lane's answer only when authenticated.
- **Set `USHER_PUSH_ENABLED=false` and `USHER_WORKER_ENABLED=false` for a
  bounded live run** — otherwise a websocket and a gap closer decide the count.
- **`push_gap_min_interval_seconds` bounds how *often* the gap is closed, not
  the walk.** Size comes from `ReconcileService.cursor_for` (no completed run
  means no `since`, so the whole library); `USHER_PUSH_GAP_CLOSE` decides.
  **The bound is a refusal, not a cap** — a truncated walk records `COMPLETED`,
  so every item it missed is skipped by every later delta, permanently.
- **Log the gap close in `_close_gap`**, never in per-pass `refresh()`.

## Wiring, dependencies and the import contracts

- **`get_session` is the request's commit/rollback boundary.**
- **An async client fixture needs `asgi_lifespan.LifespanManager`** —
  `ASGITransport` implements "http", not "lifespan", so `session_factory` is
  never set.
- **The default user is a request-scoped dependency (`get_default_user_id`),
  never a lifespan write** — a startup write turns an outage into a crash loop.
- **`EnrichService` is deliberately absent from `api/deps.py`**: its provider
  owns the TMDb token bucket and request scope gives each request a fresh one.
  It belongs on `app.state`.
- **`TitleReadService` holds no `SourceAdapter`, asserted on imports** — "it did
  not raise" is also what swallowing everything produces. Read annotations as
  *text* and walk both `Import` and `ImportFrom`.
- **`usher.composition` needs no contract for the core layers** (it imports
  `usher.db`/`usher.adapters`, so a core module reaching it already breaks
  layering), but `usher.api` is a composition root and exempt from that, so it
  has its own: *no router names the wiring, the curation service or the LLM
  port*. It needs `allow_indirect_imports = true` — a router may *reach* the
  wiring through `api/deps.py`, never *name* it.
- **Cite contracts by name, never ordinal.**
- **Never gate a request-path resource on a lane switch, and park it as `None`
  rather than leaving the attribute absent.** The embedder is built whatever the
  switches say, onto `app.state.embedder`; `embedding_enabled` is the condition,
  and `worker_enabled`'s only job there is `report=`.

## Routes, the problem envelope, DTOs and tickets

- **`app.routes` does not contain the app's routes** — `include_router` appends
  one opaque `_IncludedRouter` each, so an `APIRoute` filter returns an empty
  list a `for` loop iterates happily. Descend `route.original_router.routes`,
  guard the descent found a known path, and group by `route.path`.
- **RFC 9457's `instance` is `request.url.path`**, so a malformed path parameter
  is echoed and no spelling avoids it. The rule covers what a client
  **submitted as data**: assert over pydantic's `input`, never response text.
- **Adopting a status is not adopting the envelope.** `_CODE_FOR_STATUS`
  (`api/errors.py:107`) maps 404/405/422 only; anything else falls back to
  `{"detail": …}`. Raise `ProblemException(...)`; do not widen the map, which
  ADR-0030 owns. Keep the problem-responses scan green.
- **FastAPI has no per-response media type**, so `UsherAPI.openapi` post-passes
  problem bodies onto `PROBLEM_MEDIA_TYPE`. **Key it off the `$ref`, never a
  status list**, keep it idempotent (`app.openapi_schema` caches), subclass
  rather than assign. `content` in `responses=` appends; dropping `model=` dangles.
- **Use `HTTP_422_UNPROCESSABLE_CONTENT`** — the old name warns once per
  *request*, and this suite runs with no expected warnings.
- **A leak check may not forbid "emby"** — an operator names a source "Living
  Room Emby". Assert against a distinctive `external_id` and that key.
- **Every model in `api/dto/` ends in `Response`, nested ones included** —
  `test_api_dto.py` discovers by that suffix and asserts no credential-shaped
  field, so a differently named model is invisible to the scan.
- **`request.url_for` substitutes path parameters raw**, so `api/deps.py`'s
  `quote(ticket, safe="=")` is the only encoding step; `RedirectResponse`
  re-quotes `Location` with `%` safe and `wrap_deep_link` encodes once.
- **`TICKET_TTL_SECONDS: Final = 300` is a constant, not a setting.** Expired
  and forged both answer `404 ticket_invalid` (D1), and **mint and redeem share
  a process** — another `USHER_SECRET_KEY` looks exactly like a ticket bug.

## The worker lane

- **The lane builds the worker once per *process***, lazily inside the loop
  (`if worker is None`, `api/lanes.py:623`), because `_user_id()` is a database
  call and `start()` must connect to nothing. Rule at `api/lanes.py:595`.
- **`SourceRegistry` holds only the adapter cache** and takes the scope's
  pipeline as an argument (`bound(pipeline)`) — a signature, not a convention.
- **Adapter construction is double-checked-locked**, re-read inside the lock; a
  race leaks a socket per loser.
- **The event buffer is per-job scope, never the worker's** — `discard()` would
  empty a concurrent job's frames (ADR-0033 amendment).
- **`asyncio.wait`, never `TaskGroup`, never `gather`.** A task group cancels
  siblings on the first escape, abandoning claims mid-write; `gather` returns
  with siblings unawaited. Under `wait` every task settles first.
- **Two workers cost budget, not correctness**: `USHER_JOB_CONCURRENCY` and
  `USHER_TMDB_REQUESTS_PER_SECOND` are per process against a per-client limit.

## `traceresponse`

- **Every response carries the trace id** via `api/trace_response.py` and
  `telemetry.traceresponse`; the console's "Open trace" link depends on it.
- **A raw ASGI middleware, never `BaseHTTPMiddleware`** — the latter wraps
  `receive`, the machinery SSE disconnect handling depends on.
- **Read `trace.get_current_span()` at `http.response.start`, before
  delegating** — the SERVER span is current in the user middleware slot, and
  `ExceptionMiddleware` is inside it, so problem documents carry the header.
- ⚠️ **A bare 500 does not carry it** — `ServerErrorMiddleware` sits outside the
  user slot, and both alternatives are worse: `set_global_response_propagator`
  is a process global ignoring `is_recording()`, and overriding
  `build_middleware_stack` makes `instrument_app` skip FastAPI instrumentation.
- **Answer `None` — no header — for a non-recording span or an invalid
  context.** An all-zero header is well-formed and names nothing; a sampled-out
  span is the half an all-zero check misses, since a valid id with no exported
  trace opens an empty Tempo page.
- **The header name is contested.** Published W3C trace-context Level 2 defines
  no response header, and on `main` the binding moved to `Server-Timing` (metric
  `trace`, same grammar). Shipped as `traceresponse`, what OTel emits.
