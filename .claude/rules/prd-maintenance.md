---
paths:
  - "docs/**/*.md"
---

# Maintaining the PRD and specs

Loaded when working with anything under `docs/`. The standing obligation to keep
the PRD current lives in `CLAUDE.md`; this file is the mechanics.

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
├── 10-telemetry-and-dashboards.md   instrumentation, Grafana
└── decisions/0001-NNNN              ADRs — the *why* behind contested calls

docs/specs/                          point-in-time designs for implementation
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
| Make a decision someone could reasonably dispute | Add an ADR in `decisions/`, link it from the section |
| Reverse an earlier decision | **Update the existing ADR's status and add the new evidence** — never silently contradict it |
| Add or complete a section | Update the status table in `docs/prd/README.md` |
| Discover a load-bearing fact (rate limit, dataset size, API behaviour) | Record it with its source in the relevant section |
| Learn something that invalidates a stated fact | Correct it and say so — stale "verified" facts are worse than none |

## Conventions

- **One concern per file.** If a section starts covering two subsystems, split it.
- **Reasoning goes in `decisions/`, not inline.** PRD sections state the outcome
  and link to the ADR. This is what stops settled arguments being re-litigated.
- **Mark incompleteness explicitly** — ⏳ not yet designed, 🔶 provisional. An
  unmarked section reads as settled when it isn't.
- **Verified facts carry their source.** Dataset sizes, rate limits, and API
  behaviours in this PRD were measured or read from primary docs. Keep the
  citation when you copy the number.
- **ADR format:** context → decision → consequences → evidence. Short is fine.
  Write one only when the call was contested — where a reasonable person would
  choose differently, or where the reasoning would otherwise be lost.

## Before changing a subsystem

Read its PRD section first. Several ADRs record evidence that reversed an
initial instinct — notably
[0001](../../docs/prd/decisions/0001-abc-over-protocol.md) (ABCs over Protocols)
and [0002](../../docs/prd/decisions/0002-postgres-first-search.md)
(Postgres-first search). Re-deriving those from scratch wastes the work already
done.

## Housekeeping

After editing docs, check that internal links still resolve:

```bash
python3 - <<'EOF'
import re, pathlib
bad = []
for md in pathlib.Path("docs").rglob("*.md"):
    for link in re.findall(r'\]\(([^)#][^)]*\.md)\)', md.read_text()):
        if not (md.parent / link).resolve().exists():
            bad.append(f"{md}: {link}")
print("\n".join(bad) if bad else "OK")
EOF
```
