# Self-Healing Sync, Slice 1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A push lane stops only when retrying cannot help: it keeps trying through an outage, waits a day on a rejected login and comes back by itself after a crash, and an operator can store a new password.

**Architecture:**
- **`PushSupervisor.run` becomes a loop that only `SourceNotSupported` ends.** The ceiling still logs one ERROR and writes `supports_push = false`. The lane then keeps trying, at most `USHER_PUSH_MAX_BACKOFF_SECONDS` apart.
- **A rejected login waits a day.** `PortAuthFailed` writes the column, logs what to do, and sleeps 86,400 s without returning.
- **`LaneSupervisor` records a crash and restarts it.** `_guard` stamps the time, and the first refresh at least `USHER_PUSH_MAX_BACKOFF_SECONDS` later starts a new lane on a new adapter. The old adapter stays open until then, so the gauge reads 0.
- **`usher set-credentials` stores a new password,** on the model of `usher rotate-secret`: the variables' names on the command line, never the values.

**Tech Stack:** Python 3.13, httpx, SQLAlchemy async over asyncpg, PostgreSQL 17 (testcontainers), loguru, OpenTelemetry metrics, pytest with strict mypy over `tests/`.

**Spec:** `docs/specs/2026-11-05-self-healing-sync-design.md`, **slice 1 only**: "Slice 1: a push lane comes back by itself", and the slice 1 parts of "Testing", "Acceptance" and "Rollout". Read it before any task. It is a point-in-time record, so never edit it. Slices 2 to 5 get their own plans.

## Decisions this plan makes

The spec leaves these to the builder. Each is settled here so no task re-opens it.

1. **`run` is `while True`, and only `SourceNotSupported` returns.** Its final shape, after Task 3:

   ```python
   gate, outage, failures, delivering = _Gate(), _Outage(), 0, False
   while True:
       outage.attempts += 1
       # Read before the connection opens, so `_streak` can tell whether it delivered.
       received = adapter.push_messages_received
       try:
           async with adapter.events() as events:
               await self._gap(source, adapter, gate)
               delivering = await self._note(source, adapter, delivering, outage)
               async for event in events:
                   delivering = await self._note(source, adapter, delivering, outage)
                   outcome = await self._apply(source, adapter, event)
                   if outcome.deferred_to_delta:
                       await self._gap(source, adapter, gate)
       except SourceNotSupported as exc:
           ...  # logs as today, writes False, returns
       except asyncio.CancelledError:
           raise
       except PortAuthFailed as exc:  # before UsherPortError, its parent
           failures, delivering = self._streak(failures, adapter, received), False
           outage.marked = True
           ...  # the rejected ERROR, writes False
           wait = _REJECTED_LOGIN_WAIT_SECONDS
       except UsherPortError as exc:
           failures, delivering = self._streak(failures, adapter, received), False
           ...  # WARNING
           wait = await self._after_failure(source, failures, outage)
       else:
           failures, delivering = self._streak(failures, adapter, received), False
           ...  # WARNING
           wait = await self._after_failure(source, failures, outage)
       await self._sleep(wait)
   ```

   `_after_failure(self, source, failures, outage) -> float`: if `failures == self._max_failures`, set `outage.marked = True`, log the ceiling ERROR and `await self._set_push_available(source, False)`. Return `self._backoff(failures)`. `==` fires once per streak. *Why the sleep sits after the `try`:* a cancel then never arrives inside a handler, and every failure sleeps, including the one at the ceiling.
2. **A day is the module constant `_REJECTED_LOGIN_WAIT_SECONDS: Final = 86_400.0`,** with no setting, no constructor parameter and no jitter. *Why:* the spec names no setting, nothing overrides it, and one lane has no herd to spread.
3. **`_backoff` caps the exponent:** `base = min(self._backoff_seconds * 2 ** min(failures - 1, 20), self._max_backoff)`, then the same jitter. *Why:* it is `Scheduler._back_off`'s spelling.
4. **`_Outage` holds the outage's state** (`__slots__ = ("attempts", "marked")`, for `_Gate`'s reason). `attempts` counts connections tried since the channel last delivered, counted at the top of the loop, so the count includes the attempt that worked. `marked` is set at the ceiling and on a rejected login. `_note(self, source, adapter, was, outage)` writes on a transition as today, then, if the channel is delivering: logs the INFO if `outage.marked`, sets `attempts = 0` and `marked = False`. An ordinary reconnect logs nothing.
5. **The log lines.** None carries a credential: `PortAuthFailed`'s three messages name none.
   - Ceiling ERROR: `"{source}'s push channel failed {count} times in a row; marking it unavailable and still trying, at most {wait:.0f} s apart"`, with `wait=self._max_backoff`.
   - Rejected ERROR: ``"{source}'s login was rejected; marking push unavailable and trying again in a day. Store the new password with `usher set-credentials` and restart Usher: {error}"``.
   - INFO: `"{source}'s push channel is back on attempt {attempts}"`.
   - The two WARNINGs replace `({failures}/{ceiling})` with `({failures} in a row)`, because `7/5` would read wrong.
