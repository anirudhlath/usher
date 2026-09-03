---
paths:
  - "src/usher/config.py"
  - "src/usher/cli.py"
  - "src/usher/__main__.py"
  - "src/usher/db/migrations/env.py"
  - "compose.yml"
  - "Dockerfile"
  - ".env.example"
  - "pyproject.toml"
  - ".github/workflows/**"
---

# Settings, the CLI boundary, compose and the image

Rules for this subsystem; ADR-0026 and the source docstrings hold the arguments.
Derive counts (`alembic heads`, `docker compose config`), never quote them.

## A settings failure must never print the credential it rejected

- **pydantic's `ValidationError` message carries `input_value=…`**, so an
  unrendered settings failure prints the whole DSN with its password *and* the
  `secret_key` the operator did not get wrong. Render `loc` + `msg`, drop
  `input`, and scrub the value out of `msg` too, so a future validator that
  interpolates it cannot reopen this.
- **`--traceback` deliberately does not reopen it** — that stack is six pydantic
  frames which diagnose nothing and one credential.
- **Every entry point building `Settings` needs the boundary, not just the
  CLI.** `db/migrations/env.py` is the second, and since the image's `CMD` is
  `alembic upgrade head && exec python -m usher`, its traceback is the first
  thing in a misconfigured container's log.
- **The rendering lives in `usher.config.settings_rejection`, not `usher.cli`**
  — a contract forbids importing `usher.cli`, which is what stranded the control
  at one entry point. `env.py` raises `SystemExit(settings_rejection(exc,
  entry_point="alembic"))` **`from None`**, since chaining re-prints the
  original, and `SystemExit` rather than `print` so that `&&` still stops.
- **Pin it with a subprocess and an explicit environment variable** — `env.py`
  cannot be imported (it touches `alembic.context`), and a developer checkout
  has a real `.env`, so merely *unsetting* the variable passes for the wrong
  reason. Assert three absences (canary password, `input_value`, `Traceback`),
  one presence (`database_url`), and the non-zero exit.

## What the CLI boundary catches, and what it must not

- **`cli.OPERATOR_ERRORS` (`cli.py:166`) is an enumerated tuple** — `OSError`,
  `DBAPIError`, `httpx.HTTPError`, `PortUnavailable`, `PortAuthFailed`,
  `PortRateLimited`. Every membership decision is recorded there and in
  ADR-0026.
- **`OSError` is a member because asyncpg lets `ConnectionRefusedError` out
  unwrapped during connect** — `except SQLAlchemyError`, the obvious spelling,
  misses the most common operator failure there is.
- **Never widen to `except Exception`**; it passes every presentation case and
  breaks the one that matters, since a bug's traceback is the bug report. Judge
  a candidate member by who can act on it, never by the package it comes from.
- **`DBAPIError`, never `SQLAlchemyError`.** The latter roots everything
  SQLAlchemy raises, including `InvalidRequestError` — `MissingGreenlet`,
  `PendingRollbackError`, `ArgumentError`, `CompileError`, each a bug wearing
  the database's clothes, and catching one leaves two log lines and no frame.
  `OperationalError`/`ProgrammingError`/`InterfaceError` are `DBAPIError`
  subclasses, so a missing table and a dead pool are unaffected.
- **`PortUnavailable`, `PortAuthFailed` and `PortRateLimited` are members;
  `PortDataMalformed` is deliberately not.** An adapter translates transport
  failures into the taxonomy before they cross the port boundary, so
  `httpx.HTTPError` cannot fire behind a port and the tuple was blind to every
  adapter until those three were added. `PortDataMalformed` means *this project
  sent something wrong* and keeps its stack; commands that know what it means
  catch it themselves, which ADR-0026 permits and is not a per-command boundary.
