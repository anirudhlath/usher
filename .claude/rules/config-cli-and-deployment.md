---
paths:
  - "src/usher/config.py"
  - "src/usher/cli.py"
  - "src/usher/__main__.py"
  - "src/usher/db/migrations/env.py"
  - "compose.yml"
  - "Dockerfile"
  - ".env.example"
---

# Settings, the CLI boundary, compose and the image

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**A settings failure was printing the credential it rejected, and the CLI was
the only reader that unwrapped it.** pydantic v2's `ValidationError` message
carries `input_value=…`, so `USHER_DATABASE_URL` with a non-asyncpg driver made
`usher bootstrap-status` print the **whole DSN including the password**, and a
truncated `USHER_SECRET_KEY` printed the key — both fields are `SecretStr` in
`Settings` for exactly that reason. This is the same defect `usher.api.errors`
exists to prevent on the 422 path, and it survived four milestones because a
traceback is where nobody looks for a leak. `cli._settings_problem` renders
`loc` + `msg` and drops `input`, scrubbing the value out of `msg` too so a
future validator that interpolates it cannot quietly reopen this.
**`--traceback` deliberately does not reopen it**: a settings failure's stack is
six pydantic frames that diagnose nothing, so re-raising would add only the
credential.
✅ **And `alembic` was a second entry point that bypassed the scrub entirely —
found 2026-08-13, fixed the same day.** `usher.db.migrations.env` called
`get_settings()` with no boundary of its own, so `uv run alembic upgrade head`
with a bad `USHER_DATABASE_URL` printed pydantic's raw `ValidationError` —
`input_value={…}`, a truncated `secret_key` in it, under a full traceback.
Reproduced by running it.

**Two things made it worse than the original rather than a smaller copy.** The
CLI's version leaked a *rejected* value; this leaked every field pydantic
echoes, so a wrong DSN exposed `USHER_SECRET_KEY` — the setting the operator
did **not** get wrong. And the image's `CMD` is
`alembic upgrade head && exec python -m usher`, so on a misconfigured container
that traceback is the **first thing in the log**, emitted before the
application whose boundary would have caught it ever starts.

**The repair could not be a call into `cli._settings_problem`**: an
import-linter contract forbids anything importing `usher.cli`, which is what
had kept the control stranded at one of the two entry points that needed it.
The rendering moved to `usher.config.settings_rejection` — one definition, the
`db/repositories/_errors.py` collapse again — and `env.py`'s `_database_url`
raises `SystemExit(settings_rejection(exc, entry_point="alembic"))` from it.
`from None` and not `from exc`, because chaining re-prints the original under a
*"direct cause"* header and puts back the whole thing being removed;
`SystemExit` and not a `print` so the `&&` in that `CMD` still stops.