6. **`LaneSupervisor` gains `clock: Callable[[], float] = time.monotonic`** (keyword) and `self._crashed_at: dict[uuid.UUID, float] = {}`. `_guard`'s `except Exception` records `self._clock()`, adds to the counter, and `logger.exception`s `"the push lane for {source} crashed and restarts at the first refresh at least {wait:.0f} s from now: {error}"`. `CancelledError` re-raises with no record.
7. **`crashed_sources()` is `sorted(self._names[id] for id in self._crashed_at)`.** A `SourceNotSupported` lane is no longer named. *Why:* it did not crash, and the one test pinning it is being split.
8. **`refresh()` branches on the record.** It drops lanes of unwanted sources first (`_stop_lane` also pops the record). Then for each done task: no record → `_release_adapter` as today, staying in `_lanes` and never restarted; a record with `clock() - crashed_at >= settings.push_max_backoff_seconds` → `_restart_lane(pipeline, wanted[id])`; otherwise it is left alone. Then it starts lanes for wanted sources with none.
9. **`_restart_lane` re-spaces first:** `self._crashed_at[source.id] = self._clock()`, then `adapter = await open_adapter(pipeline, source)`. `None` returns, keeping the old adapter and task, so the gauge reads 0. Otherwise `del` the record, take `old = self._open_adapters[source.id]`, `self._launch(source, adapter)`, then `await old.aclose()`. `_start_lane` splits into the open and `_launch(self, source, adapter) -> None`, which awaits nothing. `stop()` clears `_crashed_at`. *Why this order:* a credential row gone or unreadable stays spaced, with no WARNING or stack per refresh and no starving of the sources behind it, and a failing close leaves the new lane running for `_refresh_loop` to log.
10. **The counter is `usher.source.push.crashes`** (unit `"1"`, attribute `source`), module-level in `api/lanes.py` on `metrics.get_meter("usher.push")`, the pattern of `services/push.py:33-34`. No panel and no alert: `Push down` already fires.
11. **`usher set-credentials`:**
    - A subparser with `allow_abbrev=False`. Tripwires `--username` and `--password` (`nargs="?"`, a new action `_RefuseACredentialOnTheCommandLine`, `dest=argparse.SUPPRESS`, `help=argparse.SUPPRESS`), which call `parser.error` without the value. Required `--source` (metavar `NAME`), `--username-env` and `--password-env` (metavar `VAR`).
    - `_parse_without_echoing_unknown_values` gets its own arm: `"unrecognized arguments for set-credentials ({n}, not shown -- on this command an unrecognized value is most likely a password)"`, then as the rotate arm continues. `_dispatch` gets its own `elif` before `else`, which is `serve`.
    - `async def _set_credentials(settings, *, source_name, username_env, password_env) -> None` checks both names with `_ENVIRONMENT_VARIABLE_NAME.fullmatch` and reads both values **before** opening a session. Then `_session_for(settings)`, `build_pipeline(session, settings)`, and `pipeline.sources.list_all()` filtered by name, not `selected_sources`, so a disabled source can be fixed. Zero or several matches → `SystemExit`. Then `pipeline.credentials.put(source.credentials_ref, SourceCredentials(username=..., password=SecretStr(...)), owner_id=source.id)` and `await session.commit()`.
    - **No refusal echoes any argument, `--source` included, because a password can be a legal variable name.** Messages name only the flag, as literals: `"the variable --password-env names is not set or is empty; its name is not repeated here, because a password can look like a variable name"`, `"no source has the name given to --source"`, `"{count} sources have the name given to --source, and this command cannot tell them apart"`.
    - Success prints `usher set-credentials: stored new credentials for {source.name}. Restart Usher: running lanes and the worker's cached adapters hold the old password until then.`, and neither value nor the username.
    - It attempts no login. *Why:* the spec asks for none, and an extra rejection could count against an account that can lock.
12. **The rotation refusal names the new command:** ``"and must be re-entered -- store each with `usher set-credentials` once the new key is in place"``. *Why:* the old advice, `POST /admin/sources`, mints a second source away from the first one's rows.
13. **The test seam: the script alone ends a lane.** `_ScriptSpent(Exception)`, not a `UsherPortError`. `_ScriptedAdapter(source, connections: list[list[SourceEvent] | UsherPortError], *, emit_interval=0.0, hang=False)`; `unbounded` and `max_attempts` go. `events()` does `attempts += 1`, raises `SourceNotSupported` if disabled, raises `_ScriptSpent` on an empty script, then pops: a `UsherPortError` entry is raised, a list opens a connection (`[]` delivers nothing and drops). Helpers: `_run_until_spent(supervisor, adapter, *, timeout=5.0)` (`with pytest.raises(_ScriptSpent): await asyncio.wait_for(supervisor.run(SUPERVISED_SOURCE, adapter), timeout=timeout)`), `_downs(n)` (`[PortUnavailable("down") for _ in range(n)]`), and `_supervisor(...)` gains `sleep: Callable[[float], Awaitable[None]] | None = None`.
14. **Status rows go straight to `✅ landed, acceptance pending` in the slice's own PR.** *Why:* the spec says so, the row is true the moment the PR merges, and a follow-up docs PR's merge would restart production. Task 6's flip to accepted is a docs PR the owner merges.

## Global Constraints

**Scope and safety**

- **Slice 1 only.** No presence pass, no second `Scheduler`, no Alertmanager or rule-file publishing, no weekly walk, no versions pass, no credentials endpoint.
- **Branch:** the controller renames `docs/self-healing-design` to `feat/self-healing-lane` before Task 1. The PR is based on main.
- Never undo with `git checkout`, `restore`, `stash` or `reset`; `reset --soft` is fine. Every plant comes after the task's commit, with a `cp` backup in `/var/tmp/self-healing/`, a `grep` showing it landed, and each restore checked with `diff`.
- **No implementer talks to a real server or runs `usher set-credentials` against production.** Task 6 is the controller's, GETs only. **No credential, token, user id or host enters the repo.**
- **Merging deploys to production and restarts Usher.** The merge is the owner's, and only with no walk running (`docker ps --filter name=usher-prod-sync`). Shared Emby is GETs only.
- **Layering:** `services/` depends only on `domain/` and `ports/`; `lint-imports` enforces it.
- **Secrets are `SecretStr`,** unwrapped at the point of use, and never logged, printed or put in an exception message.

**Writing rules**

- The `guard-prose` hook caps Python prose: a module docstring at 3 lines, a def docstring at 20, a comment block at 5. No dates, hex, "measured", `ADR-` or 🔴 in Python comments.
- **TDD.** Every behaviour change starts with a failing test, run and seen failing for the stated reason.
- **The PRD is updated in the same commit as the behaviour it describes**, stating behaviour, not reasoning. A replaced statement goes; it never stands beside the new one.
- **Never edit an earlier spec or plan.**
- Commit messages read `area: sentence`, with **no trailers and no model identifiers**.
- **A clock in a test starts at `1_000.0`, not 0,** and a threshold gets a fixture standing on the boundary, read off the collaborator.
- **SDD agents all use model `opus`**: implementers, reviewers and fixers alike.

**Commands**

- **Iterate:** `PYTHONDONTWRITEBYTECODE=1 uv run pytest <file> -p no:randomly`
- **Gate,** once per task, before its commit: `uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly`
- **Docs:** `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_docs_currency.py tests/unit/test_docs_pointers.py tests/unit/test_dashboards.py tests/unit/test_prd_10_panel_backing.py`
- **A plant's failure:** `PYTHONDONTWRITEBYTECODE=1 uv run pytest <case> -p no:randomly 2>&1 | grep -E '^E .*<the assertion or its message>'`