- **`UsherPortError` wholesale is the one-line version and it is wrong.** Many
  commands reach the boundary through raise sites the repositories document as
  tripwires for **bugs in this project's own code**; the line is *reaching an
  upstream* against everything else. The port-local refusals stay out because
  services absorb them, and the taxonomy is read from `__subclasses__()`
  (`ports-and-error-taxonomy.md`).

## The CLI surface

- **A new subcommand owes its own dispatch case.** `_dispatch`'s `else` is
  `serve`, so one that parses but has no arm silently starts uvicorn — and the
  CLI-wide boundary sweep cannot see it, since that sweep patches every dispatch
  coroutine *and* `uvicorn.run` to fail identically on purpose.
- **`main` reads `sys.argv` itself**, because a console script calls it with no
  arguments and treating `argv is None` as "no arguments" made `usher
  sync-status` start the HTTP server. `argv or ["serve"]` still applies once
  `sys.argv[1:]` is empty, which the container's `CMD` depends on.
- **`--source` is optional** — omitted, `sync` walks every *enabled* source and
  skips one with a missing credential row. **`--kind` is `full` or `delta`
  only** (`watch_state` is a lane `sync` runs after the item walk, not an
  alternative), and **`--resolve` requires `--title`** or it blanks a link.
- **`db.users.ensure_default_user` is deliberately not a repository port** — no
  service needs it, and an ABC plus a fake plus a contract suite for one
  `SELECT` is a port with nothing on the other side.
- **`kill -9 "$(cat pidfile)"` on a backgrounded `uv run <cmd> &` does not stop
  the work.** `uv run` forks the real interpreter, so `$!` is the wrapper and
  the child keeps committing; kill `pgrep -P "$wrapper_pid"` or the process
  group. Deployments are unaffected — systemd, Docker and Ctrl-C signal it all.

## `.env` has two readers with different vocabularies

- **Compose substitutes `${…}` from `.env`; pydantic-settings reads the same
  file with `extra="forbid"`.** A compose-only variable is an *extra* input to
  `Settings` and fails **every** entry point — invisible to the suite, since
  `conftest.py::clean_environment` neutralises the `env_file` source.
- **`extra="forbid"` is worth keeping, so the fix is a namespace.** It turns
  `USHER_LOG_LEVL=DEBUG` into a startup failure rather than a line that silently
  does nothing. `USHER_COMPOSE_*` is dropped before validation; everything else
  under `USHER_` is a setting or a typo. Splitting the files leaves compose
  nothing to read; renaming one key lets the next variable reintroduce it.
- **The test that matters is not the one that copies the file.** Regex-scan all
  of `compose.yml` for `${…}` — a `volumes:` or `image:` line is the same hazard
  as `ports:` — plus its twin over `.env.example`; both are needed.
- **Any case written for this passes `_env_file=` explicitly**, since the
  autouse fixture neutralises the class-level `env_file`.

## `env_file:` versus `environment:`

- **`env_file:` hands the whole file over; `environment:` names one variable at
  a time**, forwarding only what somebody remembered to write — it once
  delivered 5 of 30 settings, silently dropping both lane switches. Use
  `env_file:`, long form with `required: false`, so a checkout with no `.env`
  fails on the secret-key guard rather than on a missing file.
- **`environment:` still wins, so what stays in it is what the compose
  *topology* owns**, each with its reason in the file: `USHER_DATABASE_URL`
  (`postgres`, not `localhost`), `USHER_HOST`/`USHER_PORT` (what `ports:`,
  `EXPOSE` and the healthcheck assume), `USHER_SECRET_KEY` as `${…:?}` for the
  guard that fails `docker compose up`, and the two `/data` paths.
- **`USHER_COMPOSE_HOST_PORT` (default `8100`) is the host-side publish port**
  for container port 8000, not a bare `"8000:8000"` — this host already
  publishes something else there. Postgres is never published at all.

## Where a per-deployment fact gets logged