**Pinned by a subprocess, which is the only spelling that tests what the
container runs.** `env.py` touches `alembic.context` at import and cannot be
imported by a unit test, so the rest of its file is structural; this case runs
`python -m alembic upgrade head` with `USHER_DATABASE_URL` set to a
wrong-driver DSN carrying a canary password. **The environment variable is
load-bearing rather than convenient**: a developer checkout has a real `.env`
supplying a valid DSN, so a case that merely *unset* the variable would pass
locally for the wrong reason and only ever fail in CI. Three absences (the
password, `input_value`, `Traceback`) and one presence (`database_url`, so the
assertions are not satisfied by a command that printed nothing) plus the
non-zero exit. Planted and watched to fail before it was believed: with the
`except` removed the case reports `'hunter2xyzzy' is contained here:
l://admin:hunter2xyzzy@db:5432/usher', input_type=str]`.
**A refused Postgres connection reaches the CLI as a bare `ConnectionRefusedError`,
not as a SQLAlchemy error.** asyncpg lets the `OSError` out unwrapped during
connect, so `except SQLAlchemyError` — the obvious spelling for a database error
boundary — misses the single most common operator failure there is. Checked by
running it, not by reading the class hierarchy. `SQLAlchemyError` is still in
`cli.OPERATOR_ERRORS` because it *does* catch the other half (a missing table
from an `alembic upgrade head` that never ran).
**`except Exception` at a CLI boundary trades a wart for a blindfold.** It
passes every behavioural case about presentation and breaks the one that
matters — a bug's traceback is the bug report, and an operator can do nothing
with `AttributeError: 'NoneType' object has no attribute 'id'` collapsed to one
line either way. `cli.OPERATOR_ERRORS` is an enumerated tuple for that reason,
and `test_a_programming_error_keeps_its_traceback` is what fails when somebody
widens it.
**`.env` has two readers with different vocabularies, and that broke the
README's own first step for four milestones.** Docker Compose reads `.env`
to substitute `${...}` into `compose.yml`; pydantic-settings reads the same
file as a settings source with `extra="forbid"`. So a compose-only variable
is an *extra* input to `Settings` — and `USHER_HOST_PORT`, the host-side
publish port shipped in `.env.example` since M1, made `cp .env.example .env`
fail **every** entry point: `uv run pytest` at 1637 passed / 461 errors,
`usher bootstrap-status` and `usher push --probe` with a raw traceback and
exit 1. Found by M5's smoke test on 2026-08-02, present on `origin/main`
since M1, and invisible to 2,098 passing tests for the reason
`tests/conftest.py::clean_environment` exists at all: it neutralises the
`env_file` source so a developer's own `.env` cannot fail the suite. **The
461 errors that did appear came from the one path that fixture cannot
reach** — `tests/integration/conftest.py::_upgrade_head`, session-scoped,
which saves and restores `os.environ` but has no way to hide a file.

- **`extra="forbid"` is worth keeping and is why the fix is a namespace.**
  It is what turns `USHER_LOG_LEVL=DEBUG` into a startup failure rather than
  a line in `.env` that silently does nothing. `extra="ignore"` fixes the
  crash by breaking that; splitting the files leaves compose nothing to read
  (compose substitutes from `.env` and nowhere else, short of `--env-file`
  on every invocation); renaming the one key fixes today and lets the next
  compose variable reintroduce it. So the two readings are separated by
  **name**: `USHER_COMPOSE_*` is dropped before validation, everything else
  under `USHER_` is a setting or a typo.
- **The test that matters is not the one that copies the file.** A case
  building `Settings` from `.env.example` passes against a fix that
  special-cases `usher_host_port`. What fails if a *future* compose variable
  reintroduces the outage is `test_every_variable_compose_substitutes_is_a_
  setting_or_compose_reserved`, which regex-scans the whole of `compose.yml`
  for `${...}` — over the whole file, not just `ports:`, because a variable
  added to a `volumes:` or an `image:` line is the same hazard — plus its
  twin over `.env.example`. Both are needed: the M1 commit that introduced
  `USHER_HOST_PORT` touched both files.
- **Any case written for this must pass `_env_file=` explicitly.** The
  autouse fixture neutralises the class-level `env_file`, so a case that
  relies on it proves nothing. Same shape as the `sitecustomize.py`
  installation proof.
**`env_file:` and `environment:` are different mechanisms, and picking the
second forwarded 5 of 30 settings into the container.** `printenv` inside
the running container showed `USHER_DATABASE_URL`, `USHER_SECRET_KEY`,
`USHER_TMDB_API_KEY` and the two `OTEL_*` — nothing else. 24 documented
settings were unreachable, **12 of them M5's own** (`USHER_PUSH_*`,
`USHER_SSE_*`, both lane switches). `environment:` names one variable at a
time and compose substitutes its value; `env_file:` hands the file over. The
first needs a line somebody remembers to write, which is why the count drifts
by a milestone's worth of settings at a time.

- **`USHER_WORKER_ENABLED` is the one with teeth.** It is documented
  (`README.md`, `.env.example`) and it *works* when delivered directly —
  `/health/ready` reports `"worker": false` and the lane stops. Set in
  `.env`, the only place the docs point at, it was silently ignored, so an
  operator following the README leaves `worker: true` and then starts
  `usher work` in a second container: two workers, and `JobWorker.startup()`
  requeues everything `running`, so each steals the other's live claims.