## Review Focus

The five inputs the spec implies and no other case reaches, most likely first. Each one's test is a step in the task named.

1. **A deploy during a wait.** A cancel during a backoff or a day's wait ends the lane promptly and writes nothing more. Task 1 `test_cancelling_a_lane_in_its_backoff_ends_it_at_once`; Task 3 `test_cancelling_a_lane_waiting_out_its_day_ends_it_at_once` and `test_a_rejected_login_parks_its_lane_reads_zero_and_stops_at_once`.
2. **A connection delivering only unmapped frames between failures.** The column stays false, and the INFO fires at the first mapped event with the full count. Task 1 `test_frames_that_map_to_nothing_leave_the_column_false_and_the_count_running`.
3. **The credential row gone at restart.** The old adapter stays, the gauge reads 0, the restart is spaced again, and no stack trace comes per refresh. Task 4 `test_a_restart_with_no_stored_credentials_keeps_the_old_adapter_and_is_spaced_again`.
4. **A database failure writing `supports_push`** at the ceiling or on a rejection crashes the lane, which restarts after the spacing rather than being lost. Task 1 `test_a_database_failure_at_the_ceiling_escapes_the_lane`, Task 3 `test_a_database_failure_on_a_rejected_login_escapes_the_lane`, Task 4 `test_a_lane_whose_availability_write_fails_crashes_and_comes_back`.
5. **A password that is a legal variable name, given as `--password-env`, and two sources sharing a name.** Refused, with nothing echoed. Task 2 `test_a_password_that_is_a_legal_name_is_refused_as_unset_and_not_repeated`, `test_a_name_matching_two_sources_is_refused_and_writes_nothing`.

## File map

| File | Task | What changes |
|---|---|---|
| `src/usher/services/push.py` | 1, 3 | `run`'s loop, `_Outage`, `_after_failure`, `_note`, `_backoff`, the constant and the rejected arm |
| `tests/unit/test_services_push.py` | 1, 3 | The seam, the migrated cases, the new cases |
| `src/usher/config.py:333-336`, `.env.example:116-117` | 1 | The setting's description |
| `docs/prd/08-operations.md:208`, `docs/prd/03-sources-and-sync.md:58-61, :80-82` | 1 | A lane keeps trying |
| `.claude/rules/emby-push-and-ingest.md:100-104`, `dashboards/README.md:1157-1165, :1655-1680` | 1 | The same |
| `src/usher/cli.py` | 2 | The command, the refusal's advice |
| `tests/unit/test_cli_set_credentials.py`, `tests/integration/test_set_credentials.py` | 2 | Create |
| `tests/unit/test_cli_errors.py:85`, `tests/unit/test_cli_rotation.py:530-532, :557-558`, `tests/integration/test_rotation.py:686, :737-738` | 2 | The new command's row, the moved pins |
| `docs/guide/command-line.md:39`, `CLAUDE.md:219, :236`, `docs/prd/08-operations.md:133, :197`, `docs/runbooks/rotation.md:190-191` | 2 | The new command |
| `tests/unit/test_adapters_emby_push.py` | 3 | The real rejected path |
| `docs/prd/08-operations.md:207` | 3 | A rejected login |
| `src/usher/api/lanes.py` | 4 | The record, the restart, the counter, the docstrings |
| `src/usher/api/dto/health.py:47-49`, `src/usher/services/reconcile.py:864-865` | 4 | Stale words |
| `tests/unit/test_api_lanes.py` | 1, 3, 4 | The new cases |
| `tests/unit/test_telemetry_metric_names.py:185` | 4 | 47 becomes 48 |
| `docs/prd/10-telemetry-and-dashboards.md:142`, `docs/prd/08-operations.md:208`, `docs/prd/03-sources-and-sync.md:197-199` | 4 | The counter, the crash row, `push_available` |
| `docs/plans/progress.md`, `docs/prd/README.md` | 5, 6 | The status rows |

These stay as they are: `dashboards/alerts/usher.yml:240`'s "The nightly reconciler covers" (slice 2's), `composition.py:159`, `services/sources.py:111, :121`, `services/scheduler.py:130`, `ports/source.py:466`, `services/push.py:375-378`, `docs/runbooks/rotation.md:57, :296`, and `_ScriptedAdapter` in `test_services_playback.py` and `test_api_playback*.py`, which are unrelated classes.

---

### Task 1: A lane keeps trying past the ceiling

**Files:**
- Modify: `src/usher/services/push.py:306-478`, `src/usher/config.py:333-336`, `.env.example:116-117`, `docs/prd/08-operations.md:208`, `docs/prd/03-sources-and-sync.md:58-61, :80-82`, `.claude/rules/emby-push-and-ingest.md:100-104`, `dashboards/README.md:1157-1165, :1655-1680`
- Test: `tests/unit/test_services_push.py`, `tests/unit/test_api_lanes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PushSupervisor.run` returns only on `SourceNotSupported`. In `services/push.py`: `_Outage`, `_after_failure(self, source, failures, outage) -> float`, `_note(self, source, adapter, was, outage) -> bool`. In `test_services_push.py`: `_ScriptSpent`, `_run_until_spent`, `_downs`, `_supervisor(sleep=...)`, used by Task 3. In `test_api_lanes.py`: `meter_reader`, `_DownAdapter`, `_Adapters.down`, used by Tasks 3 and 4.

- [ ] **Step 1: Move the test seam and migrate the existing cases**

Apply Decision 13. The cases at `test_services_push.py:1051, :1082, :1104, :1172, :1179, :1250, :1262, :1283` move to `_run_until_spent` with their values unchanged. `:1209`, `:1237`, `:1337` and `:1357` stay as they are. `grep -n 'unbounded\|max_attempts' tests/unit/test_services_push.py` then prints nothing. These change:

| Case | Script; ceiling | Asserts |
|---|---|---|
| `test_the_failure_counter_is_reset_by_delivery_not_by_connection` | `[[], [], []]`; 3 | `push_connections == 3`, `push_available == [False]`, `sleeps == [5.0, 10.0, 20.0]`. Drop the docstring's "so the reconciler would go on skipping" sentence and the "must not sleep" message |
| `test_a_delivering_channel_resets_the_counter` | `[[i1], [i2], [i3], []]`; 2 | `push_connections == 4`, `push_available == [True, True, True, False]` |
| `test_a_connection_that_delivered_only_messages_resets_the_counter` | `[[_MESSAGE]] * 3`, and that plus `[[]]`; 2 | The first: 3 connections, `push_available == []`. The second: 4, `[False]` |
| `test_the_backoff_doubles_and_is_capped` | `[[]] * 6`; 6, max 40 | `sleeps == [5.0, 10.0, 20.0, 40.0, 40.0, 40.0]`, and its comment says so |
| `test_a_channel_that_ends_quietly_counts_as_a_failure` | `_QuietAdapter` with `[[], []]`; 2 | `push_connections == 2`, `push_available == [False]` |
| `test_a_source_with_no_push_channel_is_marked_and_left_alone` | unchanged | Its docstring's "reaches the ceiling instead of returning" becomes "keeps trying instead of returning" |

- [ ] **Step 2: Write the new cases**

With `i1 = _event("i1")`, backoff 5 unless stated, and the existing jitter stub:

| Case | Script; settings | Asserts |
|---|---|---|
| `test_a_lane_keeps_trying_past_the_ceiling_and_comes_back` | `_downs(7) + [[i1]]`; ceiling 3, max 20 | `push_available == [False, True]`, `sleeps == [5.0, 10.0, 20.0, 20.0, 20.0, 20.0, 20.0, 5.0]`, `gaps == 1`, `applied == [i1]`, `attempts == 9` |
| `test_a_channel_failing_two_thousand_times_raises_nothing` | `_downs(2000)`; ceiling 5, defaults, `timeout=30.0` | `attempts == 2001`, `len(sleeps) == 2000`, `sleeps[-1] == 300.0`, `push_available == [False]` |
| `test_the_ceiling_logs_one_error_each_streak` | `_downs(7) + [[i1]] + _downs(3)`; ceiling 3, max 20 | Exactly two ERROR lines, each exactly `Living Room Emby's push channel failed 3 times in a row; marking it unavailable and still trying, at most 20 s apart`; `push_available == [False, True, False]` |
| `test_a_silent_socket_keeps_trying_and_is_polled_by_the_delta` | `[[]] * 7`; ceiling 3, `clock=_ticks(step=30.0)`, `gap_min_interval_seconds=60.0` | `push_connections == 7`, `gaps == 4` (at 0, 60, 120 and 180: on the boundary), `adapter.supports_push is False`, `push_available == [False]` |
| `test_coming_back_after_the_ceiling_says_which_attempt_it_took` | `_downs(4) + [[i1]]`; ceiling 3 | One INFO line, exactly `Living Room Emby's push channel is back on attempt 5` |
| `test_the_attempt_count_starts_at_the_last_delivery` | `[[i1]] + _downs(3) + [[i2]]`; ceiling 3 | One INFO line, `... is back on attempt 4` |
| `test_an_ordinary_reconnect_says_nothing_about_coming_back` | `[[i1]] + _downs(1) + [[i2]]`; ceiling 3 | Premise `push_available == [True, True]`; no line contains `back on attempt` |
| `test_frames_that_map_to_nothing_leave_the_column_false_and_the_count_running` | `_downs(3) + [[_MESSAGE]] + _downs(1) + [[i1]]`; ceiling 3 | `push_available == [False, True]`, one ERROR, one INFO, `... is back on attempt 6` |
| `test_a_database_failure_at_the_ceiling_escapes_the_lane` | `_downs(3)`; ceiling 3, the writer raises `RuntimeError` | `pytest.raises(RuntimeError)` from `run`, `attempts == 3`. It passes before and after: it pins what the new loop must not catch |
| `test_cancelling_a_lane_in_its_backoff_ends_it_at_once` | `_downs(1)`; ceiling 1, a sleep that sets an `Event` and then waits forever | On entry `push_available == [False]`; cancelling raises `CancelledError`; still `[False]` |

Capture log lines as `test_api_lanes.py:1094` does (`logger.add(lines.append, level="TRACE", serialize=True)`, `logger.remove(sink)`, then `record.message` and `record.level.name` from each JSON line).

In `test_api_lanes.py`, add a `meter_reader` fixture as the other test files have it (`InMemoryMetricReader`, `metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))`, yield), `_DownAdapter` (`events()` raises `PortUnavailable`), `_Adapters.down`, and `test_a_lane_past_the_ceiling_keeps_its_adapter_and_reads_zero`: `push_max_consecutive_failures=1`, with A seeded `supports_push=True` so that the false is this lane's write. `register_push_gauges(supervisor.push_snapshots)`. `_drain` until the stored value is false, then `await supervisor.refresh()`: `running_sources() == ["A"]`, `crashed_sources() == []`, `built["A"]._closes == 0`, `push_available(A's id) is False`, `gauge_readings(reader, "usher.source.push.connected") == [(0.0, {"source": "A"})]`, and `await asyncio.wait_for(supervisor.stop(), 5.0)` returns.

- [ ] **Step 3: Run them and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_push.py tests/unit/test_api_lanes.py -p no:randomly`
Expected: the cases past the ceiling fail with `DID NOT RAISE`, because today's `run` returns there. The cancel case times out waiting for its sleep. The lane-level case fails on `running_sources()`, because the lane has ended and the refresh released its adapter. The cases below the ceiling and the database case pass.

- [ ] **Step 4: Implement in `services/push.py`**

Decisions 1, 3, 4 and 5, without the `PortAuthFailed` arm. Rewrite `run`'s docstring: it stops only for `SourceNotSupported`, keeps trying otherwise, and the gauge reads 0 meanwhile, so it does not look like a working lane. Drop "Returns; never raises a `UsherPortError`" and the "identical to a working one" argument.

- [ ] **Step 5: Run them and see them pass**

Run: the Step 3 command. Expected: all pass.

- [ ] **Step 6: Replace the words that stop being true**

