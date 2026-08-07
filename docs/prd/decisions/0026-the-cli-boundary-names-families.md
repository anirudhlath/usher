# ADR-0026 — The CLI's error boundary names families, and `Exception` is not one of them

**Status:** Accepted — extends [08](../08-operations.md)

## Context

M7's smoke test recorded a finding it declined to fix: `usher
bootstrap-status` and `usher sync-status` against an unreachable database
printed **sixty lines of asyncpg, greenlet and SQLAlchemy frames** and exited
1. The exit code was right. The operator's entire actionable information was
the last line, `ConnectionRefusedError: [Errno 111] Connect call failed`, and
the fifty frames above it are library code nobody can act on.

**The obvious fix is one line and it is wrong.**

```python
except Exception as exc:
    raise SystemExit(f"usher: {exc}") from exc
```

That passes every case anybody would think to write about presentation. It
also collapses `AttributeError: 'NoneType' object has no attribute 'id'` —
a bug in row composition, reproduced by nothing the operator controls — to a
single line, and the traceback that was the bug report is gone. The failure
is silent: the CLI looks *better* right up until the first real defect, and
then the report that arrives is one sentence.

So the question this ADR settles is not *whether* to have a boundary. It is
what a boundary is allowed to catch.

## Decision

**`main` has exactly one `try`, and it catches enumerated families.**

`cli.OPERATOR_ERRORS` is `(OSError, SQLAlchemyError, httpx.HTTPError)`, with
`ValidationError` handled separately for the reason below. A family belongs
in the tuple when an operator can act on it: start the database, fix the URL,
reconnect the network, correct the `.env`. **Everything else keeps its full
traceback**, because a stack is the right output for a failure whose audience
is a developer.

`usher --traceback <command>` re-raises. The boundary is allowed to swallow a
stack *because* the stack is one flag away.

**Three supporting calls, each of which had a plausible alternative:**

- **`OSError` is in the tuple, and it is load-bearing.** asyncpg lets a
  refused TCP connection out **unwrapped**, so the natural spelling —
  `except SQLAlchemyError`, a database error boundary for a database error —
  misses the exact case this whole thing exists for. `SQLAlchemyError` stays
  because it catches the other half: a missing table from an `alembic upgrade
  head` that never ran.
- **A settings failure is redacted, and `--traceback` does not reopen it.**
  See Evidence: pydantic's message carries the rejected value, and for
  `USHER_DATABASE_URL` that value is a DSN with a password in it.
- **Ctrl-C exits 130 with one word.** `usher bootstrap` is a multi-hour
  download an operator is *expected* to interrupt; a `KeyboardInterrupt`
  traceback through `asyncio.run` reads as the run failing rather than as
  their own decision.

## Consequences

**Gained:**

- **A failure an operator can fix reads like one.** Two lines instead of
  sixty, on the two commands they run first when something is wrong — and on
  the other twelve, which had the same defect and were never reported because
  nobody got that far.
- **A bug still reads like a bug.** The one property the obvious fix
  destroys, kept and pinned by a case.
- **A credential stopped being printed.** Not the goal, and the most valuable
  thing in the change.

**Given up:**

- **The stack costs a second invocation.** An operator diagnosing something
  genuinely strange runs the command again with `--traceback`. Acceptable
  because it is one flag and the message says so; unacceptable if the message
  did not, which is why naming the flag is a case of its own.
- **The families are a list somebody has to extend.** A new failure mode that
  is nobody's bug and nobody's family arrives as a traceback until the tuple
  learns about it. That is the right default direction: an unrecognised
  failure showing too much is recoverable, showing too little is not.

**Also:**

- **The shape is asserted by AST as well as by behaviour.** Every behavioural
  case here passes equally well against one `try`/`except` pair per command —
  which is the shape that rots, because the *next* command is written by
  copying an arm and not the handler. M8's `usher curate` is that next
  command, and it inherited the boundary by adding a `_dispatch` arm and a row
  in the argv table, with no handler of its own. A case walks `main`'s body and
  requires exactly one `Try`, with `get_settings`, `configure_telemetry` and
  `_dispatch` all inside it. One case about this implementation, one about the
  next — the same pairing [0025](0025-rows-build-sequentially.md) uses.
- **`SystemExit` passes through untouched**, and that is free rather than
  arranged: it is a `BaseException` and the handlers name only `Exception`
  subclasses. Pinned anyway, because "free" stops being true the moment
  somebody widens the tuple — and five places in `cli.py` exit with a message
  chosen for the failure it describes: `_as_uuid`, the semantic-search guard,
  `similar`'s cross-argument rule, and both of M8 `usher curate`'s (no LLM
  configured, and a generation that did not happen). It was three when this
  ADR was written; the mechanism is what the bullet is about, and the count is
  restated rather than left stale.
- **The parametrised case runs over the parser's own subcommand list**, so a
  command added without a row in the table fails rather than quietly sitting
  outside the boundary.

**Rejected:**

- **`except Exception`.** The whole subject of this ADR. It passes every case
  in `tests/unit/test_cli_errors.py` except
  `test_a_programming_error_keeps_its_traceback`, which is why that case
  exists.
- **A `Settings` field instead of a flag.** `--traceback` changes how one
  invocation presents itself; it does not configure a deployment.
  [08](../08-operations.md) already retracted a setting added ahead of its
  mechanism, and this would be one added *behind* its mechanism, which is no
  better.
- **Per-command handling.** Rejected on the shape argument above, and
  measurable rather than aesthetic: the finding named two commands, and all
  fourteen the CLI advertised on **2026-08-05** had the defect. Stated with
  its date because it is a measurement rather than a count to keep current —
  the parser advertises fifteen since M8's `usher curate`, and every one of
  them is still inside the one boundary.
- **Truncating or reformatting the exception's own message.** The message is
  the operator's information. The boundary drops the *stack* and keeps the
  message intact — except for a rejected settings value, which is the one
  thing it removes on purpose.

## Evidence

Reproduced directly on **2026-08-05** against the shipped code, before the
change:

| | before | after |
|---|---|---|
| `usher bootstrap-status`, database down | 60-line traceback, exit 1 | 2 lines, exit 1 |
| propagated exception type | **bare `ConnectionRefusedError`**, not a SQLAlchemy type | unchanged; caught by `OSError` |
| `USHER_DATABASE_URL=mysql://admin:<pw>@db/usher` | **the password, in the message** | `database_url: … must use the postgresql+asyncpg:// driver` |
| `USHER_SECRET_KEY` too short | **the key, in the message** | `secret_key: Value should have at least 32 items after validation, not 4` |

