---
paths:
  - "docs/**/*.md"
---

# Maintaining the PRD and specs

Loaded when you open a **Markdown** file under `docs/` — the trigger is
`docs/**/*.md`, not `docs/**`. The two non-Markdown artefacts in this tree,
`docs/evals/bars.toml` and `docs/evals/ledger.jsonl`, therefore do **not** load
this file; they load `.claude/rules/evals.md`, whose `docs/evals/**` covers
every extension and whose rules (a bar is hashed and never moved; the ledger is
append-only) are the ones that actually govern them. Nothing here applies to
either. The standing obligation to keep the PRD current lives in `CLAUDE.md`;
this file is the mechanics.

## Documentation map

```
docs/prd/
├── README.md                        index + status table + conventions
├── 00-overview.md                   vision, goals, non-goals, glossary
├── 01-architecture.md               layering, ports, repo layout, stack
├── 02-data-model.md                 entities, identity, enrichment tiers
├── 03-sources-and-sync.md           adapters, push events, priority queue
├── 04-catalog-bootstrap.md          bulk datasets, licensing
├── 05-search-and-similarity.md      FTS, embeddings, similarity
├── 06-rows-and-recommendations.md   Row/RowProvider, LLM curation
├── 07-client-api.md                 HTTP surface, SSE, playback
├── 08-operations.md                 config, secrets, failure, testing
├── 09-roadmap.md                    milestones
└── 10-telemetry-and-dashboards.md   instrumentation, Grafana

docs/specs/                          point-in-time designs for implementation
docs/plans/                          one file per milestone or eval phase,
                                     plus progress.md's four status tables
docs/evals/                          run write-ups — and bars.toml /
                                     ledger.jsonl, which are `evals.md`'s
```

## PRD vs spec

`docs/prd/` says **what and why**, and evolves with the project.
`docs/specs/` is a **point-in-time** design handed to an implementation plan.

**When they disagree, the PRD is authoritative and the spec is stale.** Do not
edit an old spec to match — specs are historical records of what was planned.
Write a new spec if a new plan is needed.

## When to update what

| When you… | Do this |
|---|---|
| Change a design decision during implementation | Update the relevant `docs/prd/NN-*.md` section |
| Reverse an earlier decision | **Update the section that states it and add the new evidence** — never silently contradict it |
| Add or complete a section | Update the status table in `docs/prd/README.md` |
| Land a task from a plan | Update the status cell in **both** tables. `docs/plans/progress.md` and `docs/prd/README.md`'s implementation-plan table each carry one for every plan, and `test_docs_currency.py` checks only that a plan is *named* by them, never that they agree — so the two drifted to `✅ all six landed` against `📋 planned, nothing landed` for the same branch, in one commit, on 2026-08-25 |
| Write a plan file for a spec that has no status table yet | Give it **its own level-2 heading and table** in `docs/plans/progress.md` naming that spec, and add a row to `docs/prd/README.md`'s implementation-plan table. **Never a row under an existing heading** — that heading names a different spec, so the row makes it false in order to satisfy a check about documentation being true. A plan that really is a *milestone* of the v1 design is the exception and belongs in the milestone table. `tests/unit/test_docs_currency.py` reads every table as one union and fails if a plan file is named by none of them |
| Discover a load-bearing fact (rate limit, dataset size, API behaviour) | Record it with its source in the relevant section |
| Learn something that invalidates a stated fact | Correct it and say so — stale "verified" facts are worse than none |

**The status tables can be green, mutually consistent and wrong together.** Both
E1 rows — `docs/plans/progress.md`'s and `docs/prd/README.md`'s — read *"🔨 in
progress — 15 tasks planned; Task 1 landed"* for a fortnight after PR #45 merged
the whole phase branch, and they **agreed with each other** throughout, so a
check comparing them would have passed. Nothing compares either to the tree.
When you land a task, the cell is the work — the row being present is only what
the test can see. (Both were corrected 2026-09-02.)

## Conventions

- **One concern per file.** If a section starts covering two subsystems, split it.
- **Mark incompleteness explicitly** — ⏳ not yet designed, 🔶 provisional. An
  unmarked section reads as settled when it isn't.
- **Verified facts carry their source.** Dataset sizes, rate limits, and API
  behaviours in this PRD were measured or read from primary docs. Keep the
  citation when you copy the number.

## Before changing a subsystem

Read its PRD section first.

## Housekeeping

After editing docs, check that internal links still resolve:

```bash
uv run pytest tests/unit/test_docs_pointers.py
```