- `config.py:333-336`: ``# PRD 08: at this many failures in a row, mark `supports_push = false` and log one ERROR; the lane keeps trying.``, and keep the sentence on `ge=1`.
- `.env.example:116-117`: `# Equal-jitter reconnect backoff, and the failure count after which the source is` / `# marked supports_push=false while the lane keeps trying.`
- PRD 08's "Push socket drops" row becomes:

  ```markdown
  | Push socket drops | Backoff reconnect; delta reconcile on reconnect, **only when a completed walk gives that delta a cursor** (`USHER_PUSH_GAP_CLOSE`). The same refusal covers a push event deferred to a delta. After `USHER_PUSH_MAX_CONSECUTIVE_FAILURES` (default 5) mark `supports_push = false`, log one ERROR, and keep trying: the wait doubles up to `USHER_PUSH_MAX_BACKOFF_SECONDS` and stays there. **The failure counter resets on delivery, not on connection**, and any message is delivery — a `Sessions` frame that maps to no event included — so the ceiling counts consecutive connections that delivered nothing. The adapter stays open, so the gauge reads 0 and the status route answers `push_available: false` until a connection delivers. The column returns to `true` at the first event that maps to something |
  ```
- PRD 03:58-61: "and a run of reconnects that each stay silent marks push unavailable while the lane goes on reconnecting ([08](08-operations.md))", keeping the 25-minute clause.
- PRD 03:81-82: "If the socket cannot be established the adapter reports `supports_push = false` and the lane keeps retrying; a connection that returns closes the gap with its delta."
- `.claude/rules/emby-push-and-ingest.md:100-104`: "a reset on *events* marks an idle library unavailable", and "so N empty stale-outs in a row still mark it unavailable: N stale limits of unbroken silence is what reads as broken. The lane keeps trying either way; only `SourceNotSupported` ends it."
- `dashboards/README.md:1655-1680`: replace the section `## ⚠️ A silent push channel is given up after ~7.5 minutes, and *Push down* waits 15` with `## A silent push channel keeps trying, and *Push down* fires on it`. It states: the lane marks the source unavailable at the ceiling, goes on reconnecting at most `USHER_PUSH_MAX_BACKOFF_SECONDS` apart and keeps its adapter; the series reads 0; the alert fires after `for: 15m`; `USHER_PUSH_STALE_AFTER_SECONDS=1200` is no longer needed. It quotes no log line.
- `dashboards/README.md:1157-1165`: replace "On this HEAD the same state would produce *no series* — … and stay silent for the one whose lane died holding a socket." with: a lane no longer stops at that ceiling, keeps its adapter and goes on trying, so the same state reads `0`; only a source with no push channel leaves no series; D11's alert is `== 0` and never `absent(...)`, because an `absent()` alert would fire for every source nobody configured. `:816-824` stays true.

- [ ] **Step 7: Run the docs tests and the gate, then commit**

```bash
git add src/usher/services/push.py src/usher/config.py .env.example tests/unit/test_services_push.py tests/unit/test_api_lanes.py docs/prd/08-operations.md docs/prd/03-sources-and-sync.md .claude/rules/emby-push-and-ingest.md dashboards/README.md
git commit -m "push: keep a lane trying past the failure ceiling"
```

- [ ] **Step 8: Plant the three defects the new cases name**

One at a time, each backed up, grepped, scored and restored:
- Drop `min(failures - 1, 20)` from `_backoff`. `test_a_channel_failing_two_thousand_times_raises_nothing` shows `^E .*OverflowError`.
- `failures >= self._max_failures` in `_after_failure`. `test_the_ceiling_logs_one_error_each_streak` fails on its ERROR count.
- Drop `outage.attempts = 0` from `_note`. `test_the_attempt_count_starts_at_the_last_delivery` fails on `attempt 4`.

---

### Task 2: `usher set-credentials`

**Files:**
- Modify: `src/usher/cli.py` (helpers near `:1424`, the refusal at `:1531-1535`, the parser after the rotate block near `:1948`, `_parse_without_echoing_unknown_values` at `:1952-1966`, the dispatch near `:2195`), `docs/guide/command-line.md:39`, `CLAUDE.md:219, :236`, `docs/prd/08-operations.md:133, :197`, `docs/runbooks/rotation.md:190-191`
- Create: `tests/unit/test_cli_set_credentials.py`, `tests/integration/test_set_credentials.py`
- Test: `tests/unit/test_cli_errors.py:85`, `tests/unit/test_cli_rotation.py:530-532, :557-558`, `tests/integration/test_rotation.py:686, :737-738`

**Interfaces:**
- Consumes: `pipeline.sources.list_all()`, `PostgresCredentialStore.put(ref, credentials, *, owner_id)` (the caller commits), `SourceCredentials(username: str, password: SecretStr)`.
- Produces: `usher set-credentials --source NAME --username-env VAR --password-env VAR`, named by Task 3's ERROR.

- [ ] **Step 1: Write the unit cases**

In `tests/unit/test_cli_set_credentials.py`, modelled on `test_cli_rotation.py` (`_merged(argv)`, `configured` and `dispatched` from `tests.unit.commands`, and an unreachable database, which proves the refusal comes first). Each asserts its message and that the canary appears in neither stream:
- `test_a_credential_on_the_command_line_is_refused_and_not_repeated`, parametrized over `--password hunter2`, `--password=hunter2`, `--username someone`, `--username=someone`; exit 2.
- `test_an_abbreviated_flag_is_unrecognised_and_not_repeated`: `--pass`, `--user`, `--password-e`.
- `test_an_unrecognised_value_is_counted_and_not_repeated`.
- `test_a_value_that_is_no_variable_name_is_refused_and_not_repeated`: both flags, with `pa$$w0rd`, `+/==` and a hyphenated value.
- `test_a_password_that_is_a_legal_name_is_refused_as_unset_and_not_repeated`: both flags.
- `test_an_empty_variable_is_refused_like_an_unset_one`.
- `test_it_has_its_own_dispatch_arm`: `dispatched(monkeypatch, arm="_set_credentials", argv=[...])`, whole-shape keywords.

Add to `test_cli_errors.py:85`: `"set-credentials": ["set-credentials", "--source", "A", "--username-env", "U", "--password-env", "P"]`.

- [ ] **Step 2: Write the integration cases**

In `tests/integration/test_set_credentials.py`, copying `tests/integration/test_rotation.py`'s `_main`, env fixture, `get_settings.cache_clear()` and `capsys`:
- `test_it_overwrites_the_stored_row_so_a_new_adapter_logs_in_with_the_new_password`: `FakeEmbyServer(password=NEW)`. The premise, an adapter on the old row raises `PortAuthFailed`; after the command, an adapter on the stored row authenticates.
- `test_it_keeps_the_sources_id_and_its_rows`: a seeded `media_items` row. Afterwards `list_all()` returns the one source, with the same id, and the row reads back unchanged.
- `test_it_prints_neither_value_and_says_to_restart`: the source's name and `Restart Usher` present, both canaries absent from both streams.
- `test_a_name_matching_no_source_is_refused_and_writes_nothing`: the name given appears nowhere in the output, and the stored row reads back unchanged.
- `test_a_name_matching_two_sources_is_refused_and_writes_nothing`: the same, for both stored rows.
- `test_a_disabled_source_can_have_its_credentials_stored`: a source seeded `enabled=False`; afterwards an adapter on the stored row authenticates.

