# Comment convention, and the polish pass that adopts it

Four stages, all landing in PR #44, in this order. The order is the point: each
stage reads rationale that the next one deletes.

1. Apply the `/simplify` findings, while the prose explaining why the code is
   shaped this way is still there to read.
2. Cut code prose to the convention.
3. Delete `docs/prd/decisions/`.
4. Cut the PRD to user-facing behaviour.

## Why

`src/usher` is 63.5% comment and docstring by non-blank line, `tests` 41.8%,
across 692 files and ~120,000 prose lines. M10 did not cause this — its added
files measure 62.8% against `main`'s 62.4%. The cause is structural:
`.claude/rules/*.md` are capped at 200 lines and were cut ~85% on 2026-09-02,
and four of them delegate the overflow explicitly (`evals.md:22` — "the module
docstrings are the design record"). Docstrings became the uncapped destination.

The register that results is history, not documentation: `services/scheduler.py`
carries 38 comment lines on `RETENTION_PERIOD = timedelta(days=1)`, including a
dated correction of the comment's own prior claim.

## The convention

From the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
and [PEP 257](https://peps.python.org/pep-0257/). Google's rule is the one that
bites: *"Never describe the code."*

| subject | rule |
|---|---|
| Module docstring | One-line summary. ≤ 3 lines. |
| Function/class docstring | Enough to write a call without reading the body. Args/Returns/Raises only when a caller needs them. ≤ 20 lines. |
| Contiguous `#` block | ≤ 5 lines. Explains why, never what. |
| Forbidden | Dates, row counts, measurement narratives, `🔴` markers, corrections of prior claims, refutations, commit SHAs, alternatives considered, "how we got here". |
| Rationale | Not kept. A rule that must survive is a bare line in `.claude/rules/`, with no justification attached. |

No file-wide ratio cap: a `ports/` ABC is legitimately docstring-dense.

**The convention is not written down as a document.** It is the hook plus the
ruff config. A prose document describing a prose limit is the failure mode this
plan exists to end.

## Enforcement

**`ruff`** gains `"D"` in `[tool.ruff.lint] select` — pydocstyle structure, no
custom code. Currently `["E","F","I","UP","B","SIM","RUF","S"]`.

**`.claude/hooks/guard-prose.sh`**, a fourth hook, `PreToolUse` on `Edit|Write`,
enforcing the caps and the forbidden register. It is a **ratchet**: an edit that
leaves a file over a cap is refused only if it made the file worse. Without
that, the hook refuses the 317 files stage 2 exists to fix.

## Stage 1 — the `/simplify` findings

Four agents; ~50 findings deduplicated to 35. The first two below were reported
independently by three agents each, the third by two; they lead the list.

### Converged

1. ✅ `telemetry.py:539` — the observable-gauge template is a fifth verbatim copy
   (`:329`, `:432`, `:504`, `:652`). One `register_gauge` helper.
2. `db/repositories/backup.py:690-949` — seven per-row merge loops: one round
   trip per row (~14,259) and the SQL string plus its `TextClause` rebuilt
   inside each loop. Set-based `unnest` statements, hoisted constants, one
   helper for what remains.
3. `scripts/measure_*.py` — the `main()` bar/secrets/redaction preamble is four
   copies and has drifted: three redact `str(exc)` where the original redacts
   `format_exc()`, losing the traceback. One `run_measurement`.

### Reuse

4. `services/backup.py:276` — third hand-rolled atomic scratch-write, missing
   the `fsync` the other two take, PID suffix where they use `uuid4`.
5. `services/restore.py:358` — damaged-gzip exception set, second spelling.
6. `scripts/measure_source_lane.py:471,694` — quiet-host check, fourth copy,
   missing the settle sleep the other three take.
7. `scripts/measure_source_lane.py:628` — `Timing` → dict hand-typed; three
   sites want `dataclasses.asdict`.
8. `scripts/*` — `build_session(...)` triplicated with placeholder credentials.
9. ✅ `tests/integration/` — `_column_set`/`_index_set` verbatim copies; 26 copies
   of the `sessions` fixture; `_scratch`/`_drop`; the Grafana panel walk ×3; the
   CLI dispatch test ×4 and its env helper ×5.

### Simplification

10. `services/restore.py:152` — `RestoreReport` shreds `TableOutcome` into four
    parallel maps and rebuilds it; `_apply` is typed `Any` to dodge the import.
11. `scripts/audit_bounded_columns.py:1942` — seven reading-independent scans
    re-run per pair; ~3× off a 23.5 s guard by hoisting them.
12. `ports/repository/llm_call.py:173` — `list_since` has no caller in `src/`,
    and ships `SELECT *` unbounded.
13. `db/backup_manifest.py:212` — `BackupEntry.restore` is derived state
    validated against the mapping it came from. A property.
14. `scripts/measure_source_lane.py:430` — `_overlap_table` dead, two fields
    unread, a bare `_ = _CPU_SETTLE_SECONDS` discard.
15. `services/scheduler.py:628,166` — test-only accessors; two parallel backoff
    dicts that must be cleared together.
16. `ports/repository/search_query.py:95` — `surface` derivable from `tier`.
17. `ports/repository/_references.py:12` — paragraph repeated verbatim at `:38`.
18. `scripts/measure_source_latency.py:804` — three injected seams with no
    caller; the reuse they were built for declined them.

### Efficiency

19. `api/routers/search.py:364` — a `users` SELECT per keystroke, paid on the
    short-`q` arm and when analytics is off.
20. `db/models/search.py` — no index on `title_neighbors.computed_at`; the
    scheduler full-scans 3.3M rows ~288×/day to learn a job is not due.
21. `db/repositories/title.py:140`, `episode.py:192` — the natural-key ladder
    runs all three joins unconditionally; lazy `COALESCE` SubPlans instead.
22. `services/search.py:1595` — two transactions and two WAL flushes per
    keystroke.
23. `services/restore.py:263` — the decoded row list rescanned once per table.
24. `db/repositories/search.py:743` — `SELECT DISTINCT model_name` scans ~270 MB
    of inline vector heap.
25. `services/backup.py:254` — the carried set materialised whole, then gzipped
    synchronously on the event loop.
26. `services/similar.py:768` — two scopes per due tick.
27. `db/backup_identity.py:292` — the reference list deduplicated twice.

### Altitude

28. ✅ `cli.py:737` — the `usher work` daemon is a hand-copied
    `LaneSupervisor._run_worker`, guarded by an AST test asserting the two
    copies still look alike. One loop in `services/jobs.py`.
29. `config.py:751` — `USHER_SEARCH_SUGGEST_ANALYTICS` is a knob over a
    synchronous write on the request path, shipped `false`, so the milestone's
    analytics feature is inert. Buffer or enqueue in `SearchAnalytics`.
30. `services/reconcile.py:164` — the sync failure *kind* is a magic prefix on a
    free-text column read back by substring. An `error_code` column.
31. `services/similar.py:800` — `ScheduledJob` has no "declined" outcome, so a
    refusal counts as work, pollutes the duration histogram and never backs off.
32. ✅ `domain/title.py:111` — `allow_inf_nan=False` on one field; belongs on
    `DomainModel.model_config`, which closes three more.
33. `api/routers/images.py:284` — the port-error mapping is a per-route ladder,
    so `PortRateLimited` and `PortAuthFailed` escape as bare `500 text/plain`.
34. ✅ `composition.py:1029,1066` — two scope factories differing only in whether
    they commit, stated only in prose.
35. `services/scheduler.py:732` — the retention drain's termination is
    guaranteed by a `Settings` validator two layers away.

Items 29, 30, 31 and 33 change behaviour and are outside `/simplify`'s remit.
They are included because every reviewer finding gets fixed here. Each needs a
failing test first.

## Stage 2 — code prose

692 files, staged `src/usher` → `scripts` → `tests`, one agent per slice in its
own worktree under `~/code/.worktrees/usher-m10/<slice>/`, merged `--no-ff` with
the gate green after each merge.

Nothing is relocated. Prose is deleted, not moved. Target is the convention
above, not a percentage.

`pyproject.toml` is in scope: the `extend-exclude` comment is 30 lines and the
mypy override comment is longer.

## Stage 3 — delete `docs/prd/decisions/`

48 files, 12,378 lines, removed outright. Nothing is folded into `CLAUDE.md`.

Dead pointers to strip: 3 in `CLAUDE.md`, 60 in `.claude/rules/`, 3 in
`docs/runbooks/`. The 575 in `docs/prd/` and 1,452 in code die with stages 2
and 4 rather than needing separate edits — of 1,513 ADR citations in
`src`/`tests`/`scripts`, only **61 are in executable code**; the rest are inside
prose already being deleted.

Stripping the pointers shortens `CLAUDE.md` and the rules files. It does not add
to them.

## Stage 4 — the PRD

`docs/prd/` is 12 files and 13,219 lines. Target: a concise statement of
user-facing behaviour per feature. No rationale, no measurements, no
alternatives, no implementation notes.

## Out of scope: `docs/plans/` and `docs/specs/`

Untouched, by decision — 15 files and 98,172 lines of plans, 6 files and 2,177
of specs. They are frozen history and are read as history.

The consequence is that the 1,009 ADR citations in `docs/plans/` go stale when
stage 3 lands. Accepted: a dangling pointer inside a completed milestone plan
misleads nobody, because the document it sits in is already a record of a past
state rather than a description of the current one. The in-flight M10 plan is
the one exception worth watching.

## Risks

- **A failed experiment gets re-run.** ADR-0002 records that the Meilisearch
  gate was run on 2026-08-03 and failed. Deleting it does not delete the
  temptation. Same class: "there is no honest conversion between embedding
  widths" — without it, someone writes a conversion migration and corrupts every
  vector.
- **Rules lose their defence.** `abc.ABC` over `Protocol`, `.evolve()` over
  `model_copy`, UUIDv7 identity and the Emby containment rule all look like
  mistakes to a competent reader. They survive as bare lines in `.claude/rules/`
  with nothing behind them, so a challenge to any of them has no answer.
- Both were raised and accepted. Recorded here because this file is itself
  scheduled for deletion once M10 closes, which is the appropriate lifetime for
  the record.
- **PR size.** #44 is 68,536 insertions; the four stages remove on the order of
  180,000 lines. No reviewer or review tool will process the diff whole, so
  per-slice commits have to carry the review.
- **The gate under load.** `npm run verify` needs `--maxWorkers=3` to be
  trustworthy when agents run concurrently.
