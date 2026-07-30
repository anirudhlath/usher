# CLAUDE.md

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL.

**Status: M1 foundation complete.** The project scaffold, environment
config, domain models, port ABCs, persistence (SQLAlchemy schema + Alembic
migrations + title repository), the telemetry bootstrap, a FastAPI app
with liveness/readiness endpoints, and the container image + compose stack
+ CI all exist and are verified working — see `docs/plans/
2026-07-28-m1-foundation.md` for the task breakdown and `docs/prd/
09-roadmap.md` for what's next (M2). Do not invent commands for tooling
that does not exist yet — check the Commands section below before
assuming something runs.

## Keep the PRD current

`docs/prd/` is the authoritative, living description of what Usher is and why.
Code that contradicts it is a bug in one of them — resolve it, never let it
drift silently.

**Update the PRD in the same commit as the change that invalidates it.** Not in
a follow-up, not "later". A change that alters behaviour and leaves the PRD
stale is incomplete.

Start at `docs/prd/README.md` for the index. Detailed maintenance conventions
load automatically when working in `docs/`.

## Conventions that will bite you

- **Ports are `abc.ABC`, not `typing.Protocol`.** Deliberate — see
  [ADR-0001](docs/prd/decisions/0001-abc-over-protocol.md). Do not "modernise"
  them to Protocols.
- **Layering is enforced, not advisory.** `domain/` imports nothing from
  `adapters/`, `db/`, or `api/`; `services/` depends only on `domain/` and
  `ports/`. CI checks this with `import-linter`.
- **No source-specific concept escapes its adapter.** If something only makes
  sense for Emby, it belongs in `adapters/emby/` or on `MediaItem` — never on
  `Title`, never in an API response.
- **Identity is our UUIDv7.** `tmdb_id`/`imdb_id` are indexed attributes, never
  primary keys, never identifiers in an API contract.
- **Domain models are frozen — use `.evolve()`, never `model_copy(update=)`.**
  Every `usher.domain` model inherits `DomainModel`
  (`src/usher/domain/base.py`), so `model_copy(update=...)` is reachable on
  all of them but skips validation entirely: it can hand back an instance
  with a wrong-typed or out-of-range field that pydantic still serializes
  without complaint. `.evolve(**changes)` re-validates from scratch and is
  the only sanctioned write path.
- **Ship importers, never data.** No third-party metadata may be committed or
  included in a release artifact — IMDb and TMDb both prohibit redistribution.
  Users run importers and hold their own API keys. Attribution strings stay in
  the API surface.
- **Use `uv`** for all Python work: `uv sync`, `uv run <cmd>`, `uv add <pkg>`.
  Never pip/conda, never activate a venv.
- **TDD.** Failing test first, then implementation.
- **Secrets in `Settings` are `pydantic.SecretStr`**, never plain `str` —
  `database_url`, `secret_key`, `tmdb_api_key`. Unwrap with
  `.get_secret_value()` only at the point of use (e.g. handing a DSN to
  `create_async_engine`); never store the unwrapped value in a variable that
  outlives that call, and never let it reach a log line or an exception
  message. This is how `docs/prd/08-operations.md`'s "credentials are never
  logged" rule is enforced rather than merely asserted.

## Verified facts worth not re-deriving

**Emby push works.** Verified 2026-07-29 against the live server with a normal
non-admin token: `/embywebsocket` upgrades (101), delivers periodic `Sessions`,
and pushes `UserDataChanged` within seconds of an out-of-band state change. Two
earlier negative findings were both wrong — see
[ADR-0004](docs/prd/decisions/0004-push-over-polling.md).

Health-check caveat: a handshake against *any* path succeeds, so a successful
upgrade is not a health signal. Assert on received messages instead.

## Commands

Verified working as of Group A (scaffold + config):

```bash
uv sync                          # install dependencies
uv run pytest                    # run the test suite (now needs Docker — see Group E below)
uv run pytest tests/unit         # fast unit tests only, no Docker required
uv run ruff check .              # lint — clean
uv run ruff format .             # format — clean
uv run mypy                      # type check, strict mode — clean
uv run lint-imports              # enforce architecture contracts — 4 kept, 0 broken
```