- [ ] **Step 3: Run them and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_cli_set_credentials.py tests/integration/test_set_credentials.py -p no:randomly`
Expected: FAIL on `invalid choice: 'set-credentials'`, or on a missing `_set_credentials`.

- [ ] **Step 4: Implement Decision 11 in `cli.py`**

- [ ] **Step 5: Run them and see them pass**

Run: the Step 3 command. Expected: all pass.

- [ ] **Step 6: Move the rotation refusal (Decision 12), pins first**

`tests/unit/test_cli_rotation.py:558` and `tests/integration/test_rotation.py:686` move to `"usher set-credentials"`. The saturated arms (`test_cli_rotation.py:530-532`, `test_rotation.py:737-738`) keep their not-in lines and gain `"set-credentials" not in`. See the first two fail on `^E .*usher set-credentials`, then change `cli.py:1533-1534` and see them pass. `docs/runbooks/rotation.md:190-191` says the same.

- [ ] **Step 7: Document the command**

- `docs/guide/command-line.md:39`'s row gains `set-credentials`. A paragraph before `## Measuring quality`, on the model of `rotate-secret`'s at `:276-283`, gives the `docker compose exec -e` form and says to restart Usher.
- `CLAUDE.md:219`: 21 becomes 22. After `:236`: `uv run usher set-credentials --source NAME --username-env VAR --password-env VAR  # the variables' NAMES`.
- PRD 08:133 reads "until an operator re-enters it with `usher set-credentials`".
- PRD 08 gains, before `## Failure and degradation`, a section ``### Changing a stored password — `usher set-credentials` ``, stating behaviour: it takes two variables' names, overwrites the stored login, keeps the source's id and rows, works on a disabled source and prints neither value; and Usher must be restarted, because running lanes and the worker's cached adapters hold the old password, and a lane waiting out its day does not notice by itself.

- [ ] **Step 8: Run the docs tests and the gate, then commit**

```bash
git add src/usher/cli.py tests/unit/test_cli_set_credentials.py tests/integration/test_set_credentials.py tests/unit/test_cli_errors.py tests/unit/test_cli_rotation.py tests/integration/test_rotation.py docs/guide/command-line.md CLAUDE.md docs/prd/08-operations.md docs/runbooks/rotation.md
git commit -m "cli: store a source's new password with usher set-credentials"
```

- [ ] **Step 9: Plant: read the values after the first query**

Move the read of the variables below `pipeline.sources.list_all()`. `test_a_password_that_is_a_legal_name_is_refused_as_unset_and_not_repeated` fails on its message, because the unreachable database answers first. Restore and `diff`.

---

### Task 3: A rejected login waits a day

**Files:**
- Modify: `src/usher/services/push.py`, `docs/prd/08-operations.md:207`
- Test: `tests/unit/test_services_push.py`, `tests/unit/test_adapters_emby_push.py`, `tests/unit/test_api_lanes.py`

**Interfaces:**
- Consumes: Task 1's `_Outage`, `_after_failure`, both test seams and `meter_reader`; Task 2's command, by name in the ERROR.
- Produces: `_REJECTED_LOGIN_WAIT_SECONDS`, and `_Adapters.rejecting` in `test_api_lanes.py`.

- [ ] **Step 1: Write the lane's cases in `test_services_push.py`**

| Case | Script; settings | Asserts |
|---|---|---|
| `test_a_rejected_login_waits_a_day_and_does_not_return` | `[PortAuthFailed("rejected"), [i1]]` | `sleeps == [86_400.0, 5.0]`, `push_available == [False, True]`, `gaps == 1`, `applied == [i1]`; the ERROR contains `usher set-credentials` and `restart Usher`; one INFO, `... is back on attempt 2` |
| `test_a_401_that_survives_a_good_login_waits_the_same_day` | `[[i1], [i2]]`, with `_Lane.apply` raising `PortAuthFailed` once | `sleeps == [86_400.0, 5.0]`, `push_available == [True, False, True]` |
| `test_a_login_rejected_every_day_stays_a_day_apart` | `[PortAuthFailed("rejected") for _ in range(3)]`; ceiling 3 | `sleeps == [86_400.0] * 3`; no line contains `times in a row` |
| `test_cancelling_a_lane_waiting_out_its_day_ends_it_at_once` | `[PortAuthFailed("rejected")]`, a sleep that records its seconds, sets an `Event` and waits forever | `seconds == 86_400.0`; `push_available == [False]` before and after the cancel |
| `test_a_database_failure_on_a_rejected_login_escapes_the_lane` | `[PortAuthFailed("rejected")]`, the writer raises `RuntimeError` | `pytest.raises(RuntimeError)` from `run`, `attempts == 1`, no sleep |

- [ ] **Step 2: Write the real path in `test_adapters_emby_push.py`**