- **Decide a per-process fact where the decision is made, never in a per-pass
  function** — a "no TMDb API key configured" warning in a function the lane
  called every 5 s trains an operator to ignore warnings. It lives in
  `composition.metadata_provider`, which a push-only deployment never reaches,
  correctly. A *build* that runs once per process is still the wrong place to
  decide a fact about the deployment.
- **There are two composition roots — `usher.api` and `usher.cli`.**
  `usher.composition` is not a third; its module docstring says so.

## The image

- **Measure with `docker images`, never `docker image inspect --format
  '{{.Size}}'`.** Under the containerd snapshotter that field is the
  *compressed* content size and understates the image by ~4.2x — `docker images`
  labels its columns, `inspect` answers with no label at all.
- **The shipped image omits the embedding extra** — adding it costs ~250 MB.
- **Three stages:** `console` (`node:26-alpine`) builds `web/dist`, `builder`
  has `uv` and builds the venv, `runtime` copies only `.venv/`, `src/`,
  `alembic.ini` and `/web/dist`, running as `uid=1000(usher)` with no compiler.
- **`README.md` must be `COPY`'d into the builder stage** before the second
  `uv sync` — `pyproject.toml` declares `readme = "README.md"` and hatchling
  reads it while building `usher`'s own wheel.
- **`[tool.ruff] extend-exclude = ["docs", ".claude", "web"]` keeps ruff off
  prose** — ruff 0.16+ formats Python fences inside Markdown, so an unscoped
  `ruff format .` rewrites `docs/` and `.claude/rules/` — and an explicit path
  argument bypasses the exclude entirely.

## Healthchecks

- **The Postgres healthcheck forces TCP (`pg_isready -h 127.0.0.1 …`).**
  `pgvector/pgvector:pg17` runs a temporary bootstrap server during `initdb`
  with `listen_addresses=''`, which the socket-default `pg_isready` calls
  "accepting" — and `depends_on: service_healthy` gates on the **first**
  success, `start_period` exempting only early *failures*.
- **`usher`'s healthcheck targets `/health/ready`.** Plain compose never
  restarts a container for a failed healthcheck (`restart:` triggers on the
  process exiting), so there is no restart-loop risk and readiness is more
  informative than an always-200 `/health`. It uses `urllib.request`, since
  `python:3.13-slim` has no `curl`/`wget`.
- ⚠️ **Nothing in CI verifies HTTP.** The `image` job builds and checks only
  that the console bundle is where `Settings.console_dist_dir` looks. A change
  to the healthcheck, the `CMD` or readiness is verified by running the stack
  locally or not at all.

## Startup and migrations

- **`python -m usher` is what the container runs after `alembic upgrade head`**,
  reading `Settings.host`/`port`. **Keep the `CMD`'s module form and its
  `exec`**, so `docker stop`'s SIGTERM reaches uvicorn, not a wrapping shell.
- **Ask `uv run alembic heads` for the head revision**; never quote a hash.
- **Container-start migration has no distributed lock** — fine at one replica, a
  real problem past one, where migrations belong in a separate one-shot step.
  `/health/ready` surfaces a lost race as a 503 rather than preventing it.

## CI and action pinning

- **Verify a floating major tag with `gh api
  repos/<owner>/<repo>/git/ref/tags/<tag>`, never against the releases page.**
  They are different objects: a repo can publish a `v9.0.0` release while its
  moving major tags stop at `v7`, and the job then fails at *Set up job* before
  one line of this project runs — exactly the half local verification is blind
  to. An exact release tag needs no such check and is the stronger pin.
- **`.python-version` (`3.13`) is load-bearing.** `requires-python = ">=3.13"`
  has no upper bound, so on a fresh runner `uv sync --frozen` otherwise resolves
  the newest Python released, not the one every group verified against.
- **`act` is not installed and was not added** — an emulator whose own
  correctness is unverified adds little. Verify a workflow change by running each
  `run:` step's literal command locally, and note what that cannot reach: the
  actions themselves, and Docker-in-CI for `tests/integration/`.