`[tool.ruff] extend-exclude = ["docs"]` keeps ruff off `docs/plans/*.md` and
`docs/prd/*.md` — ruff 0.16+ formats/lints Python code fences embedded in
Markdown by default, and those two directories hold planning and PRD prose
with embedded code fences that must stay byte-identical for other groups to
transcribe. Without the exclude, an unscoped `ruff format .` silently
rewrites that prose.

Verified working as of Group D (db engine, models, migrations) — requires a
live Postgres (e.g. `docker run -d -e POSTGRES_USER=usher -e
POSTGRES_PASSWORD=usher -e POSTGRES_DB=usher -p 5432:5432
pgvector/pgvector:pg17`), so not part of the default `uv run pytest` run:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head                       # apply migrations
uv run alembic downgrade base                     # reverse them (0001 is fully reversible)
uv run alembic revision --autogenerate -m "..."    # generate a migration from model changes
```

**`--autogenerate` is blind to two categories of change — verify by eye, not
just by running it:**
- **CHECK constraint bodies.** Changing a bound (e.g. loosening
  `ck_titles_year_non_negative`'s `>= 0`) and running `--autogenerate`
  produces an empty `pass` migration with no warning — verified directly.
  This schema deliberately mirrors every Pydantic field constraint as a
  CHECK, so this will eventually bite: tightening or loosening one in a
  model file does not, by itself, get picked up.
- **Triggers and functions** (the three `set_updated_at()` triggers from
  the first migration). These aren't SQLAlchemy `Table` metadata at all, so
  autogenerate never sees them, in either direction — adding, dropping, or
  changing one is always a hand-written `op.execute(...)` migration.

Verified working as of Group E (title repository, first integration tests) —
`tests/integration/` runs against a real PostgreSQL, started and torn down
per test run by `testcontainers` (`pgvector/pgvector:pg17`; first run pulls
the image, ~625 MB). Docker must be running; nothing else to set up. Its
schema comes from running the real Alembic migration once per test session
(`postgres_url`, `tests/integration/conftest.py`), not `Base.metadata.
create_all` — CHECK constraint bodies and the three `set_updated_at`
triggers are invisible to `create_all` the same way they're invisible to
`--autogenerate` (above), so a suite that never runs the migration can't
catch either drifting from the models. Each test still gets a fully
isolated database via a connection-bound transaction rolled back
afterward, not a schema recreate — cheaper than the 23-tests-worth of
`create_all`/`drop_all` cycles that used to cost, and `tests/integration/
test_migrations.py` is the ongoing regression check (trigger existence,
plus an autogenerate diff against the migrated database asserting no
drift):

```bash
uv run pytest                        # full suite — 235 tests, needs Docker for the 44 under tests/integration/
uv run pytest tests/unit             # 191 tests, no Docker
uv run pytest tests/integration      # 44 tests, needs Docker
uv run pytest -m "not integration"   # marker equivalent of tests/unit
uv run pytest -m integration         # marker equivalent of tests/integration
```

Two ways to select the same split — pick whichever fits: directory (what
Task 10 itself was written and verified against) or the `integration`
marker (registered in `pyproject.toml`, auto-applied to everything under
`tests/integration/` by that directory's `conftest.py`). Both are kept in
sync deliberately, so Group G's CI can use either without the two
diverging. Not wired into `addopts` as a default `-m "not integration"` —
that would make `pytest tests/integration/...` silently collect zero tests
instead of running them.

`tests/contract/title_repository_contract.py` holds the behavioural
assertions every `TitleRepository` implementation must satisfy — the same
suite runs against `FakeTitleRepository` (`tests/unit/`, no Docker) and
`PostgresTitleRepository` (`tests/integration/`, real Postgres), so the two
are verified to actually agree instead of merely looking alike. This is the
pattern PRD 08 calls the "contract suite" for `SourceAdapter`; M3 is
expected to reuse it.

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

Verified working as of Group G (container image, compose stack, CI) — M1
is now deployable, not just runnable from a dev shell:

```bash
docker build -t usher .                       # multi-stage, ~332MB, non-root
echo "USHER_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build                  # postgres + usher, both healthchecked
curl -sf http://localhost:8100/health         # {"status":"ok"}
curl -sf http://localhost:8100/health/ready   # {"status":"ready","checks":{"database":true,"migrations":true}}
docker compose down                           # data/ bind mounts survive -- not removed by down, -v or not
```

`USHER_HOST_PORT` (`.env`, defaults to `8100`) is the *host*-side publish
port for `usher`'s container port `8000` — deliberately not a bare
`"8000:8000"`, since this host already publishes an unrelated container's
app on host port 8000. Postgres's own port is never published to the host
at all, only reachable from `usher` over the compose network as
`postgres:5432`, matching PRD 08's deployment shape.

The image is genuinely multi-stage: a `builder` stage has `uv` and builds
the venv, a `runtime` stage copies only `.venv/` and `src/` across. No
dependency in `uv.lock` needed a compiler to install (verified: `python:
3.13-slim` has none, and the build never installed one) — every one
resolved to a prebuilt `cp313` wheel. Verified directly against the built
image: runs as `uid=1000(usher)` (`touch /root/nope` → `Permission
denied`), has neither `uv` nor `gcc`/`cc` on `PATH`. `pyproject.toml`
declares `readme = "README.md"`; hatchling (the build backend) reads that
file while building `usher`'s own wheel, so `README.md` has to be `COPY`'d
into the builder stage before the second `uv sync` (the one that installs
the project itself, not just its dependencies) — omitted, that step fails.

**The Postgres healthcheck forces TCP
(`pg_isready -h 127.0.0.1 -U usher -d usher`), not the more obvious
`pg_isready -U usher -d usher`.** `pgvector/pgvector:pg17` runs a
*temporary* bootstrap server during `initdb` on a fresh volume — started
with `listen_addresses=''` (Unix socket only, confirmed against the
running container's own log line: `LOG: listening on Unix socket
"/var/run/postgresql/.s.PGSQL.5432"`, no TCP line) — to run init scripts
before the real server starts. `pg_isready` with no `-h` defaults to the
Unix socket, so an unqualified healthcheck reaches that temporary server.
Verified directly, twice: once with a standalone `docker run` polled every
~0.1s, once against the literal container `docker compose up` creates for
this project (same tight poll, racing the container's own creation from a
background process started before `docker compose up`). Both runs show
the same shape — the Unix-socket form reports "accepting connections"
while the bootstrap server is up, then "rejecting connections" for
roughly a second while it shuts down and the real server starts, then
"accepting" again once the real server is listening (standalone:
accepting at t+1.8s, rejecting t+2.0s–2.9s, accepting again from t+3.0s;
against the compose-managed container: same shape, ~1.1s-wide window). The
TCP-forced form (`-h 127.0.0.1`) never once false-positived in either run:
"no response" solidly until the exact moment the real server started
accepting TCP connections, because the bootstrap server never listens on
TCP at all. `depends_on: condition: service_healthy` gates on the first
successful check, not N consecutive ones, and `start_period` only exempts
early *failures* from counting — it does not delay a false-positive
*success* from being believed — so the Unix-socket form is a real,
reproducible way for `usher` to start against a Postgres that is about to
be torn down and restarted. Docker's own 2s-interval healthcheck did not
happen to land inside the ~1.1s window in the compose runs observed here —
that's host-load luck, not a guarantee, which is why this was verified by
tight-polling the mechanism directly rather than trusting a handful of
`docker compose up` runs to have been unlucky in the right way.

**`usher`'s own healthcheck targets `/health/ready`, not `/health`.**
Plain `docker compose` (no Swarm) never restarts a container because its
healthcheck failed — verified against Docker's documented behaviour, an
unhealthy status only ever changes what `docker compose ps` reports and
what `depends_on: condition: service_healthy` gates on; `restart:
unless-stopped` triggers on the container's *process* exiting, a
condition a failing healthcheck alone does not cause. With no restart-loop
risk in this deployment shape, `/health/ready` (database + migration
state) is strictly more informative for what a compose healthcheck
actually gates than `/health` (always 200, checks nothing) would be.
Compose has no separate liveness/readiness probe pair the way Kubernetes
does, so one healthcheck necessarily conflates the two; readiness is the
more useful of the two to conflate it into. No `curl`/`wget` in
`python:3.13-slim` (and adding either would cut against a small image), so
both the `usher` healthcheck and the CI verification below use Python's
own `urllib.request` — `urlopen` already raises on any non-2xx status or
connection failure, which is already a nonzero exit, so no explicit
try/except is needed for a check where any exception already means
"unhealthy".

`Settings.host`/`Settings.port` validated but were previously read by
nothing — the only way to start the server was the `uvicorn` CLI with
hardcoded `--host 0.0.0.0 --port 8000`. `src/usher/__main__.py`
(`python -m usher`, what the container's `CMD` now runs after `alembic
upgrade head`) fixes this: `uvicorn.run("usher.api.app:create_app",
factory=True, host=settings.host, port=settings.port)`, the same code
path the CLI form uses internally. Local dev is unaffected — `uv run
uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000`
still works exactly as documented above.

Migrations run on container start (`alembic upgrade head && exec python -m
usher`, `exec` so `docker stop`'s SIGTERM reaches uvicorn directly instead
of being swallowed by the wrapping shell) — verified end to end against a
clean volume: `docker exec ... psql -c '\dt'` shows all five core tables
(`titles`, `sources`, `media_items`, `users`, `watch_states`) plus
`alembic_version` at `a8a0e10ff464`, and `SELECT tgname FROM pg_trigger
WHERE NOT tgisinternal` shows all three `set_updated_at` triggers — the
migration ran for real, not `create_all`. **This has no distributed lock**
— fine at M1's one-replica scale, a real problem the moment `usher` is
ever scaled past one replica, at which point migrations belong in a
separate one-shot step instead of every replica's own startup;
`/health/ready`'s migration-mismatch check would surface a lost race as a
503 rather than prevent it. Noted in the Dockerfile's own `CMD` comment,
not solved — nothing in M1 runs more than one replica.

Test count grew from 235 to 237 (`src/usher/__main__.py`'s two new unit
tests). Full suite with coverage, exactly as CI runs it: `uv run pytest
--cov=usher --cov-report=term-missing` → 237 passed, 98% coverage.

CI (`.github/workflows/ci.yml`) pins `actions/checkout@v7` and
`astral-sh/setup-uv@v9` — the plan's `@v4`/`@v5` were several majors
stale by the time this ran (checked against each action's own GitHub
releases). A new `.python-version` file (`3.13`) at the repo root exists
because of a real gap found by running the install step, not by
inspection: `pyproject.toml`'s `requires-python = ">=3.13"` has no upper
bound, and a bare `uv sync --frozen` on a machine with no Python
preinstalled (verified on a stock `ubuntu:24.04` container with a
freshly-installed `uv`, standing in for a fresh runner) resolved **Python
3.14.6** — newer than the 3.13.14 every group has actually developed and
had mypy strict/pytest/ruff verified against. With `.python-version`
present, the identical command resolves `3.13.14` instead. `act` is not
installed on this host and was not added to check this workflow (a
GitHub-Actions emulator whose own correctness is itself unverified doesn't
add much confidence over not having it) — instead, every `run:` step's
literal command was run locally exactly as written, in order, and all
passed: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format
--check .`, `uv run mypy` (`Success: no issues found in 67 source files`
— the mypy-override contingency for `usher.db.migrations.*` was never
needed), `uv run lint-imports` (4 contracts kept), `uv run pytest --cov=
usher --cov-report=term-missing`. Not reproduced byte-for-byte: the
`setup-uv` action's own code (its net effect — a working `uv` on `PATH`
that obeys `.python-version` — was verified by installing `uv` the same
way, astral's own install script, on a bare `ubuntu:24.04` container,
which is a reasonable proxy for a fresh runner but not the literal
`ubuntu-latest` GitHub-hosted image), and Docker-in-CI for
`tests/integration/`'s testcontainers (GitHub's own docs state
`ubuntu-latest` ships Docker running by default, and this project's `uv
run pytest` already depends on exactly that locally, but no run happened
on an actual GitHub-hosted runner).
