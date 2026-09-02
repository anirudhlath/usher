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

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed. The always-on conventions live in `CLAUDE.md`; this file is the
evidence.

## Re-derive before you quote

```bash
docker build -t usher:now . && docker images usher    # DISK USAGE, not inspect
docker compose config                                 # what reaches the container
uv run alembic heads                                  # the head revision
uv run pytest --collect-only -q | tail -1             # suite size
uv run lint-imports                                   # contract count
grep -c '^USHER_' .env.example                        # documented settings
grep -rn 'uses:' .github/workflows/                   # every action pin
gh api repos/actions/upload-artifact/git/ref/tags/v4  # does a floating tag exist?
```

**Every number in this file is dated because every one of them has drifted.**
As of **2026-09-02**: head migration **`m10b`**, **5,747** tests collected,
**12** import-linter contracts, **73** `Settings` fields against **72**
`USHER_*` lines in `.env.example`, **266** `.py` files under `src/`, **17** CLI
subcommands, **17** routers.

## A settings failure was printing the credential it rejected

pydantic v2's `ValidationError` message carries `input_value=…`, so
`USHER_DATABASE_URL` with a non-asyncpg driver made `usher bootstrap-status`
print the **whole DSN including the password**, and a truncated
`USHER_SECRET_KEY` printed the key — both fields are `SecretStr` in `Settings`
for exactly that reason. This is the same defect `usher.api.errors` exists to
prevent on the 422 path, and it survived four milestones because a traceback is
where nobody looks for a leak. `cli._settings_problem` renders `loc` + `msg`
and drops `input`, scrubbing the value out of `msg` too so a future validator
that interpolates it cannot quietly reopen this. **`--traceback` deliberately
does not reopen it**: a settings failure's stack is six pydantic frames that
diagnose nothing, so re-raising would add only the credential.

✅ **And `alembic` was a second entry point that bypassed the scrub entirely —
found and fixed 2026-08-13.** `usher.db.migrations.env` called `get_settings()`
with no boundary of its own, so `uv run alembic upgrade head` with a bad
`USHER_DATABASE_URL` printed pydantic's raw `ValidationError` —
`input_value={…}`, a truncated `secret_key` in it, under a full traceback.
Reproduced by running it. **Two things made it worse than the original rather
than a smaller copy.** The CLI's version leaked a *rejected* value; this leaked
every field pydantic echoes, so a wrong DSN exposed `USHER_SECRET_KEY` — the
setting the operator did **not** get wrong. And the image's `CMD` is `alembic
upgrade head && exec python -m usher`, so on a misconfigured container that
traceback is the **first thing in the log**, before the application whose
boundary would have caught it ever starts.

**The repair could not be a call into `cli._settings_problem`**: an
import-linter contract forbids anything importing `usher.cli`, which is what
had kept the control stranded at one of the two entry points that needed it.
The rendering moved to `usher.config.settings_rejection`, and `env.py`'s
`_database_url` raises `SystemExit(settings_rejection(exc,
entry_point="alembic"))` from it — `from None` and not `from exc`, because
chaining re-prints the original under a *"direct cause"* header and puts back
the whole thing being removed; `SystemExit` and not a `print` so the `&&` in
that `CMD` still stops.

**Pinned by a subprocess, which is the only spelling that tests what the
container runs.** `env.py` touches `alembic.context` at import and cannot be
imported by a unit test, so the case runs `python -m alembic upgrade head` with
`USHER_DATABASE_URL` set to a wrong-driver DSN carrying a canary password.
**The environment variable is load-bearing rather than convenient**: a
developer checkout has a real `.env` supplying a valid DSN, so a case that
merely *unset* the variable would pass locally for the wrong reason and only
ever fail in CI. Three absences (the password, `input_value`, `Traceback`) and
one presence (`database_url`, so the assertions are not satisfied by a command
that printed nothing) plus the non-zero exit. Planted and watched to fail
first: with the `except` removed the case reports `'hunter2xyzzy' is contained
here: l://admin:hunter2xyzzy@db:5432/usher', input_type=str]`.