- **`environment:` still wins over `env_file:`, so what is left in it is
  what the compose *topology* owns**, four keys, each with its reason in the
  file: `USHER_DATABASE_URL` (`postgres`, not `localhost`),
  `USHER_HOST`/`USHER_PORT` (bind-all and 8000 — what `ports:`, `EXPOSE` and
  the healthcheck all assume), and `USHER_SECRET_KEY` (kept as `${...:?}`
  purely for the guard that fails at `docker compose up` with a sentence).
- **Measured with `docker compose config`, not argued**: 5 `USHER_*`/`OTEL_*`
  keys rendered into the container before, **39 after** (38 `Settings` fields
  plus `USHER_COMPOSE_HOST_PORT`, which the app ignores by design — the
  namespace proving itself), with `published: "8100"` → `target: 8000`
  unchanged. `env_file:` uses the long form with `required: false` so a
  checkout with no `.env` still parses and fails on the secret-key guard
  rather than on a missing file.
**A per-process fact logged in a per-pass function is ~17,280 warnings a
day.** `build_worker` logged `no TMDb API key configured; enrich jobs will
not be claimed` unconditionally, and `usher.api.lanes._run_worker` calls it
once per pass at `IDLE_SLEEP_SECONDS = 5.0` — measured at exact 5 s
intervals in the default no-key deployment, and in `usher push` too. The
information is worth surfacing; at that rate it trains an operator to ignore
warnings, which is the failure a log level exists to prevent. It moved to
`composition.metadata_provider`, which is where the decision is *made* and
which each of the three composition roots calls exactly once per process —
and which a push-only deployment never reaches at all, correctly, since with
no worker there are no enrich jobs to leave unclaimed. (The sentence itself
reads `enrich and derive jobs will not be claimed` since 2026-08-07 — M7 put
`DERIVE` behind the same `provider is not None` guard and left this line
promising one kind while two went unclaimed. The finding above is about
*where* the line lives, not its wording.) `usher work` was
already calling `build_worker` once outside its loop, so that root saw one
warning either way; the lane was the one at 5 s. The case that has teeth
drains **three** worker passes and asserts the sink is empty — asserting
after one pass cannot tell "once" from "per pass", the same shape
`test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass` needed.

`[tool.ruff] extend-exclude = ["docs"]` keeps ruff off `docs/plans/*.md` and
`docs/prd/*.md` — ruff 0.16+ formats/lints Python code fences embedded in
Markdown by default, and those two directories hold planning and PRD prose
with embedded code fences that must stay byte-identical for other groups to
transcribe. Without the exclude, an unscoped `ruff format .` silently
rewrites that prose.

**Measure the image with `docker images`, NOT with
`docker image inspect --format '{{.Size}}'`** — Docker 29.2.1 on this host uses
the containerd snapshotter, under which that field is the **compressed** content
size and understates the image by ~4.2x. Measured 2026-08-03 on the same build:
`inspect` **84.2 MB** against `docker images` **356 MB**. The M1 figure of
332 MB is an uncompressed one, so 356 MB is the like-for-like comparison —
**+24 MB / +7.2% across five milestones**, and M6 added no runtime dependency at
all. Task 28's own command in the M6 plan is the `inspect` form, which would
have reported a 4x improvement that did not happen.
**Re-measured 2026-08-12 at M9's close: `359 MB`** (`docker build -t usher:m9-h6 .`
off the M9 lock, `docker images` reporting `DISK USAGE 359MB` against
`CONTENT SIZE 84.9MB`). M9's C1 read **358 MB** off the lock ten days earlier, so
the milestone's whole HTTP surface — **eleven new routers, seventeen in all**,
the image proxy, the playback ticket — cost **+3 MB / +0.8%** and no new runtime
distribution. (*"Five routers"* stood here for one commit and was wrong: the
count at the M9 plan commit was **6** and it is **17** at the close, measured as
`ls src/usher/api/routers/*.py` minus `__init__.py`, not recalled.) **The
methodology finding is the durable half and it is unchanged**: the two numbers on
that line still differ by 4.2×, so a reader taking the smaller one is still off
by a factor of four. What *has* changed is that Docker 29.6 labels the columns
`DISK USAGE` and `CONTENT SIZE` rather than printing one ambiguous `SIZE`, which
makes the trap visible at the terminal for the first time — do not read that as
the trap being gone, because `docker image inspect --format '{{.Size}}'` still
answers the compressed figure with no label at all.
**The shipped image does NOT install the embedding extra, and that is what the
compose stack pulls.** Built with `uv sync --frozen --no-dev --extra embedding`
for comparison it is **607 MB** (venv 314 MB against 133 MB), **+251 MB and
still no torch** — against ADR-0022's counterfactual of roughly 5 GB for
`sentence-transformers`.