🔴 **The leak is the reason this stopped being a presentation change.**
pydantic v2 renders a `ValidationError` with `input_value=…` attached, so the
value that failed validation is *in the message*, and both of those fields
are `SecretStr` in `Settings` specifically so their values never reach a log
line or an exception. It is the same defect `usher.api.errors` was written to
prevent on the 422 path ([08](../08-operations.md)), one surface over, on the
surface an operator is most likely to paste into an issue — and it survived
four milestones because a traceback is not where anybody looks for a leak.

`cli._settings_problem` keeps `loc` and `msg`, drops `input`, and scrubs the
rejected value out of `msg` as well, so a validator that interpolates its own
input cannot quietly reopen this. `--traceback` does not re-raise it: the
stack is six pydantic frames that diagnose nothing, so the only thing
re-raising would add is the credential.

Two mutations were applied in place and both were killed:

| mutation | killed by |
|---|---|
| `except OPERATOR_ERRORS` → `except Exception` | `test_a_programming_error_keeps_its_traceback` |
| `_settings_problem` returns pydantic's own message | three cases, including `test_the_traceback_flag_does_not_reopen_the_settings_leak` |

30 cases in `tests/unit/test_cli_errors.py`; suite 3,247 passed / 5 skipped.

## Uncertainty

⚠️ **`OPERATOR_ERRORS` is a claim about today's failure modes, and it will be
incomplete.** The families were chosen from what the CLI actually does — one
database, one HTTP client, one settings source — and a milestone that adds a
subprocess, a message broker or a filesystem watcher adds a family with it.
The tuple is public and carries its own comment for that reason.

⚠️ **`str(exc)` on a SQLAlchemy error includes the statement and its bound
parameters.** No credential reaches a bound parameter in this system — source
credentials are read through the encrypted-at-rest path and never appear in a
query — but that is a property of today's queries rather than a control. If a
later milestone parameterises a query with anything sensitive, this message is
one of the two places it would surface, and the other one already has a
handler.

**What this ADR really guards against is the one-line fix.** `except
Exception` will look like an improvement to whoever writes it, pass review,
pass the ordinary suite, and go unnoticed until the first defect it hides.
That is why the refusal lives in a numbered decision and in a case rather than
in a comment above the handler.