## What the CLI boundary catches, and what it must not

`cli.OPERATOR_ERRORS` (`src/usher/cli.py:166`) is an enumerated tuple, and on
2026-09-02 it is `OSError`, `DBAPIError`, `httpx.HTTPError`, `PortUnavailable`,
`PortAuthFailed`, `PortRateLimited`. Every membership decision below is
recorded at that tuple and in ADR-0026.

**A refused Postgres connection reaches the CLI as a bare
`ConnectionRefusedError`, not as a SQLAlchemy error** — which is why `OSError`
is a member. asyncpg lets the `OSError` out unwrapped during connect, so
`except SQLAlchemyError`, the obvious spelling for a database error boundary,
misses the single most common operator failure there is. Checked by running it,
not by reading the class hierarchy.

**`except Exception` at a CLI boundary trades a wart for a blindfold.** It
passes every behavioural case about presentation and breaks the one that
matters — a bug's traceback is the bug report, and an operator can do nothing
with `AttributeError: 'NoneType' object has no attribute 'id'` collapsed to one
line either way. The tuple is enumerated for that reason, and
`test_a_programming_error_keeps_its_traceback` fails anyone who widens it.

**And the blindfold this file warns about had already been on, spelled
`SQLAlchemyError` rather than `Exception` — narrowed to `DBAPIError`
2026-08-19 (issue #8, ADR-0026's amendment).** `SQLAlchemyError` was a member
from M1, under a comment reading *"everything the driver does wrap"*. It is in
fact the root of everything SQLAlchemy raises, including
`InvalidRequestError` — which is `MissingGreenlet`, `PendingRollbackError`,
`ObjectDeletedError`, `ArgumentError` and `CompileError`, every one a bug in
this project wearing the database's clothes.

Measured, not argued: one of three `usher work` daemons died **78 minutes into
M9's S3 130,334-request enrichment crawl** on an unhandled `MissingGreenlet`,
and the whole record it left was this boundary's own two lines
(`/tmp/m9-exec/S3/w1.log`, 2026-08-11 23:26:32Z). Issue #8 was then filed
reading *"the run used bare `usher work`, so no stack was recorded"* — putting
the fault on the operator for not passing `--traceback` when the fault was the
tuple. A stack that would have named the frame was caught here and thrown away,
and the bug stayed unexplained for a week over ~92,000 jobs nobody could re-run.

**The repair is `DBAPIError`, which is what that comment already claimed.**
`OperationalError`, `ProgrammingError` and `InterfaceError` are all
`DBAPIError` subclasses, so a missing table (an `alembic upgrade head` that
never ran), a dead pool and a rejected permission are unaffected, and
`test_a_database_error_the_driver_does_wrap_is_operator_facing` passes
unchanged. Two cases guard it: `test_a_missing_greenlet_keeps_its_traceback`
(behavioural) and `test_the_operator_database_family_is_what_the_driver_wraps`
(membership — it asserts no member of the tuple is a base of `MissingGreenlet`,
so a future widening back to `SQLAlchemyError` fails rather than quietly
restoring the blindfold).

**The general form is worth more than the fix: a family named by the library
that raises it is not a family; a family named by who can act on it is.**
`SQLAlchemyError`, `httpx.HTTPError` and `OSError` are all library-shaped
names, and only the last two happen to coincide with "the operator can fix
this" — `OSError` because a refused socket is a refused socket,
`httpx.HTTPError` because an adapter has already translated everything that was
not transport. SQLAlchemy is the one dependency here raising *both* kinds under
one root. Check any future member against that question, not against the
package it comes from.

**`httpx.HTTPError` can never fire behind a port, so the CLI boundary was blind
to every adapter in the project — fixed 2026-08-07, and the interesting half is
the six families that stayed out.** Verified by `issubclass(family,
cli.OPERATOR_ERRORS)` → `False` for `UsherPortError` and **all nine** of its
subclasses. `OpenAICompatibleClient` translates everything it catches into the
taxonomy *before* it crosses the port boundary, so the family the tuple named
was unreachable from that adapter, and `usher curate` against an unreachable
`USHER_LLM_BASE_URL` printed a **stack** ending in `PortUnavailable: POST
/chat/completions failed: ConnectError`, having already committed the
`llm_calls` row. Billed *and* handed a stack.

**That translation is two mechanisms and not one, and this file named the wrong
one until 2026-08-07.** `_send`'s `except usher.adapters.http.
UNTRANSLATED_FAILURES` (shared by all three adapters since 2026-08-10) is the
**transport** mechanism and raises `PortUnavailable` **and nothing else**;
`PortAuthFailed` and `PortRateLimited` are decided by **status code** in
`_decode`, and so is the family that was missing. Measured 2026-08-07 by
driving `complete_json` over `httpx.MockTransport`, no socket opened:

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
accept, a model name it does not serve and an over-length prompt all land
(`curation-and-llm.md` measured the last as a plain HTTP 400 at pool 700 on a
16k-context model, a setting PRD 08 invites an operator to raise). What keeps
`usher curate` off a stack there is its own `except PortDataMalformed`, not the
tuple.

- **Task 18 declined the widening and Task 18's review reopened it.** The
  refusal was *"widening a settled ADR wants evidence per family"* — a good
  bar, and the reproduction above is the evidence. See ADR-0026's
  **Amendment**.
- **`UsherPortError` itself is the one-line version and it is wrong.** Every
  command was swept — fifteen at that date, **17 subcommands on 2026-09-02**.
  Seven have a path where widening changes behaviour, and only three are
  cleanly operator-fixable; the other four reach the boundary through raise
  sites the repositories document as tripwires for **bugs in this project's own
  code** — `TitleNeighborRepository.replace`'s bounds, the credits delete's
  scope, `curated_rows`' assembly CHECKs, `FastEmbedEmbedder`'s
  vectors-to-texts mismatch. The line drawn is **reaching an upstream** against
  **everything else**.
- **The three port-local subclasses — `SourceNotSupported`,
  `FilterNotSupported`, `AvailabilitySweepRefused` — stay out for a *different*
  reason:** `ReconcileService` and `PushService` absorb two and
  `PostgresSearchIndex._TRANSLATORS` covers every `SearchFilters` field, so no
  measured path reaches the boundary with one. The rule that the taxonomy is
  read from `__subclasses__()` and never hand-written lives in
  `ports-and-error-taxonomy.md` (moved there 2026-09-01).
- **`PortDataMalformed` staying out is load-bearing in two places**:
  `cli._vocabulary_line` catches it and prints it as a status line, and
  `cli._curate` turns it into a sentence about last night's screen. Both are a
  command that knows what the message *means*, which ADR-0026 permits — as
  distinct from the per-command *boundary* it rejects.

**A command's `_dispatch` arm is unpinned by the CLI-wide boundary sweep,
because that sweep makes both arms fail identically on purpose.** Found
2026-08-07 by M8 Task 18's sweep. `_dispatch`'s `else` is `serve`, so a
subcommand that parses and has no arm of its own does not fail — it starts
uvicorn. `tests/unit/test_cli_errors.py::
test_every_command_reports_a_dead_database_the_same_way` runs over every
subcommand and cannot see it: `_every_command_raises` patches every dispatch
coroutine **and** `uvicorn.run` to raise the same exception, which is what
makes it a test of the *boundary* and what makes the two arms
indistinguishable. Measured — deleting `elif args.command == "curate"` left the
whole selection green. The case that closes it makes the two differ.
**A new command owes this case; the boundary table does not supply it.**

**A console script calls `main()` with *no arguments*, which is why `main`
reads `sys.argv` itself.** Before that it treated `argv is None` as "no
arguments at all" and substituted `["serve"]`, so `usher sync-status` would
have silently started the HTTP server — an entry point that ignores everything
it is given and looks like it works, because the server does start. `argv or
["serve"]` still applies once `sys.argv[1:]` is empty, which is the property
the container's `CMD` depends on; both halves are pinned in
`tests/unit/test_main.py`. `usher` is a console script (`[project.scripts]`)
and `python -m usher` is the same code path; both land on `usher.cli.main`.

`--source` is optional: omitted, `sync` walks every *enabled* source, and a
source whose credential row has gone missing is skipped with a message rather
than taking the other two down. `--kind` offers `full` and `delta` only —
`watch_state` is a real `SyncRunKind` and is a lane `sync` always runs *after*
the item walk, never an alternative to it. `--resolve` and `--title` are used
together, and `parse_args` refuses one without the other: `attach_title` writes
what it is given, so `--resolve` alone would blank a link.

`usher.db.users.ensure_default_user` creates the row nothing ever had — a
singleton `is_default` user standing in PRD 01's authentication seam, without
which the watch lane and the `watch_history` handler were unrunnable
(`watch_states.user_id` is a real foreign key). Deliberately not a repository
port: no *service* needs it (`WatchStateSyncService` takes a `user_id` per
call), and an ABC plus a fake plus a contract suite for one `SELECT` is a port
with nothing on the other side.

**`kill -9 "$(cat pidfile)"` on a backgrounded `uv run <command> &` does not
stop the work.** `uv run` forks a child process (the real interpreter) rather
than exec-replacing itself — verified with `ps --forest`, two live PIDs, the
`uv` wrapper and its `python3` child. Killing only the wrapper left the child
running, orphaned, still committing to the database; the first kill/resume
attempt against this pipeline was contaminated by it (a `bootstrap-status` read
raced an orphaned child still writing). A real deployment is unaffected —
systemd's `KillMode=control-group`, Docker's container-wide signal delivery and
an interactive Ctrl-C all reach the whole process group — but a hand-rolled
`nohup ... & echo $!` script is not. Kill the child (`pgrep -P
"$wrapper_pid"`) or the whole process group, never just the captured `$!`.

## `.env` has two readers with different vocabularies

That broke the README's own first step for four milestones. Docker Compose
reads `.env` to substitute `${...}` into `compose.yml`; pydantic-settings reads
the same file as a settings source with `extra="forbid"`. So a compose-only
variable is an *extra* input to `Settings` — and `USHER_HOST_PORT`, the
host-side publish port shipped in `.env.example` since M1, made `cp .env.example
.env` fail **every** entry point: `uv run pytest` at 1637 passed / 461 errors,
`usher bootstrap-status` and `usher push --probe` with a raw traceback and exit
1. Found by M5's smoke test on 2026-08-02, present on `origin/main` since M1,
and invisible to 2,098 passing tests because
`tests/conftest.py::clean_environment` neutralises the `env_file` source so a
developer's own `.env` cannot fail the suite. **The 461 errors that did appear
came from the one path that fixture cannot reach** —
`tests/integration/conftest.py::_upgrade_head`, session-scoped, which saves and
restores `os.environ` but has no way to hide a file.

- **`extra="forbid"` is worth keeping and is why the fix is a namespace.** It
  is what turns `USHER_LOG_LEVL=DEBUG` into a startup failure rather than a
  line in `.env` that silently does nothing. `extra="ignore"` fixes the crash
  by breaking that; splitting the files leaves compose nothing to read (it
  substitutes from `.env` and nowhere else, short of `--env-file` on every
  invocation); renaming the one key fixes today and lets the next compose
  variable reintroduce it. So the two readings are separated by **name**:
  `USHER_COMPOSE_*` is dropped before validation, everything else under
  `USHER_` is a setting or a typo.
- **The test that matters is not the one that copies the file.** A case
  building `Settings` from `.env.example` passes against a fix that
  special-cases `usher_host_port`. What fails if a *future* compose variable
  reintroduces the outage is `test_every_variable_compose_substitutes_is_a_
  setting_or_compose_reserved`, which regex-scans the whole of `compose.yml`
  for `${...}` — the whole file, not just `ports:`, because a variable added to
  a `volumes:` or `image:` line is the same hazard — plus its twin over
  `.env.example`. Both are needed: the M1 commit that introduced
  `USHER_HOST_PORT` touched both files.
- **Any case written for this must pass `_env_file=` explicitly.** The autouse
  fixture neutralises the class-level `env_file`, so a case that relies on it
  proves nothing.

**`env_file:` and `environment:` are different mechanisms, and picking the
second forwarded 5 of 30 settings into the container.** `printenv` inside the
running container showed `USHER_DATABASE_URL`, `USHER_SECRET_KEY`,
`USHER_TMDB_API_KEY` and the two `OTEL_*` — nothing else. 24 documented
settings were unreachable, **12 of them M5's own** (`USHER_PUSH_*`,
`USHER_SSE_*`, both lane switches). `environment:` names one variable at a time
and compose substitutes its value; `env_file:` hands the file over. The first
needs a line somebody remembers to write, which is why the count drifts by a
milestone's worth of settings at a time.

- **`USHER_WORKER_ENABLED` is the one with teeth.** It is documented
  (`README.md`, `.env.example`) and *works* when delivered directly —
  `/health/ready` reports `"worker": false` and the lane stops. Set in `.env`,
  the only place the docs point at, it was silently ignored, so an operator
  following the README leaves `worker: true` and then starts `usher work` in a
  second container. **What that used to cost was correctness and now costs only
  budget.** Until M9's W1 (2026-08-12), `JobWorker.startup()` called
  `requeue_running()` on its `older_than_seconds=0.0` default — every `running`
  row, so two workers stole each other's live claims. **`recover()` never did
  this**: claims carry a lease (`USHER_JOB_LEASE_SECONDS` →
  `Settings.job_lease_seconds`, default 300) and a heartbeat (`JobQueue.touch`),
  and `recover()` (`services/jobs.py:268`) passes
  `older_than_seconds=self._lease_seconds` (`jobs.py:288`), so it requeues only
  lapsed claims; `api/lanes.py:638` calls it once per throttle interval (half
  the lease). Two workers still share one queue and still spend the same
  upstream budget twice, but a live claim is no longer stealable. The
  measurement is under W1 in `tmdb-and-enrichment.md`.
- **`environment:` still wins over `env_file:`, so what is left in it is what
  the compose *topology* owns** — six keys, each with its reason in the file:
  `USHER_DATABASE_URL` (`postgres`, not `localhost`), `USHER_HOST`/`USHER_PORT`
  (bind-all and 8000, what `ports:`, `EXPOSE` and the healthcheck all assume),
  `USHER_SECRET_KEY` (kept as `${...:?}` purely for the guard that fails at
  `docker compose up` with a sentence), and `USHER_IMAGE_CACHE_DIR`
  (`compose.yml:58`) / `USHER_BULK_DATA_DIR` (`compose.yml:81`), both
  container-side paths under the `/data` mount.
- **Measured with `docker compose config`, not argued**: 5 `USHER_*`/`OTEL_*`
  keys rendered into the container before, **39 after** — 38 `Settings` fields
  as of 2026-08-02 plus `USHER_COMPOSE_HOST_PORT`, which the app ignores by
  design, the namespace proving itself. (On 2026-09-02: **73** fields against
  **72** `USHER_*` lines in `.env.example`; count rather than trust either
  number.) `published: "8100"` → `target: 8000` unchanged. `env_file:` uses the
  long form with `required: false` so a checkout with no `.env` still parses
  and fails on the secret-key guard rather than on a missing file.

`USHER_COMPOSE_HOST_PORT` (`.env`, default `8100`) is the *host*-side publish
port for `usher`'s container port `8000` — deliberately not a bare
`"8000:8000"`, since this host already publishes an unrelated container's app
on host port 8000. Postgres's own port is never published to the host at all,
only reachable from `usher` over the compose network as `postgres:5432`. It was
`USHER_HOST_PORT` until 2026-08-02, which is the bug above.

## A per-process fact logged in a per-pass function is ~17,280 warnings a day

`build_worker` logged `no TMDb API key configured; enrich jobs will not be
claimed` unconditionally, and `usher.api.lanes._run_worker` called it **once
per pass** at `IDLE_SLEEP_SECONDS = 5.0` — measured at exact 5 s intervals in
the default no-key deployment, and in `usher push` too. The information is
worth surfacing; at that rate it trains an operator to ignore warnings, which
is the failure a log level exists to prevent.

⚠️ **The per-pass call site is gone and the finding is not.** Since M9's W1 the
lane builds the worker **once per process** — `api/lanes.py:623` is guarded by
`if worker is None`, and `api/lanes.py:595`'s docstring states the rule at the
definition. The warning still does not belong there, because a *build* that
happens once in this process is still the wrong place to decide a fact about
the deployment. It lives in `composition.metadata_provider`, which is where the
decision is *made* — and which a push-only deployment never reaches at all,
correctly, since with no worker there are no enrich jobs to leave unclaimed.

**There are two composition roots, not three, and four call sites.**
`composition.py:1` opens *"The wiring both composition roots share"*, `:3` names
them (`usher.api` and `usher.cli`), and `:18` says of `usher.composition`
itself **"Not a third composition root"** — it decides *when* to run nothing,
opens no session and owns no process lifetime. `metadata_provider` is called
once per process by each root, at four sites: `cli.py:522`, `cli.py:646`,
`cli.py:1712` and `api/app.py:100`.

(The sentence itself reads `enrich and derive jobs will not be claimed` since
2026-08-07 — M7 put `DERIVE` behind the same `provider is not None` guard and
left this line promising one kind while two went unclaimed. The finding is
about *where* the line lives, not its wording.) `usher work` already called
`build_worker` once outside its loop, so that root saw one warning either way;
the lane was the one at 5 s. The case with teeth drains **three** worker passes
and asserts the sink is empty — asserting after one pass cannot tell "once"
from "per pass".

## The image, and how to measure it

**Measure with `docker images`, NOT with `docker image inspect --format
'{{.Size}}'`** — Docker 29.2.1 on this host uses the containerd snapshotter,
under which that field is the **compressed** content size and understates the
image by ~4.2x. Measured 2026-08-03 on the same build: `inspect` **84.2 MB**
against `docker images` **356 MB**. The M1 figure of 332 MB is uncompressed, so
356 MB is the like-for-like comparison — **+24 MB / +7.2% across five
milestones**. Task 28's own command in the M6 plan is the `inspect` form, which
would have reported a 4x improvement that did not happen.

**Re-measured 2026-08-12 at M9's close: `359 MB`** (`docker images` reporting
`DISK USAGE 359MB` against `CONTENT SIZE 84.9MB`, off the M9 lock). M9's C1
read **358 MB** ten days earlier, so the milestone's whole HTTP surface —
**eleven new routers, seventeen in all**, the image proxy, the playback ticket
— cost **+3 MB / +0.8%** and no new runtime distribution. (*"Five routers"*
stood here for one commit and was wrong: the count at the M9 plan commit was
**6**, measured as `ls src/usher/api/routers/*.py` minus `__init__.py`, not
recalled.) **The methodology finding is the durable half and it is
unchanged**: the two numbers still differ by 4.2×. Docker 29.6 now labels the
columns `DISK USAGE` and `CONTENT SIZE` rather than one ambiguous `SIZE` — do
not read that as the trap being gone, because `docker image inspect --format
'{{.Size}}'` still answers the compressed figure with no label at all.

**The shipped image does NOT install the embedding extra, and that is what the
compose stack pulls.** Built with `uv sync --frozen --no-dev --extra embedding`
for comparison it is **607 MB** (venv 314 MB against 133 MB), **+251 MB and
still no torch** — against ADR-0022's counterfactual of ~5 GB for
`sentence-transformers`.

**Three stages since the console landed:** `console` (`node:26-alpine`,
`Dockerfile:16`) builds `web/dist`, `builder` (`:40`) has `uv` and builds the
venv, and `runtime` (`:74`) copies only `.venv/` (`:88`), `src/` (`:89`),
`alembic.ini` (`:90`) and `--from=console /web/dist` (`:97`). No dependency in
`uv.lock` needed a compiler — every one resolved to a prebuilt `cp313` wheel,
and the built image runs as `uid=1000(usher)` (`touch /root/nope` →
`Permission denied`) with neither `uv` nor `gcc`/`cc` on `PATH`.
`pyproject.toml` declares `readme = "README.md"`; hatchling reads that file
while building `usher`'s own wheel, so `README.md` has to be `COPY`'d into the
builder stage (`Dockerfile:71`) before the second `uv sync` — omitted, that
step fails.

`[tool.ruff] extend-exclude = ["docs", ".claude", "web"]` (`pyproject.toml:213`)
keeps ruff off `docs/plans`, `docs/prd`, `.claude/rules` and `web/` — ruff
0.16+ formats/lints Python code fences embedded in Markdown by default, and
those directories hold prose with embedded fences that must stay byte-identical
for other groups to transcribe. Without it, an unscoped `ruff format .`
silently rewrites that prose.

## Healthchecks

**The Postgres healthcheck forces TCP (`pg_isready -h 127.0.0.1 -U usher -d
usher`, `compose.yml:205`), not the obvious `pg_isready -U usher -d usher`.**
`pgvector/pgvector:pg17` runs a *temporary* bootstrap server during `initdb` on
a fresh volume, started with `listen_addresses=''` — Unix socket only,
confirmed against the container's own log line (`LOG: listening on Unix socket
"/var/run/postgresql/.s.PGSQL.5432"`, no TCP line) — and `pg_isready` with no
`-h` defaults to that socket. Verified by tight-polling twice (a standalone
`docker run` and the literal container `docker compose up` creates): the
Unix-socket form reports "accepting" while the bootstrap server is up,
"rejecting" for ~1 s while it shuts down, then "accepting" again — a ~1.1 s
window. The TCP-forced form never once false-positived, because the bootstrap
server never listens on TCP at all.

Why that window is a hazard rather than a curiosity: `depends_on: condition:
service_healthy` gates on the **first** successful check, not N consecutive
ones, and `start_period` only exempts early *failures* from counting — it does
not delay a false-positive *success* from being believed. Docker's 2 s interval
did not happen to land inside the window in the runs observed, which is
host-load luck, not a guarantee.

**`usher`'s own healthcheck targets `/health/ready`, not `/health`
(`compose.yml:171`).** Plain `docker compose` (no Swarm) never restarts a
container because its healthcheck failed — an unhealthy status only changes
what `docker compose ps` reports and what `depends_on` gates on; `restart:
unless-stopped` triggers on the container's *process* exiting, which a failing
healthcheck alone does not cause. With no restart-loop risk in this shape,
`/health/ready` (database + migration state) is strictly more informative than
`/health` (always 200, checks nothing), and compose has no separate
liveness/readiness pair to keep them apart. There is no `curl`/`wget` in
`python:3.13-slim` and adding either would cut against a small image, so the
check is Python's own `urllib.request` — `urlopen` already raises on any
non-2xx or connection failure, which is already a nonzero exit, so no explicit
try/except is needed where any exception already means "unhealthy".

⚠️ **Nothing in CI verifies HTTP.** `ci.yml`'s `image` job builds the image and
runs exactly one check inside it — that the console bundle is where
`Settings.console_dist_dir` looks (`test -f /app/web/dist/index.html`, `test -d
/app/web/dist/assets`). No workflow starts the app, opens a socket or fetches a
URL; `urllib.request` appears in `compose.yml` and nowhere else. A change to
the healthcheck, the `CMD` or readiness is verified by running the stack
locally or not at all.

## Startup, migrations and `python -m usher`

`Settings.host`/`Settings.port` validated but were read by nothing — the only
way to start the server was the `uvicorn` CLI with hardcoded `--host 0.0.0.0
--port 8000`. `src/usher/__main__.py` (`python -m usher`, what the container's
`CMD` runs after `alembic upgrade head`) fixes this:
`uvicorn.run("usher.api.app:create_app", factory=True, host=settings.host,
port=settings.port)`, the same code path the CLI form uses internally. The
`CMD` stays the module form — that is the spelling whose `exec`/SIGTERM
behaviour was verified against a running container (`exec` so `docker stop`'s
SIGTERM reaches uvicorn directly rather than being swallowed by the wrapping
shell).

Migrations run on container start, verified end to end against a clean volume:
`docker exec ... psql -c '\dt'` showed the five core tables plus
`alembic_version`, and `SELECT tgname FROM pg_trigger WHERE NOT tgisinternal`
showed all three `set_updated_at` triggers — the migration ran for real, not
`create_all`. **That run was M1 and read `a8a0e10ff464`, which is the *first*
revision, not the head.** The head on 2026-09-02 is **`m10b`**
(`m10b_watch_lane_resume.py`, `down_revision = "m10a"`); ask `uv run alembic
heads` rather than quoting a hash out of this file.

**Container-start migration has no distributed lock** — fine at one replica, a
real problem the moment `usher` is scaled past one, at which point migrations
belong in a separate one-shot step; `/health/ready`'s migration-mismatch check
would surface a lost race as a 503 rather than prevent it. Noted in the
Dockerfile's own `CMD` comment, not solved.

## CI: what is pinned, and the pin that failed

`.github/workflows/ci.yml` is the only workflow; its actions, read 2026-09-02:

| action | pin | kind |
|---|---|---|
| `actions/checkout` | `@v7` (4 uses) | floating major |
| `astral-sh/setup-uv` | `@v9.0.0` | exact release |
| `actions/setup-node` | `@v7.0.0` | exact release |
| `actions/upload-artifact` | `@v4` (2 uses: `console-e2e`, `console-visual`) | floating major |

**`@v9` was wrong once and the first real CI run is what said so.** Checking an
action's *releases* answers whether a version exists, not whether the
**floating major tag** does, and those are different objects: `astral-sh/
setup-uv` publishes `v9.0.0` as a release but its moving major tags stop at
`v7` (`v1`…`v7` exist; `v8` and `v9` do not). So `@v9` resolved to nothing and
the job failed in 2 s at *Set up job* — `Unable to resolve action
astral-sh/setup-uv@v9, unable to find version v9` — before `checkout` ran and
before one line of this project's code executed. Every `run:` step below had
been verified locally, which is exactly the half that failure is blind to.
Found 2026-08-02 on the first push to a real GitHub remote; the workflow had
been unexecuted for five milestones.

**So verify a floating tag with `gh api repos/<owner>/<repo>/git/ref/tags/
<tag>`, not against the releases page** — and note that the two floating pins
in the table are exactly the ones that check applies to. An exact release tag
needs no such check and is the stronger pin anyway.

A `.python-version` file (`3.13`) at the repo root exists because of a real gap
found by running the install step, not by inspection: `pyproject.toml`'s
`requires-python = ">=3.13"` has no upper bound, and a bare `uv sync --frozen`
on a machine with no Python preinstalled (verified on a stock `ubuntu:24.04`
container, standing in for a fresh runner) resolved **Python 3.14.6** — newer
than the 3.13.14 every group has developed and verified against. With
`.python-version` present, the identical command resolves `3.13.14`.

**What was and was not reproduced locally, 2026-08-02.** `act` is not installed
on this host and was not added (an emulator whose own correctness is unverified
adds little over not having it). Instead every `run:` step's literal command
was run locally, in order, and all passed. **Those outputs are M1-era and are
quoted only as a record of that run**: mypy said *"Success: no issues found in
67 source files"* (the mypy-override contingency for `usher.db.migrations.*`
was never needed), lint-imports said 4 contracts kept, pytest said 237 passed
at 98% coverage — up from 235 with `src/usher/__main__.py`'s two new unit
tests. On 2026-09-02 the same tree has **266** `.py` files under `src/`, **12**
contracts and **5,747** collected tests. Not reproduced at all: the `setup-uv`
action's own code (its net effect — a working `uv` on `PATH` that obeys
`.python-version` — was checked by installing `uv` via astral's script on a
bare `ubuntu:24.04`, a proxy for a fresh runner but not the literal
`ubuntu-latest` image), and Docker-in-CI for `tests/integration/`.