`USHER_COMPOSE_HOST_PORT` (`.env`, defaults to `8100`) is the *host*-side
publish port for `usher`'s container port `8000` — deliberately not a bare
`"8000:8000"`, since this host already publishes an unrelated container's
app on host port 8000. Postgres's own port is never published to the host
at all, only reachable from `usher` over the compose network as
`postgres:5432`, matching PRD 08's deployment shape. It was `USHER_HOST_PORT`
until 2026-08-02, which is the bug below.

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
`astral-sh/setup-uv@v9.0.0` — the plan's `@v4`/`@v5` were several majors
stale by the time this ran (checked against each action's own GitHub
releases).

**`@v9` was wrong and the first real CI run is what said so.** Checking an
action's *releases* answers whether a version exists, not whether the
**floating major tag** does, and those are different objects:
`astral-sh/setup-uv` publishes `v9.0.0` as a release but its moving major
tags stop at `v7` (`v1`…`v7` exist; `v8` and `v9` do not). So `@v9`
resolved to nothing and the job failed in 2 s at *Set up job* —
`Unable to resolve action astral-sh/setup-uv@v9, unable to find version v9`
— before `checkout` ran, before `uv` existed, and before one line of this
project's code was executed. Every `run:` step below had been verified
locally, which is exactly the half that failure is blind to. Verify a
floating tag with `gh api repos/<owner>/<repo>/git/ref/tags/<tag>`, not
against the releases page; an exact release tag needs no such check and is
the stronger pin anyway. Found 2026-08-02, on the first push to a real
GitHub remote — which is to say the workflow was unexecuted for five
milestones and the note below saying so was the accurate part.

A new `.python-version` file (`3.13`) at the repo root exists
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

**Gotcha found running this: `kill -9 "$(cat pidfile)"` on a backgrounded
`uv run <command> &` does not stop the work.** `uv run` forks a child
process (the real interpreter) rather than exec-replacing itself — verified
directly with `ps --forest`, which showed two live PIDs, the `uv` wrapper
and its `python3` child. Killing only the wrapper PID left the child
running, orphaned, still committing to the database — the first kill/resume
attempt against this exact pipeline was contaminated by exactly this before
it was caught (a `bootstrap-status` read raced an orphaned child still
writing). A real deployment is unaffected: systemd's `KillMode=control-group`,
Docker's container-wide signal delivery, and an interactive terminal's
Ctrl-C all reach the whole process group, not just one PID in it. A
hand-rolled `nohup ... & echo $!` script does not — kill the child
(`pgrep -P "$wrapper_pid"`) or the whole process group, never just the
captured `$!`.

**`usher` is a console script (`[project.scripts]`, added 2026-08-01) and
`python -m usher` is the same code path.** Both land on `usher.cli.main`.
The container's `CMD` stays `alembic upgrade head && exec python -m usher` —
the module form is the one whose `exec`/SIGTERM behaviour was verified
against a running container, and there is nothing to gain by re-verifying an
equivalent spelling. Verified from a clean `uv sync`.

**A console script calls `main()` with *no arguments*, which is why `main`
reads `sys.argv` itself.** Before that it treated `argv is None` as "no
arguments at all" and substituted `["serve"]`, so `usher sync-status` would
have silently started the HTTP server — an entry point that ignores
everything it is given and looks like it works, because the server does
start. `argv or ["serve"]` still applies once `sys.argv[1:]` is empty, which
is the property the container's `CMD` depends on. Both halves pinned in
`tests/unit/test_main.py`.