`test_a_rejected_login_reaches_the_server_once_until_the_day_has_passed`: a real `EmbyAdapter(..., reauth_cooldown_seconds=0.0)` (the adapter's `clock` does not reach `EmbySession`, so the cooldown runs on real time), `FakeEmbyServer` and `FakePushConnector([connection])` with `connection.deliver(json.dumps(load_emby_fixture("push_library_changed")))`, wired as `tests/fakes/emby_harness.py:110-138`. Nothing in that file builds a `PushSupervisor` today, so build one directly, with recording callables for `apply`, `close_gap` and `set_push_available` and the hook below as `sleep`. Call `server.reject_credentials()` first. The hook asserts `server.authentications == 1`, `adapter.supports_push is False` and `seconds == 86_400.0`, then sets `server.credentials_valid = True`. Afterwards `server.authentications == 2`, the frame was applied and `push_available == [False, True]`. Cancel the lane.

- [ ] **Step 3: Write the lane-level case in `test_api_lanes.py`**

`test_a_rejected_login_parks_its_lane_reads_zero_and_stops_at_once`: `_RejectedAdapter` (`events()` raises `PortAuthFailed`), added through `_Adapters.rejecting`, on a source seeded with `supports_push=True` so that the false is this lane's write. After draining to that write: `running_sources() == ["A"]`, `crashed_sources() == []`, `gauge_readings(reader, "usher.source.push.connected") == [(0.0, {"source": "A"})]` through `register_push_gauges(supervisor.push_snapshots)`, the stored `supports_push is False`, and `await asyncio.wait_for(supervisor.stop(), 5.0)` returns.

- [ ] **Step 4: Run them and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_push.py tests/unit/test_adapters_emby_push.py tests/unit/test_api_lanes.py -p no:randomly`
Expected: the sleeps read `5.0` rather than `86_400.0`, the real path's hook fails on `seconds`, the database case raises `_ScriptSpent` because nothing writes below the ceiling, and the lane-level case times out waiting for the write.

- [ ] **Step 5: Implement Decision 1's `PortAuthFailed` arm and Decision 2**

Log the rejected ERROR of Decision 5 with `error=str(exc)`. Then `await self._set_push_available(source, False)`.

- [ ] **Step 6: Run them and see them pass**

Run: the Step 4 command. Expected: all pass.

- [ ] **Step 7: Replace PRD 08's row**

```markdown
| Source credentials rejected | `GET /admin/sources/{id}/status` reports `authenticated: false`; re-authentication is retried after a cooldown rather than on every call. A push lane marks `supports_push = false` and waits a day before its next try; its gauge reads 0, and its ERROR says to store the new password with `usher set-credentials` and restart Usher. Catalog unaffected |
```

- [ ] **Step 8: Run the docs tests and the gate, then commit**

```bash
git add src/usher/services/push.py tests/unit/test_services_push.py tests/unit/test_adapters_emby_push.py tests/unit/test_api_lanes.py docs/prd/08-operations.md
git commit -m "push: wait a day after a rejected login"
```

- [ ] **Step 9: Plant: move the `PortAuthFailed` arm below `UsherPortError`**

Careful spelling, so ruff stays silent. `test_a_rejected_login_waits_a_day_and_does_not_return` fails on `^E .*86400`. Restore and `diff`.

---

### Task 4: A crashed lane starts again

**Files:**
- Modify: `src/usher/api/lanes.py` (`__init__:58`, `stop:155`, `crashed_sources:196-205`, `push_available:281-287`, `refresh:292-311`, `_start_lane:330`, `_guard:352-370`, `_stop_lane:372`), `src/usher/api/dto/health.py:47-49`, `src/usher/services/reconcile.py:864-865`, `docs/prd/10-telemetry-and-dashboards.md:142`, `docs/prd/08-operations.md:208`, `docs/prd/03-sources-and-sync.md:197-199`
- Test: `tests/unit/test_api_lanes.py`, `tests/unit/test_telemetry_metric_names.py:185`

**Interfaces:**
- Consumes: Task 1's lane, which no longer returns at the ceiling; Task 1's `meter_reader`, `_DownAdapter` and `_Adapters.down` in `test_api_lanes.py`.
- Produces: `LaneSupervisor(..., clock=...)`, `_launch(self, source, adapter) -> None`, `_restart_lane(self, pipeline, source) -> None`, and the counter `usher.source.push.crashes`.

- [ ] **Step 1: Write the cases**

Helpers: `_Clock` (starts at `1_000.0`, `advance(seconds)`) and `_supervisor(..., clock=...)`. The spacing is read off `_settings().push_max_backoff_seconds`, never a literal. `first` below is `fakes.adapters.built["A"]` before the restart; an adapter counts closes in `_closes`.

- `test_a_crashed_lane_restarts_on_a_new_adapter_past_the_spacing`: crash A and `_drain` to `crashed_sources() == ["A"]`. `crashing.discard("A")`, advance spacing − 1, refresh: `built["A"] is first`, `first._closes == 0`, still named, `push_available(id) is False`, gauge `[(0.0, {"source": "A"})]`. Advance 1, on the boundary, refresh: `built["A"] is not first`, `first._closes == 1`, `crashed_sources() == []`, `running_sources() == ["A"]`, `gauge_readings(reader, "usher.source.push.crashes") == [(1.0, {"source": "A"})]`, and an event pushed at the new adapter lands (`_stored`).
- `test_a_lane_that_crashes_on_every_start_stays_spaced_and_reads_zero`: a refresh inside the spacing does not build; past it, builds once; the counter reads 2 and the gauge 0.
- `test_the_second_lane_keeps_ingesting_after_the_first_crashes_and_while_it_restarts`: B ingests after A's crash. Then `first.aclose` parks on an `Event`, `refreshing = asyncio.create_task(supervisor.refresh())`, wait for entry, push at B, `_drain` until stored, `assert not refreshing.done()`, which is the observed overlap, release and await. The assertion is that B ingested, not that it was running. While parked, `running_sources() == ["A", "B"]` as well: the new lane is launched before the old adapter closes.
- `test_a_disabled_sources_crashed_lane_does_not_come_back`: `await fakes.sources.update(source.evolve(enabled=False))`, refresh: `first._closes == 1`, `crashed_sources() == []`, `push_snapshots() == {}`. Past the spacing, refresh: not rebuilt.
- `test_a_restart_with_no_stored_credentials_keeps_the_old_adapter_and_is_spaced_again`: the credential deleted, past the spacing, refresh: same adapter, `_closes == 0`, named, gauge 0, one `NO_CREDENTIALS` WARNING, no exception. A refresh inside the new spacing adds no WARNING. Restored and past it: restarted.
- `test_a_restart_whose_credential_read_raises_is_spaced_again`: `fakes.credentials.get` raises `RuntimeError`: `pytest.raises` from `refresh()`, and a second refresh inside the spacing does not call `get` for A.
- `test_a_lane_whose_availability_write_fails_crashes_and_comes_back`: `_DownAdapter`, `push_max_consecutive_failures=1`, A seeded with `supports_push=True` (the writer no-ops on an equal value), `fakes.sources.update` raising. Named as crashed. With `update` restored and past the spacing, refresh: `running_sources() == ["A"]`, not named.
- `test_stopping_after_a_crash_closes_the_adapter_and_forgets_it`: `stop()` closes the old adapter once and `crashed_sources() == []`.
- `test_a_source_with_no_push_channel_releases_its_adapter_and_is_not_restarted`, which replaces the misnamed `test_a_lane_that_reached_the_failure_ceiling_releases_its_adapter_and_is_named_as_stopped` at `:1107`: `fakes.adapters.prepare` disables push on A only. `_closes == 1` and stays 1 across refreshes past any spacing, `set(push_snapshots()) == {"B"}`, `push_available(A's id) is None`, `crashed_sources() == []`, `running_sources() == ["B"]`, and B's adapter is the same object with `_closes == 0`.

`test_a_crashed_lane_says_so` at `:1080` and `test_a_lane_that_crashes_does_not_take_the_others_down` at `:1003` stay, as do `test_api_health.py`'s. `test_telemetry_metric_names.py:185`: 47 becomes 48.

- [ ] **Step 2: Run them and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_api_lanes.py tests/unit/test_telemetry_metric_names.py -p no:randomly`
Expected: FAIL on the unknown `clock` keyword, and on `len(catalogue) == 48`.

- [ ] **Step 3: Implement Decisions 6 to 10 in `api/lanes.py`**

Docstrings: `crashed_sources` names lanes that crashed and have not yet been restarted; `refresh` says a lane with no push channel releases its adapter without being restarted, and a crashed one restarts past the spacing; `push_available` answers `None` when no adapter is open. The comment at `api/dto/health.py:47-49` says the same as `crashed_sources`. `reconcile.py:864-865` ends: "a push lane crashes and comes back after `USHER_PUSH_MAX_BACKOFF_SECONDS`."

- [ ] **Step 4: Run them and see them pass**

Run: the Step 2 command, plus `tests/unit/test_api_health.py`. Expected: all pass.

- [ ] **Step 5: Update the PRD**

- PRD 10, after `usher.source.push.events`: ``| `usher.source.push.crashes` | counter | source | ✅ self-healing |``.
- PRD 08, a new row after "Push socket drops":

  ```markdown
  | A push lane crashes | `/health/ready` names the source in `lanes.crashed_sources` and its gauge reads 0 until the first refresh at least `USHER_PUSH_MAX_BACKOFF_SECONDS` later starts a new lane on a new adapter. `usher.source.push.crashes` counts them. A source with no push channel is not a crash: its lane ends, its adapter is released, and the status route answers `push_available: null` |
  ```
- PRD 03:197-199: "`push_available` is three-valued: `null` ("not probed") when the source has no push lane, or its lane ended because the source has no push channel, otherwise the live answer — a connection *and* at least one received message *and* a recent one; a crashed lane answers `false` until it restarts. The status check opens no socket."

- [ ] **Step 6: Run the docs tests and the gate, then commit**

```bash
git add src/usher/api/lanes.py src/usher/api/dto/health.py src/usher/services/reconcile.py tests/unit/test_api_lanes.py tests/unit/test_telemetry_metric_names.py docs/prd/10-telemetry-and-dashboards.md docs/prd/08-operations.md docs/prd/03-sources-and-sync.md
git commit -m "lanes: restart a crashed push lane on a new adapter"
```

- [ ] **Step 7: Plant the three defects**

One at a time:
- `>` for `>=` in `refresh`'s spacing test. `test_a_crashed_lane_restarts_on_a_new_adapter_past_the_spacing` fails on the boundary.
- Move `_restart_lane`'s re-spacing below `open_adapter`. `test_a_restart_with_no_stored_credentials_keeps_the_old_adapter_and_is_spaced_again` fails on the WARNING count, and the raising case on its second `get`.
- `await old.aclose()` before `self._launch`. `test_the_second_lane_keeps_ingesting_after_the_first_crashes_and_while_it_restarts` fails on `running_sources()`, not on B's ingest, since B has its own task.

Restore each and `diff`.

---

### Task 5: Status rows and the PR

**Files:**
- Modify: `docs/plans/progress.md`, `docs/prd/README.md`

**Interfaces:**
- Consumes: Tasks 1 to 4.
- Produces: the slice's PR. The merge is the owner's.

- [ ] **Step 1: Run the full gate on the whole branch**

Run: the Gate command above. Expected: green, with its pytest summary line naming a nonzero count.

- [ ] **Step 2: Flip the rows, in both tables**

Tasks 1 to 5 in `docs/plans/progress.md` and the row in `docs/prd/README.md` read `✅ landed, acceptance pending`. Task 6 stays `📋 planned`. Run the docs tests.

```bash
git add docs/plans/progress.md docs/prd/README.md
git commit -m "docs: mark self-healing slice 1 landed, acceptance pending"
```

- [ ] **Step 3: Push and open the PR**

`git push -u origin feat/self-healing-lane`, retrying only on network errors. The body follows `.github/pull_request_template.md`'s headings and ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. It says that acceptance waits on the next natural outage. **Do not merge.**

---

### Task 6: Live acceptance at the next natural outage

The controller's, after the owner has merged. GETs only against Shared Emby; a one-off's output is counts only.

**Files:**
- Modify: `docs/plans/progress.md`, `docs/prd/README.md`, through a docs PR.

**Interfaces:**
- Consumes: the deployed slice and the next outage.
- Produces: raw output on the PR, then the rows flipped.

- [ ] **Step 1: Notice the outage**

`docker exec observability-prometheus-1 promtool query instant http://localhost:9090 'min_over_time(usher_source_push_connected_ratio[7d])'`. An empty result is not evidence: the series must be there. A 0 means an outage happened. Until one does, the row stays as it is.

- [ ] **Step 2: Capture the proof**

- `promtool query range --start … --end … --step=60s http://localhost:9090 'usher_source_push_connected_ratio'`: the series reads 0 and returns to 1.
- `docker inspect -f '{{.State.StartedAt}}' usher-prod-usher-1` predates the 0: no deploy between.
- Loki (`service_name="docker"`): the ceiling ERROR, `still trying`, and the INFO, `is back on attempt`.
- A throwaway one-off under `~/code/usher-deploy/data/bulk/`, through `docker compose run --rm --no-deps -T usher`, printing counts and booleans only: a delta run started in the window, and `supports_push` true.

- [ ] **Step 3: Post the raw output on the PR, and flip the rows**

A docs PR moves Task 6 and the README row to `✅ accepted`. It deploys, so the owner merges it. Remove the throwaway.