`--source` is optional: omitted, `sync` walks every *enabled* source, and a
source whose credential row has gone missing is skipped with a message
rather than taking the other two down. `--kind` offers `full` and `delta`
only — `watch_state` is a real `SyncRunKind` and is a lane `sync` always
runs *after* the item walk (it resolves each state against a `MediaItem`),
never an alternative to it. `--resolve` and `--title` are used together, and
`parse_args` refuses one without the other: `attach_title` writes what it is
given, so `--resolve` alone would blank a link instead of creating one.

**A command's `_dispatch` arm is unpinned by the CLI-wide boundary sweep,
because that sweep makes both arms fail identically on purpose.** Found
2026-08-07 by M8 Task 18's sweep. `_dispatch`'s `else` is `serve`, so a
subcommand that parses and has no arm of its own does not fail — it starts
uvicorn. `tests/unit/test_cli_errors.py::
test_every_command_reports_a_dead_database_the_same_way` runs over every
subcommand and cannot see it: `_every_command_raises` patches every dispatch
coroutine **and** `uvicorn.run` to raise the same exception, which is exactly
what makes it a test of the *boundary* and exactly what makes the two arms
indistinguishable. Measured — deleting `elif args.command == "curate"` left
the whole selection green. The case that closes it makes the two differ
(`_curate` records, `uvicorn.run` raises), and it is the same shape as the
`argv is None` defect one layer up, where `usher sync-status` silently
started the HTTP server and looked like it worked because the server does
start. **A new command owes this case; the boundary table does not supply
it.**

**`httpx.HTTPError` can never fire behind a port, so the CLI boundary was
blind to every adapter in the project — fixed 2026-08-07 by widening
`OPERATOR_ERRORS`, and the interesting half is the six families that stayed
out.** Verified 2026-08-07 by `issubclass(family, cli.OPERATOR_ERRORS)` →
`False` for `UsherPortError` and **all nine** of its subclasses.
`OpenAICompatibleClient` translates everything it catches into the taxonomy
*before* it crosses the port boundary — which is what the taxonomy is for — so
the family the tuple named was unreachable from that adapter, and `usher curate`
against an unreachable `USHER_LLM_BASE_URL` printed a **stack** ending in
`PortUnavailable: POST /chat/completions failed: ConnectError`, having already
committed the `llm_calls` row. Billed *and* handed a stack; ADR-0026's own
motivating defect in a family the ADR did not name.

**That translation is two mechanisms and not one, and this file named the wrong
one until 2026-08-07.** It said `OpenAICompatibleClient` *"translates every
**transport** failure into `PortUnavailable`/`PortAuthFailed`/
`PortRateLimited`"* — right about which three families the widening needed,
wrong about where they come from, and silent about a fourth. `_send`'s `except
usher.adapters.http.UNTRANSLATED_FAILURES` (shared by all three adapters
since 2026-08-10; `_send`'s own copy is gone) is the **transport** mechanism and it raises
`PortUnavailable` **and nothing else**; `PortAuthFailed` and `PortRateLimited`
are decided by **status code** in `_decode`, and so is the family that was
missing. Measured 2026-08-07 by driving `complete_json` over
`httpx.MockTransport`, no socket opened:

| where | what happened | family |
|---|---|---|
| `_send` | any transport failure — `ConnectError`, `ConnectTimeout`, `ReadTimeout`, `RemoteProtocolError`, `TooManyRedirects`, `InvalidURL`, `CookieConflict`, or the bare `RuntimeError` a closed `AsyncClient` raises | `PortUnavailable` |
| `_decode` | HTTP 429 | `PortRateLimited` |
| `_decode` | HTTP 401, 403 | `PortAuthFailed` |
| `_decode` | **any other 4xx except 408** — measured on 400, 402, 404, 409, 422, 499 | **`PortDataMalformed`** |
| `_decode` | HTTP 408, and every 5xx | `PortUnavailable` |
| `_decode`, `_content`, `_parse` | a 200 whose body is not the promised shape — not JSON, a JSON list, no `choices`, `finish_reason == "length"`, content that will not parse | `PortDataMalformed` |

**The omitted row is the one that behaves differently at the boundary**, which
is what makes the omission worse than an abbreviation: `PortDataMalformed` is
deliberately *not* in `OPERATOR_ERRORS`, so of the four families this adapter
can raise it is the only one that still reaches `main` carrying a stack. And it
is not a corner — the 4xx-other row is where a schema the provider will not
accept, a model name it does not serve and an over-length prompt all land.
`curation-and-llm.md` measured the last of those as a plain HTTP 400 at pool
700 on a 16k-context model, i.e. on a setting PRD 08 invites an operator to
raise. What keeps `usher curate` itself off a stack there is its own `except
PortDataMalformed`, not the tuple — the distinction the last bullet below is
about.

- **Task 18 declined it and Task 18's review reopened it.** The refusal was
  *"widening a settled ADR wants evidence per family"* — a good bar, and the
  reproduction above is the evidence. The tuple now carries
  `PortUnavailable`, `PortAuthFailed`, `PortRateLimited`; see ADR-0026's
  **Amendment**, which is where the argument lives.
- **`UsherPortError` itself is the one-line version and it is wrong.** All
  fifteen commands were swept: seven have a path where widening changes
  behaviour, and only three of those are cleanly operator-fixable. The other
  four reach the boundary through raise sites the repositories document as
  tripwires for **bugs in this project's own code** —
  `TitleNeighborRepository.replace`'s bounds (*"a bug in the blend"*), the
  credits delete's scope (*"the one job it has"*), `curated_rows`' assembly
  CHECKs, and `FastEmbedEmbedder`'s vectors-to-texts mismatch (*"the most
  damaging bug available in this milestone"*). The line drawn is **reaching
  an upstream** against **everything else**.
- **`UsherPortError` has nine subclasses, not four and not six, and this file
  said four.** `SourceNotSupported` (`ports/source.py`),
  `FilterNotSupported` (`ports/search.py`) and `AvailabilitySweepRefused`
  (`ports/ingest.py`) live beside their own port rather than in
  `ports/errors.py`, so a reader who greps the taxonomy module undercounts by
  three — twice, in two reviews. `test_the_port_taxonomy_is_split_and_the_
  base_class_is_not_in_the_tuple` now reads the set off `__subclasses__()`
  (importing all three explicitly, since a class nothing imported is a
  subclass Python does not report) so the count cannot go stale again.
  All three stay out for a *different* reason from the content families:
  `ReconcileService` and `PushService` absorb two of them and
  `PostgresSearchIndex._TRANSLATORS` covers every `SearchFilters` field, so
  no measured path reaches the boundary with one.
- **`PortDataMalformed` staying out is load-bearing in two places**:
  `cli._vocabulary_line` catches it and prints it as a status line, and
  `cli._curate` turns it into a sentence about last night's screen. Both are
  a command that knows what the message *means*, which ADR-0026 permits — as
  distinct from the per-command *boundary* it rejects.

`usher.db.users.ensure_default_user` creates the row nothing ever had.
`usher.domain.watch.User` documents a singleton `is_default` user as what
stands in PRD 01's authentication seam and `watch_states.user_id` is a real
foreign key, so the watch lane and the `watch_history` handler were both
unrunnable without it. Deliberately not a repository port — no *service*
needs it (`WatchStateSyncService` takes a `user_id` per call), and an ABC
plus a fake plus a contract suite for one `SELECT` is a port with nothing on
the other side.

**And the blindfold this file warns about had already been on, spelled
`SQLAlchemyError` rather than `Exception`.** The entry above is right that
`except Exception` at a CLI boundary trades a wart for a blindfold, and
`test_a_programming_error_keeps_its_traceback` does fail anyone who widens the
tuple that way. What neither caught is that `SQLAlchemyError` — a member since
M1, whose comment reads *"everything the driver does wrap"* — is also the base
of `InvalidRequestError`, and therefore of `MissingGreenlet`,
`PendingRollbackError`, `ObjectDeletedError`, `ArgumentError` and
`CompileError`. Measured: M9's S3 lost a `usher work` daemon to an unhandled
`MissingGreenlet` and the whole of what it left in its log was
`_operator_problem`'s two lines (`/tmp/m9-exec/S3/w1.log`, 2026-08-11
23:26:32Z). Issue #8 was filed blaming the missing `--traceback` flag. Narrowed
to `DBAPIError` on 2026-08-19 — `OperationalError`/`ProgrammingError`/
`InterfaceError` are all subclasses, so the missing-table case is unaffected —
plus `test_the_operator_database_family_is_what_the_driver_wraps`, which
asserts no member of the tuple is a base of `MissingGreenlet`. **The general
form: a family named by the library that raises it is not a family. Name it by
who can act on it.** SQLAlchemy is this project's one dependency that raises an
operator's problem and a programmer's under a single root, which is why it is
the one that needed the narrower name. Full evidence and the
identity-map artefact the same conflict path leaves behind:
`.claude/rules/db-and-sql.md`; the decision is ADR-0026's 2026-08-19
amendment.

## `OTEL_SEMCONV_STABILITY_OPT_IN` cannot be set from anything this repository ships (2026-08-14, M10 O3)

**Why this is filed here rather than beside the telemetry it affects.** Setting
that variable silently renames the HTTP server-duration metric and re-units it,
emptying any dashboard panel built on the old name — the measurement is in
`.claude/rules/api-telemetry-and-lanes.md`. But the *configuration* half is
about `config.py`, `compose.yml`, `.env.example` and
`tests/unit/test_deployment_config.py`, and the telemetry rules file **does not
load on any of those paths**. Somebody adding an `OTEL_*` key to `compose.yml`
would never have seen the assertion that refuses it. A finding filed where it
cannot fire is a finding nobody has.

This is stronger than the sentence PRD 10 used to carry — *"Nothing in this
project's config sets that variable"*, removed in commit `4148589`, which was a
statement about today's *values*. This is a statement about the *shape* of the
configuration, and there are four doors:

- **As a line in `.env`** → `ValidationError: otel_semconv_stability_opt_in —
  Extra inputs are not permitted`, from **every** entry point, because they all
  build `Settings`. `Settings.model_config` is `extra="forbid"`
  (`config.py:144-149`) and pydantic-settings' dotenv source hands an unmatched
  key back under its full lowercased name. Measured directly by planting the
  line, not reasoned.
  ⚠️ **The mechanism is narrower than "`.env` refuses `OTEL_*`", and getting
  that wrong would make the rule look false**: `Settings` declares
  `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_SERVICE_NAME` as *aliased fields*
  (`config.py:757-758`), and both are accepted from `.env` today. What is
  refused is every **un-declared** key — probed one at a time:
  `OTEL_SEMCONV_STABILITY_OPT_IN`, `OTEL_TOTALLY_MADE_UP`, `ZZZ_RANDOM_KEY` and
  `USHER_MADE_UP` all rejected; the two aliased ones accepted.
- **In `compose.yml`'s `environment:` block** →
  `tests/unit/test_deployment_config.py:337` asserts that block's key set
  **equals** the five names in `_TOPOLOGY_OWNED` (`:89-97`). *(That constant
  holds five members while its own docstring says "the four" twice, at `:340`
  and `:343` — the docstring is stale, the assertion is not.)*
- **In `.env.example`** → `tests/unit/test_deployment_config.py:282` asserts
  `.env.example`'s key set **equals** the `Settings` field set.
- **As a process environment variable** → **works.** `Settings` builds normally
  (measured), because the variable never reaches pydantic at all. This is the
  only door, and it is outside everything the repository ships.

So the convention in force is held by three tests and a validation error rather
than by a comment — which is the point of writing it down this way. **The same
argument covers any future `OTEL_*` knob**: the SDK reads them from
`os.environ`, `Settings` refuses them from `.env`, and the two aliased fields
above are the only ones this project routes through its own configuration.
